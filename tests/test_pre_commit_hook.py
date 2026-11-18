"""Tests for the pre-commit hook configuration in .pre-commit-hooks.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HOOK_CONFIG = Path(__file__).resolve().parent.parent / ".pre-commit-hooks.yaml"


def _files_pattern() -> re.Pattern[str]:
    text = HOOK_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*files:\s*(.+?)\s*$", text, re.MULTILINE)
    assert match is not None, "files: pattern not found in .pre-commit-hooks.yaml"
    return re.compile(match.group(1))


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "sub/uv.lock",
        "poetry.lock",
        "pdm.lock",
        "pylock.toml",
        "Pipfile.lock",
        "pixi.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.lock",
        "pyproject.toml",
        "sub/pyproject.toml",
        "uv.toml",
        "sub/uv.toml",
        ".python-version",
        "sub/.python-version",
    ],
)
def test_pre_commit_hook_matches_dependency_and_config_files(path: str):
    assert _files_pattern().search(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "setup.py",
        "src/app.py",
        "example.py.lock.bak",
        # The hook runs `wheelgap .`, which never picks a script lock, so changing one shouldn't run it.
        "example.py.lock",
        "scripts/tool.py.lock",
        "my-pyproject.toml",
        "my-uv.toml",
        "foo.python-version",
        "pyproject.toml.bak",
        "sub/requirements.in",
        "setup.cfg",
        "tox.ini",
    ],
)
def test_pre_commit_hook_ignores_unrelated_files(path: str):
    assert _files_pattern().search(path) is None
