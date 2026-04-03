#!/usr/bin/env python
"""CARLA real-time MJPEG streaming server with interactive camera controls.

Runs in Python 3.7 (carla37 conda env) because the CARLA Python API requires it.
Provides:
  - /stream          : MJPEG video stream from CARLA camera
  - /control         : POST camera movement commands
  - /info            : GET current camera state + scene info
  - /health          : GET health check

Camera controls support WASD movement, mouse rotation, and scroll zoom.
"""

import carla
import numpy as np
from PIL import Image
import io
import json
import math
import time
import threading
import signal
import sys
from flask import Flask, Response, request, jsonify
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CARLA_HOST = "localhost"
CARLA_PORT = 2000
STREAM_PORT = 5556
STREAM_WIDTH = 1280
STREAM_HEIGHT = 720
STREAM_FPS = 15  # Target FPS for streaming
CAMERA_FOV = 90

# Camera movement speeds
MOVE_SPEED = 2.0        # Units per keypress
ROTATE_SPEED = 2.0      # Degrees per mouse pixel
ZOOM_SPEED = 3.0        # Units per scroll tick
FLY_SPEED = 1.0         # Vertical movement speed

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
class CameraState:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 20.0
        self.pitch = -30.0
        self.yaw = 0.0
        self.roll = 0.0
        self.fov = CAMERA_FOV
        self.lock = threading.Lock()
        self.latest_frame = None
        self.frame_event = threading.Event()
        self.connected = False
        self.frame_count = 0
        self.fps = 0.0
        self._fps_time = time.time()
        self._fps_count = 0

    def update_from_transform(self, transform):
        with self.lock:
            self.x = transform.location.x
            self.y = transform.location.y
            self.z = transform.location.z
            self.pitch = transform.rotation.pitch
            self.yaw = transform.rotation.yaw
            self.roll = transform.rotation.roll

    def get_transform(self):
        with self.lock:
            return carla.Transform(
                carla.Location(x=self.x, y=self.y, z=self.z),
                carla.Rotation(pitch=self.pitch, yaw=self.yaw, roll=self.roll)
            )

    def move_forward(self, amount):
        """Move in the direction the camera is facing (horizontal plane)."""
        with self.lock:
            rad = math.radians(self.yaw)
            self.x += math.cos(rad) * amount
            self.y += math.sin(rad) * amount

    def move_right(self, amount):
        """Strafe right relative to camera facing."""
        with self.lock:
            rad = math.radians(self.yaw + 90)
            self.x += math.cos(rad) * amount
            self.y += math.sin(rad) * amount

    def move_up(self, amount):
        with self.lock:
            self.z += amount

    def rotate(self, dyaw, dpitch):
        with self.lock:
            self.yaw = (self.yaw + dyaw) % 360
            self.pitch = max(-89, min(89, self.pitch + dpitch))

    def zoom(self, amount):
        """Move forward/backward along view direction (including pitch)."""
        with self.lock:
            rad_yaw = math.radians(self.yaw)
            rad_pitch = math.radians(self.pitch)
            cos_pitch = math.cos(rad_pitch)
            self.x += math.cos(rad_yaw) * cos_pitch * amount
            self.y += math.sin(rad_yaw) * cos_pitch * amount
            self.z += math.sin(rad_pitch) * amount

    def set_position(self, x, y, z, pitch=None, yaw=None):
        with self.lock:
            self.x = x
            self.y = y
            self.z = z
            if pitch is not None:
                self.pitch = pitch
            if yaw is not None:
                self.yaw = yaw

    def update_fps(self):
        self._fps_count += 1
        now = time.time()
        elapsed = now - self._fps_time
        if elapsed >= 1.0:
            self.fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_time = now

    def to_dict(self):
        with self.lock:
            return {
                "x": round(self.x, 2),
                "y": round(self.y, 2),
                "z": round(self.z, 2),
                "pitch": round(self.pitch, 2),
                "yaw": round(self.yaw, 2),
                "fov": self.fov,
                "fps": round(self.fps, 1),
                "connected": self.connected,
                "frame_count": self.frame_count,
            }


cam_state = CameraState()
carla_world = None
carla_client = None

# ---------------------------------------------------------------------------
# CARLA camera thread
# ---------------------------------------------------------------------------
_shutting_down = False


def camera_callback(image):
    """Called by CARLA whenever a new camera frame is captured."""
    if _shutting_down:
        return
    try:
        # Convert BGRA to RGB JPEG
        array = np.frombuffer(image.raw_data, dtype=np.uint8)
        array = array.reshape((image.height, image.width, 4))
        rgb = array[:, :, [2, 1, 0]]  # BGRA -> RGB

        # Encode to JPEG
        pil_img = Image.fromarray(rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format='JPEG', quality=75)
        jpeg_bytes = buf.getvalue()

        cam_state.latest_frame = jpeg_bytes
        cam_state.frame_count += 1
        cam_state.update_fps()
        cam_state.frame_event.set()
    except Exception:
        pass


def _wait_for_async_mode(client, timeout=120):
    """Wait until CARLA world is in asynchronous mode (pipeline done)."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            world = client.get_world()
            settings = world.get_settings()
            if not settings.synchronous_mode:
                return world
        except Exception:
            pass
        time.sleep(1)
    # Timeout — return world anyway
    try:
        return client.get_world()
    except Exception:
        return None


def carla_loop():
    """Main CARLA connection and camera management loop.

    Resilient to:
    - build_scene.py calling load_world() (destroys all actors)
    - build_scene.py enabling synchronous_mode (no sensor ticks)
    - Camera actor being destroyed externally
    """
    global carla_world, carla_client

    while True:
        camera = None
        try:
            print("[STREAM] Connecting to CARLA at {}:{}...".format(CARLA_HOST, CARLA_PORT))
            client = carla.Client(CARLA_HOST, CARLA_PORT)
            client.set_timeout(30.0)  # Higher timeout for map loads / heavy scenes
            # Retry world connection up to 5 times (CARLA may be loading a map)
            world = None
            for _attempt in range(5):
                try:
                    world = client.get_world()
                    break
                except RuntimeError as re:
                    if "time-out" in str(re).lower():
                        print("[STREAM] CARLA busy (timeout), retrying in 3s... ({}/5)".format(_attempt + 1))
                        time.sleep(3)
                    else:
                        raise
            if world is None:
                raise RuntimeError("Could not connect to CARLA after 5 attempts")
            carla_client = client
            carla_world = world

            # If world is in synchronous mode (pipeline running), wait
            settings = world.get_settings()
            if settings.synchronous_mode:
                print("[STREAM] World is in synchronous mode (pipeline running). Waiting...")
                cam_state.connected = False
                world = _wait_for_async_mode(client)
                if world is None:
                    raise Exception("Could not get world after sync mode wait")
                carla_world = world

            cam_state.connected = True
            map_name = world.get_map().name
            print("[STREAM] Connected! Map: {}".format(map_name))

            # Get initial spectator position
            spectator = world.get_spectator()
            cam_state.update_from_transform(spectator.get_transform())

            # Set up camera sensor
            bp_lib = world.get_blueprint_library()
            camera_bp = bp_lib.find('sensor.camera.rgb')
            camera_bp.set_attribute('image_size_x', str(STREAM_WIDTH))
            camera_bp.set_attribute('image_size_y', str(STREAM_HEIGHT))
            camera_bp.set_attribute('fov', str(cam_state.fov))
            camera_bp.set_attribute('sensor_tick', str(1.0 / STREAM_FPS))

            # Spawn camera at current position
            camera = world.spawn_actor(camera_bp, cam_state.get_transform())
            camera.listen(camera_callback)
            print("[STREAM] Camera sensor active. Streaming at {}x{} @ {} FPS".format(
                STREAM_WIDTH, STREAM_HEIGHT, STREAM_FPS))

            # Track frame count to detect stalls
            last_frame_count = cam_state.frame_count
            stall_checks = 0

            # Main loop: update camera position from state
            while True:
                # Check if camera was destroyed (e.g. by load_world)
                if not camera.is_alive:
                    print("[STREAM] Camera destroyed externally. Reconnecting...")
                    break

                # Check if world switched to sync mode (pipeline started)
                try:
                    settings = world.get_settings()
                    if settings.synchronous_mode:
                        print("[STREAM] Sync mode detected (pipeline running). Reconnecting after it finishes...")
                        break
                except Exception:
                    print("[STREAM] Lost world reference. Reconnecting...")
                    break

                transform = cam_state.get_transform()
                camera.set_transform(transform)

                # Also update spectator so the CARLA viewport matches
                try:
                    spectator.set_transform(transform)
                except Exception:
                    pass  # Spectator may be invalid after map change

                # Detect frame stalls (no new frames for 5+ seconds)
                if cam_state.frame_count == last_frame_count:
                    stall_checks += 1
                    if stall_checks > 150:  # 150 * 33ms = ~5 seconds
                        print("[STREAM] Frame stall detected. Reconnecting...")
                        break
                else:
                    last_frame_count = cam_state.frame_count
                    stall_checks = 0

                time.sleep(1.0 / 30)  # 30 Hz control update

        except KeyboardInterrupt:
            print("[STREAM] Shutting down...")
            break
        except Exception as e:
            cam_state.connected = False
            print("[STREAM] Error: {}. Reconnecting in 3s...".format(e))
            time.sleep(3)
        finally:
            cam_state.connected = False
            if camera is not None:
                try:
                    camera.stop()
                    camera.destroy()
                except:
                    pass


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route('/stream')
def video_stream():
    """MJPEG video stream endpoint."""
    def generate():
        while True:
            cam_state.frame_event.wait(timeout=2.0)
            cam_state.frame_event.clear()

            frame = cam_state.latest_frame
            if frame is None:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n'
                   + frame + b'\r\n')

    return Response(
        generate(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        }
    )


@app.route('/snapshot')
def snapshot():
    """Capture a single frame as JPEG."""
    frame = cam_state.latest_frame
    if frame is None:
        return jsonify({"error": "No frame available"}), 503
    return Response(frame, mimetype='image/jpeg')


@app.route('/control', methods=['POST'])
def control():
    """Handle camera control commands.

    Supported commands:
      move_forward, move_backward, move_left, move_right,
      move_up, move_down, rotate, zoom, set_position, set_weather
    """
    data = request.json or {}
    cmd = data.get('command', '')

    if cmd == 'move_forward':
        cam_state.move_forward(data.get('speed', MOVE_SPEED))
    elif cmd == 'move_backward':
        cam_state.move_forward(-data.get('speed', MOVE_SPEED))
    elif cmd == 'move_left':
        cam_state.move_right(-data.get('speed', MOVE_SPEED))
    elif cmd == 'move_right':
        cam_state.move_right(data.get('speed', MOVE_SPEED))
    elif cmd == 'move_up':
        cam_state.move_up(data.get('speed', FLY_SPEED))
    elif cmd == 'move_down':
        cam_state.move_up(-data.get('speed', FLY_SPEED))
    elif cmd == 'rotate':
        dyaw = data.get('dyaw', 0) * ROTATE_SPEED
        dpitch = data.get('dpitch', 0) * ROTATE_SPEED
        cam_state.rotate(dyaw, dpitch)
    elif cmd == 'zoom':
        cam_state.zoom(data.get('delta', 0) * ZOOM_SPEED)
    elif cmd == 'set_position':
        cam_state.set_position(
            data.get('x', cam_state.x),
            data.get('y', cam_state.y),
            data.get('z', cam_state.z),
            data.get('pitch'),
            data.get('yaw'),
        )
    elif cmd == 'set_weather':
        preset = data.get('preset', 'ClearNoon')
        if carla_client:
            try:
                world = carla_client.get_world()
                weather = getattr(carla.WeatherParameters, preset, carla.WeatherParameters.ClearNoon)
                world.set_weather(weather)
                return jsonify({"success": True, "weather": preset})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)})
    elif cmd == 'load_map':
        map_name = data.get('map', 'Town10HD_Opt')
        if carla_client:
            try:
                carla_client.load_world(map_name)
                time.sleep(3)
                return jsonify({"success": True, "map": map_name, "note": "Camera will reconnect"})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)})
    elif cmd == 'get_actors':
        if carla_client:
            try:
                world = carla_client.get_world()
                actors = world.get_actors()
                vehicles = [a.type_id for a in actors if a.type_id.startswith('vehicle.')]
                walkers = [a.type_id for a in actors if a.type_id.startswith('walker.')]
                props = [a.type_id for a in actors if a.type_id.startswith('static.prop.')]
                return jsonify({
                    "success": True,
                    "vehicles": len(vehicles),
                    "walkers": len(walkers),
                    "props": len(props),
                    "total": len(actors),
                })
            except Exception as e:
                return jsonify({"success": False, "error": str(e)})
    else:
        return jsonify({"error": "Unknown command: " + cmd}), 400

    return jsonify({"success": True, "camera": cam_state.to_dict()})


@app.route('/info')
def info():
    """Get current camera state and scene info."""
    result = cam_state.to_dict()
    if carla_client:
        try:
            world = carla_client.get_world()
            m = world.get_map()
            result["map"] = m.name
            weather = world.get_weather()
            result["weather"] = {
                "cloudiness": weather.cloudiness,
                "precipitation": weather.precipitation,
                "sun_altitude": weather.sun_altitude_angle,
            }
        except:
            pass
    return jsonify(result)


@app.route('/health')
def health():
    """Health check."""
    return jsonify({
        "status": "ok" if cam_state.connected else "disconnected",
        "fps": cam_state.fps,
        "frames": cam_state.frame_count,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _port_in_use(port):
    """Check if a port is already in use."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0


def main():
    global _shutting_down

    # Check port availability first
    if _port_in_use(STREAM_PORT):
        print("[STREAM] ERROR: Port {} is already in use!".format(STREAM_PORT))
        print("[STREAM] Kill the existing process: lsof -ti:{} | xargs kill -9".format(STREAM_PORT))
        sys.exit(1)

    # Start CARLA camera thread
    carla_thread = threading.Thread(target=carla_loop, daemon=True)
    carla_thread.start()

    # Wait for first frame
    print("[STREAM] Waiting for first frame...")
    for _ in range(30):
        if cam_state.latest_frame is not None:
            print("[STREAM] First frame received!")
            break
        time.sleep(1)

    # Start Flask
    print("[STREAM] Starting streaming server on port {}".format(STREAM_PORT))
    try:
        app.run(host='0.0.0.0', port=STREAM_PORT, threaded=True, debug=False)
    finally:
        _shutting_down = True


if __name__ == '__main__':
    main()
