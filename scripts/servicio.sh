#!/usr/bin/env bash
# servicio.sh - arranque, parada y estado de SuperFoto UNO Q sin sudo.
#
# Pensado para la Arduino UNO Q (Debian 13, usuario arduino), pero funciona en
# cualquier Linux con python3 + OpenCV. No usa sudo, no toca systemd y no toca
# el puerto 8000 (reservado a detector-tarjetas).
#
# Uso:
#   scripts/servicio.sh start          # arranca en segundo plano
#   scripts/servicio.sh stop           # detiene el proceso
#   scripts/servicio.sh restart        # stop + start
#   scripts/servicio.sh status         # estado, PID, puerto y /api/health
#   scripts/servicio.sh logs [N]       # ultimas N lineas del log (80 por defecto)
#   scripts/servicio.sh boot-enable    # arranque automatico por cron (@reboot)
#   scripts/servicio.sh boot-disable   # quita el arranque automatico
#   scripts/servicio.sh boot-status    # muestra si el arranque automatico esta activo
#   scripts/servicio.sh help
#
# Variables de entorno:
#   SUPERFOTO_PORT       puerto HTTP (default 8010; 8000 prohibido)
#   SUPERFOTO_HOST       bind (default 0.0.0.0)
#   SUPERFOTO_DATA_DIR   datos/PID/log (default <proyecto>/data)
#   PYTHON               interprete (default python3)
#
# Archivos de estado:
#   data/superfoto.pid   PID del servidor
#   data/server.log      stdout/stderr del servidor
#   data/servicio.lock   lock para evitar start/stop simultaneos
#
# El script es idempotente: start dos veces no duplica el proceso y stop dos
# veces no falla. Si el puerto esta ocupado por un proceso ajeno, se niega a
# matarlo.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
HOST="${SUPERFOTO_HOST:-0.0.0.0}"
PORT="${SUPERFOTO_PORT:-8010}"
DATA_DIR="${SUPERFOTO_DATA_DIR:-$ROOT/data}"
PID_FILE="$DATA_DIR/superfoto.pid"
LOG_FILE="$DATA_DIR/server.log"
LOCK_FILE="$DATA_DIR/servicio.lock"
SERVER="$ROOT/app/server.py"
CRON_TAG="# superfoto-unoq-service"
CRON_LINE="@reboot $SCRIPT_DIR/servicio.sh start >/dev/null 2>&1 $CRON_TAG"
LOG_MAX_BYTES=5242880

LOCKED=0

die() { printf 'servicio: %s\n' "$*" >&2; exit 1; }
info() { printf 'servicio: %s\n' "$*"; }

case "$PORT" in
  ''|*[!0-9]*) die "SUPERFOTO_PORT debe ser un numero entero (1-65535)" ;;
esac
if [ "$PORT" -eq 8000 ]; then
  die "el puerto 8000 esta reservado para detector-tarjetas; usa 8010"
fi
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  die "SUPERFOTO_PORT fuera de rango (1-65535)"
fi

usage() {
  cat <<'EOF'
Uso: scripts/servicio.sh <comando>

Comandos:
  start          Arranca el servidor en segundo plano (idempotente).
  stop           Detiene el servidor (TERM y, si hace falta, KILL).
  restart        stop + start.
  status         Muestra estado, PID, URL y /api/health.
  logs [N]       Muestra las ultimas N lineas de data/server.log (default 80).
  boot-enable    Agrega "@reboot .../servicio.sh start" al crontab del usuario.
  boot-disable   Quita esa linea del crontab.
  boot-status    Indica si el arranque automatico esta activo.
  help           Esta ayuda.

Variables:
  SUPERFOTO_PORT (default 8010), SUPERFOTO_HOST (default 0.0.0.0),
  SUPERFOTO_DATA_DIR (default <proyecto>/data), PYTHON (default python3).

El puerto 8000 esta prohibido: pertenece a detector-tarjetas.
EOF
}

lock() {
  [[ "$LOCKED" == "1" ]] && return 0
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE" || die "no puedo abrir $LOCK_FILE"
    flock -n 9 || die "otra operacion de servicio esta en curso"
  fi
  LOCKED=1
}

read_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local raw
  raw=$(head -n 1 "$PID_FILE" 2>/dev/null | tr -cd '0-9')
  [[ -n "$raw" ]] || return 1
  printf '%s' "$raw"
}

pid_alive() {
  [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

pid_is_ours() {
  local pid="${1:-}" cmd
  [[ -n "$pid" ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [[ "$cmd" == *"$SERVER"* || "$cmd" == *"app/server.py"* ]]
}

running_pid() {
  local pid
  pid=$(read_pid) || true
  if pid_alive "$pid" && pid_is_ours "$pid"; then
    printf '%s' "$pid"
    return 0
  fi
  if [[ -f "$PID_FILE" ]]; then
    rm -f "$PID_FILE"
  fi
  return 1
}

port_busy() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"
}

server_pid_by_pgrep() {
  command -v pgrep >/dev/null 2>&1 || return 1
  pgrep -f -- "$SERVER" 2>/dev/null | head -n 1
}

health_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1
  else
    "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
port = sys.argv[1]
urllib.request.urlopen("http://127.0.0.1:%s/api/health" % port, timeout=2).read()
PY
  fi
}

rotate_log() {
  [[ -f "$LOG_FILE" ]] || return 0
  local size
  size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
  if [[ "$size" =~ ^[0-9]+$ ]] && (( size > LOG_MAX_BYTES )); then
    mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    info "log rotado a $LOG_FILE.1"
  fi
}

cmd_start() {
  lock
  local pid
  pid=$(running_pid) || true
  if [[ -n "$pid" ]]; then
    info "ya esta corriendo (pid $pid)"
    return 0
  fi

  if port_busy; then
    # Si el puerto lo ocupa este mismo proyecto (p. ej. arrancado por
    # deploy_unoq.sh), lo adoptamos en vez de negarnos.
    pid=$(server_pid_by_pgrep) || true
    if [[ -n "$pid" ]] && pid_is_ours "$pid"; then
      printf '%s\n' "$pid" > "$PID_FILE"
      info "ya esta corriendo (pid $pid, adoptado)"
      return 0
    fi
    die "el puerto $PORT esta ocupado por otro proceso; no lo toco. Revisa: ss -ltnp | grep :$PORT"
  fi

  command -v "$PYTHON" >/dev/null 2>&1 || die "no encuentro el interprete '$PYTHON'"
  [[ -f "$SERVER" ]] || die "no encuentro $SERVER"
  if ! "$PYTHON" -c 'import cv2, numpy' >/dev/null 2>&1; then
    die "$PYTHON no puede importar cv2/numpy. En la UNO Q deben venir de python3-opencv y python3-numpy."
  fi

  mkdir -p "$DATA_DIR"
  rotate_log
  info "iniciando en $HOST:$PORT (datos: $DATA_DIR)"
  (
    cd "$ROOT" || exit 1
    # 9>&- cierra el fd del lock en el hijo: si lo heredara, el servidor
    # quedaria holding el flock y un stop/restart posterior fallaria.
    SUPERFOTO_DATA_DIR="$DATA_DIR" \
    SUPERFOTO_PORT="$PORT" \
    PYTHONUNBUFFERED=1 \
      nohup "$PYTHON" "$SERVER" --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 9>&- &
    echo $! > "$PID_FILE"
  )

  local i
  for i in $(seq 1 20); do
    pid=$(read_pid) || true
    if pid_alive "$pid" && health_ok; then
      info "OK pid $pid - http://127.0.0.1:$PORT/"
      return 0
    fi
    sleep 0.5
  done

  info "ERROR: el servidor no respondio en 10 s. Ultimas lineas del log:"
  tail -n 30 "$LOG_FILE" 2>/dev/null || true
  return 1
}

cmd_stop() {
  lock
  local pid
  pid=$(running_pid) || true
  if [[ -z "$pid" ]]; then
    # Compatibilidad: proceso arrancado por deploy sin PID file.
    pid=$(server_pid_by_pgrep) || true
  fi
  if [[ -z "$pid" ]] || ! pid_alive "$pid"; then
    info "no esta corriendo"
    rm -f "$PID_FILE"
    return 0
  fi
  if ! pid_is_ours "$pid"; then
    die "el PID $pid no parece ser SuperFoto; no lo toco"
  fi

  info "deteniendo pid $pid"
  kill -TERM "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    pid_alive "$pid" || break
    sleep 0.5
  done
  if pid_alive "$pid"; then
    info "no termino en 10 s; enviando KILL"
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  info "detenido"
  return 0
}

cmd_status() {
  local pid
  pid=$(running_pid) || true
  if [[ -n "$pid" ]]; then
    local ip
    # Preferir la IP de la ruta por defecto (LAN), no la primera de
    # hostname -I, que en la UNO Q puede ser la bridge de Docker (172.17.x).
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    [[ -n "$ip" ]] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [[ -n "$ip" ]] || ip="127.0.0.1"
    echo "estado:  corriendo"
    echo "pid:     $pid"
    echo "url:     http://$ip:$PORT/"
    echo "datos:   $DATA_DIR"
    echo "log:     $LOG_FILE"
    if health_ok; then
      echo "health:  OK"
      if command -v curl >/dev/null 2>&1; then
        curl -s --max-time 2 "http://127.0.0.1:$PORT/api/health" | head -c 700
        echo
      fi
    else
      echo "health:  sin respuesta (revisa el log)"
    fi
    return 0
  fi

  echo "estado:  detenido"
  echo "datos:   $DATA_DIR"
  if port_busy; then
    echo "aviso:   el puerto $PORT esta ocupado por otro proceso"
    ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}\b" || true
  fi
  return 3
}

cmd_logs() {
  local n="${1:-80}"
  [[ "$n" =~ ^[0-9]+$ ]] || n=80
  [[ -f "$LOG_FILE" ]] || die "no hay log en $LOG_FILE"
  tail -n "$n" "$LOG_FILE"
}

require_crontab() {
  command -v crontab >/dev/null 2>&1 || die "crontab no esta instalado"
}

cmd_boot_enable() {
  require_crontab
  local tmp
  tmp=$(mktemp) || die "no puedo crear un temporal"
  crontab -l 2>/dev/null | grep -vF "$CRON_TAG" > "$tmp" || true
  printf '%s\n' "$CRON_LINE" >> "$tmp"
  if ! crontab "$tmp"; then
    rm -f "$tmp"
    die "no pude instalar el crontab"
  fi
  rm -f "$tmp"
  info "arranque automatico habilitado:"
  crontab -l 2>/dev/null | grep -F "$CRON_TAG" || true
}

cmd_boot_disable() {
  require_crontab
  local tmp
  tmp=$(mktemp) || die "no puedo crear un temporal"
  crontab -l 2>/dev/null | grep -vF "$CRON_TAG" > "$tmp" || true
  if ! crontab "$tmp"; then
    rm -f "$tmp"
    die "no pude actualizar el crontab"
  fi
  rm -f "$tmp"
  info "arranque automatico deshabilitado"
}

cmd_boot_status() {
  require_crontab
  if crontab -l 2>/dev/null | grep -qF "$CRON_TAG"; then
    echo "arranque automatico: habilitado"
    crontab -l 2>/dev/null | grep -F "$CRON_TAG"
    return 0
  fi
  echo "arranque automatico: deshabilitado"
  return 1
}

case "${1:-help}" in
  start)       cmd_start ;;
  stop)        cmd_stop ;;
  restart)     cmd_stop || exit $?; cmd_start ;;
  status)      cmd_status ;;
  logs)        shift; cmd_logs "${1:-80}" ;;
  boot-enable) cmd_boot_enable ;;
  boot-disable) cmd_boot_disable ;;
  boot-status) cmd_boot_status ;;
  help|-h|--help) usage ;;
  *) usage; exit 2 ;;
esac
