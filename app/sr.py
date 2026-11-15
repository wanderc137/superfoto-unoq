"""Fusion de superresolucion online (drizzle por remap inverso) + postproceso.

Idea: cada frame aceptado se reproyecta sobre una grilla fina de escala ``S``
usando la matriz de alineacion subpixel. Como cada frame cae en offsets
subpixel distintos, la grilla fina recibe muestras del mismo escenario en
posiciones complementarias: al promediar se reduce el ruido y se recupera
detalle por encima del limite de banda de un frame individual. Despues se
compensa la PSF efectiva (apertura del pixel + interpolacion) con Richardson-
Lucy sobre la luminancia.

El acumulador es float32 y se llena *online* (capturar y acumular en el mismo
hilo de trabajo): nunca se guardan miles de frames en disco ni en memoria.
"""

from __future__ import annotations

import math
import random

import cv2
import numpy as np

# Bytes por pixel de salida del acumulador: 3 canales float32 (12) +
# pesos float32 (4) + contador uint16 (2). El contador es uint16 para no
# saturar en sesiones largas (limite: 65535 frames por pixel).
_BYTES_PER_OUTPUT_PX = 18
_MAX_COUNT = np.iinfo(np.uint16).max


def _clamp_scale(
    ref_shape: tuple[int, int],
    scale: float,
    max_output_pixels: int,
    max_accumulator_bytes: int,
) -> float:
    """Ajusta la escala para no exceder pixeles ni memoria de acumuladores."""
    h, w = int(ref_shape[0]), int(ref_shape[1])
    if h <= 0 or w <= 0:
        raise ValueError("ref_shape invalido")
    s = float(min(2.0, max(0.5, scale)))
    base_px = float(h * w)
    if base_px * s * s > max_output_pixels:
        s = math.sqrt(max_output_pixels / base_px)
    if base_px * s * s * _BYTES_PER_OUTPUT_PX > max_accumulator_bytes:
        s = math.sqrt(max_accumulator_bytes / (base_px * _BYTES_PER_OUTPUT_PX))
    # La fusion nunca reduce la resolucion de la fuente: minimo 1.0.
    if s < 1.0:
        raise ValueError(
            "frame demasiado grande para el presupuesto de memoria "
            f"({h}x{w}); reduci la resolucion de captura"
        )
    return float(min(2.0, s))


class SuperResolver:
    """Acumula frames alineados en una grilla fina y produce la superfoto."""

    def __init__(
        self,
        ref_shape: tuple[int, int],
        scale: float,
        max_output_pixels: int = 32_000_000,
        max_accumulator_bytes: int = 700 * 1024 * 1024,
        interp: int = cv2.INTER_LINEAR,
        max_store_frames: int = 32,
        max_store_bytes: int = 96 * 1024 * 1024,
        seed: int = 12345,
    ) -> None:
        if len(ref_shape) < 2:
            raise ValueError("ref_shape debe ser (alto, ancho)")
        self.ref_shape = (int(ref_shape[0]), int(ref_shape[1]))
        self.requested_scale = float(scale)
        self.scale = _clamp_scale(ref_shape, scale, max_output_pixels, max_accumulator_bytes)
        self.interp = interp
        h, w = self.ref_shape
        # floor evita que el redondeo supere el presupuesto de pixeles.
        self.out_shape = (max(1, int(h * self.scale)), max(1, int(w * self.scale)))
        oh, ow = self.out_shape

        self.acc = np.zeros((oh, ow, 3), dtype=np.float32)
        self.wsum = np.zeros((oh, ow), dtype=np.float32)
        self.count = np.zeros((oh, ow), dtype=np.uint16)
        self.frames = 0
        self.weight_total = 0.0
        self._ref_bgr: np.ndarray | None = None
        # Buffer acotado para el refinamiento iterativo (IBP): NO se guardan
        # miles de frames, solo una muestra uniforme (reservoir sampling).
        self.max_store_frames = int(max(0, max_store_frames))
        self.max_store_bytes = int(max(0, max_store_bytes))
        self._store: list[tuple[np.ndarray, np.ndarray, float]] = []
        self._store_bytes = 0
        self._rng = random.Random(seed)
        self.refine_iterations = 0
        self.rejected = 0
        self._coverage_cache: dict | None = None

    # ------------------------------------------------------------------
    @property
    def effective_scale(self) -> float:
        return self.out_shape[1] / float(self.ref_shape[1])

    @property
    def output_pixels(self) -> int:
        return self.out_shape[0] * self.out_shape[1]

    @property
    def approximate_bytes(self) -> int:
        return self.output_pixels * _BYTES_PER_OUTPUT_PX

    def _identity_like(self, frame: np.ndarray) -> np.ndarray:
        return np.eye(2, 3, dtype=np.float64)

    # ------------------------------------------------------------------
    def add_frame(self, frame: np.ndarray, matrix: np.ndarray, weight: float = 1.0) -> dict:
        """Reproyecta y acumula un frame. Devuelve metricas de la operacion."""
        if frame is None or frame.size == 0:
            raise ValueError("frame vacio")
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        if frame.shape[:2] != self.ref_shape:
            frame = cv2.resize(frame, (self.ref_shape[1], self.ref_shape[0]))

        w = float(max(0.05, min(4.0, weight)))
        if self._ref_bgr is None:
            self._ref_bgr = frame.copy()

        # matrix mapea coordenadas del frame -> coordenadas de la referencia.
        # La grilla fina es la referencia escalada por S. El termino (S-1)/2
        # replica la convencion de cv2.resize (centro de pixel), de modo que la
        # superfoto queda en fase con el baseline bicubico y con el ground truth.
        linear = np.asarray(matrix, dtype=np.float64).reshape(2, 3)[:, :2]
        trans = np.asarray(matrix, dtype=np.float64).reshape(2, 3)[:, 2]
        half = 0.5 * (self.scale - 1.0)
        m_hr = np.hstack([linear * self.scale, (trans * self.scale + half).reshape(2, 1)])

        warped = cv2.warpAffine(
            frame,
            m_hr,
            (self.out_shape[1], self.out_shape[0]),
            flags=self.interp,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        mask = cv2.warpAffine(
            np.ones(frame.shape[:2], dtype=np.uint8),
            m_hr,
            (self.out_shape[1], self.out_shape[0]),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        # Sin temporales de 4.6 MP: cuando el peso es 1 se suma in place con
        # casting directo (np.add convierte en el buffer de destino). Con pesos
        # distintos hace falta el factor, pero es el caso minoritario.
        if abs(w - 1.0) <= 1e-3:
            np.add(self.acc, warped, out=self.acc, casting="unsafe")
            np.add(self.wsum, mask, out=self.wsum, casting="unsafe")
        else:
            self.acc += warped.astype(np.float32) * w
            self.wsum += mask.astype(np.float32) * w
        # Incremento saturado: al llegar a 65535 deja de contar (no desborda).
        inc = (mask > 0) & (self.count < _MAX_COUNT)
        np.add(self.count, inc.astype(np.uint16), out=self.count, casting="unsafe")
        self.frames += 1
        self.weight_total += w
        self._coverage_cache = None
        self._store_frame(frame, m_hr, w)

        return {
            "frames": self.frames,
            "weight": w,
            "output_shape": self.out_shape,
            "scale": self.effective_scale,
        }

    # ------------------------------------------------------------------
    def _store_frame(self, frame: np.ndarray, m_hr: np.ndarray, weight: float) -> None:
        """Reservoir sampling de frames para el refinamiento (memoria acotada)."""
        if self.max_store_frames <= 0 or self.max_store_bytes <= 0:
            return
        nbytes = int(frame.nbytes)
        # El presupuesto de bytes manda: con frames 1080p entran ~15, con 720p
        # entran los 32 completos. Si entraran menos de 2 no vale la pena.
        cap = min(self.max_store_frames, max(1, self.max_store_bytes // max(1, nbytes)))
        if cap < 2:
            return
        item = (frame.copy(), np.asarray(m_hr, dtype=np.float64).copy(), float(weight))
        if len(self._store) < cap:
            self._store.append(item)
            self._store_bytes += nbytes
            return
        # Reemplazo uniforme (reservoir sampling) sobre todos los frames vistos.
        j = self._rng.randrange(self.frames)
        if j < cap:
            old = self._store[j]
            self._store_bytes -= old[0].nbytes
            self._store[j] = item
            self._store_bytes += nbytes

    @property
    def stored_frames(self) -> int:
        return len(self._store)

    @property
    def stored_bytes(self) -> int:
        return self._store_bytes

    def refine(self, iterations: int = 8, lam: float = 0.7) -> np.ndarray:
        """Refinamiento iterativo (iterative back-projection).

        La fusion online promedia interpolaciones: eso reduce ruido pero no
        resuelve el problema inverso del muestreo (el contenido por encima del
        Nyquist de la fuente queda atenuado). El IBP impone consistencia con
        los frames originales: simula cada observacion desde la estimacion HR,
        calcula el residuo en la grilla LR y lo reproyecta, lo que recupera
        detalle real y no solo interpolacion.

        Usa el buffer acotado de frames (``max_store_frames``), de modo que el
        costo es constante aunque la sesion tenga miles de frames.
        """
        if self.frames == 0:
            raise ValueError("no hay frames acumulados")
        iterations = int(max(0, iterations))
        if iterations == 0 or not self._store:
            return self.result()[0]

        base, _ = self.result()
        x = base.astype(np.float32)
        out_h, out_w = self.out_shape
        h, w = self.ref_shape
        sigma_box = 0.5 * self.scale

        # Matrices inversas precalculadas (HR -> LR de cada frame).
        inverses = [cv2.invertAffineTransform(m) for _, m, _ in self._store]
        total_w = sum(max(1e-3, wt) for _, _, wt in self._store) or 1.0

        for _ in range(iterations):
            blurred = cv2.GaussianBlur(x, (0, 0), sigma_box)
            residual = np.zeros_like(x)
            for (frame, _m, wt), minv in zip(self._store, inverses):
                simulated = cv2.warpAffine(
                    blurred, minv, (w, h), flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT_101,
                )
                diff = frame.astype(np.float32) - simulated
                back = cv2.warpAffine(
                    diff, _m, (out_w, out_h), flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT_101,
                )
                residual += back * max(1e-3, wt)
            x = np.clip(x + (lam / total_w) * residual, 0.0, 255.0)

        self.refine_iterations = iterations
        return x.astype(np.uint8)

    def finalize(
        self,
        refine_iterations: int = 8,
        psf_sigma: float = 1.0,
        rl_iterations: int = 15,
        white_balance: bool = False,
        denoise_chroma: bool = True,
        unsharp: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Refina + postprocesa. Devuelve (imagen final BGR, mascara cobertura)."""
        image = self.refine(refine_iterations)
        _, coverage = self.result()
        image = postprocess(
            image,
            psf_sigma=psf_sigma,
            rl_iterations=rl_iterations,
            white_balance=white_balance,
            denoise_chroma=denoise_chroma,
            unsharp=unsharp,
        )
        return image, coverage

    # ------------------------------------------------------------------
    @property
    def coverage_stats(self) -> dict:
        """Fraccion de pixeles finos cubiertos 1/4/16 veces y conteo medio.

        ``mean_count`` puede llegar a ``frames`` (hasta 65535, limite del
        contador uint16). No satura a 255 como un contador uint8.
        """
        if self._coverage_cache is not None:
            return dict(self._coverage_cache)
        total = float(self.count.size) or 1.0
        c = self.count
        stats = {
            "coverage_1": float(np.count_nonzero(c >= 1)) / total,
            "coverage_4": float(np.count_nonzero(c >= 4)) / total,
            "coverage_16": float(np.count_nonzero(c >= 16)) / total,
            "mean_count": float(c.mean()) if self.frames else 0.0,
            "frames": self.frames,
        }
        self._coverage_cache = stats
        return dict(stats)

    def coverage_mask(self) -> np.ndarray:
        """Mascara uint8 0..255 con la cobertura normalizada."""
        if self.frames == 0:
            return np.zeros(self.out_shape, dtype=np.uint8)
        peak = float(self.count.max()) or 1.0
        return np.clip(self.count.astype(np.float32) / peak * 255.0, 0, 255).astype(np.uint8)

    def result(self, fill_scale: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Devuelve (superfoto BGR uint8, mascara de cobertura).

        Las zonas sin cobertura se rellenan con un upsample bicubico del primer
        frame (referencia) para no dejar huecos negros.
        """
        if self.frames == 0:
            raise ValueError("no hay frames acumulados")
        out = np.empty_like(self.acc)
        np.divide(self.acc, np.maximum(self.wsum, 1e-6)[..., None], out=out)
        empty = self.wsum <= 1e-6
        if np.any(empty) and self._ref_bgr is not None:
            # Relleno con el frame de referencia (bordes que no cubrio el warp):
            # es barato y no deja huecos negros.
            fill = cv2.resize(self._ref_bgr, (self.out_shape[1], self.out_shape[0]),
                              interpolation=cv2.INTER_CUBIC)
            out[empty] = fill[empty].astype(np.float32)
        img = np.clip(out, 0.0, 255.0).astype(np.uint8)
        return img, self.coverage_mask()


# ---------------------------------------------------------------------------
# Postproceso
# ---------------------------------------------------------------------------

def _gaussian_psf(sigma: float) -> np.ndarray:
    """PSF gaussiana normalizada, impar y centrada."""
    sigma = float(max(0.3, sigma))
    radius = max(1, int(math.ceil(3.0 * sigma)))
    size = 2 * radius + 1
    ax = np.arange(-radius, radius + 1, dtype=np.float32)
    xx, yy = np.meshgrid(ax, ax)
    psf = np.exp(-(xx ** 2 + yy ** 2) / (2.0 * sigma ** 2))
    return (psf / psf.sum()).astype(np.float32)


def richardson_lucy_luminance(
    img: np.ndarray,
    sigma: float = 1.0,
    iterations: int = 15,
    damping: float = 1.0,
) -> np.ndarray:
    """Deconvolucion Richardson-Lucy sobre la luminancia (canales intactos).

    Se deconvoluciona solo Y y se aplica la ganancia a BGR: preserva el color y
    evita artefactos de croma tipicos de deconvolucionar cada canal.
    """
    iterations = int(iterations)
    if iterations <= 0 or img.size == 0:
        return img

    lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
    y = lab[:, :, 0].astype(np.float32) + 1.0
    psf = _gaussian_psf(sigma)
    est = y.copy()
    eps = 1e-4
    for _ in range(iterations):
        conv = cv2.filter2D(est, cv2.CV_32F, psf, borderType=cv2.BORDER_REFLECT)
        ratio = y / np.maximum(conv, eps)
        corr = cv2.filter2D(ratio, cv2.CV_32F, psf, borderType=cv2.BORDER_REFLECT)
        if damping != 1.0:
            corr = np.power(np.maximum(corr, eps), damping)
        est = np.clip(est * corr, 0.0, 255.0)

    gain = est / np.maximum(y, eps)
    # Limitar la ganancia evita amplificar ruido en zonas planas.
    gain = np.clip(gain, 0.5, 2.0)
    out = img.astype(np.float32)
    out *= gain[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def unsharp_luminance(
    img: np.ndarray,
    sigma1: float = 1.0,
    strength1: float = 0.5,
    sigma2: float = 4.0,
    strength2: float = 0.3,
) -> np.ndarray:
    """Unsharp mask multiescala sobre la luminancia real (BGR2GRAY).

    Se calcula la ganancia en Y y se aplica a los tres canales: mismo detalle
    que en Lab pero sin problemas de rango entre versiones de OpenCV.
    """
    if img.size == 0:
        return img
    y = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    blur1 = cv2.GaussianBlur(y, (0, 0), sigma1)
    blur2 = cv2.GaussianBlur(y, (0, 0), sigma2)
    sharp = y + strength1 * (y - blur1) + strength2 * (blur1 - blur2)
    gain = np.clip(sharp / np.maximum(y, 1.0), 0.5, 2.0)
    out = img.astype(np.float32) * gain[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def chroma_denoise(img: np.ndarray, h: int = 5, hcolor: int = 5, template: int = 7,
                   search: int = 21) -> np.ndarray:
    """Suavizado suave de croma (bilateral a media resolucion).

    El ojo es mucho menos sensible al ruido de croma que al de luminancia: se
    filtra a/b a media resolucion, lo que cuesta milisegundos incluso a 12 MP
    (fastNlMeans seria demasiado lento en la placa).
    """
    if img.size == 0:
        return img
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)
    sigma_color = float(max(5, h * 4))
    sigma_space = max(1.0, float(hcolor) / 2.0)
    # bilateralFilter solo acepta 1 o 3 canales: se filtra a y b por separado.
    for ch in (1, 2):
        small = cv2.resize(lab[:, :, ch], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        den = cv2.bilateralFilter(small, 5, sigma_color, sigma_space)
        lab[:, :, ch] = cv2.resize(den, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)


def gray_world_balance(img: np.ndarray, max_cast: float = 1.35) -> np.ndarray:
    """Balance de blancos conservador: solo corrige dominantes evidentes."""
    if img.size == 0:
        return img
    means = img.reshape(-1, 3).mean(axis=0)
    if means.min() <= 1.0:
        return img
    ratio = float(means.max() / means.min())
    if ratio < max_cast:
        return img
    target = float(means.mean())
    gains = target / np.maximum(means, 1.0)
    out = img.astype(np.float32) * gains.reshape(1, 1, 3)
    return np.clip(out, 0, 255).astype(np.uint8)


def postprocess(
    img: np.ndarray,
    psf_sigma: float = 1.0,
    rl_iterations: int = 15,
    white_balance: bool = False,
    denoise_chroma: bool = True,
    unsharp: bool = True,
    rl_damping: float = 1.0,
) -> np.ndarray:
    """Pipeline de postproceso completo (idempotente y sin estado)."""
    out = img
    if white_balance:
        out = gray_world_balance(out)
    if rl_iterations > 0 and psf_sigma > 0:
        out = richardson_lucy_luminance(out, psf_sigma, rl_iterations, rl_damping)
    if unsharp:
        out = unsharp_luminance(out)
    if denoise_chroma:
        out = chroma_denoise(out)
    return out


# ---------------------------------------------------------------------------
# Metricas objetivas (usadas por benchmark y tests; sin skimage)
# ---------------------------------------------------------------------------

def psnr(a: np.ndarray, b: np.ndarray, data_range: float = 255.0) -> float:
    """PSNR entre dos imagenes del mismo tamano (BGR o gris)."""
    if a.shape != b.shape:
        raise ValueError(f"shapes distintos: {a.shape} vs {b.shape}")
    diff = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(diff * diff))
    if mse <= 1e-12:
        return 99.0
    return float(10.0 * math.log10((data_range ** 2) / mse))


def ssim(a: np.ndarray, b: np.ndarray, data_range: float = 255.0) -> float:
    """SSIM global con ventana gaussiana 11x11 sigma 1.5 (implementacion propia)."""
    if a.shape != b.shape:
        raise ValueError(f"shapes distintos: {a.shape} vs {b.shape}")
    if a.ndim == 3:
        a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    x = a.astype(np.float32)
    y = b.astype(np.float32)
    c1 = (0.01 * data_range) ** 2
    c2 = (0.03 * data_range) ** 2
    mu_x = cv2.GaussianBlur(x, (11, 11), 1.5)
    mu_y = cv2.GaussianBlur(y, (11, 11), 1.5)
    mu_xx = mu_x * mu_x
    mu_yy = mu_y * mu_y
    mu_xy = mu_x * mu_y
    sigma_xx = cv2.GaussianBlur(x * x, (11, 11), 1.5) - mu_xx
    sigma_yy = cv2.GaussianBlur(y * y, (11, 11), 1.5) - mu_yy
    sigma_xy = cv2.GaussianBlur(x * y, (11, 11), 1.5) - mu_xy
    num = (2 * mu_xy + c1) * (2 * sigma_xy + c2)
    den = (mu_xx + mu_yy + c1) * (sigma_xx + sigma_yy + c2)
    return float(np.mean(num / np.maximum(den, 1e-9)))
