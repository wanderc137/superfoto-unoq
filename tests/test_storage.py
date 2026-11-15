"""Persistencia y limpieza de sesiones."""

from __future__ import annotations

import time

import numpy as np

from app.storage import Storage


def _write_session(storage: Storage, idx: int):
    path = storage.new_session(suffix=f"t{idx}")
    image = np.full((60, 80, 3), 40 + idx, np.uint8)
    storage.save(
        path.name,
        image=image,
        thumb=image,
        compare=image,
        meta={"metrics": {"test": idx}, "frames_accepted": idx},
        coverage=image,
    )
    time.sleep(0.02)
    return path.name


def test_storage_cleanup_keeps_latest(tmp_path):
    storage = Storage(tmp_path, keep=3)
    ids = [_write_session(storage, i) for i in range(5)]
    removed = storage.cleanup()
    remaining = [s["id"] for s in storage.list_sessions()]
    assert len(remaining) == 3
    assert set(remaining) == set(ids[-3:])
    assert len(removed) == 2


def test_storage_file_path_allowlist(tmp_path):
    storage = Storage(tmp_path, keep=3)
    sid = _write_session(storage, 1)
    assert storage.file_path(sid, "png") is not None
    assert storage.file_path(sid, "json") is not None
    assert storage.file_path(sid, "../session.json") is None
    assert storage.file_path("../../etc", "png") is None
    assert storage.file_path(sid, "exe") is None


def test_storage_list_metadata(tmp_path):
    storage = Storage(tmp_path, keep=3)
    sid = _write_session(storage, 2)
    session = storage.get_session(sid)
    assert session is not None
    assert session["files"]["png"] == "superfoto.png"
    assert session["urls"]["png"] == f"/api/result/{sid}/png"
    assert session["frames_accepted"] == 2
    usage = storage.disk_usage()
    assert usage["total_mb"] > 0


def test_storage_describe_has_no_absolute_paths(tmp_path):
    """I-002: la lista de sesiones solo expone id y ruta relativa."""
    import json as _json

    storage = Storage(tmp_path, keep=3)
    sid = _write_session(storage, 3)
    entry = storage.get_session(sid)
    assert "path" not in entry
    assert entry["rel"] == f"sessions/{sid}"
    assert str(tmp_path) not in _json.dumps(entry)


def test_storage_atomic_save_failure_leaves_no_partial_files(tmp_path, monkeypatch):
    """I-004: si una escritura falla, no quedan archivos ni .tmp."""
    import cv2 as _cv2

    import pytest

    storage = Storage(tmp_path, keep=3)
    path = storage.new_session(suffix="fail")
    image = np.full((40, 50, 3), 90, np.uint8)

    calls = {"n": 0}
    real_imwrite = _cv2.imwrite

    def flaky_imwrite(filename, img, params=None):
        calls["n"] += 1
        if calls["n"] == 2:  # falla al escribir el JPEG
            return False
        return real_imwrite(filename, img, params or [])

    monkeypatch.setattr(_cv2, "imwrite", flaky_imwrite)
    with pytest.raises(OSError):
        storage.save(path.name, image=image, thumb=image, compare=image, meta={})
    monkeypatch.setattr(_cv2, "imwrite", real_imwrite)

    assert not path.exists(), "la sesion incompleta debe descartarse"
    assert not list((tmp_path / "sessions").glob("*/*.tmp.*"))


def test_storage_atomic_save_leaves_no_tmp_on_success(tmp_path):
    storage = Storage(tmp_path, keep=3)
    sid = _write_session(storage, 7)
    session_dir = tmp_path / "sessions" / sid
    assert (session_dir / "session.json").is_file()
    assert not list(session_dir.glob("*.tmp.*"))


def test_storage_low_space_aborts_with_clear_message(tmp_path):
    """I-009: sin espacio suficiente se aborta antes de escribir."""
    import pytest

    from app.storage import StorageError

    storage = Storage(tmp_path, keep=3, min_free_mb=100_000_000)
    sid = storage.new_session(suffix="big")
    image = np.full((40, 50, 3), 30, np.uint8)
    with pytest.raises(StorageError) as exc:
        storage.save(sid.name, image=image, thumb=image, compare=image, meta={})
    assert "espacio" in str(exc.value).lower()
    assert "min_free_mb" in str(exc.value)
    assert not (tmp_path / "sessions" / sid.name / "session.json").exists()


def test_storage_cleanup_protects_current_session(tmp_path):
    storage = Storage(tmp_path, keep=3)
    ids = [_write_session(storage, i) for i in range(4)]
    # Limpieza agresiva protegiendo la sesion indicada (aunque sea vieja).
    removed = storage.cleanup(keep=1, protect=ids[0])
    remaining = {s["id"] for s in storage.list_sessions()}
    assert ids[0] in remaining
    assert len(remaining) == 2
    assert set(removed) == set(ids[1:-1])


def test_storage_disk_usage_reports_threshold(tmp_path):
    storage = Storage(tmp_path, keep=3, min_free_mb=123)
    usage = storage.disk_usage()
    assert usage["min_free_mb"] == 123
    assert isinstance(usage["low_space"], bool)
