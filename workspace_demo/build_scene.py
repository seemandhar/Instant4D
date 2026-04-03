#!/home/sejain/miniconda3/envs/carla37/bin/python
# -*- coding: utf-8 -*-
"""
build_scene.py — Rainy Night Police Roadblock
Reads workspace/scene_plan.json and builds the scene in CARLA:
  - Six vehicles (two Dodge Charger police cruisers, one Dodge Charger Police 2020
    unmarked dark vehicle, three civilian cars)
  - Twenty-one props (cones, barriers, warning signs, street furniture;
    static.prop.bin used in place of the unavailable static.prop.trashcan)
  - Ten sidewalk pedestrian bystanders (left sidewalk: 0007, 0009, 0010, 0011,
    0012, 0013; right sidewalk: 0003, 0004, 0006, 0008)
  - HardRainNight weather
  - Four camera renders saved to renders/: main_view, overhead_wide,
    street_level_approach, bystander_pov

Run with:
    /home/sejain/miniconda3/envs/carla37/bin/python workspace/build_scene.py
"""

import carla
import numpy as np
import os
import sys
import time
import json
import traceback

try:
    from PIL import Image
except ImportError:
    print("[ERROR] Pillow is not installed.  Run: pip install Pillow")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths — all absolute so cwd changes never break anything
# ---------------------------------------------------------------------------
SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT          = os.path.dirname(SCRIPT_DIR)
PLAN_PATH          = os.path.join(SCRIPT_DIR, "scene_plan.json")
RENDER_RESULT_PATH = os.path.join(SCRIPT_DIR, "render_result.json")
RENDERS_DIR        = os.path.join(REPO_ROOT,  "renders")

IMG_WIDTH  = 1280
IMG_HEIGHT = 720

CARLA_HOST    = "localhost"
CARLA_PORT    = 2000
CARLA_TIMEOUT = 60.0

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_plan(path):
    with open(path, "r") as fh:
        return json.load(fh)


def map_short_name(map_field):
    """
    client.load_world() needs just the short name, e.g. 'Town10HD_Opt',
    not the full asset path '/Game/Carla/Maps/Town10HD_Opt'.
    """
    return map_field.rstrip("/").split("/")[-1]


def build_transform(t):
    return carla.Transform(
        carla.Location(x=float(t["x"]), y=float(t["y"]), z=float(t["z"])),
        carla.Rotation(
            pitch=float(t.get("pitch", 0.0)),
            yaw=float(t.get("yaw",   0.0)),
            roll=float(t.get("roll",  0.0)),
        ),
    )


def find_bp(bp_lib, bp_id):
    """Return a blueprint by ID. Falls back to filter(). Returns None if not found."""
    try:
        bp = bp_lib.find(bp_id)
        if bp is not None:
            return bp
    except Exception:
        pass
    results = bp_lib.filter(bp_id)
    return results[0] if results else None


def save_carla_image(carla_img, filepath):
    """CARLA raw_data is BGRA; reorder to RGB and save as PNG."""
    arr = np.frombuffer(carla_img.raw_data, dtype=np.uint8)
    arr = arr.reshape((carla_img.height, carla_img.width, 4))
    rgb = arr[:, :, [2, 1, 0]]   # BGR -> RGB (drop alpha)
    Image.fromarray(rgb).save(filepath)
    print(f"[camera]  Saved: {filepath}")


def capture_frame(world, bp_lib, cam_cfg, out_path, all_actors, label="camera"):
    """
    Spawn a free-floating RGB camera, tick >= 12 times so weather and actors
    are fully rendered, save the last received frame, then destroy the sensor.
    The actor is appended to all_actors so the outer finally can clean it up
    if this function raises before the local destroy() call.
    Returns True on success.
    """
    camera_bp = find_bp(bp_lib, "sensor.camera.rgb")
    if camera_bp is None:
        print(f"[camera]  ERROR: sensor.camera.rgb not found — skipping '{label}'.")
        return False

    camera_bp.set_attribute("image_size_x", str(IMG_WIDTH))
    camera_bp.set_attribute("image_size_y", str(IMG_HEIGHT))
    camera_bp.set_attribute("fov", str(cam_cfg.get("fov", 90)))

    cam_transform = carla.Transform(
        carla.Location(x=cam_cfg["x"], y=cam_cfg["y"], z=cam_cfg["z"]),
        carla.Rotation(
            pitch=cam_cfg.get("pitch", 0.0),
            yaw=cam_cfg.get("yaw",   0.0),
            roll=cam_cfg.get("roll",  0.0),
        ),
    )

    camera = None
    try:
        camera = world.spawn_actor(camera_bp, cam_transform)
    except Exception as exc:
        print(f"[camera]  ERROR: could not spawn camera '{label}': {exc}")
        return False

    # Register immediately so outer finally can clean up on unexpected raise.
    all_actors.append(camera)

    frames = []
    camera.listen(lambda img: frames.append(img))

    # Tick >= 12 times — synchronous mode delivers sensor callbacks on same tick.
    # Extra ticks ensure weather (especially HardRainNight) is fully rendered.
    for i in range(15):
        try:
            world.tick()
        except Exception as exc:
            print(f"[camera]  WARN: tick {i} failed for '{label}': {exc}")

    camera.stop()

    success = False
    if frames:
        try:
            save_carla_image(frames[-1], out_path)
            success = True
        except Exception as exc:
            print(f"[camera]  ERROR: failed saving '{label}': {exc}")
    else:
        print(f"[camera]  WARN: no frames received for '{label}'.")

    try:
        camera.destroy()
        try:
            all_actors.remove(camera)
        except ValueError:
            pass
    except Exception:
        pass

    return success


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

_Z_LADDER = [0.5, 0.7, 1.0, 1.5, 2.0]


def spawn_vehicle(world, bp_lib, entry, fallback_spawn_points):
    """Spawn a vehicle with a z-retry ladder; fall back to map spawn points."""
    bp_id = entry["blueprint"]
    bp    = find_bp(bp_lib, bp_id)
    if bp is None:
        print(f"[vehicle] WARN: blueprint '{bp_id}' not found — skipping.")
        return None

    color_str = entry.get("color", "")
    if color_str and bp.has_attribute("color"):
        try:
            bp.set_attribute("color", color_str)
        except Exception as exc:
            print(f"[vehicle] WARN: cannot set color '{color_str}': {exc}")

    t_data   = entry["transform"]
    first_z  = float(t_data.get("z", 0.5))
    base_rot = carla.Rotation(
        pitch=float(t_data.get("pitch", 0.0)),
        yaw=float(t_data.get("yaw",   0.0)),
        roll=float(t_data.get("roll",  0.0)),
    )

    seen, z_ladder = set(), []
    for z in [first_z] + _Z_LADDER:
        if z not in seen:
            seen.add(z)
            z_ladder.append(z)

    for z in z_ladder:
        tf = carla.Transform(
            carla.Location(x=float(t_data["x"]), y=float(t_data["y"]), z=z),
            base_rot,
        )
        try:
            actor = world.try_spawn_actor(bp, tf)
            if actor is not None:
                print(f"[vehicle] OK   '{bp_id}'  id={actor.id}  z={z}")
                return actor
            print(f"[vehicle] WARN: blocked at z={z} for '{bp_id}', retrying ...")
        except Exception as exc:
            print(f"[vehicle] WARN: exception at z={z} for '{bp_id}': {exc}")

    # Fall back to map-provided spawn points to guarantee at least one placement
    print(f"[vehicle] INFO: falling back to map spawn points for '{bp_id}' ...")
    for sp in fallback_spawn_points[:30]:
        try:
            actor = world.try_spawn_actor(bp, sp)
            if actor is not None:
                print(f"[vehicle] OK   '{bp_id}'  id={actor.id}  (map spawn point fallback)")
                return actor
        except Exception:
            pass

    print(f"[vehicle] FAIL: could not spawn '{bp_id}' at any location.")
    return None


def spawn_prop(world, bp_lib, entry):
    bp_id = entry["blueprint"]
    bp    = find_bp(bp_lib, bp_id)
    if bp is None:
        print(f"[prop]    WARN: blueprint '{bp_id}' not found — skipping.")
        return None
    tf = build_transform(entry["transform"])
    try:
        actor = world.try_spawn_actor(bp, tf)
        if actor is not None:
            print(f"[prop]    OK   '{bp_id}'  id={actor.id}")
            return actor
        print(f"[prop]    WARN: spawn blocked (collision?) for '{bp_id}'.")
    except Exception as exc:
        print(f"[prop]    ERROR: exception spawning '{bp_id}': {exc}")
    return None


def spawn_walker(world, bp_lib, entry):
    bp_id = entry["blueprint"]
    bp    = find_bp(bp_lib, bp_id)
    if bp is None:
        print(f"[walker]  WARN: blueprint '{bp_id}' not found — skipping.")
        return None

    if bp.has_attribute("is_invincible"):
        bp.set_attribute("is_invincible", "false")

    # Clamp z >= 1.0 to avoid ground-plane collisions on spawn
    t      = dict(entry["transform"])
    t["z"] = max(float(t.get("z", 1.0)), 1.0)
    tf     = build_transform(t)

    try:
        actor = world.try_spawn_actor(bp, tf)
        if actor is not None:
            print(f"[walker]  OK   '{bp_id}'  id={actor.id}")
            return actor
        print(f"[walker]  WARN: spawn blocked (collision?) for '{bp_id}'.")
    except Exception as exc:
        print(f"[walker]  ERROR: exception spawning '{bp_id}': {exc}")
    return None


# ---------------------------------------------------------------------------
# Vehicle cleanup helper — aggressive double-pass
# ---------------------------------------------------------------------------

_VEHICLE_TYPE_KEYWORDS = ("car", "truck", "van", "motorcycle", "bicycle")


def destroy_all_vehicles_and_walkers(world, label=""):
    """
    Destroy every vehicle and walker actor currently in the world.
    Returns the number of actors destroyed.
    """
    tag = f"[cleanup{label}]"
    destroyed = 0

    # Destroy WalkerAIController actors first (avoid orphaned controllers)
    for actor in world.get_actors():
        if actor.type_id == "controller.ai.walker":
            try:
                actor.stop()
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    # Destroy all vehicles by filter
    for actor in world.get_actors().filter("vehicle.*"):
        try:
            actor.destroy()
            destroyed += 1
        except Exception:
            pass

    # Keyword scan for any vehicle-like actors that slipped through
    for actor in world.get_actors():
        tid = actor.type_id.lower()
        if any(kw in tid for kw in _VEHICLE_TYPE_KEYWORDS):
            try:
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    # Destroy all walkers
    for actor in world.get_actors():
        if actor.type_id.startswith("walker."):
            try:
                actor.destroy()
                destroyed += 1
            except Exception:
                pass

    print(f"{tag} Destroyed {destroyed} actor(s).")
    return destroyed


# ---------------------------------------------------------------------------
# Weather presets
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


# ===========================================================================
# MAIN
# ===========================================================================

os.makedirs(RENDERS_DIR, exist_ok=True)

print(f"[init]    Reading plan: {PLAN_PATH}")
plan = load_plan(PLAN_PATH)

title          = plan.get("title",          "Untitled Scene")
map_field      = plan.get("map",            "Town10HD_Opt")
map_name       = map_short_name(map_field)   # strip '/Game/Carla/Maps/' prefix
weather_name   = plan.get("weather",        "HardRainNight")
weather_custom = plan.get("weather_custom", None)

vehicles_plan = plan.get("vehicles",      [])
props_plan    = plan.get("props",         [])
walkers_plan  = plan.get("walkers",       [])
main_cam_cfg  = plan.get("camera",        {})
extra_cams    = plan.get("extra_cameras", [])

print(f"[init]    Title   : {title}")
print(f"[init]    Map     : {map_name}  (from '{map_field}')")
print(f"[init]    Weather : {weather_name}")
print(f"[init]    Vehicles: {len(vehicles_plan)}")
print(f"[init]    Props   : {len(props_plan)}")
print(f"[init]    Walkers : {len(walkers_plan)}")
print(f"[init]    Cameras : 1 main + {len(extra_cams)} extra")
print(f"[init]    Renders : {RENDERS_DIR}")

# State variables — initialised before try so finally always has them
all_ok          = False
world           = None
all_actors      = []
vehicles_ok     = []
vehicles_failed = []
props_ok        = []
props_failed    = []
walkers_ok      = []
walkers_failed  = []
render_results  = []
errors          = []

try:
    # ------------------------------------------------------------------
    # 1. Connect to CARLA server
    # ------------------------------------------------------------------
    print(f"\n[connect] Connecting to CARLA at {CARLA_HOST}:{CARLA_PORT} ...")
    client = carla.Client(CARLA_HOST, CARLA_PORT)
    client.set_timeout(CARLA_TIMEOUT)
    print(f"[connect] Server version: {client.get_server_version()}")

    # ------------------------------------------------------------------
    # 2. Load map and wait for full initialisation
    # ------------------------------------------------------------------
    print(f"[map]     Loading '{map_name}' ...")
    world = client.load_world(map_name)
    time.sleep(3)                          # Let map assets fully load
    print(f"[map]     Loaded: {map_name}")

    bp_lib       = world.get_blueprint_library()
    spawn_points = world.get_map().get_spawn_points()
    print(f"[map]     Available map spawn points: {len(spawn_points)}")

    # ------------------------------------------------------------------
    # 3. Enable synchronous mode
    # ------------------------------------------------------------------
    print("[sync]    Enabling synchronous mode ...")
    settings = world.get_settings()
    settings.synchronous_mode    = True
    settings.fixed_delta_seconds = 0.05   # 20 FPS simulation step
    world.apply_settings(settings)
    world.tick()
    print("[sync]    Synchronous mode active.")

    # ------------------------------------------------------------------
    # 4. Set weather FIRST, then tick multiple times to let it render
    # ------------------------------------------------------------------
    print(f"\n[weather] Applying '{weather_name}' weather ...")
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
        weather_label = f"{weather_name} (custom params)"
    elif weather_name in WEATHER_PRESETS:
        wp = WEATHER_PRESETS[weather_name]
        weather_label = f"{weather_name} (preset)"
    else:
        wp = WEATHER_PRESETS["ClearNoon"]
        weather_label = f"ClearNoon (fallback — '{weather_name}' not recognised)"

    world.set_weather(wp)
    world.tick()
    time.sleep(1)
    # Tick at least 5 more times before any capture to fully render weather
    for _ in range(6):
        world.tick()
    print(f"[weather] Applied: {weather_label}")
    print(f"[weather]   cloudiness={wp.cloudiness}  precipitation={wp.precipitation}  "
          f"fog={wp.fog_density}  sun_alt={wp.sun_altitude_angle}  wetness={wp.wetness}")

    # ------------------------------------------------------------------
    # 5. Clear ALL pre-existing vehicles and walkers (two-pass approach)
    #    Pass 1: destroy everything now visible
    #    Settle ticks: allow traffic-manager vehicles to appear
    #    Pass 2: catch anything that arrived after pass 1
    # ------------------------------------------------------------------
    print("\n[cleanup] Pass 1 — clearing existing vehicles and walkers ...")
    cleared = destroy_all_vehicles_and_walkers(world, label=" pass-1")

    for _ in range(5):
        world.tick()

    print("[cleanup] Pass 2 — second sweep (catches late TM-spawned vehicles) ...")
    cleared += destroy_all_vehicles_and_walkers(world, label=" pass-2")

    world.tick()
    print(f"[cleanup] Total cleared: {cleared} actor(s) across both passes.")

    # ------------------------------------------------------------------
    # 6. Spawn vehicles
    # ------------------------------------------------------------------
    print(f"\n[spawn]   Spawning {len(vehicles_plan)} vehicle(s) ...")
    for entry in vehicles_plan:
        actor = spawn_vehicle(world, bp_lib, entry, spawn_points)
        if actor:
            all_actors.append(actor)
            vehicles_ok.append(entry["blueprint"])
        else:
            vehicles_failed.append(entry["blueprint"])
    world.tick()

    # ------------------------------------------------------------------
    # 7. Spawn props
    # ------------------------------------------------------------------
    print(f"\n[spawn]   Spawning {len(props_plan)} prop(s) ...")
    for entry in props_plan:
        actor = spawn_prop(world, bp_lib, entry)
        if actor:
            all_actors.append(actor)
            props_ok.append(entry["blueprint"])
        else:
            props_failed.append(entry["blueprint"])
    world.tick()

    # ------------------------------------------------------------------
    # 8. Spawn walkers (z clamped to >= 1.0 in spawn_walker())
    # ------------------------------------------------------------------
    print(f"\n[spawn]   Spawning {len(walkers_plan)} walker(s) ...")
    for entry in walkers_plan:
        actor = spawn_walker(world, bp_lib, entry)
        if actor:
            all_actors.append(actor)
            walkers_ok.append(entry["blueprint"])
        else:
            walkers_failed.append(entry["blueprint"])
    world.tick()

    # ------------------------------------------------------------------
    # 9. Let physics settle (2 s + 20 ticks)
    # ------------------------------------------------------------------
    print("\n[settle]  Waiting for scene physics to settle ...")
    for _ in range(20):
        world.tick()
    time.sleep(2)
    print("[settle]  Scene settled.")

    # ------------------------------------------------------------------
    # 10. Position spectator to main camera view (for CARLA window preview)
    # ------------------------------------------------------------------
    if main_cam_cfg:
        spectator = world.get_spectator()
        spectator.set_transform(carla.Transform(
            carla.Location(
                x=main_cam_cfg["x"],
                y=main_cam_cfg["y"],
                z=main_cam_cfg["z"],
            ),
            carla.Rotation(
                pitch=main_cam_cfg.get("pitch", 0.0),
                yaw=main_cam_cfg.get("yaw",   0.0),
                roll=main_cam_cfg.get("roll",  0.0),
            ),
        ))
        world.tick()

    # ------------------------------------------------------------------
    # 11. Capture images — main view then each extra camera
    # ------------------------------------------------------------------
    total_cams = 1 + len(extra_cams)
    print(f"\n[capture] Capturing {total_cams} image(s) ...")

    # Main view
    main_out = os.path.join(RENDERS_DIR, "main_view.png")
    ok = capture_frame(world, bp_lib, main_cam_cfg, main_out, all_actors,
                       label="main_view")
    render_results.append({"name": "main_view", "path": main_out, "success": ok})

    # Extra cameras (names from scene_plan: overhead_wide, street_level_approach, bystander_pov)
    for ecam in extra_cams:
        cam_name = ecam.get("name", "extra_camera")
        out_path = os.path.join(RENDERS_DIR, f"{cam_name}.png")
        ok = capture_frame(world, bp_lib, ecam, out_path, all_actors,
                           label=cam_name)
        render_results.append({"name": cam_name, "path": out_path, "success": ok})

    all_ok = all(r["success"] for r in render_results)

except KeyboardInterrupt:
    print("\n[INFO] Interrupted by user.")
    errors.append("KeyboardInterrupt")

except Exception as exc:
    msg = f"{type(exc).__name__}: {exc}"
    print(f"\n[FATAL]   {msg}")
    traceback.print_exc()
    errors.append(msg)

finally:
    # ------------------------------------------------------------------
    # Write render_result.json (always, even on error)
    # ------------------------------------------------------------------
    render_result = {
        "success":          all_ok,
        "scene":            title,
        "map":              map_name,
        "weather":          weather_name,
        "actors_spawned":   len(all_actors),
        "vehicles_spawned": len(vehicles_ok),
        "vehicles_failed":  vehicles_failed,
        "props_spawned":    len(props_ok),
        "props_failed":     props_failed,
        "walkers_spawned":  len(walkers_ok),
        "walkers_failed":   walkers_failed,
        "renders":          render_results,
        "images":           [r["path"] for r in render_results if r.get("success")],
        "images_failed":    [r["path"] for r in render_results if not r.get("success")],
        "errors":           errors,
        "warnings":         vehicles_failed + props_failed + walkers_failed,
    }
    try:
        with open(RENDER_RESULT_PATH, "w") as fh:
            json.dump(render_result, fh, indent=2)
        print(f"\n[result]  Wrote: {RENDER_RESULT_PATH}")
    except Exception as write_exc:
        print(f"[result]  WARNING: could not write render_result.json: {write_exc}")

    # ------------------------------------------------------------------
    # Destroy all actors we spawned (cameras first via reversed order)
    # ------------------------------------------------------------------
    print(f"[cleanup] Destroying {len(all_actors)} spawned actor(s) ...")
    for actor in reversed(all_actors):
        try:
            actor.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Restore asynchronous mode
    # ------------------------------------------------------------------
    if world is not None:
        try:
            s = world.get_settings()
            s.synchronous_mode    = False
            s.fixed_delta_seconds = None
            world.apply_settings(s)
            print("[sync]    Restored asynchronous mode.")
        except Exception as restore_exc:
            print(f"[sync]    WARNING: could not restore async mode: {restore_exc}")

    # ------------------------------------------------------------------
    # Final summary to stdout
    # ------------------------------------------------------------------
    summary = {
        "success":          all_ok,
        "scene":            title,
        "map":              map_name,
        "weather":          weather_name,
        "actors_spawned":   len(all_actors),
        "vehicles_spawned": len(vehicles_ok),
        "vehicles_ok":      vehicles_ok,
        "vehicles_failed":  vehicles_failed,
        "props_spawned":    len(props_ok),
        "props_failed":     props_failed,
        "walkers_spawned":  len(walkers_ok),
        "walkers_failed":   walkers_failed,
        "images":           [r["path"] for r in render_results if r.get("success")],
        "images_failed":    [r["path"] for r in render_results if not r.get("success")],
    }
    print("\n--- SCENE BUILD SUMMARY ---")
    print(json.dumps(summary, indent=2))

sys.exit(0 if all_ok else 1)
