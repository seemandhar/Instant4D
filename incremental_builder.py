"""
incremental_builder.py  --  Python 3.10+ wrapper for incremental CARLA scene building.

This module is imported by web.py (which runs Python 3.10+).  Since the CARLA
Python API requires Python 3.7, the actual CARLA interaction is delegated to
carla_builder_worker.py which runs as a subprocess under the carla37 conda env.

Communication protocol:
  - The wrapper writes the scene plan to a temp JSON file.
  - It launches the worker subprocess with the carla37 interpreter.
  - The worker prints one JSON object per line to stdout.
  - The wrapper reads stdout line-by-line and invokes the event_callback
    for each parsed event.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Path to the carla37 Python interpreter
CARLA_PYTHON = "/home/sejain/miniconda3/envs/carla37/bin/python"

# Path to the worker script (same directory as this file)
WORKER_SCRIPT = str(Path(__file__).parent / "carla_builder_worker.py")

# Default renders directory
DEFAULT_RENDERS_DIR = str(Path(__file__).parent / "renders")


class IncrementalBuilder:
    """Builds a CARLA scene incrementally from a scene_plan dict.

    The build runs in a subprocess (Python 3.7 + CARLA API).  Progress
    events are streamed back via an event_callback function.

    Usage::

        def on_event(event: dict):
            print(event["type"], event["data"])

        builder = IncrementalBuilder(event_callback=on_event)
        result = builder.build(scene_plan)
    """

    def __init__(
        self,
        event_callback: Optional[Callable[[dict], None]] = None,
        carla_python: str = CARLA_PYTHON,
        worker_script: str = WORKER_SCRIPT,
        renders_dir: Optional[str] = None,
    ):
        """
        Args:
            event_callback: Called with a dict for each worker event.
                Expected signature: callback({"type": "...", "data": {...}})
            carla_python: Path to the Python 3.7 interpreter with carla installed.
            worker_script: Path to carla_builder_worker.py.
            renders_dir: Directory for rendered images.  None = skip captures.
        """
        self.event_callback = event_callback or (lambda ev: None)
        self.carla_python = carla_python
        self.worker_script = worker_script
        self.renders_dir = renders_dir or DEFAULT_RENDERS_DIR
        self._process: Optional[subprocess.Popen] = None
        self._result: Optional[dict] = None

    def _emit(self, event: dict) -> None:
        """Safely invoke the event callback."""
        try:
            self.event_callback(event)
        except Exception as exc:
            log.warning("event_callback raised: %s", exc)

    def build(self, scene_plan: dict) -> dict:
        """Execute the scene plan synchronously and return a summary dict.

        This method blocks until the worker subprocess completes.
        Events are streamed to event_callback as they arrive.

        Args:
            scene_plan: A dict matching the scene_plan.json schema.

        Returns:
            Summary dict from the build_complete event, or an error dict.
        """
        # Ensure renders directory exists
        os.makedirs(self.renders_dir, exist_ok=True)

        # Write plan to a temp file
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="scene_plan_")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(scene_plan, fh, indent=2)
        except Exception:
            os.close(tmp_fd)
            raise

        try:
            result = self._run_worker(tmp_path)
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return result

    def build_async(self, scene_plan: dict) -> threading.Thread:
        """Start the build in a background thread.

        Returns the thread object.  The result is available via
        ``self.get_result()`` after the thread completes, or events
        can be consumed in real-time via the callback.
        """
        self._result = None

        def _target():
            self._result = self.build(scene_plan)

        t = threading.Thread(target=_target, name="incremental-builder", daemon=True)
        t.start()
        return t

    def get_result(self) -> Optional[dict]:
        """Return the last build result (set after build/build_async completes)."""
        return self._result

    def cancel(self) -> None:
        """Attempt to terminate the running worker subprocess."""
        proc = self._process
        if proc is not None and proc.poll() is None:
            log.info("Cancelling builder worker (pid=%d)", proc.pid)
            try:
                proc.terminate()
            except OSError:
                pass
            # Give it a moment, then force-kill
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

    def _run_worker(self, plan_path: str) -> dict:
        """Launch the worker subprocess and stream its JSON-line output."""
        cmd = [
            self.carla_python,
            self.worker_script,
            plan_path,
            self.renders_dir,
        ]

        log.info("Starting builder worker: %s", " ".join(cmd))
        self._emit({
            "type": "build_phase",
            "data": {"phase": "subprocess", "status": "started", "cmd": cmd},
        })

        start_time = time.time()
        last_complete_event = None
        last_error_event = None

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,  # line-buffered
                universal_newlines=True,
                env=self._build_env(),
            )
        except FileNotFoundError:
            err = {
                "type": "build_error",
                "data": {
                    "message": "carla37 python not found at: %s" % self.carla_python,
                    "elapsed": 0,
                },
            }
            self._emit(err)
            return err["data"]
        except Exception as exc:
            err = {
                "type": "build_error",
                "data": {"message": "failed to start worker: %s" % exc, "elapsed": 0},
            }
            self._emit(err)
            return err["data"]

        # Read stdout line by line
        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue

                # Try to parse as JSON
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON output from the worker (e.g. carla warnings)
                    log.debug("worker non-JSON: %s", line)
                    continue

                # Track terminal events
                event_type = event.get("type", "")
                if event_type == "build_complete":
                    last_complete_event = event
                elif event_type == "build_error":
                    last_error_event = event

                # Forward to callback
                self._emit(event)

        except Exception as exc:
            log.error("Error reading worker stdout: %s", exc)

        # Wait for process to finish and collect stderr
        try:
            _, stderr_output = self._process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            _, stderr_output = self._process.communicate()
        except Exception:
            stderr_output = ""

        returncode = self._process.returncode
        elapsed = round(time.time() - start_time, 1)
        self._process = None

        # Log stderr if non-empty (CARLA sometimes prints warnings there)
        if stderr_output and stderr_output.strip():
            for stderr_line in stderr_output.strip().split("\n"):
                log.debug("worker stderr: %s", stderr_line)

        # Return the appropriate result
        if last_complete_event is not None:
            result = last_complete_event.get("data", {})
            self._result = result
            return result

        if last_error_event is not None:
            result = last_error_event.get("data", {})
            self._result = result
            return result

        # No terminal event received -- build likely crashed
        result = {
            "success": False,
            "message": "worker exited with code %d without a terminal event" % (
                returncode if returncode is not None else -1
            ),
            "elapsed": elapsed,
            "stderr": (stderr_output or "").strip()[:2000],
        }
        self._emit({"type": "build_error", "data": result})
        self._result = result
        return result

    @staticmethod
    def _build_env() -> dict:
        """Build the environment for the worker subprocess.

        Inherits the current environment but ensures the carla37 conda
        paths are prioritized so `import carla` works.
        """
        env = os.environ.copy()
        # Ensure the carla37 conda env's site-packages are available
        carla37_prefix = "/home/sejain/miniconda3/envs/carla37"
        carla37_bin = os.path.join(carla37_prefix, "bin")

        # Prepend carla37 bin to PATH so the right python is found
        env["PATH"] = carla37_bin + ":" + env.get("PATH", "")

        # Remove any PYTHONPATH that might conflict
        env.pop("PYTHONPATH", None)

        return env
