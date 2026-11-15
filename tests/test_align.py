"""Registro subpixel: precision, rechazo de desplazamientos grandes y convencion."""

from __future__ import annotations

import cv2
import numpy as np

from app.align import SubpixelAligner, apply_matrix


def _texture(h: int = 300, w: int = 400, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w), dtype=np.uint8)
    return cv2.GaussianBlur(img, (5, 5), 1.2)


def _shift(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
    m = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], np.float64)
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REFLECT_101)


def test_align_known_shift():
    """Error de estimacion < 0.2 px para un desplazamiento conocido."""
    ref = _texture()
    moved = _shift(ref, 2.3, -1.7)
    aligner = SubpixelAligner(ref, scale=0.5, max_shift=64.0, min_corr=0.05, use_ecc=True)
    res = aligner.align(moved)
    assert res["valid"] is True
    dx, dy = res["shift"]
    assert abs(dx - 2.3) < 0.2, f"dx={dx}"
    assert abs(dy + 1.7) < 0.2, f"dy={dy}"

    # La matriz debe registrar el frame: warpAffine(movil, M) ~ referencia.
    warped = apply_matrix(moved, res["matrix"], ref.shape[:2])
    mad = float(np.abs(warped.astype(np.float32) - ref.astype(np.float32)).mean())
    assert mad < 2.0, f"MAD={mad}"


def test_align_phase_only_is_less_precise_but_valid():
    ref = _texture()
    moved = _shift(ref, -1.4, 0.9)
    res = SubpixelAligner(ref, scale=0.5, max_shift=64.0, min_corr=0.05, use_ecc=False).align(moved)
    assert res["valid"] and res["method"] == "phase"
    # Sin ECC la precision tipica es ~0.5 px; el default (ECC) baja a ~0.05.
    assert abs(res["shift"][0] + 1.4) < 0.7
    assert abs(res["shift"][1] - 0.9) < 0.7


def test_align_rejects_large_shift():
    ref = _texture()
    moved = _shift(ref, 60.0, 0.0)
    res = SubpixelAligner(ref, max_shift=32.0, use_ecc=True).align(moved)
    assert res["valid"] is False
    assert res["reason"] == "max_shift"


def test_align_rejects_unrelated_frame():
    ref = _texture()
    noise = _texture(seed=99)
    res = SubpixelAligner(ref, min_corr=0.35, use_ecc=False).align(noise)
    assert res["valid"] is False
    assert res["reason"] == "low_correlation"


def test_align_rejects_featureless_frame():
    """Una escena sin textura (camara tapada) no debe fusionarse."""
    ref = _texture()
    flat = np.full((300, 400), 20, np.uint8)
    res = SubpixelAligner(ref, use_ecc=True).align(flat)
    assert res["valid"] is False
    assert res["reason"] == "low_texture"


def test_align_identity_is_exact():
    ref = _texture()
    res = SubpixelAligner(ref, use_ecc=True).align(ref)
    assert res["valid"] is True
    assert abs(res["shift"][0]) < 0.05 and abs(res["shift"][1]) < 0.05
