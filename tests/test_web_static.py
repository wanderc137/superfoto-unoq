"""Verificacion estatica de la UI: sin innerHTML dinamico ni recursos externos."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def test_web_has_no_dynamic_innerhtml():
    """I-008: nada de innerHTML con datos del servidor."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\.innerHTML\s*=", js)
    assert "insertAdjacentHTML" not in js
    assert "outerHTML" not in js
    # El historial se construye con createElement + textContent.
    assert "document.createElement" in js
    assert "textContent" in js


def test_web_has_no_external_resources():
    """Sin CDNs ni fuentes/hojas externas.

    El unico "http://" permitido en el HTML es el placeholder de ejemplo de la
    URL de IP Webcam (texto de ayuda, no se carga nada de ahi).
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)
    assert "<script src=\"/app.js\"" in html
    for name in ("app.js", "style.css"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert "http://" not in text, name
        assert "https://" not in text, name
        assert "cdn." not in text, name
        assert "@import" not in text, name


def test_index_references_local_assets():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'src="/app.js"' in html
    assert 'href="/style.css"' in html
    assert 'id="history"' in html
    assert 'id="actionMsg"' in html


def test_ui_has_prominent_ip_field_and_probe():
    """La IP/URL del telefono es editable y verificable antes de capturar."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")

    assert 'id="url"' in html
    assert "IP o URL del teléfono" in html
    assert 'placeholder="192.168.1.23:8080"' in html
    assert "192.168.1.23:8080/video" in html  # ayuda con ejemplos
    assert 'id="probeBtn"' in html and "Probar conexión" in html
    assert 'id="discoverBtn"' in html and "Buscar cámara" in html
    assert 'id="probeInfo"' in html
    assert 'id="autoHint"' in html and "se busca automáticamente" in html
    assert "ipBlock" in html and ".ipBlock" in css

    # Persistencia y llamada al endpoint.
    assert "superfoto.ipUrl" in js
    assert "localStorage.setItem" in js and "localStorage.getItem" in js
    assert "/api/probe?url=" in js
    # Validacion antes de iniciar con source=ip.
    assert "Escribí la IP del teléfono o usá Buscar cámara" in js
    # Preset de prueba real: 300 s, todos los frames, 1.5x, 55.
    assert "$(\"duration\").value = 300" in js
    assert "$(\"frames\").value = 0" in js
    assert "$(\"scale\").value = 1.5" in js
    assert "$(\"quality\").value = 55" in js
    # Sin innerHTML dinamico.
    assert not re.search(r"\.innerHTML\s*=", js)
