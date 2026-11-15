#!/usr/bin/env bash
# Arranca superfoto-unoq en el host de desarrollo (webcam 0 o demo sintetica).
#
#   scripts/run_local.sh            # demo sintetica en 8010
#   scripts/run_local.sh webcam     # /dev/video0 del host
#   scripts/run_local.sh ip http://192.168.1.23:8080
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-synthetic}"
URL="${2:-}"
PORT="${SUPERFOTO_PORT:-8010}"
PYTHON="${PYTHON:-python3}"

export SUPERFOTO_DATA_DIR="${SUPERFOTO_DATA_DIR:-$PROJECT_DIR/data}"
export SUPERFOTO_PORT="$PORT"
export SUPERFOTO_SOURCE="$SOURCE"
export PYTHONUNBUFFERED=1
[[ -n "$URL" ]] && export SUPERFOTO_SOURCE_URL="$URL"
[[ "$SOURCE" == "webcam" ]] && export SUPERFOTO_SOURCE=webcam

echo "[run_local] fuente=$SOURCE url=${URL:-auto} puerto=$PORT datos=$SUPERFOTO_DATA_DIR"
echo "[run_local] UI: http://127.0.0.1:$PORT/"
exec "$PYTHON" "$PROJECT_DIR/app/server.py" --host 0.0.0.0 --port "$PORT"
