"""Main multi-agent pipeline for CARLA 3D scene generation."""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskStartedMessage,
    TaskProgressMessage,
    TaskNotificationMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
)

from config import PipelineConfig, get_claude_auth_env, PROJECT_ROOT
from agents.definitions import build_agent_definitions, ORCHESTRATOR_PROMPT_TEMPLATE

log = logging.getLogger(__name__)


class PipelineEvent:
    """Event emitted during pipeline execution."""
    def __init__(self, event_type: str, data: dict):
        self.event_type = event_type
        self.data = data
        self.timestamp = time.time()

    def to_dict(self):
        return {"type": self.event_type, "data": self.data, "ts": self.timestamp}


def _ensure_dirs(config: PipelineConfig):
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    config.renders_dir.mkdir(parents=True, exist_ok=True)


async def run_pipeline(
    user_prompt: str,
    config: PipelineConfig = None,
    event_callback=None,
    model: str = None,
):
    """Run the multi-agent scene generation pipeline.

    Args:
        user_prompt: Natural language scene description
        config: Pipeline configuration
        event_callback: Optional callback(PipelineEvent) for streaming events
        model: Model override for orchestrator (e.g., "sonnet", "opus")

    Returns:
        dict with pipeline results including the parsed scene plan
    """
    if config is None:
        config = PipelineConfig()

    _ensure_dirs(config)

    def emit(event_type, data):
        event = PipelineEvent(event_type, data)
        if event_callback:
            event_callback(event)
        return event

    emit("pipeline_start", {"prompt": user_prompt})

    # Build agent definitions
    agents = build_agent_definitions(config)
    emit("agents_loaded", {"agents": list(agents.keys())})

    # Build orchestrator prompt
    orchestrator_prompt = ORCHESTRATOR_PROMPT_TEMPLATE.format(
        user_prompt=user_prompt,
        carla_python=config.carla_python,
        max_review_iterations=config.max_review_iterations,
        max_fix_iterations=config.max_fix_iterations,
    )

    # Auth - CLI handles its own auth via ~/.claude/.credentials.json
    auth_env = get_claude_auth_env()

    emit("orchestrator_start", {"model": model or "default"})

    start_time = time.time()
    result_text = ""
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        async for message in query(
            prompt=orchestrator_prompt,
            options=ClaudeAgentOptions(
                model=model,
                cwd=str(PROJECT_ROOT),
                allowed_tools=["Read", "Write", "Bash", "Glob", "Grep", "Agent"],
                agents=agents,
                permission_mode="acceptEdits",
                max_turns=40,
                env=auth_env,
            ),
        ):
            if isinstance(message, ResultMessage):
                result_text = getattr(message, "result", "") or ""
                emit("pipeline_complete", {
                    "result": result_text,
                    "elapsed": time.time() - start_time,
                })

            elif isinstance(message, SystemMessage):
                session_id = getattr(message, "session_id", None)
                emit("system", {"session_id": session_id})

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        emit("text", {"message": block.text})
                    elif isinstance(block, ToolUseBlock):
                        emit("tool_use", {
                            "name": block.name,
                            "input_preview": _summarize_input(block.input),
                        })
                    elif isinstance(block, ThinkingBlock):
                        emit("thinking", {"preview": block.thinking[:200]})

            elif isinstance(message, TaskStartedMessage):
                agent_type = getattr(message, "agent_type", "unknown")
                emit("subagent_start", {"agent": agent_type})

            elif isinstance(message, TaskProgressMessage):
                usage = getattr(message, "usage", None)
                if usage:
                    inp = getattr(usage, "input_tokens", 0)
                    out = getattr(usage, "output_tokens", 0)
                    total_input_tokens += inp
                    total_output_tokens += out
                    emit("subagent_progress", {
                        "input_tokens": inp,
                        "output_tokens": out,
                    })

            elif isinstance(message, TaskNotificationMessage):
                status = getattr(message, "status", None)
                status_val = status.value if hasattr(status, "value") else str(status)
                emit("subagent_done", {"status": status_val})

    except Exception as e:
        log.exception("Pipeline failed")
        emit("error", {"message": str(e)})
        return {
            "success": False,
            "error": str(e),
            "elapsed": time.time() - start_time,
        }

    elapsed = time.time() - start_time

    # Parse the approved scene_plan.json
    plan_path = config.workspace_dir / "scene_plan.json"
    plan = None
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text())
            emit("plan_parsed", {"plan_path": str(plan_path), "objects": len(plan.get("objects", []))})
        except Exception as e:
            log.warning("Failed to parse scene_plan.json: %s", e)
            emit("plan_parse_error", {"message": str(e)})

    # Collect rendered images
    images = []
    if config.renders_dir.exists():
        images = sorted([
            str(f.relative_to(PROJECT_ROOT))
            for f in config.renders_dir.glob("*.png")
        ])

    return {
        "success": True,
        "plan": plan,
        "plan_path": str(plan_path) if plan else None,
        "result": result_text,
        "images": images,
        "elapsed": elapsed,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }


def _summarize_input(inp):
    """Summarize tool input for logging."""
    if isinstance(inp, dict):
        if "prompt" in inp:
            return inp["prompt"][:100] + "..."
        if "command" in inp:
            return inp["command"][:100]
        if "file_path" in inp:
            return inp["file_path"]
    return str(inp)[:100]


async def run_cli(prompt: str, model: str = None):
    """Run pipeline from CLI with console output."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    def console_callback(event):
        t = event.event_type
        d = event.data
        if t == "pipeline_start":
            print(f"\n{'='*60}")
            print(f"  CARLA 3D Scene Generator")
            print(f"  Prompt: {d['prompt']}")
            print(f"{'='*60}\n")
        elif t == "agents_loaded":
            print(f"  Agents: {', '.join(d['agents'])}")
        elif t == "orchestrator_start":
            print(f"\n  Starting orchestrator (model: {d['model']})...")
        elif t == "text":
            print(f"  {d['message'][:200]}")
        elif t == "subagent_start":
            print(f"\n  >> Subagent started: {d['agent']}")
        elif t == "subagent_done":
            print(f"  << Subagent done: {d['status']}")
        elif t == "tool_use":
            print(f"     Tool: {d['name']} - {d.get('input_preview', '')[:80]}")
        elif t == "plan_parsed":
            print(f"  Scene plan parsed: {d.get('objects', 0)} objects")
        elif t == "pipeline_complete":
            print(f"\n{'='*60}")
            print(f"  Pipeline complete! ({d['elapsed']:.1f}s)")
            print(f"{'='*60}")
        elif t == "error":
            print(f"\n  ERROR: {d['message']}")

    result = await run_pipeline(prompt, event_callback=console_callback, model=model)

    if result["success"]:
        print(f"\n  Plan: {'found' if result.get('plan') else 'not found'}")
        print(f"  Images: {result.get('images', [])}")
        print(f"  Tokens: {result.get('total_input_tokens', 0)} in / {result.get('total_output_tokens', 0)} out")
    else:
        print(f"\n  FAILED: {result.get('error', 'unknown')}")

    return result


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "A busy city intersection at sunset with cars, pedestrians, and street furniture"
    asyncio.run(run_cli(prompt))
