"""Descubrimiento de camaras IP Webcam en la LAN.

Barrido concurrente con threads (no asyncio: el resto del servicio es threading
puro) y timeouts cortos. Se considera camara valida si ``/status.json`` devuelve
la firma de IP Webcam o si ``/video`` responde multipart/JPEG.

El barrido esta acotado: 254 hosts x N puertos con 64 workers y 0.8 s de timeout
termina en ~1-2 s en el peor caso, sin bloquear el resto del servicio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("superfoto.discover")

IPWEBCAM_KEYS = ("ipwebcam", "previewResolution", "videoWidth", "audioFormat", "sensorOrientation")


def local_ip() -> str:
    """IP local preferida (para elegir la subred por defecto)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 53))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def guess_subnet() -> str:
    return ".".join(local_ip().split(".")[:3]) or "192.168.1"


def _http_get(host: str, port: int, path: str, timeout: float) -> tuple[int, dict, bytes]:
    """GET minimo con http.client (mas rapido que urllib para 254 hosts)."""
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "superfoto-unoq/1.0"})
        resp = conn.getresponse()
        body = resp.read(4096)
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _probe_host(host: str, ports: tuple[int, ...], timeout: float) -> list[dict]:
    """Prueba un host en todos los puertos. Devuelve 0..N camaras."""
    found: list[dict] = []
    for port in ports:
        t0 = time.monotonic()
        # Filtro rapido: si el puerto no acepta TCP, no vale la pena el HTTP.
        try:
            with socket.create_connection((host, port), timeout=min(timeout, 0.35)):
                pass
        except OSError:
            continue

        resolution = None
        name = ""
        try:
            status, headers, body = _http_get(host, port, "/status.json", timeout)
            if status == 200 and body:
                data = json.loads(body.decode("utf-8", "replace"))
                if isinstance(data, dict) and (
                    any(k in data for k in IPWEBCAM_KEYS)
                    or "ipwebcam" in str(data).lower()
                    or "resolution" in data
                ):
                    resolution = data.get("previewResolution") or data.get("resolution")
                    name = str(data.get("deviceName") or data.get("name") or "IP Webcam")
        except (OSError, ValueError):
            pass

        if not resolution:
            # Fallback: /video responde multipart o jpeg.
            try:
                status, headers, body = _http_get(host, port, "/video", timeout)
                ctype = ""
                for key, value in headers.items():
                    if key.lower() == "content-type":
                        ctype = value.lower()
                        break
                if "multipart" in ctype or "jpeg" in ctype:
                    resolution = resolution or "?"
            except OSError:
                continue

        if resolution:
            found.append({
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}",
                "resolution": str(resolution) if resolution else None,
                "name": name or f"IP Webcam {host}",
                "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            })
    return found


def discover_ip_webcam(
    subnet: str | None = None,
    ports: list[int] | tuple[int, ...] = (8080, 8081),
    timeout: float = 0.8,
    max_workers: int = 64,
    hosts: list[str] | None = None,
    max_seconds: float = 6.0,
) -> list[dict]:
    """Busca camaras IP Webcam en la subred indicada.

    Devuelve la lista ordenada por latencia: ``[{"url", "resolution", ...}]``.
    Nunca lanza: en caso de error devuelve lista vacia.
    """
    if hosts is not None:
        targets = list(hosts)
    else:
        base = subnet or guess_subnet()
        base = base.rstrip(".")
        targets = [f"{base}.{i}" for i in range(1, 255)]

    ports = tuple(int(p) for p in ports) or (8080,)
    deadline = time.monotonic() + max(0.5, max_seconds)
    results: list[dict] = []
    workers = max(1, min(256, int(max_workers)))

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="disc")
    try:
        futures = {executor.submit(_probe_host, host, ports, timeout): host for host in targets}
        for future in as_completed(futures, timeout=max(0.5, max_seconds)):
            if time.monotonic() > deadline:
                break
            try:
                results.extend(future.result())
            except Exception:  # noqa: BLE001 - un host roto no rompe el barrido
                continue
    except Exception as exc:  # TimeoutError del as_completed, etc.
        log.info("Descubrimiento parcial: %s", exc)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda item: item.get("latency_ms", 9999))
    if results:
        log.info("Camaras encontradas: %s", ", ".join(r["url"] for r in results))
    return results


async def discover_ip_webcam_async(
    subnet: str | None = None,
    ports: list[int] | tuple[int, ...] = (8080, 8081),
    timeout: float = 0.8,
    max_workers: int = 64,
) -> list[dict]:
    """Version asincrona: corre el barrido bloqueante en un thread aparte."""
    return await asyncio.to_thread(
        discover_ip_webcam, subnet=subnet, ports=ports, timeout=timeout, max_workers=max_workers
    )


if __name__ == "__main__":  # pragma: no cover - utilidad de diagnostico
    logging.basicConfig(level=logging.INFO)
    print(f"IP local: {local_ip()} · subred: {guess_subnet()}")
    for cam in discover_ip_webcam():
        print(f"  {cam['url']}  {cam.get('resolution')}  {cam['latency_ms']} ms")
