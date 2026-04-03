#!/usr/bin/env bash
set -euo pipefail

# ─── CARLA 3D Scene Generator - Run Script ────────────────────────────────────
# Starts: CARLA server (Docker), CARLA streaming server, Flask web UI
# Usage: ./run.sh [--no-carla] [--port PORT] [--gpu GPU]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-5555}"
STREAM_PORT="${STREAM_PORT:-5556}"
CARLA_GPU="${CARLA_GPU:-5}"
CARLA_PORT="${CARLA_PORT:-2000}"
CARLA_PYTHON="/home/sejain/miniconda3/envs/carla37/bin/python"
NO_CARLA=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-carla) NO_CARLA=true; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --gpu) CARLA_GPU="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Cleanup function
cleanup() {
    echo ""
    echo "  Shutting down..."
    [[ -n "${STREAM_PID:-}" ]] && kill "$STREAM_PID" 2>/dev/null && echo "  Streaming server stopped"
    [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null && echo "  Web UI stopped"
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "============================================"
echo "  CARLA 3D Scene Generator"
echo "  Multi-Agent Pipeline + Interactive 3D UI"
echo "============================================"

# ─── Check Auth ──────────────────────────────────────────────────────────────
if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]] && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then
        echo "  Auth: Claude Code credentials detected"
    else
        echo ""
        echo "  WARNING: No authentication configured!"
        echo "  Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY, or log in with 'claude login'"
        echo ""
    fi
fi

# ─── Start CARLA Server ─────────────────────────────────────────────────────
if [[ "$NO_CARLA" == "false" ]]; then
    echo ""
    echo "[1/4] Starting CARLA server (GPU: $CARLA_GPU, Port: $CARLA_PORT)..."

    docker stop carla-server 2>/dev/null || true
    docker rm carla-server 2>/dev/null || true

    docker run -d \
        --name carla-server \
        --privileged \
        --gpus "\"device=${CARLA_GPU}\"" \
        -p "${CARLA_PORT}-$((CARLA_PORT+2)):${CARLA_PORT}-$((CARLA_PORT+2))" \
        carlasim/carla:latest \
        ./CarlaUE4.sh -RenderOffScreen -nosound -carla-port="${CARLA_PORT}"

    echo "  Waiting for CARLA to start..."
    for i in $(seq 1 60); do
        if $CARLA_PYTHON -c "
import carla
c = carla.Client('localhost', $CARLA_PORT)
c.set_timeout(2.0)
w = c.get_world()
print('OK')
" 2>/dev/null | grep -q "OK"; then
            echo "  CARLA server is ready!"
            break
        fi
        if [[ $i -eq 60 ]]; then
            echo "  WARNING: CARLA may not be fully ready yet. Continuing..."
        fi
        sleep 2
    done
else
    echo ""
    echo "[1/4] Skipping CARLA server (--no-carla)"
fi

# ─── Verify CARLA Python ────────────────────────────────────────────────────
echo ""
echo "[2/4] Verifying CARLA Python environment..."
if $CARLA_PYTHON -c "import carla; print('OK')" 2>/dev/null | grep -q "OK"; then
    echo "  CARLA Python API verified."
else
    echo "  ERROR: CARLA Python API not available!"
    exit 1
fi

# ─── Kill stale processes on our ports ──────────────────────────────────────
lsof -ti:"$STREAM_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
lsof -ti:"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# ─── Start Streaming Server ─────────────────────────────────────────────────
echo ""
echo "[3/4] Starting CARLA streaming server on port $STREAM_PORT..."
$CARLA_PYTHON carla_stream.py &
STREAM_PID=$!
echo "  Stream PID: $STREAM_PID"

# Wait for streaming server to be ready
for i in $(seq 1 30); do
    if curl -s "http://localhost:$STREAM_PORT/health" 2>/dev/null | grep -q "ok"; then
        echo "  Streaming server ready!"
        break
    fi
    sleep 1
done

# ─── Start Web UI ────────────────────────────────────────────────────────────
echo ""
echo "[4/4] Starting web UI on port $PORT..."
python web.py &
WEB_PID=$!
echo "  Web PID: $WEB_PID"
sleep 1

echo ""
echo "============================================"
echo "  All services running!"
echo ""
echo "  Web UI:    http://localhost:$PORT"
echo "  Stream:    http://localhost:$STREAM_PORT/stream"
echo "  CARLA:     localhost:$CARLA_PORT"
echo ""
echo "  Controls:"
echo "    WASD     - Move camera"
echo "    Mouse    - Look around (click viewport first)"
echo "    Q/E      - Fly up/down"
echo "    Scroll   - Zoom in/out"
echo "    Shift    - Fast movement"
echo "    ESC      - Release mouse"
echo ""
echo "  Press Ctrl+C to stop all services"
echo "============================================"

# Wait for either process to exit
wait -n "$STREAM_PID" "$WEB_PID" 2>/dev/null || true
cleanup
