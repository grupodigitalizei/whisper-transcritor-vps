"""Unit tests for the pure/validation helpers in whisper-app.py.

We stub the heavy `whisper` import (torch) so the module loads fast and without
side effects — everything under test here is pure Python (validation, path math,
atomic writes) and doesn't need the ML stack.

Run:  ./venv/bin/python -m pytest tests/ -v
"""
import os
import sys
import json
import types
import importlib.util

import pytest

# ── Load whisper-app.py with `whisper` stubbed out ─────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_PATH = os.path.join(_HERE, "..", "whisper-app.py")

sys.modules.setdefault("whisper", types.ModuleType("whisper"))  # avoid importing torch

_spec = importlib.util.spec_from_file_location("whisper_app_under_test", _APP_PATH)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

HTTPException = app.HTTPException


# ── _validate_folder_name ──────────────────────────────────────────────────────
def test_folder_empty_is_root():
    assert app._validate_folder_name("") == ""
    assert app._validate_folder_name(None) == ""


def test_folder_valid_nested():
    assert app._validate_folder_name("Clientes/Adriana") == "Clientes/Adriana"


def test_folder_strips_surrounding_slashes():
    assert app._validate_folder_name("/A/B/") == "A/B"


@pytest.mark.parametrize("bad", ["..", "A/../B", "A\\B", "A//B", "x" * 61])
def test_folder_rejects_bad_paths(bad):
    with pytest.raises(HTTPException):
        app._validate_folder_name(bad)


# ── _ancestors_of ──────────────────────────────────────────────────────────────
def test_ancestors():
    assert app._ancestors_of("A/B/C") == ["A", "A/B", "A/B/C"]
    assert app._ancestors_of("") == []
    assert app._ancestors_of("Solo") == ["Solo"]


# ── _safe_filename ─────────────────────────────────────────────────────────────
def test_safe_filename_ok():
    assert app._safe_filename("abcd1234_video.mp4") == "abcd1234_video.mp4"


@pytest.mark.parametrize("bad", ["", "../etc/passwd", "a/b", "a\\b", "..", "."])
def test_safe_filename_rejects_traversal(bad):
    with pytest.raises(HTTPException):
        app._safe_filename(bad)


# ── _validate_media_url (SSRF guard) ───────────────────────────────────────────
@pytest.mark.parametrize("good", [
    "https://www.youtube.com/watch?v=abc",
    "http://example.com/video.mp4",
    "https://drive.google.com/file/d/x/view",
])
def test_url_accepts_public(good):
    assert app._validate_media_url(good) == good


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "http://localhost/x",
    "http://127.0.0.1/x",
    "http://192.168.0.10/x",
    "http://10.0.0.5/x",
    "http://169.254.1.1/x",
    "not a url",
])
def test_url_rejects_internal_and_bad_scheme(bad):
    with pytest.raises(HTTPException):
        app._validate_media_url(bad)


# ── _host_allows_cookies (cookie leak guard) ───────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=x", True),
    ("https://youtu.be/x", True),
    ("https://rr3---sn-abc.googlevideo.com/x", True),
    ("https://evil.com/x", False),
    ("https://youtube.com.evil.com/x", False),  # suffix-spoof must NOT match
    ("https://notyoutube.com/x", False),
])
def test_cookie_allowlist(url, expected):
    assert app._host_allows_cookies(url) is expected


# ── _atomic_write_json ─────────────────────────────────────────────────────────
def test_atomic_write_roundtrip(tmp_path):
    p = str(tmp_path / "data.json")
    payload = {"a": 1, "b": ["x", "y"], "acentuação": "ç"}
    app._atomic_write_json(p, payload)
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == payload
    # No leftover temp files in the directory
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".tmp_")]
    assert leftovers == []


def test_atomic_write_overwrites(tmp_path):
    p = str(tmp_path / "data.json")
    app._atomic_write_json(p, {"v": 1})
    app._atomic_write_json(p, {"v": 2})
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["v"] == 2


# ── _build_markdown_text (Markdown export) ─────────────────────────────────────
def test_markdown_includes_title_and_body():
    md = app._build_markdown_text(name="Minha Aula", text="Olá mundo.",
                                   lang="pt", duration="1m 20s", model="turbo",
                                   date="08 de Jul. de 2026, 02:44")
    assert md.startswith("# Minha Aula")
    assert "Olá mundo." in md
    assert "**Duração:** 1m 20s" in md
    assert "**Idioma:** pt" in md
    assert "**Modelo:** turbo" in md
    assert "**Transcrito em:** 08 de Jul. de 2026, 02:44" in md


def test_markdown_omits_unknown_metadata():
    md = app._build_markdown_text(name="Sem Metadados", text="Texto.")
    assert "**Duração:**" not in md
    assert "**Idioma:**" not in md
    assert "**Modelo:**" not in md
    assert "**Transcrito em:**" not in md
    assert "# Sem Metadados" in md
    assert "Texto." in md


# ── _validate_display_name (rename) ────────────────────────────────────────────
def test_display_name_trims_and_accepts():
    assert app._validate_display_name("  Minha Aula  ") == "Minha Aula"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "x" * 151])
def test_display_name_rejects_bad_input(bad):
    with pytest.raises(HTTPException):
        app._validate_display_name(bad)


# ── _media_type_for (Biblioteca de Mídia — filtro por tipo) ────────────────────
@pytest.mark.parametrize("filename,expected", [
    ("abc123_aula.mp4", "video"),
    ("abc123_aula.MOV", "video"),
    ("abc123_aula.mp3", "audio"),
    ("abc123_aula.WAV", "audio"),
    ("abc123_aula.pdf", "other"),
    ("abc123_aula", "other"),
])
def test_media_type_for(filename, expected):
    assert app._media_type_for(filename) == expected
