"""Normalizacion de la IP/URL de IP Webcam (permite cualquier telefono)."""

from __future__ import annotations

import pytest

from app.source import (
    SourceError,
    ip_webcam_photo_url,
    normalize_ip_webcam_url,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        # Sin esquema ni puerto: se completa todo.
        ("192.168.1.23", "http://192.168.1.23:8080/video"),
        # Sin esquema, con puerto.
        ("192.168.1.23:8080", "http://192.168.1.23:8080/video"),
        # Con esquema, sin path.
        ("http://192.168.1.23:8080", "http://192.168.1.23:8080/video"),
        # Caso existente: ya es canonica.
        ("http://192.168.1.23:8080/video", "http://192.168.1.23:8080/video"),
        # https se respeta.
        ("https://192.168.1.23:8080/video", "https://192.168.1.23:8080/video"),
        # https sin puerto -> 8080.
        ("https://cam.example", "https://cam.example:8080/video"),
        # photo.jpg se descarta.
        ("http://192.168.1.23:8080/photo.jpg", "http://192.168.1.23:8080/video"),
        # Raiz -> /video.
        ("http://192.168.1.23:8080/", "http://192.168.1.23:8080/video"),
        # Barra final -> sin barra.
        ("http://192.168.1.23:8080/video/", "http://192.168.1.23:8080/video"),
        # Espacios alrededor.
        ("  http://192.168.1.23:8080/video  ", "http://192.168.1.23:8080/video"),
        # Hostname sin puerto.
        ("localhost", "http://localhost:8080/video"),
        # Prefijo propio terminado en /video: se conserva.
        ("http://192.168.1.23:8080/cam/video", "http://192.168.1.23:8080/cam/video"),
        # Path no estandar: se canoniza a /video.
        ("http://192.168.1.23:9090/stream", "http://192.168.1.23:9090/video"),
        # Mayusculas en el esquema y el path.
        ("HTTP://192.168.1.23:8080/VIDEO", "http://192.168.1.23:8080/video"),
        # IPv6 entre corchetes.
        ("http://[::1]:8080", "http://[::1]:8080/video"),
    ],
)
def test_normalize_ip_webcam_url(value, expected):
    assert normalize_ip_webcam_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "ftp://192.168.1.23", "http://", "http://host:70000",
     "http://host:0", "http://ho st:8080", "://sin.host", None, 123],
)
def test_normalize_ip_webcam_url_rejects_invalid(value):
    with pytest.raises(SourceError) as exc:
        normalize_ip_webcam_url(value)
    assert str(exc.value)
    assert exc.value.kind == "invalid"


def test_normalize_error_messages_are_actionable():
    with pytest.raises(SourceError) as exc:
        normalize_ip_webcam_url("")
    assert "IP" in str(exc.value) and "192.168.1.23:8080" in str(exc.value)

    with pytest.raises(SourceError) as exc:
        normalize_ip_webcam_url("ftp://x")
    assert "http" in str(exc.value).lower()

    with pytest.raises(SourceError) as exc:
        normalize_ip_webcam_url("http://host:70000")
    assert "puerto" in str(exc.value).lower()


@pytest.mark.parametrize(
    "value,photo",
    [
        ("192.168.1.23", "http://192.168.1.23:8080/photo.jpg"),
        ("http://192.168.1.23:8080/photo.jpg", "http://192.168.1.23:8080/photo.jpg"),
        ("https://cam.example/video", "https://cam.example:8080/photo.jpg"),
        ("http://192.168.1.23:8080/cam/video", "http://192.168.1.23:8080/cam/photo.jpg"),
    ],
)
def test_ip_webcam_photo_url(value, photo):
    assert ip_webcam_photo_url(value) == photo


def test_ip_camera_source_uses_normalized_urls():
    from app.source import IPCameraSource

    src = IPCameraSource("192.168.1.23")  # no abre conexion en el constructor
    assert src.url == "http://192.168.1.23:8080/video"
    assert src.photo_url == "http://192.168.1.23:8080/photo.jpg"
    assert src.base_url == "http://192.168.1.23:8080"
    src.stop()

    src2 = IPCameraSource("http://192.168.1.23:8080/photo.jpg")
    assert src2.url == "http://192.168.1.23:8080/video"
    assert src2.photo_url == "http://192.168.1.23:8080/photo.jpg"
    src2.stop()
