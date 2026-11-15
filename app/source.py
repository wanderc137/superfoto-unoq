"""Fuentes de frames y parser MJPEG.

Fuentes soportadas:

* ``IPCameraSource``  : app IP Webcam Android (``/video`` MJPEG + ``/photo.jpg``).
* ``VideoFileSource`` : cualquier archivo que OpenCV pueda decodificar.
* ``WebcamSource``    : camara USB/V4L2 (descarta decodificadores Qualcomm Venus).
* ``ImageFolderSource``: secuencia de imagenes ordenadas.
* ``SyntheticSource`` : escena sintetica con ground truth y desplazamientos
  subpixel (demo y tests sin hardware).

Todas cumplen el mismo protocolo: ``read() -> (ok, frame)``, ``stop()``,
``is_open`` y ``meta``. ``read()`` nunca bloquea indefinidamente: los timeouts
se manejan dentro de cada fuente (P-06: siempre hay forma de desbloquear).
"""

from __future__ import annotations

import io
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import cv2
import numpy as np

log = logging.getLogger("superfoto.source")

DEFAULT_BOUNDARY = b"--myboundary"
_BOUNDARY_LINE_RE = re.compile(rb"^(?:\r?\n)?(--[^\r\n]{1,120})(?:\r\n|\n)")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")


class SourceError(RuntimeError):
    """Error de fuente con mensaje apto para mostrar en la UI.

    ``kind`` permite mapear el error a un codigo HTTP: ``invalid`` (400) para
    valores mal formados, ``connection``/``decode`` (502) para fallos del
    dispositivo remoto.
    """

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


@runtime_checkable
class FrameSource(Protocol):
    @property
    def is_open(self) -> bool: ...

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def stop(self) -> None: ...

    @property
    def meta(self) -> dict: ...


# ---------------------------------------------------------------------------
# Parser MJPEG
# ---------------------------------------------------------------------------

class MJPEGStreamParser:
    """Parser incremental de ``multipart/x-mixed-replace``.

    Tolera que los chunks de red corten en cualquier byte: mantiene un buffer
    interno y solo emite frames completos. Usa ``Content-Length`` cuando el
    servidor lo envia (IP Webcam lo hace) y cae a busqueda de boundary si no.

    IP Webcam genera un boundary **aleatorio por conexion** y lo anuncia sin
    el prefijo ``--`` (``boundary=Ba4oTvQMY8ew04N8dcnM``). Si el servidor no
    lo anuncia, el parser lo detecta de la primera linea del stream.
    """

    def __init__(self, boundary: bytes = DEFAULT_BOUNDARY, max_buffer: int = 32 * 1024 * 1024) -> None:
        self.boundary = boundary if boundary.startswith(b"--") else b"--" + boundary
        self.max_buffer = int(max_buffer)
        self._buf = bytearray()
        self._content_length: int | None = None
        self._state = 0  # 0: boundary, 1: headers, 2: payload
        self.frames = 0
        self.dropped = 0

    # -- API -----------------------------------------------------------
    def reset(self) -> None:
        self._buf.clear()
        self._content_length = None
        self._state = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Agrega bytes y devuelve la lista de payloads JPEG completos."""
        if chunk:
            self._buf.extend(chunk)
        out: list[bytes] = []
        while True:
            payload = self._next_part()
            if payload is None:
                break
            if payload:
                out.append(payload)
                self.frames += 1
            else:
                self.dropped += 1
        if len(self._buf) > self.max_buffer:
            # Stream corrupto: descartar lo viejo para no crecer sin limite.
            del self._buf[: len(self._buf) // 2]
        return out

    # -- interno -------------------------------------------------------
    def _find_boundary(self, start: int = 0) -> int:
        return bytes(self._buf).find(self.boundary, start)

    def _consume_boundary(self) -> bool:
        """Busca y consume un boundary al inicio del buffer."""
        idx = self._find_boundary()
        if idx < 0 and self._adopt_boundary_from_stream():
            idx = self._find_boundary()
        if idx < 0:
            # Conservar una cola por si el boundary viene partido (puede ser
            # largo: IP Webcam usa tokens aleatorios de ~20-40 bytes).
            keep = max(len(self.boundary) * 2, 512)
            if len(self._buf) > keep:
                del self._buf[:-keep]
            return False
        del self._buf[: idx + len(self.boundary)]
        return True

    def _adopt_boundary_from_stream(self) -> bool:
        """Deduce el boundary de la primera linea si el servidor no lo anuncio.

        IP Webcam responde a veces sin ``boundary=`` en el Content-Type. En ese
        caso el stream arranca con ``--<token>\\r\\n`` y ese token es el
        boundary real. Solo se intenta mientras siga el boundary por defecto.
        """
        if self.boundary != DEFAULT_BOUNDARY:
            return False
        match = _BOUNDARY_LINE_RE.match(bytes(self._buf))
        if not match:
            return False
        candidate = match.group(1)
        # Descartar lineas que claramente son payload/headers, no boundaries.
        if len(candidate) < 3 or b"Content-" in candidate or candidate.endswith(b"--"):
            return False
        self.boundary = candidate
        log.debug("MJPEG: boundary detectado en el stream: %r", candidate)
        return True

    def _read_headers(self) -> bool:
        """Consume headers hasta la linea vacia. True si estan completos."""
        buf = bytes(self._buf)
        end = buf.find(b"\r\n\r\n")
        sep = 4
        alt = buf.find(b"\n\n")
        if alt >= 0 and (end < 0 or alt < end):
            end, sep = alt, 2
        if end < 0:
            # Tolerar headers sin CRLF final si ya hay un JPEG.
            if b"\xff\xd8" in buf:
                self._content_length = None
                self._state = 2
                return True
            if len(self._buf) > 16384:
                self._buf.clear()
            return False
        headers = buf[:end].decode("latin-1", "replace")
        del self._buf[: end + sep]
        self._content_length = _parse_content_length(headers)
        self._state = 2
        return True

    def _read_payload(self) -> bytes | None:
        """Devuelve el payload completo o None si falta data."""
        if self._content_length is not None:
            if len(self._buf) < self._content_length:
                return None
            payload = bytes(self._buf[: self._content_length])
            del self._buf[: self._content_length]
            self._content_length = None
            self._state = 0
            return payload
        # Sin Content-Length: el frame termina en el proximo boundary.
        idx = self._find_boundary()
        if idx < 0:
            return None
        payload = bytes(self._buf[:idx]).strip(b"\r\n")
        del self._buf[:idx]
        self._state = 0
        return payload

    def _next_part(self) -> bytes | None:
        while True:
            if self._state == 0:
                if not self._consume_boundary():
                    return None
                self._state = 1
            elif self._state == 1:
                if not self._read_headers():
                    return None
            else:
                payload = self._read_payload()
                if payload is None:
                    return None
                return payload


def _parse_content_length(headers: str) -> int | None:
    for line in headers.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def parse_boundary(content_type: str) -> bytes:
    """Extrae el boundary de un Content-Type multipart.

    Acepta ``boundary=token``, ``boundary="token"`` y ``boundary=--token``
    (IP Webcam real anuncia el token sin ``--``). Devuelve el valor tal cual;
    ``MJPEGStreamParser`` agrega el prefijo ``--`` si falta.
    """
    match = re.search(r'boundary\s*=\s*"?([^";,]+)"?', content_type or "", re.IGNORECASE)
    if not match:
        return DEFAULT_BOUNDARY
    raw = match.group(1).strip().strip('"')
    if not raw:
        return DEFAULT_BOUNDARY
    return raw.encode("latin-1", "replace")


# ---------------------------------------------------------------------------
# Utilidades de decodificacion
# ---------------------------------------------------------------------------

def decode_image(data: bytes) -> np.ndarray | None:
    """Decodifica un buffer JPEG/PNG a BGR uint8."""
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


# ---------------------------------------------------------------------------
# IP Webcam
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_DEFAULT_IPWEBCAM_PORT = 8080
_IPWEBCAM_STREAM_PATH = "/video"
_IPWEBCAM_PHOTO_NAME = "/photo.jpg"
_MAX_PHOTO_BYTES = 12 * 1024 * 1024


def normalize_ip_webcam_url(value: str) -> str:
    """Normaliza la IP/URL de IP Webcam a una URL canonica terminada en /video.

    Acepta todas estas formas (y devuelve siempre lo mismo)::

        192.168.1.23
        192.168.1.23:8080
        http://192.168.1.23:8080
        http://192.168.1.23:8080/video
        https://192.168.1.23:8080/video
        http://192.168.1.23:8080/photo.jpg

    Reglas: si falta el esquema se usa ``http``; si falta el puerto se usa
    8080; un path vacio, ``/`` o ``/photo.jpg`` se convierte en ``/video``.
    Lanza ``SourceError(kind="invalid")`` con un mensaje claro en espanol si
    el valor no es utilizable.
    """
    if not isinstance(value, str) or not value.strip():
        raise SourceError(
            "Escribi la IP o URL del telefono (ej: 192.168.1.23:8080)", kind="invalid"
        )
    raw = value.strip()
    if len(raw) > 2048:
        raise SourceError("la URL es demasiado larga (maximo 2048 caracteres)", kind="invalid")
    if not _SCHEME_RE.match(raw):
        raw = "http://" + raw

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise SourceError(
            "URL invalida: revisa el puerto (debe estar entre 1 y 65535)", kind="invalid"
        ) from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SourceError(
            f"esquema no soportado: '{scheme or '?'}'. Usa http:// o https://", kind="invalid"
        )
    host = parts.hostname
    if not host:
        raise SourceError("la URL no tiene host/IP (ej: 192.168.1.23:8080)", kind="invalid")
    if any(ch.isspace() for ch in (parts.netloc or "")):
        raise SourceError("el host no puede tener espacios", kind="invalid")

    if port is None:
        port = _DEFAULT_IPWEBCAM_PORT
    if not 1 <= int(port) <= 65535:
        raise SourceError("puerto invalido: debe estar entre 1 y 65535", kind="invalid")

    host_part = f"[{host}]" if ":" in host else host
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += ":" + parts.password
        userinfo += "@"

    path = (parts.path or "").rstrip("/")
    low = path.lower()
    if not path:
        path = _IPWEBCAM_STREAM_PATH
    elif low.endswith(_IPWEBCAM_PHOTO_NAME):
        prefix = path[: -len(_IPWEBCAM_PHOTO_NAME)]
        path = f"{prefix}{_IPWEBCAM_STREAM_PATH}"
    elif low.endswith(_IPWEBCAM_STREAM_PATH):
        # Se conserva un prefijo propio (/cam/video) pero el sufijo se
        # canoniza a minusculas: /VIDEO y /Video -> /video.
        path = path[: -len(_IPWEBCAM_STREAM_PATH)] + _IPWEBCAM_STREAM_PATH
    else:
        # Cualquier otro path (p. ej. /stream, /mjpeg) se canoniza a /video.
        path = _IPWEBCAM_STREAM_PATH

    return f"{scheme}://{userinfo}{host_part}:{int(port)}{path}"


def ip_webcam_photo_url(url: str) -> str:
    """Deriva el still de resolucion completa a partir de la URL canonica."""
    canonical = normalize_ip_webcam_url(url)
    base = canonical[: -len(_IPWEBCAM_STREAM_PATH)]
    return f"{base}{_IPWEBCAM_PHOTO_NAME}"


def probe_photo(value: str, timeout: float = 3.0) -> dict:
    """Prueba una IP Webcam descargando un still de ``/photo.jpg``.

    Devuelve ``{ok, url, photo_url, resolution, content_type, bytes}`` o lanza
    ``SourceError`` (``kind`` = ``invalid`` | ``connection`` | ``decode``).
    Se usa desde ``GET /api/probe`` para validar la IP antes de capturar.
    """
    canonical = normalize_ip_webcam_url(value)
    photo_url = ip_webcam_photo_url(canonical)
    request = urllib.request.Request(photo_url, headers={"User-Agent": "superfoto-unoq/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            data = resp.read(_MAX_PHOTO_BYTES)
    except urllib.error.HTTPError as exc:
        raise SourceError(
            f"el telefono respondio HTTP {exc.code} al pedir la foto", kind="connection"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        host = urlsplit(canonical).netloc
        raise SourceError(
            f"no se pudo conectar con {host}: {reason}", kind="connection"
        ) from exc

    if not data:
        raise SourceError("el telefono respondio sin datos en /photo.jpg", kind="decode")
    frame = decode_image(data)
    if frame is None:
        raise SourceError(
            "la respuesta de /photo.jpg no es una imagen valida", kind="decode"
        )
    return {
        "ok": True,
        "url": canonical,
        "photo_url": photo_url,
        "resolution": [int(frame.shape[0]), int(frame.shape[1])],
        "content_type": content_type,
        "bytes": len(data),
    }


class IPCameraSource:
    """Stream MJPEG de la app IP Webcam con reconexion y backoff exponencial."""

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        frame_timeout_ms: int = 5000,
        reconnect_backoff: tuple[float, float] = (1.0, 30.0),
        prefer_still: bool = True,
        max_still_stale_s: float = 2.0,
    ) -> None:
        canonical = normalize_ip_webcam_url(url)
        self.base_url = canonical[: -len(_IPWEBCAM_STREAM_PATH)]
        self.url = canonical
        self.photo_url = ip_webcam_photo_url(canonical)
        self.timeout = float(timeout)
        self.frame_timeout_ms = int(frame_timeout_ms)
        self.backoff = (float(reconnect_backoff[0]), float(reconnect_backoff[1]))
        self.prefer_still = prefer_still
        self.max_still_stale_s = float(max_still_stale_s)

        self._lock = threading.Condition()
        self._frame: np.ndarray | None = None
        self._stamp = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._open = False
        self._connected = False
        self._error = ""
        self._reconnects = 0
        self._frames = 0
        self._photo_shape: tuple[int, int] | None = None
        self._last_photo = 0.0
        self._fps_est = 0.0

    # -- protocolo -----------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open and not self._stop.is_set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def error(self) -> str:
        return self._error

    @property
    def meta(self) -> dict:
        with self._lock:
            shape = self._frame.shape[:2] if self._frame is not None else None
        return {
            "kind": "ip_webcam",
            "url": self.base_url,
            "stream_url": self.url,
            "connected": self._connected,
            "reconnects": self._reconnects,
            "frames": self._frames,
            "fps": round(self._fps_est, 2),
            "frame_shape": list(shape) if shape else None,
            "error": self._error,
        }

    def start(self) -> "IPCameraSource":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ipcam-reader", daemon=True)
        self._thread.start()
        self._open = True
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._lock.notify_all()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._open = False
        self._connected = False

    def read(self) -> tuple[bool, np.ndarray | None]:
        deadline = time.monotonic() + self.frame_timeout_ms / 1000.0
        with self._lock:
            while True:
                if self._frame is not None and self._stamp > 0:
                    age = time.monotonic() - self._stamp
                    if age <= self.max_still_stale_s or not self.prefer_still:
                        frame = self._frame
                        self._stamp = 0.0  # consumido: el proximo read espera uno nuevo
                        return True, frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, None
                self._lock.wait(min(remaining, 0.25))

    # -- hilo de lectura ----------------------------------------------
    def _run(self) -> None:
        backoff = self.backoff[0]
        while not self._stop.is_set():
            try:
                self._stream_once()
                backoff = self.backoff[0]
                self._connected = False
                self._error = "stream finalizado"
            except Exception as exc:  # noqa: BLE001 - red: cualquier error es reintentable
                self._connected = False
                self._error = f"{type(exc).__name__}: {exc}"
                log.warning("IP Webcam: %s (reintento en %.1fs)", self._error, backoff)
            if self._stop.is_set():
                break
            self._reconnects += 1
            self._stop.wait(backoff)
            backoff = min(self.backoff[1], backoff * 1.7)

    def _stream_once(self) -> None:
        req = urllib.request.Request(self.url, headers={"User-Agent": "superfoto-unoq/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            boundary = parse_boundary(content_type)
            parser = MJPEGStreamParser(boundary)
            self._connected = True
            self._error = ""
            log.info("IP Webcam conectada: %s (boundary=%s)", self.url, boundary.decode("latin-1"))
            last_t = time.monotonic()
            while not self._stop.is_set():
                chunk = resp.read(65536)
                if not chunk:
                    raise ConnectionError("conexion cerrada por el servidor")
                for payload in parser.feed(chunk):
                    frame = decode_image(payload)
                    if frame is None:
                        parser.dropped += 1
                        continue
                    now = time.monotonic()
                    dt = now - last_t
                    last_t = now
                    if dt > 0:
                        self._fps_est = 0.8 * self._fps_est + 0.2 * (1.0 / dt)
                    with self._lock:
                        self._frame = frame
                        self._stamp = now
                        self._frames += 1
                        self._lock.notify_all()
                    if self.prefer_still and now - self._last_photo > 2.0:
                        self._last_photo = now
                        self._fetch_still_async()

    def _fetch_still_async(self) -> None:
        """Pide un still de resolucion completa en un hilo aparte (no bloquea)."""
        def worker() -> None:
            try:
                req = urllib.request.Request(self.photo_url, headers={"User-Agent": "superfoto-unoq/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                img = decode_image(data)
                if img is not None:
                    with self._lock:
                        # El still es de mayor resolucion: reemplaza el frame
                        # del stream y fija la forma esperada.
                        if self._photo_shape is None or img.shape[0] >= self._photo_shape[0]:
                            self._photo_shape = img.shape[:2]
                            self._frame = img
                            self._stamp = time.monotonic()
                            self._lock.notify_all()
            except Exception:  # pragma: no cover - el still es opcional
                pass

        threading.Thread(target=worker, name="ipcam-still", daemon=True).start()

    def grab_photo(self) -> np.ndarray | None:
        """Descarga el still de resolucion completa (``/photo.jpg``)."""
        try:
            req = urllib.request.Request(self.photo_url, headers={"User-Agent": "superfoto-unoq/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return decode_image(resp.read())
        except Exception:
            return None


def probe_ip_camera(url: str, timeout: float = 1.2) -> dict:
    """Comprueba si una URL responde como IP Webcam (no lanza).

    Devuelve ``{"ok": bool, "url", "status": int|None, "resolution": ..., "error"}``.
    """
    result = {"ok": False, "url": url, "status": None, "resolution": None, "error": ""}
    try:
        url = normalize_ip_webcam_url(url)
        result["url"] = url
    except SourceError as exc:
        result["error"] = str(exc)
        return result

    # ``url`` es canonica y termina en /video: para /status.json y /photo.jpg
    # hace falta la base sin ese sufijo. Concatenar sobre ``url`` generaba
    # /video/status.json (404) y hacia fallar camaras perfectamente validas.
    base = url[: -len(_IPWEBCAM_STREAM_PATH)] or url

    def fetch_json(path: str) -> dict | None:
        try:
            with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as resp:
                import json

                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    status = fetch_json("/status.json")
    if isinstance(status, dict):
        result["ok"] = True
        result["status"] = 200
        res = status.get("previewResolution") or status.get("resolution")
        result["resolution"] = str(res) if res else None
        return result

    # Fallback: mirar si /video responde multipart.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "superfoto-unoq/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "multipart" in ctype.lower() or "jpeg" in ctype.lower():
                result["ok"] = True
                result["status"] = resp.status
                return result
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# Archivo de video
# ---------------------------------------------------------------------------

class VideoFileSource:
    """Video local leido frame a frame (nunca se carga completo en RAM)."""

    def __init__(self, path: str | Path, loop: bool = True, max_frames: int | None = None) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise SourceError(f"archivo no encontrado: {self.path}")
        self.loop = bool(loop)
        self.max_frames = int(max_frames) if max_frames else None
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise SourceError(f"no se pudo abrir el video: {self.path.name}")
        self._lock = threading.Lock()
        self._open = True
        self._frames = 0
        self._fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self._total = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self._height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        log.info("Video: %s (%dx%d, %.1f fps, %d frames)", self.path.name, self._width, self._height,
                 self._fps, self._total)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def meta(self) -> dict:
        return {
            "kind": "video_file",
            "path": str(self.path),
            "frames": self._frames,
            "total_frames": self._total,
            "fps": round(self._fps, 2),
            "frame_shape": [self._height, self._width] if self._width else None,
            "looping": self.loop,
        }

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        with self._lock:
            if self.max_frames and self._frames >= self.max_frames:
                return False, None
            ok, frame = self._cap.read()
            if not ok or frame is None:
                if not self.loop:
                    return False, None
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    return False, None
            self._frames += 1
            return True, frame

    def stop(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
            self._open = False


# ---------------------------------------------------------------------------
# Webcam USB / V4L2
# ---------------------------------------------------------------------------

# La UNO Q expone /dev/video0 y /dev/video1 como decodificadores Qualcomm
# Venus (no son camaras). Hay que descartarlos antes de intentar capturar.
NON_CAMERA_PATTERNS = ("venus", "decoder", "qcom", "qualcomm", "encod", "codec")


def _sysfs_name(index: int) -> str:
    path = Path(f"/sys/class/video4linux/video{index}/name")
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def list_video_devices() -> list[dict]:
    """Lista /dev/video* con su nombre sysfs y si parece una camara real."""
    devices: list[dict] = []
    for entry in sorted(Path("/dev").glob("video*")):
        match = re.match(r"video(\\d+)$", entry.name)
        if not match:
            continue
        index = int(match.group(1))
        name = _sysfs_name(index)
        lower = name.lower()
        is_camera = bool(name) and not any(p in lower for p in NON_CAMERA_PATTERNS)
        devices.append({"index": index, "path": str(entry), "name": name, "camera": is_camera})
    return devices


class WebcamSource:
    """Camara UVC local. Descarta decodificadores (Venus) y valida el frame."""

    def __init__(
        self,
        index: int = 0,
        width: int = 0,
        height: int = 0,
        fps: float = 0.0,
        validate: bool = True,
    ) -> None:
        self.index = int(index)
        self._lock = threading.Lock()
        self._open = False
        self._frames = 0
        self._name = _sysfs_name(self.index)
        if validate:
            self._reject_non_camera()

        cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise SourceError(self._help_message(index, "no se pudo abrir el dispositivo"))
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            cap.set(cv2.CAP_PROP_FPS, float(fps))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap = cap
        self._width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self._height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self._fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if validate:
            self._validate_frames()
        self._open = True
        log.info("Webcam /dev/video%d (%s) %dx%d @ %.1f fps", self.index, self._name or "?",
                 self._width, self._height, self._fps)

    # -- helpers -------------------------------------------------------
    def _reject_non_camera(self) -> None:
        lower = (self._name or "").lower()
        if any(p in lower for p in NON_CAMERA_PATTERNS):
            raise SourceError(self._help_message(self.index, f"'{self._name}' es un decodificador, no una camara"))

    def _help_message(self, index: int, reason: str) -> str:
        devices = list_video_devices()
        real = [d for d in devices if d["camera"]]
        detail = ", ".join(f"/dev/video{d['index']} ({d['name']})" for d in devices) or "ninguno"
        hint = ""
        if real:
            hint = " Camaras disponibles: " + ", ".join(f"/dev/video{d['index']}" for d in real) + "."
        else:
            hint = " No se detecto ninguna camara UVC real: conectala o usa una fuente archivo/demo."
        return f"/dev/video{index}: {reason}. Dispositivos: {detail}.{hint}"

    def _validate_frames(self) -> None:
        """Rechaza dispositivos que abren pero solo entregan frames vacios."""
        for _ in range(8):
            ok, frame = self._cap.read()
            if ok and frame is not None and frame.size:
                if float(frame.std()) > 0.5:
                    return
            time.sleep(0.03)
        self._cap.release()
        raise SourceError(self._help_message(self.index, "el dispositivo no entrega imagen (posible decodificador)"))

    # -- protocolo -----------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def meta(self) -> dict:
        return {
            "kind": "webcam",
            "index": self.index,
            "name": self._name,
            "frames": self._frames,
            "fps": round(self._fps, 2),
            "frame_shape": [self._height, self._width] if self._width else None,
        }

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        with self._lock:
            ok, frame = self._cap.read()
        if ok and frame is not None:
            self._frames += 1
            return True, frame
        return False, None

    def stop(self) -> None:
        with self._lock:
            self._open = False
            try:
                self._cap.release()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Carpeta de imagenes
# ---------------------------------------------------------------------------

class ImageFolderSource:
    """Secuencia de imagenes ordenadas alfabeticamente (con loop opcional)."""

    def __init__(self, path: str | Path, loop: bool = True) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise SourceError(f"carpeta no encontrada: {self.path}")
        self.loop = bool(loop)
        self.files = sorted(
            p for p in self.path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
        if not self.files:
            raise SourceError(f"la carpeta no tiene imagenes: {self.path}")
        self._lock = threading.Lock()
        self._i = 0
        self._open = True
        self._frames = 0
        first = cv2.imread(str(self.files[0]), cv2.IMREAD_COLOR)
        self._shape = first.shape[:2] if first is not None else None

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def meta(self) -> dict:
        return {
            "kind": "image_folder",
            "path": str(self.path),
            "frames": self._frames,
            "total_frames": len(self.files),
            "frame_shape": list(self._shape) if self._shape else None,
        }

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        with self._lock:
            if self._i >= len(self.files):
                if not self.loop:
                    return False, None
                self._i = 0
            path = self.files[self._i]
            self._i += 1
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if frame is None:
            return False, None
        self._frames += 1
        return True, frame

    def stop(self) -> None:
        self._open = False


# ---------------------------------------------------------------------------
# Fuente sintetica (demo / tests / benchmark)
# ---------------------------------------------------------------------------

def make_ground_truth(height: int, width: int, seed: int = 1, band_limit_sigma: float = 0.45) -> np.ndarray:
    """Escena sintetica *band-limitada* con detalle fino realista.

    Importante: una camara real nunca entrega contenido por encima del Nyquist
    optico. Si el ground truth tuviera ruido blanco pixel a pixel o gratings
    por encima de ese limite, ninguna fusion podria reconstruirlo (esa energia
    se pierde por aliasing) y el benchmark seria enganoso. Por eso la escena se
    genera con bordes duros, texto y gratings de hasta ~0.4 ciclos por pixel de
    la fuente, y se aplica un desenfoque optico leve al final.
    """
    rng = np.random.default_rng(seed)
    h, w = height, width

    # Fondo con degradado suave.
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, w, dtype=np.float32)[None, :]
    img = np.stack([
        208 - 45 * yy + 0 * xx,
        214 - 32 * xx + 0 * yy,
        226 - 22 * (xx * yy),
    ], axis=2).astype(np.uint8)

    # Gratings: 0.08..0.20 ciclos/pixel GT (0.16..0.40 tras el downsample 2x).
    freqs = (0.08, 0.115, 0.15, 0.185)
    for i, freq in enumerate(freqs):
        x0 = int(w * (0.03 + 0.235 * i))
        y0 = int(h * 0.05)
        gx = int(w * 0.20)
        gy = int(h * 0.18)
        xs = np.arange(gx, dtype=np.float32)
        grating = (0.5 + 0.5 * np.sin(2 * np.pi * freq * xs)) * 255
        patch = np.repeat(grating[None, :], gy, axis=0).astype(np.uint8)
        img[y0:y0 + gy, x0:x0 + gx] = cv2.merge([patch, patch, patch])

    # Discos y anillos con borde duro.
    for _ in range(16):
        cx = int(rng.uniform(0.05, 0.95) * w)
        cy = int(rng.uniform(0.30, 0.95) * h)
        r = int(rng.uniform(0.02, 0.06) * min(h, w))
        color = tuple(int(v) for v in rng.integers(20, 200, 3))
        cv2.circle(img, (cx, cy), r, color, -1, lineType=cv2.LINE_8)
        cv2.circle(img, (cx, cy), max(2, r // 2), (250, 250, 250), 1, lineType=cv2.LINE_8)

    # Lineas finas (detalle al limite).
    for _ in range(24):
        x1 = int(rng.uniform(0, w))
        y1 = int(rng.uniform(0, h))
        x2 = int(np.clip(x1 + rng.normal(0, 0.07 * w), 0, w - 1))
        y2 = int(np.clip(y1 + rng.normal(0, 0.07 * h), 0, h - 1))
        cv2.line(img, (x1, y1), (x2, y2), tuple(int(v) for v in rng.integers(0, 120, 3)),
                 1, lineType=cv2.LINE_8)

    # Texto: excelente para evaluar legibilidad tras la fusion.
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, "superfoto", (int(w * 0.05), int(h * 0.93)), font, max(0.6, h / 900.0),
                (10, 10, 10), max(1, int(h / 700)), cv2.LINE_AA)
    cv2.putText(img, "UNO Q", (int(w * 0.55), int(h * 0.62)), font, max(0.7, h / 800.0),
                (250, 250, 250), max(1, int(h / 700)), cv2.LINE_AA)

    # Textura correlacionada (banda limitada: se genera a baja resolucion).
    small = rng.normal(128, 12, (max(8, h // 8), max(8, w // 8))).astype(np.float32)
    texture = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    img = np.clip(img.astype(np.float32) * 0.92 + (texture - 128.0)[..., None] * 0.5, 0, 255).astype(np.uint8)

    # Desenfoque optico leve: deja el contenido dentro de la banda recuperable.
    if band_limit_sigma > 0:
        img = cv2.GaussianBlur(img, (0, 0), float(band_limit_sigma))
    return img


class SyntheticSource:
    """Ground truth sintetico + frames LR con jitter subpixel, ruido y JPEG."""

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        frames: int = 0,
        seed: int = 1234,
        downsample: int = 2,
        max_shift_px: float = 2.5,
        noise_sigma: float = 2.0,
        jpeg_quality: int = 88,
        loop: bool = True,
        fps: float = 30.0,
        optical_sigma: float = 0.35,
        band_limit_sigma: float = 0.35,
    ) -> None:
        self.width = int(max(64, width))
        self.height = int(max(48, height))
        self.downsample = int(max(1, downsample))
        self.gt_shape = (self.height * self.downsample, self.width * self.downsample)
        self.max_shift_px = float(max_shift_px)
        self.noise_sigma = float(noise_sigma)
        self.jpeg_quality = int(jpeg_quality)
        self.loop = bool(loop)
        self.fps = float(fps)
        self.seed = int(seed)
        self._rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)
        self._gt = make_ground_truth(self.gt_shape[0], self.gt_shape[1], seed=seed,
                                     band_limit_sigma=band_limit_sigma)
        # La escena que "ve" el sensor incluye la PSF optica del objetivo
        # (expresada en pixeles de la fuente LR).
        self.optical_sigma = float(max(0.0, optical_sigma))
        if self.optical_sigma > 0:
            self._scene = cv2.GaussianBlur(self._gt, (0, 0), self.optical_sigma * self.downsample)
        else:
            self._scene = self._gt
        self._limit = int(frames) if frames else 0
        self._frames = 0
        self._open = True
        self._started = time.monotonic()
        self._first = True
        # Escala subpixel acumulada (para la deriva lenta del "pulso").
        self._drift = (0.0, 0.0)

    # -- protocolo -----------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def ground_truth(self) -> np.ndarray:
        return self._gt

    @property
    def meta(self) -> dict:
        return {
            "kind": "synthetic",
            "width": self.width,
            "height": self.height,
            "downsample": self.downsample,
            "gt_shape": list(self.gt_shape),
            "frames": self._frames,
            "total_frames": self._limit,
            "noise_sigma": self.noise_sigma,
            "jpeg_quality": self.jpeg_quality,
            "optical_sigma": self.optical_sigma,
        }

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        if self._limit and self._frames >= self._limit:
            if not self.loop:
                return False, None
            self._frames = 0
        frame = self.next_frame()
        self._frames += 1
        return True, frame

    def next_frame(self) -> np.ndarray:
        """Genera el proximo frame (util tambien para benchmark/tests)."""
        if self._first:
            dx, dy = 0.0, 0.0
            self._first = False
        else:
            dx = self._rng.uniform(-self.max_shift_px, self.max_shift_px)
            dy = self._rng.uniform(-self.max_shift_px, self.max_shift_px)
        # Desplazamiento en pixeles del ground truth (2x el del frame LR).
        m = np.array([[1.0, 0.0, dx * self.downsample], [0.0, 1.0, dy * self.downsample]])
        shifted = cv2.warpAffine(self._scene, m, (self.gt_shape[1], self.gt_shape[0]),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
        frame = cv2.resize(shifted, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if self.noise_sigma > 0:
            noise = self._np_rng.normal(0, self.noise_sigma, frame.shape)
            frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        if 0 < self.jpeg_quality < 100:
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if ok:
                frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return frame

    def stop(self) -> None:
        self._open = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

SOURCE_ALIASES = {
    "auto": "auto",
    "ip": "ip",
    "ipcam": "ip",
    "ip_webcam": "ip",
    "webcam_ip": "ip",
    "file": "file",
    "video": "file",
    "folder": "folder",
    "images": "folder",
    "webcam": "webcam",
    "usb": "webcam",
    "v4l2": "webcam",
    "fake": "synthetic",
    "synthetic": "synthetic",
    "demo": "synthetic",
}

SOURCE_LABELS = {
    "auto": "Automatica (busca IP Webcam en la LAN)",
    "ip": "IP Webcam (telefono por WiFi)",
    "file": "Archivo de video",
    "folder": "Carpeta de imagenes",
    "webcam": "Camara USB local",
    "synthetic": "Demo sintetica",
}


def normalize_source(kind: str | None) -> str:
    key = (kind or "auto").strip().lower()
    if key not in SOURCE_ALIASES:
        raise SourceError(
            f"fuente desconocida: '{kind}'. Validas: {', '.join(sorted(SOURCE_ALIASES))}"
        )
    return SOURCE_ALIASES[key]


def create_source(
    kind: str = "auto",
    args: dict | None = None,
    config=None,
    allow_discover: bool = True,
) -> FrameSource:
    """Construye una fuente a partir del tipo y argumentos.

    ``args`` acepta ``url``/``ip``, ``path``, ``index``, ``width``, ``height``,
    ``frames``, ``seed``, ``duration``, ``loop``, ``noise_sigma``,
    ``jpeg_quality``, ``downsample``, ``max_shift``.
    """
    args = dict(args or {})
    kind_n = normalize_source(kind)

    timeout = float(args.get("timeout", 5.0))
    backoff = getattr(config, "reconnect_backoff", (1.0, 30.0))
    frame_timeout_ms = getattr(config, "frame_timeout_ms", 5000)

    if kind_n == "ip":
        url = str(args.get("url") or args.get("ip") or "")
        if not url:
            url = getattr(config, "source_url", "") or ""
        if not url:
            raise SourceError(
                "falta la IP o URL del telefono (ej: 192.168.1.23:8080). "
                "Escribila en el campo 'IP o URL del telefono' o usa 'Buscar camara'."
            )
        return IPCameraSource(url, timeout=timeout, frame_timeout_ms=frame_timeout_ms,
                              reconnect_backoff=backoff, prefer_still=bool(args.get("prefer_still", True)))

    if kind_n == "file":
        path = args.get("path") or getattr(config, "source_path", "") or ""
        if not path:
            raise SourceError("falta la ruta del archivo de video")
        return VideoFileSource(path, loop=bool(args.get("loop", True)),
                               max_frames=args.get("max_frames"))

    if kind_n == "folder":
        path = args.get("path") or getattr(config, "source_path", "") or ""
        if not path:
            raise SourceError("falta la ruta de la carpeta de imagenes")
        return ImageFolderSource(path, loop=bool(args.get("loop", True)))

    if kind_n == "webcam":
        return WebcamSource(
            index=int(args.get("index", 0)),
            width=int(args.get("width", 0) or 0),
            height=int(args.get("height", 0) or 0),
            fps=float(args.get("fps", 0) or 0),
        )

    if kind_n == "synthetic":
        return SyntheticSource(
            width=int(args.get("width", 640)),
            height=int(args.get("height", 480)),
            frames=int(args.get("frames", 0) or 0),
            seed=int(args.get("seed", 1234)),
            downsample=int(args.get("downsample", 2) or 2),
            max_shift_px=float(args.get("max_shift", 2.5)),
            noise_sigma=float(args.get("noise_sigma", 2.0)),
            jpeg_quality=int(args.get("jpeg_quality", 88)),
            loop=bool(args.get("loop", True)),
            optical_sigma=float(args.get("optical_sigma", 0.35)),
            band_limit_sigma=float(args.get("band_limit_sigma", 0.35)),
        )

    # auto: URL configurada -> descubrimiento LAN -> error explicativo
    url = str(args.get("url") or getattr(config, "source_url", "") or "")
    if url:
        if probe_ip_camera(url, timeout=min(2.0, timeout)).get("ok"):
            return IPCameraSource(url, timeout=timeout, frame_timeout_ms=frame_timeout_ms,
                                  reconnect_backoff=backoff)
        log.warning("La URL configurada no responde como IP Webcam: %s", url)

    if allow_discover and config is not None:
        from .discover import discover_ip_webcam

        found = discover_ip_webcam(
            subnet=getattr(config, "discover_subnet", "192.168.1"),
            ports=tuple(getattr(config, "discover_ports", (8080,))),
            timeout=float(getattr(config, "discover_timeout", 0.8)),
        )
        if found:
            best = found[0]
            log.info("Camara descubierta: %s", best["url"])
            return IPCameraSource(best["url"], timeout=timeout, frame_timeout_ms=frame_timeout_ms,
                                  reconnect_backoff=backoff)

    path = getattr(config, "source_path", "") or ""
    if path and Path(path).is_file():
        return VideoFileSource(path, loop=True)

    raise SourceError(
        "No se encontro ninguna camara IP Webcam en la red y no hay archivo configurado. "
        "Conecta el telefono con IP Webcam, o elegi 'archivo', 'carpeta', 'webcam' o "
        "'demo sintetica'."
    )
