"""Flask web UI for CARLA 3D Scene Generator with SSE streaming."""

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue, Empty

from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from flask_cors import CORS

from config import PipelineConfig, PROJECT_ROOT, get_claude_auth_env, RENDERS_DIR, WORKSPACE_DIR
from pipeline import run_pipeline, PipelineEvent
from incremental_builder import IncrementalBuilder
from scene_editor import get_scene_state, build_edit_prompt, execute_edit_plan

log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "carla-3d-opus-dev")

# In-memory job tracking
jobs = {}  # job_id -> {status, events_queue, result, ...}

CARLA_PYTHON = "/home/sejain/miniconda3/envs/carla37/bin/python"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def generate_scene():
    """Start a scene generation pipeline."""
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    model = data.get("model", "sonnet")
    job_id = str(uuid.uuid4())[:8]

    # Create job
    jobs[job_id] = {
        "id": job_id,
        "prompt": prompt,
        "model": model,
        "status": "running",
        "events": Queue(),
        "result": None,
        "created_at": time.time(),
    }

    # Run pipeline in background thread
    t = threading.Thread(target=_run_pipeline_bg, args=(job_id, prompt, model), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/build", methods=["POST"])
def build_scene():
    """Build a scene directly from a scene_plan.json (bypass agent pipeline).

    Accepts either:
      - {"plan": {...}}  -- inline scene plan dict
      - {"plan_path": "/path/to/scene_plan.json"}  -- path to plan file
    """
    data = request.json or {}
    plan = data.get("plan")
    plan_path = data.get("plan_path")

    if not plan and not plan_path:
        return jsonify({"error": "plan or plan_path is required"}), 400

    # Load plan from file if path provided
    if not plan and plan_path:
        plan_file = Path(plan_path)
        if not plan_file.exists():
            return jsonify({"error": f"plan file not found: {plan_path}"}), 404
        try:
            plan = json.loads(plan_file.read_text())
        except Exception as e:
            return jsonify({"error": f"failed to parse plan file: {e}"}), 400

    job_id = str(uuid.uuid4())[:8]

    jobs[job_id] = {
        "id": job_id,
        "prompt": "(direct build)",
        "status": "running",
        "events": Queue(),
        "result": None,
        "created_at": time.time(),
    }

    t = threading.Thread(target=_run_build_bg, args=(job_id, plan), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/events/<job_id>")
def stream_events(job_id):
    """SSE endpoint for streaming pipeline events."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    def event_stream():
        while True:
            try:
                event = job["events"].get(timeout=30)
                if event is None:  # Sentinel for end
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/job/<job_id>")
def get_job(job_id):
    """Get job status and result."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({
        "id": job["id"],
        "prompt": job["prompt"],
        "status": job["status"],
        "result": job["result"],
    })


STREAM_PORT = int(os.environ.get("STREAM_PORT", 5556))


@app.route("/proxy/stream")
def proxy_stream():
    """Proxy the MJPEG stream from carla_stream.py through Flask's port.

    This allows the UI to work even when only one port (5555) is exposed
    (SSH tunnel, firewall, reverse proxy, etc.).
    """
    import socket

    def generate():
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", STREAM_PORT), timeout=10)
            sock.sendall(b"GET /stream HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            # Skip HTTP response headers
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = sock.recv(4096)
                if not data:
                    return
                buf += data
            # Yield everything after the headers
            _, body = buf.split(b"\r\n\r\n", 1)
            if body:
                yield body
            # Stream the rest
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                yield data
        except GeneratorExit:
            pass
        except Exception as e:
            log.warning("Stream proxy error: %s", e)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/proxy/info")
def proxy_info():
    """Proxy stream info endpoint."""
    import urllib.request
    try:
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{STREAM_PORT}/info", timeout=5
        )
        data = req.read()
        return Response(data, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/control", methods=["POST", "OPTIONS"])
def proxy_control():
    """Proxy camera control commands to the stream server."""
    if request.method == "OPTIONS":
        return "", 200
    import urllib.request
    try:
        body = request.get_data()
        req = urllib.request.Request(
            f"http://127.0.0.1:{STREAM_PORT}/control",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read()
        return Response(data, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/snapshot")
def proxy_snapshot():
    """Proxy snapshot endpoint."""
    import urllib.request
    try:
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{STREAM_PORT}/snapshot", timeout=10
        )
        data = req.read()
        content_type = req.headers.get("Content-Type", "image/jpeg")
        return Response(data, mimetype=content_type)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/images")
def list_images():
    """List all rendered images."""
    images = []
    if RENDERS_DIR.exists():
        images = sorted([f.name for f in RENDERS_DIR.glob("*.png")])
    return jsonify({"images": images})


@app.route("/renders/<path:filename>")
def serve_render(filename):
    """Serve rendered images."""
    return send_from_directory(str(RENDERS_DIR), filename)


@app.route("/api/control", methods=["POST"])
def control_scene():
    """Control the CARLA scene (weather, camera, spawn/remove objects)."""
    data = request.json or {}
    action = data.get("action", "")

    if action == "load_map":
        map_name = data.get("map", "Town10HD_Opt")
        result = _run_carla_command(f"""
import carla, json, time
client = carla.Client('localhost', 2000)
client.set_timeout(60.0)
world = client.load_world('{map_name}')
time.sleep(3.0)
m = world.get_map()
print(json.dumps({{"success": True, "map": m.name}}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "weather":
        preset = data.get("preset", "ClearNoon")
        result = _run_carla_command(f"""
import carla, json
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()
weather = getattr(carla.WeatherParameters, '{preset}', carla.WeatherParameters.ClearNoon)
world.set_weather(weather)
print(json.dumps({{"success": True, "weather": "{preset}"}}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "camera":
        x = data.get("x", 0)
        y = data.get("y", 0)
        z = data.get("z", 20)
        pitch = data.get("pitch", -30)
        yaw = data.get("yaw", 0)
        result = _run_carla_command(f"""
import carla, json
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()
spectator = world.get_spectator()
t = carla.Transform(
    carla.Location(x={x}, y={y}, z={z}),
    carla.Rotation(pitch={pitch}, yaw={yaw}, roll=0)
)
spectator.set_transform(t)
print(json.dumps({{"success": True, "camera": {{"x": {x}, "y": {y}, "z": {z}, "pitch": {pitch}, "yaw": {yaw}}}}}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "snapshot":
        # Capture a snapshot from current camera position
        result = _run_carla_command(f"""
import carla, numpy as np, json, time
from PIL import Image

client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()

spectator = world.get_spectator()
cam_transform = spectator.get_transform()

bp_lib = world.get_blueprint_library()
camera_bp = bp_lib.find('sensor.camera.rgb')
camera_bp.set_attribute('image_size_x', '1280')
camera_bp.set_attribute('image_size_y', '720')
camera_bp.set_attribute('fov', '90')

camera = world.spawn_actor(camera_bp, cam_transform)
images = []
camera.listen(lambda img: images.append(img))
time.sleep(2.0)

if images:
    img = images[-1]
    arr = np.frombuffer(img.raw_data, dtype=np.uint8).reshape(img.height, img.width, 4)
    pil_img = Image.fromarray(arr[:, :, [2,1,0]])  # BGRA -> RGB
    fname = 'snapshot_' + str(int(time.time())) + '.png'
    pil_img.save('{str(RENDERS_DIR)}/' + fname)
    print(json.dumps({{"success": True, "image": fname}}))
else:
    print(json.dumps({{"success": False, "error": "No image captured"}}))

camera.stop()
camera.destroy()
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "spawn_vehicle":
        blueprint = data.get("blueprint", "vehicle.tesla.model3")
        x = data.get("x", 0)
        y = data.get("y", 0)
        z = data.get("z", 0.5)
        yaw = data.get("yaw", 0)
        result = _run_carla_command(f"""
import carla, json
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()
bp_lib = world.get_blueprint_library()
bp = bp_lib.find('{blueprint}')
t = carla.Transform(carla.Location(x={x}, y={y}, z={z}), carla.Rotation(yaw={yaw}))
actor = world.try_spawn_actor(bp, t)
if actor:
    print(json.dumps({{"success": True, "actor_id": actor.id, "blueprint": "{blueprint}"}}))
else:
    print(json.dumps({{"success": False, "error": "Spawn failed - location may be occupied"}}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "clear_scene":
        result = _run_carla_command("""
import carla, json
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()
actors = world.get_actors()
destroyed = 0
for a in actors:
    if a.type_id.startswith('vehicle.') or a.type_id.startswith('walker.') or a.type_id.startswith('static.prop.'):
        a.destroy()
        destroyed += 1
print(json.dumps({"success": True, "destroyed": destroyed}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    elif action == "get_scene_info":
        result = _run_carla_command("""
import carla, json
client = carla.Client('localhost', 2000)
client.set_timeout(10.0)
world = client.get_world()
m = world.get_map()
weather = world.get_weather()
actors = world.get_actors()
vehicles = [a.type_id for a in actors if a.type_id.startswith('vehicle.')]
walkers = [a.type_id for a in actors if a.type_id.startswith('walker.')]
props = [a.type_id for a in actors if a.type_id.startswith('static.prop.')]
spectator = world.get_spectator()
t = spectator.get_transform()
print(json.dumps({
    "success": True,
    "map": m.name,
    "vehicles": len(vehicles),
    "walkers": len(walkers),
    "props": len(props),
    "camera": {"x": round(t.location.x,1), "y": round(t.location.y,1), "z": round(t.location.z,1),
               "pitch": round(t.rotation.pitch,1), "yaw": round(t.rotation.yaw,1)},
    "weather": {"cloudiness": weather.cloudiness, "precipitation": weather.precipitation,
                "sun_altitude": weather.sun_altitude_angle}
}))
""")
        return jsonify(json.loads(result) if result.strip().startswith("{") else {"success": False, "error": result})

    else:
        return jsonify({"error": f"Unknown action: {action}"}), 400


def _run_carla_command(script: str, timeout: int = 60) -> str:
    """Execute a CARLA Python script and return stdout."""
    try:
        result = subprocess.run(
            [CARLA_PYTHON, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            return json.dumps({"success": False, "error": result.stderr[:500]})
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Command timed out"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@app.route("/api/edit_scene", methods=["POST"])
def edit_scene():
    """Edit the current scene using natural language commands.

    The AI analyzes the current scene state and produces an edit plan
    that is executed directly against CARLA in real-time.
    """
    data = request.json or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "prompt": f"(edit) {command}",
        "status": "running",
        "events": Queue(),
        "result": None,
        "created_at": time.time(),
    }

    t = threading.Thread(target=_run_edit_bg, args=(job_id, command), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "status": "running"})


def _run_edit_bg(job_id: str, command: str):
    """Run scene edit in background thread."""
    job = jobs[job_id]

    def event_cb(event: dict):
        if "ts" not in event:
            event["ts"] = time.time()
        job["events"].put(event)

    try:
        # Phase 1: Get current scene state from CARLA
        event_cb({"type": "edit_phase", "data": {"phase": "analyzing", "message": "Analyzing current scene..."}})
        scene_state = get_scene_state()
        if "error" in scene_state:
            raise RuntimeError(f"Failed to get scene state: {scene_state['error']}")

        event_cb({
            "type": "edit_phase",
            "data": {
                "phase": "scene_state",
                "message": f"Scene: {scene_state.get('total_actors', 0)} actors on {scene_state.get('map', '?')}",
                "vehicles": len(scene_state.get("vehicles", [])),
                "props": len(scene_state.get("props", [])),
                "walkers": len(scene_state.get("walkers", [])),
            },
        })

        # Phase 2: Ask AI for edit plan
        event_cb({"type": "edit_phase", "data": {"phase": "planning", "message": "AI is planning the edit..."}})

        prompt = build_edit_prompt(scene_state, command)
        edit_plan = _get_edit_plan_from_ai(prompt)

        if not edit_plan:
            raise RuntimeError("AI did not produce a valid edit plan")

        description = edit_plan.get("description", "")
        actions = edit_plan.get("actions", [])
        event_cb({
            "type": "edit_phase",
            "data": {
                "phase": "plan_ready",
                "message": f"Plan: {description} ({len(actions)} actions)",
                "actions": len(actions),
                "description": description,
            },
        })

        # Phase 3: Execute edit plan
        event_cb({"type": "edit_phase", "data": {"phase": "executing", "message": "Executing edit..."}})
        result = execute_edit_plan(edit_plan, event_callback=event_cb)

        event_cb({
            "type": "edit_complete",
            "data": {
                "success": True,
                "description": description,
                "actions_executed": result.get("actions_executed", 0),
            },
        })

        job["result"] = result
        job["status"] = "completed"

    except Exception as e:
        log.exception("Edit bg failed")
        job["result"] = {"success": False, "error": str(e)}
        job["status"] = "failed"
        event_cb({"type": "error", "data": {"message": str(e)}})
    finally:
        job["events"].put(None)  # Sentinel


def _get_edit_plan_from_ai(prompt: str) -> dict:
    """Call Claude to generate an edit plan from the prompt."""
    import asyncio as _asyncio
    from claude_agent_sdk import (
        query, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock,
    )

    async def _ask():
        full_response = ""
        config = PipelineConfig()
        auth_env = get_claude_auth_env()

        options = ClaudeAgentOptions(
            model="sonnet",
            max_turns=3,
            system_prompt="You are a CARLA scene editor. Output ONLY valid JSON with no markdown fences.",
        )
        if auth_env.get("ANTHROPIC_API_KEY"):
            options.api_key = auth_env["ANTHROPIC_API_KEY"]

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                # Final result — use .result attribute
                full_response = getattr(message, "result", "") or ""
            elif isinstance(message, AssistantMessage):
                # Extract text from content blocks
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_response = block.text

        return full_response

    response_text = _asyncio.run(_ask())

    # Extract JSON from response
    if not response_text:
        return None

    # Try to parse as JSON directly
    text = response_text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


DEMO_DIR = PROJECT_ROOT / "workspace_demo"


@app.route("/api/load_demo", methods=["POST"])
def load_demo():
    """Load the demo scene incrementally using IncrementalBuilder."""
    demo_plan_path = DEMO_DIR / "scene_plan.json"
    if not demo_plan_path.exists():
        return jsonify({"success": False, "error": "Demo scene_plan.json not found"}), 404

    try:
        plan = json.loads(demo_plan_path.read_text())
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to parse demo plan: {e}"}), 500

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "prompt": "(demo scene)",
        "status": "running",
        "events": Queue(),
        "result": None,
        "created_at": time.time(),
    }

    t = threading.Thread(target=_run_build_bg, args=(job_id, plan), daemon=True)
    t.start()

    return jsonify({"success": True, "job_id": job_id, "message": "Demo scene building incrementally..."})


@app.route("/api/demo_status/<job_id>")
def demo_status(job_id):
    """Check demo loading status."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({
        "status": job["status"],
        "result": job.get("result"),
    })


def _run_pipeline_bg(job_id: str, prompt: str, model: str):
    """Run pipeline in background thread, then build the scene incrementally."""
    job = jobs[job_id]

    def event_cb(event: PipelineEvent):
        job["events"].put(event.to_dict())

    try:
        # Phase 1: Run agent pipeline to produce scene_plan.json
        config = PipelineConfig()
        result = asyncio.run(run_pipeline(
            user_prompt=prompt,
            config=config,
            event_callback=event_cb,
            model=model,
        ))

        if not result.get("success"):
            job["result"] = result
            job["status"] = "failed"
            job["events"].put(None)  # Sentinel
            return

        plan = result.get("plan")
        if not plan:
            # Pipeline succeeded but no plan was produced
            job["result"] = result
            job["result"]["warning"] = "No scene_plan.json produced by agents"
            job["status"] = "completed"
            job["events"].put(None)
            return

        # Phase 2: Run IncrementalBuilder to execute the plan
        job["events"].put({
            "type": "build_starting",
            "data": {"message": "Agent pipeline done. Starting incremental build..."},
            "ts": time.time(),
        })

        def builder_event_cb(event: dict):
            # Add timestamp if not present
            if "ts" not in event:
                event["ts"] = time.time()
            job["events"].put(event)

        builder = IncrementalBuilder(
            event_callback=builder_event_cb,
            renders_dir=str(config.renders_dir),
        )
        build_result = builder.build(plan)

        # Merge pipeline result and build result
        result["build"] = build_result

        # Collect final images (builder may have added new ones)
        if config.renders_dir.exists():
            result["images"] = sorted([
                str(f.relative_to(PROJECT_ROOT))
                for f in config.renders_dir.glob("*.png")
            ])

        job["result"] = result
        job["status"] = "completed" if build_result.get("success", False) else "failed"

    except Exception as e:
        log.exception("Pipeline bg failed")
        job["result"] = {"success": False, "error": str(e)}
        job["status"] = "failed"
        job["events"].put({"type": "error", "data": {"message": str(e)}, "ts": time.time()})
    finally:
        job["events"].put(None)  # Sentinel


def _run_build_bg(job_id: str, plan: dict):
    """Run IncrementalBuilder in a background thread (no agent pipeline)."""
    job = jobs[job_id]

    def builder_event_cb(event: dict):
        if "ts" not in event:
            event["ts"] = time.time()
        job["events"].put(event)

    try:
        builder = IncrementalBuilder(
            event_callback=builder_event_cb,
            renders_dir=str(RENDERS_DIR),
        )
        build_result = builder.build(plan)

        # Collect rendered images
        images = []
        if RENDERS_DIR.exists():
            images = sorted([f.name for f in RENDERS_DIR.glob("*.png")])

        job["result"] = {
            "success": build_result.get("success", False),
            "build": build_result,
            "images": images,
        }
        job["status"] = "completed" if build_result.get("success", False) else "failed"

    except Exception as e:
        log.exception("Build bg failed")
        job["result"] = {"success": False, "error": str(e)}
        job["status"] = "failed"
        job["events"].put({"type": "error", "data": {"message": str(e)}, "ts": time.time()})
    finally:
        job["events"].put(None)  # Sentinel


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5555))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
