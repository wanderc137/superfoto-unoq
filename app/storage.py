"""Persistencia de sesiones y resultados.

Estructura en disco (por sesion)::

    data/sessions/<session_id>/
        superfoto.png    imagen final sin perdida
        superfoto.jpg    calidad 95
        thumb.jpg        miniatura
        compare.jpg      recorte 1:1 bicubico vs superfoto
        reference.jpg    frame de referencia usado
        coverage.png     mapa de cobertura subpixel
        session.json     metricas y metadatos

Se conservan solo las ``keep`` sesiones mas recientes para no llenar el disco
de la placa (771 MB libres).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("superfoto.storage")

SESSION_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}(_[A-Za-z0-9]{1,24})?$")
RESULT_FILES = {
    "png": "superfoto.png",
    "jpg": "superfoto.jpg",
    "thumb": "thumb.jpg",
    "compare": "compare.jpg",
    "reference": "reference.jpg",
    "coverage": "coverage.png",
    "json": "session.json",
}


class StorageError(OSError):
    """Error de almacenamiento con mensaje apto para mostrar en la UI."""


class Storage:
    """Gestiona el directorio de datos y el ciclo de vida de las sesiones.

    * Las escrituras son **atomicas** (``.tmp`` + ``os.replace``): un corte de
      energia nunca deja un archivo de resultado a medio escribir.
    * Antes de guardar se verifica el **espacio libre** (``min_free_mb``). Si no
      alcanza, se hace una limpieza agresiva y, si aun asi falta, se aborta con
      un mensaje claro en lugar de llenar el disco.
    """

    def __init__(self, base: str | Path, keep: int = 3, previews_limit: int = 8,
                 min_free_mb: int = 500, fsync: bool = True) -> None:
        self.base = Path(base).expanduser()
        self.sessions_dir = self.base / "sessions"
        self.previews_dir = self.base / "previews"
        self.keep = int(max(1, keep))
        self.previews_limit = int(max(0, previews_limit))
        self.min_free_mb = int(max(0, min_free_mb))
        self.fsync = bool(fsync)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_tmp()

    # ------------------------------------------------------------------
    # Escritura atomica y espacio libre
    # ------------------------------------------------------------------
    @staticmethod
    def _tmp_path(path: Path) -> Path:
        # Conserva la extension original: cv2.imwrite infiere el codec de ella.
        # Resultado: "superfoto.tmp.png" -> se renombra a "superfoto.png".
        return path.with_name(f"{path.stem}.tmp{path.suffix}")

    def _fsync_file(self, path: Path) -> None:
        if not self.fsync:
            return
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        tmp = self._tmp_path(path)
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _atomic_imwrite(self, path: Path, image, params: list[int] | None = None) -> None:
        tmp = self._tmp_path(path)
        try:
            ok = cv2.imwrite(str(tmp), image, params or [])
            if not ok or not tmp.is_file():
                raise OSError(f"no se pudo escribir {path.name}")
            self._fsync_file(tmp)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def cleanup_tmp(self) -> list[str]:
        """Elimina restos ``*.tmp`` de escrituras interrumpidas."""
        removed: list[str] = []
        if not self.sessions_dir.is_dir():
            return removed
        for path in self.sessions_dir.glob("*/*.tmp.*"):
            try:
                path.unlink()
                removed.append(str(path.relative_to(self.sessions_dir)))
            except OSError:
                pass
        return removed

    def _free_bytes(self) -> int:
        try:
            return int(shutil.disk_usage(str(self.base)).free)
        except OSError:
            return 0

    def _ensure_free_space(self, need_bytes: int, protect: str = "") -> None:
        """Garantiza ``min_free_mb`` libres despues de escribir ``need_bytes``."""
        min_free = self.min_free_mb * 1024 * 1024
        free = self._free_bytes()
        if free - need_bytes >= min_free:
            return
        log.warning("Poco espacio: %.0f MB libres, se necesita %.0f MB + %d MB de margen",
                    free / 1e6, need_bytes / 1e6, self.min_free_mb)
        # Limpieza agresiva: conservar solo la sesion mas reciente.
        removed = self.cleanup(keep=1, protect=protect)
        if removed:
            log.warning("Limpieza agresiva: %s", ", ".join(removed))
        free = self._free_bytes()
        if free - need_bytes >= min_free:
            return
        raise StorageError(
            "espacio insuficiente en disco: quedan "
            f"{free / 1e6:.0f} MB, se necesitan {need_bytes / 1e6:.0f} MB "
            f"y el minimo configurado es {self.min_free_mb} MB. "
            "Libera espacio en la placa o baja sessions_keep/min_free_mb."
        )

    # ------------------------------------------------------------------
    def new_session(self, suffix: str = "") -> Path:
        """Crea y devuelve el directorio de una sesion nueva."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean = re.sub(r"[^A-Za-z0-9]", "", suffix or "")[:20]
        name = f"{stamp}_{clean}" if clean else stamp
        path = self.sessions_dir / name
        n = 2
        while path.exists():
            path = self.sessions_dir / f"{name}{n}"
            n += 1
        path.mkdir(parents=True, exist_ok=False)
        return path

    # ------------------------------------------------------------------
    def save(
        self,
        session_id: str,
        image: np.ndarray,
        thumb: np.ndarray,
        compare: np.ndarray,
        meta: dict,
        reference: np.ndarray | None = None,
        coverage: np.ndarray | None = None,
        jpeg_quality: int = 95,
    ) -> dict:
        """Escribe todos los artefactos de una sesion y devuelve el resumen.

        Todos los archivos se escriben primero como ``.tmp`` y se renombran con
        ``os.replace`` al final (atomico). Si algo falla, se limpian los
        temporales y se descarta la sesion incompleta.
        """
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        # Estimacion conservadora: PNG de imagen ruidosa ~0.75 B/px + margen.
        need = int(getattr(image, "nbytes", 0) * 0.75) + 2 * 1024 * 1024
        self._ensure_free_space(need, protect=session_dir.name)

        json_path = session_dir / RESULT_FILES["json"]
        try:
            self._atomic_imwrite(session_dir / RESULT_FILES["png"], image)
            self._atomic_imwrite(session_dir / RESULT_FILES["jpg"], image,
                                 [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
            self._atomic_imwrite(session_dir / RESULT_FILES["thumb"], thumb,
                                 [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            if compare is not None and compare.size:
                self._atomic_imwrite(session_dir / RESULT_FILES["compare"], compare,
                                     [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if reference is not None and reference.size:
                self._atomic_imwrite(session_dir / RESULT_FILES["reference"], reference,
                                     [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if coverage is not None and coverage.size:
                self._atomic_imwrite(session_dir / RESULT_FILES["coverage"], coverage)

            payload = dict(meta)
            payload.setdefault("session_id", Path(session_id).name)
            payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            payload["files"] = {
                key: name for key, name in RESULT_FILES.items() if (session_dir / name).is_file()
            }
            payload["bytes"] = {
                name: (session_dir / name).stat().st_size
                for name in payload["files"].values()
            }
            payload["output_shape"] = [int(image.shape[0]), int(image.shape[1])]
            # session.json se escribe al final: es el marcador de completitud.
            self._atomic_write_bytes(
                json_path, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            )
        except Exception as exc:
            self.cleanup_tmp()
            if not json_path.exists():
                # Sesion nunca completada: no dejar restos en el historial.
                shutil.rmtree(session_dir, ignore_errors=True)
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"no se pudo guardar la sesion {session_dir.name}: {exc}") from exc

        log.info("Sesion guardada: %s (%dx%d)", session_id, image.shape[1], image.shape[0])
        return payload

    # ------------------------------------------------------------------
    def _session_dir(self, session_id: str) -> Path:
        name = Path(str(session_id)).name
        if not SESSION_ID_RE.match(name):
            raise ValueError(f"id de sesion invalido: {session_id!r}")
        return self.sessions_dir / name

    def session_dir(self, session_id: str) -> Path | None:
        try:
            path = self._session_dir(session_id)
        except ValueError:
            return None
        return path if path.is_dir() else None

    def file_path(self, session_id: str, key: str) -> Path | None:
        """Resuelve un archivo por clave de allowlist (``png``, ``jpg``...)."""
        name = RESULT_FILES.get((key or "").strip().lower())
        if not name:
            return None
        session_dir = self.session_dir(session_id)
        if session_dir is None:
            return None
        path = session_dir / name
        if not path.is_file():
            return None
        # Defensa extra: el path resuelto debe quedar dentro del directorio.
        try:
            path.resolve().relative_to(session_dir.resolve())
        except ValueError:
            return None
        return path

    # ------------------------------------------------------------------
    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Sesiones mas recientes primero (con su session.json si existe)."""
        items: list[dict] = []
        if not self.sessions_dir.is_dir():
            return items
        dirs = [p for p in self.sessions_dir.iterdir() if p.is_dir()]
        dirs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for path in dirs[:limit]:
            items.append(self._describe(path))
        return items

    def get_session(self, session_id: str) -> dict | None:
        path = self.session_dir(session_id)
        return self._describe(path) if path else None

    def _describe(self, path: Path) -> dict:
        meta: dict = {}
        json_path = path / "session.json"
        if json_path.is_file():
            try:
                meta = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        files = {key: name for key, name in RESULT_FILES.items() if (path / name).is_file()}
        entry = {
            # Solo identificador y ruta RELATIVA: nunca exponer rutas absolutas.
            "id": path.name,
            "rel": f"sessions/{path.name}",
            "created_at": meta.get("created_at")
            or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "files": files,
            "urls": {key: f"/api/result/{path.name}/{key}" for key in files},
            "metrics": meta.get("metrics", {}),
            "output_shape": meta.get("output_shape"),
            "source": meta.get("source", {}),
            "scale": meta.get("scale"),
            "frames_accepted": meta.get("frames_accepted"),
            "elapsed_seconds": meta.get("elapsed_seconds"),
            "bytes_total": sum((path / name).stat().st_size for name in files.values()),
            "has_json": bool(meta),
        }
        return entry

    # ------------------------------------------------------------------
    def cleanup(self, keep: int | None = None, protect: str = "") -> list[str]:
        """Borra las sesiones mas viejas dejando ``keep`` (default: config).

        ``protect`` permite excluir una sesion (por ejemplo la que se esta
        guardando) de la limpieza.
        """
        limit = self.keep if keep is None else max(1, int(keep))
        dirs = [p for p in self.sessions_dir.iterdir() if p.is_dir()] if self.sessions_dir.is_dir() else []
        dirs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        removed: list[str] = []
        kept = 0
        for path in dirs:
            if path.name == protect or kept < limit:
                kept += 1
                continue
            try:
                shutil.rmtree(path)
                removed.append(path.name)
            except OSError as exc:  # pragma: no cover
                log.warning("No se pudo borrar %s: %s", path.name, exc)
        if removed:
            log.info("Sesiones eliminadas: %s", ", ".join(removed))
        self.cleanup_tmp()
        return removed

    def prune_previews(self) -> list[str]:
        """Mantiene como maximo ``previews_limit`` archivos de preview."""
        if self.previews_limit <= 0 or not self.previews_dir.is_dir():
            return []
        files = sorted(
            (p for p in self.previews_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = []
        for path in files[self.previews_limit:]:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
        return removed

    def disk_usage(self) -> dict:
        try:
            usage = shutil.disk_usage(str(self.base))
            free_mb = usage.free / 1e6
            return {
                "total_mb": round(usage.total / 1e6, 1),
                "free_mb": round(free_mb, 1),
                "used_pct": round(100.0 * usage.used / max(1, usage.total), 1),
                "min_free_mb": self.min_free_mb,
                "low_space": free_mb < self.min_free_mb,
            }
        except OSError:
            return {"total_mb": 0.0, "free_mb": 0.0, "used_pct": 0.0,
                    "min_free_mb": self.min_free_mb, "low_space": True}
