"""The uv settings uv applies to a uv.lock and a script's lock (#812).

Each expectation was checked against uv 0.13.1 (`uv lock`, `uv sync -v`, `uv run -v`) with public packages."""

import json

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import DockerfileError, dockerfile_requirements
from wheelgap.sources import SourceError, load_inputs

_PYPI = "https://pypi.org/simple"
_FILES = "https://files.pythonhosted.org/packages"


def _pyproject(dependencies: list[str], tool: str = "", name: str = "demo") -> str:
    return (
        f'[project]\nname = "{name}"\nversion = "0.1"\nrequires-python = ">=3.12"\n'
        f"dependencies = {json.dumps(dependencies)}\n\n{tool}"
    )


def _results(capsys, path, *args: str) -> tuple[int, dict[str, str], dict[str, str]]:
    """The exit code, each checked package's linux-x86_64 status and each skipped package's reason."""
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json", *args])
    data = json.loads(capsys.readouterr().out)
    statuses = {p["name"]: p["results"]["linux-x86_64"] for p in data["packages"]}
    return code, statuses, {s["name"]: s["reason"] for s in data["skipped"]}


def _by_name(source) -> dict:
    return {p.name: p for p in source.packages}


# #812: a project uv.lock and the uv settings in a folder above it.


def _lock(tmp_path, folder) -> object:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(_pyproject(["fastlib==2.0.0"], name="proj"), encoding="utf-8")
    lock = folder / "uv.lock"
    lock.write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "proj"\nversion = "0.1"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "fastlib" }]\n\n'
        f'[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = {{ registry = "{_PYPI}" }}\n'
        f'sdist = {{ url = "{_FILES}/fastlib-2.0.0.tar.gz", hash = "sha256:00" }}\n'
        f'wheels = [{{ url = "{_FILES}/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:00" }}]\n',
        encoding="utf-8",
    )
    return lock


def test_uv_lock_applies_a_uv_toml_in_a_folder_above_from_the_issue(tmp_path, capsys, fake_pypi):
    # uv sync -v in proj/: "Found workspace configuration at repo/uv.toml", then it builds markupsafe from source.
    (tmp_path / ".git").mkdir()
    (tmp_path / "uv.toml").write_text('no-binary-package = ["fastlib"]\n', encoding="utf-8")
    lock = _lock(tmp_path, tmp_path / "proj")
    assert _results(capsys, lock)[1] == {"fastlib": "source"}
    main([str(lock), "-p", "3.13", "-t", "linux-x86_64", "--explain"])
    assert "install policy: ../uv.toml no-binary-package" in capsys.readouterr().out


def test_uv_lock_settings_above_a_repository_root_apply_too(tmp_path, capsys, fake_pypi):
    # uv 0.13.1 doesn't stop at a .git: repo/proj finds the uv.toml above repo.
    (tmp_path / "uv.toml").write_text("no-binary = true\n", encoding="utf-8")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert _results(capsys, _lock(tmp_path, tmp_path / "repo" / "proj"))[1] == {"fastlib": "source"}


@pytest.mark.parametrize(
    ("between", "expected"),
    [
        ('[tool.uv]\nexclude-newer = "2026-01-01T00:00:00Z"\n', "wheel"),  # the nearest settings: uv stops there
        ("[tool.ruff]\nline-length = 100\n", "source"),  # no [tool.uv]: uv skips it
        ("[project\n", "source"),  # uv warns that it can't parse it and keeps looking
    ],
)
def test_uv_lock_nearest_settings_win(tmp_path, capsys, fake_pypi, between, expected):
    (tmp_path / "uv.toml").write_text('no-binary-package = ["fastlib"]\n', encoding="utf-8")
    (tmp_path / "mid").mkdir()
    (tmp_path / "mid" / "pyproject.toml").write_text(between, encoding="utf-8")
    assert _results(capsys, _lock(tmp_path, tmp_path / "mid" / "proj"))[1] == {"fastlib": expected}


def test_uv_lock_unreadable_uv_toml_above_is_an_error(tmp_path):
    (tmp_path / "uv.toml").write_text("[broken\n", encoding="utf-8")  # uv sync stops with "Failed to parse"
    lock = _lock(tmp_path, tmp_path / "proj")
    with pytest.raises(SourceError, match=r"uv\.toml: invalid TOML"):
        load_inputs([str(lock)])


def test_dockerfile_uv_sync_refuses_settings_above_the_lock_the_image_doesnt_get(tmp_path):
    (tmp_path / "uv.toml").write_text("no-build = true\n", encoding="utf-8")
    app = tmp_path / "app"
    _lock(tmp_path, app)
    (app / "Dockerfile").write_text(
        "FROM python:3.13\nWORKDIR /app\nCOPY uv.lock pyproject.toml ./\nRUN uv sync --locked\n", encoding="utf-8"
    )
    with pytest.raises(DockerfileError, match=r":4: \.\./uv\.toml sets no-build in a folder above the lock"):
        dockerfile_requirements(app / "Dockerfile", uv_syncs={}, pythons=("3.13",))
