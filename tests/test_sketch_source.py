"""Verificacion estatica del sketch MCU (el compile real va en la placa)."""

from __future__ import annotations

import re
from pathlib import Path

SKETCH = Path(__file__).resolve().parent.parent / "sketch" / "superfoto_mcu" / "superfoto_mcu.ino"


def test_sketch_exists_and_uses_led_matrix_only():
    text = SKETCH.read_text(encoding="utf-8")
    assert '#include "Arduino_LED_Matrix.h"' in text
    # ArduinoGraphics no esta instalada en la placa: no debe incluirse
    # (la mencion en un comentario esta permitida).
    assert not re.search(r'#include\s*[<"]ArduinoGraphics', text)
    assert "matrix.begin()" in text
    assert "setGrayscaleBits" in text
    assert "matrix.draw(canvas)" in text
    assert "delay(33)" in text  # loop no bloqueante a ~30 fps


def test_sketch_has_four_distinct_states():
    text = SKETCH.read_text(encoding="utf-8")
    for fn in ("renderIdle", "renderCapture", "renderProcess", "renderDone"):
        assert f"static void {fn}" in text, fn
    assert "MODE_IDLE" in text and "MODE_CAPTURE" in text
    assert "MODE_PROCESS" in text and "MODE_DONE" in text
    # Heartbeat presente y comandos por Serial.
    assert "heartbeat(" in text
    for cmd in ("'I'", "'C'", "'P'", "'D'"):
        assert cmd in text
    # Sin acentos en los mensajes de consola.
    assert text.isascii()
