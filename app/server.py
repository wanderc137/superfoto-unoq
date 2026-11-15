"""Servidor HTTP + API REST (solo stdlib: ``http.server.ThreadingHTTPServer``).

No se usan frameworks porque en la placa no hay pip disponible y Flask/FastAPI
no estan instalados.

Endpoints
---------
GET  /api/health                 estado general del servicio
GET  /api/status                 estado de la sesion (o idle)
GET  /api/sources                fuentes disponibles + defaults (para la UI)
GET  /api/sessions               historial
GET  /api/session/latest         ultima sesion finalizada
GET  /api/result/<id>/<file>     descarga (png|jpg|thumb|compare|reference|coverage|json)
GET  /api/preview                ultimo frame aceptado (JPEG)
GET  /api/probe?url=<ip|url>     prueba una IP Webcam con un still /photo.jpg
GET  /api/discover               busca camaras IP Webcam en la LAN
POST /api/session/start          inicia captura/fusion
POST /api/session/stop           detiene y dispara postproceso
GET  /                           UI web (web/index.html)
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np

if __package__ in (None, ""):  # permite `python app/server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.config import APP_NAME, APP_VERSION, WEB_DIR, Config
    from app.discover import discover_ip_webcam
    from app.session import SuperFotoSession
    from app.source import SOURCE_LABELS, SourceError, probe_photo
    from app.storage import Storage
else:
    from .config import APP_NAME, APP_VERSION, WEB_DIR, Config
    from .discover import discover_ip_webcam
    from .session import SuperFotoSession
    from .source import SOURCE_LABELS, SourceError, probe_photo
    from .storage import Storage

log = logging.getLogger("superfoto.server")

STATIC_ALLOWLIST = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}

CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "json": "application/json; charset=utf-8",
}


def query_int(query: str, name: str, default: int = 0) -> int:
    """Lee un parametro entero del query string de forma estructurada.

    Evita falsos positivos por substring (``preview=wait=1`` no debe activar
    ``wait``). Valores invalidos devuelven ``default``.
    """
    try:
        values = parse_qs(query or "", keep_blank_values=True).get(name)
    except ValueError:
        return default
    if not values:
        return default
    try:
        return int(str(values[0]).strip() or default)
    except (TypeError, ValueError):
        return default


class AppState:
    """Estado compartido entre el handler y el hilo principal."""

    def __init__(self, config: Config, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage or Storage(
            config.data_dir,
            keep=config.sessions_keep,
            min_free_mb=config.min_free_mb,
        )
        self.session: SuperFotoSession | None = None
        self.lock = threading.RLock()
        self.started_at = time.monotonic()
        self.default_preview: bytes | None = None
        self.discovering = threading.Event()

    # ------------------------------------------------------------------
    def current(self) -> SuperFotoSession | None:
        with self.lock:
            return self.session

    def start_session(self, payload: dict) -> dict:
        with self.lock:
            if self.session is not None and self.session.active:
                raise RuntimeError("ya hay una sesion activa")
            source_kind = str(payload.get("source") or self.config.source or "auto")
            args = payload.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            for key in ("url", "path", "index", "width", "height", "frames", "seed", "duration"):
                if key in payload and key not in args:
                    args[key] = payload[key]
            session = SuperFotoSession(
                config=self.config,
                storage=self.storage,
                source_kind=source_kind,
                source_args=args,
                scale=payload.get("scale"),
                quality_percentile=payload.get("quality_percentile"),
                min_sharpness=payload.get("min_sharpness"),
                psf_sigma=payload.get("psf_sigma"),
                rl_iterations=payload.get("rl_iterations"),
                refine_iterations=payload.get("refine_iterations"),
                target_frames=payload.get("target_frames") or payload.get("frames"),
                duration_s=payload.get("duration_s"),
                label=str(payload.get("label") or ""),
            )
            result = session.start()
            self.session = session
            return result

    def stop_session(self) -> dict:
        session = self.current()
        if session is None:
            return {"ok": False, "status": "idle", "error": "no hay sesion"}
        return session.request_stop()

    def status(self) -> dict:
        session = self.current()
        if session is None:
            base = {
                "status": "idle",
                "session_id": "",
                "seen": 0,
                "accepted": 0,
                "rejected": 0,
                "elapsed_seconds": 0,
                "progress": 0,
                "source": None,
                "has_preview": False,
                "error": "",
                "message": "Sin sesion activa",
            }
        else:
            base = session.status
        base["service"] = {
            "name": APP_NAME,
            "version": APP_VERSION,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "port": self.config.port,
            # Solo el nombre del directorio: nunca rutas absolutas del host.
            "data_dir_name": self.config.data_dir.name,
            "disk": self.storage.disk_usage(),
            "sessions_count": len(self.storage.list_sessions(limit=100)),
        }
        return base

    def latest(self) -> dict | None:
        sessions = self.storage.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def preview(self) -> bytes | None:
        session = self.current()
        if session is not None:
            data = session.preview_jpeg
            if data:
                return data
        return self.default_preview or _placeholder_jpeg()

    def health(self) -> dict:
        session = self.current()
        return {
            "ok": True,
            "name": APP_NAME,
            "version": APP_VERSION,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "state": session.state if session else "idle",
            "session_active": bool(session and session.active),
            "data_dir_name": self.config.data_dir.name,
            "disk": self.storage.disk_usage(),
            "sessions_keep": self.config.sessions_keep,
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
        }


def _placeholder_jpeg() -> bytes:
    img = np.full((360, 640, 3), 32, np.uint8)
    cv2.putText(img, "sin frame", (16, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 1,
                cv2.LINE_AA)
    cv2.putText(img, "inicia una sesion para ver el preview", (16, 76), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (150, 150, 150), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else b""


class SuperFotoHandler(BaseHTTPRequestHandler):
    server_version = f"SuperFoto/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    state: AppState  # inyectado por create_server

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # silencio en consola
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200,
                    extra: dict | None = None, download: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message, "status": status}, status)

    def _read_json(self) -> dict:
        """Body opcional (P-04): POST sin body es valido."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("body demasiado grande")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ValueError(f"JSON invalido: {exc}") from exc
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Verbos
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        st = self.state
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/favicon.ico":
                return self._serve_static("favicon.svg")
            if path == "/api/health":
                return self._send_json(st.health())
            if path == "/api/status":
                return self._send_json(st.status())
            if path == "/api/sources":
                return self._send_json({
                    "ok": True,
                    "sources": [{"id": key, "label": value} for key, value in SOURCE_LABELS.items()],
                    "defaults": {
                        "scale": st.config.default_scale,
                        "quality_percentile": st.config.quality_percentile,
                        "psf_sigma": st.config.psf_sigma,
                        "rl_iterations": st.config.rl_iterations,
                        "refine_iterations": st.config.refine_iterations,
                        "target_frames": st.config.target_frames,
                        "duration_s": st.config.duration_s,
                        "source": st.config.source,
                        "source_url": st.config.source_url,
                        "frame_timeout_ms": st.config.frame_timeout_ms,
                    },
                })
            if path == "/api/sessions":
                return self._send_json({"ok": True, "sessions": st.storage.list_sessions()})
            if path == "/api/session/latest":
                latest = st.latest()
                return self._send_json({"ok": True, "session": latest})
            if path == "/api/preview":
                data = st.preview()
                return self._send_bytes(data, "image/jpeg")
            if path == "/api/probe":
                return self._probe(parsed.query)
            if path == "/api/discover":
                return self._discover()
            if path.startswith("/api/result/"):
                return self._serve_result(path)
            if path.startswith("/api/"):
                return self._error(404, f"endpoint desconocido: {path}")
            return self._serve_static(path.lstrip("/"))
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s", path)
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_HEAD(self) -> None:  # noqa: N802
        return self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        st = self.state
        try:
            if path == "/api/session/start":
                payload = self._read_json()
                try:
                    result = st.start_session(payload)
                except SourceError as exc:
                    return self._error(400, str(exc))
                except RuntimeError as exc:
                    return self._error(409, str(exc))
                return self._send_json(result, 202)
            if path == "/api/session/stop":
                wait = query_int(parsed.query, "wait", 0) > 0
                result = st.stop_session()
                if wait:
                    session = st.current()
                    if session is not None:
                        session.stop(wait=True, timeout=300)
                        result = {"ok": True, "status": session.status}
                return self._send_json(result, 200)
            if path.startswith("/api/"):
                return self._error(404, f"endpoint desconocido: {path}")
            return self._error(405, "metodo no permitido")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s", path)
            return self._error(500, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Handlers especificos
    # ------------------------------------------------------------------
    def _serve_static(self, name: str) -> None:
        safe = Path(name).name
        content_type = STATIC_ALLOWLIST.get(safe)
        if content_type is None:
            return self._error(404, "archivo no encontrado")
        path = (WEB_DIR / safe).resolve()
        try:
            path.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error(403, "ruta no permitida")
        if not path.is_file():
            return self._error(404, f"falta {safe}")
        self._send_bytes(path.read_bytes(), content_type)

    def _serve_result(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        # /api/result/<id>/<file>
        if len(parts) != 4:
            return self._error(400, "formato: /api/result/<session_id>/<archivo>")
        _, _, session_id, key = parts
        file_path = self.state.storage.file_path(session_id, key)
        if file_path is None:
            return self._error(404, f"archivo no encontrado: {session_id}/{key}")
        ctype = CONTENT_TYPES.get(key.lower())
        if ctype is None:
            ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        name = file_path.name
        self._send_bytes(file_path.read_bytes(), ctype, download=name)

    def _probe(self, query: str) -> None:
        """GET /api/probe?url=<valor>: valida la IP/URL antes de capturar."""
        values = parse_qs(query or "").get("url") or [""]
        value = str(values[0]).strip()
        if not value:
            return self._error(
                400, "falta el parametro url (ej: /api/probe?url=192.168.1.23:8080)"
            )
        try:
            info = probe_photo(value, timeout=3.0)
        except SourceError as exc:
            # 400: valor mal formado; 502: el dispositivo no respondio.
            status = 400 if getattr(exc, "kind", "invalid") == "invalid" else 502
            return self._error(status, str(exc))
        except Exception as exc:  # noqa: BLE001 - nunca un 500 por una IP rara
            return self._error(502, f"no se pudo probar la camara: {type(exc).__name__}")
        self._send_json(info)

    def _discover(self) -> None:
        config = self.state.config
        urls = discover_ip_webcam(
            subnet=config.discover_subnet,
            ports=config.discover_ports,
            timeout=config.discover_timeout,
        )
        self._send_json({"ok": True, "cameras": urls, "subnet": config.discover_subnet})


def create_server(config: Config, storage: Storage | None = None,
                  host: str | None = None, port: int | None = None) -> tuple[ThreadingHTTPServer, AppState]:
    """Crea (sin arrancar) el servidor HTTP y su estado."""
    config.ensure_dirs()
    state = AppState(config, storage=storage)

    handler = type("BoundSuperFotoHandler", (SuperFotoHandler,), {"state": state})
    httpd = ThreadingHTTPServer((host if host is not None else config.host,
                                 int(port if port is not None else config.port)), handler)
    httpd.daemon_threads = True
    return httpd, state


def serve(config: Config | None = None) -> None:
    config = config or Config.load()
    logging.basicConfig(
        level=getattr(logging, str(config.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    httpd, state = create_server(config)
    host, port = httpd.server_address[:2]
    log.info("%s %s escuchando en http://%s:%s", APP_NAME, APP_VERSION, host, port)
    log.info("datos en %s (disco libre %.0f MB)", config.data_dir,
             state.storage.disk_usage()["free_mb"])

    stop = threading.Event()

    def shutdown(signum=None, frame=None):  # noqa: ANN001
        stop.set()
        session = state.current()
        if session is not None and session.active:
            log.info("Deteniendo sesion activa...")
            session.request_stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        session = state.current()
        if session is not None and session.active:
            session.stop(wait=False)
        httpd.server_close()
        log.info("Servidor detenido")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SuperFoto UNO Q: servicio de superresolucion")
    parser.add_argument("--host", default=None, help="interfaz (default: config/0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="puerto (default: 8010)")
    parser.add_argument("--data-dir", default=None, help="directorio de datos")
    parser.add_argument("--source", default=None, help="fuente por defecto (auto|ip|file|webcam|folder|synthetic)")
    parser.add_argument("--url", default=None, help="URL de IP Webcam")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING")
    args = parser.parse_args(argv)

    config = Config.load()
    if args.data_dir:
        config.data_dir = Path(args.data_dir).expanduser()
    if args.port:
        config.port = int(args.port)
    if args.host:
        config.host = args.host
    if args.source:
        config.source = args.source
    if args.url:
        config.source_url = args.url
    if args.log_level:
        config.log_level = args.log_level
    config.validate()
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
