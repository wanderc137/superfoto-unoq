#!/usr/bin/env python3
"""Captura un video largo de un objeto pequeno (hoja, circuito, moneda).

Se ejecuta en el HOST (no en la placa): toma la camara USB local o un stream de
IP Webcam y graba un archivo H.264 con ffmpeg si esta disponible. No abre
ninguna ventana.

Ejemplos:
    # webcam USB 0 en 1920x1080 durante 5 minutos
    python scripts/captura_real.py --source 0 --width 1920 --height 1080 --duration 300

    # IP Webcam del telefono
    python scripts/captura_real.py --source http://192.168.1.23:8080/video --duration 300

    # camara 2 del host (640x360@15)
    python scripts/captura_real.py --source 2 --width 640 --height 360 --fps 15 --duration 120

Al terminar imprime duracion, frames, fps efectivos y tamano del archivo, y
recuerda el comando para procesarlo en la UNO Q.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Captura de video largo para superfoto")
    parser.add_argument("--source", "-s", default="0",
                        help="indice de camara (0, 2...) o URL (http://ip:8080/video)")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=300.0, help="segundos (default 300)")
    parser.add_argument("--out", "-o", default="", help="archivo de salida .mp4")
    parser.add_argument("--codec", choices=("auto", "ffmpeg", "cv2"), default="auto")
    parser.add_argument("--crf", type=int, default=18, help="calidad x264 (menor = mejor)")
    return parser.parse_args(argv)


def open_capture(source: str, width: int, height: int, fps: float) -> tuple[cv2.VideoCapture, str]:
    if source.isdigit():
        index = int(source)
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(index)
        label = f"camara /dev/video{index}"
        # MJPG da mas fps a resolucion alta en webcams UVC.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    else:
        url = source
        if not url.startswith("http"):
            url = "http://" + url
        cap = cv2.VideoCapture(url)
        label = f"stream {url}"
    if not cap.isOpened():
        raise SystemExit(f"ERROR: no se pudo abrir {label}")
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if fps:
        cap.set(cv2.CAP_PROP_FPS, fps)
    # Descartar frames iniciales: las webcams tardan en estabilizar exposicion.
    for _ in range(8):
        cap.read()
        time.sleep(0.03)
    return cap, label


class FFmpegWriter:
    """Escribe H.264 por pipe (cv2 del host no puede codificar H.264)."""

    def __init__(self, path: Path, width: int, height: int, fps: float, crf: int = 18) -> None:
        self.path = path
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", f"{max(1.0, fps):.3f}", "-i", "-",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        self.frames = 0

    def write(self, frame: np.ndarray) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.tobytes())
        self.frames += 1

    def close(self) -> bool:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait(timeout=120)
        if code != 0:
            err = b""
            if self.proc.stderr is not None:
                err = self.proc.stderr.read()[-800:]
            print(f"ffmpeg termino con codigo {code}: {err.decode('utf-8', 'replace')}",
                  file=sys.stderr)
            return False
        return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration <= 0:
        raise SystemExit("ERROR: --duration debe ser > 0")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else PROJECT_DIR / "capturas" / f"hoja_{stamp}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    cap, label = open_capture(args.source, args.width, args.height, args.fps)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    src_fps = float(cap.get(cv2.CAP_PROP_FPS) or args.fps or 30.0)
    if width <= 0 or height <= 0:
        ok, frame = cap.read()
        if not ok:
            raise SystemExit("ERROR: la camara no entrega imagen")
        height, width = frame.shape[:2]

    use_ffmpeg = args.codec in ("auto", "ffmpeg") and shutil.which("ffmpeg") is not None
    if args.codec == "ffmpeg" and not use_ffmpeg:
        print("ADVERTENCIA: ffmpeg no esta disponible; se usara mp4v de OpenCV", file=sys.stderr)
    target_fps = min(args.fps, 60.0) if args.fps else src_fps

    print("=" * 66)
    print("superfoto-unoq · captura real")
    print("=" * 66)
    print(f"fuente   : {label}")
    print(f"resolucion: {width}x{height} @ {src_fps:.1f} fps (objetivo {target_fps:.1f})")
    print(f"duracion : {args.duration:.0f} s")
    print(f"salida   : {out}  ({'H.264 via ffmpeg' if use_ffmpeg else 'mp4v via OpenCV'})")
    print("-" * 66)
    print("Enfoca la hoja, estabiliza la camara y no muevas el objeto.")
    print("Ctrl+C detiene la captura conservando lo grabado.")
    sys.stdout.flush()

    writer: FFmpegWriter | None = None
    cv_writer: cv2.VideoWriter | None = None
    if use_ffmpeg:
        writer = FFmpegWriter(out, width, height, target_fps, args.crf)
    else:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        cv_writer = cv2.VideoWriter(str(out), fourcc, target_fps, (width, height))
        if not cv_writer.isOpened():
            raise SystemExit("ERROR: no se pudo crear el archivo de salida")

    frames = 0
    dropped = 0
    t0 = time.time()
    last_report = t0
    started = t0
    try:
        while True:
            if time.time() - t0 >= args.duration:
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                dropped += 1
                if dropped > 120:
                    print("ERROR: la camara dejo de entregar frames", file=sys.stderr)
                    break
                time.sleep(0.01)
                continue
            dropped = 0
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            if writer is not None:
                try:
                    writer.write(frame)
                except (BrokenPipeError, OSError):
                    print("ERROR: ffmpeg cerro el pipe", file=sys.stderr)
                    break
            elif cv_writer is not None:
                cv_writer.write(frame)
            frames += 1

            now = time.time()
            if now - last_report >= 5.0:
                elapsed = now - t0
                fps_eff = frames / max(1e-6, elapsed)
                size_mb = out.stat().st_size / 1e6 if out.exists() else 0.0
                eta = max(0.0, args.duration - elapsed)
                print(f"  {elapsed:6.1f}s  frames={frames:6d}  {fps_eff:5.1f} fps  "
                      f"{size_mb:7.1f} MB  ETA {eta:5.0f}s")
                sys.stdout.flush()
                last_report = now
    except KeyboardInterrupt:
        print("\nCaptura interrumpida por el usuario.")
    finally:
        cap.release()
        ok_close = True
        if writer is not None:
            ok_close = writer.close()
        if cv_writer is not None:
            cv_writer.release()

    elapsed = time.time() - started
    size = out.stat().st_size if out.exists() else 0
    print("-" * 66)
    print(f"duracion      : {elapsed:.1f} s")
    print(f"frames        : {frames}  ({frames / max(1e-6, elapsed):.2f} fps efectivos)")
    print(f"archivo       : {out}  ({size / 1e6:.2f} MB)")
    if frames == 0 or size == 0:
        print("ERROR: no se grabo ningun frame", file=sys.stderr)
        return 1
    if not ok_close:
        print("ADVERTENCIA: ffmpeg reporto un error al cerrar el archivo", file=sys.stderr)

    print()
    print("Siguiente paso: copiar y procesar en la UNO Q con")
    print(f"  scripts/prueba_real_unoq.sh \"{out}\"")
    print("o manualmente:")
    print(f"  scp -P 2222 \"{out}\" arduino@192.168.1.5:/home/arduino/superfoto-unoq/data/videos/")
    print("  ssh -p 2222 arduino@192.168.1.5 'cd superfoto-unoq && "
          f"python3 scripts/process_video.py --input data/videos/{out.name} --scale 1.5'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
