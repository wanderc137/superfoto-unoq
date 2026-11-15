"""Orquestacion del pipeline de captura + fusion + exportacion.

Todo el procesamiento pesado ocurre en un unico hilo de trabajo:

    leer frame -> gate de calidad -> alinear -> acumular (online) -> ...

Al terminar (manual, por frames, por duracion o por fin de la fuente) el mismo
hilo ejecuta el refinamiento (IBP + Richardson-Lucy) y guarda la sesion. Asi no
se guardan miles de frames ni se compite por CPU entre hilos.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .align import SubpixelAligner
from .config import Config
from .quality import QualityGate, laplacian_variance
from .source import FrameSource, SourceError, create_source
from .sr import SuperResolver, psnr, ssim
from .storage import Storage

log = logging.getLogger("superfoto.session")

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_CAPTURING = "capturing"
STATE_PROCESSING = "processing"
STATE_FINISHED = "finished"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

# Umbrales del aviso de escena en movimiento (I-010).
SCENE_MOTION_MIN_FRAMES = 30
SCENE_MOTION_RATIO = 0.5
SCENE_MOTION_ADVICE = (
    "La escena se mueve demasiado: fija la camara y el objeto, "
    "y volve a capturar el segmento estable"
)

# Nitidez del frame de referencia por debajo de la cual la fuente esta
# desenfocada. Es la varianza del Laplaciano a resolucion de captura: una foto
# enfocada de 1080p supera ampliamente 100; una fuera de foco queda en 5-30.
# La superresolucion no inventa detalle que la optica no capturo, asi que el
# aviso es honesto y evita que el usuario crea que el algoritmo no funciona.
SOFT_SOURCE_SHARPNESS = 45.0

# Recorte del comparativo: 480x360 px de la superfoto, ampliados 2x para que
# las diferencias de nitidez se distingan en la pantalla de un telefono.
COMPARE_CROP_W = 480
COMPARE_CROP_H = 360
COMPARE_ZOOM = 2
COMPARE_GAP = 8


def _soft_source_advice(sharpness: float) -> str:
    return (
        f"La captura salio desenfocada (nitidez {sharpness:.0f} de "
        f"{SOFT_SOURCE_SHARPNESS:.0f}): la superresolucion no recupera detalle "
        "que la optica no capturo. Enfoca la camara y repeti"
    )



def _sanitize_source_meta(meta: dict | None) -> dict:
    """Quita rutas absolutas del meta de la fuente (solo nombre de archivo)."""
    out = dict(meta or {})
    path = out.get("path")
    if isinstance(path, str) and path:
        out["path"] = Path(path).name
    return out


def scene_motion_warning(seen: int, align_invalid: int, accepted: int) -> str:
    """Devuelve el aviso si la mayoria de los frames no se pudieron alinear.

    Dos criterios (basta uno):

    * ``align_invalid / seen > 0.5`` (criterio principal);
    * ``align_invalid / (accepted + align_invalid) > 0.5``: cuando el gate de
      calidad rechaza muchos frames, el cociente sobre ``seen`` se diluye y no
      reflejaria que *casi todo lo que llego al alineador era otro campo*. Es
      el caso real de un video donde la escena cambia de campo varias veces
      (581 invalidos de 588 evaluados = 99%, pero solo 46% de los vistos).
    """
    if seen < SCENE_MOTION_MIN_FRAMES:
        return ""
    usable = accepted + align_invalid
    if usable < SCENE_MOTION_MIN_FRAMES:
        return ""
    ratio_seen = align_invalid / float(max(1, seen))
    ratio_evaluated = align_invalid / float(max(1, usable))
    if ratio_seen > SCENE_MOTION_RATIO or ratio_evaluated > SCENE_MOTION_RATIO:
        return SCENE_MOTION_ADVICE
    return ""

FINAL_STATES = (STATE_FINISHED, STATE_ERROR, STATE_STOPPED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_thumb(image: np.ndarray, max_side: int = 320) -> np.ndarray:
    h, w = image.shape[:2]
    if max(h, w) <= max_side:
        return image.copy()
    scale = max_side / float(max(h, w))
    return cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=cv2.INTER_AREA)


def _structural_sharpness(panel: np.ndarray) -> float:
    """Potencia de borde en la cola alta (percentil 99 del gradiente).

    La varianza del Laplaciano se infla con el grano del postproceso: en la
    sesion real daba 9x "mas nitidez" donde no habia ninguna mejora visible.
    El percentil 99 mide solo los bordes fuertes, que el ruido uniforme no
    alcanza, y sigue siendo sensible a la ganancia real de detalle.
    """
    gray = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.5)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.percentile(cv2.magnitude(gx, gy), 99))


def _detail_crop_origin(image: np.ndarray, cw: int, ch: int) -> tuple[int, int]:
    """Esquina (x, y) de la ventana cw x ch con mas detalle de la imagen.

    El centro de una foto suele ser una zona lisa (fondo, cielo, aire): un
    recorte ahi no muestra ninguna diferencia. Se elige la ventana con mayor
    energia de gradiente (Sobel) con una imagen integral, costo O(pixeles).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Un suavizado leve evita que el ruido de pixel gane la seleccion.
    gray = cv2.GaussianBlur(gray, (0, 0), 1.2)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    energy = gx * gx + gy * gy
    integ = cv2.integral(energy, sdepth=cv2.CV_64F)
    sums = integ[ch:, cw:] - integ[:-ch, cw:] - integ[ch:, :-cw] + integ[:-ch, :-cw]
    _, _, _, loc = cv2.minMaxLoc(sums)
    return int(loc[0]), int(loc[1])


def _make_compare(reference: np.ndarray, result: np.ndarray, frames: int = 0) -> np.ndarray:
    """Recorte 1:1 ampliado 2x: bicubico del frame de referencia vs superfoto.

    Se recorta la zona con mas detalle (no el centro) y se rotula la nitidez
    medida de cada panel: el comparativo tiene que mostrar *donde* y *cuanto*
    se gano, no solo dos imagenes parecidas de una zona vacia.
    """
    if result is None or result.size == 0:
        return np.empty((0, 0, 3), np.uint8)
    rh, rw = result.shape[:2]
    if rh < 16 or rw < 16 or reference is None or reference.size <= 4:
        return np.empty((0, 0, 3), np.uint8)

    cw = min(COMPARE_CROP_W, rw)
    ch = min(COMPARE_CROP_H, rh)
    x0, y0 = _detail_crop_origin(result, cw, ch)
    crop = result[y0:y0 + ch, x0:x0 + cw]

    up = cv2.resize(reference, (rw, rh), interpolation=cv2.INTER_CUBIC)[y0:y0 + ch, x0:x0 + cw]
    if up.shape[:2] != crop.shape[:2]:
        up = cv2.resize(up, (cw, ch), interpolation=cv2.INTER_CUBIC)

    # Detalle estructural a 1:1 (la metrica no debe depender del zoom de
    # presentacion, ni contar el grano del postproceso).
    sharp_base = _structural_sharpness(up)
    sharp_res = _structural_sharpness(crop)
    gain = (sharp_res / sharp_base - 1.0) * 100.0 if sharp_base > 1e-6 else 0.0

    zoom = max(1, int(COMPARE_ZOOM))
    if zoom > 1:
        size = (cw * zoom, ch * zoom)
        up = cv2.resize(up, size, interpolation=cv2.INTER_NEAREST)
        crop = cv2.resize(crop, size, interpolation=cv2.INTER_NEAREST)

    ph, pw = crop.shape[:2]
    label_h = max(24, ch // 14)
    canvas = np.full((label_h + ph, pw * 2 + COMPARE_GAP, 3), 30, np.uint8)
    canvas[label_h:, :pw] = up
    canvas[label_h:, pw:pw + COMPARE_GAP] = 255
    canvas[label_h:, pw + COMPARE_GAP:] = crop

    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = max(0.45, label_h / 34.0)
    thickness = 1
    left = "BICUBICO 1 frame"
    right = f"SUPERFOTO {frames} frames | detalle {gain:+.0f}%" if frames else \
        f"SUPERFOTO fusion | detalle {gain:+.0f}%"
    # Sin acentos: cv2.putText no maneja UTF-8 (leccion P-09).
    cv2.putText(canvas, left, (8, int(label_h * 0.72)), font, fs, (255, 255, 255), thickness,
                cv2.LINE_AA)
    cv2.putText(canvas, right, (pw + COMPARE_GAP + 8, int(label_h * 0.72)), font, fs,
                (255, 255, 255), thickness, cv2.LINE_AA)
    return canvas


class SuperFotoSession:
    """Sesion de captura y fusion superresolucion."""

    def __init__(
        self,
        config: Config | None = None,
        source: FrameSource | None = None,
        storage: Storage | None = None,
        source_kind: str = "auto",
        source_args: dict | None = None,
        scale: float | None = None,
        quality_percentile: float | None = None,
        min_sharpness: float | None = None,
        psf_sigma: float | None = None,
        rl_iterations: int | None = None,
        refine_iterations: int | None = None,
        target_frames: int | None = None,
        duration_s: float | None = None,
        label: str = "",
    ) -> None:
        self.config = config or Config.load()
        self.storage = storage or Storage(
            self.config.data_dir,
            keep=self.config.sessions_keep,
            min_free_mb=self.config.min_free_mb,
        )
        self._injected_source = source
        self.source_kind = source_kind or self.config.source
        self.source_args = dict(source_args or {})
        self.label = label

        self.scale = float(min(2.0, max(1.0, scale if scale is not None else self.config.default_scale)))
        self.quality_percentile = float(
            quality_percentile if quality_percentile is not None else self.config.quality_percentile
        )
        self.min_sharpness = min_sharpness if min_sharpness is not None else self.config.min_sharpness
        self.psf_sigma = float(psf_sigma if psf_sigma is not None else self.config.psf_sigma)
        self.rl_iterations = int(rl_iterations if rl_iterations is not None else self.config.rl_iterations)
        self.refine_iterations = int(
            refine_iterations if refine_iterations is not None else self.config.refine_iterations
        )
        self.target_frames = int(target_frames if target_frames is not None else self.config.target_frames)
        self.duration_s = float(duration_s if duration_s is not None else self.config.duration_s)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: FrameSource | None = None
        self._session_dir = None
        self._gate = QualityGate(
            percentile=self.quality_percentile,
            min_sharpness=self.min_sharpness,
            max_clipped_ratio=self.config.max_clipped_ratio,
            min_contrast_std=self.config.min_contrast_std,
        )
        self._resolver: SuperResolver | None = None
        self._aligner: SubpixelAligner | None = None
        self._reference: np.ndarray | None = None
        self._preview_jpeg: bytes | None = None
        self._preview_stamp = 0.0
        self._last_quality: dict = {}

        self.state = STATE_IDLE
        self.error = ""
        self.message = ""
        self.session_id = ""
        self.seen = 0
        self.accepted = 0
        self.rejected = 0
        self.align_invalid = 0
        self.quality_rejected = 0
        self.scene_moving = False
        self.advice = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.elapsed = 0.0
        self.result_meta: dict = {}
        self.source_meta: dict = {}
        self.output_shape: tuple[int, int] | None = None
        self.output_pixels = 0
        self.effective_scale = self.scale
        self.shifts: list[float] = []

    # ------------------------------------------------------------------
    # Propiedades de estado
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return self.state in (STATE_STARTING, STATE_CAPTURING, STATE_PROCESSING)

    @property
    def source(self) -> FrameSource | None:
        return self._source

    @property
    def preview_jpeg(self) -> bytes | None:
        with self._lock:
            return self._preview_jpeg

    @property
    def elapsed_seconds(self) -> float:
        with self._lock:
            if self.started_at and self.state in (STATE_CAPTURING, STATE_PROCESSING):
                return time.monotonic() - self.started_at
            return self.elapsed

    @property
    def progress(self) -> float:
        """Progreso 0..1 estimado (frames objetivo o tiempo)."""
        with self._lock:
            target = max(1, self.target_frames)
            by_frames = self.accepted / target if self.target_frames > 0 else 0.0
            by_time = (self.elapsed_seconds / self.duration_s) if self.duration_s > 0 else 0.0
            return float(min(1.0, max(by_frames, by_time)))

    @property
    def status(self) -> dict:
        """Snapshot JSON-serializable del estado de la sesion."""
        with self._lock:
            resolver = self._resolver
            cov = resolver.coverage_stats if resolver is not None and resolver.frames else {}
            store = {
                "frames": resolver.frames if resolver else 0,
                "stored_frames": resolver.stored_frames if resolver else 0,
                "store_mb": round((resolver.stored_bytes if resolver else 0) / 1e6, 1),
            }
            src_meta = _sanitize_source_meta(self.source_meta)
            if self._source is not None:
                try:
                    live = _sanitize_source_meta(self._source.meta)
                    src_meta.update({k: v for k, v in live.items() if v is not None})
                except Exception:  # pragma: no cover
                    pass
            out_shape = list(self.output_shape) if self.output_shape else None
            if out_shape is None and resolver is not None:
                out_shape = [resolver.out_shape[0], resolver.out_shape[1]]
            payload: dict[str, Any] = {
                "status": self.state,
                "session_id": self.session_id,
                "source": self.source_kind,
                "source_meta": src_meta,
                "seen": self.seen,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "align_invalid": self.align_invalid,
                "quality_rejected": self.quality_rejected,
                "scene_moving": self.scene_moving,
                "advice": self.advice,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "progress": round(self.progress, 4),
                "scale": round(self.effective_scale, 3),
                "requested_scale": round(self.scale, 3),
                "output_size_estimate": f"{out_shape[1]}x{out_shape[0]}" if out_shape else None,
                "output_pixels": self.output_pixels,
                "quality": dict(self._gate.stats),
                "last_quality": dict(self._last_quality),
                "coverage": cov,
                "resolver": store,
                "psf_sigma": self.psf_sigma,
                "rl_iterations": self.rl_iterations,
                "refine_iterations": self.refine_iterations,
                "target_frames": self.target_frames,
                "duration_s": self.duration_s,
                "has_preview": self._preview_jpeg is not None,
                "error": self.error,
                "message": self.message,
                "result": dict(self.result_meta) if self.result_meta else {},
                "label": self.label,
            }
            if self.shifts:
                payload["shift_mean_px"] = round(float(np.mean(self.shifts)), 3)
                payload["shift_max_px"] = round(float(np.max(self.shifts)), 3)
            return payload

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def start(self) -> dict:
        """Inicia la captura. Lanza SourceError si la fuente no se puede abrir."""
        with self._lock:
            if self.active:
                raise RuntimeError("ya hay una sesion activa")
            self.state = STATE_STARTING
            self.error = ""
            self.message = "Iniciando fuente..."

        if self._injected_source is not None:
            source = self._injected_source
        else:
            source = create_source(self.source_kind, self.source_args, self.config)

        # IPCameraSource (y cualquier fuente con hilo lector) necesita start().
        # Sin esto la ruta IP Webcam nunca entrega frames.
        starter = getattr(source, "start", None)
        if callable(starter):
            try:
                starter()
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"no se pudo iniciar la fuente: {exc}") from exc

        session_dir = self.storage.new_session(suffix=self.source_kind)
        with self._lock:
            self._source = source
            self._session_dir = session_dir
            self.session_id = session_dir.name
            self.source_meta = _sanitize_source_meta(getattr(source, "meta", {}) or {})
            self.seen = self.accepted = self.rejected = self.align_invalid = 0
            self.quality_rejected = 0
            self.scene_moving = False
            self.advice = ""
            self._gate.reset()
            self._resolver = None
            self._aligner = None
            self._reference = None
            self._preview_jpeg = None
            self.result_meta = {}
            self.shifts = []
            self.output_shape = None
            self.output_pixels = 0
            self.effective_scale = self.scale
            self.started_at = time.monotonic()
            self.finished_at = 0.0
            self.elapsed = 0.0
            self.state = STATE_CAPTURING
            self.message = "Capturando"

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="superfoto-worker", daemon=True)
        self._thread.start()
        log.info("Sesion %s iniciada (fuente=%s, escala=%.2f)", self.session_id, self.source_kind, self.scale)
        return {"ok": True, "session_id": self.session_id, "status": self.state, "scale": self.scale}

    def request_stop(self) -> dict:
        """Pide detener sin bloquear (la UI hace polling de /api/status)."""
        with self._lock:
            if not self.active:
                return {"ok": False, "status": self.state, "error": self.error}
            self._stop_event.set()
            if self.state == STATE_CAPTURING:
                self.message = "Deteniendo captura..."
        return {"ok": True, "session_id": self.session_id, "status": self.state}

    def stop(self, wait: bool = True, timeout: float = 900.0) -> dict:
        """Detiene y, si ``wait``, espera a que termine el postproceso."""
        self.request_stop()
        thread = self._thread
        if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return self.status

    # ------------------------------------------------------------------
    # Hilo de trabajo
    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._capture_loop()
            if self._resolver is None or self._resolver.frames == 0:
                self._finish_error("no se acepto ningun frame; revisa enfoque/iluminacion o la fuente")
                return
            self._process_and_save()
        except SourceError as exc:
            self._finish_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - una sesion no debe tumbar el servicio
            log.exception("Error en la sesion")
            self._finish_error(f"{type(exc).__name__}: {exc}")
        finally:
            source = self._source
            if source is not None:
                try:
                    source.stop()
                except Exception:  # pragma: no cover
                    pass

    def _capture_loop(self) -> None:
        source = self._source
        assert source is not None
        started = time.monotonic()
        last_preview = 0.0
        failures = 0
        max_elapsed = self.config.max_elapsed_s

        while not self._stop_event.is_set():
            if self.target_frames > 0 and self.accepted >= self.target_frames:
                self.message = "Frames objetivo alcanzados"
                break
            if self.duration_s > 0 and (time.monotonic() - started) >= self.duration_s:
                self.message = "Duracion alcanzada"
                break
            if max_elapsed > 0 and (time.monotonic() - started) >= max_elapsed:
                self.message = "Limite de tiempo de captura alcanzado"
                break

            ok, frame = source.read()
            if not ok or frame is None:
                failures += 1
                if not getattr(source, "is_open", True):
                    break
                if failures >= 40:
                    self.message = "La fuente dejo de entregar frames"
                    break
                time.sleep(0.02)
                continue
            failures = 0

            with self._lock:
                self.seen += 1
            verdict = self._gate.update(frame)
            with self._lock:
                self._last_quality = verdict
            if not verdict["accept"]:
                with self._lock:
                    self.rejected += 1
                    self.quality_rejected += 1
                self._check_scene_motion()
                continue

            if self._resolver is None:
                self._init_pipeline(frame)
                self._accumulate(frame, None, verdict)
            else:
                align = self._aligner.align(frame) if self._aligner else None
                if align is None or not align["valid"]:
                    with self._lock:
                        self.align_invalid += 1
                        self.rejected += 1
                        self._last_quality = dict(verdict, reason=(align or {}).get("reason", "align"))
                    self._check_scene_motion()
                    continue
                self._accumulate(frame, align, verdict)

            now = time.monotonic()
            if now - last_preview >= self.config.preview_interval_s:
                last_preview = now
                self._update_preview(frame)
            with self._lock:
                if self._resolver is not None:
                    self.output_shape = self._resolver.out_shape
                    self.output_pixels = self._resolver.output_pixels
                    self.effective_scale = self._resolver.effective_scale
            if self.accepted % 25 == 0:
                log.info("frames: vistos=%d aceptados=%d rechazados=%d", self.seen, self.accepted,
                         self.rejected)

        with self._lock:
            self.elapsed = time.monotonic() - started

    def _check_scene_motion(self) -> None:
        """Avisa cuando la escena cambia de campo y no se puede fusionar (I-010)."""
        with self._lock:
            if self.scene_moving:
                return
            advice = scene_motion_warning(self.seen, self.align_invalid, self.accepted)
            if not advice:
                return
            self.scene_moving = True
            self.advice = advice
            self.message = advice
        log.warning("Sesion %s: %s (alineacion invalida %d/%d)", self.session_id, advice,
                    self.align_invalid, self.seen)

    def _init_pipeline(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        try:
            resolver = SuperResolver(
                (h, w),
                self.scale,
                max_output_pixels=self.config.max_output_pixels,
                max_accumulator_bytes=self.config.max_accumulator_bytes,
            )
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        aligner = SubpixelAligner(
            frame,
            scale=self.config.align_scale,
            max_shift=self.config.max_shift_pixels,
            min_corr=self.config.min_correlation,
            use_ecc=self.config.use_ecc,
        )
        with self._lock:
            self._resolver = resolver
            self._aligner = aligner
            self._reference = frame.copy()
            self.output_shape = resolver.out_shape
            self.output_pixels = resolver.output_pixels
            self.effective_scale = resolver.effective_scale
        log.info("Pipeline inicializado: %dx%d -> %dx%d (escala %.3f)", w, h,
                 resolver.out_shape[1], resolver.out_shape[0], resolver.effective_scale)

    def _accumulate(self, frame: np.ndarray, align: dict | None, verdict: dict) -> None:
        resolver = self._resolver
        if resolver is None:
            return
        matrix = align["matrix"] if align is not None else np.eye(2, 3)
        # Peso por nitidez relativa al umbral + penalizacion por recorte.
        threshold = max(1.0, float(verdict.get("threshold") or 1.0))
        sharp = float(verdict.get("sharpness") or 0.0)
        weight = float(np.clip((sharp / threshold) ** 0.5, 0.3, 3.0))
        clipped = float(verdict.get("clipped_ratio") or 0.0)
        weight *= float(np.clip(1.0 - clipped * 2.0, 0.4, 1.0))
        resolver.add_frame(frame, matrix, weight)
        with self._lock:
            self.accepted += 1
            if align is not None and align.get("valid"):
                dx, dy = align.get("shift", (0.0, 0.0))
                self.shifts.append(float(np.hypot(dx, dy)))

    def _update_preview(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        scale = min(1.0, 720.0 / max(1, max(h, w)))
        small = frame if scale >= 1.0 else cv2.resize(frame, (int(w * scale), int(h * scale)),
                                                      interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ok:
            with self._lock:
                self._preview_jpeg = buf.tobytes()
                self._preview_stamp = time.monotonic()

    # ------------------------------------------------------------------
    def _process_and_save(self) -> None:
        resolver = self._resolver
        assert resolver is not None
        with self._lock:
            self.state = STATE_PROCESSING
            self.message = "Procesando superresolucion (IBP + deconvolucion)..."

        t0 = time.monotonic()
        image, coverage = resolver.finalize(
            refine_iterations=self.refine_iterations,
            psf_sigma=self.psf_sigma,
            rl_iterations=self.rl_iterations,
            white_balance=False,
            denoise_chroma=self.config.chroma_denoise,
            unsharp=self.config.unsharp,
        )
        process_s = time.monotonic() - t0

        reference = self._reference
        if reference is None:
            reference = np.zeros((1, 1, 3), np.uint8)
        thumb = _make_thumb(image)
        compare = _make_compare(reference, image, frames=self.accepted)

        # Fuente desenfocada: la fusion no puede inventar detalle. Se avisa sin
        # bloquear la entrega, y solo si no hay ya un aviso mas grave.
        ref_sharpness = 0.0
        if reference.size > 4:
            ref_sharpness = laplacian_variance(cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY))
        if ref_sharpness and ref_sharpness < SOFT_SOURCE_SHARPNESS and not self.scene_moving:
            advice = _soft_source_advice(ref_sharpness)
            with self._lock:
                self.advice = advice
            log.warning("Sesion %s: %s (nitidez %.1f)", self.session_id, advice, ref_sharpness)

        metrics: dict[str, Any] = {
            "frames_seen": self.seen,
            "frames_accepted": self.accepted,
            "frames_rejected": self.rejected,
            "quality_rejected": self.quality_rejected,
            "align_invalid": self.align_invalid,
            "scene_moving": self.scene_moving,
            "reference_sharpness": round(ref_sharpness, 1),
            "elapsed_seconds": round(self.elapsed, 2),
            "process_seconds": round(process_s, 2),
            "effective_scale": round(resolver.effective_scale, 3),
            "output_pixels": resolver.output_pixels,
            "coverage": resolver.coverage_stats,
            "refine_iterations": self.refine_iterations,
            "psf_sigma": self.psf_sigma,
            "rl_iterations": self.rl_iterations,
            "stored_frames": resolver.stored_frames,
        }
        if self.shifts:
            metrics["shift_mean_px"] = round(float(np.mean(self.shifts)), 3)
            metrics["shift_max_px"] = round(float(np.max(self.shifts)), 3)

        # Verdad de referencia disponible solo para fuentes sinteticas: permite
        # reportar honestamente cuanto se gano sobre el baseline bicubico.
        gt = getattr(self._source, "ground_truth", None)
        downsample = None
        try:
            downsample = self.source_meta.get("downsample") if isinstance(self.source_meta, dict) else None
            if downsample is None:
                downsample = getattr(self._source, "meta", {}).get("downsample")
        except Exception:  # pragma: no cover
            downsample = None
        # La comparacion solo es valida si la escala de fusion coincide con el
        # factor de decimacion del simulador (si no, se comparan resoluciones
        # distintas). Se reporta igual, marcada como no comparable.
        comparable = downsample is None or abs(float(resolver.effective_scale) - float(downsample)) <= 0.05
        if gt is not None:
            gt_resized = cv2.resize(gt, (image.shape[1], image.shape[0]),
                                    interpolation=cv2.INTER_AREA)
            baseline = cv2.resize(reference, (image.shape[1], image.shape[0]),
                                  interpolation=cv2.INTER_CUBIC)
            metrics["ground_truth"] = {
                "psnr_superfoto": round(psnr(gt_resized, image), 3),
                "psnr_bicubic": round(psnr(gt_resized, baseline), 3),
                "ssim_superfoto": round(ssim(gt_resized, image), 4),
                "ssim_bicubic": round(ssim(gt_resized, baseline), 4),
                "comparable": bool(comparable),
                "downsample": downsample,
            }
            if comparable:
                metrics["ground_truth"]["gain_db"] = round(
                    metrics["ground_truth"]["psnr_superfoto"]
                    - metrics["ground_truth"]["psnr_bicubic"], 3
                )

        meta = {
            "session_id": self.session_id,
            "created_at": _now_iso(),
            "source": {"kind": self.source_kind, "meta": self.source_meta},
            "scale": round(resolver.effective_scale, 3),
            "requested_scale": round(self.scale, 3),
            "frames_accepted": self.accepted,
            "elapsed_seconds": round(self.elapsed, 2),
            "metrics": metrics,
            "label": self.label,
            "advice": self.advice,
            "params": {
                "quality_percentile": self.quality_percentile,
                "min_sharpness": self.min_sharpness,
                "target_frames": self.target_frames,
                "duration_s": self.duration_s,
                "max_shift_pixels": self.config.max_shift_pixels,
                "min_correlation": self.config.min_correlation,
                "use_ecc": self.config.use_ecc,
            },
        }

        saved = self.storage.save(
            self.session_id,
            image=image,
            thumb=thumb,
            compare=compare,
            meta=meta,
            reference=reference if reference.size > 4 else None,
            coverage=coverage,
        )
        self.storage.cleanup()
        self.storage.prune_previews()

        message = "Superfoto lista"
        if self.scene_moving:
            message = f"{SCENE_MOTION_ADVICE}. Se fusionaron {self.accepted} frames."
        elif self.advice:
            message = (
                f"Superfoto lista con {self.accepted} frames, pero la fuente salio "
                "desenfocada: revisa el aviso antes de repetir"
            )
        elif self.accepted < 8:
            if self.rejected == 0 and self.accepted == self.seen:
                # Todos los frames vistos se fusionaron: el limite fue el tiempo
                # o el objetivo, no la calidad. El mensaje anterior culpaba a la
                # escena y confundia ("pocos utiles 5 de 5").
                message = (
                    f"Superfoto lista con {self.accepted} frames (captura corta). "
                    "Aumenta la duracion para promediar mas frames y reducir ruido"
                )
            else:
                message = (
                    f"Pocos frames utiles ({self.accepted} de {self.seen}): "
                    "revisa textura de la escena, enfoque e iluminacion"
                )
        elif self.align_invalid > self.accepted * 3:
            message = (
                f"Superfoto lista con {self.accepted} frames; "
                f"{self.align_invalid} se descartaron por falta de textura o movimiento"
            )
        with self._lock:
            self.result_meta = saved
            self.state = STATE_FINISHED
            self.finished_at = time.monotonic()
            self.message = message
            self.output_shape = image.shape[:2]
        log.info("Sesion %s finalizada: %dx%d en %.1fs (%.1fs de postproceso)", self.session_id,
                 image.shape[1], image.shape[0], self.elapsed, process_s)

    def _finish_error(self, message: str) -> None:
        with self._lock:
            self.state = STATE_ERROR
            self.error = message
            self.message = message
            self.finished_at = time.monotonic()
        log.error("Sesion %s: %s", self.session_id or "-", message)


def session_summary(status: dict) -> str:
    """Resumen de una linea (para CLI)."""
    return (
        f"{status.get('status')} · {status.get('accepted', 0)}/{status.get('seen', 0)} frames · "
        f"{status.get('output_size_estimate') or '?'} · {status.get('elapsed_seconds', 0)} s"
    )
