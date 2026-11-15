"""Configuracion central del servicio superfoto-unoq.

Los valores por defecto apuntan a la Arduino UNO Q (``/home/arduino/...``),
pero TODOS los parametros relevantes se pueden sobrescribir por variables de
entorno para poder correr el mismo codigo en el host de desarrollo::

    SUPERFOTO_DATA_DIR=/tmp/sf-data
    SUPERFOTO_PORT=8010
    SUPERFOTO_HOST=127.0.0.1
    SUPERFOTO_SOURCE=fake
    SUPERFOTO_SOURCE_URL=http://192.168.1.23:8080
    SUPERFOTO_SOURCE_PATH=/ruta/video.mp4
    SUPERFOTO_SCALE=1.5
    SUPERFOTO_TARGET_FRAMES=30
    SUPERFOTO_DURATION=15
    SUPERFOTO_QUALITY_PERCENTILE=60
    SUPERFOTO_PSF_SIGMA=1.0
    SUPERFOTO_RL_ITERATIONS=12
    SUPERFOTO_MAX_OUTPUT_PIXELS=32000000
    SUPERFOTO_SESSIONS_KEEP=3

Tambien se puede usar un ``config.json`` en la raiz del proyecto con las
mismas claves (sin secretos).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "superfoto-unoq"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Constantes fisicas / de algoritmo (seccion 7 del plan)
# ---------------------------------------------------------------------------

MIN_SCALE = 1.0
MAX_SCALE = 2.0
MAX_OUTPUT_PIXELS = 32_000_000
# Presupuesto de memoria para los acumuladores float32. La placa tiene ~2.4 GB
# libres: 700 MB deja aire para OpenCV, el preview y el postproceso.
MAX_ACCUMULATOR_BYTES = 700 * 1024 * 1024

QUALITY_PERCENTILE = 60.0
SHARPNESS_ROI_RATIO = 0.5
MAX_CLIPPED_RATIO = 0.05
MIN_CONTRAST_STD = 3.0
QUALITY_WARMUP_FRAMES = 8
QUALITY_HISTORY = 120

ALIGN_SCALE = 0.5
MAX_SHIFT_PX = 32.0
MIN_CORRELATION = 0.35
USE_ECC_REFINEMENT = True
ECC_CRITERIA_EPS = 1e-4
ECC_CRITERIA_ITERS = 50

COVERAGE_MIN_WEIGHT = 0.1
WEIGHT_SHARPNESS_EXP = 1.0
WEIGHT_EXPOSURE_EXP = 1.0

# Defaults medidos con el benchmark sintetico (ver scripts/benchmark_sr.py):
# el refinamiento IBP ya deconvoluciona el muestreo; el RL termina de compensar
# la PSF optica. sigma=0.6 px de salida y 15 iteraciones dieron el mejor PSNR.
PSF_SIGMA = 0.6
RL_ITERATIONS = 15
REFINE_ITERATIONS = 8
REFINE_LAMBDA = 0.7
UNSHARP_ENABLED = False
UNSHARP_SIGMA1 = 1.0
UNSHARP_STRENGTH1 = 0.5
UNSHARP_SIGMA2 = 4.0
UNSHARP_STRENGTH2 = 0.3
DENOISE_CHROMA_H = 5
DENOISE_CHROMA_HCOLOR = 5

FRAME_TIMEOUT_MS = 5000
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0
DISCOVER_TIMEOUT = 0.8
DISCOVER_PORTS = (8080, 8081)
DISCOVER_MAX_WORKERS = 64

MAX_PREVIEW_FILES = 8
DEFAULT_TARGET_FRAMES = 30
DEFAULT_DURATION_S = 15.0
MAX_ELAPSED_S = 300.0

# Puerto del servicio vecino: NO tocar nunca.
DETECTOR_TARJETAS_PORT = 8000

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
SKETCH_DIR = BASE_DIR / "sketch" / "superfoto_mcu"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def default_data_dir() -> Path:
    """Directorio de datos por defecto (override por entorno)."""
    return Path(_env_str("SUPERFOTO_DATA_DIR", str(Path.home() / "superfoto-unoq" / "data")))


@dataclass
class Config:
    """Configuracion efectiva del servicio."""

    host: str = "0.0.0.0"
    port: int = 8010
    data_dir: Path = field(default_factory=default_data_dir)
    sessions_keep: int = 3

    # Superresolucion
    default_scale: float = 1.5
    max_output_pixels: int = MAX_OUTPUT_PIXELS
    max_accumulator_bytes: int = MAX_ACCUMULATOR_BYTES

    # Calidad
    quality_percentile: float = QUALITY_PERCENTILE
    min_sharpness: float | None = None
    max_clipped_ratio: float = MAX_CLIPPED_RATIO
    min_contrast_std: float = MIN_CONTRAST_STD

    # Alineacion
    align_scale: float = ALIGN_SCALE
    max_shift_pixels: float = MAX_SHIFT_PX
    min_correlation: float = MIN_CORRELATION
    use_ecc: bool = USE_ECC_REFINEMENT

    # Postproceso
    psf_sigma: float = PSF_SIGMA
    rl_iterations: int = RL_ITERATIONS
    refine_iterations: int = REFINE_ITERATIONS
    refine_lambda: float = REFINE_LAMBDA
    chroma_denoise: bool = True
    unsharp: bool = UNSHARP_ENABLED

    # Captura
    frame_timeout_ms: int = FRAME_TIMEOUT_MS
    reconnect_backoff: tuple[float, float] = (RECONNECT_MIN_S, RECONNECT_MAX_S)
    target_frames: int = DEFAULT_TARGET_FRAMES
    duration_s: float = DEFAULT_DURATION_S
    max_elapsed_s: float = MAX_ELAPSED_S

    # Fuente por defecto (auto | ip | file | webcam | folder | fake)
    source: str = "auto"
    source_url: str = ""
    source_path: str = ""

    # Descubrimiento
    discover_ports: tuple[int, ...] = DISCOVER_PORTS
    discover_timeout: float = DISCOVER_TIMEOUT
    discover_subnet: str = "192.168.1"

    # Almacenamiento: espacio libre minimo que debe quedar tras guardar.
    min_free_mb: int = 500

    # Servicio
    preview_interval_s: float = 0.35
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Construye la configuracion: defaults -> config.json -> entorno."""
        cfg = cls()
        cfg_path = Path(path) if path else (BASE_DIR / "config.json")
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg._apply_mapping(data)
            except (OSError, ValueError):
                # Un config.json corrupto no debe impedir el arranque.
                pass
        cfg._apply_env()
        cfg.validate()
        return cfg

    def _apply_mapping(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if not hasattr(self, key) or key.startswith("_"):
                continue
            current = getattr(self, key)
            try:
                if key == "data_dir":
                    setattr(self, key, Path(value).expanduser())
                elif key == "reconnect_backoff":
                    pair = tuple(float(v) for v in value)[:2]
                    if len(pair) == 2:
                        setattr(self, key, pair)
                elif key == "discover_ports":
                    setattr(self, key, tuple(int(v) for v in value))
                elif isinstance(current, bool):
                    setattr(self, key, bool(value))
                elif isinstance(current, float):
                    setattr(self, key, float(value))
                elif isinstance(current, int):
                    setattr(self, key, int(value))
                elif isinstance(current, str) or current is None:
                    setattr(self, key, value if value is not None else None)
            except (TypeError, ValueError):
                continue

    def _apply_env(self) -> None:
        self.host = _env_str("SUPERFOTO_HOST", self.host)
        self.port = _env_int("SUPERFOTO_PORT", self.port)
        self.data_dir = Path(_env_str("SUPERFOTO_DATA_DIR", str(self.data_dir))).expanduser()
        self.sessions_keep = _env_int("SUPERFOTO_SESSIONS_KEEP", self.sessions_keep)
        self.min_free_mb = _env_int("SUPERFOTO_MIN_FREE_MB", self.min_free_mb)
        self.default_scale = _env_float("SUPERFOTO_SCALE", self.default_scale)
        self.max_output_pixels = _env_int("SUPERFOTO_MAX_OUTPUT_PIXELS", self.max_output_pixels)
        self.quality_percentile = _env_float("SUPERFOTO_QUALITY_PERCENTILE", self.quality_percentile)
        self.psf_sigma = _env_float("SUPERFOTO_PSF_SIGMA", self.psf_sigma)
        self.rl_iterations = _env_int("SUPERFOTO_RL_ITERATIONS", self.rl_iterations)
        self.refine_iterations = _env_int("SUPERFOTO_REFINE_ITERATIONS", self.refine_iterations)
        self.frame_timeout_ms = _env_int("SUPERFOTO_FRAME_TIMEOUT_MS", self.frame_timeout_ms)
        self.target_frames = _env_int("SUPERFOTO_TARGET_FRAMES", self.target_frames)
        self.duration_s = _env_float("SUPERFOTO_DURATION", self.duration_s)
        self.source = _env_str("SUPERFOTO_SOURCE", self.source)
        self.source_url = _env_str("SUPERFOTO_SOURCE_URL", self.source_url)
        self.source_path = _env_str("SUPERFOTO_SOURCE_PATH", self.source_path)
        self.discover_subnet = _env_str("SUPERFOTO_DISCOVER_SUBNET", self.discover_subnet)
        self.discover_timeout = _env_float("SUPERFOTO_DISCOVER_TIMEOUT", self.discover_timeout)
        self.log_level = _env_str("SUPERFOTO_LOG_LEVEL", self.log_level)
        if os.environ.get("SUPERFOTO_USE_ECC") is not None:
            self.use_ecc = _env_str("SUPERFOTO_USE_ECC", "1").lower() not in ("0", "false", "no")

    def validate(self) -> None:
        """Normaliza valores fuera de rango (nunca lanza excepcion)."""
        self.port = int(self.port) if 1 <= int(self.port) <= 65535 else 8010
        self.sessions_keep = max(1, min(50, int(self.sessions_keep)))
        self.min_free_mb = max(50, min(200_000, int(self.min_free_mb)))
        self.default_scale = float(min(MAX_SCALE, max(MIN_SCALE, self.default_scale)))
        self.max_output_pixels = max(1_000_000, int(self.max_output_pixels))
        self.quality_percentile = float(min(95.0, max(0.0, self.quality_percentile)))
        self.psf_sigma = float(min(4.0, max(0.3, self.psf_sigma)))
        self.rl_iterations = int(min(40, max(0, self.rl_iterations)))
        self.refine_iterations = int(min(30, max(0, self.refine_iterations)))
        self.target_frames = int(min(5000, max(1, self.target_frames)))
        self.duration_s = float(min(3600.0, max(0.0, self.duration_s)))
        self.frame_timeout_ms = int(min(60_000, max(200, self.frame_timeout_ms)))

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["data_dir"] = str(self.data_dir)
        data["reconnect_backoff"] = list(self.reconnect_backoff)
        data["discover_ports"] = list(self.discover_ports)
        return data

    @property
    def sessions_dir(self) -> Path:
        return Path(self.data_dir) / "sessions"

    @property
    def previews_dir(self) -> Path:
        return Path(self.data_dir) / "previews"

    def ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
