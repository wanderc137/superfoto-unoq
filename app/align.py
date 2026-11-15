"""Registro subpixel de frames.

Estrategia en dos etapas:

1. ``cv2.phaseCorrelate`` sobre una version reducida (``scale``) con ventana
   Hann: robusto, sin necesidad de una estimacion inicial y con precision
   subpixel.
2. Refinamiento opcional con ``cv2.findTransformECC`` sobre un recorte central
   a resolucion nativa, inicializado con la traslacion de fase. Esto baja el
   error de ~0.2 px a ~0.02 px, que es lo que la fusion necesita para ganar
   detalle real.

La matriz devuelta siempre mapea coordenadas del frame **movil** a coordenadas
del frame **de referencia**, de modo que::

    alineado = cv2.warpAffine(frame, matrix, (ancho, alto))

queda registrado con la referencia. Ese contrato se verifica en los tests.
"""

from __future__ import annotations

import cv2
import numpy as np

from .quality import to_gray


def _resize(gray: np.ndarray, scale: float) -> np.ndarray:
    if abs(scale - 1.0) < 1e-6:
        return gray
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _translation_matrix(tx: float, ty: float) -> np.ndarray:
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty]], dtype=np.float64)


def _central_crop(shape: tuple[int, int], max_side: int) -> tuple[int, int, int, int]:
    """Devuelve (y0, y1, x0, x1) del recorte central de lado <= max_side."""
    h, w = shape[:2]
    ch = min(h, max_side)
    cw = min(w, max_side)
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    return y0, y0 + ch, x0, x0 + cw


class SubpixelAligner:
    """Estima la transformacion movil -> referencia con precision subpixel."""

    def __init__(
        self,
        ref_frame: np.ndarray,
        scale: float = 0.5,
        max_shift: float = 32.0,
        min_corr: float = 0.35,
        use_ecc: bool = True,
        ecc_motion: str = "translation",
        ecc_iters: int = 25,
        ecc_eps: float = 1e-4,
        ecc_max_side: int = 360,
    ) -> None:
        if ref_frame is None or ref_frame.size == 0:
            raise ValueError("frame de referencia vacio")
        self.ref_gray = to_gray(ref_frame).copy()
        self.ref_shape = self.ref_gray.shape[:2]
        self.scale = float(scale) if 0.1 <= float(scale) <= 1.0 else 0.5
        self.max_shift = float(max_shift)
        self.min_corr = float(min_corr)
        self.use_ecc = bool(use_ecc)
        self.ecc_motion = ecc_motion
        self.ecc_iters = int(ecc_iters)
        self.ecc_eps = float(ecc_eps)
        self.ecc_max_side = int(ecc_max_side)

        self._ref_small = _resize(self.ref_gray, self.scale).astype(np.float32)
        h, w = self._ref_small.shape[:2]
        # Ventana Hann: reduce el sangrado de bordes en phaseCorrelate.
        self._window = cv2.createHanningWindow((w, h), cv2.CV_32F)

        y0, y1, x0, x1 = _central_crop(self.ref_shape, self.ecc_max_side)
        self._crop = (y0, y1, x0, x1)
        self._ref_crop = self.ref_gray[y0:y1, x0:x1].astype(np.float32) / 255.0

    # ------------------------------------------------------------------
    def _phase(self, gray: np.ndarray) -> tuple[float, float, float]:
        small = _resize(gray, self.scale).astype(np.float32)
        if small.shape != self._ref_small.shape:
            small = cv2.resize(small, (self._ref_small.shape[1], self._ref_small.shape[0]))
        (dx, dy), response = cv2.phaseCorrelate(self._ref_small, small, self._window)
        # phaseCorrelate devuelve el desplazamiento de ``small`` respecto a la
        # referencia en coordenadas reducidas; volvemos a resolucion nativa.
        return float(dx) / self.scale, float(dy) / self.scale, float(response)

    def _ecc(self, gray: np.ndarray, init_tx: float, init_ty: float) -> tuple[np.ndarray, float]:
        """Refina la traslacion con ECC sobre el recorte central (res. nativa)."""
        y0, y1, x0, x1 = self._crop
        cur = gray[y0:y1, x0:x1].astype(np.float32) / 255.0
        if cur.shape != self._ref_crop.shape:
            cur = cv2.resize(cur, (self._ref_crop.shape[1], self._ref_crop.shape[0]))
        motion = cv2.MOTION_TRANSLATION
        if self.ecc_motion == "euclidean":
            motion = cv2.MOTION_EUCLIDEAN

        warp = np.eye(2, 3, dtype=np.float32)
        # findTransformECC devuelve el warp que mapea la PLANTILLA (referencia)
        # en la imagen de entrada (frame movil): warpAffine(template, warp) ~=
        # input. Para registrar el frame hay que invertirlo. Se inicializa con
        # la traslacion de fase, que usa la misma convencion.
        warp[0, 2] = init_tx
        warp[1, 2] = init_ty
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, self.ecc_iters, self.ecc_eps)
        try:
            cc, warp = cv2.findTransformECC(self._ref_crop, cur, warp, motion, criteria, None, 5)
        except cv2.error:
            return _translation_matrix(init_tx, init_ty), -1.0

        warp = cv2.invertAffineTransform(np.asarray(warp, dtype=np.float64))
        if not np.all(np.isfinite(warp)):
            return _translation_matrix(init_tx, init_ty), -1.0

        # ECC trabaja sobre el recorte central: hay que recomponer la
        # transformacion para el frame completo (importante si hay rotacion).
        y0, _, x0, _ = self._crop
        offset = np.array([x0, y0], dtype=np.float64)
        linear = warp[:, :2]
        translation = warp[:, 2] + offset - linear.dot(offset)
        full = np.hstack([linear, translation.reshape(2, 1)])
        return full, float(cc)

    # ------------------------------------------------------------------
    def align(self, frame: np.ndarray) -> dict:
        """Alinea ``frame`` contra la referencia.

        Devuelve ``{"matrix", "shift", "correlation", "valid", "method"}``.
        ``shift`` es el desplazamiento (dx, dy) del frame movil medido en
        pixeles del frame nativo (positivo = el contenido se movio a la
        derecha/abajo respecto de la referencia).
        """
        if frame is None or frame.size == 0:
            return {"matrix": np.eye(2, 3), "shift": (0.0, 0.0), "correlation": 0.0,
                    "valid": False, "method": "none", "reason": "empty"}

        gray = to_gray(frame)
        if gray.shape[:2] != self.ref_shape:
            gray = cv2.resize(gray, (self.ref_shape[1], self.ref_shape[0]))

        # Sin textura no hay registro posible (camara tapada, pared lisa):
        # mejor rechazar que fusionar frames mal alineados.
        # Textura sobre una submuestra: std() a resolucion nativa costaba
        # ~58 ms por frame (mas que la propia alineacion).
        gh, gw = gray.shape[:2]
        if max(gh, gw) > 256:
            tex_img = cv2.resize(gray, (max(8, gw // 4), max(8, gh // 4)),
                                 interpolation=cv2.INTER_AREA)
        else:
            tex_img = gray
        texture = float(tex_img.std())
        if texture < 4.0:
            return {
                "matrix": np.eye(2, 3),
                "shift": (0.0, 0.0),
                "correlation": 0.0,
                "valid": False,
                "method": "none",
                "reason": "low_texture",
                "texture": texture,
            }

        dx, dy, response = self._phase(gray)
        method = "phase"
        # (dx, dy) es el desplazamiento del CONTENIDO respecto de la
        # referencia; para alinear hay que aplicarle el opuesto.
        matrix = _translation_matrix(-dx, -dy)
        shift = (dx, dy)
        correlation = response

        # El pico de phaseCorrelate NO es comparable con el coeficiente de ECC:
        # depende del tamano y del contenido. Se usa un umbral mas laxo solo
        # para descartar pares sin relacion (ruido, escenas distintas).
        phase_min = float(min(0.5, max(0.05, self.min_corr * 0.5)))
        if response < phase_min:
            return {
                "matrix": matrix,
                "shift": shift,
                "correlation": correlation,
                "valid": False,
                "method": method,
                "reason": "low_correlation",
            }

        if np.hypot(dx, dy) > self.max_shift:
            return {
                "matrix": matrix,
                "shift": shift,
                "correlation": correlation,
                "valid": False,
                "method": method,
                "reason": "max_shift",
            }

        if self.use_ecc and response >= 0.05:
            ecc_matrix, cc = self._ecc(gray, dx, dy)
            if cc >= 0.0:
                # La traslacion del recorte central es valida en todo el frame
                # (usamos MOTION_TRANSLATION por defecto, sin rotacion).
                matrix = ecc_matrix
                # matrix lleva el movil a la referencia, por lo que su
                # traslacion es el opuesto del desplazamiento del contenido.
                shift = (-float(matrix[0, 2]), -float(matrix[1, 2]))
                correlation = float(cc)  # coeficiente ECC: 0..1 interpretable
                method = "ecc"
                if np.hypot(*shift) > self.max_shift:
                    return {
                        "matrix": matrix,
                        "shift": shift,
                        "correlation": correlation,
                        "valid": False,
                        "method": method,
                        "reason": "max_shift",
                    }

        if method == "phase" and correlation < phase_min:
            return {
                "matrix": matrix,
                "shift": shift,
                "correlation": correlation,
                "valid": False,
                "method": method,
                "reason": "low_correlation",
            }

        if correlation < self.min_corr:
            return {
                "matrix": matrix,
                "shift": shift,
                "correlation": correlation,
                "valid": False,
                "method": method,
                "reason": "low_correlation",
            }

        return {
            "matrix": matrix,
            "shift": shift,
            "correlation": correlation,
            "valid": True,
            "method": method,
            "reason": "ok",
            "texture": texture,
        }


def apply_matrix(frame: np.ndarray, matrix: np.ndarray, out_shape: tuple[int, int] | None = None) -> np.ndarray:
    """Warp de conveniencia usado por tests y por la CLI."""
    h, w = out_shape if out_shape else frame.shape[:2]
    interp = cv2.INTER_LANCZOS4 if frame.ndim == 3 else cv2.INTER_LINEAR
    return cv2.warpAffine(frame, matrix, (w, h), flags=interp, borderMode=cv2.BORDER_REFLECT_101)
