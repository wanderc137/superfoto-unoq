"""Gate de calidad: nitidez, sobreexposicion, bajo contraste y umbral adaptativo."""

from __future__ import annotations

import cv2
import numpy as np

from app.quality import QualityGate
from tests.helpers import make_frame


def test_quality_blurry_vs_sharp():
    sharp = make_frame(320, 240, seed=1)
    blurry = cv2.GaussianBlur(sharp, (0, 0), 3.0)

    gate = QualityGate(percentile=60.0)
    # Calentamiento con frames nitidos: la historia fija el umbral.
    for _ in range(10):
        assert gate.update(sharp)["accept"] is True
    verdict = gate.update(blurry)
    assert verdict["accept"] is False
    assert verdict["reason"] == "blurry"
    assert verdict["sharpness"] < verdict["threshold"]


def test_quality_min_sharpness_explicit():
    sharp = make_frame(240, 180, seed=2)
    verdict = QualityGate(min_sharpness=1e9).update(sharp)
    assert verdict["accept"] is False
    verdict_ok = QualityGate(min_sharpness=0.0).update(sharp)
    assert verdict_ok["accept"] is True


def test_quality_overexposed():
    frame = make_frame(240, 180, seed=3)
    frame = frame.copy()
    frame[: frame.shape[0] // 4, :] = 255  # 25% saturado (> 5%)
    verdict = QualityGate().update(frame)
    assert verdict["accept"] is False
    assert verdict["reason"] == "overexposed"
    assert verdict["exposure_ok"] is False


def test_quality_low_contrast():
    flat = np.full((120, 160, 3), 128, np.uint8)
    verdict = QualityGate().update(flat)
    assert verdict["accept"] is False
    assert verdict["reason"] == "low_contrast"


def test_quality_adaptive_toggle_and_stats():
    gate = QualityGate(percentile=50.0)
    frame = make_frame(200, 260, seed=4)
    for _ in range(12):
        gate.update(frame)
    stats = gate.stats
    assert stats["seen"] == 12
    assert stats["threshold"] > 0
    gate.set_adaptive(False)
    assert gate.adaptive is False
    assert gate.threshold == 0.0  # sin min_sharpness y sin adaptativo


def test_quality_thread_safe_concurrent_updates():
    """I-005: actualizaciones concurrentes no pierden cuentas ni lanzan."""
    import threading

    gate = QualityGate(percentile=50.0)
    frames = [make_frame(160, 120, seed=i) for i in range(4)]
    errors: list[Exception] = []

    def worker(seed: int) -> None:
        try:
            for i in range(40):
                gate.update(frames[(seed + i) % len(frames)])
                _ = gate.threshold
                _ = gate.stats
        except Exception as exc:  # pragma: no cover - solo si hay carrera
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    stats = gate.stats
    assert stats["seen"] == 6 * 40
    assert stats["accepted"] + stats["rejected"] == stats["seen"]
