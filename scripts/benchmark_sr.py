#!/usr/bin/env python3
"""Benchmark sintetico de superresolucion.

Genera un ground truth de alta resolucion, simula una camara (PSF optica,
jitter subpixel, ruido gaussiano y compresion JPEG), corre el pipeline completo
de superfoto (alineacion subpixel + fusion online + IBP + Richardson-Lucy) y lo
compara contra el baseline bicubico de un solo frame.

El modelo fisico es el honesto: una camara real tiene una PSF optica de ~0.5 px
de la fuente; con esa PSF el baseline pierde microdetalle y la fusion puede
recuperarlo. No se inyectan gratings por encima del Nyquist optico (seria
irrecuperable y el benchmark seria enganoso).

Uso:
    python scripts/benchmark_sr.py --quick
    python scripts/benchmark_sr.py --gt 4000 --frames 300
    python scripts/benchmark_sr.py --quick --out-dir /tmp/bench
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.align import SubpixelAligner  # noqa: E402
from app.source import SyntheticSource  # noqa: E402
from app.sr import SuperResolver, postprocess, psnr, ssim  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark de superresolucion por fusion")
    parser.add_argument("--quick", action="store_true",
                        help="version reducida (1200x900 GT, 120 frames) para CI")
    parser.add_argument("--gt", type=int, default=None, help="ancho del ground truth")
    parser.add_argument("--frames", type=int, default=None, help="cantidad de frames")
    parser.add_argument("--scale", type=float, default=2.0, help="escala de fusion")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--noise", type=float, default=3.0, help="sigma de ruido gaussiano")
    parser.add_argument("--jpeg-quality", type=int, default=88)
    parser.add_argument("--optical-sigma", type=float, default=0.5,
                        help="PSF optica en pixeles de la fuente LR")
    parser.add_argument("--psf-sigma", type=float, default=0.6,
                        help="sigma de la PSF usada por Richardson-Lucy (px de salida)")
    parser.add_argument("--rl-iterations", type=int, default=15)
    parser.add_argument("--refine-iterations", type=int, default=8)
    parser.add_argument("--out-dir", default="", help="guardar imagenes del benchmark")
    parser.add_argument("--json", default="", help="guardar resultados en JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.quick:
        gt_w = args.gt or 1200
        frames = args.frames or 120
    else:
        gt_w = args.gt or 4000
        frames = args.frames or 300
    gt_w = int(gt_w) - (int(gt_w) % 2)
    gt_h = int(round(gt_w * 0.75))
    gt_h -= gt_h % 2
    lr_w, lr_h = gt_w // 2, gt_h // 2

    print("=" * 72)
    print("superfoto-unoq · benchmark de superresolucion")
    print("=" * 72)
    print(f"ground truth : {gt_w}x{gt_h}")
    print(f"frames LR    : {lr_w}x{lr_h} x {frames}  (downsample 2x)")
    print(f"ruido        : sigma={args.noise}  JPEG q={args.jpeg_quality}  "
          f"PSF optica={args.optical_sigma} px LR")
    print(f"fusion       : escala={args.scale}  IBP={args.refine_iterations} iter  "
          f"RL sigma={args.psf_sigma} x {args.rl_iterations}")
    print("-" * 72)

    t_start = time.time()
    source = SyntheticSource(
        width=lr_w,
        height=lr_h,
        frames=frames,
        seed=args.seed,
        downsample=2,
        noise_sigma=args.noise,
        jpeg_quality=args.jpeg_quality,
        optical_sigma=args.optical_sigma,
        loop=False,
    )
    gt = source.ground_truth
    print(f"ground truth generado en {time.time() - t_start:.1f}s")
    gt = cv2.resize(gt, (gt_w, gt_h), interpolation=cv2.INTER_AREA)

    frames_list: list[np.ndarray] = []
    t0 = time.time()
    for _ in range(frames):
        ok, frame = source.read()
        if not ok or frame is None:
            break
        frames_list.append(frame)
    t_read = time.time() - t0
    n = len(frames_list)
    print(f"frames generados: {n} en {t_read:.1f}s ({n / max(t_read, 1e-6):.1f} fps)")

    # --- Baseline: bicubico de un solo frame -----------------------------
    t0 = time.time()
    baseline = cv2.resize(frames_list[0], (gt_w, gt_h), interpolation=cv2.INTER_CUBIC)
    t_bicubic = time.time() - t0
    psnr_base = psnr(gt, baseline)
    ssim_base = ssim(gt, baseline)

    # --- Pipeline: alineacion subpixel -----------------------------------
    t0 = time.time()
    aligner = SubpixelAligner(frames_list[0], scale=0.5, max_shift=32.0, min_corr=0.35,
                             use_ecc=True)
    matrices = []
    invalid = 0
    for frame in frames_list:
        res = aligner.align(frame)
        if res["valid"]:
            matrices.append(res["matrix"])
        else:
            matrices.append(None)
            invalid += 1
    t_align = time.time() - t0
    valid = n - invalid
    print(f"alineacion   : {valid}/{n} validos en {t_align:.1f}s "
          f"({1000 * t_align / max(n, 1):.1f} ms/frame)")

    # --- Fusion online ----------------------------------------------------
    t0 = time.time()
    resolver = SuperResolver((lr_h, lr_w), args.scale)
    for frame, matrix in zip(frames_list, matrices):
        if matrix is not None:
            resolver.add_frame(frame, matrix, 1.0)
    fused, coverage = resolver.result()
    t_fuse = time.time() - t0
    out_h, out_w = fused.shape[:2]
    print(f"fusion       : {out_w}x{out_h} en {t_fuse:.1f}s "
          f"({1000 * t_fuse / max(valid, 1):.1f} ms/frame) · cobertura {resolver.coverage_stats['coverage_1']:.2%}")

    # --- Refinamiento (IBP) + deconvolucion ------------------------------
    t0 = time.time()
    refined = resolver.refine(iterations=args.refine_iterations)
    t_refine = time.time() - t0
    t0 = time.time()
    final = postprocess(refined, psf_sigma=args.psf_sigma, rl_iterations=args.rl_iterations,
                        white_balance=False, denoise_chroma=True, unsharp=False)
    t_post = time.time() - t0

    psnr_fused = psnr(gt, fused)
    psnr_refined = psnr(gt, refined)
    psnr_final = psnr(gt, final)
    ssim_final = ssim(gt, final)
    gain = psnr_final - psnr_base
    total = time.time() - t_start

    print("-" * 72)
    print(f"{'etapa':<26}{'PSNR dB':>10}{'SSIM':>10}")
    print(f"{'bicubico (1 frame)':<26}{psnr_base:>10.3f}{ssim_base:>10.4f}")
    print(f"{'fusion online':<26}{psnr_fused:>10.3f}{ssim(gt, fused):>10.4f}")
    print(f"{'+ IBP':<26}{psnr_refined:>10.3f}{ssim(gt, refined):>10.4f}")
    print(f"{'+ RL deconvolucion':<26}{psnr_final:>10.3f}{ssim_final:>10.4f}")
    print("-" * 72)
    print(f"mejora sobre bicubico : {gain:+.2f} dB   SSIM {ssim_final - ssim_base:+.4f}")
    print(f"tiempos               : lectura {t_read:.1f}s · alineacion {t_align:.1f}s · "
          f"fusion {t_fuse:.1f}s · IBP {t_refine:.1f}s · RL {t_post:.1f}s")
    print(f"total                 : {total:.1f}s")
    ok = gain >= 1.5 and ssim_final > ssim_base
    print(f"criterio (+1.5 dB y mejor SSIM): {'CUMPLE' if ok else 'NO CUMPLE'}")
    print("=" * 72)

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out / "ground_truth.png"), gt)
        cv2.imwrite(str(out / "frame_00.png"), frames_list[0])
        cv2.imwrite(str(out / "bicubic.png"), baseline)
        cv2.imwrite(str(out / "fusion.png"), fused)
        cv2.imwrite(str(out / "superfoto.png"), final)
        cv2.imwrite(str(out / "coverage.png"), coverage)
        crop = _compare_crop(baseline, final)
        cv2.imwrite(str(out / "compare.png"), crop)
        print(f"imagenes guardadas en {out}")

    result = {
        "gt_shape": [gt_h, gt_w],
        "frames": n,
        "scale": args.scale,
        "noise": args.noise,
        "jpeg_quality": args.jpeg_quality,
        "optical_sigma": args.optical_sigma,
        "psf_sigma": args.psf_sigma,
        "rl_iterations": args.rl_iterations,
        "refine_iterations": args.refine_iterations,
        "psnr_bicubic": round(psnr_base, 3),
        "psnr_fusion": round(psnr_fused, 3),
        "psnr_ibp": round(psnr_refined, 3),
        "psnr_final": round(psnr_final, 3),
        "ssim_bicubic": round(ssim_base, 4),
        "ssim_final": round(ssim_final, 4),
        "gain_db": round(gain, 3),
        "seconds": round(total, 2),
        "passed": bool(ok),
    }
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"resultados JSON en {args.json}")
    return 0 if ok else 1


def _compare_crop(a: np.ndarray, b: np.ndarray, max_side: int = 640) -> np.ndarray:
    h, w = a.shape[:2]
    ch, cw = min(max_side, h), min(max_side // 2, w // 2)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    gap = np.full((ch, 4, 3), 255, np.uint8)
    return np.hstack([a[y0:y0 + ch, x0:x0 + cw], gap, b[y0:y0 + ch, x0:x0 + cw]])


if __name__ == "__main__":
    raise SystemExit(main())
