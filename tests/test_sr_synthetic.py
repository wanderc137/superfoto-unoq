"""Validacion objetiva de la superresolucion contra un ground truth sintetico.

Version reducida del benchmark (1200x900 de GT, 120 frames de 600x450) para que
la suite corra en menos de un minuto. El modelo fisico incluye PSF optica,
jitter subpixel, ruido y JPEG; el baseline es el bicubico de un solo frame, que
es el estandar de la industria para comparar.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.align import SubpixelAligner
from app.source import SyntheticSource
from app.sr import SuperResolver, postprocess, psnr, ssim

GT_W, GT_H = 1200, 900
LR_W, LR_H = 600, 450
N_FRAMES = 120


def _pipeline(noise: float = 3.0, quality: int = 88, optical_sigma: float = 0.5,
              seed: int = 1234, refine_iterations: int = 6):
    source = SyntheticSource(
        width=LR_W, height=LR_H, frames=N_FRAMES, seed=seed, downsample=2,
        noise_sigma=noise, jpeg_quality=quality, optical_sigma=optical_sigma, loop=False,
    )
    gt = source.ground_truth
    frames = [source.read()[1] for _ in range(N_FRAMES)]

    aligner = SubpixelAligner(frames[0], scale=0.5, max_shift=32.0, min_corr=0.35, use_ecc=True)
    matrices = []
    for frame in frames:
        res = aligner.align(frame)
        matrices.append(res["matrix"] if res["valid"] else None)

    resolver = SuperResolver((LR_H, LR_W), 2.0)
    for frame, matrix in zip(frames, matrices):
        if matrix is not None:
            resolver.add_frame(frame, matrix, 1.0)
    assert resolver.frames >= N_FRAMES - 2

    refined = resolver.refine(iterations=refine_iterations)
    final = postprocess(refined, psf_sigma=0.6, rl_iterations=15, white_balance=False,
                        denoise_chroma=True, unsharp=False)
    baseline = cv2.resize(frames[0], (GT_W, GT_H), interpolation=cv2.INTER_CUBIC)
    return frames, gt, resolver, refined, final, baseline


def test_sr_synthetic_gt_beats_bicubic(tmp_path):
    frames, gt, resolver, refined, final, baseline = _pipeline()

    p_base = psnr(gt, baseline)
    p_final = psnr(gt, final)
    s_base = ssim(gt, baseline)
    s_final = ssim(gt, final)
    print(f"\nbicubico {p_base:.2f} dB / SSIM {s_base:.4f}")
    print(f"superfoto {p_final:.2f} dB / SSIM {s_final:.4f}  (IBP {psnr(gt, refined):.2f} dB)")

    assert final.shape[:2] == (GT_H, GT_W)
    assert p_final - p_base >= 1.5, f"ganancia insuficiente: {p_final - p_base:.2f} dB"
    assert s_final > s_base, f"SSIM no mejoro: {s_final:.4f} vs {s_base:.4f}"
    assert resolver.coverage_stats["coverage_4"] > 0.9

    # El mapa de cobertura no debe estar vacio.
    _, coverage = resolver.result()
    assert coverage.shape == (GT_H, GT_W)
    assert coverage.max() > 0

    # Persistencia opcional para inspeccion manual durante el desarrollo.
    if tmp_path is not None:
        cv2.imwrite(str(tmp_path / "final.png"), final)


def test_sr_online_fusion_is_consistent():
    _, gt, resolver, _, _, _ = _pipeline(noise=2.0, refine_iterations=0)
    fused, coverage = resolver.result()
    # La fusion sola ya debe parecerse razonablemente al GT (no es ruido).
    assert psnr(gt, fused) > 18.0
    assert resolver.coverage_stats["coverage_1"] > 0.99


def test_sr_scale_and_memory_guards():
    # max_output_pixels reduce la escala pedida.
    resolver = SuperResolver((900, 1200), 2.0, max_output_pixels=2_000_000)
    assert resolver.output_pixels <= 2_000_000
    assert resolver.effective_scale < 2.0
    assert resolver.effective_scale >= 1.0

    # Un frame gigante con presupuesto chico no puede fusionarse sin bajar de 1x.
    with pytest.raises(ValueError):
        SuperResolver((4000, 6000), 2.0, max_accumulator_bytes=8 * 1024 * 1024)


def test_sr_rejects_empty():
    resolver = SuperResolver((10, 10), 1.5)
    with pytest.raises(ValueError):
        resolver.add_frame(np.empty((0, 0, 3), np.uint8), np.eye(2, 3))
    with pytest.raises(ValueError):
        resolver.result()


def test_sr_coverage_counter_does_not_saturate():
    """I-001: el contador de cobertura es uint16; 400 frames no saturan a 255."""
    rng = np.random.default_rng(3)
    frames = rng.integers(0, 255, (10, 8, 3), dtype=np.uint8)
    resolver = SuperResolver((30, 40), 2.0)
    for _ in range(400):
        resolver.add_frame(frames, np.eye(2, 3), 1.0)

    stats = resolver.coverage_stats
    assert resolver.frames == 400
    assert resolver.count.dtype == np.uint16
    # Antes (uint8) mean_count quedaba clavado en 255.
    assert stats["mean_count"] == pytest.approx(400.0, rel=1e-6)
    assert int(resolver.count.max()) == 400
    assert stats["coverage_16"] == 1.0

    mask = resolver.coverage_mask()
    assert mask.dtype == np.uint8
    assert mask.max() == 255  # normalizado por el pico real, no por 255


def test_sr_coverage_counter_saturates_safely_at_uint16_limit():
    """El incremento debe saturar, no desbordar (wrap) al llegar a 65535."""
    resolver = SuperResolver((10, 10), 1.0)
    resolver.count[:] = np.uint16(65535)
    resolver.frames = 1
    resolver.add_frame(np.zeros((10, 10, 3), np.uint8), np.eye(2, 3), 1.0)
    assert int(resolver.count.max()) == 65535
