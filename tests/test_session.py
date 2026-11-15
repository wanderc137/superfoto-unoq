"""Politica de aviso de escena en movimiento (I-010) y saneamiento de rutas."""

from __future__ import annotations

import cv2
import numpy as np

from app.session import (
    COMPARE_CROP_H,
    COMPARE_CROP_W,
    COMPARE_GAP,
    COMPARE_ZOOM,
    SCENE_MOTION_ADVICE,
    SOFT_SOURCE_SHARPNESS,
    _detail_crop_origin,
    _make_compare,
    _sanitize_source_meta,
    _soft_source_advice,
    _structural_sharpness,
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


def _detail_scene(w=1200, h=800):
    """Fondo liso con un parche de textura fina en (x=900, y=120)."""
    img = np.full((h, w, 3), 120, np.uint8)
    yy, xx = np.mgrid[120:320, 900:1100]
    checker = ((xx // 3 + yy // 3) % 2 * 90 + 80).astype(np.uint8)
    img[120:320, 900:1100] = cv2.cvtColor(checker, cv2.COLOR_GRAY2BGR)
    return img


def test_detail_crop_origin_picks_textured_region_not_center():
    """El comparativo no debe caer en el centro vacio de la foto."""
    img = _detail_scene()
    x0, y0 = _detail_crop_origin(img, 240, 160)
    # La ventana elegida tiene que solaparse con el parche texturado.
    assert x0 + 240 > 900 and x0 < 1100
    assert y0 + 160 > 120 and y0 < 320


def test_make_compare_shows_detail_crop_zoomed():
    reference = _detail_scene()
    # Superfoto simulada: la misma escena pero con bordes mas marcados.
    result = np.clip(reference.astype(np.float32) * 1.1 - 12, 0, 255).astype(np.uint8)
    compare = _make_compare(reference, result, frames=12)
    zoom = max(1, COMPARE_ZOOM)
    assert compare.shape[0] > COMPARE_CROP_H * zoom
    assert compare.shape[1] == COMPARE_CROP_W * zoom * 2 + COMPARE_GAP
    # El panel derecho (superfoto) tiene mas detalle que el bicubico.
    panel = compare[compare.shape[0] - COMPARE_CROP_H * zoom:, COMPARE_CROP_W * zoom + COMPARE_GAP:]
    assert panel.shape[:2] == (COMPARE_CROP_H * zoom, COMPARE_CROP_W * zoom)
    assert panel.std() > 0


def test_make_compare_accepts_small_images_without_crashing():
    reference = np.full((12, 12, 3), 90, np.uint8)
    compare = _make_compare(reference, reference.copy(), frames=3)
    assert compare.size == 0  # demasiado chica: se omite el comparativo


def test_soft_source_advice_mentions_defocus_and_threshold():
    advice = _soft_source_advice(4.0)
    assert "desenfocada" in advice
    assert f"{SOFT_SOURCE_SHARPNESS:.0f}" in advice
    assert "Enfoca" in advice


def test_structural_sharpness_rewards_real_detail_not_noise():
    sharp = _detail_scene()
    blurred = cv2.GaussianBlur(sharp, (0, 0), 3.0)
    assert _structural_sharpness(sharp) > _structural_sharpness(blurred)

    # Ruido de pixel puro sobre fondo liso: el percentil alto no lo alcanza.
    rng = np.random.default_rng(7)
    flat = np.full((240, 320, 3), 128, np.uint8)
    noisy = np.clip(flat.astype(np.float32) + rng.normal(0, 25, flat.shape), 0, 255).astype(np.uint8)
    assert _structural_sharpness(noisy) < 40.0
