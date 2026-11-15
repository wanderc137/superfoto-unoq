"""Politica de aviso de escena en movimiento (I-010) y saneamiento de rutas."""

from __future__ import annotations

from app.session import (
    SCENE_MOTION_ADVICE,
    _sanitize_source_meta,
    scene_motion_warning,
)


def test_scene_motion_warning_thresholds():
    # Todavia pocos frames: no se avisa.
    assert scene_motion_warning(10, 9, 1) == ""
    # Muchos frames pero casi todos utiles: no se avisa.
    assert scene_motion_warning(100, 20, 70) == ""
    # Mas de la mitad de los frames no alinean: se avisa.
    assert scene_motion_warning(100, 60, 5) == SCENE_MOTION_ADVICE
    # Justo en el limite (50%) no se avisa (el umbral es estricto).
    assert scene_motion_warning(100, 50, 50) == ""


def test_scene_motion_warning_real_case_quality_dilutes_ratio():
    """Caso real: 581/1250 invalidos (46%) pero 581/588 evaluados (99%)."""
    assert scene_motion_warning(1250, 581, 7) == SCENE_MOTION_ADVICE


def test_scene_motion_warning_no_false_positive_on_static_scene():
    # Escena estatica normal: pocos invalidos entre muchos aceptados.
    assert scene_motion_warning(400, 12, 380) == ""
    # Gate muy selectivo pero alineacion perfecta: no es movimiento.
    assert scene_motion_warning(400, 3, 20) == ""


def test_scene_motion_warning_needs_enough_evaluated_frames():
    # Con pocos frames evaluados por el alineador no se puede concluir nada.
    assert scene_motion_warning(30, 20, 2) == ""
    # Con suficientes frames evaluados y mayoria invalida, avisa.
    assert scene_motion_warning(40, 39, 1) == SCENE_MOTION_ADVICE
    assert scene_motion_warning(40, 25, 5) == SCENE_MOTION_ADVICE


def test_sanitize_source_meta_removes_absolute_paths():
    meta = {
        "kind": "video_file",
        "path": "/home/arduino/superfoto-unoq/data/videos/hoja.mp4",
        "total_frames": 1250,
    }
    clean = _sanitize_source_meta(meta)
    assert clean["path"] == "hoja.mp4"
    assert "/" not in clean["path"]
    assert clean["total_frames"] == 1250
    assert meta["path"].startswith("/")  # no muta el original
    assert _sanitize_source_meta(None) == {}
