"""Fuentes de frames: parser MJPEG, reconexion, archivos, carpetas y sintetica."""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.source import (
    IPCameraSource,
    ImageFolderSource,
    MJPEGStreamParser,
    SourceError,
    SyntheticSource,
    VideoFileSource,
    create_source,
    list_video_devices,
)
from tests.helpers import FakeMJPEGServer, encode_jpeg, make_frame


# ---------------------------------------------------------------- parser
def _multipart(frames: list[bytes], boundary: bytes = b"--myboundary") -> bytes:
    out = bytearray()
    for jpeg in frames:
        out += boundary + b"\r\n"
        out += b"Content-Type: image/jpeg\r\n"
        out += f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
        out += jpeg + b"\r\n"
    return bytes(out)


def test_source_mjpeg_parser_chunked():
    frames = [encode_jpeg(make_frame(64, 48, seed=i)) for i in range(5)]
    payload = _multipart(frames)
    parser = MJPEGStreamParser(b"--myboundary")
    collected: list[bytes] = []
    # Chunks de 7 bytes: corta headers, JPEG y boundaries en cualquier punto.
    for i in range(0, len(payload), 7):
        collected.extend(parser.feed(payload[i:i + 7]))
    assert len(collected) == 5
    assert all(len(c) > 100 for c in collected)
    assert parser.frames == 5


def test_source_mjpeg_parser_without_content_length():
    frames = [encode_jpeg(make_frame(48, 32, seed=i)) for i in range(3)]
    payload = bytearray()
    for jpeg in frames:
        payload += b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
    # Sin Content-Length el frame termina en el boundary siguiente: se agrega
    # uno final (es lo que hace un stream real).
    payload += b"--b\r\n"
    parser = MJPEGStreamParser(b"--b")
    out: list[bytes] = []
    for i in range(0, len(payload), 11):
        out.extend(parser.feed(bytes(payload[i:i + 11])))
    assert len(out) == 3


def test_source_mjpeg_parser_garbage_is_tolerated():
    parser = MJPEGStreamParser()
    assert parser.feed(b"HTTP/1.1 200 OK\r\n\r\nbasura sin boundary") == []
    good = _multipart([encode_jpeg(make_frame(32, 32))])
    assert len(parser.feed(good)) == 1


def test_source_parse_boundary_real_ip_webcam_header():
    """IP Webcam real anuncia el token SIN el prefijo ``--``."""
    from app.source import parse_boundary

    assert parse_boundary(
        "multipart/x-mixed-replace;boundary=Ba4oTvQMY8ew04N8dcnM"
    ) == b"Ba4oTvQMY8ew04N8dcnM"
    assert parse_boundary(
        'multipart/x-mixed-replace; boundary="tok-123"'
    ) == b"tok-123"
    assert parse_boundary("multipart/x-mixed-replace; boundary=--myboundary") == b"--myboundary"
    assert parse_boundary("image/jpeg") == b"--myboundary"


def test_source_mjpeg_parser_autodetects_boundary_from_stream():
    """Si el Content-Type no anuncia boundary, se deduce de la 1ra linea."""
    frames = [encode_jpeg(make_frame(48, 32, seed=i)) for i in range(3)]
    payload = _multipart(frames, boundary=b"--RandomTok42")
    parser = MJPEGStreamParser()  # boundary por defecto, distinto del real
    out: list[bytes] = []
    for i in range(0, len(payload), 9):
        out.extend(parser.feed(payload[i:i + 9]))
    assert len(out) == 3
    assert parser.boundary == b"--RandomTok42"


def test_source_ip_camera_random_boundary_without_dashes():
    """Regresion: IP Webcam usa un boundary aleatorio por conexion."""
    with FakeMJPEGServer(width=160, height=120, boundary="Ba4oTvQMY8ew04N8dcnM") as server:
        src = IPCameraSource(server.url, timeout=2.0, frame_timeout_ms=4000,
                             reconnect_backoff=(0.05, 0.2))
        src.start()
        ok, frame = src.read()
        src.stop()
    assert ok is True, f"sin frames (error={src.error})"
    assert frame is not None and frame.shape[:2] == (120, 160)


# ---------------------------------------------------------------- IP Webcam
def test_source_ip_camera_reads_stream():
    with FakeMJPEGServer(width=160, height=120, content_length=True) as server:
        src = IPCameraSource(server.url, timeout=2.0, frame_timeout_ms=3000,
                             reconnect_backoff=(0.05, 0.2))
        src.start()
        ok, frame = src.read()
        src.stop()
    assert ok is True
    assert frame is not None and frame.shape[:2] == (120, 160)
    assert src.meta["kind"] == "ip_webcam"


def test_source_reconnect_after_close():
    with FakeMJPEGServer(width=120, height=90, frames_before_close=5) as server:
        src = IPCameraSource(server.url, timeout=2.0, frame_timeout_ms=4000,
                             reconnect_backoff=(0.05, 0.2))
        src.start()
        received = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and received < 12:
            ok, frame = src.read()
            if ok and frame is not None:
                received += 1
        src.stop()
        connections = server.connections
    assert received >= 12, f"solo {received} frames"
    assert connections >= 2, "no hubo reconexion"
    assert src.meta["reconnects"] >= 1


# ---------------------------------------------------------------- archivos
def test_source_image_folder(tmp_path: Path):
    for i in range(3):
        cv2.imwrite(str(tmp_path / f"img_{i:02d}.png"), make_frame(80, 60, seed=i))
    src = ImageFolderSource(tmp_path)
    ok, frame = src.read()
    assert ok and frame.shape == (60, 80, 3)
    assert src.meta["total_frames"] == 3
    src.stop()
    assert src.is_open is False


def test_source_video_file(tmp_path: Path):
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (96, 64))
    assert writer.isOpened(), "el host no puede escribir mp4v"
    for i in range(10):
        writer.write(make_frame(96, 64, seed=i))
    writer.release()

    src = VideoFileSource(path, loop=False)
    ok, frame = src.read()
    assert ok and frame.shape[:2] == (64, 96)
    for _ in range(20):
        ok, frame = src.read()
    assert ok is False  # sin loop se agota
    src.stop()


def test_source_missing_file_raises(tmp_path: Path):
    with pytest.raises(SourceError):
        VideoFileSource(tmp_path / "no_existe.mp4")


# ---------------------------------------------------------------- sintetica
def test_source_synthetic_is_deterministic_and_has_gt():
    a = SyntheticSource(width=160, height=120, frames=6, seed=42)
    b = SyntheticSource(width=160, height=120, frames=6, seed=42)
    fa = [a.read()[1] for _ in range(3)]
    fb = [b.read()[1] for _ in range(3)]
    assert np.array_equal(fa[0], fb[0])
    assert np.array_equal(fa[2], fb[2])
    assert a.ground_truth.shape[:2] == (240, 320)
    # El primer frame no tiene desplazamiento: es el de referencia.
    first = fa[0].astype(np.float32)
    down = cv2.resize(a.ground_truth, (160, 120), interpolation=cv2.INTER_AREA).astype(np.float32)
    assert float(np.abs(first - down).mean()) < 6.0


def test_source_factory_and_aliases():
    src = create_source("demo", {"width": 96, "height": 72, "frames": 2})
    assert isinstance(src, SyntheticSource)
    ok, frame = src.read()
    assert ok and frame.shape[:2] == (72, 96)
    with pytest.raises(SourceError):
        create_source("fuente_inventada")
    with pytest.raises(SourceError):
        create_source("file", {"path": "/no/existe.mp4"})


def test_source_list_video_devices_shape():
    devices = list_video_devices()
    assert isinstance(devices, list)
    for dev in devices:
        assert set(dev) == {"index", "path", "name", "camera"}
