"""Scene Editor — AI-powered natural language scene modifications.

Takes the current CARLA scene state + a user edit command and produces
an edit plan (JSON) that is executed directly against CARLA.

Architecture:
  1. Query CARLA for current scene state (actors, weather, map)
  2. Send state + user command to Claude for edit planning
  3. Execute edit plan actions (destroy, spawn, weather, camera)
  4. Stream progress events to frontend via callback
"""

import json
import subprocess
import time
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CARLA_PYTHON = "/home/sejain/miniconda3/envs/carla37/bin/python"


def get_scene_state(carla_host="localhost", carla_port=2000) -> dict:
    """Query CARLA for the current scene state — all actors, weather, map."""
    script = r'''
import carla, json, sys

client = carla.Client("{host}", {port})
client.set_timeout(15.0)
world = client.get_world()
m = world.get_map()
weather = world.get_weather()
spectator = world.get_spectator()
cam = spectator.get_transform()

actors = world.get_actors()
vehicles = []
props = []
walkers = []

for a in actors:
    t = a.get_transform()
    info = {{
        "id": a.id,
        "type_id": a.type_id,
        "x": round(t.location.x, 1),
        "y": round(t.location.y, 1),
        "z": round(t.location.z, 1),
        "yaw": round(t.rotation.yaw, 1),
    }}
    if a.type_id.startswith("vehicle."):
        vehicles.append(info)
    elif a.type_id.startswith("walker.pedestrian."):
        walkers.append(info)
    elif a.type_id.startswith("static.prop."):
        props.append(info)

result = {{
    "map": m.name.split("/")[-1],
    "weather": {{
        "cloudiness": weather.cloudiness,
        "precipitation": weather.precipitation,
        "precipitation_deposits": weather.precipitation_deposits,
        "wind_intensity": weather.wind_intensity,
        "sun_altitude_angle": weather.sun_altitude_angle,
        "sun_azimuth_angle": weather.sun_azimuth_angle,
        "fog_density": weather.fog_density,
    }},
    "camera": {{
        "x": round(cam.location.x, 1),
        "y": round(cam.location.y, 1),
        "z": round(cam.location.z, 1),
        "pitch": round(cam.rotation.pitch, 1),
        "yaw": round(cam.rotation.yaw, 1),
    }},
    "vehicles": vehicles,
    "props": props,
    "walkers": walkers,
    "total_actors": len(vehicles) + len(props) + len(walkers),
}}
print(json.dumps(result))
'''.format(host=carla_host, port=carla_port)

    try:
        result = subprocess.run(
            [CARLA_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return {"error": result.stderr[:500]}
        return json.loads(result.stdout.strip())
    except Exception as e:
        return {"error": str(e)}


def build_edit_prompt(scene_state: dict, user_command: str) -> str:
    """Build the prompt for the scene editor agent."""
    return f"""You are a CARLA scene editor. Given the current scene state and a user's edit command, produce a JSON edit plan.

## Current Scene State
```json
{json.dumps(scene_state, indent=2)}
```

## User's Edit Command
"{user_command}"

## Available Actions

Your edit plan is a JSON object with an "actions" array. Each action has a "type" and relevant parameters:

### 1. destroy — Remove actors from the scene
```json
{{"type": "destroy", "actor_ids": [123, 456], "reason": "User asked to remove cars"}}
```
- Use the actor IDs from the scene state above
- You can also use filters:
```json
{{"type": "destroy_by_type", "type_prefix": "vehicle.", "reason": "Remove all vehicles"}}
```

### 2. spawn — Add new actors
```json
{{"type": "spawn", "category": "vehicle", "blueprint": "vehicle.tesla.model3", "x": 10.0, "y": 5.0, "z": 0.5, "yaw": 90}}
{{"type": "spawn", "category": "prop", "blueprint": "static.prop.bench01", "x": 15.0, "y": -3.0, "z": 0.3, "yaw": 0}}
{{"type": "spawn", "category": "walker", "blueprint": "walker.pedestrian.0010", "x": 12.0, "y": 2.0, "z": 1.0, "yaw": 180}}
```

### 3. weather — Change weather
```json
{{"type": "weather", "preset": "HardRainNight"}}
```
Or custom:
```json
{{"type": "weather_custom", "cloudiness": 80, "precipitation": 60, "sun_altitude_angle": -10}}
```

### 4. camera — Move the camera
```json
{{"type": "camera", "x": 10.0, "y": 0.0, "z": 20.0, "pitch": -30.0, "yaw": 45.0}}
```

## Available Blueprints

### Vehicles
vehicle.tesla.model3, vehicle.audi.a2, vehicle.audi.tt, vehicle.bmw.grandtourer,
vehicle.dodge.charger_2020, vehicle.dodge.charger_police, vehicle.jeep.wrangler_rubicon,
vehicle.mercedes.coupe, vehicle.mercedes.coupe_2020, vehicle.toyota.prius,
vehicle.volkswagen.t2, vehicle.ford.crown, vehicle.ford.ambulance,
vehicle.nissan.patrol_2021, vehicle.nissan.micra, vehicle.mini.cooper_s_2021,
vehicle.lincoln.mkz_2020, vehicle.citroen.c3, vehicle.seat.leon,
vehicle.chevrolet.impala, vehicle.micro.microlino, vehicle.carlamotors.carlacola,
vehicle.harley-davidson.low_rider, vehicle.vespa.zx125, vehicle.kawasaki.ninja

### Props
static.prop.bench01, static.prop.bench02, static.prop.trashcan01, static.prop.trashcan02,
static.prop.trashcan03, static.prop.trafficcone01, static.prop.trafficcone02,
static.prop.constructioncone, static.prop.streetbarrier, static.prop.busstop,
static.prop.mailbox, static.prop.atm, static.prop.vendingmachine,
static.prop.plantpot01 through plantpot08, static.prop.bin,
static.prop.garbage01 through garbage05, static.prop.shoppingcart,
static.prop.trafficwarning, static.prop.streetsign,
static.prop.plastictable, static.prop.plasticchair, static.prop.table, static.prop.chair,
static.prop.barrel, static.prop.container, static.prop.box01 through box03,
static.prop.creasedbox01 through creasedbox03, static.prop.barbeque,
static.prop.slide, static.prop.swing, static.prop.swingcouch, static.prop.pergola,
static.prop.fountain, static.prop.doghouse, static.prop.gnome,
static.prop.dirtdebris01 through dirtdebris03, static.prop.brokentile01 through brokentile04

### Walkers
walker.pedestrian.0001 through walker.pedestrian.0049

### Weather Presets
ClearNoon, ClearNight, ClearSunset, CloudyNoon, CloudyNight, CloudySunset,
WetNoon, WetNight, WetSunset, SoftRainNoon, SoftRainNight, SoftRainSunset,
MidRainyNoon, MidRainyNight, MidRainSunset, HardRainNoon, HardRainNight, HardRainSunset

## Output
Respond with ONLY a valid JSON object (no markdown, no explanation):
```json
{{
  "description": "Brief description of what the edit does",
  "actions": [
    ...
  ]
}}
```

## Guidelines
- Place new objects near the current camera position for visibility
- Use the existing actor positions to place new objects in sensible locations
- For "remove" commands, identify the right actors by type_id
- For "add more X" commands, place new objects near existing similar ones
- Keep z values correct: vehicles=0.5, props=0.3, walkers=1.0
- Spread spawned objects out (not all at same position)
- Be generous — if user says "add trees", add 5-8 plantpots, not just 1
"""


def execute_edit_plan(edit_plan: dict, event_callback=None,
                      carla_host="localhost", carla_port=2000) -> dict:
    """Execute an edit plan against CARLA."""
    actions = edit_plan.get("actions", [])
    if not actions:
        return {"success": True, "message": "No actions to execute", "changes": 0}

    results = []
    total = len(actions)

    for i, action in enumerate(actions):
        action_type = action.get("type", "")

        if event_callback:
            event_callback({
                "type": "edit_action",
                "data": {
                    "index": i + 1,
                    "total": total,
                    "action_type": action_type,
                    "description": action.get("reason", action.get("blueprint", "")),
                },
            })

        if action_type == "destroy":
            actor_ids = action.get("actor_ids", [])
            script = _build_destroy_script(actor_ids, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "destroy", "result": result})

        elif action_type == "destroy_by_type":
            type_prefix = action.get("type_prefix", "")
            script = _build_destroy_by_type_script(type_prefix, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "destroy_by_type", "result": result})

        elif action_type == "spawn":
            blueprint = action.get("blueprint", "")
            x = action.get("x", 0)
            y = action.get("y", 0)
            z = action.get("z", 0.5)
            yaw = action.get("yaw", 0)
            script = _build_spawn_script(blueprint, x, y, z, yaw, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "spawn", "blueprint": blueprint, "result": result})

            if event_callback:
                success = '"success": true' in result.lower() if result else False
                event_callback({
                    "type": "edit_spawn",
                    "data": {
                        "blueprint": blueprint,
                        "index": i + 1,
                        "total": total,
                        "success": success,
                    },
                })

            time.sleep(0.3)  # Pacing for live view

        elif action_type == "weather":
            preset = action.get("preset", "ClearNoon")
            script = _build_weather_script(preset, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "weather", "result": result})

        elif action_type == "weather_custom":
            params = {k: v for k, v in action.items() if k != "type"}
            script = _build_weather_custom_script(params, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "weather_custom", "result": result})

        elif action_type == "camera":
            x = action.get("x", 0)
            y = action.get("y", 0)
            z = action.get("z", 20)
            pitch = action.get("pitch", -30)
            yaw = action.get("yaw", 0)
            script = _build_camera_script(x, y, z, pitch, yaw, carla_host, carla_port)
            result = _run_carla_script(script)
            results.append({"type": "camera", "result": result})

        else:
            results.append({"type": action_type, "result": "unknown action type"})

    return {
        "success": True,
        "description": edit_plan.get("description", ""),
        "actions_executed": len(results),
        "results": results,
    }


def _run_carla_script(script: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            [CARLA_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return json.dumps({"success": False, "error": result.stderr[:300]})
        return output
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _build_destroy_script(actor_ids, host, port):
    ids_str = ",".join(str(i) for i in actor_ids)
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
ids = [{ids_str}]
destroyed = 0
for aid in ids:
    actor = world.get_actor(aid)
    if actor:
        actor.destroy()
        destroyed += 1
print(json.dumps({{"success": True, "destroyed": destroyed}}))
'''


def _build_destroy_by_type_script(type_prefix, host, port):
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
destroyed = 0
for a in world.get_actors():
    if a.type_id.startswith("{type_prefix}"):
        a.destroy()
        destroyed += 1
print(json.dumps({{"success": True, "destroyed": destroyed, "type_prefix": "{type_prefix}"}}))
'''


def _build_spawn_script(blueprint, x, y, z, yaw, host, port):
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
bp_lib = world.get_blueprint_library()

# Fuzzy blueprint match
bp = None
try:
    bp = bp_lib.find("{blueprint}")
except:
    results = bp_lib.filter("*" + "{blueprint}".split(".")[-1] + "*")
    if len(results) > 0:
        bp = results[0]

if bp is None:
    print(json.dumps({{"success": False, "error": "Blueprint not found: {blueprint}"}}))
else:
    t = carla.Transform(
        carla.Location(x={x}, y={y}, z={z}),
        carla.Rotation(yaw={yaw})
    )
    actor = world.try_spawn_actor(bp, t)
    if actor:
        print(json.dumps({{"success": True, "actor_id": actor.id, "blueprint": actor.type_id}}))
    else:
        # Try higher z values
        for zz in [1.0, 1.5, 2.0]:
            t2 = carla.Transform(carla.Location(x={x}, y={y}, z=zz), carla.Rotation(yaw={yaw}))
            actor = world.try_spawn_actor(bp, t2)
            if actor:
                print(json.dumps({{"success": True, "actor_id": actor.id, "blueprint": actor.type_id, "z_adjusted": zz}}))
                break
        else:
            print(json.dumps({{"success": False, "error": "Spawn failed at ({x}, {y}) - location blocked"}}))
'''


def _build_weather_script(preset, host, port):
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
weather = getattr(carla.WeatherParameters, "{preset}", carla.WeatherParameters.ClearNoon)
world.set_weather(weather)
print(json.dumps({{"success": True, "weather": "{preset}"}}))
'''


def _build_weather_custom_script(params, host, port):
    param_str = ", ".join(f"{k}={v}" for k, v in params.items())
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
w = world.get_weather()
params = {json.dumps(params)}
for k, v in params.items():
    if hasattr(w, k):
        setattr(w, k, v)
world.set_weather(w)
print(json.dumps({{"success": True, "params": params}}))
'''


def _build_camera_script(x, y, z, pitch, yaw, host, port):
    return f'''
import carla, json
client = carla.Client("{host}", {port})
client.set_timeout(10.0)
world = client.get_world()
spectator = world.get_spectator()
t = carla.Transform(
    carla.Location(x={x}, y={y}, z={z}),
    carla.Rotation(pitch={pitch}, yaw={yaw}, roll=0)
)
spectator.set_transform(t)
print(json.dumps({{"success": True, "camera": {{"x": {x}, "y": {y}, "z": {z}, "pitch": {pitch}, "yaw": {yaw}}}}}))
'''
