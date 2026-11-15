import json
import shutil
import tomllib

import pytest

from wheelgap import check as check_module
from wheelgap.check import check, marker_holds, poetry_extras_decide, poetry_marker_holds
from wheelgap.cli import main
from wheelgap.sources import (
    SourceError,
    UvSelection,
    _poetry_extras_declared_elsewhere,
    load_inputs,
    select_uv_dependencies,
)
from wheelgap.targets import build_targets


def installed(fixtures, selection=None, platform="linux-x86_64"):
    sources = load_inputs([str(fixtures / "uvselect")])
    if selection is not None:
        select_uv_dependencies(sources, selection)
    return {r.package.name for r in check(sources, build_targets(["3.13"], [platform])) if r.needed}


def test_unselected_graph_stays_conservative(fixtures):
    assert installed(fixtures) == {
        "common",
        "library",
        "librarybase",
        "one",
        "two",
        "three",
        "unused",
        "api",
        "gpu",
        "devtool",
        "docstool",
    }


def test_no_dev_checks_runtime_without_unrequested_optional_packages(fixtures):
    assert installed(fixtures, UvSelection(no_dev=True)) == {"common", "library", "librarybase", "one"}


def test_extra_activates_transitive_extras_and_revisits_a_cycle(fixtures):
    expected = {"common", "library", "librarybase", "one", "two", "three", "api"}
    selection = UvSelection(extras=frozenset({"api"}), no_dev=True)
    assert installed(fixtures, selection) == expected
    assert installed(fixtures, selection, "win-amd64") == expected | {"winonly"}


def test_explicit_extra_keeps_default_dev_group(fixtures):
    assert "devtool" in installed(fixtures, UvSelection(extras=frozenset({"api"})))
    assert "docstool" not in installed(fixtures, UvSelection(extras=frozenset({"api"})))


def test_all_extras_does_not_activate_unrequested_dependency_extras(fixtures):
    selected = installed(fixtures, UvSelection(all_extras=True))
    assert {"api", "gpu", "devtool"} <= selected
    assert not {"unused", "docstool"} & selected


def test_group_and_only_group(fixtures):
    assert {"devtool", "docstool", "common"} <= installed(fixtures, UvSelection(groups=frozenset({"docs"})))
    assert installed(fixtures, UvSelection(only_groups=frozenset({"docs"}))) == {"docstool"}
    assert installed(fixtures, UvSelection(only_groups=frozenset({"docs", "dev"}))) == {"docstool", "devtool"}


def test_extra_markers_evaluate_whole_expression_per_extra():
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    assert marker_holds("extra == 'two' and extra != 'one'", target, extras=frozenset({"one", "two"}))
    assert not marker_holds("extra == 'one' and extra == 'two'", target, extras=frozenset({"one", "two"}))
    assert not marker_holds("'gpu' in extras", target, extras=frozenset({"api"}))
    assert marker_holds("extra == 'extra-7-app-gpu'", target, extras=frozenset({"api"}))
    assert marker_holds("extra == 'group-7-app-docs'", target, extras=frozenset({"api"}))


@pytest.mark.parametrize(
    "selection, message",
    [
        (UvSelection(extras=frozenset({"typo"})), "unknown extras"),
        (UvSelection(groups=frozenset({"typo"})), "unknown groups"),
        (UvSelection(only_groups=frozenset({"dev"}), no_dev=True), "explicit dev"),
        (UvSelection(only_groups=frozenset({"docs"}), all_extras=True), "cannot be combined"),
    ],
)
def test_invalid_selection_is_an_error(fixtures, selection, message):
    with pytest.raises(SourceError, match=message):
        select_uv_dependencies(load_inputs([str(fixtures / "uvselect")]), selection)


def test_cli_production_selection_is_offline_and_has_no_false_gaps(fixtures, capsys, fake_pypi):
    assert main([str(fixtures / "uvselect"), "--extra", "api", "--no-dev", "-p", "3.13", "-t", "linux", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {p["name"] for p in report["packages"]} == {"common", "api", "library", "librarybase", "one", "two", "three"}
    assert fake_pypi == []


def test_config_selection_and_cli_override(fixtures, tmp_path, capsys, fake_pypi):
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nextras = ["gpu"]\ngroups = ["docs"]\n')
    assert main([str(tmp_path), "--extra", "api", "--only-group", "docs"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert main([str(tmp_path), "--extra", "api", "--group", "dev", "--json", "-t", "linux", "-p", "3.13"]) == 0
    names = {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert "api" in names and "gpu" not in names and "docstool" not in names
    assert fake_pypi == []


def test_non_uv_input_rejects_selection(tmp_path, capsys, fake_pypi):
    path = tmp_path / "requirements.txt"
    path.write_text("purewheel==3.1\n")
    assert main([str(path), "--no-dev"]) == 2
    assert "requires uv.lock" in capsys.readouterr().err
    assert fake_pypi == []


def test_empty_extras_and_groups_recorded_only_in_uv_metadata(tmp_path):
    (tmp_path / "uv.lock").write_text("""
version = 1
[[package]]
name = "app"
source = { virtual = "." }
[package.metadata]
provides-extras = ["empty"]
[package.metadata.requires-dev]
dev = []
docs = []
""")
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(extras=frozenset({"empty"}), groups=frozenset({"docs"})))
    assert check(sources, build_targets(["3.13"], ["linux"])) == []


@pytest.mark.parametrize(
    "defaults,expected",
    [
        ('["docs"]', {"common", "library", "librarybase", "one", "docstool"}),
        ('"all"', {"common", "library", "librarybase", "one", "docstool", "devtool"}),
        ("[]", {"common", "library", "librarybase", "one"}),
    ],
)
def test_uv_default_groups(fixtures, tmp_path, defaults, expected):
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path)
    (tmp_path / "pyproject.toml").write_text(f"[tool.uv]\ndefault-groups = {defaults}\n")
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection())
    assert {r.package.name for r in check(sources, build_targets(["3.13"], ["linux"])) if r.needed} == expected


def test_unselected_mode_does_not_interpret_uv_defaults(fixtures, tmp_path):
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\ndefault-groups = 42\n")
    sources = load_inputs([str(tmp_path)])
    assert "docstool" in {r.package.name for r in check(sources, build_targets(["3.13"], ["linux"])) if r.needed}
    with pytest.raises(SourceError, match="default-groups"):
        select_uv_dependencies(sources, UvSelection())


def test_manifest_groups_and_requirements_activate_dependency_extras(tmp_path):
    (tmp_path / "uv.lock").write_text("""
version = 1
[manifest]
requirements = [{ name = "dep", extras = ["feature"] }]
[manifest.dependency-groups]
docs = [{ name = "docs" }]
[[package]]
name = "dep"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = []
[package.optional-dependencies]
feature = [{ name = "feature" }]
[[package]]
name = "feature"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = []
[[package]]
name = "docs"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = []
""")
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(no_dev=True))
    targets = build_targets(["3.13"], ["linux"])
    assert {r.package.name for r in check(sources, targets) if r.needed} == {"dep", "feature"}
    select_uv_dependencies(sources, UvSelection(only_groups=frozenset({"docs"})))
    assert {r.package.name for r in check(sources, targets) if r.needed} == {"docs"}


def test_workspace_dependency_extras_are_normalized_without_installing_unused_members(fixtures, tmp_path):
    lock = (
        (fixtures / "uvselect" / "uv.lock")
        .read_text()
        .replace("version = 1\n", 'version = 1\n[manifest]\nmembers = ["member", "unusedmember"]\n', 1)
    )
    lock = lock.replace('{ name = "common" }', '{ name = "common" }, { name = "member", extra = ["web_API"] }', 1)
    lock += """
[[package]]
name = "member"
version = "1.0"
source = { editable = "packages/member" }
dependencies = [{ name = "common" }]
[package.optional-dependencies]
web_API = [{ name = "api" }]
[[package]]
name = "unusedmember"
source = { editable = "packages/unusedmember" }
dependencies = [{ name = "gpu" }]
"""
    (tmp_path / "uv.lock").write_text(lock)
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(no_dev=True))
    assert {r.package.name for r in check(sources, build_targets(["3.13"], ["linux"])) if r.needed} == {
        "common",
        "library",
        "librarybase",
        "one",
        "api",
    }


def test_only_group_overrides_configured_project_selection(fixtures, tmp_path, capsys, fake_pypi):
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nextras = ["gpu"]\ngroups = ["dev"]\n')
    assert main([str(tmp_path), "--only-group", "docs", "--json", "-t", "linux", "-p", "3.13"]) == 1
    assert {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]} == {"docstool"}
    assert fake_pypi == []


def test_online_lookups_only_include_selected_packages(fixtures, monkeypatch, capsys):
    from wheelgap import cli

    calls = []
    monkeypatch.setattr(cli, "fill_all", lambda packages, **kwargs: calls.extend(p.name for p in packages))
    assert main([str(fixtures / "uvselect"), "--no-dev", "--online", "--json", "-t", "linux", "-p", "3.13"]) == 0
    assert set(calls) == {"app", "common", "library", "librarybase", "one"}
    capsys.readouterr()


@pytest.mark.parametrize(
    "selection, expected",
    [
        (UvSelection(extras=frozenset({"api"}), no_dev=True), {"oracle-app", "basepkg", "libpkg", "featurepkg"}),
        (UvSelection(only_groups=frozenset({"docs"})), {"docspkg"}),
        (UvSelection(all_extras=True), {"oracle-app", "basepkg", "libpkg", "featurepkg", "gpupkg", "devpkg"}),
        (UvSelection(extras=frozenset({"empty"}), no_dev=True), {"oracle-app", "basepkg"}),
    ],
)
def test_rooted_workspace_matches_uv_export(fixtures, selection, expected):
    from wheelgap.check import reachable

    # Generated offline with uv lock; expected selections independently checked with uv export
    # --frozen --offline. Unreferenced workspace members must not become installation roots.
    sources = load_inputs([str(fixtures / "uvworkspace")])
    select_uv_dependencies(sources, selection)
    source = sources[0]
    target = build_targets(["3.13"], ["linux"])[0]
    assert {source.packages[i].name for i in reachable(source, target)} == expected


def test_selected_explain_keeps_group_provenance_for_extra_dependencies(fixtures, tmp_path):
    lock = (fixtures / "uvselect" / "uv.lock").read_text()
    # library is also a runtime dependency, but only the docs group activates its second extra.
    lock = lock.replace('docs = [{ name = "docstool" }]', 'docs = [{ name = "library", extra = ["two"] }]')
    (tmp_path / "uv.lock").write_text(lock)
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(groups=frozenset({"docs"}), no_dev=True))
    target = build_targets(["3.13"], ["linux"])[0]
    rows = {r.package.name: r for r in check(sources, [target])}
    assert rows["one"].via[target] == ["app", "library", "one"]
    assert rows["two"].via[target] == ["(docs group)", "library", "two"]
    assert rows["three"].via[target] == ["(docs group)", "library", "two", "library", "three"]


def test_virtual_workspace_keeps_member_roots(fixtures):
    from wheelgap.check import reachable

    source = load_inputs([str(fixtures / "uvvirtual")])[0]
    target = build_targets(["3.13"], ["linux"])[0]
    select_uv_dependencies([source], UvSelection(no_dev=True))
    assert {source.packages[i].name for i in reachable(source, target)} == {
        "basepkg",
        "libpkg",
        "featurepkg",
        "gpupkg",
        "devpkg",
        "docspkg",
        "unusedmember",
    }
    select_uv_dependencies([source], UvSelection(only_groups=frozenset({"docs"})))
    assert {source.packages[i].name for i in reachable(source, target)} == {"docspkg"}


def test_cli_repeatable_and_normalized_selectors(fixtures, capsys, fake_pypi):
    assert (
        main(
            [
                str(fixtures / "uvselect"),
                "--extra",
                "API",
                "--extra",
                "api",
                "--group",
                "docs",
                "--group",
                "dev",
                "--json",
                "-t",
                "linux",
                "-p",
                "3.13",
            ]
        )
        == 1
    )
    names = {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert {"api", "devtool", "docstool", "two", "three"} <= names
    assert not {"gpu", "unused", "winonly"} & names
    assert fake_pypi == []


def test_extra_target_markers_are_evaluated_for_each_python(fixtures, tmp_path):
    lock = (fixtures / "uvselect" / "uv.lock").read_text()
    lock = lock.replace(
        'api = [{ name = "api",', 'api = [{ name = "gpu", marker = "python_version < \'3.13\'" }, { name = "api",'
    )
    (tmp_path / "uv.lock").write_text(lock)
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(extras=frozenset({"api"}), no_dev=True))
    targets = build_targets(["3.12", "3.13"], ["linux"])
    rows = {r.package.name: r for r in check(sources, targets)}
    assert [s.value for s in rows["gpu"].results.values()] == ["source", "n/a"]


def test_incomplete_edge_version_does_not_hide_a_dependency(fixtures, tmp_path):
    lock = (fixtures / "uvselect" / "uv.lock").read_text()
    lock = lock.replace(
        '{ name = "library", extra = ["one"] }', '{ name = "library", version = "missing", extra = ["one"] }'
    )
    (tmp_path / "uv.lock").write_text(lock)
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(no_dev=True))
    assert {r.package.name for r in check(sources, build_targets(["3.13"], ["linux"])) if r.needed} == {
        "common",
        "library",
        "librarybase",
        "one",
    }


def test_automatic_narrow_lock_rechecks_only_selected_packages(fixtures, tmp_path, monkeypatch, capsys):
    from wheelgap import cli

    lock = (
        (fixtures / "uvselect" / "uv.lock")
        .read_text()
        .replace('requires-python = ">=3.11"', 'requires-python = "==3.12.*"')
    )
    (tmp_path / "uv.lock").write_text(lock)
    calls = []

    def recheck(packages, simple_index=None, read_cache=False, write_cache=False):
        calls.extend(p.name for p in packages)
        return 0, 0

    monkeypatch.setattr(cli, "recheck_lock", recheck)
    assert main([str(tmp_path), "--no-dev", "--json", "-t", "linux", "-p", "3.13"]) == 0
    assert set(calls) == {"app", "common", "library", "librarybase", "one"}
    assert "locked for Python ==3.12.*" in capsys.readouterr().err


def test_offline_selection_ignores_unselected_missing_artifacts(fixtures, tmp_path, capsys, fake_pypi):
    lock = (
        (fixtures / "uvselect" / "uv.lock")
        .read_text()
        .replace('sdist = { url = "https://example.invalid/gpu-1.0.tar.gz" }', "")
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    arguments = [str(tmp_path), "--offline", "--json", "-t", "linux", "-p", "3.13"]
    assert main([*arguments, "--no-dev"]) == 0
    assert "gpu" not in {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert main(arguments) == 1
    report = json.loads(capsys.readouterr().out)
    assert next(p for p in report["packages"] if p["name"] == "gpu")["results"]["linux-x86_64"] == "missing"
    assert fake_pypi == []


def test_online_selection_preserves_private_index_and_python_context(fixtures, tmp_path, monkeypatch, capsys):
    from wheelgap import pypi

    lock = (
        (fixtures / "uvselect" / "uv.lock")
        .read_text()
        .replace(
            'name = "common"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }',
            'name = "common"\nversion = "1.0"\nsource = { registry = "https://private.example/simple" }',
        )
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    calls = []

    def fill(package, python=None, read_cache=False, write_cache=False, pythons=()):
        calls.append((package.name, pythons))

    monkeypatch.setattr(pypi, "fill", fill)
    assert main([str(tmp_path), "--online", "--no-dev", "--json", "-t", "linux", "-p", "3.13"]) == 0
    assert set(calls) == {("library", ("3.13",)), ("librarybase", ("3.13",)), ("one", ("3.13",))}
    report = capsys.readouterr()
    assert "kept the locked files of 1 package from other indexes" in report.err
    assert "common" in {p["name"] for p in json.loads(report.out)["packages"]}


def test_offline_selection_skips_narrow_lock_recheck(fixtures, tmp_path, capsys, fake_pypi):
    lock = (
        (fixtures / "uvselect" / "uv.lock")
        .read_text()
        .replace('requires-python = ">=3.11"', 'requires-python = "==3.12.*"')
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    assert main([str(tmp_path), "--offline", "--no-dev", "--json", "-t", "linux", "-p", "3.13"]) == 0
    assert "weren't re-checked" in capsys.readouterr().err
    assert fake_pypi == []


def test_ignore_note_counts_packages_the_selection_leaves_out(fixtures, capsys, fake_pypi):
    # devtool is in the lock but not installed with --no-dev: ignoring it isn't a typo.
    main([str(fixtures / "uvselect"), "--no-dev", "--ignore", "devtool,nosuchpkg", "-t", "linux", "-p", "3.13"])
    err = capsys.readouterr().err
    assert "--ignore matches no package: nosuchpkg" in err
    assert "devtool" not in err


def test_ignore_note_counts_poetry_packages_the_selection_leaves_out(poetry_groups, capsys, fake_pypi):
    # devonly is in the poetry.lock but not installed with --no-dev: ignoring it isn't a typo.
    main([str(poetry_groups), "--no-dev", "--ignore", "devonly,nosuchpkg", "-t", "linux", "-p", "3.13"])
    err = capsys.readouterr().err
    assert "--ignore matches no package: nosuchpkg" in err
    assert "devonly" not in err


def test_configured_selection_leaves_other_inputs_alone(tmp_path, capsys, fake_pypi):
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nextras = ["api"]\ngroups = ["docs"]\n')
    path = tmp_path / "requirements.txt"
    path.write_text("purewheel==3.1\n")
    assert main([str(path), "-t", "linux-x86_64", "-p", "3.13"]) == 0
    assert "requires uv.lock" not in capsys.readouterr().err


def test_selection_names_workspace_members_it_does_not_check(fixtures, capsys, fake_pypi):
    args = [str(fixtures / "uvworkspace"), "-t", "linux", "-p", "3.13", "--json"]
    main([*args, "--extra", "api", "--no-dev"])
    err = capsys.readouterr().err
    assert "workspace members outside it aren't checked" in err
    assert "unusedmember" in err and "libpkg" not in err
    main([*args, "--only-group", "docs"])
    assert "workspace members" not in capsys.readouterr().err


POETRY_GROUPS_LOCK = """\
[[package]]
name = "app-dep"
version = "1.0"
groups = ["main"]
files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]

[[package]]
name = "devonly"
version = "1.0"
groups = ["dev"]
files = [{file = "devonly-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "docstool"
version = "1.0"
groups = ["docs"]
files = [{file = "docstool-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "winonly"
version = "1.0"
groups = ["main", "dev"]
markers = { main = "sys_platform == \\"win32\\"" }
files = [{file = "winonly-1.0-py3-none-any.whl", hash = "sha256:00"}]

[metadata]
lock-version = "2.1"
"""

POETRY_GROUPS_PYPROJECT = """\
[tool.poetry.group.dev.dependencies]
devonly = "*"

[tool.poetry.group.docs]
optional = true

[tool.poetry.group.docs.dependencies]
docstool = "*"
"""


def _poetry_names(capsys, argv):
    code = main([*argv, "--json", "-t", "linux-x86_64", "-p", "3.13"])
    return code, {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}


@pytest.fixture
def poetry_groups(tmp_path):
    (tmp_path / "poetry.lock").write_text(POETRY_GROUPS_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(POETRY_GROUPS_PYPROJECT, encoding="utf-8")
    return tmp_path


def test_poetry_lock_no_dev_drops_a_dev_only_gap(poetry_groups, capsys, fake_pypi):
    code, names = _poetry_names(capsys, [str(poetry_groups)])
    assert code == 1 and {"app-dep", "devonly", "docstool"} <= names  # unchanged without a selector
    code, names = _poetry_names(capsys, [str(poetry_groups), "--no-dev"])
    # docs is optional in pyproject.toml, so Poetry doesn't install it unless asked for. Without dev, winonly
    # keeps only main's win32 marker, so it isn't installed on Linux.
    assert code == 0 and names == {"app-dep"}
    assert fake_pypi == []


def test_poetry_lock_group_adds_an_optional_group(poetry_groups, capsys, fake_pypi):
    code, names = _poetry_names(capsys, [str(poetry_groups), "--no-dev", "--group", "docs"])
    assert code == 1 and names == {"app-dep", "docstool"}


def test_poetry_lock_only_group_keeps_only_it(poetry_groups, capsys, fake_pypi):
    code, names = _poetry_names(capsys, [str(poetry_groups), "--only-group", "dev"])
    assert code == 1 and names == {"devonly", "winonly"}
    code, names = _poetry_names(capsys, [str(poetry_groups), "--only-group", "main"])
    assert code == 0 and names == {"app-dep"}


def test_poetry_lock_unknown_group_is_an_error(poetry_groups, capsys, fake_pypi):
    assert main([str(poetry_groups), "--group", "nosuch"]) == 2
    assert "unknown groups: nosuch" in capsys.readouterr().err


POETRY_NO_DEV_LOCK = """\
[[package]]
name = "app-dep"
version = "1.0"
groups = ["main"]
files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]

[metadata]
lock-version = "2.1"
"""


@pytest.mark.parametrize(
    ("pyproject", "argv"),
    [
        (None, ["--no-dev"]),
        ("[tool.poetry.group.docs.dependencies]\n", ["--no-dev"]),
        ("[tool.poetry.group.docs.dependencies]\n", ["--no-dev", "--group", "docs"]),
        ("[tool.poetry.group.docs.dependencies]\n", ["--no-dev", "--only-group", "main"]),
        ('[tool.wheelgap]\ngroups = ["docs"]\n\n[tool.poetry.group.docs.dependencies]\n', ["--no-dev"]),
    ],
)
def test_poetry_lock_no_dev_without_a_dev_group_is_an_error(tmp_path, capsys, fake_pypi, pyproject, argv):
    # Like `poetry install --without dev` (Poetry 2.5.1): "Group(s) not found: dev (via --without)".
    (tmp_path / "poetry.lock").write_text(POETRY_NO_DEV_LOCK, encoding="utf-8")
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    assert main([str(tmp_path), *argv]) == 2
    err = capsys.readouterr().err
    assert "Group(s) not found: dev (via --no-dev)" in err
    assert ("no pyproject.toml beside it to declare one" in err) == (pyproject is None)
    assert main([str(tmp_path), *argv[1:], "-t", "linux-x86_64", "-p", "3.13"]) == 0  # fine without --no-dev
    assert fake_pypi == []


@pytest.mark.parametrize(
    "pyproject",
    [
        "[tool.poetry.dev-dependencies]\n",  # Poetry 2.5.1 accepts --without dev for each of these empty dev groups
        "[tool.poetry.group.dev.dependencies]\n",
        "[dependency-groups]\ndev = []\n",
    ],
)
def test_poetry_lock_no_dev_with_an_empty_declared_dev_group(tmp_path, capsys, fake_pypi, pyproject):
    (tmp_path / "poetry.lock").write_text(POETRY_NO_DEV_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    code, names = _poetry_names(capsys, [str(tmp_path), "--no-dev"])
    assert code == 0 and names == {"app-dep"}


def test_poetry_lock_no_dev_with_a_dev_group_in_the_lock_only(poetry_groups, capsys, fake_pypi):
    (poetry_groups / "pyproject.toml").unlink()  # the lock's own groups still say there's a dev group
    code, names = _poetry_names(capsys, [str(poetry_groups), "--no-dev"])
    assert code == 1 and names == {"app-dep", "docstool"}  # without pyproject.toml, docs isn't known to be optional


def test_poetry_lock_unknown_extra_is_an_error(poetry_groups, capsys, fake_pypi):
    # Like `poetry install --extras api`: "Extra [api] is not specified." This lock has no [extras] table.
    assert main([str(poetry_groups), "--extra", "api"]) == 2
    assert "unknown extras: api" in capsys.readouterr().err


def test_poetry_lock_configured_groups_apply(poetry_groups, capsys, fake_pypi):
    with (poetry_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write('\n[tool.wheelgap]\ngroups = ["docs"]\n')
    code, names = _poetry_names(capsys, [str(poetry_groups), "--no-dev"])
    assert code == 1 and names == {"app-dep", "docstool"}


def test_poetry_1x_lock_rejects_group_selection(tmp_path, capsys, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-dep"\nversion = "1.0"\ncategory = "main"\nfiles = []\n\n'
        '[metadata]\nlock-version = "2.0"\n',
        encoding="utf-8",
    )
    assert main([str(tmp_path), "--no-dev"]) == 2
    assert "needs a poetry.lock from Poetry 2.x" in capsys.readouterr().err


def test_poetry_lock_configured_extras_alone_select(poetry_groups, capsys, fake_pypi):
    # An empty configured list selects no extras: Poetry's default groups, so the optional docs group is left out.
    with (poetry_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write("\n[tool.wheelgap]\nextras = []\n")
    code, names = _poetry_names(capsys, [str(poetry_groups)])
    assert code == 1 and names == {"app-dep", "devonly", "winonly"}


def test_poetry_lock_knows_empty_pep735_groups(poetry_groups, capsys, fake_pypi):
    with (poetry_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write("\n[dependency-groups]\nType_Check = []\n")
    code, names = _poetry_names(capsys, [str(poetry_groups), "--no-dev", "--group", "type-check"])
    assert code == 0 and names == {"app-dep"}


def test_poetry_1x_lock_ignores_configured_groups(tmp_path, capsys, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-dep"\nversion = "1.0"\ncategory = "main"\n'
        'files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]\n\n'
        '[[package]]\nname = "devonly"\nversion = "1.0"\ncategory = "dev"\n'
        'files = [{file = "devonly-1.0.tar.gz", hash = "sha256:00"}]\n\n'
        '[metadata]\nlock-version = "2.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\ngroups = ["docs"]\n', encoding="utf-8")
    # No per-package groups to select by, so every package stays in.
    code, names = _poetry_names(capsys, [str(tmp_path)])
    assert code == 1 and names == {"app-dep", "devonly"}


# Shaped like `poetry lock` output (Poetry 2.5.1) for a [project] with optional-dependencies s3, yaml (linux only),
# Fancy_Name, both and an empty one: each package's markers say which extras need it. devtool and backport are also
# in the dev group, through devrunner (and backport through s3lib), so pyproject.toml declares each name only once.
POETRY_EXTRAS_LOCK = """\
[[package]]
name = "app-dep"
version = "1.0"
optional = false
groups = ["main"]
files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]

[[package]]
name = "s3lib"
version = "1.0"
optional = true
groups = ["main"]
markers = "extra == \\"s3\\""
files = [{file = "s3lib-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "linuxyaml"
version = "1.0"
optional = true
groups = ["main"]
markers = "sys_platform == \\"linux\\" and extra == \\"yaml\\""
files = [{file = "linuxyaml-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "pretty"
version = "1.0"
optional = true
groups = ["main"]
markers = "extra == \\"fancy-name\\""
files = [{file = "pretty-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "shared"
version = "1.0"
optional = true
groups = ["main"]
markers = "extra == \\"s3\\" or extra == \\"both\\""
files = [{file = "shared-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "devtool"
version = "1.0"
optional = false
groups = ["main", "dev"]
markers = {main = "extra == \\"both\\""}
files = [{file = "devtool-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "backport"
version = "1.0"
optional = false
groups = ["main", "dev"]
markers = {main = "extra == \\"s3\\" and python_version < \\"3.11\\"", dev = "python_version < \\"3.11\\""}
files = [{file = "backport-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "devrunner"
version = "1.0"
optional = false
groups = ["dev"]
files = [{file = "devrunner-1.0-py3-none-any.whl", hash = "sha256:00"}]

[extras]
both = ["devtool", "shared"]
empty = []
fancy-name = ["pretty"]
s3 = ["s3lib", "shared"]
yaml = ["linuxyaml"]

[metadata]
lock-version = "2.1"
python-versions = ">=3.10"
"""

POETRY_EXTRAS_PYPROJECT = """\
[project]
name = "app"
version = "0"
requires-python = ">=3.10"
dependencies = ["app-dep"]

[project.optional-dependencies]
s3 = ["s3lib", "shared"]
yaml = ["linuxyaml; sys_platform == 'linux'"]
Fancy_Name = ["pretty"]
both = ["shared", "devtool"]
empty = []

[tool.poetry]
package-mode = false

[tool.poetry.group.dev.dependencies]
devrunner = "*"
"""

POETRY_EXTRAS_ALL = {"app-dep", "s3lib", "linuxyaml", "pretty", "shared", "devtool", "devrunner"}


@pytest.fixture
def poetry_extras(tmp_path):
    (tmp_path / "poetry.lock").write_text(POETRY_EXTRAS_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(POETRY_EXTRAS_PYPROJECT, encoding="utf-8")
    return tmp_path


def _names(capsys, argv, target="linux-x86_64", python="3.13"):
    code = main([*argv, "--json", "-t", target, "-p", python])
    out, err = capsys.readouterr()
    report = json.loads(out)
    return code, {p["name"] for p in report["packages"] if [*p["results"].values()] != ["n/a"]}, err


# What `poetry install --dry-run` (Poetry 2.5.1) installs for the same options on a Poetry 2.5.1 lock of this shape,
# plus what Poetry 2.0-2.2 may install too: linuxyaml's and backport's markers combine an extra with other conditions
# (`sys_platform == "linux" and extra == "yaml"`), so they're kept for every extra, with a note when the extras
# selected would have left them out.
@pytest.mark.parametrize(
    "args, expected, noted",
    [
        ([], POETRY_EXTRAS_ALL, None),  # no selector: every extra still counts
        (["--no-dev"], {"app-dep", "linuxyaml"}, "linuxyaml, backport"),  # poetry install --without dev
        (["--no-dev", "--extra", "s3"], {"app-dep", "s3lib", "shared", "linuxyaml"}, "linuxyaml kept"),
        (["--no-dev", "--extra", "Fancy_Name"], {"app-dep", "pretty", "linuxyaml"}, "linuxyaml, backport"),
        (["--no-dev", "--extra", "yaml"], {"app-dep", "linuxyaml"}, "backport kept"),
        (["--no-dev", "--extra", "empty"], {"app-dep", "linuxyaml"}, "linuxyaml, backport"),
        (["--no-dev", "--all-extras"], POETRY_EXTRAS_ALL - {"devrunner"}, None),
        (["--extra", "both"], {"app-dep", "shared", "devtool", "devrunner", "linuxyaml"}, "linuxyaml, backport"),
        (["--only-group", "main", "--extra", "both"], {"app-dep", "shared", "devtool", "linuxyaml"}, "linuxyaml"),
        (["--only-group", "dev", "--extra", "s3"], {"devtool", "devrunner"}, None),  # extras need main
    ],
)
def test_poetry_lock_extras_follow_poetry_install(poetry_extras, capsys, fake_pypi, args, expected, noted):
    code, names, err = _names(capsys, [str(poetry_extras), *args])
    assert names == expected and code == (1 if expected - {"app-dep", "devrunner"} else 0)
    if noted is None:
        assert "note" not in err
    else:
        assert f"note: poetry.lock: {noted}" in err and "kept for every extra" in err
    assert fake_pypi == []


def test_poetry_lock_mixed_markers_are_kept_for_every_extra(poetry_extras, capsys, fake_pypi):
    # backport: main `extra == "s3" and python_version < "3.11"`, dev `python_version < "3.11"`. Its python_version
    # still applies, but s3 isn't needed for it.
    for args in (["--no-dev"], ["--no-dev", "--extra", "s3"], ["--extra", "both"]):
        _, names, _ = _names(capsys, [str(poetry_extras), *args], python="3.10")
        assert "backport" in names
        _, names, _ = _names(capsys, [str(poetry_extras), *args])
        assert "backport" not in names


def test_poetry_lock_extras_keep_their_other_markers(poetry_extras, capsys, fake_pypi):
    args = [str(poetry_extras), "--no-dev", "--json", "--extra", "yaml", "--extra", "s3"]
    main([*args, "-t", "linux-x86_64,macos-arm64", "-p", "3.10", "-p", "3.13"])
    results = {p["name"]: p["results"] for p in json.loads(capsys.readouterr().out)["packages"]}
    # sys_platform == "linux" and python_version < "3.11" still apply.
    assert results["linuxyaml"] == {
        "3.10 linux-x86_64": "source",
        "3.10 macos-arm64": "n/a",
        "3.13 linux-x86_64": "source",
        "3.13 macos-arm64": "n/a",
    }
    assert results["backport"] == {
        "3.10 linux-x86_64": "source",
        "3.10 macos-arm64": "source",
        "3.13 linux-x86_64": "n/a",
        "3.13 macos-arm64": "n/a",
    }


def test_poetry_lock_extras_leave_out_packages_no_selected_extra_needs(poetry_extras):
    sources = load_inputs([str(poetry_extras)])
    select_uv_dependencies(sources, UvSelection(extras=frozenset({"s3"}), no_dev=True))
    (source,) = sources
    packages = {p.name: p for p in source.packages}
    assert set(packages) == {"app-dep", "s3lib", "shared", "backport", "linuxyaml"}
    assert {"pretty", "devtool", "devrunner"} <= source.unselected_keys
    # A selected extra decides s3lib and shared everywhere; linuxyaml keeps its marker, read with extras unknown.
    assert packages["s3lib"].markers is None and packages["shared"].markers is None
    assert packages["linuxyaml"].markers == ['sys_platform == "linux" and extra == "yaml"']


def test_poetry_lock_package_counts_if_any_groups_extra_is_selected(tmp_path):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "twice"\nversion = "1.0"\ngroups = ["main", "docs"]\n'
        'markers = {main = "extra == \\"a\\"", docs = "extra == \\"b\\""}\nfiles = []\n\n'
        '[extras]\na = ["twice"]\nb = []\n\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    for extras, kept in (({"a"}, True), ({"b"}, True), (set(), False)):
        sources = load_inputs([str(tmp_path)])
        select_uv_dependencies(sources, UvSelection(extras=frozenset(extras), groups=frozenset({"docs"})))
        assert bool(sources[0].packages) is kept
        assert not kept or sources[0].packages[0].markers is None


def test_poetry_lock_extra_names_in_the_lock_are_normalized(poetry_extras):
    lock = poetry_extras / "poetry.lock"
    lock.write_text(lock.read_text(encoding="utf-8").replace("fancy-name = [", "Fancy_Name = ["), encoding="utf-8")
    sources = load_inputs([str(poetry_extras)])
    select_uv_dependencies(sources, UvSelection(extras=frozenset({"fancy-name"}), no_dev=True))
    assert "pretty" in {p.name for p in sources[0].packages}


def test_poetry_lock_ignore_note_counts_packages_extras_leave_out(poetry_extras, capsys, fake_pypi):
    main([str(poetry_extras), "--no-dev", "--ignore", "s3lib,nosuchpkg", "-t", "linux", "-p", "3.13"])
    err = capsys.readouterr().err
    assert "--ignore matches no package: nosuchpkg" in err
    assert "s3lib" not in err


def test_poetry_lock_configured_extras_apply(poetry_extras, capsys, fake_pypi):
    with (poetry_extras / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write('\n[tool.wheelgap]\nextras = ["s3"]\n')
    code, names, _ = _names(capsys, [str(poetry_extras), "--no-dev"])
    assert code == 1 and names == {"app-dep", "s3lib", "shared", "linuxyaml"}
    code, names, _ = _names(capsys, [str(poetry_extras), "--no-dev", "--extra", "fancy-name"])  # replaces them
    assert code == 1 and names == {"app-dep", "pretty", "linuxyaml"}


def test_poetry_lock_unknown_extra_names_the_lock(poetry_extras, capsys, fake_pypi):
    assert main([str(poetry_extras), "--extra", "s3", "--extra", "nope"]) == 2
    assert "poetry.lock: unknown extras: nope" in capsys.readouterr().err


def test_poetry_1x_lock_rejects_extras(tmp_path, capsys, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-dep"\nversion = "1.0"\ncategory = "main"\nfiles = []\n\n'
        '[extras]\ns3 = ["app-dep"]\n\n[metadata]\nlock-version = "2.0"\n',
        encoding="utf-8",
    )
    assert main([str(tmp_path), "--extra", "s3"]) == 2
    assert "needs a poetry.lock from Poetry 2.x" in capsys.readouterr().err


def test_only_group_with_extra_is_still_an_error_with_a_uv_lock_input(fixtures, poetry_extras, capsys, fake_pypi):
    assert main([str(fixtures / "uvselect"), str(poetry_extras), "--only-group", "main", "--extra", "s3"]) == 2
    assert "cannot be combined with --extra or --all-extras for uv.lock inputs" in capsys.readouterr().err


def test_selection_names_the_input_it_does_not_apply_to(poetry_extras, capsys, fake_pypi):
    (poetry_extras / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert main([str(poetry_extras / "poetry.lock"), str(poetry_extras / "requirements.txt"), "--no-dev"]) == 2
    err = capsys.readouterr().err
    assert "requires uv.lock, Poetry 2.x poetry.lock or pdm.lock inputs (not requirements.txt)" in err


# Locks from Poetry 2.5.1 that Poetry 2.0-2.2 (installer.re-resolve) install more of than their markers say. Each
# has a name that is in an extra and declared elsewhere in pyproject.toml; extras then leave nothing out.
LOST_MARKER_LOCK = """\
[[package]]
name = "requests"
version = "2.32.3"
optional = true
groups = ["main"]
markers = "extra == \\"s3\\""
files = [{file = "requests-2.32.3.tar.gz", hash = "sha256:00"}]

[[package]]
name = "certifi"
version = "2024.8.30"
optional = true
groups = ["main"]
markers = "extra == \\"s3\\""
files = [{file = "certifi-2024.8.30.tar.gz", hash = "sha256:00"}]

[extras]
s3 = ["requests"]

[metadata]
lock-version = "2.1"
"""

LEGACY_EXTRAS_LOCK = """\
[[package]]
name = "attrs"
version = "26.1.0"
optional = true
groups = ["main"]
markers = "extra == \\"c\\""
files = [{file = "attrs-26.1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "colorama"
version = "0.4.6"
optional = false
groups = ["main"]
markers = "sys_platform == \\"darwin\\" and extra == \\"c\\""
files = [{file = "colorama-0.4.6.tar.gz", hash = "sha256:00"}]

[[package]]
name = "six"
version = "1.17.0"
optional = false
groups = ["main"]
markers = "extra == \\"s\\""
files = [{file = "six-1.17.0.tar.gz", hash = "sha256:00"}]

[extras]
c = ["attrs", "colorama"]
s = ["six"]

[metadata]
lock-version = "2.1"
"""

GROUP_AND_EXTRA_LOCK = """\
[[package]]
name = "idna"
version = "3.10"
optional = false
groups = ["main", "dev"]
markers = {main = "extra == \\"b\\"", dev = "sys_platform == \\"win32\\""}
files = [{file = "idna-3.10.tar.gz", hash = "sha256:00"}]

[extras]
b = ["idna"]

[metadata]
lock-version = "2.1"
"""

TWO_EXTRAS_LOCK = """\
[[package]]
name = "six"
version = "1.17.0"
optional = true
groups = ["main"]
markers = "python_version < \\"3.13\\" and extra == \\"a\\" or python_version >= \\"3.13\\" and extra == \\"b\\""
files = [{file = "six-1.17.0.tar.gz", hash = "sha256:00"}]

[extras]
a = ["six"]
b = ["six"]

[metadata]
lock-version = "2.1"
"""

LEGACY = '[tool.poetry]\npackage-mode = false\n\n[tool.poetry.dependencies]\npython = ">=3.10"\n'


@pytest.mark.parametrize(
    "lock, pyproject, args, python, expected, named",
    [
        # 1. No pyproject.toml beside the lock: nothing shows how the extras are declared. Poetry 2.2.1 installs
        # requests and certifi on macOS, from `requests; sys_platform == 'darwin'` plus s3 = ["requests"].
        (
            LOST_MARKER_LOCK,
            None,
            ["--only-group", "main"],
            "3.13",
            {"requests", "certifi"},
            "no pyproject.toml beside it",
        ),
        (
            LOST_MARKER_LOCK,
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests; sys_platform == \'darwin\'"]\n\n'
            '[project.optional-dependencies]\ns3 = ["requests"]\n',
            ["--only-group", "main"],
            "3.13",
            {"requests", "certifi"},
            "requests in an extra",
        ),
        # 2. Legacy non-optional dependencies listed in [tool.poetry.extras]: Poetry 2.2.1 installs six and colorama.
        (
            LEGACY_EXTRAS_LOCK,
            LEGACY + 'six = "*"\nattrs = { version = "*", optional = true }\n\n[tool.poetry.extras]\ns = ["six"]\n',
            ["--only-group", "main"],
            "3.13",
            {"attrs", "colorama", "six"},
            "six in an extra",
        ),
        (
            LEGACY_EXTRAS_LOCK,
            LEGACY + 'colorama = { version = "*", platform = "darwin" }\nattrs = { version = "*", optional = true }\n\n'
            '[tool.poetry.extras]\nc = ["colorama", "attrs"]\n',
            ["--only-group", "main"],
            "3.13",
            {"attrs", "colorama", "six"},
            "colorama in an extra",
        ),
        # 3. In an extra and in a group with a marker: Poetry 2.0.1-2.2.1 install idna on macOS.
        (
            GROUP_AND_EXTRA_LOCK,
            '[project]\nname = "x"\nversion = "0"\n\n[project.optional-dependencies]\nb = ["idna"]\n\n'
            '[tool.poetry.group.dev.dependencies]\nidna = { version = "*", markers = "sys_platform == \'win32\'" }\n',
            ["--group", "dev"],
            "3.13",
            {"idna"},
            "idna in an extra",
        ),
        # 4. The same name in two extras, split by python_version: Poetry 2.2.1 `-E b` installs six on 3.12.
        (
            TWO_EXTRAS_LOCK,
            '[project]\nname = "x"\nversion = "0"\n\n[project.optional-dependencies]\n'
            "a = [\"six; python_version < '3.13'\"]\nb = [\"six; python_version >= '3.13'\"]\n",
            ["--extra", "b"],
            "3.12",
            {"six"},
            "six in an extra",
        ),
    ],
)
def test_poetry_lock_extras_leave_nothing_out_when_poetry_versions_disagree(
    tmp_path, capsys, fake_pypi, lock, pyproject, args, python, expected, named
):
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    code, names, err = _names(capsys, [str(tmp_path), *args], target="macos-arm64", python=python)
    assert code == 1 and names == expected
    assert named in err and "extras leave no package out of this lock" in err
    if pyproject is not None and "extras" in pyproject:
        # The configured form too: extras = [] selects no extras.
        (tmp_path / "pyproject.toml").write_text(pyproject + "\n[tool.wheelgap]\nextras = []\n", encoding="utf-8")
        _, names, _ = _names(capsys, [str(tmp_path)], target="macos-arm64", python=python)
        assert names == expected


@pytest.mark.parametrize(
    "pyproject, expected",
    [
        ('[project.optional-dependencies]\na = ["six"]\nb = ["six"]\nall = ["six", "attrs"]\n', []),  # locks right
        ("[project.optional-dependencies]\na = [\"six; os_name == 'nt'\"]\nb = [\"six; os_name == 'nt'\"]\n", ["six"]),
        ('[project.optional-dependencies]\na = ["six"]\nb = ["six>=1"]\n', ["six"]),
        ("[project.optional-dependencies]\na = [\"six; os_name == 'nt'\"]\n", []),  # one entry: locked right
        ('[project]\ndependencies = ["six"]\n\n[project.optional-dependencies]\na = ["six"]\n', ["six"]),
        (
            '[project.optional-dependencies]\na = ["six"]\n\n[tool.poetry.dependencies]\nsix = { optional = true }\n',
            ["six"],
        ),
        ('[project.optional-dependencies]\na = ["six"]\n\n[tool.poetry.group.lint.dependencies]\nSix = "*"\n', ["six"]),
        ('[project.optional-dependencies]\na = ["six"]\n\n[tool.poetry.dev-dependencies]\nsix = "*"\n', ["six"]),
        (
            '[project.optional-dependencies]\na = ["six"]\n\n[dependency-groups]\ndev = ["six; os_name == \'nt\'"]\n',
            ["six"],
        ),
        # Legacy: an optional table is the extra's own declaration, with or without a condition.
        (LEGACY + 'six = { version = "*", optional = true }\n\n[tool.poetry.extras]\na = ["six"]\nb = ["six"]\n', []),
        (
            LEGACY
            + 'six = { version = "*", optional = true, python = "<3.13" }\n\n[tool.poetry.extras]\na = ["six"]\n',
            [],
        ),
        (
            LEGACY + 'six = { version = "*", optional = true, markers = "os_name == \'nt\'" }\n\n'
            '[tool.poetry.extras]\na = ["six"]\nb = ["six"]\n',
            ["six"],
        ),
        (
            LEGACY + 'six = [{ version = "<2", optional = true, python = "<3.13" }, '
            '{ version = ">=2", optional = true, python = ">=3.13" }]\n\n[tool.poetry.extras]\na = ["six"]\n',
            ["six"],
        ),
        (LEGACY + 'six = "*"\n\n[tool.poetry.extras]\na = ["six"]\n', ["six"]),
        (LEGACY + 'six = { version = "*", platform = "darwin" }\n\n[tool.poetry.extras]\na = ["six"]\n', ["six"]),
        (LEGACY + 'six = "*"\n', []),  # in no extra
        # A legacy optional dependency with a condition, in two extras.
        (
            LEGACY + 'six = { version = "*", optional = true, python = "<3.13" }\n\n'
            '[tool.poetry.extras]\na = ["six"]\nb = ["six"]\n',
            ["six"],
        ),
        (
            LEGACY + 'six = { version = "*", optional = true, platform = "darwin" }\n\n'
            '[tool.poetry.extras]\na = ["six"]\nb = ["six"]\n',
            ["six"],
        ),
        # Names are normalized everywhere: Typing_Extensions and typing.extensions are one package.
        (
            '[project]\ndependencies = ["typing.extensions"]\n\n'
            '[project.optional-dependencies]\na = ["Typing_Extensions"]\n',
            ["typing-extensions"],
        ),
        ('[project.optional-dependencies]\na = ["Typing_Extensions"]\nb = ["typing.extensions"]\n', []),
        (
            '[project.optional-dependencies]\na = ["Typing_Extensions"]\n\n'
            '[tool.poetry.group.dev.dependencies]\n"typing.extensions" = "*"\n',
            ["typing-extensions"],
        ),
        (
            '[project.optional-dependencies]\na = ["Typing_Extensions"]\n\n'
            '[dependency-groups]\ndev = ["typing.extensions"]\n',
            ["typing-extensions"],
        ),
        (
            '[project.optional-dependencies]\na = ["Typing_Extensions"]\n\n'
            '[tool.poetry.dependencies]\n"typing.extensions" = "*"\n',
            ["typing-extensions"],
        ),
        (
            LEGACY + 'Typing_Extensions = { version = "*", optional = true }\n\n'
            '[tool.poetry.extras]\na = ["typing.extensions"]\nb = ["typing-extensions"]\n',
            [],
        ),
        (
            LEGACY + 'Typing_Extensions = "*"\n\n[tool.poetry.extras]\na = ["typing.extensions"]\n',
            ["typing-extensions"],
        ),
    ],
)
def test_poetry_extras_declared_elsewhere(pyproject, expected):
    assert _poetry_extras_declared_elsewhere(tomllib.loads(pyproject)) == (expected, [])


def test_poetry_extra_markers_have_poetry_meanings(monkeypatch):
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    extras = (frozenset({"a", "b", "c", "fancy-name"}), frozenset({"a", "b"}))
    assert poetry_marker_holds('extra == "a" and extra == "b"', target, extras)  # `extra` is a set of extras
    assert poetry_marker_holds('"b" == extra', target, extras)
    assert not poetry_marker_holds('"c" == extra', target, extras)
    assert not poetry_marker_holds('extra != "a"', target, extras)
    assert not poetry_marker_holds('extra == "Fancy_Name"', target, extras)  # normalized, and not selected
    assert not poetry_marker_holds('extra == "a" and sys_platform == "win32"', target, extras)
    assert not poetry_marker_holds(
        '(extra == "c" or extra == "fancy-name") and sys_platform == "linux"', target, extras
    )
    assert poetry_marker_holds('(extra == "c" or extra == "a") and sys_platform == "linux"', target, extras)
    # Unknown, so kept: a name that isn't one of the project's extras, another operator, another variable.
    assert poetry_marker_holds('extra == "other"', target, extras)
    assert poetry_marker_holds('"a" in extra', target, extras)
    assert poetry_marker_holds("extra == sys_platform", target, extras)
    assert poetry_marker_holds("not a marker ((", target, extras)
    # With no target, only extras are known: whether the marker can hold on some target.
    assert poetry_marker_holds('extra == "fancy-name"', None, (extras[0], frozenset())) is False
    assert poetry_marker_holds('extra == "a" and sys_platform == "win32"', None, extras)
    monkeypatch.setattr(check_module, "_evaluate3", lambda *a, **k: 1 / 0)
    assert poetry_marker_holds('extra == "c"', target, extras)  # packaging changed: kept rather than hidden


@pytest.mark.parametrize(
    "marker, decided",
    [
        # Only extra comparisons on the project's extras: the selection decides them.
        ('extra == "a" or extra == "b"', (True, False)),
        ('extra == "c"', (False, False)),
        ('"c" == extra', (False, False)),
        ('extra == "Fancy_Name"', (False, False)),  # normalized: fancy-name, not selected
        ('(extra == "c" or extra == "a") and extra != "b"', (False, False)),
        ('(extra == "c" or extra == "a") and extra == "b"', (True, False)),
        # Anything else is evaluated per target with extras unknown, noting whether the selection rules one out.
        ('sys_platform == "win32" or extra == "c"', (None, True)),
        ('sys_platform == "win32" or extra == "a"', (None, False)),
        ('sys_platform == "win32" and extra == "b" or extra == "c"', (None, True)),
        ('python_version < "3.11"', (None, False)),
        ('extra == "other"', (None, False)),  # not one of the project's extras
        ('"a" in extra', (None, False)),
        ("extra == sys_platform", (None, False)),
        ("not a marker ((", (None, False)),
    ],
)
def test_poetry_extras_decide(marker, decided):
    extras = (frozenset({"a", "b", "c", "fancy-name"}), frozenset({"a", "b"}))
    assert poetry_extras_decide(marker, extras) == decided


def test_poetry_extras_decide_keeps_a_marker_when_packaging_changes(monkeypatch):
    monkeypatch.setattr(check_module, "_evaluate3", lambda *a, **k: 1 / 0)
    assert poetry_extras_decide('extra == "c"', (frozenset({"c"}), frozenset())) == (None, False)


# Poetry 2.5.1 locks whose markers mix an extra with other conditions, where Poetry 2.0-2.2 (default settings) install
# a package the markers leave out without the extra: `uvx poetry@2.2.1 install --dry-run --no-root` on macOS, Python
# 3.12. Poetry 2.5.1 installs none of these packages, so wheelgap reports a superset of both.
def _lock(*packages: tuple[str, str | None], extras: str) -> str:
    out = ""
    for name, markers in packages:
        out += f'[[package]]\nname = "{name}"\nversion = "1.0"\ngroups = ["main"]\n'
        if markers is not None:
            out += f"markers = {markers}\n"
        out += f'files = [{{file = "{name}-1.0.tar.gz", hash = "sha256:00"}}]\n\n'
    return out + f'[extras]\n{extras}\n\n[metadata]\nlock-version = "2.1"\n'


EXTRA_A = '"extra == \\"a\\""'
PEP621 = '[project]\nname = "x"\nversion = "0"\nrequires-python = ">=3.10"\n'


@pytest.mark.parametrize(
    "lock, pyproject, args, expected",
    [
        # B1: Poetry 2.2.1 `--only main` installs idna and urllib3.
        (
            _lock(
                ("certifi", EXTRA_A),
                ("idna", '"sys_platform == \\"win32\\" or extra == \\"a\\""'),
                ("requests", EXTRA_A),
                ("urllib3", '"python_version == \\"3.10\\" or extra == \\"a\\""'),
                extras='a = ["requests"]',
            ),
            PEP621 + "dependencies = [\"urllib3; python_version < '3.11'\", \"idna; sys_platform == 'win32'\"]\n\n"
            '[project.optional-dependencies]\na = ["requests"]\n',
            ["--only-group", "main"],
            {"idna", "urllib3"},
        ),
        # B2: legacy; Poetry 2.2.1 installs idna.
        (
            _lock(
                ("certifi", EXTRA_A),
                ("idna", '"extra == \\"a\\" or sys_platform == \\"win32\\""'),
                ("requests", EXTRA_A),
                extras='a = ["requests"]',
            ),
            LEGACY + 'requests = { version = "*", optional = true }\n'
            'idna = { version = "*", markers = "sys_platform == \'win32\'" }\n\n'
            '[tool.poetry.extras]\na = ["requests"]\n',
            ["--only-group", "main"],
            {"idna"},
        ),
        # B3: a group dependency with a marker that the extra reaches; Poetry 2.2.1 installs pysocks.
        (
            _lock(
                ("requests", EXTRA_A),
                ("pysocks", '{main = "extra == \\"a\\"", dev = "sys_platform == \\"win32\\""}'),
                extras='a = ["requests"]',
            ).replace(
                'name = "pysocks"\nversion = "1.0"\ngroups = ["main"]',
                'name = "pysocks"\nversion = "1.0"\ngroups = ["main", "dev"]',
            ),
            PEP621
            + '\n[project.optional-dependencies]\na = ["requests[socks]"]\n\n[tool.poetry.group.dev.dependencies]\n'
            'pysocks = { version = "*", markers = "sys_platform == \'win32\'" }\n',
            ["--group", "dev"],
            {"pysocks"},
        ),
        # B4: Poetry 2.2.1 `-E b` installs urllib3.
        (
            _lock(
                ("requests", EXTRA_A),
                ("urllib3", '"sys_platform == \\"win32\\" and extra == \\"b\\" or extra == \\"a\\""'),
                extras='a = ["requests"]\nb = ["urllib3"]',
            ),
            PEP621
            + '\n[project.optional-dependencies]\na = ["requests"]\nb = ["urllib3; sys_platform == \'win32\'"]\n',
            ["--extra", "b"],
            {"urllib3"},
        ),
        # S2: urllib3 in an extra, reached only through `requests; python_version < '3.11'`. Neither Poetry installs
        # it on 3.12, but its marker has B1's shape, so it's kept too.
        (
            _lock(
                ("requests", '"python_version == \\"3.10\\""'),
                ("urllib3", '"python_version == \\"3.10\\" or extra == \\"net\\""'),
                extras='net = ["urllib3"]',
            ),
            PEP621 + "dependencies = [\"requests; python_version < '3.11'\"]\n\n"
            '[project.optional-dependencies]\nnet = ["urllib3"]\n',
            ["--only-group", "main"],
            {"urllib3"},
        ),
    ],
)
def test_poetry_lock_mixed_extra_markers_report_a_superset(
    tmp_path, capsys, fake_pypi, lock, pyproject, args, expected
):
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    code, names, err = _names(capsys, [str(tmp_path), *args], target="macos-arm64", python="3.12")
    assert code == 1 and names == expected
    assert f"note: poetry.lock: {', '.join(sorted(expected))} kept for every extra" in err


def test_poetry_lock_unconditional_dependency_an_extra_reaches_needs_no_note(tmp_path, capsys, fake_pypi):
    # S1: `dependencies = ["idna"]` and a = ["requests"], which depends on idna: both Poetry versions install idna only.
    lock = _lock(("certifi", EXTRA_A), ("idna", None), ("requests", EXTRA_A), extras='a = ["requests"]')
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    pyproject = PEP621 + 'dependencies = ["idna"]\n\n[project.optional-dependencies]\na = ["requests"]\n'
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    code, names, err = _names(capsys, [str(tmp_path), "--only-group", "main"], target="macos-arm64", python="3.12")
    assert names == {"idna"} and "note" not in err


def test_poetry_lock_unreadable_requirement_leaves_nothing_out(poetry_extras, capsys, fake_pypi):
    pyproject = poetry_extras / "pyproject.toml"
    pyproject.write_text(POETRY_EXTRAS_PYPROJECT.replace('"app-dep"]', '"app-dep", "not a requirement (("]'), "utf-8")
    assert _poetry_extras_declared_elsewhere(tomllib.loads(pyproject.read_text("utf-8"))) == (
        [],
        ["not a requirement (("],
    )
    code, names, err = _names(capsys, [str(poetry_extras), "--no-dev"])
    assert names == POETRY_EXTRAS_ALL - {"devrunner"}
    assert "pyproject.toml has requirements wheelgap can't read (not a requirement (()" in err


def test_configured_extras_leave_a_pdm_lock_as_it_is(tmp_path, capsys, fake_pypi):
    # devonly's dev group isn't in [metadata] groups: a group selection would leave it out, configured extras don't.
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default"]\nstrategy = ["inherit_metadata"]\nlock_version = "4.5.0"\n\n'
        '[[package]]\nname = "devonly"\nversion = "1.0"\ngroups = ["dev"]\n'
        'files = [{file = "devonly-1.0.tar.gz", hash = "sha256:00"}]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nextras = ["api"]\n', encoding="utf-8")
    code, names, _ = _names(capsys, [str(tmp_path)])
    assert code == 1 and names == {"devonly"}


# Shaped like `pdm lock -G docs` output (PDM 2.29): [metadata] groups lists every locked group, including the
# optional-dependencies group docs and the empty dev group lint; Plugins is declared but wasn't locked.
PDM_GROUPS_LOCK = """\
[metadata]
groups = ["default", "dev", "docs", "lint"]
strategy = ["inherit_metadata"]
lock_version = "4.5.1"
content_hash = "sha256:00"

[[metadata.targets]]
requires_python = ">=3.9"

[[package]]
name = "app-dep"
version = "1.0"
groups = ["default"]
files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]

[[package]]
name = "devonly"
version = "1.0"
groups = ["dev"]
files = [{file = "devonly-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "docstool"
version = "1.0"
groups = ["docs"]
files = [{file = "docstool-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "shared"
version = "1.0"
groups = ["dev"]
files = [{file = "shared-1.0.tar.gz", hash = "sha256:00"}]

[[package]]
name = "shared"
version = "1.0"
extras = ["toml"]
groups = ["default"]
files = [{file = "shared-1.0.tar.gz", hash = "sha256:00"}]
"""

PDM_GROUPS_PYPROJECT = """\
[project]
name = "app"
version = "0"
dependencies = ["app-dep", "shared[toml]"]

[project.optional-dependencies]
docs = ["docstool"]
Plugins = ["plugintool"]

[dependency-groups]
dev = ["devonly", "shared"]
lint = []
"""

PDM_ALL = {"app-dep", "devonly", "docstool", "shared"}


@pytest.fixture
def pdm_groups(tmp_path):
    (tmp_path / "pdm.lock").write_text(PDM_GROUPS_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(PDM_GROUPS_PYPROJECT, encoding="utf-8")
    return tmp_path


def test_pdm_lock_no_dev_drops_a_dev_only_gap(pdm_groups, capsys, fake_pypi):
    code, names = _poetry_names(capsys, [str(pdm_groups)])
    assert code == 1 and names == PDM_ALL  # unchanged without a selector
    code, names = _poetry_names(capsys, [str(pdm_groups), "--no-dev"])
    # Like `pdm install --without dev`: every other locked group stays, so docstool (the locked optional group
    # docs) does too. shared stays: its shared[toml] entry is in the default group.
    assert code == 1 and names == {"app-dep", "docstool", "shared"}
    assert fake_pypi == []


def test_pdm_lock_group_must_be_locked(pdm_groups, capsys, fake_pypi):
    # docs is locked, so it's already checked; naming it doesn't change anything.
    code, names = _poetry_names(capsys, [str(pdm_groups), "--no-dev", "--group", "docs"])
    assert code == 1 and names == {"app-dep", "docstool", "shared"}
    # Plugins is in pyproject.toml but not in the lock: PDM refuses to install it.
    assert main([str(pdm_groups), "--group", "plugins"]) == 2
    err = capsys.readouterr().err
    assert "groups not in the lock: plugins" in err and "pdm lock -G plugins" in err


def test_pdm_lock_only_group_keeps_only_it(pdm_groups, capsys, fake_pypi):
    code, names = _poetry_names(capsys, [str(pdm_groups), "--only-group", "dev"])
    assert code == 1 and names == {"devonly", "shared"}
    code, names = _poetry_names(capsys, [str(pdm_groups), "--only-group", "default"])
    assert code == 1 and names == {"app-dep", "shared"}
    # lint is locked but empty: a known group that selects nothing, without needing pyproject.toml.
    (pdm_groups / "pyproject.toml").unlink()
    code, names = _poetry_names(capsys, [str(pdm_groups), "--only-group", "lint"])
    assert code == 0 and names == set()


def test_pdm_lock_unknown_group_is_an_error(pdm_groups, capsys, fake_pypi):
    assert main([str(pdm_groups), "--group", "nosuch"]) == 2
    assert "unknown groups: nosuch" in capsys.readouterr().err


def test_pdm_lock_rejects_extras(pdm_groups, capsys, fake_pypi):
    assert main([str(pdm_groups), "--extra", "docs"]) == 2
    assert "pdm.lock supports groups only" in capsys.readouterr().err


def test_pdm_lock_configured_groups_keep_locked_optional_groups(pdm_groups, capsys, fake_pypi):
    with (pdm_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write('\n[tool.wheelgap]\ngroups = ["dev"]\n')
    code, names = _poetry_names(capsys, [str(pdm_groups)])
    assert code == 1 and names == PDM_ALL
    code, names = _poetry_names(capsys, [str(pdm_groups), "--no-dev"])
    assert code == 1 and names == {"app-dep", "docstool", "shared"}


def test_pdm_lock_configured_extras_select_nothing(pdm_groups, capsys, fake_pypi):
    # Configured extras apply to uv.lock inputs only: they neither filter a pdm.lock nor make it an error.
    with (pdm_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write('\n[tool.wheelgap]\nextras = ["docs"]\n')
    code, names = _poetry_names(capsys, [str(pdm_groups)])
    assert code == 1 and names == PDM_ALL


def test_ignore_note_counts_pdm_packages_the_selection_leaves_out(pdm_groups, capsys, fake_pypi):
    # devonly is in the pdm.lock but not installed with --no-dev: ignoring it isn't a typo.
    main([str(pdm_groups), "--no-dev", "--ignore", "devonly,nosuchpkg", "-t", "linux", "-p", "3.13"])
    err = capsys.readouterr().err
    assert "--ignore matches no package: nosuchpkg" in err
    assert "devonly" not in err


def test_pdm_lock_without_inherit_metadata(pdm_groups, capsys, fake_pypi):
    # Without inherit_metadata in its strategy, PDM doesn't go by the package groups.
    lock = pdm_groups / "pdm.lock"
    lock.write_text(PDM_GROUPS_LOCK.replace('["inherit_metadata"]', '["cross_platform"]'), encoding="utf-8")
    with (pdm_groups / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write('\n[tool.wheelgap]\ngroups = ["dev"]\n')
    # Configured groups leave it unfiltered; a command-line selection is an error.
    code, names = _poetry_names(capsys, [str(pdm_groups)])
    assert code == 1 and names == PDM_ALL
    assert main([str(pdm_groups), "--no-dev"]) == 2
    assert "inherit_metadata" in capsys.readouterr().err


def test_pdm_lock_without_package_groups(tmp_path, capsys, fake_pypi):
    # Shaped like `pdm lock --strategy no_inherit_metadata` output: no package records its groups.
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default", "dev"]\nstrategy = []\nlock_version = "4.5.1"\n'
        'content_hash = "sha256:00"\n\n'
        '[[package]]\nname = "app-dep"\nversion = "1.0"\n'
        'files = [{file = "app_dep-1.0-py3-none-any.whl", hash = "sha256:00"}]\n\n'
        '[[package]]\nname = "devonly"\nversion = "1.0"\n'
        'files = [{file = "devonly-1.0.tar.gz", hash = "sha256:00"}]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\ngroups = ["dev"]\n', encoding="utf-8")
    # Configured groups leave it unfiltered; a command-line selection is an error.
    code, names = _poetry_names(capsys, [str(tmp_path)])
    assert code == 1 and names == {"app-dep", "devonly"}
    assert main([str(tmp_path), "--no-dev"]) == 2
    assert "needs a pdm.lock with package groups" in capsys.readouterr().err


_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_PYPI = "https://pypi.org/simple"
_FILES = "https://files.pythonhosted.org/packages"

_CPU_EDGE = f'{{ name = "torch", version = "2.5.1", source = {{ registry = "{_CPU_INDEX}" }}'

TWO_INDEX_TORCH_LOCK = f"""\
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "cpu-extra", marker = "sys_platform == 'linux'" }},
    {{ name = "torch", version = "2.5.1", source = {{ registry = "{_PYPI}" }}, marker = "sys_platform == 'darwin'" }},
]

[package.dev-dependencies]
dev = [{{ name = "devtool" }}]

[[package]]
name = "cpu-extra"
version = "1.0"
source = {{ registry = "{_PYPI}" }}
wheels = [{{ url = "{_FILES}/cpu_extra-1.0-py3-none-any.whl", hash = "sha256:00" }}]
dependencies = [
    {_CPU_EDGE}, marker = "sys_platform == 'linux'" }},
]

[[package]]
name = "devtool"
version = "1.0"
source = {{ registry = "{_PYPI}" }}
wheels = [{{ url = "{_FILES}/devtool-1.0-py3-none-any.whl", hash = "sha256:00" }}]

[[package]]
name = "torch"
version = "2.5.1"
source = {{ registry = "{_CPU_INDEX}" }}
wheels = [{{ url = "{_CPU_INDEX}/torch-2.5.1-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:00" }}]

[[package]]
name = "torch"
version = "2.5.1"
source = {{ registry = "{_PYPI}" }}
wheels = [{{ url = "{_FILES}/torch-2.5.1-cp313-none-macosx_11_0_arm64.whl", hash = "sha256:00" }}]
"""


@pytest.mark.parametrize("flags", [[], ["--group", "dev"], ["--no-dev"]])
def test_uv_two_index_same_version_exits_zero_with_and_without_group(tmp_path, capsys, fake_pypi, flags):
    (tmp_path / "uv.lock").write_text(TWO_INDEX_TORCH_LOCK, encoding="utf-8")
    code = main([str(tmp_path), *flags, "-p", "3.13", "-t", "linux-x86_64", "-t", "macos-arm64", "--json"])
    assert code == 0
    pkgs = json.loads(capsys.readouterr().out)["packages"]
    torch_rows = [p for p in pkgs if p["name"] == "torch"]
    assert [p["results"] for p in torch_rows] == [
        {"linux-x86_64": "wheel", "macos-arm64": "n/a"},
        {"linux-x86_64": "n/a", "macos-arm64": "wheel"},
    ]
    # When one index lacks a fitting wheel, --explain --json shows the true route only on its target.
    gap_lock = TWO_INDEX_TORCH_LOCK.replace(
        "torch-2.5.1-cp313-none-macosx_11_0_arm64.whl",
        "torch-2.5.1-cp313-cp313-win_amd64.whl",
    )
    (tmp_path / "uv.lock").write_text(gap_lock, encoding="utf-8")
    assert (
        main([str(tmp_path), *flags, "-p", "3.13", "-t", "linux-x86_64", "-t", "macos-arm64", "--json", "--explain"])
        == 1
    )
    gap_pkgs = json.loads(capsys.readouterr().out)["packages"]
    gap_torch = [p for p in gap_pkgs if p["name"] == "torch"]
    assert [(p["results"], p["via"]) for p in gap_torch] == [
        ({"linux-x86_64": "wheel", "macos-arm64": "n/a"}, {}),
        ({"linux-x86_64": "n/a", "macos-arm64": "missing"}, {"macos-arm64": ["app", "torch"]}),
    ]
    assert fake_pypi == []


def test_uv_two_index_same_version_diff_agrees(tmp_path, capsys, fake_pypi):
    from wheelgap.diff import main as diff_main

    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old_lock = tmp_path / "old" / "uv.lock"
    new_lock = tmp_path / "new" / "uv.lock"
    old_lock.write_text(
        f"""\
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [{{ name = "torch", version = "2.5.0" }}]

[[package]]
name = "torch"
version = "2.5.0"
source = {{ registry = "{_PYPI}" }}
wheels = [
    {{ url = "{_FILES}/torch-2.5.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:00" }},
    {{ url = "{_FILES}/torch-2.5.0-cp313-none-macosx_11_0_arm64.whl", hash = "sha256:00" }},
]
""",
        encoding="utf-8",
    )
    new_lock.write_text(TWO_INDEX_TORCH_LOCK, encoding="utf-8")
    assert diff_main([str(old_lock), str(new_lock), "-p", "3.13", "-t", "linux-x86_64", "-t", "macos-arm64"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]

    # If the PyPI torch entry drops its macOS wheel, diff flags macos-arm64 only.
    broken = TWO_INDEX_TORCH_LOCK.replace(
        "torch-2.5.1-cp313-none-macosx_11_0_arm64.whl",
        "torch-2.5.1-cp313-cp313-win_amd64.whl",
    )
    new_lock.write_text(broken, encoding="utf-8")
    assert diff_main([str(old_lock), str(new_lock), "-p", "3.13", "-t", "linux-x86_64", "-t", "macos-arm64"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "torch 2.5.0 → 2.5.1",
        "  macos-arm64: ✓ → ✗ missing",
        "1 package got worse.",
    ]
    assert fake_pypi == []
