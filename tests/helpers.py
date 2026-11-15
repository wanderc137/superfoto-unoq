"""Utilidades compartidas por los tests (sin hardware, sin red externa)."""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from app.sr import psnr, ssim  # reexportadas por comodidad

__all__ = ["psnr", "ssim", "FakeMJPEGServer", "make_frame", "wait_until", "encode_jpeg"]


def make_frame(width: int = 320, height: int = 240, seed: int = 0) -> np.ndarray:
    """Frame de prueba con textura y bordes (no uniforme: sirve para nitidez)."""
    rng = np.random.default_rng(seed)
    base = np.full((height, width, 3), 120, np.uint8)
    small = rng.integers(40, 220, (max(4, height // 16), max(4, width // 16), 3), dtype=np.uint8)
    texture = cv2.resize(small, (width, height), interpolation=cv2.INTER_NEAREST)
    base = cv2.addWeighted(base, 0.4, texture, 0.6, 0)
    cv2.circle(base, (width // 2, height // 2), max(6, height // 6), (250, 250, 250), 2)
    cv2.putText(base, "UNO Q", (max(4, width // 12), int(height * 0.8)),
                cv2.FONT_HERSHEY_SIMPLEX, max(0.4, width / 900.0), (10, 10, 10), 1, cv2.LINE_AA)
    return base


def encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("no se pudo codificar JPEG")
    return buf.tobytes()


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    """Espera activa hasta que ``predicate()`` sea verdadero."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeMJPEGServer:
    """Servidor local que imita IP Webcam: /video (MJPEG), /photo.jpg y /status.json.

    ``frames_before_close`` permite simular cortes de conexion para probar la
    reconexion con backoff.
    """

    def __init__(self, width: int = 240, height: int = 180, boundary: str = "--myboundary",
                 frames_before_close: int = 0, content_length: bool = True,
                 fps: float = 40.0) -> None:
        self.boundary = boundary
        self.frames_before_close = int(frames_before_close)
        self.content_length = bool(content_length)
        self.delay = 1.0 / max(1.0, fps)
        self.frame = make_frame(width, height, seed=5)
        self.jpeg = encode_jpeg(self.frame)
        self.connections = 0
        self.frames_sent = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()

        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # silencio
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/photo.jpg"):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(server.jpeg)))
                    self.end_headers()
                    self.wfile.write(server.jpeg)
                    return
                if self.path.startswith("/status.json"):
                    body = b'{"name":"fake","previewResolution":"240x180","ipwebcam":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not self.path.startswith("/video"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type",
                                 f"multipart/x-mixed-replace; boundary={server.boundary}")
                self.end_headers()
                with server._lock:
                    server.connections += 1
                    conn_index = server.connections
                sent = 0
                try:
                    while not server._stop.is_set():
                        part = bytearray()
                        part += f"{server.wire_boundary}\r\n".encode()
                        part += b"Content-Type: image/jpeg\r\n"
                        if server.content_length:
                            part += f"Content-Length: {len(server.jpeg)}\r\n".encode()
                        part += b"\r\n"
                        part += server.jpeg
                        part += b"\r\n"
                        self.wfile.write(bytes(part))
                        self.wfile.flush()
                        sent += 1
                        with server._lock:
                            server.frames_sent += 1
                        if server.frames_before_close and sent >= server.frames_before_close:
                            return  # cierra la conexion: el cliente debe reconectar
                        time.sleep(server.delay)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # -- API -----------------------------------------------------------
    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def wire_boundary(self) -> str:
        """Delimitador tal como viaja en el stream: MIME exige ``--`` delante.

        IP Webcam anuncia ``boundary=<token>`` y en el wire escribe
        ``--<token>``; el helper debe imitar eso para que los tests cubran el
        caso real (boundary aleatorio sin ``--`` en el header).
        """
        return self.boundary if self.boundary.startswith("--") else "--" + self.boundary

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeMJPEGServer":
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
