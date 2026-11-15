"""API REST: health, start/stop con fuente sintetica, resultados e historial."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.config import Config
from app.server import create_server
from app.storage import Storage


@pytest.fixture()
def server(tmp_path: Path):
    config = Config()
    config.data_dir = tmp_path
    config.port = 0
    config.sessions_keep = 3
    config.ensure_dirs()
    httpd, state = create_server(config, storage=Storage(tmp_path, keep=3),
                                host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, state
    session = state.current()
    if session is not None and session.active:
        session.stop(wait=True, timeout=60)
    httpd.shutdown()
    httpd.server_close()


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        return json.loads(resp.read())


def post_json(base: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_state(base: str, wanted: tuple[str, ...], timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    st = get_json(base, "/api/status")
    while time.monotonic() < deadline:
        st = get_json(base, "/api/status")
        if st["status"] in wanted:
            return st
        time.sleep(0.2)
    return st


def test_api_health(server):
    base, _ = server
    data = get_json(base, "/api/health")
    assert data["ok"] is True
    assert data["version"]
    assert data["state"] == "idle"


def test_api_start_stop_exports_files(server):
    base, _ = server
    status, data = post_json(base, "/api/session/start", {
        "source": "synthetic",
        "args": {"width": 320, "height": 240, "frames": 24},
        "target_frames": 20,
        "duration_s": 60,
        "scale": 2.0,
        "refine_iterations": 4,
        "rl_iterations": 10,
        "psf_sigma": 0.6,
    })
    assert status == 202
    assert data["ok"] is True
    assert data["status"] == "capturing"
    session_id = data["session_id"]

    # Segunda sesion simultanea: 409.
    code, err = post_json(base, "/api/session/start", {"source": "synthetic"})
    assert code == 409

    final = wait_state(base, ("finished", "error"), timeout=120)
    assert final["status"] == "finished", final.get("error")
    assert final["accepted"] >= 10

    files = get_json(base, "/api/sessions")["sessions"]
    assert files and files[0]["id"] == session_id
    for key in ("png", "jpg", "thumb", "compare", "json", "coverage"):
        with urllib.request.urlopen(f"{base}/api/result/{session_id}/{key}", timeout=30) as resp:
            payload = resp.read()
        assert len(payload) > 100, key

    meta = get_json(base, f"/api/result/{session_id}/json")
    assert meta["metrics"]["frames_accepted"] >= 10
    # I-002: el session.json descargable tampoco expone rutas absolutas.
    assert str(Path(meta.get("source", {}).get("meta", {}).get("path") or "")).startswith("/") is False
    assert "/home/" not in json.dumps(meta)
    assert meta["metrics"]["output_pixels"] > 0
    # Fuente sintetica: debe reportar la ganancia contra el ground truth.
    gt = meta["metrics"].get("ground_truth")
    assert gt and "gain_db" in gt

    latest = get_json(base, "/api/session/latest")["session"]
    assert latest["id"] == session_id

    with urllib.request.urlopen(f"{base}/api/preview", timeout=30) as resp:
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert len(resp.read()) > 1000

    with urllib.request.urlopen(base + "/", timeout=30) as resp:
        html = resp.read().decode()
        assert "SuperFoto" in html


def test_api_start_with_offline_ipcam_reports_error(server):
    base, _ = server
    code, data = post_json(base, "/api/session/start", {"source": "ip"})
    assert code == 400
    assert "URL" in data["error"] or "url" in data["error"].lower()


def test_api_status_after_stop_is_finished(server):
    base, state = server
    status, data = post_json(base, "/api/session/start", {
        "source": "synthetic",
        "args": {"width": 240, "height": 180, "frames": 20},
        "target_frames": 12,
        "duration_s": 60,
        "scale": 1.5,
        "refine_iterations": 2,
        "rl_iterations": 5,
    })
    assert status == 202
    post_json(base, "/api/session/stop", {})
    st = wait_state(base, ("finished", "error"), timeout=120)
    assert st["status"] == "finished"
    assert st["progress"] > 0


def _all_keys(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for value in node:
            keys |= _all_keys(value)
    return keys


def test_api_health_and_status_hide_absolute_paths(server):
    """I-002: no se filtran rutas absolutas del host."""
    base, state = server
    data_dir = str(state.config.data_dir)
    for path in ("/api/health", "/api/status", "/api/sessions"):
        payload = get_json(base, path)
        text = json.dumps(payload)
        assert data_dir not in text, path
        assert "/home/" not in text, path
        # La clave "data_dir" (absoluta) no debe existir; "data_dir_name" si.
        assert "data_dir" not in _all_keys(payload), path
        # Ningun valor debe ser una ruta absoluta de sistema.
        values = []

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                values.append(node)

        walk(payload)
        bad = [v for v in values if re.match(r"^/(home|root|tmp|var|usr|etc)/", v)]
        assert not bad, (path, bad)

    # El endpoint de salud expone solo el nombre del directorio de datos.
    health = get_json(base, "/api/health")
    assert health["data_dir_name"] == state.config.data_dir.name


def test_query_int_parsing(server):
    """I-003: el query string se parsea de verdad, no por substring."""
    from app.server import query_int

    assert query_int("wait=1", "wait") == 1
    assert query_int("wait=0", "wait") == 0
    assert query_int("preview=wait=1", "wait") == 0
    assert query_int("other=wait=1&wait=2", "wait") == 2
    assert query_int("wait=abc", "wait") == 0
    assert query_int("", "wait") == 0
    assert query_int("?wait=1", "wait") == 0  # sin '?', parse_qs lo trata como clave


def test_api_scene_moving_reports_advice(server):
    """I-010: si la escena se mueve, el estado lo avisa y se guarda igual."""
    base, _ = server
    status, data = post_json(base, "/api/session/start", {
        "source": "synthetic",
        # max_shift 60 px > max_shift_pixels (32): todos los frames se rechazan.
        "args": {"width": 240, "height": 180, "frames": 0, "max_shift": 60.0, "noise_sigma": 1.0},
        "target_frames": 1000,
        "duration_s": 8,
        "scale": 1.5,
        "refine_iterations": 0,
        "rl_iterations": 0,
    })
    assert status == 202
    assert data["ok"] is True

    st = wait_state(base, ("finished", "error"), timeout=120)
    assert st["status"] == "finished", st.get("error")
    assert st["scene_moving"] is True
    assert st["advice"]
    assert "escena" in st["advice"].lower()
    # Contabilidad separada de motivos: calidad vs alineacion.
    assert st["align_invalid"] >= 25
    # La contabilidad separa calidad de alineacion (puede haber ambas).
    assert abs(st["quality_rejected"] + st["align_invalid"] + st["accepted"] - st["seen"]) <= 1
    assert "escena" in st["message"].lower()

    session_id = st["session_id"]
    meta = get_json(base, f"/api/result/{session_id}/json")
    assert meta["metrics"]["scene_moving"] is True
    assert meta["metrics"]["align_invalid"] >= 25
    assert meta["advice"]


def test_api_sessions_hide_absolute_paths(server):
    """La lista de sesiones solo trae id + ruta relativa (I-002)."""
    base, _ = server
    status, data = post_json(base, "/api/session/start", {
        "source": "synthetic",
        "args": {"width": 200, "height": 150},
        "target_frames": 8,
        "duration_s": 60,
        "refine_iterations": 0,
        "rl_iterations": 0,
    })
    assert status == 202
    st = wait_state(base, ("finished", "error"), timeout=120)
    assert st["status"] == "finished"
    sessions = get_json(base, "/api/sessions")["sessions"]
    entry = next(s for s in sessions if s["id"] == st["session_id"])
    assert entry["rel"] == f"sessions/{st['session_id']}"
    assert "path" not in entry


def test_api_probe_ok_with_fake_camera(server):
    """GET /api/probe valida la IP/URL contra un IP Webcam fake."""
    from tests.helpers import FakeMJPEGServer

    base, _ = server
    with FakeMJPEGServer(width=320, height=240) as cam:
        data = get_json(base, f"/api/probe?url={cam.url}")
        assert data["ok"] is True
        assert data["url"] == f"http://127.0.0.1:{cam.port}/video"
        assert data["photo_url"] == f"http://127.0.0.1:{cam.port}/photo.jpg"
        assert data["resolution"] == [240, 320]
        assert "jpeg" in data["content_type"].lower()
        # Forma corta (sin esquema) tambien funciona.
        short = get_json(base, f"/api/probe?url=127.0.0.1:{cam.port}")
        assert short["url"] == data["url"]


def test_api_probe_missing_and_invalid(server):
    base, _ = server
    code, data = post_json(base, "/api/probe", {})  # POST no permitido
    assert code in (404, 405, 400)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/api/probe", timeout=10)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert body["ok"] is False
    assert "url" in body["error"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/api/probe?url=ftp://192.168.1.23", timeout=10)
    assert exc.value.code == 400
    assert "http" in json.loads(exc.value.read())["error"].lower()


def test_api_probe_unreachable_returns_502(server):
    import socket

    base, _ = server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/api/probe?url=127.0.0.1:{port}", timeout=15)
    assert exc.value.code == 502
    body = json.loads(exc.value.read())
    assert body["ok"] is False
    assert "no se pudo conectar" in body["error"].lower()


def test_api_start_with_ip_source_uses_args_url(server):
    """POST /api/session/start con source=ip y args.url (cualquier telefono)."""
    from tests.helpers import FakeMJPEGServer

    base, _ = server
    with FakeMJPEGServer(width=200, height=150) as cam:
        status, data = post_json(base, "/api/session/start", {
            "source": "ip",
            "args": {"url": f"127.0.0.1:{cam.port}"},  # sin esquema: se normaliza
            "target_frames": 6,
            "duration_s": 30,
            "scale": 1.5,
            "refine_iterations": 0,
            "rl_iterations": 0,
        })
        assert status == 202
        assert data["ok"] is True
        st = wait_state(base, ("finished", "error"), timeout=120)
    assert st["status"] == "finished", st.get("error")
    assert st["accepted"] >= 3
    assert st["source_meta"]["kind"] == "ip_webcam"
    assert st["source_meta"]["url"] == f"http://127.0.0.1:{cam.port}"
    assert st["source_meta"]["stream_url"] == f"http://127.0.0.1:{cam.port}/video"
