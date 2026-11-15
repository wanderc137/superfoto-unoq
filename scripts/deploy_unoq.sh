#!/usr/bin/env bash
# Despliegue de superfoto-unoq en la Arduino UNO Q por SSH (sin sudo, sin rsync).
#
#   scripts/deploy_unoq.sh              # copia, reinicia y verifica
#   scripts/deploy_unoq.sh --no-restart # solo copia
#   scripts/deploy_unoq.sh --stop       # detiene el servicio remoto
#   scripts/deploy_unoq.sh --via-adb    # fallback por ADB
#
# Preserva data/sessions y data/videos entre despliegues (leccion D-10).
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-192.168.1.5}"
REMOTE_PORT="${REMOTE_PORT:-2222}"
REMOTE_USER="${REMOTE_USER:-arduino}"
REMOTE_DIR="${REMOTE_DIR:-/home/arduino/superfoto-unoq}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/unoq_key}"
PORT="${SUPERFOTO_PORT:-8010}"
SERVICE_NAME="superfoto-unoq"

# ---- Allowlists (P-05): nada de valores arbitrarios en comandos remotos ----
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9._-]{1,253}$ ]] || { echo "REMOTE_HOST invalido" >&2; exit 2; }
[[ "$REMOTE_PORT" =~ ^[0-9]{1,5}$ ]] || { echo "REMOTE_PORT invalido" >&2; exit 2; }
[[ "$REMOTE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "REMOTE_USER invalido" >&2; exit 2; }
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]{1,200}$ && "$REMOTE_DIR" != *..* ]] || {
  echo "REMOTE_DIR invalido (ruta absoluta sin '..')" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || { echo "SUPERFOTO_PORT invalido" >&2; exit 2; }
[[ "$PORT" != "8000" ]] || { echo "El puerto 8000 es de detector-tarjetas: no tocar" >&2; exit 2; }

MODE="deploy"
[[ "${1:-}" == "--stop" ]] && MODE="stop"
[[ "${1:-}" == "--no-restart" ]] && MODE="deploy-norestart"
[[ "${1:-}" == "--via-adb" ]] && MODE="adb"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -p "$REMOTE_PORT")
SCP_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -P "$REMOTE_PORT")
if [[ -f "$SSH_KEY" ]]; then
  SSH_OPTS+=(-i "$SSH_KEY")
  SCP_OPTS+=(-i "$SSH_KEY")
fi
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

log() { printf '[deploy] %s\n' "$*"; }

remote_stop() {
  local rdir port
  rdir=$(printf '%q' "$REMOTE_DIR")
  port=$PORT
  ssh "${SSH_OPTS[@]}" "$REMOTE" "bash -lc '
    dir=$rdir
    pidfile=\"\$dir/data/superfoto.pid\"
    if [ -f \"\$pidfile\" ]; then
      pid=\$(cat \"\$pidfile\" 2>/dev/null || true)
      if [ -n \"\$pid\" ] && kill -0 \"\$pid\" 2>/dev/null; then
        cmd=\$(tr \"\\0\" \" \" < /proc/\$pid/cmdline 2>/dev/null || true)
        case \"\$cmd\" in
          *superfoto-unoq*) kill \"\$pid\" 2>/dev/null || true; sleep 1 ;;
        esac
      fi
      rm -f \"\$pidfile\"
    fi
    # Solo procesos de ESTE proyecto (nunca detector-tarjetas).
    pkill -f \"\$dir/app/server.py\" 2>/dev/null || true
    exit 0
  '"
}

if [[ "$MODE" == "stop" ]]; then
  log "deteniendo servicio remoto en $REMOTE"
  remote_stop
  log "detenido"
  exit 0
fi

if [[ "$MODE" == "adb" ]]; then
  # Fallback: la UNO Q tambien expone ADB. El lado Linux va sobre ADB shell.
  command -v adb >/dev/null 2>&1 || { echo "adb no esta instalado" >&2; exit 3; }
  adb devices | grep -qE '^[0-9A-Za-z]+\s+device$' || { echo "no hay dispositivo ADB" >&2; exit 3; }
  TMP_TAR=$(mktemp /tmp/superfoto-XXXXXX.tar.gz)
  tar -C "$PROJECT_DIR" \
      --exclude='./data' --exclude='./.git' --exclude='./.venv' \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='./build' \
      -czf "$TMP_TAR" ./app ./web ./scripts ./systemd ./config.example ./requirements.txt
  log "copiando por adb (${REMOTE_DIR})"
  adb push "$TMP_TAR" /tmp/superfoto-deploy.tar.gz >/dev/null
  adb shell "mkdir -p '$REMOTE_DIR' && tar -xzf /tmp/superfoto-deploy.tar.gz -C '$REMOTE_DIR' && rm -f /tmp/superfoto-deploy.tar.gz"
  rm -f "$TMP_TAR"
  log "desplegado por adb (arranque manual: cd $REMOTE_DIR && nohup python3 app/server.py &)"
  exit 0
fi

log "empaquetando $PROJECT_DIR"
TAR_STREAM=$(mktemp /tmp/superfoto-deploy-XXXXXX.tar.gz)
tar -C "$PROJECT_DIR" \
    --exclude='./data' --exclude='./.git' --exclude='./.venv' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='./build' \
    --exclude='./capturas' --exclude='*.mp4' --exclude='PLAN.md' \
    -czf "$TAR_STREAM" ./app ./web ./scripts ./systemd ./docs ./config.example ./requirements.txt ./README.md

R_DIR=$(printf '%q' "$REMOTE_DIR")
log "copiando a $REMOTE:$REMOTE_DIR (tar sobre ssh)"
scp "${SCP_OPTS[@]}" -q "$TAR_STREAM" "$REMOTE:/tmp/superfoto-deploy.tar.gz"
rm -f "$TAR_STREAM"
ssh "${SSH_OPTS[@]}" "$REMOTE" "bash -lc '
  set -e
  dir=$R_DIR
  mkdir -p \"\$dir\"
  tar -xzf /tmp/superfoto-deploy.tar.gz -C \"\$dir\"
  rm -f /tmp/superfoto-deploy.tar.gz
  mkdir -p \"\$dir/data/sessions\" \"\$dir/data/previews\" \"\$dir/data/videos\"
  chmod +x \"\$dir/scripts/\"*.sh 2>/dev/null || true
  echo \"[deploy] archivos en \$dir: \$(ls -1 \"\$dir/app\"/*.py | wc -l) modulos python\"
  du -sh \"\$dir/data/sessions\" 2>/dev/null || true
'"

if [[ "$MODE" == "deploy-norestart" ]]; then
  log "copiado (sin reiniciar)"
  exit 0
fi

log "reiniciando servicio (sin sudo)"
remote_stop
ssh "${SSH_OPTS[@]}" "$REMOTE" "bash -lc '
  dir=$R_DIR
  cd \"\$dir\"
  export SUPERFOTO_DATA_DIR=\"\$dir/data\"
  export SUPERFOTO_PORT=$PORT
  export PYTHONUNBUFFERED=1
  nohup python3 \"\$dir/app/server.py\" --host 0.0.0.0 --port $PORT \
      > \"\$dir/data/server.log\" 2>&1 &
  echo \$! > \"\$dir/data/superfoto.pid\"
  sleep 2
  echo \"[deploy] pid \$(cat \$dir/data/superfoto.pid)\"
  tail -n 5 \"\$dir/data/server.log\" || true
'"

log "verificando http://$REMOTE_HOST:$PORT/api/health"
for i in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://$REMOTE_HOST:$PORT/api/health" >/tmp/superfoto-health.json 2>/dev/null; then
    log "OK: $(cat /tmp/superfoto-health.json)"
    rm -f /tmp/superfoto-health.json
    log "UI en http://$REMOTE_HOST:$PORT/"
    exit 0
  fi
  sleep 1
done

echo "[deploy] ERROR: el servicio no respondio en 20 s. Ultimas lineas del log:" >&2
ssh "${SSH_OPTS[@]}" "$REMOTE" "tail -n 30 $R_DIR/data/server.log 2>/dev/null || true" >&2
exit 1
