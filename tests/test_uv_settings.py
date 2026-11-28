"""The uv settings and [tool.uv.sources] uv applies to a pyproject.toml, a script and a uv.lock (#810, #811, #812).

Each expectation was checked against uv 0.13.1 (`uv lock`, `uv sync -v`, `uv run -v`) with public packages."""

import json

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import DockerfileError, dockerfile_requirements
from wheelgap.sources import SourceError, load_inputs, select_pyproject_extras, select_pyproject_groups

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


# #811: [tool.uv] no-binary / no-build settings for pyproject.toml and script inputs.


def test_pyproject_no_binary_package_from_the_issue_builds_from_source(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], '[tool.uv]\nno-binary-package = ["fastlib"]\n'), encoding="utf-8"
    )
    assert _results(capsys, tmp_path / "pyproject.toml")[:2] == (1, {"fastlib": "source"})
    main([str(tmp_path / "pyproject.toml"), "-p", "3.13", "-t", "linux-x86_64", "--explain"])
    assert "install policy: [tool.uv] no-binary-package" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("no-build = true", {"fastlib": "wheel", "nowheels": "missing"}),
        ("no-binary = true", {"fastlib": "source", "nowheels": "source"}),
        ('no-build-package = ["nowheels"]', {"fastlib": "wheel", "nowheels": "missing"}),
    ],
)
def test_pyproject_build_restrictions(tmp_path, capsys, fake_pypi, setting, expected):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0", "nowheels==0.9"], f"[tool.uv]\n{setting}\n"), encoding="utf-8"
    )
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == expected


def test_pyproject_uv_toml_beside_it_replaces_its_tool_uv(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0", "nowheels==0.9"], "[tool.uv]\nno-binary = true\n"), encoding="utf-8"
    )
    (tmp_path / "uv.toml").write_text('no-build-package = ["nowheels"]\n', encoding="utf-8")
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == {"fastlib": "wheel", "nowheels": "missing"}


def test_pyproject_build_restrictions_apply_to_selected_extras(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            [],
            '[project.optional-dependencies]\nfast = ["fastlib==2.0.0"]\n\n'
            '[tool.uv]\nno-binary-package = ["fastlib"]\n',
        ),
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "pyproject.toml", "--extra", "fast")[1] == {"fastlib": "source"}


def test_workspace_member_gets_the_roots_build_restrictions_not_its_own(tmp_path, capsys, fake_pypi):
    # uv sync --package a builds from the root's settings and ignores the member's own no-binary-package.
    (tmp_path / "pyproject.toml").write_text(
        _pyproject([], '[tool.uv.workspace]\nmembers = ["a"]\n\n[tool.uv]\nno-build-package = ["nowheels"]\n', "root"),
        encoding="utf-8",
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0", "nowheels==0.9"], '[tool.uv]\nno-binary-package = ["fastlib"]\n', "a"),
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "a" / "pyproject.toml")[1] == {"fastlib": "wheel", "nowheels": "missing"}
    main([str(tmp_path / "a" / "pyproject.toml"), "-p", "3.13", "-t", "linux-x86_64", "--explain"])
    assert "install policy: ../pyproject.toml [tool.uv] no-build-package" in capsys.readouterr().out


def test_unsure_workspace_membership_with_build_restrictions_is_refused(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject([], '[tool.uv.workspace]\nmembers = ["**/a"]\n\n[tool.uv]\nno-binary = true\n', "root"),
        encoding="utf-8",
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], "[tool.uv]\nno-build = false\n", "a"), encoding="utf-8"
    )
    with pytest.raises(SourceError, match="can't tell whether this is a member of the workspace"):
        load_inputs([str(tmp_path / "a" / "pyproject.toml")])


def test_script_block_build_restrictions_from_the_issue(tmp_path, capsys, fake_pypi):
    (tmp_path / "demo.py").write_text(
        '# /// script\n# dependencies = ["fastlib==2.0.0"]\n# [tool.uv]\n# no-binary-package = ["fastlib"]\n'
        '# ///\nprint("hi")\n',
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "demo.py")[1] == {"fastlib": "source"}


def test_script_joins_its_block_with_a_uv_toml_above(tmp_path, capsys, fake_pypi):
    # uv run (from any folder) applies the nearest settings from the script's folder up, joined with its block's.
    (tmp_path / "uv.toml").write_text('no-binary-package = ["fastlib"]\n', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "demo.py").write_text(
        '# /// script\n# dependencies = ["fastlib==2.0.0", "nowheels==0.9"]\n# [tool.uv]\n'
        '# no-build-package = ["nowheels"]\n# ///\n',
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "scripts" / "demo.py")[1] == {"fastlib": "source", "nowheels": "missing"}


@pytest.mark.parametrize("name", ["pyproject.toml", "demo.py"])
def test_deps_refuses_build_restrictions_it_cant_apply_to_dependencies(tmp_path, capsys, fake_pypi, name):
    if name == "demo.py":
        text = '# /// script\n# dependencies = ["fastlib"]\n# [tool.uv]\n# no-build = true\n# ///\n'
    else:
        text = _pyproject(["fastlib"], "[tool.uv]\nno-build = true\n")
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert main([str(tmp_path / name), "-p", "3.13", "-t", "linux-x86_64", "--deps"]) == 2
    assert "[tool.uv] no-build, which uv applies to the dependencies it resolves too" in capsys.readouterr().err


# #810: [tool.uv.sources] for pyproject.toml and script inputs.


def test_pyproject_sources_from_the_issue_are_not_checked_on_pypi(tmp_path, capsys, fake_pypi):
    # uv builds markupsafe from git and takes torch from download.pytorch.org: PyPI's files answer for neither.
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            ["markupsafe", "torch==2.5.1"],
            '[tool.uv.sources]\nmarkupsafe = { git = "https://github.com/pallets/markupsafe", tag = "3.0.2" }\n'
            'torch = { index = "pytorch-cpu" }\n\n'
            '[[tool.uv.index]]\nname = "pytorch-cpu"\nurl = "https://download.pytorch.org/whl/cpu"\nexplicit = true\n',
        ),
        encoding="utf-8",
    )
    code, statuses, skipped = _results(capsys, tmp_path / "pyproject.toml")
    assert statuses == {}
    assert skipped == {"markupsafe": "git", "torch": "private index"}
    assert fake_pypi == []
    assert code == 0


@pytest.mark.parametrize(
    ("source", "skip"),
    [
        ('{ git = "https://github.com/benjaminp/six", tag = "1.17.0" }', "git"),
        ('{ url = "https://files.example/fastlib-2.0.0.tar.gz" }', "url"),
        ('{ path = "../fastlib" }', "path"),
        ('{ path = "../fastlib", editable = true }', "editable"),
        ("{ workspace = true }", "workspace"),
    ],
)
def test_pyproject_source_outside_an_index_is_skipped(tmp_path, source, skip):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], f"[tool.uv.sources]\nfastlib = {source}\n"), encoding="utf-8"
    )
    (source_,) = load_inputs([str(tmp_path / "pyproject.toml")])
    assert _by_name(source_)["fastlib"].skip == skip


def test_pyproject_index_source_on_pypi_is_still_looked_up(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            ["fastlib==2.0.0"],
            f'[tool.uv.sources]\nfastlib = {{ index = "pypi" }}\n\n[[tool.uv.index]]\nname = "pypi"\nurl = "{_PYPI}"\n',
        ),
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == {"fastlib": "wheel"}


@pytest.mark.parametrize(
    "source",
    [
        # uv takes the git source on Linux and PyPI's release elsewhere (uv lock forks on the marker).
        '{ git = "https://github.com/kjd/idna", tag = "v3.10", marker = "sys_platform == \'linux\'" }',
        '[{ index = "a", marker = "sys_platform == \'linux\'" },\n'
        ' { index = "b", marker = "sys_platform != \'linux\'" }]',
        '{ index = "a", extra = "cpu" }',
        # Declared nowhere wheelgap reads: uv refuses it, unless the user's own uv.toml has it.
        '{ index = "undeclared" }',
    ],
)
def test_pyproject_source_wheelgap_cant_follow_is_unknown(tmp_path, capsys, fake_pypi, source):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            ["fastlib==2.0.0"],
            f'[tool.uv.sources]\nfastlib = {source}\n\n[[tool.uv.index]]\nname = "a"\nurl = "https://a.example/simple"\n'
            '[[tool.uv.index]]\nname = "b"\nurl = "https://b.example/simple"\n',
        ),
        encoding="utf-8",
    )
    code, statuses, _ = _results(capsys, tmp_path / "pyproject.toml")
    assert statuses == {"fastlib": "unknown"}
    assert fake_pypi == []


@pytest.mark.parametrize(
    "source", ['"git"', '{ git = "https://g.example/x", path = "x" }', "{ tag = 1 }", "{ workspace = false }"]
)
def test_pyproject_source_uv_refuses_is_an_error(tmp_path, source):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], f"[tool.uv.sources]\nfastlib = {source}\n"), encoding="utf-8"
    )
    with pytest.raises(SourceError, match=r"\[tool\.uv\.sources\] fastlib"):
        load_inputs([str(tmp_path / "pyproject.toml")])


@pytest.mark.parametrize(
    ("config", "where"), [("[tool.uv]\nno-sources = true\n", "pyproject.toml"), ("no-sources = true\n", "uv.toml")]
)
def test_no_sources_turns_the_sources_off(tmp_path, capsys, fake_pypi, config, where):
    # uv lock with no-sources takes the package from PyPI, though [tool.uv.sources] names git.
    tool = '[tool.uv.sources]\nfastlib = { git = "https://g.example/fastlib" }\n'
    if where == "uv.toml":
        (tmp_path / "uv.toml").write_text(config, encoding="utf-8")
    else:
        tool = config + "\n" + tool
    (tmp_path / "pyproject.toml").write_text(_pyproject(["fastlib==2.0.0"], tool), encoding="utf-8")
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == {"fastlib": "wheel"}


def test_sources_apply_to_selected_extras_and_groups(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            [],
            '[project.optional-dependencies]\nfast = ["fastlib==2.0.0"]\n\n'
            '[dependency-groups]\ndev = ["purewheel==3.1"]\n\n'
            '[tool.uv.sources]\nfastlib = { git = "https://g.example/fastlib" }\npurewheel = { path = "../pw" }\n',
        ),
        encoding="utf-8",
    )
    (source,) = load_inputs([str(tmp_path / "pyproject.toml")])
    select_pyproject_extras(source, ["fast"], all_extras=False)
    select_pyproject_groups(source, ["dev"], [])
    assert {name: p.skip for name, p in _by_name(source).items()} == {"fastlib": "git", "purewheel": "path"}


def test_workspace_member_gets_the_roots_sources_under_its_own(tmp_path):
    # uv lock at the root: six comes from the root's git source, idna from the member's own index (uv 0.13.1).
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            ["a"],
            '[tool.uv.workspace]\nmembers = ["pkgs/*"]\n\n[tool.uv.sources]\na = { workspace = true }\n'
            'six = { git = "https://github.com/benjaminp/six", tag = "1.17.0" }\n'
            'fastlib = { git = "https://g.example/fastlib" }\n',
            name="root",
        ),
        encoding="utf-8",
    )
    member = tmp_path / "pkgs" / "a"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        _pyproject(
            ["six", "idna", "fastlib==2.0.0"],
            '[tool.uv.sources]\nidna = { index = "mine" }\nfastlib = { index = "mine" }\n\n'
            '[[tool.uv.index]]\nname = "mine"\nurl = "https://test.pypi.org/simple"\n',
            name="a",
        ),
        encoding="utf-8",
    )
    packages = _by_name(load_inputs([str(member / "pyproject.toml")])[0])
    assert packages["six"].skip == "git"
    assert (packages["idna"].skip, packages["idna"].registry) == (None, "https://test.pypi.org/simple")
    assert packages["fastlib"].registry == "https://test.pypi.org/simple"  # the member's own source wins


def test_script_sources_are_read_from_its_block(tmp_path, capsys, fake_pypi):
    (tmp_path / "demo.py").write_text(
        '# /// script\n# dependencies = ["fastlib==2.0.0", "purewheel==3.1"]\n#\n# [tool.uv.sources]\n'
        '# fastlib = { git = "https://g.example/fastlib" }\n# ///\nprint("hi")\n',
        encoding="utf-8",
    )
    code, statuses, skipped = _results(capsys, tmp_path / "demo.py")
    assert (statuses, skipped) == ({"purewheel": "wheel"}, {"fastlib": "git"})


_CPU = "https://download.pytorch.org/whl/cpu"


@pytest.mark.parametrize(
    ("tool", "where", "expected", "skipped"),
    [
        # uv lock (0.13.1) takes markupsafe from that registry in all of these; with default or index-url it never
        # asks PyPI. So nothing goes to PyPI: the package is unknown, or skipped as a private index.
        (f'[[tool.uv.index]]\nname = "pytorch-cpu"\nurl = "{_CPU}"\n', "pyproject.toml", "unknown", None),
        (
            f'[[tool.uv.index]]\nname = "pytorch-cpu"\nurl = "{_CPU}"\ndefault = true\n',
            "pyproject.toml",
            None,
            "private index",
        ),
        (f'[tool.uv]\nindex-url = "{_CPU}"\n', "pyproject.toml", None, "private index"),
        (f'[tool.uv]\nextra-index-url = ["{_CPU}"]\n', "pyproject.toml", "unknown", None),
        (f'default-index = "{_CPU}"\n', "uv.toml", None, "private index"),
        ('find-links = ["./wheels"]\n', "uv.toml", "unknown", None),
        ("no-index = true\n", "uv.toml", "unknown", None),
    ],
)
def test_pyproject_index_settings_keep_packages_off_pypi(tmp_path, capsys, fake_pypi, tool, where, expected, skipped):
    if where == "uv.toml":
        (tmp_path / "uv.toml").write_text(tool, encoding="utf-8")
        tool = ""
    (tmp_path / "pyproject.toml").write_text(_pyproject(["fastlib==2.0.0"], tool), encoding="utf-8")
    _, statuses, skips = _results(capsys, tmp_path / "pyproject.toml")
    assert statuses == ({} if expected is None else {"fastlib": expected})
    assert skips == ({} if skipped is None else {"fastlib": skipped})
    assert fake_pypi == []


def test_extra_index_note_names_the_index_without_its_credentials(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], '[tool.uv]\nextra-index-url = ["https://me:s3cret@corp.example/simple"]\n'),
        encoding="utf-8",
    )
    main([str(tmp_path / "pyproject.toml"), "-p", "3.13", "-t", "linux-x86_64", "--explain"])
    captured = capsys.readouterr()
    assert "names the index https://corp.example/simple, which uv may take fastlib from" in captured.out + captured.err
    assert "s3cret" not in captured.out + captured.err


@pytest.mark.parametrize(
    "tool",
    [
        '[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n',
        f'[[tool.uv.index]]\nname = "cpu"\nurl = "{_CPU}"\nexplicit = true\n',  # only for sources that name it
    ],
)
def test_pyproject_index_settings_that_leave_pypi_the_index(tmp_path, capsys, fake_pypi, tool):
    (tmp_path / "pyproject.toml").write_text(_pyproject(["fastlib==2.0.0"], tool), encoding="utf-8")
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == {"fastlib": "wheel"}


def test_script_block_index_url_keeps_its_packages_off_pypi(tmp_path, capsys, fake_pypi):
    (tmp_path / "demo.py").write_text(
        f'# /// script\n# dependencies = ["fastlib==2.0.0"]\n# [tool.uv]\n# index-url = "{_CPU}"\n# ///\n',
        encoding="utf-8",
    )
    assert _results(capsys, tmp_path / "demo.py")[2] == {"fastlib": "private index"}
    assert fake_pypi == []


def test_source_index_defined_only_in_uv_toml_is_unknown(tmp_path, capsys, fake_pypi):
    # uv lock: "Index `cpu` was found in a project-level `uv.toml`, but indexes referenced via `tool.uv.sources` must
    # be defined in the project's `pyproject.toml`".
    (tmp_path / "uv.toml").write_text(f'[[index]]\nname = "cpu"\nurl = "{_CPU}"\nexplicit = true\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0"], '[tool.uv.sources]\nfastlib = { index = "cpu" }\n'), encoding="utf-8"
    )
    (source,) = load_inputs([str(tmp_path / "pyproject.toml")])
    assert "no [[tool.uv.index]] of the project defines" in (_by_name(source)["fastlib"].error or "")


def test_sources_naming_one_package_twice_are_an_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(
            ["fastlib==2.0.0"],
            '[tool.uv.sources]\nfastlib = { git = "https://g.example/a" }\nFastLib = { path = "../b" }\n',
        ),
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match="names 'fastlib' twice once normalized"):
        load_inputs([str(tmp_path / "pyproject.toml")])


# How uv combines a blanket setting with the other kind's package list (BuildOptions.no_build_package and
# no_binary_package), each checked with uv 0.13.1 sync or run on six and markupsafe.
_PRECEDENCE = [
    # six's wheel installs: no-build-package exempts it from no-binary.
    ('no-binary = true\nno-build-package = ["fastlib"]', {"fastlib": "wheel", "nowheels": "source"}),
    # markupsafe builds: no-binary-package exempts it from no-build.
    ('no-build = true\nno-binary-package = ["fastlib"]', {"fastlib": "source", "nowheels": "missing"}),
    # markupsafe's wheel installs: an explicit `no-binary = false` turns no-binary-package off.
    ('no-binary = false\nno-binary-package = ["fastlib"]', {"fastlib": "wheel", "nowheels": "source"}),
]


@pytest.mark.parametrize(("settings", "expected"), _PRECEDENCE)
def test_pyproject_package_lists_override_blanket_settings(tmp_path, capsys, fake_pypi, settings, expected):
    (tmp_path / "pyproject.toml").write_text(
        _pyproject(["fastlib==2.0.0", "nowheels==0.9"], f"[tool.uv]\n{settings}\n"), encoding="utf-8"
    )
    assert _results(capsys, tmp_path / "pyproject.toml")[1] == expected


@pytest.mark.parametrize(("settings", "expected"), _PRECEDENCE)
def test_uv_lock_package_lists_override_blanket_settings(tmp_path, capsys, fake_pypi, settings, expected):
    (tmp_path / "uv.toml").write_text(settings + "\n", encoding="utf-8")
    assert _results(capsys, _lock(tmp_path, tmp_path / "proj"))[1] == {"fastlib": expected["fastlib"]}


@pytest.mark.parametrize(
    ("block", "above", "expected"),
    [
        # uv run: the block's no-binary, the uv.toml's no-build-package for six, which installs its wheel.
        ("no-binary = true", 'no-build-package = ["fastlib"]', "wheel"),
        # The block's `no-binary = false` turns the uv.toml's no-binary-package off: six's wheel installs.
        ("no-binary = false", 'no-binary-package = ["fastlib"]', "wheel"),
    ],
)
def test_script_block_and_settings_above_combine_as_uv_run_does(tmp_path, capsys, fake_pypi, block, above, expected):
    (tmp_path / "uv.toml").write_text(above + "\n", encoding="utf-8")
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "demo.py").write_text(
        f'# /// script\n# dependencies = ["fastlib==2.0.0"]\n# [tool.uv]\n# {block}\n# ///\n', encoding="utf-8"
    )
    assert _results(capsys, tmp_path / "s" / "demo.py")[1] == {"fastlib": expected}


@pytest.mark.parametrize("input_name", ["uv.lock", "pyproject.toml"])
@pytest.mark.parametrize("git_between", [False, True])
def test_unparseable_pyproject_above_a_project_is_refused(tmp_path, input_name, git_between):
    # uv sync and uv lock (0.13.1) in proj/ stop with "Failed to parse: mid/pyproject.toml" while looking for the
    # workspace, past a .git too; settings discovery alone only warns.
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    lock = _lock(tmp_path, tmp_path / "proj")
    if git_between:
        (tmp_path / "proj" / ".git").mkdir()
    with pytest.raises(SourceError, match=r"pyproject\.toml can't be parsed .*looks for the project's workspace there"):
        load_inputs([str(lock if input_name == "uv.lock" else lock.with_name("pyproject.toml"))])


@pytest.mark.parametrize("input_name", ["uv.lock", "pyproject.toml"])
def test_unparseable_pyproject_above_the_first_one_uv_parses_is_ignored(tmp_path, capsys, fake_pypi, input_name):
    # uv's workspace discovery stops at the first pyproject.toml above that parses, whatever it holds.
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (tmp_path / "mid").mkdir()
    (tmp_path / "mid" / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    lock = _lock(tmp_path, tmp_path / "mid" / "proj")
    path = lock if input_name == "uv.lock" else lock.with_name("pyproject.toml")
    assert _results(capsys, path)[1] == {"fastlib": "wheel"}


def test_unparseable_pyproject_above_a_script_only_warns_in_uv(tmp_path, capsys, fake_pypi):
    # uv run warns about it during settings discovery and runs the script.
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "demo.py").write_text(
        '# /// script\n# dependencies = ["fastlib==2.0.0"]\n# ///\n', encoding="utf-8"
    )
    assert _results(capsys, tmp_path / "s" / "demo.py")[1] == {"fastlib": "wheel"}
