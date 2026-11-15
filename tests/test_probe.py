"""Probe de IP Webcam: still /photo.jpg contra un servidor fake local."""

from __future__ import annotations

import socket

import pytest

from app.source import SourceError, probe_photo
from tests.helpers import FakeMJPEGServer


def test_probe_photo_ok():
    with FakeMJPEGServer(width=320, height=240) as server:
        # Acepta la forma corta (sin esquema): la normaliza sola.
        info = probe_photo(f"127.0.0.1:{server.port}", timeout=3.0)
    assert info["ok"] is True
    assert info["url"] == f"http://127.0.0.1:{server.port}/video"
    assert info["photo_url"] == f"http://127.0.0.1:{server.port}/photo.jpg"
    assert info["resolution"] == [240, 320]  # [alto, ancho]
    assert "jpeg" in info["content_type"].lower()
    assert info["bytes"] > 1000


def test_probe_photo_invalid_url():
    with pytest.raises(SourceError) as exc:
        probe_photo("ftp://192.168.1.23", timeout=1.0)
    assert exc.value.kind == "invalid"
    assert "http" in str(exc.value).lower()


def test_probe_photo_connection_error():
    # Puerto libre que nadie escucha -> conexion rechazada inmediata.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(SourceError) as exc:
        probe_photo(f"127.0.0.1:{port}", timeout=1.0)
    assert exc.value.kind == "connection"
    assert "no se pudo conectar" in str(exc.value).lower()
    assert "/home/" not in str(exc.value)


def test_probe_photo_non_image_response():
    """Un servidor que responde HTML no debe pasar como camara."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            body = b"<html>no soy una camara</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        with pytest.raises(SourceError) as exc:
            probe_photo(f"127.0.0.1:{port}", timeout=2.0)
        assert exc.value.kind == "decode"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_probe_ip_camera_accepts_url_ending_in_video():
    """Regresion: la URL canonica termina en /video y el probe debe usar la base.

    Antes se pedia ``/video/status.json`` y ``/video/video`` (404), por lo que
    una camara valida se reportaba como caida y la sesion no arrancaba.
    """
    from app.source import probe_ip_camera

    with FakeMJPEGServer(width=320, height=240) as server:
        for url in (server.url, f"{server.url}/video", f"{server.url}/photo.jpg"):
            info = probe_ip_camera(url, timeout=3.0)
            assert info["ok"] is True, f"{url} -> {info}"
            assert info["url"] == f"{server.url}/video"
