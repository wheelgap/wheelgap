"""Failed pip/uv wheel build logs become explainable package checks."""

import json

import pytest

from wheelgap.cli import main
from wheelgap.install_log import failed_builds
from wheelgap.sources import SourceError


@pytest.mark.parametrize(
    "log,expected,unpinned",
    [
        (
            "Collecting nowheels==0.9\nBuilding wheel for nowheels (pyproject.toml)\n"
            "ERROR: Failed building wheel for nowheels\n",
            ["nowheels==0.9"],
            [],
        ),
        ("error: Failed to build `nowheels==0.9`\n", ["nowheels==0.9"], []),
        ("Building wheel for nowheels (pyproject.toml)\nFailed to build nowheels\n", ["nowheels"], ["nowheels"]),
        ("Collecting nowheels==0.9\nFailed to build nowheels, fastlib\n", ["nowheels==0.9", "fastlib"], ["fastlib"]),
    ],
)
def test_pip_and_uv_failure_recovery(log, expected, unpinned):
    assert failed_builds(log) == (expected, unpinned)


@pytest.mark.parametrize(
    "log,fragment",
    [
        ("successfully installed nowheels\n", "no supported"),
        ("ERROR: Failed building wheel for ../unsafe\n", "cannot read"),
        ("Collecting nowheels==0.9\nCollecting nowheels==1.0\nFailed to build nowheels\n", "ambiguous"),
        ("Failed to build nowheels==0.9\nFailed to build nowheels==1.0\n", "conflicting"),
    ],
)
def test_bad_or_ambiguous_failure_refuses_to_guess(log, fragment):
    with pytest.raises(SourceError, match=fragment):
        failed_builds(log)


def test_failed_install_log_checks_pinned_package_with_explanation(tmp_path, fake_pypi, capsys):
    path = tmp_path / "failed.txt"
    path.write_text(
        "Collecting nowheels==0.9\nBuilding wheel for nowheels (pyproject.toml)\n"
        "ERROR: Failed building wheel for nowheels\n",
        encoding="utf-8",
    )
    code = main(["--from-log", str(path), "-p", "3.13", "-t", "win-arm64", "--json", "--no-cache"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    pkg = next(p for p in data["packages"] if p["name"] == "nowheels")
    assert pkg["version"] == "0.9"
    assert pkg["results"]["win-arm64"] in ("source", "missing")
    assert "rejected" in pkg and "suggest" in pkg
    assert fake_pypi


def test_unpinned_failure_note_is_stderr_only(tmp_path, fake_pypi, capsys):
    path = tmp_path / "failed.txt"
    path.write_text("Failed to build nowheels\n", encoding="utf-8")
    main(["--from-log", str(path), "--json", "-p", "3.13", "-t", "win-arm64"])
    out = capsys.readouterr()
    assert "checking the newest available release" in out.err
    json.loads(out.out)


def test_install_log_invalid_args_and_oversize(tmp_path, capsys):
    path = tmp_path / "failed.txt"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    assert main(["--from-log", str(path), "--json"]) == 2
    assert "exceeds" in capsys.readouterr().err
