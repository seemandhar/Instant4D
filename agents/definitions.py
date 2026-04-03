"""Agent definitions for the CARLA 3D scene generation pipeline."""

from claude_agent_sdk import AgentDefinition
from config import PipelineConfig

# --------------------------------------------------------------------------- #
# Agent prompts
# --------------------------------------------------------------------------- #

SCENE_PLANNER_PROMPT = r"""You are the Scene Planner agent for a CARLA 3D scene generation pipeline.

## Your Job
Given a user's natural-language scene description, produce a detailed JSON scene plan that can be
used to programmatically build the scene in the CARLA simulator (version 0.9.13).

## Available Assets in CARLA 0.9.13

### Maps
- Town01 through Town05 (and their _Opt variants)
- Town10HD / Town10HD_Opt (modern downtown, best visual quality)

### Vehicle blueprints (prefix: vehicle.*)
vehicle.audi.a2, vehicle.audi.tt, vehicle.chevrolet.impala, vehicle.citroen.c3,
vehicle.dodge.charger_police, vehicle.dodge.charger_2020, vehicle.ford.ambulance,
vehicle.ford.crown, vehicle.jeep.wrangler_rubicon, vehicle.lincoln.mkz_2020,
vehicle.mercedes.coupe, vehicle.mercedes.coupe_2020, vehicle.mini.cooper_s_2021,
vehicle.toyota.prius, vehicle.nissan.patrol_2021, vehicle.tesla.model3,
vehicle.harley-davidson.low_rider, vehicle.vespa.zx125, vehicle.micro.microlino,
vehicle.carlamotors.carlacola, vehicle.bmw.grandtourer, vehicle.nissan.micra,
vehicle.volkswagen.t2, vehicle.kawasaki.ninja

### Static props (prefix: static.prop.*)
mailbox, bin, swingcouch, barbeque, trashcan02, trashcan04, trashcan05,
trampoline, slide, briefcase, pergola, wateringcan, bench01, bench02, bench03,
plantpot01-08, plastictable, plasticchair, table, chair, streetbarrier,
constructioncone, trafficcone01, trafficcone02, trafficwarning,
advertisement, busstop, atm, vendingmachine, streetsign, creasedbox01-03,
box01-03, container, barrel, garbage01-06, dirtdebris01-04, brokentile01-05,
fountain, kiosk, ironplank, platformgarbage01, shoppingcart, shoppingbag,
travelcase, doghouse, gnome, swing, glasscontainer, clothcontainer

### Pedestrian walkers (prefix: walker.pedestrian.*)
walker.pedestrian.0001 through walker.pedestrian.0047

### Weather presets
ClearNoon, ClearNight, ClearSunset, CloudyNoon, CloudyNight, CloudySunset,
WetNoon, WetNight, WetSunset, WetCloudyNoon, WetCloudyNight, WetCloudySunset,
SoftRainNoon, SoftRainNight, SoftRainSunset, MidRainyNoon, MidRainyNight,
MidRainSunset, HardRainNoon, HardRainNight, HardRainSunset

## Output Format
Write a JSON file to workspace/scene_plan.json with this structure:
```json
{{
  "title": "Scene title",
  "description": "Detailed description of the scene",
  "map": "/Game/Carla/Maps/Town10HD_Opt",
  "weather": "ClearSunset",
  "weather_custom": null,
  "camera": {{
    "x": 0.0, "y": 0.0, "z": 20.0,
    "pitch": -30.0, "yaw": 0.0, "roll": 0.0,
    "fov": 90
  }},
  "vehicles": [
    {{
      "blueprint": "vehicle.tesla.model3",
      "color": "255,0,0",
      "transform": {{"x": 10.0, "y": 5.0, "z": 0.5, "pitch": 0, "yaw": 90, "roll": 0}}
    }}
  ],
  "props": [
    {{
      "blueprint": "static.prop.bench01",
      "transform": {{"x": 15.0, "y": -3.0, "z": 0.3, "pitch": 0, "yaw": 45, "roll": 0}}
    }}
  ],
  "walkers": [
    {{
      "blueprint": "walker.pedestrian.0001",
      "transform": {{"x": 12.0, "y": 2.0, "z": 1.0, "pitch": 0, "yaw": 180, "roll": 0}}
    }}
  ],
  "extra_cameras": [
    {{
      "name": "overhead",
      "x": 0.0, "y": 0.0, "z": 50.0,
      "pitch": -90.0, "yaw": 0.0, "roll": 0.0,
      "fov": 90
    }}
  ]
}}
```

## Guidelines
- Choose a map that best fits the scene description
- Place objects at realistic positions (z should be slightly above ground ~0.3-1.0 for ground objects)
- Use spawn points near interesting areas of the map
- **CRITICAL: Set weather to EXACTLY match the user's description.** If they say "rainy night", use HardRainNight. If "sunset", use ClearSunset. If "foggy morning", use a custom weather. Never default to ClearNoon unless the user asks for daytime.
- Weather mapping: night -> use *Night presets, rain -> use *Rain* presets, sunset -> use *Sunset presets
- Position camera to capture the full scene nicely
- Include 2-3 extra camera angles for variety
- Be creative with props and vehicles to match the user's vision
- For Town10HD, good spawn areas are around x=-10 to 50, y=-30 to 30
"""

OBJECT_PLACER_PROMPT = r"""You are the Object Placer agent for a CARLA 3D scene generation pipeline.

## Your Job
Read the scene plan from workspace/scene_plan.json and generate a complete Python script
that connects to CARLA and builds the scene exactly as planned.

## Important Details
- CARLA server is at localhost:2000
- Use the carla Python API (already importable)
- The script must be standalone and self-contained
- Script output path: workspace/build_scene.py
- Save rendered images to the renders/ directory
- Image resolution: {image_width}x{image_height}

## Script Requirements
1. Connect to CARLA server
2. Load the specified map and wait for it (time.sleep(3) after load)
3. Set weather FIRST, then world.tick() and time.sleep(1) to let it take effect
4. Clear existing vehicles/walkers (destroy all actors)
5. Spawn vehicles using world.get_map().get_spawn_points() for reliable positions.
   Use try_spawn_actor and if it fails, try nearby spawn points.
6. Spawn props using try_spawn_actor with the planned positions
7. Spawn walkers at planned positions (z=1.0 or higher to avoid ground collisions)
8. Set up camera sensor, wait for scene to settle (time.sleep(2) + multiple world.tick())
9. Capture images from main camera and extra cameras
10. Save images as PNG files in renders/ directory
11. Print a summary of what was placed to stdout
12. IMPORTANT: After setting weather, call world.tick() at least 5 times before capturing to ensure weather is fully rendered

## Camera Capture Pattern
```python
import carla
import numpy as np
from PIL import Image
import time
import json

client = carla.Client('localhost', 2000)
client.set_timeout(30.0)

# Load map
world = client.load_world('Town10HD_Opt')
time.sleep(2.0)  # Wait for map load

# Set spectator camera for a view
spectator = world.get_spectator()
transform = carla.Transform(
    carla.Location(x=X, y=Y, z=Z),
    carla.Rotation(pitch=P, yaw=YAW, roll=R)
)
spectator.set_transform(transform)

# For capturing images, attach an RGB camera sensor to a vehicle or fixed point
bp_lib = world.get_blueprint_library()
camera_bp = bp_lib.find('sensor.camera.rgb')
camera_bp.set_attribute('image_size_x', str(WIDTH))
camera_bp.set_attribute('image_size_y', str(HEIGHT))
camera_bp.set_attribute('fov', str(FOV))

# Spawn camera at a transform
cam_transform = carla.Transform(carla.Location(x=X, y=Y, z=Z), carla.Rotation(pitch=P, yaw=YAW))
camera = world.spawn_actor(camera_bp, cam_transform)

# Capture
image_data = []
camera.listen(lambda img: image_data.append(img))
time.sleep(2.0)  # Wait for frames
if image_data:
    img = image_data[-1]
    array = np.frombuffer(img.raw_data, dtype=np.uint8).reshape(img.height, img.width, 4)
    pil_img = Image.fromarray(array[:, :, :3])  # Drop alpha
    pil_img.save('renders/main_view.png')
camera.stop()
camera.destroy()
```

## Error Handling
- Wrap spawn calls in try/except to handle blueprint-not-found gracefully
- If a specific blueprint fails, skip it and log a warning
- Always clean up sensors after capturing

Write the complete script to workspace/build_scene.py.
""".format(image_width=1280, image_height=720)

CODE_REVIEWER_PROMPT = r"""You are the Code Reviewer agent for a CARLA 3D scene generation pipeline.

## Your Job
Review the Python script at workspace/build_scene.py for correctness and potential issues.

## Check For
1. **CARLA API correctness**: Proper use of carla.Client, world methods, blueprint library
2. **Transform values**: Reasonable x, y, z coordinates (not underground or sky-high)
3. **Blueprint names**: Must match CARLA 0.9.13 blueprint IDs exactly
4. **Camera setup**: Proper sensor attachment and image capture
5. **Resource cleanup**: All spawned actors and sensors should be destroyed or tracked
6. **Error handling**: Script shouldn't crash on individual spawn failures
7. **Image saving**: Correct numpy/PIL usage for BGRA to RGB conversion
8. **Timing**: Adequate sleep/wait times for CARLA operations

## Output
Write your review to workspace/code_review.json:
```json
{{
  "approved": true/false,
  "issues": [
    {{
      "severity": "error|warning|info",
      "line": "approximate line or section",
      "description": "What's wrong",
      "fix": "How to fix it"
    }}
  ],
  "summary": "Overall assessment"
}}
```

If approved is false, list all issues that MUST be fixed before the script can run.
If approved is true, the script is ready to execute.
"""

SCENE_RENDERER_PROMPT = r"""You are the Scene Renderer agent for a CARLA 3D scene generation pipeline.

## Your Job
1. Execute the build_scene.py script to build and render the CARLA scene
2. Capture the output and any errors
3. Report results

## Execution
Run the script using the CARLA Python environment:
```bash
cd {workspace_dir}
{carla_python} build_scene.py 2>&1
```

## After Execution
1. Check if render images were created in the renders/ directory
2. List all generated image files and their sizes
3. Report any errors encountered during execution

## If Errors Occur
- Read the error traceback carefully
- Check if it's a CARLA connection issue (server not running)
- Check if it's a blueprint issue (wrong blueprint name)
- Check if it's a Python import issue
- Write error details to workspace/render_log.txt

## Output
Write results to workspace/render_result.json:
```json
{{
  "success": true/false,
  "images": ["renders/main_view.png", ...],
  "errors": [],
  "stdout": "script output",
  "actors_spawned": 0
}}
```
""".format(workspace_dir=str(PipelineConfig().workspace_dir), carla_python=PipelineConfig().carla_python)

QUALITY_CHECKER_PROMPT = r"""You are the Quality Checker agent for a CARLA 3D scene generation pipeline.

## Your Job
Review the render results metadata to assess quality and suggest improvements.

## IMPORTANT: Do NOT read image files (*.png). They are binary and will stall you.
Only check metadata files and file existence via ls/Bash.

## Check
1. Read workspace/render_result.json to see if rendering succeeded
2. Read workspace/scene_plan.json to understand what was intended
3. Run: ls -la renders/*.png to verify images exist and have reasonable sizes
4. Compare planned objects vs actually spawned objects from render_result.json

## Assessment Criteria
- Were all planned vehicles spawned? (check render_result.json errors/warnings)
- Were all planned props placed?
- Were walkers added successfully?
- Did any objects fail to spawn?
- Do all expected image files exist with non-zero size?

## Output
Write assessment to workspace/quality_report.json:
```json
{{
  "quality_score": 0-100,
  "scene_complete": true/false,
  "missing_objects": ["list of objects that failed to spawn"],
  "suggestions": ["improvement suggestions"],
  "needs_fix": true/false,
  "fix_instructions": "what to change if needs_fix is true"
}}
```

If quality_score >= 70, the scene is acceptable.
If quality_score < 70, set needs_fix to true with specific fix instructions.
"""

SCENE_FIXER_PROMPT = r"""You are the Scene Fixer agent for a CARLA 3D scene generation pipeline.

## Your Job
Fix issues identified by the Quality Checker in the scene building script.

## Process
1. Read workspace/quality_report.json for issues and fix instructions
2. Read workspace/build_scene.py to understand current script
3. Read workspace/scene_plan.json for the intended scene
4. Read workspace/render_result.json for execution results
5. Modify workspace/build_scene.py to fix the identified issues

## Common Fixes
- Replace invalid blueprint IDs with valid alternatives
- Adjust spawn positions if objects are clipping or underground
- Fix camera positions for better composition
- Add missing error handling
- Fix timing issues (increase sleep durations)
- Correct coordinate systems

## After Fixing
Update workspace/build_scene.py with the corrected script.
Write workspace/fix_log.json:
```json
{{
  "fixes_applied": ["description of each fix"],
  "confidence": 0-100
}}
```
"""


def build_agent_definitions(config: PipelineConfig) -> dict:
    """Build all agent definitions for the pipeline."""
    return {
        "scene_planner": AgentDefinition(
            description="Plans 3D scenes from natural language descriptions, producing structured JSON scene specifications",
            prompt=SCENE_PLANNER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob"],
            model="sonnet",
        ),
        "object_placer": AgentDefinition(
            description="Generates CARLA Python scripts to build scenes from JSON plans",
            prompt=OBJECT_PLACER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob"],
            model="sonnet",
        ),
        "code_reviewer": AgentDefinition(
            description="Reviews CARLA scene-building scripts for correctness and safety",
            prompt=CODE_REVIEWER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob", "Grep"],
            model="sonnet",
        ),
        "scene_renderer": AgentDefinition(
            description="Executes scene-building scripts and captures rendered images",
            prompt=SCENE_RENDERER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob"],
            model="sonnet",
        ),
        "quality_checker": AgentDefinition(
            description="Assesses rendered scene quality and identifies missing objects",
            prompt=QUALITY_CHECKER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob"],
            model="sonnet",
        ),
        "scene_fixer": AgentDefinition(
            description="Fixes issues in scene-building scripts based on quality reports",
            prompt=SCENE_FIXER_PROMPT,
            tools=["Read", "Write", "Bash", "Glob", "Grep"],
            model="sonnet",
        ),
    }


ORCHESTRATOR_PROMPT_TEMPLATE = r"""You are the orchestrator of a multi-agent pipeline that generates 3D scenes in the CARLA simulator.

## User Request
{user_prompt}

## Pipeline Stages
Execute these stages in order. After each stage, check outputs before proceeding.

### Stage 1: Scene Planning
Use the `scene_planner` agent to create a detailed scene plan.
Tell it: "The user wants: {user_prompt}. Create a scene plan and write it to workspace/scene_plan.json"

### Stage 2: Code Generation
Use the `object_placer` agent to generate the CARLA Python script.
Tell it: "Read workspace/scene_plan.json and generate workspace/build_scene.py to build this scene in CARLA."

### Stage 3: Code Review
Use the `code_reviewer` agent to review the generated script.
Tell it: "Review workspace/build_scene.py for correctness. Write review to workspace/code_review.json"

If the review finds critical issues (approved=false), go back to Stage 2 with fix instructions.
Maximum {max_review_iterations} review iterations.

### Stage 4: Render
Use the `scene_renderer` agent to execute the script and render the scene.
Tell it: "Execute workspace/build_scene.py using {carla_python} and capture results."

### Stage 5: Quality Check
Use the `quality_checker` agent to assess the rendered output.
Tell it: "Check the render results in workspace/render_result.json and images in renders/"

### Stage 6: Fix (if needed)
If quality_checker reports needs_fix=true, use the `scene_fixer` agent to fix issues.
Then re-render (Stage 4) and re-check (Stage 5).
Maximum {max_fix_iterations} fix iterations.

## Working Directories
- workspace/: All intermediate files (plans, scripts, logs)
- renders/: Rendered images

## Important
- Always wait for each stage to complete before moving to the next
- Read outputs after each agent completes to decide next steps
- The CARLA Python executable is: {carla_python}
- CARLA server is at localhost:2000
- After the pipeline completes, summarize what was created and any issues encountered
"""
