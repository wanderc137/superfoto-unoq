#!/usr/bin/env bash
# Prueba real: copia un video a la UNO Q, lo procesa ahi y trae los resultados.
#
#   scripts/prueba_real_unoq.sh capturas/hoja_20261114.mp4
#   SCALE=2.0 MAX_FRAMES=9000 scripts/prueba_real_unoq.sh video.mp4
#
# Sin sudo, sin instalar nada. Usa el Python del sistema de la placa.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VIDEO="${1:-}"
SCALE="${SCALE:-1.5}"
MAX_FRAMES="${MAX_FRAMES:-0}"
REFINE="${REFINE:-8}"
RL="${RL:-15}"
PSF="${PSF:-0.6}"
QUALITY="${QUALITY:-55}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

REMOTE_HOST="${REMOTE_HOST:-192.168.1.5}"
REMOTE_PORT="${REMOTE_PORT:-2222}"
REMOTE_USER="${REMOTE_USER:-arduino}"
REMOTE_DIR="${REMOTE_DIR:-/home/arduino/superfoto-unoq}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/unoq_key}"
LOCAL_RESULTS="${LOCAL_RESULTS:-$PROJECT_DIR/resultados_real}"

[[ -n "$VIDEO" ]] || { echo "uso: $0 <video.mp4> [SCALE=1.5 MAX_FRAMES=0]" >&2; exit 2; }
[[ -f "$VIDEO" ]] || { echo "ERROR: no existe $VIDEO" >&2; exit 2; }
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9._-]{1,253}$ ]] || { echo "REMOTE_HOST invalido" >&2; exit 2; }
[[ "$REMOTE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "REMOTE_USER invalido" >&2; exit 2; }
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]{1,200}$ && "$REMOTE_DIR" != *..* ]] || {
  echo "REMOTE_DIR invalido" >&2; exit 2; }
[[ "$SCALE" =~ ^[0-9](\.[0-9]+)?$ ]] || { echo "SCALE invalido" >&2; exit 2; }
[[ "$MAX_FRAMES" =~ ^[0-9]+$ ]] || { echo "MAX_FRAMES invalido" >&2; exit 2; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p "$REMOTE_PORT")
SCP_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -P "$REMOTE_PORT")
if [[ -f "$SSH_KEY" ]]; then
  SSH_OPTS+=(-i "$SSH_KEY")
  SCP_OPTS+=(-i "$SSH_KEY")
fi
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
BASENAME="$(basename "$VIDEO")"
STAMP="$(date +%Y%m%d_%H%M%S)"
DEST_DIR="$LOCAL_RESULTS/$STAMP"

log() { printf '[prueba-real] %s\n' "$*"; }

log "verificando servicio en $REMOTE_HOST"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'python3 -c "import cv2,numpy;print(cv2.__version__, numpy.__version__)"' \
  || { echo "ERROR: la placa no responde por SSH o no tiene cv2" >&2; exit 3; }

log "copiando $BASENAME ($(du -h "$VIDEO" | cut -f1)) a la placa"
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR/data/videos'"
scp "${SCP_OPTS[@]}" -q "$VIDEO" "$REMOTE:$REMOTE_DIR/data/videos/$BASENAME"

R_DIR=$(printf '%q' "$REMOTE_DIR")
R_VIDEO=$(printf '%q' "$REMOTE_DIR/data/videos/$BASENAME")
log "procesando en la placa (escala ${SCALE}x, frames: ${MAX_FRAMES:-todos})"
echo "  (puede tardar decenas de minutos; Ctrl+C no corrompe la sesion ya guardada)"
ssh "${SSH_OPTS[@]}" "$REMOTE" "bash -lc '
  cd $R_DIR
  export SUPERFOTO_DATA_DIR=$R_DIR/data
  python3 scripts/process_video.py \
      --input $R_VIDEO \
      --scale $SCALE \
      --max-frames $MAX_FRAMES \
      --refine-iterations $REFINE \
      --rl-iterations $RL \
      --psf-sigma $PSF \
      --quality-percentile $QUALITY \
      --out $R_DIR/data \
      --progress-every 20 $EXTRA_ARGS
'"

log "ubicando la sesion generada"
SESSION_ID=$(ssh "${SSH_OPTS[@]}" "$REMOTE" "ls -1t '$REMOTE_DIR/data/sessions' | head -n 1")
[[ -n "$SESSION_ID" ]] || { echo "ERROR: no se encontro la sesion remota" >&2; exit 4; }
log "sesion: $SESSION_ID"

mkdir -p "$DEST_DIR"
log "trayendo resultados a $DEST_DIR"
ssh "${SSH_OPTS[@]}" "$REMOTE" "tar -czf - -C '$REMOTE_DIR/data/sessions' '$SESSION_ID'" \
  | tar -xzf - -C "$DEST_DIR"

# El video remoto se borra EN LA PLACA (nunca archivos locales del usuario).
# KEEP_REMOTE_VIDEO=1 lo conserva para reprocesarlo.
if [[ "${KEEP_REMOTE_VIDEO:-0}" != "1" ]]; then
  R_VIDEO=$(printf '%q' "$REMOTE_DIR/data/videos/$BASENAME")
  ssh "${SSH_OPTS[@]}" "$REMOTE" "rm -f $R_VIDEO" || true
  log "video remoto eliminado"
fi

RESULT="$DEST_DIR/$SESSION_ID"
echo
echo "=============================================================="
echo "RESULTADOS"
echo "=============================================================="
ls -lh "$RESULT" 2>/dev/null || true
echo
echo "superfoto : $RESULT/superfoto.png"
echo "comparar  : $RESULT/compare.jpg"
echo "metricas  : $RESULT/session.json"
echo
echo "Para inspeccionar con zoom profundo abri la UI (http://$REMOTE_HOST:8010)"
echo "o abri el PNG en cualquier visor de imagenes."
