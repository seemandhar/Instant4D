"""Configuration and authentication for CARLA 3D Scene Generator."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.resolve()
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
RENDERS_DIR = PROJECT_ROOT / "renders"
CARLA_PYTHON = "/home/sejain/miniconda3/envs/carla37/bin/python"


@dataclass
class PipelineConfig:
    carla_host: str = "localhost"
    carla_port: int = 2000
    carla_python: str = CARLA_PYTHON
    workspace_dir: Path = WORKSPACE_DIR
    renders_dir: Path = RENDERS_DIR
    image_width: int = 1280
    image_height: int = 720
    max_review_iterations: int = 2
    max_fix_iterations: int = 2
    default_map: str = "/Game/Carla/Maps/Town10HD_Opt"


def get_claude_auth_env() -> dict:
    """Build env dict for Claude Agent SDK authentication.

    The SDK spawns a Claude Code CLI subprocess which handles its own auth
    via ~/.claude/.credentials.json. We only need to pass env vars if the
    user explicitly sets ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY.
    """
    auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if auth_token:
        log.info("Using OAuth authentication (env var)")
        return {"ANTHROPIC_AUTH_TOKEN": auth_token}
    elif api_key:
        log.info("Using API key authentication (env var)")
        return {"ANTHROPIC_API_KEY": api_key}
    else:
        # Claude Code CLI will use its own credentials from ~/.claude/.credentials.json
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if creds_path.exists():
            log.info("Claude Code credentials found - CLI will handle auth")
        else:
            log.warning("No authentication found! Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, or log in with 'claude login'")
        return {}
