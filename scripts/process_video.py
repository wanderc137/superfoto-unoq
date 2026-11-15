#!/usr/bin/env python3
"""Procesa un archivo de video sin UI (corre en la UNO Q o en el host).

Lee el video frame a frame (nunca lo carga entero en RAM), pasa cada frame por
el gate de calidad, alinea a nivel subpixel y fusiona online. Al terminar guarda
la sesion completa: PNG, JPEG, comparativa 1:1, thumbnail, cobertura y
session.json.

Para videos largos el proceso puede tardar decenas de minutos u horas: se
muestra progreso con ETA cada ``--progress-every`` frames.

Ejemplos:
    python scripts/process_video.py --input hoja.mp4 --max-frames 9000 --scale 1.5
    python scripts/process_video.py --input video.mp4 --out /home/arduino/superfoto-unoq/data/sessions
    python scripts/process_video.py --input video.mp4 --fast
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.config import Config  # noqa: E402
from app.session import SuperFotoSession  # noqa: E402
from app.source import VideoFileSource  # noqa: E402
from app.storage import Storage  # noqa: E402


def non_negative_int(value: str) -> int:
    """argparse: entero >= 0 (un negativo es un error, no 'ilimitado')."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"valor entero invalido: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(f"debe ser >= 0 (recibido {number})")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Superresolucion de un video (sin UI)")
    parser.add_argument("--input", "-i", required=True, help="video de entrada")
    parser.add_argument("--out", "-o", default="", help="directorio base donde guardar la sesion")
    parser.add_argument("--max-frames", type=non_negative_int, default=0,
                        help="tope de frames aceptados (0=todos; no se aceptan negativos)")
    parser.add_argument("--scale", type=float, default=1.5, help="escala de salida 1.0-2.0")
    parser.add_argument("--min-sharpness", type=float, default=None,
                        help="umbral fijo de nitidez (default: adaptativo por percentil)")
    parser.add_argument("--quality-percentile", type=float, default=60.0)
    parser.add_argument("--psf-sigma", type=float, default=0.6)
    parser.add_argument("--rl-iterations", type=non_negative_int, default=15)
    parser.add_argument("--refine-iterations", type=non_negative_int, default=8)
    parser.add_argument("--no-ecc", action="store_true", help="alineacion solo por fase (mas rapido)")
    parser.add_argument("--fast", action="store_true",
                        help="perfil rapido: sin ECC, IBP 4 iteraciones, RL 10")
    parser.add_argument("--progress-every", type=non_negative_int, default=10,
                        help="frames entre reportes")
    parser.add_argument("--keep-all", action="store_true", help="no borrar sesiones anteriores")
    parser.add_argument("--label", default="", help="etiqueta libre para session.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    video = Path(args.input).expanduser()
    if not video.is_file():
        print(f"ERROR: no existe el video {video}", file=sys.stderr)
        return 2

    config = Config.load()
    if args.out:
        base = Path(args.out).expanduser()
    else:
        base = Path(config.data_dir)
    config.data_dir = base
    config.max_elapsed_s = 0  # sin limite: videos largos pueden tardar horas
    config.sessions_keep = 999 if args.keep_all else config.sessions_keep
    if args.no_ecc or args.fast:
        config.use_ecc = False

    refine = 4 if args.fast else int(args.refine_iterations)
    rl = 10 if args.fast else int(args.rl_iterations)

    storage = Storage(base, keep=config.sessions_keep)
    source = VideoFileSource(video, loop=False)
    meta = source.meta
    total_video = int(meta.get("total_frames") or 0)
    print("=" * 70)
    print("superfoto-unoq · procesamiento de video")
    print("=" * 70)
    print(f"entrada       : {video}")
    print(f"resolucion    : {meta.get('frame_shape')}  fps: {meta.get('fps')}  "
          f"frames: {total_video or '?'}")
    print(f"escala        : {args.scale}x   IBP: {refine}   RL: {rl} (sigma {args.psf_sigma})")
    print(f"limite frames : {args.max_frames or 'todos'}")
    print(f"salida        : {base / 'sessions'}")
    print("-" * 70)

    session = SuperFotoSession(
        config=config,
        source=source,
        storage=storage,
        source_kind="file",
        source_args={"path": str(video)},
        scale=args.scale,
        min_sharpness=args.min_sharpness,
        quality_percentile=args.quality_percentile,
        psf_sigma=args.psf_sigma,
        rl_iterations=rl,
        refine_iterations=refine,
        target_frames=int(args.max_frames) or 0,
        duration_s=0,
        label=args.label or video.stem,
    )

    t0 = time.time()
    started = session.start()
    print(f"sesion        : {started['session_id']}")

    last_accepted = 0
    last_t = t0
    rate = 0.0
    motion_warned = False
    try:
        while session.active:
            time.sleep(2.0)
            st = session.status
            accepted = int(st.get("accepted") or 0)
            seen = int(st.get("seen") or 0)
            now = time.time()
            if accepted != last_accepted and now - last_t >= 1.0:
                inst = (accepted - last_accepted) / max(1e-6, now - last_t)
                rate = inst if rate == 0 else 0.8 * rate + 0.2 * inst
                last_accepted = accepted
                last_t = now
            if accepted and accepted % max(1, args.progress_every) == 0 or seen % 200 == 0:
                elapsed = now - t0
                eta = ""
                # La ETA sobre frames VISTOS es la realista para un video.
                if total_video and rate > 0:
                    remaining = max(0, total_video - seen) / max(rate, 1e-6)
                    eta = f"  ETA {remaining / 60:.1f} min"
                elif args.max_frames and rate > 0:
                    remaining = max(0, args.max_frames - accepted) / rate
                    eta = f"  ETA {remaining / 60:.1f} min"
                if st.get("scene_moving") and not motion_warned:
                    motion_warned = True
                    print(f"  AVISO: {st.get('advice')}")
                    print(f"         alineacion invalida {st.get('align_invalid')} de {seen} vistos; "
                          "el resultado usara solo el segmento estable.")
                print(f"  frames vistos={seen} aceptados={accepted} "
                      f"rechazados={st.get('rejected', 0)} "
                      f"{rate * 60:.1f} frames/min  {elapsed / 60:.1f} min{eta}")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nInterrumpido: deteniendo y guardando lo acumulado...")

    final = session.stop(wait=True, timeout=0)
    st = final if isinstance(final, dict) else session.status
    print("-" * 70)
    if st.get("status") == "error":
        print(f"ERROR: {st.get('error')}")
        return 1
    result = st.get("result") or {}
    session_id = st.get("session_id") or result.get("session_id")
    session_dir = Path(base) / "sessions" / str(session_id)
    print(f"estado        : {st.get('status')}")
    print(f"frames        : aceptados {st.get('accepted')} de {st.get('seen')} vistos")
    quality_rejected = int(st.get("quality_rejected") or 0)
    align_invalid = int(st.get("align_invalid") or 0)
    reasons = (st.get("quality") or {}).get("reasons") or {}
    detail = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())) or "sin detalle"
    print(f"rechazos      : calidad {quality_rejected} ({detail}) · "
          f"alineacion {align_invalid} (escena distinta o movimiento)")
    if st.get("scene_moving") or st.get("advice"):
        print(f"AVISO         : {st.get('advice') or 'la escena se mueve demasiado'}")
    print(f"salida        : {st.get('output_size_estimate')}  ({st.get('elapsed_seconds')} s)")
    metrics = (result.get("metrics") or {})
    if metrics.get("ground_truth"):
        gt = metrics["ground_truth"]
        print(f"vs ground truth: superfoto {gt['psnr_superfoto']} dB · "
              f"bicubico {gt['psnr_bicubic']} dB · ganancia {gt['gain_db']} dB")
    print("=" * 70)
    if session_dir.is_dir():
        print(f"sesion guardada en: {session_dir}")
        for name in ("superfoto.png", "superfoto.jpg", "thumb.jpg", "compare.jpg", "session.json"):
            path = session_dir / name
            if path.is_file():
                print(f"  {name:<16} {path.stat().st_size / 1e6:8.2f} MB")
        usage = shutil.disk_usage(str(base))
        print(f"disco libre: {usage.free / 1e6:.0f} MB")
        return 0
    print("ADVERTENCIA: no se encontro el directorio de la sesion", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
