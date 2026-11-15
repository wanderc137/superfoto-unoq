"""superfoto-unoq: superresolucion por fusion de frames en Arduino UNO Q.

Paquete principal del servicio. Todos los modulos usan solo stdlib + OpenCV +
NumPy, tal como exige la placa (Debian 13 aarch64, sin pip disponible).
"""

from .config import APP_NAME, APP_VERSION, Config

__all__ = ["APP_NAME", "APP_VERSION", "Config"]
