"""CLI de process_video: validacion de argumentos (I-007)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("superfoto_process_video",
                                                  ROOT / "scripts" / "process_video.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_process_video_rejects_negative_max_frames():
    module = _load_module()
    with pytest.raises(SystemExit) as exc:
        module.parse_args(["--input", "x.mp4", "--max-frames", "-5"])
    assert exc.value.code == 2


def test_process_video_rejects_negative_iterations():
    module = _load_module()
    with pytest.raises(SystemExit):
        module.parse_args(["--input", "x.mp4", "--rl-iterations", "-1"])
    with pytest.raises(SystemExit):
        module.parse_args(["--input", "x.mp4", "--refine-iterations", "-3"])


def test_process_video_accepts_zero_and_defaults():
    module = _load_module()
    args = module.parse_args(["--input", "x.mp4", "--max-frames", "0"])
    assert args.max_frames == 0
    assert module.non_negative_int("12") == 12
    with pytest.raises(Exception):
        module.non_negative_int("abc")
