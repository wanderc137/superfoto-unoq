"""Gate de calidad de frames (lucky imaging).

Cada frame se evalua con:

* **nitidez**: varianza del Laplaciano sobre la ROI central a resolucion nativa;
* **exposicion**: fraccion de pixeles recortados (>=250) y de negros (<5);
* **contraste**: desviacion estandar del gris.

El umbral de nitidez es adaptativo: percentil configurable sobre una ventana
deslizante de los ultimos frames vistos. Mientras no haya historia suficiente
(``warmup``) se acepta salvo que exista un ``min_sharpness`` explicito.
"""

from __future__ import annotations

import threading
from collections import deque

import cv2
import numpy as np


def to_gray(frame: np.ndarray) -> np.ndarray:
    """Convierte BGR/gris a gris uint8 sin copiar de mas."""
    if frame is None:
        raise ValueError("frame vacio")
    if frame.ndim == 2:
        return frame
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def center_roi(frame: np.ndarray, ratio: float = 0.5) -> np.ndarray:
    """Devuelve la ROI central (vista, sin copia) de un frame."""
    h, w = frame.shape[:2]
    ratio = float(min(1.0, max(0.1, ratio)))
    rh, rw = max(8, int(h * ratio)), max(8, int(w * ratio))
    y0 = max(0, (h - rh) // 2)
    x0 = max(0, (w - rw) // 2)
    return frame[y0:y0 + rh, x0:x0 + rw]


def laplacian_variance(gray: np.ndarray) -> float:
    """Varianza del Laplaciano (metrica clasica de nitidez)."""
    if gray.size == 0:
        return 0.0
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    return float(lap.var())


class QualityGate:
    """Decide si un frame aporta informacion util a la fusion.

    El estado interno (historia, contadores y umbral) esta protegido por un
    ``threading.RLock``: ``update()`` y las propiedades se pueden consultar
    desde el hilo de captura y desde la API sin condiciones de carrera.
    """

    def __init__(
        self,
        percentile: float = 60.0,
        min_sharpness: float | None = None,
        roi_ratio: float = 0.5,
        max_clipped_ratio: float = 0.05,
        min_contrast_std: float = 3.0,
        warmup: int = 8,
        history: int = 120,
    ) -> None:
        self.percentile = float(min(99.0, max(0.0, percentile)))
        self.min_sharpness = float(min_sharpness) if min_sharpness is not None else None
        self.roi_ratio = float(roi_ratio)
        self.max_clipped_ratio = float(max_clipped_ratio)
        self.min_contrast_std = float(min_contrast_std)
        self.warmup = int(max(1, warmup))
        self._history: deque[float] = deque(maxlen=int(max(8, history)))
        self._adaptive = True
        self._seen = 0
        self._accepted = 0
        self._rejected = 0
        self._reject_reasons: dict[str, int] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def set_adaptive(self, enabled: bool) -> None:
        """Activa/desactiva el umbral adaptativo por percentil."""
        with self._lock:
            self._adaptive = bool(enabled)

    @property
    def adaptive(self) -> bool:
        with self._lock:
            return self._adaptive

    def _threshold_locked(self) -> float:
        if self._adaptive and len(self._history) >= self.warmup:
            return float(np.percentile(np.asarray(self._history, dtype=np.float32), self.percentile))
        if self.min_sharpness is not None:
            return float(self.min_sharpness)
        return 0.0

    @property
    def threshold(self) -> float:
        """Umbral efectivo actual de nitidez (thread-safe)."""
        with self._lock:
            return self._threshold_locked()

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "seen": self._seen,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "threshold": round(self._threshold_locked(), 2),
                "adaptive": self._adaptive,
                "history": len(self._history),
                "reasons": dict(self._reject_reasons),
            }

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._seen = self._accepted = self._rejected = 0
            self._reject_reasons.clear()

    # ------------------------------------------------------------------
    def measure(self, frame: np.ndarray) -> dict:
        """Metricas crudas del frame (sin decidir todavia).

        La nitidez se mide a resolucion nativa para que el umbral sea
        comparable entre frames. Exposicion y contraste se estiman sobre una
        submuestra regular: recorrer los 2 MP completos costaba ~190 ms por
        frame en la UNO Q y limitaba la captura a ~1 frame/s.
        """
        gray = to_gray(frame)
        roi = center_roi(gray, self.roi_ratio)
        sharpness = laplacian_variance(roi)

        # Submuestra 1 de cada 4 pixeles por eje: conserva los valores por
        # canal (un canal saturado sigue siendolo), a 1/16 del costo.
        sample = frame[::4, ::4]
        if sample.ndim == 3:
            clipped = float(np.count_nonzero(np.any(sample >= 250, axis=2))) / float(
                sample.shape[0] * sample.shape[1])
        else:
            clipped = float(np.count_nonzero(sample >= 250)) / float(sample.size)
        small = cv2.resize(gray, (max(8, gray.shape[1] // 4), max(8, gray.shape[0] // 4)),
                           interpolation=cv2.INTER_AREA)
        contrast = float(small.std())
        mean = float(small.mean())
        return {
            "sharpness": sharpness,
            "clipped_ratio": clipped,
            "contrast": contrast,
            "mean_luma": mean,
        }

    def update(self, frame: np.ndarray) -> dict:
        """Evalua un frame y devuelve el dict de decision (thread-safe).

        Claves: ``sharpness``, ``accept``, ``reason``, ``exposure_ok`` y,
        adicionalmente, ``threshold``, ``clipped_ratio``, ``contrast``.
        El calculo de metricas es puro; solo la actualizacion de estado va
        dentro del lock.
        """
        if frame is None or getattr(frame, "size", 0) == 0:
            with self._lock:
                self._seen += 1
                self._rejected += 1
                self._bump("empty")
                threshold = self._threshold_locked()
            return {
                "sharpness": 0.0,
                "accept": False,
                "reason": "empty",
                "exposure_ok": False,
                "threshold": threshold,
                "clipped_ratio": 1.0,
                "contrast": 0.0,
            }

        m = self.measure(frame)
        sharpness = m["sharpness"]

        with self._lock:
            self._seen += 1
            # La historia guarda TODOS los frames vistos: si solo guardaramos
            # los aceptados el umbral se dispararia y rechazaria todo.
            self._history.append(sharpness)
            threshold = self._threshold_locked()

            reason = ""
            exposure_ok = True
            if m["clipped_ratio"] > self.max_clipped_ratio:
                exposure_ok = False
                reason = "overexposed"
            elif m["contrast"] < self.min_contrast_std:
                reason = "low_contrast"
            elif sharpness < threshold:
                reason = "blurry"

            accept = reason == ""
            if accept:
                self._accepted += 1
            else:
                self._rejected += 1
                self._bump(reason)

            return {
                "sharpness": sharpness,
                "accept": accept,
                "reason": reason or "ok",
                "exposure_ok": exposure_ok,
                "threshold": threshold,
                "clipped_ratio": m["clipped_ratio"],
                "contrast": m["contrast"],
                "mean_luma": m["mean_luma"],
            }

    def _bump(self, reason: str) -> None:
        self._reject_reasons[reason] = self._reject_reasons.get(reason, 0) + 1
