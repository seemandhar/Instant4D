#!/home/sejain/miniconda3/envs/carla37/bin/python
# -*- coding: utf-8 -*-
"""
carla_builder_worker.py  --  Python 3.7 CARLA incremental scene builder.

Reads a scene_plan.json from a path given as argv[1], connects to CARLA,
and builds the scene incrementally (one actor at a time with small delays
so the live streaming camera can show each addition).

Communication: prints one JSON object per line to stdout.  The parent
process (incremental_builder.py, Python 3.10+) reads these lines and
dispatches them as events.

IMPORTANT:
  - NEVER enables synchronous_mode -- the streaming camera needs async mode.
  - Uses world.try_spawn_actor() for graceful collision handling.
  - Uses time.sleep() between spawns for visual pacing.
"""

from __future__ import print_function

import carla
import json
import math
import os
import sys
import time
import traceback

try:
    from PIL import Image
    import numpy as np
    HAS_IMAGING = True
except ImportError:
    HAS_IMAGING = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CARLA_HOST = "localhost"
CARLA_PORT = 2000
CARLA_TIMEOUT = 30.0

SPAWN_DELAY = 0.5          # seconds between spawns for visual pacing
WEATHER_SETTLE_TIME = 3.0  # seconds to let weather render in async mode
CAPTURE_SETTLE_TIME = 2.0  # seconds to let a temp camera produce frames

IMG_WIDTH = 1280
IMG_HEIGHT = 720

Z_RETRY_LADDER = [0.5, 1.0, 1.5, 2.0]

# ---------------------------------------------------------------------------
# JSON-line emitter
# ---------------------------------------------------------------------------

def emit(event_type, data):
    """Print a single JSON line to stdout for the parent process."""
    obj = {"type": event_type, "data": data, "ts": time.time()}
    line = json.dumps(obj, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_log(message, level="info"):
    emit("build_log", {"message": message, "level": level})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def map_short_name(map_field):
    """'/Game/Carla/Maps/Town10HD_Opt' -> 'Town10HD_Opt'"""
    return map_field.rstrip("/").split("/")[-1]


def build_transform(t):
    return carla.Transform(
        carla.Location(x=float(t["x"]), y=float(t["y"]), z=float(t["z"])),
        carla.Rotation(
            pitch=float(t.get("pitch", 0.0)),
            yaw=float(t.get("yaw", 0.0)),
            roll=float(t.get("roll", 0.0)),
        ),
    )


def find_bp(bp_lib, bp_id):
    """Return a blueprint by exact ID; fall back to filter() for fuzzy match."""
    # Try exact match first
    try:
        bp = bp_lib.find(bp_id)
        if bp is not None:
            return bp, bp_id
    except Exception:
        pass
    # Fuzzy filter
    results = bp_lib.filter(bp_id)
    if results:
        matched_id = results[0].id
        return results[0], matched_id
    # Try stripping prefix variations
    parts = bp_id.split(".")
    if len(parts) >= 2:
        # e.g. "static.prop.bench01" -> filter("*bench01*")
        short = parts[-1]
        results = bp_lib.filter("*" + short + "*")
        if results:
            matched_id = results[0].id
            return results[0], matched_id
    return None, None


def distance_2d(loc_a, loc_b):
    """2D distance between two carla.Location objects."""
    dx = loc_a.x - loc_b.x
    dy = loc_a.y - loc_b.y
    return math.sqrt(dx * dx + dy * dy)


def find_nearest_spawn_point(spawn_points, target_location):
    """Return the spawn point closest (2D) to target_location."""
    best = None
    best_dist = float("inf")
    for sp in spawn_points:
        d = distance_2d(sp.location, target_location)
        if d < best_dist:
            best_dist = d
            best = sp
    return best


def save_carla_image(carla_img, filepath):
    """CARLA raw_data is BGRA; reorder to RGB and save as PNG."""
    arr = np.frombuffer(carla_img.raw_data, dtype=np.uint8)
    arr = arr.reshape((carla_img.height, carla_img.width, 4))
    rgb = arr[:, :, [2, 1, 0]]  # BGR -> RGB (drop alpha)
    Image.fromarray(rgb).save(filepath)


# ---------------------------------------------------------------------------
# Weather presets (same as carla_stream / build_scene)
# ---------------------------------------------------------------------------

WEATHER_PRESETS = {
    "ClearNoon":     carla.WeatherParameters(
        cloudiness=0,   precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=70),
    "CloudyNoon":    carla.WeatherParameters(
        cloudiness=60,  precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=70),
    "WetNoon":       carla.WeatherParameters(
        cloudiness=20,  precipitation=0,  precipitation_deposits=60,
        wind_intensity=0,  fog_density=0,  wetness=80,  sun_altitude_angle=70),
    "WetCloudyNoon": carla.WeatherParameters(
        cloudiness=60,  precipitation=0,  precipitation_deposits=60,
        wind_intensity=0,  fog_density=0,  wetness=80,  sun_altitude_angle=70),
    "MidRainyNoon":  carla.WeatherParameters(
        cloudiness=80,  precipitation=50, precipitation_deposits=50,
        wind_intensity=20, fog_density=0,  wetness=80,  sun_altitude_angle=70),
    "HardRainNoon":  carla.WeatherParameters(
        cloudiness=100, precipitation=90, precipitation_deposits=90,
        wind_intensity=30, fog_density=0,  wetness=100, sun_altitude_angle=70),
    "SoftRainNoon":  carla.WeatherParameters(
        cloudiness=40,  precipitation=20, precipitation_deposits=20,
        wind_intensity=10, fog_density=0,  wetness=40,  sun_altitude_angle=70),
    "ClearSunset":   carla.WeatherParameters(
        cloudiness=0,   precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=15),
    "CloudySunset":  carla.WeatherParameters(
        cloudiness=60,  precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=15),
    "ClearNight":    carla.WeatherParameters(
        cloudiness=0,   precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=-90),
    "CloudyNight":   carla.WeatherParameters(
        cloudiness=60,  precipitation=0,  precipitation_deposits=0,
        wind_intensity=0,  fog_density=0,  wetness=0,   sun_altitude_angle=-90),
    "HardRainNight": carla.WeatherParameters(
        cloudiness=100, precipitation=90, precipitation_deposits=90,
        wind_intensity=30, fog_density=20, wetness=100, sun_altitude_angle=-90),
    "SoftRainNight": carla.WeatherParameters(
        cloudiness=40,  precipitation=20, precipitation_deposits=20,
        wind_intensity=10, fog_density=5,  wetness=40,  sun_altitude_angle=-90),
    "MidRainyNight": carla.WeatherParameters(
        cloudiness=80,  precipitation=50, precipitation_deposits=50,
        wind_intensity=20, fog_density=10, wetness=80,  sun_altitude_angle=-90),
    "WetNight":      carla.WeatherParameters(
        cloudiness=20,  precipitation=0,  precipitation_deposits=60,
        wind_intensity=0,  fog_density=0,  wetness=80,  sun_altitude_angle=-90),
}


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

_VEHICLE_TYPE_KEYWORDS = ("car", "truck", "van", "motorcycle", "bicycle")


def destroy_all_spawned_actors(world):
    """Destroy every vehicle, walker, and walker-controller in the world."""
    destroyed = 0

    # Walker AI controllers first
    for actor in world.get_actors():
        if actor.type_id == "controller.ai.walker":
            try:
                actor.stop()
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    # Vehicles
    for actor in world.get_actors().filter("vehicle.*"):
        try:
            actor.destroy()
            destroyed += 1
        except Exception:
            pass

    # Keyword scan for stragglers
    for actor in world.get_actors():
        tid = actor.type_id.lower()
        if any(kw in tid for kw in _VEHICLE_TYPE_KEYWORDS):
            try:
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    # Walkers
    for actor in world.get_actors():
        if actor.type_id.startswith("walker."):
            try:
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    # Props (static.*)
    for actor in world.get_actors().filter("static.*"):
        try:
            actor.destroy()
            destroyed += 1
        except Exception:
            pass

    return destroyed


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

def spawn_vehicle(world, bp_lib, entry, spawn_points):
    """Spawn a vehicle with z-retry ladder, then nearest-spawn-point fallback."""
    bp_id = entry["blueprint"]
    bp, matched_id = find_bp(bp_lib, bp_id)

    if bp is None:
        emit("build_warning", {
            "category": "vehicle",
            "blueprint": bp_id,
            "message": "blueprint not found, skipping",
        })
        return None

    if matched_id != bp_id:
        emit("build_warning", {
            "category": "vehicle",
            "blueprint": bp_id,
            "message": "fuzzy-matched to '%s'" % matched_id,
        })

    # Set color if specified
    color_str = entry.get("color", "")
    if color_str and bp.has_attribute("color"):
        try:
            bp.set_attribute("color", color_str)
        except Exception:
            pass

    t_data = entry["transform"]
    base_rot = carla.Rotation(
        pitch=float(t_data.get("pitch", 0.0)),
        yaw=float(t_data.get("yaw", 0.0)),
        roll=float(t_data.get("roll", 0.0)),
    )

    # Build z-retry ladder: planned z first, then standard retries
    planned_z = float(t_data.get("z", 0.5))
    seen = set()
    z_ladder = []
    for z in [planned_z] + Z_RETRY_LADDER:
        if z not in seen:
            seen.add(z)
            z_ladder.append(z)

    target_loc = carla.Location(
        x=float(t_data["x"]),
        y=float(t_data["y"]),
        z=planned_z,
    )

    # Try planned coordinates with z-retry
    for z in z_ladder:
        tf = carla.Transform(
            carla.Location(x=float(t_data["x"]), y=float(t_data["y"]), z=z),
            base_rot,
        )
        try:
            actor = world.try_spawn_actor(bp, tf)
            if actor is not None:
                return actor
        except Exception:
            pass

    # Fallback: find nearest map spawn point
    if spawn_points:
        nearest = find_nearest_spawn_point(spawn_points, target_loc)
        if nearest is not None:
            try:
                actor = world.try_spawn_actor(bp, nearest)
                if actor is not None:
                    emit_log("vehicle '%s' fell back to nearest spawn point" % bp_id)
                    return actor
            except Exception:
                pass

        # Last resort: try first 30 spawn points
        for sp in spawn_points[:30]:
            try:
                actor = world.try_spawn_actor(bp, sp)
                if actor is not None:
                    emit_log("vehicle '%s' fell back to arbitrary spawn point" % bp_id)
                    return actor
            except Exception:
                pass

    return None


def spawn_prop(world, bp_lib, entry):
    """Spawn a prop at the exact planned coordinates."""
    bp_id = entry["blueprint"]
    bp, matched_id = find_bp(bp_lib, bp_id)

    if bp is None:
        emit("build_warning", {
            "category": "prop",
            "blueprint": bp_id,
            "message": "blueprint not found, skipping",
        })
        return None

    if matched_id != bp_id:
        emit("build_warning", {
            "category": "prop",
            "blueprint": bp_id,
            "message": "fuzzy-matched to '%s'" % matched_id,
        })

    tf = build_transform(entry["transform"])
    try:
        actor = world.try_spawn_actor(bp, tf)
        if actor is not None:
            return actor
    except Exception as exc:
        emit_log("prop '%s' spawn error: %s" % (bp_id, exc), level="warning")
    return None


def spawn_walker(world, bp_lib, entry):
    """Spawn a walker with z clamped >= 1.0."""
    bp_id = entry["blueprint"]
    bp, matched_id = find_bp(bp_lib, bp_id)

    if bp is None:
        emit("build_warning", {
            "category": "walker",
            "blueprint": bp_id,
            "message": "blueprint not found, skipping",
        })
        return None

    if matched_id != bp_id:
        emit("build_warning", {
            "category": "walker",
            "blueprint": bp_id,
            "message": "fuzzy-matched to '%s'" % matched_id,
        })

    if bp.has_attribute("is_invincible"):
        bp.set_attribute("is_invincible", "false")

    t = dict(entry["transform"])
    t["z"] = max(float(t.get("z", 1.0)), 1.0)
    tf = build_transform(t)

    try:
        actor = world.try_spawn_actor(bp, tf)
        if actor is not None:
            return actor
    except Exception as exc:
        emit_log("walker '%s' spawn error: %s" % (bp_id, exc), level="warning")
    return None


# ---------------------------------------------------------------------------
# Image capture (async-mode safe)
# ---------------------------------------------------------------------------

def capture_frame_async(world, bp_lib, cam_cfg, out_path, label="camera"):
    """
    Spawn a temporary RGB camera, wait for async frames to arrive,
    save the latest frame, then destroy the camera.

    In async mode we cannot tick -- we just sleep and let the server
    deliver frames at its own pace.
    """
    if not HAS_IMAGING:
        emit_log("Pillow/numpy not installed -- cannot capture images", level="warning")
        return False

    camera_bp, _ = find_bp(bp_lib, "sensor.camera.rgb")
    if camera_bp is None:
        emit_log("sensor.camera.rgb not found -- cannot capture", level="error")
        return False

    camera_bp.set_attribute("image_size_x", str(IMG_WIDTH))
    camera_bp.set_attribute("image_size_y", str(IMG_HEIGHT))
    camera_bp.set_attribute("fov", str(cam_cfg.get("fov", 90)))

    cam_transform = carla.Transform(
        carla.Location(
            x=float(cam_cfg["x"]),
            y=float(cam_cfg["y"]),
            z=float(cam_cfg["z"]),
        ),
        carla.Rotation(
            pitch=float(cam_cfg.get("pitch", 0.0)),
            yaw=float(cam_cfg.get("yaw", 0.0)),
            roll=float(cam_cfg.get("roll", 0.0)),
        ),
    )

    camera = None
    try:
        camera = world.try_spawn_actor(camera_bp, cam_transform)
    except Exception as exc:
        emit_log("could not spawn camera '%s': %s" % (label, exc), level="error")
        return False

    if camera is None:
        emit_log("camera '%s' spawn returned None (collision?)" % label, level="error")
        return False

    frames = []
    camera.listen(lambda img: frames.append(img))

    # In async mode, just sleep to let the server push frames
    time.sleep(CAPTURE_SETTLE_TIME)

    camera.stop()

    success = False
    if frames:
        try:
            out_dir = os.path.dirname(out_path)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir)
            save_carla_image(frames[-1], out_path)
            success = True
        except Exception as exc:
            emit_log("failed saving '%s': %s" % (label, exc), level="error")
    else:
        emit_log("no frames received for camera '%s'" % label, level="warning")

    try:
        camera.destroy()
    except Exception:
        pass

    return success


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    if len(sys.argv) < 2:
        emit("build_error", {"message": "Usage: carla_builder_worker.py <scene_plan.json> [renders_dir]"})
        sys.exit(1)

    plan_path = sys.argv[1]
    renders_dir = sys.argv[2] if len(sys.argv) >= 3 else None

    # Load plan
    try:
        with open(plan_path, "r") as fh:
            plan = json.load(fh)
    except Exception as exc:
        emit("build_error", {"message": "failed to load plan: %s" % exc})
        sys.exit(1)

    title = plan.get("title", "Untitled Scene")
    map_field = plan.get("map", "Town10HD_Opt")
    map_name = map_short_name(map_field)
    weather_name = plan.get("weather", "ClearNoon")
    weather_custom = plan.get("weather_custom", None)

    vehicles_plan = plan.get("vehicles", [])
    props_plan = plan.get("props", [])
    walkers_plan = plan.get("walkers", [])
    main_cam_cfg = plan.get("camera", {})
    extra_cams = plan.get("extra_cameras", [])

    total_objects = len(vehicles_plan) + len(props_plan) + len(walkers_plan)

    start_time = time.time()
    spawned_actors = []
    actors_spawned_count = 0

    emit("build_phase", {"phase": "init", "status": "started", "title": title})

    try:
        # ---------------------------------------------------------------
        # 1. Connect to CARLA
        # ---------------------------------------------------------------
        emit("build_phase", {"phase": "connect", "status": "started"})
        client = carla.Client(CARLA_HOST, CARLA_PORT)
        client.set_timeout(CARLA_TIMEOUT)
        server_version = client.get_server_version()
        emit("build_phase", {"phase": "connect", "status": "complete",
                             "server_version": server_version})

        # ---------------------------------------------------------------
        # 2. Load map (skip if already loaded to avoid camera blackout)
        # ---------------------------------------------------------------
        emit("build_phase", {"phase": "map", "status": "started", "map": map_name})
        world = client.get_world()
        current_map = world.get_map().name
        # current_map might be like "Carla/Maps/Town10HD_Opt" or just "Town10HD_Opt"
        current_map_short = current_map.rstrip("/").split("/")[-1]

        # Validate map exists
        available_maps = [m.rstrip("/").split("/")[-1] for m in client.get_available_maps()]
        if map_name not in available_maps:
            emit_log("map '%s' not available, falling back. Available: %s" % (
                map_name, ", ".join(sorted(available_maps))), level="warning")
            # Try to find a close match
            for candidate in ["Town04_Opt", "Town10HD_Opt", "Town05_Opt", "Town03_Opt"]:
                if candidate in available_maps:
                    map_name = candidate
                    emit_log("using fallback map: %s" % map_name, level="info")
                    break

        if current_map_short == map_name:
            emit_log("map '%s' already loaded, skipping reload" % map_name)
        else:
            emit_log("loading map '%s' (current: '%s')" % (map_name, current_map_short))
            client.load_world(map_name)
            time.sleep(5.0)  # Let map assets fully load in async mode
            world = client.get_world()

        emit("build_phase", {"phase": "map", "status": "complete", "map": map_name})

        bp_lib = world.get_blueprint_library()
        spawn_points = world.get_map().get_spawn_points()

        # ---------------------------------------------------------------
        # 3. Clear existing actors
        # ---------------------------------------------------------------
        emit("build_phase", {"phase": "cleanup", "status": "started"})
        cleared = destroy_all_spawned_actors(world)
        time.sleep(1.0)
        # Second pass for stragglers
        cleared += destroy_all_spawned_actors(world)
        emit("build_phase", {"phase": "cleanup", "status": "complete",
                             "actors_destroyed": cleared})

        # ---------------------------------------------------------------
        # 4. Set weather
        # ---------------------------------------------------------------
        emit("build_phase", {"phase": "weather", "status": "started",
                             "preset": weather_name})

        if weather_custom:
            wp = carla.WeatherParameters(
                cloudiness=weather_custom.get("cloudiness", 0),
                precipitation=weather_custom.get("precipitation", 0),
                precipitation_deposits=weather_custom.get("precipitation_deposits", 0),
                wind_intensity=weather_custom.get("wind_intensity", 0),
                fog_density=weather_custom.get("fog_density", 0),
                wetness=weather_custom.get("wetness", 0),
                sun_altitude_angle=weather_custom.get("sun_altitude_angle", 70),
            )
        elif weather_name in WEATHER_PRESETS:
            wp = WEATHER_PRESETS[weather_name]
        else:
            # Try as a carla.WeatherParameters attribute
            wp = getattr(carla.WeatherParameters, weather_name, None)
            if wp is None:
                emit_log("weather preset '%s' not found, using ClearNoon" % weather_name,
                         level="warning")
                wp = WEATHER_PRESETS["ClearNoon"]

        world.set_weather(wp)
        # Multiple ticks worth of sleep for async rendering to settle
        time.sleep(WEATHER_SETTLE_TIME)

        emit("build_phase", {"phase": "weather", "status": "complete",
                             "preset": weather_name})

        # ---------------------------------------------------------------
        # 5. Move spectator camera
        # ---------------------------------------------------------------
        if main_cam_cfg:
            emit("build_phase", {"phase": "camera", "status": "started"})
            spectator = world.get_spectator()
            spectator.set_transform(carla.Transform(
                carla.Location(
                    x=float(main_cam_cfg["x"]),
                    y=float(main_cam_cfg["y"]),
                    z=float(main_cam_cfg["z"]),
                ),
                carla.Rotation(
                    pitch=float(main_cam_cfg.get("pitch", 0.0)),
                    yaw=float(main_cam_cfg.get("yaw", 0.0)),
                    roll=float(main_cam_cfg.get("roll", 0.0)),
                ),
            ))
            time.sleep(0.5)
            emit("build_phase", {"phase": "camera", "status": "complete",
                                 "x": main_cam_cfg["x"], "y": main_cam_cfg["y"],
                                 "z": main_cam_cfg["z"]})

        # ---------------------------------------------------------------
        # 6. Spawn vehicles one by one
        # ---------------------------------------------------------------
        total_vehicles = len(vehicles_plan)
        for i, entry in enumerate(vehicles_plan):
            bp_id = entry.get("blueprint", "unknown")
            try:
                actor = spawn_vehicle(world, bp_lib, entry, spawn_points)
                success = actor is not None
                if actor:
                    spawned_actors.append(actor)
                    actors_spawned_count += 1
                emit("build_spawn", {
                    "category": "vehicle",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_vehicles,
                    "success": success,
                })
            except Exception as exc:
                emit("build_spawn", {
                    "category": "vehicle",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_vehicles,
                    "success": False,
                    "error": str(exc),
                })
            time.sleep(SPAWN_DELAY)

        # ---------------------------------------------------------------
        # 7. Spawn props one by one
        # ---------------------------------------------------------------
        total_props = len(props_plan)
        for i, entry in enumerate(props_plan):
            bp_id = entry.get("blueprint", "unknown")
            try:
                actor = spawn_prop(world, bp_lib, entry)
                success = actor is not None
                if actor:
                    spawned_actors.append(actor)
                    actors_spawned_count += 1
                emit("build_spawn", {
                    "category": "prop",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_props,
                    "success": success,
                })
            except Exception as exc:
                emit("build_spawn", {
                    "category": "prop",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_props,
                    "success": False,
                    "error": str(exc),
                })
            time.sleep(SPAWN_DELAY)

        # ---------------------------------------------------------------
        # 8. Spawn walkers one by one
        # ---------------------------------------------------------------
        total_walkers = len(walkers_plan)
        for i, entry in enumerate(walkers_plan):
            bp_id = entry.get("blueprint", "unknown")
            try:
                actor = spawn_walker(world, bp_lib, entry)
                success = actor is not None
                if actor:
                    spawned_actors.append(actor)
                    actors_spawned_count += 1
                emit("build_spawn", {
                    "category": "walker",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_walkers,
                    "success": success,
                })
            except Exception as exc:
                emit("build_spawn", {
                    "category": "walker",
                    "blueprint": bp_id,
                    "index": i + 1,
                    "total": total_walkers,
                    "success": False,
                    "error": str(exc),
                })
            time.sleep(SPAWN_DELAY)

        # ---------------------------------------------------------------
        # 9. Capture images from planned camera angles
        # ---------------------------------------------------------------
        render_results = []

        if renders_dir and main_cam_cfg:
            emit("build_phase", {"phase": "capture", "status": "started"})

            # Main view
            main_out = os.path.join(renders_dir, "main_view.png")
            ok = capture_frame_async(world, bp_lib, main_cam_cfg, main_out,
                                     label="main_view")
            render_results.append({"name": "main_view", "path": main_out, "success": ok})
            emit("build_capture", {"name": "main_view", "path": main_out, "success": ok})

            # Extra cameras
            for ecam in extra_cams:
                cam_name = ecam.get("name", "extra_camera")
                out_path = os.path.join(renders_dir, "%s.png" % cam_name)
                ok = capture_frame_async(world, bp_lib, ecam, out_path, label=cam_name)
                render_results.append({"name": cam_name, "path": out_path, "success": ok})
                emit("build_capture", {"name": cam_name, "path": out_path, "success": ok})

            emit("build_phase", {"phase": "capture", "status": "complete",
                                 "images": len(render_results)})

        # ---------------------------------------------------------------
        # 10. Done -- emit summary
        # ---------------------------------------------------------------
        elapsed = time.time() - start_time
        emit("build_complete", {
            "success": True,
            "title": title,
            "map": map_name,
            "weather": weather_name,
            "actors_spawned": actors_spawned_count,
            "total_planned": total_objects,
            "elapsed": round(elapsed, 1),
            "renders": [r["path"] for r in render_results if r.get("success")],
        })

    except KeyboardInterrupt:
        emit("build_error", {"message": "interrupted by user"})
        sys.exit(1)

    except Exception as exc:
        elapsed = time.time() - start_time
        emit("build_error", {
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "actors_spawned": actors_spawned_count,
            "elapsed": round(elapsed, 1),
        })
        sys.exit(1)

    # NOTE: We do NOT destroy spawned actors -- they stay in the scene
    # for the streaming camera to show.  Cleanup happens on next build.


if __name__ == "__main__":
    main()
