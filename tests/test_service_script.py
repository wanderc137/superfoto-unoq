"""Pruebas estáticas y de humo del script de servicio (sin arrancar servidor)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "servicio.sh"


def run(*args, env=None, timeout=20):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_script_existe_y_es_ejecutable():
    assert SCRIPT.is_file(), f"falta {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "servicio.sh no es ejecutable"


def test_sintaxis_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_comandos_y_guardas_de_seguridad():
    text = SCRIPT.read_text(encoding="utf-8")
    for command in (
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "boot-enable",
        "boot-disable",
        "boot-status",
        "help",
    ):
        assert command in text, f"falta el comando {command}"
    assert "8000" in text and "detector-tarjetas" in text
    assert "superfoto.pid" in text
    assert "flock" in text
    assert "/api/health" in text
    assert "crontab" in text
    assert "9>&-" in text, "el hijo del servidor no debe heredar el fd del lock"
    assert "192.168.1" not in text, "no hardcodear IPs reales"


def test_help():
    result = run("help")
    assert result.returncode == 0
    assert "Uso:" in result.stdout
    assert "start" in result.stdout
    assert "boot-enable" in result.stdout
    assert "8000" in result.stdout


def test_status_con_data_dir_temporal(tmp_path):
    env = os.environ.copy()
    env["SUPERFOTO_DATA_DIR"] = str(tmp_path)
    result = run("status", env=env)
    assert "estado:" in result.stdout
    assert "detenido" in result.stdout or "corriendo" in result.stdout
    # Si está detenido, el contrato del script devuelve 3; si corre, 0.
    assert result.returncode in (0, 3)


def test_rechaza_puerto_8000(tmp_path):
    env = os.environ.copy()
    env["SUPERFOTO_DATA_DIR"] = str(tmp_path)
    env["SUPERFOTO_PORT"] = "8000"
    result = run("start", env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "8000" in output
    assert "detector-tarjetas" in output
    assert not (tmp_path / "superfoto.pid").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
