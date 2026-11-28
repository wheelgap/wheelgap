import json
import re
from dataclasses import asdict, fields, replace
from inspect import signature

import pytest
from test_deps import LIN, WIN, index  # noqa: F401  # index: test_deps' fake JSON API, a fixture
from test_uv_selection import pdm_groups, poetry_extras, poetry_groups  # noqa: F401

import wheelgap
from wheelgap import Status
from wheelgap.cli import main
from wheelgap.pyodide import PyodideLockError, load_pyodide_lock
from wheelgap.sources import SourceError


@pytest.mark.parametrize(
    "kwargs,flags",
    [
        ({"no_dev": True}, ["--no-dev"]),
        ({"extras": ["API"]}, ["--extra", "API"]),
        ({"all_extras": True}, ["--all-extras"]),
        ({"groups": ["docs"]}, ["--group", "docs"]),
        ({"only_groups": ["docs"]}, ["--only-group", "docs"]),
        ({"extras": ["api"], "no_dev": True}, ["--extra", "api", "--no-dev"]),
    ],
)
def test_lockfile_api_selection_matches_cli(fixtures, capsys, fake_pypi, kwargs, flags):
    path = fixtures / "uvselect"
    results = wheelgap.check_lockfile(path, wheelgap.make_targets("3.13", "linux-x86_64"), offline=True, **kwargs)
    main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--json", *flags])
    packages = json.loads(capsys.readouterr().out)["packages"]
    assert {r.name: r.statuses[next(iter(r.statuses))].value for r in results} == {
        p["name"]: p["results"]["linux-x86_64"] for p in packages
    }
    assert not fake_pypi


@pytest.mark.parametrize("fixture", ["poetry_groups", "pdm_groups", "poetry_extras"])
@pytest.mark.parametrize(
    "kwargs,flags", [({"no_dev": True}, ["--no-dev"]), ({"only_groups": ["dev"]}, ["--only-group", "dev"])]
)
def test_other_lockfile_api_selection_matches_cli(request, fixture, kwargs, flags, capsys, fake_pypi):
    path = request.getfixturevalue(fixture)
    results = wheelgap.check_lockfile(path, wheelgap.make_targets("3.13", "linux-x86_64"), offline=True, **kwargs)
    main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--json", *flags])
    assert {r.name for r in results} == {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert not fake_pypi


@pytest.mark.parametrize(
    "kwargs,flags",
    [
        ({"extras": ["Fancy_Name"], "only_groups": ["main"]}, ["--extra", "Fancy_Name", "--only-group", "main"]),
        ({"all_extras": True}, ["--all-extras"]),
    ],
)
def test_poetry_api_extras_match_cli(poetry_extras, capsys, fake_pypi, kwargs, flags):  # noqa: F811
    results = wheelgap.check_lockfile(
        poetry_extras, wheelgap.make_targets("3.13", "linux-x86_64"), offline=True, **kwargs
    )
    main([str(poetry_extras), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--json", *flags])
    assert {r.name for r in results} == {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert not fake_pypi


@pytest.mark.parametrize("script", [False, True])
@pytest.mark.parametrize("kwargs", [{"no_dev": True}, {"only_groups": ["test"], "all_extras": True}])
def test_api_selection_rejects_invalid_project_and_script_selectors(fake_pypi, tmp_path, script, kwargs):
    path = tmp_path / ("example.py" if script else "pyproject.toml")
    path.write_text(
        '# /// script\n# dependencies=["purewheel"]\n# ///\n'
        if script
        else '[project]\ndependencies=["purewheel"]\n[dependency-groups]\ntest=[]\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match="doesn't apply|cannot be combined"):
        wheelgap.check_lockfile(path, [WIN], deps=True, **kwargs)
    assert not fake_pypi


@pytest.mark.parametrize("only", [False, True])
def test_api_selects_pyproject_groups_and_extras_before_resolving(index, tmp_path, only):  # noqa: F811
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="demo"\ndependencies=["plain"]\n[project.optional-dependencies]\n'
        'crypto=["lib[crypto]"]\n[dependency-groups]\ntest=["a"]\n',
        encoding="utf-8",
    )
    kwargs = {"only_groups": iter(["test"])} if only else {"groups": iter(["test"]), "extras": iter(["crypto"])}
    names = {r.name for r in wheelgap.check_lockfile(path, [LIN], deps=True, **kwargs)}
    assert names == ({"a", "c"} if only else {"plain", "lib", "crypt", "a", "c"})


def test_api_selects_groups_only_pyproject(index, tmp_path):  # noqa: F811
    path = tmp_path / "pyproject.toml"
    path.write_text('[dependency-groups]\ntest=["app"]\n', encoding="utf-8")
    assert {r.name for r in wheelgap.check_lockfile(path, [LIN], deps=True, only_groups=["test"])} == {
        "app",
        "lib",
        "crypt",
    }


def test_api_pyproject_groups_error_lists_only_valid_group_names(fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[dependency-groups]\ntest = ["app"]\n"a\\nrun this instead" = []\n"\\u001b[2J" = []\n', encoding="utf-8"
    )
    with pytest.raises(SourceError, match="pick one with groups or only_groups: test$") as excinfo:
        wheelgap.check_lockfile(path, [LIN], deps=True)
    assert "run this instead" not in str(excinfo.value) and str(excinfo.value).isprintable()
    assert not fake_pypi


@pytest.mark.parametrize("kwargs", [{"no_dev": True}, {"extras": ["x"]}, {"groups": ["x"]}])
def test_api_selection_refuses_unsupported_inputs_before_lookup(fake_pypi, tmp_path, kwargs):
    path = tmp_path / "requirements.txt"
    path.write_text("purewheel==3.1\n", encoding="utf-8")
    with pytest.raises(SourceError, match="selection requires"):
        wheelgap.check_lockfile(path, [WIN], **kwargs)
    assert not fake_pypi


def test_api_selection_ignores_wheelgap_config_and_accepts_empty_selection(fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvselect" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\ngroups=["missing"]\n', encoding="utf-8")
    names = {
        r.name
        for r in wheelgap.check_lockfile(
            tmp_path, wheelgap.make_targets("3.13", "linux-x86_64"), offline=True, extras=[]
        )
    }
    assert "devtool" in names and not {"docstool", "gpu", "unused"} & names


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"groups": ["missing"]}, "unknown groups"),
        ({"only_groups": ["docs"], "all_extras": True}, "cannot be combined"),
        ({"groups": ["dev"], "no_dev": True}, "explicit dev"),
    ],
)
def test_api_selection_rejects_invalid_lock_selectors(fixtures, fake_pypi, kwargs, message):
    with pytest.raises(SourceError, match=message):
        wheelgap.check_lockfile(fixtures / "uvselect", [WIN], **kwargs)
    assert not fake_pypi


def test_make_targets_takes_cli_style_names():
    targets = wheelgap.make_targets(python="3.13,3.14t", platforms=["linux-aarch64", "alpine"])
    assert [t.label(True) for t in targets] == [
        "3.13 linux-aarch64",
        "3.13 musl-x86_64",
        "3.14t linux-aarch64",
        "3.14t musl-x86_64",
    ]
    assert len(wheelgap.make_targets(python="3.13", platforms="all")) == 8
    with pytest.raises(wheelgap.TargetError):
        wheelgap.make_targets(python="3.13", platforms="solaris")


def test_make_targets_next_is_the_newest_python():
    targets = wheelgap.make_targets(python=["3.14", "next-t"], platforms="linux")
    assert [t.python for t in targets] == ["3.14", "3.15t"]


def test_make_targets_rejects_free_threaded_before_3_13():
    with pytest.raises(wheelgap.TargetError, match=r"free-threading starts at Python 3\.13 \(not 3\.12t\)"):
        wheelgap.make_targets("3.12t")
    with pytest.raises(wheelgap.TargetError, match="free-threading starts at Python 3.13"):
        wheelgap.make_targets(python=["3.13", "cp312t"], platforms="linux")


@pytest.mark.parametrize("python", ["", " , "])
def test_make_targets_uses_running_python_for_empty_input(python):
    assert wheelgap.make_targets(python=python) == wheelgap.make_targets()


def test_make_targets_empty_python_keeps_other_options():
    kwargs = dict(platforms="linux-x86_64", glibc="2.28")
    assert wheelgap.make_targets(python="", **kwargs) == wheelgap.make_targets(**kwargs)


def test_package_checks_reject_empty_targets_before_lookup(fake_pypi, fixtures):
    with pytest.raises(ValueError, match="at least one target"):
        wheelgap.check_packages(["purewheel"], targets=[])
    with pytest.raises(ValueError, match="at least one target"):
        wheelgap.check_package("purewheel", targets=[])
    with pytest.raises(ValueError, match="at least one target"):
        wheelgap.check_package("purewheel", targets=[], files=["purewheel-1.0-py3-none-any.whl"])
    with pytest.raises(ValueError, match="at least one target"):
        wheelgap.check_lockfile(fixtures / "uvproj", targets=[])
    assert not fake_pypi


@pytest.mark.parametrize("packages", [["purewheel"], []])
def test_check_packages_rejects_non_positive_workers_before_lookup(fake_pypi, packages):
    with pytest.raises(ValueError, match="workers must be at least 1"):
        wheelgap.check_packages(packages, workers=0)
    assert not fake_pypi


def test_check_package_offline_with_files():
    targets = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "musl-aarch64"])
    files = ["x-1.0-cp313-cp313-manylinux_2_17_x86_64.whl", "x-1.0.tar.gz"]
    result = wheelgap.check_package("x", "1.0", targets, files=files)
    assert result.statuses == {targets[0]: Status.WHEEL, targets[1]: Status.SOURCE}
    assert not result.ok and result.gaps == [targets[1]]
    assert not result.pure
    assert result.to_dict() == {
        "name": "x",
        "version": "1.0",
        "error": None,
        "results": {"3.13 linux-x86_64": "wheel", "3.13 musl-aarch64": "source"},
    }
    assert result.to_dict(explain=True) == {
        **result.to_dict(),
        "rejected": {
            "3.13 musl-aarch64": [
                {"file": files[0], "reason": "manylinux_2_17_x86_64 doesn't match musl-aarch64"},
            ]
        },
        "via": {},
        "direct": {},
        "issues_url": None,
    }
    json.dumps(result.to_dict(explain=True))  # JSON-friendly


def test_check_package_from_index(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    result = wheelgap.check_package("purewheel", targets=targets)
    assert result.version == "3.1" and result.ok and result.pure
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/json"]


def test_check_packages_mixes_names_and_pins(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    results = wheelgap.check_packages(["purewheel", ("nowheels", "0.9"), "does-not-exist"], targets)
    by_name = {r.name: r for r in results}
    assert by_name["purewheel"].ok
    assert by_name["nowheels"].statuses[targets[0]] is Status.SOURCE
    assert by_name["does-not-exist"].error == "not on index"
    assert by_name["does-not-exist"].statuses[targets[0]] is Status.UNKNOWN


def test_result_keeps_resolution_details_out_of_public_dataclass_interface():
    assert "_resolutions" not in signature(wheelgap.Result).parameters
    result = wheelgap.Result("x", "1.0")
    assert "_resolutions" not in {f.name for f in fields(result)}
    assert "_resolutions" not in asdict(result)
    assert result.rejected == {} and result.reasons == {}


@pytest.mark.parametrize("check_one", [False, True])
def test_package_pin_respects_requires_python_per_target(fake_pypi, check_one):
    old, new = wheelgap.make_targets(python="3.9,3.13", platforms="linux-x86_64")
    if check_one:
        result = wheelgap.check_package("pythonranged", "2.0", targets=[old, new])
    else:
        (result,) = wheelgap.check_packages([("pythonranged", "2.0")], targets=[old, new])
    assert result.statuses == {old: Status.MISSING, new: Status.WHEEL}
    assert result.files == ("pythonranged-2.0-py3-none-any.whl",)
    assert result.requires_python_rejected == {"pythonranged-2.0-py3-none-any.whl": "requires Python >=3.10"}
    assert result.pure
    rejected = [("pythonranged-2.0-py3-none-any.whl", "requires Python >=3.10")]
    assert result.rejected == {old: rejected}
    assert result.reasons == {old: "pythonranged-2.0-py3-none-any.whl  requires Python >=3.10"}
    assert result.to_dict(explain=True)["rejected"] == {
        old.label(True): [{"file": f, "reason": why} for f, why in rejected]
    }
    # asdict cannot serialize dataclass Target keys; omit that public mapping for this check.
    assert "_resolutions" not in asdict(replace(result, statuses={}))
    assert fake_pypi == ["https://pypi.org/pypi/pythonranged/2.0/json"]


def test_package_pin_yanked_files_for_several_pythons(fake_pypi):
    old, new = wheelgap.make_targets(python="3.12,3.13", platforms="linux-x86_64")
    (result,) = wheelgap.check_packages([("halfyanked", "2.0")], targets=[old, new])
    assert result.yanked == {"halfyanked-2.0-py3-none-any.whl"}


def test_package_pin_lookup_error_for_several_pythons_is_reported(monkeypatch):
    # Each Python's resolution records a malformed response's error, so the Result must carry it too.
    from wheelgap import pypi

    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: {"urls": "nope"})
    old, new = wheelgap.make_targets(python="3.9,3.13", platforms="linux-x86_64")
    (result,) = wheelgap.check_packages([("x", "1.0")], targets=[old, new])
    assert result.error == "unexpected response from the index"
    assert result.statuses == {old: Status.UNKNOWN, new: Status.UNKNOWN}


def test_check_packages_counts_what_pyodide_bundles(fake_pypi, fixtures):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))  # Python 3.13
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    results = {
        r.name: r for r in wheelgap.check_packages(["fastlib", "markupsafe"], [linux, pyodide], pyodide_locks=[lock])
    }
    assert results["fastlib"].statuses == {linux: Status.WHEEL, pyodide: Status.BUNDLED}
    assert results["fastlib"].ok
    # In the lock but not on the index: the failed lookup stays a gap.
    assert results["markupsafe"].statuses[pyodide] is Status.UNKNOWN


def test_check_package_counts_what_pyodide_bundles(fake_pypi, fixtures):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    looked_up = wheelgap.check_package("fastlib", targets=[linux, pyodide], pyodide_locks=[lock])
    assert looked_up.statuses == {linux: Status.WHEEL, pyodide: Status.BUNDLED}
    offline = wheelgap.check_package(
        "MarkupSafe", "3.0.2", [linux, pyodide], files=["markupsafe-3.0.2.tar.gz"], pyodide_locks=[lock]
    )
    assert offline.statuses == {linux: Status.SOURCE, pyodide: Status.BUNDLED}
    assert offline.files == ("markupsafe-3.0.2.tar.gz",) and offline.gaps == [linux]


def test_check_lockfile_counts_what_pyodide_bundles(fake_pypi, fixtures, tmp_path):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\nnowheels==0.9\n")
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    results = {
        r.name: r
        for r in wheelgap.check_lockfile(tmp_path / "requirements.txt", [linux, pyodide], pyodide_locks=[lock])
    }
    assert results["fastlib"].statuses == {linux: Status.WHEEL, pyodide: Status.BUNDLED}
    assert results["nowheels"].statuses == {linux: Status.SOURCE, pyodide: Status.MISSING}  # not in the lock


@pytest.mark.parametrize(
    ("version", "differs"),
    [("3.0.2", False), ("3.0.2.0", False), ("2.1.5", True), (None, False)],
)
def test_result_reports_the_version_pyodide_bundles(fixtures, version, differs):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))  # markupsafe 3.0.2
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    files = [f"markupsafe-{version or '2.1.5'}.tar.gz"]
    result = wheelgap.check_package("MarkupSafe", version, [linux, pyodide], files=files, pyodide_locks=[lock])
    assert result.statuses[pyodide] is Status.BUNDLED and result.gaps == [linux]  # a different version still passes
    assert result.bundled_version == "3.0.2" and result.bundled_version_differs is differs
    assert result.bundled_versions == {"3.13": "3.0.2"}
    assert result.to_dict()["bundled"] == "3.0.2" and result.to_dict()["bundled_version_differs"] is differs


def test_result_without_a_bundled_target_has_no_bundled_version(fake_pypi, fixtures):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    by_name = {
        r.name: r for r in wheelgap.check_packages(["fastlib", "nowheels"], [linux, pyodide], pyodide_locks=[lock])
    }
    assert by_name["fastlib"].bundled_version == "2.0.0" and not by_name["fastlib"].bundled_version_differs
    assert by_name["nowheels"].bundled_version is None
    assert by_name["nowheels"].bundled_versions == {}
    assert (
        "bundled" not in by_name["nowheels"].to_dict()
        and "bundled_versions" not in by_name["nowheels"].to_dict()
        and "bundled_version_differs" not in by_name["nowheels"].to_dict()
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_version", [False, True])
@pytest.mark.parametrize("method", ["offline", "index"])
def test_pyodide_bundled_versions_are_per_python(fixtures, fake_pypi, reverse, same_version, method):
    lock_313 = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    package = "markupsafe" if method == "offline" else "fastlib"
    original_version = lock_313.packages[package]
    next_version = original_version if same_version else "9.9.9"
    lock_314 = replace(
        lock_313,
        source="fixture-pyodide-314",
        python=(3, 14),
        abi="2026_0",
        packages={**lock_313.packages, package: next_version},
    )
    locks = [lock_313, lock_314]
    if reverse:
        locks.reverse()
    targets = wheelgap.make_targets(python="3.13,3.14", platforms="pyodide")
    if method == "offline":
        result = wheelgap.check_package(
            "MarkupSafe", "3.0.2", targets, files=["markupsafe-3.0.2.tar.gz"], pyodide_locks=locks
        )
    else:
        (result,) = wheelgap.check_packages([("fastlib", "2.0.0")], targets, pyodide_locks=locks)

    assert all(result.statuses[target] is Status.BUNDLED for target in targets)
    expected = {"3.13": original_version, "3.14": next_version}
    assert result.bundled_versions == expected
    assert result.bundled_version == (original_version if same_version else None)
    assert result.bundled_version_differs is not same_version
    output = result.to_dict()
    assert output["bundled_versions"] == expected
    assert output["bundled_version_differs"] is not same_version
    assert (output.get("bundled") == original_version) if same_version else ("bundled" not in output)
    json.dumps(output)


def test_pyodide_bundled_versions_excludes_locks_without_package(fixtures):
    lock_313 = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    lock_314 = replace(
        lock_313,
        source="fixture-pyodide-314",
        python=(3, 14),
        abi="2026_0",
        packages={k: v for k, v in lock_313.packages.items() if k != "markupsafe"},
    )
    targets = wheelgap.make_targets(python="3.13,3.14", platforms="pyodide")
    result = wheelgap.check_package(
        "MarkupSafe",
        "3.0.2",
        targets,
        files=["markupsafe-3.0.2.tar.gz"],
        pyodide_locks=[lock_314, lock_313],
    )
    assert result.statuses[targets[0]] is Status.BUNDLED
    assert result.statuses[targets[1]] is Status.MISSING
    assert result.bundled_versions == {"3.13": "3.0.2"}
    assert result.bundled_version == "3.0.2"
    assert result.to_dict()["bundled_versions"] == {"3.13": "3.0.2"}


@pytest.mark.parametrize("reverse", [False, True])
def test_check_lockfile_reports_bundled_versions_per_python(tmp_path, fake_pypi, fixtures, reverse):
    lock_313 = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    lock_314 = replace(
        lock_313,
        source="fixture-pyodide-314",
        python=(3, 14),
        abi="2026_0",
        packages={**lock_313.packages, "fastlib": "9.9.9"},
    )
    locks = [lock_313, lock_314]
    if reverse:
        locks.reverse()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastlib==2.0.0\n", encoding="utf-8")
    targets = wheelgap.make_targets(python="3.13,3.14", platforms="pyodide")
    results = wheelgap.check_lockfile(requirements, targets, pyodide_locks=locks)
    assert len(results) == 1
    result = results[0]
    assert all(result.statuses[target] is Status.BUNDLED for target in targets)
    assert result.bundled_versions == {"3.13": "2.0.0", "3.14": "9.9.9"}
    assert result.bundled_version is None
    assert result.bundled_version_differs
    assert result.to_dict()["bundled_versions"] == result.bundled_versions


@pytest.mark.parametrize(
    "check",
    [
        lambda **kw: wheelgap.check_packages(["fastlib"], **kw)[0],
        lambda **kw: wheelgap.check_package("fastlib", **kw),
        lambda **kw: wheelgap.check_package("fastlib", "2.0.0", files=["fastlib-2.0.0.tar.gz"], **kw),
    ],
)
def test_pyodide_locks_can_be_a_generator(fake_pypi, fixtures, check):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    (pyodide,) = wheelgap.make_targets(python="3.13", platforms="pyodide")
    result = check(targets=[pyodide], pyodide_locks=(x for x in [lock]))
    assert result.statuses == {pyodide: Status.BUNDLED}


def test_check_lockfile_pyodide_locks_can_be_a_generator(fake_pypi, fixtures, tmp_path):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n")
    (pyodide,) = wheelgap.make_targets(python="3.13", platforms="pyodide")
    (result,) = wheelgap.check_lockfile(tmp_path / "requirements.txt", [pyodide], pyodide_locks=(x for x in [lock]))
    assert result.statuses == {pyodide: Status.BUNDLED}


@pytest.mark.parametrize(
    "check",
    [
        lambda **kw: wheelgap.check_packages(["fastlib"], **kw),
        lambda **kw: wheelgap.check_package("fastlib", **kw),
        lambda **kw: wheelgap.check_package("fastlib", files=["fastlib-2.0.0.tar.gz"], **kw),
        lambda **kw: wheelgap.check_lockfile("missing-uv.lock", **kw),
    ],
)
@pytest.mark.parametrize(
    ("python", "copies", "message"),
    [
        ("3.12", 1, "no pyodide target uses Python 3.13 .pyodide targets: 3.12"),
        ("3.13", 2, "are both for Python 3.13"),
    ],
)
def test_pyodide_locks_that_cant_apply_raise_before_lookup(fake_pypi, fixtures, check, python, copies, message):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    targets = wheelgap.make_targets(python=python, platforms=["linux-x86_64", "pyodide"])
    with pytest.raises(PyodideLockError, match=message):
        check(targets=targets, pyodide_locks=[lock] * copies)
    assert not fake_pypi


def test_check_lockfile_respects_markers_and_graph(fixtures):
    targets = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "win-amd64"])
    results = {r.name: r for r in wheelgap.check_lockfile(fixtures / "uvproj", targets)}
    assert results["winonly"].statuses == {targets[0]: Status.NOT_NEEDED, targets[1]: Status.WHEEL}
    assert "orphan" not in results  # installed on no target
    assert results["puresdist"].gaps == targets


def test_default_targets_use_running_python():
    targets = wheelgap.make_targets()
    assert {t.python for t in targets} == {wheelgap.running_python()}


def test_pinned_yanked_release_keeps_its_files(fake_pypi):
    # pip installs a release pinned with == even if it has been yanked (PEP 592).
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    assert wheelgap.check_package("yanked", "1.0", targets).statuses[targets[0]] is Status.WHEEL


@pytest.mark.parametrize(
    ("files", "pure"),
    [
        (["x-1-py3-none-any.whl"], True),
        (["x-1-py2.py3-none-any.whl"], True),
        (["x-1-py2-none-any.whl", "x-1.tar.gz"], False),  # e.g. futures: Python 2 only
        (["x-1-cp313-cp313-manylinux_2_17_x86_64.whl"], False),
    ],
)
def test_pure_means_a_python3_wheel_for_every_platform(files, pure):
    assert wheelgap.check_package("x", "1", wheelgap.make_targets("3.13", "linux"), files=files).pure is pure


def test_pinned_release_ignores_yanked_files_when_others_remain(fake_pypi):
    # pip only falls back to yanked files when every file of the pinned release is yanked.
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    assert wheelgap.check_package("halfyanked", "2.0", targets).statuses[targets[0]] is Status.SOURCE


# requires-python ==3.12.*, so uv kept only fastlib's cp312 wheel; PyPI (fake_pypi) has a cp313 one.
LOCK_312_ONLY = (
    'version = 1\nrequires-python = "==3.12.*"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\ndependencies = [{ name = "fastlib" }]\n\n'
    '[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/p/fastlib-2.0.0.tar.gz" }\n'
    'wheels = [{ url = "https://files.pythonhosted.org/p/fastlib-2.0.0-cp312-cp312-manylinux_2_17_x86_64.whl" }]\n'
)


def test_check_lockfile_rechecks_a_lock_for_another_python(fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    (result,) = wheelgap.check_lockfile(tmp_path, targets)
    assert result.name == "fastlib" and result.statuses == {targets[0]: Status.WHEEL}
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]


def test_check_lockfile_offline_uses_locked_files(fake_pypi, fixtures):
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    results = wheelgap.check_lockfile(fixtures / "uvproj", targets, offline=True)
    assert results
    assert not fake_pypi


def test_check_lockfile_refuses_targets_outside_the_lock_environments(fake_pypi, fixtures):
    # tests/fixtures/uvenvironments is what uv 0.13.1 locks for environments = [Linux on x86_64]: it has no
    # colorama (a Windows-only dependency) and none of markupsafe's Windows or macOS wheels.
    linux = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    (result,) = wheelgap.check_lockfile(fixtures / "uvenvironments", linux)
    assert (result.name, result.statuses) == ("markupsafe", {linux[0]: Status.WHEEL})
    assert not fake_pypi
    (result,) = wheelgap.check_lockfile(fixtures / "uvenvironments")  # no targets: the default ones it covers
    assert {t.platform for t in result.statuses} == {"linux-x86_64"}
    # The default Python is the running one; below the lock's requires-python (>=3.12) it is rechecked on PyPI.
    fake_pypi.clear()
    for platforms in ("win-amd64", ["linux-x86_64", "linux-aarch64"], None):  # None: the default platforms
        targets = wheelgap.make_targets(python="3.13", platforms=platforms)
        with pytest.raises(SourceError, match="uv.lock: this lock doesn't cover .*`uv sync` fails there"):
            wheelgap.check_lockfile(fixtures / "uvenvironments", targets, online=platforms is None)
    assert not fake_pypi


def test_check_lockfile_offline_skips_other_python_recheck(fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    (result,) = wheelgap.check_lockfile(tmp_path, targets, offline=True)
    assert result.statuses == {targets[0]: Status.SOURCE}
    assert not fake_pypi


@pytest.mark.parametrize("kind", ["requirements.txt", "poetry.lock"])
def test_check_lockfile_offline_refuses_missing_files(fake_pypi, tmp_path, kind):
    text = "purewheel==3.1\n" if kind == "requirements.txt" else ('[[package]]\nname = "purewheel"\nversion = "3.1"\n')
    path = tmp_path / kind
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SourceError, match="needs the index.*purewheel"):
        wheelgap.check_lockfile(path, offline=True)
    assert not fake_pypi


def test_check_lockfile_rejects_online_and_offline_before_loading(fake_pypi, tmp_path):
    with pytest.raises(ValueError, match="online.*offline"):
        wheelgap.check_lockfile(tmp_path / "absent.lock", online=True, offline=True)
    assert not fake_pypi


def test_check_lockfile_refuses_pyproject(fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\ndependencies = ["purewheel"]\n', encoding="utf-8")
    with pytest.raises(SourceError, match="lists direct dependencies only.*pass a lockfile"):
        wheelgap.check_lockfile(path)
    assert not fake_pypi


def test_check_lockfile_refuses_a_script_without_deps(fake_pypi, tmp_path):
    path = tmp_path / "example.py"
    path.write_text('# /// script\n# dependencies = ["purewheel"]\n# ///\n', encoding="utf-8")
    with pytest.raises(SourceError, match="a script's # /// script block lists direct dependencies only"):
        wheelgap.check_lockfile(path)
    assert not fake_pypi


def test_check_lockfile_missing_script_says_so(fake_pypi, tmp_path):
    # Like any other missing input, not "a script's block lists direct dependencies only".
    with pytest.raises(FileNotFoundError):
        wheelgap.check_lockfile(tmp_path / "missing.py")
    assert not fake_pypi


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_script(tmp_path):
    script = tmp_path / "example.py"
    script.write_text('# /// script\n# dependencies = ["app==1.0"]\n# ///\n', encoding="utf-8")
    rows = {r.name: r for r in wheelgap.check_lockfile(script, [WIN, LIN], deps=True)}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"].via == {WIN: ("app", "lib", "crypt")}


def test_check_lockfile_deps_refuses_a_script_naming_an_index(index, tmp_path):  # noqa: F811  (the index fixture)
    script = tmp_path / "example.py"
    script.write_text(
        '# /// script\n# dependencies = ["app==1.0"]\n# [[tool.uv.index]]\n# url = "https://example.invalid/simple"\n'
        "# ///\n",
        encoding="utf-8",
    )
    with pytest.raises(SourceError) as e:
        wheelgap.check_lockfile(script, deps=True)
    assert "the script configures an installer index or source; --deps uses only PyPI" in str(e.value)
    assert "--deps uses only PyPI, so check its lock (uv lock --script) instead" in str(e.value)
    assert index == []


# abi3lib's only wheel is abi3 for Linux: fine on 3.13 there, not on free-threaded 3.14t, and none for Windows.
# app depends on it directly on Linux and through mid everywhere, so the path differs per target.
LOCK_ABI3 = (
    'version = 1\nrequires-python = ">=3.13"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
    'dependencies = [{ name = "mid" }, { name = "abi3lib", marker = "sys_platform == \'linux\'" }]\n\n'
    '[[package]]\nname = "mid"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    'dependencies = [{ name = "abi3lib" }]\n'
    'wheels = [{ url = "https://files.pythonhosted.org/p/mid-1.0-py3-none-any.whl" }]\n\n'
    '[[package]]\nname = "abi3lib"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/p/abi3lib-1.0.tar.gz" }\n'
    'wheels = [{ url = "https://files.pythonhosted.org/p/abi3lib-1.0-cp312-abi3-manylinux_2_17_x86_64.whl" }]\n'
)
ABI3_WHEEL = "abi3lib-1.0-cp312-abi3-manylinux_2_17_x86_64.whl"


def test_check_lockfile_reasons_and_via(fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_ABI3, encoding="utf-8")
    linux, win, linux_ft, win_ft = wheelgap.make_targets(python="3.13,3.14t", platforms=["linux-x86_64", "win-amd64"])
    results = {r.name: r for r in wheelgap.check_lockfile(tmp_path, [linux, win, linux_ft, win_ft])}
    lib = results["abi3lib"]
    assert lib.statuses == {linux: Status.WHEEL, win: Status.SOURCE, linux_ft: Status.SOURCE, win_ft: Status.SOURCE}
    abi3 = "abi3 wheels can't be used on free-threaded Python (only abi3t ones can)"
    assert lib.reasons[linux_ft] == f"{ABI3_WHEEL}  {abi3}"
    assert linux not in lib.reasons and linux not in lib.rejected  # no reason for the wheel cell
    assert lib.rejected[linux_ft] == [(ABI3_WHEEL, abi3)]
    # Per gap target, each with its own path: directly on Linux, through mid on Windows.
    assert lib.via == {
        win: ("app", "mid", "abi3lib"),
        linux_ft: ("app", "abi3lib"),
        win_ft: ("app", "mid", "abi3lib"),
    }
    assert results["mid"].via == {} and results["mid"].reasons == {}  # no gaps: nothing to explain
    data = lib.to_dict(explain=True)
    assert data["via"] == {
        "3.13 win-amd64": ["app", "mid", "abi3lib"],
        "3.14t linux-x86_64": ["app", "abi3lib"],
        "3.14t win-amd64": ["app", "mid", "abi3lib"],
    }
    assert data["direct"] == {
        "3.13 win-amd64": ["mid"],
        "3.14t win-amd64": ["mid"],
    }
    assert data["rejected"]["3.14t linux-x86_64"] == [{"file": ABI3_WHEEL, "reason": abi3}]
    assert "via" not in lib.to_dict() and "rejected" not in lib.to_dict() and "direct" not in lib.to_dict()
    json.dumps(data)


def test_explain_matches_the_command_line(capsys, fake_pypi, fixtures):
    targets = wheelgap.make_targets(python="3.13,3.14", platforms=["linux-x86_64", "pyodide"])
    results = {r.name: r for r in wheelgap.check_lockfile(fixtures / "uvproj", targets)}
    argv = [str(fixtures / "uvproj"), "-p", "3.13,3.14", "-t", "linux-x86_64,pyodide", "--exit-zero", "--explain"]
    main(argv)
    out = capsys.readouterr().out
    main([*argv, "--json"])
    packages = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    assert results["nativeext"].via[targets[0]] == ("demo", "fastlib", "nativeext")
    assert results["nativeext"].reasons[targets[0]] == "no wheels published for this version"
    assert results["fastlib"].reasons[targets[2]].count("\n") == 2  # three rejected wheels
    assert "can't build packages from source" in results["nativeext"].reasons[targets[1]]
    for r in results.values():
        data = r.to_dict(explain=True)
        assert (data["rejected"], data["via"], data["direct"]) == (
            packages[r.name]["rejected"],
            packages[r.name]["via"],
            packages[r.name]["direct"],
        )
        for t, text in r.reasons.items():
            block = out.split(f"{r.name} {r.version} on {t.label(True)}: ", 1)[1].split("\n\n", 1)[0]
            lines = [line for line in block.split("\n")[1:] if not line.startswith(("  via: ", "  build needs: "))]
            # The command line lines the reasons up in a column; Result.reasons doesn't.
            assert [re.sub(r"^  (\S+\.whl) +", r"\1  ", line).removeprefix("  ") for line in lines] == text.split("\n")


def test_reasons_list_every_wheel_unaligned():
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    files = [f"x-1.0-cp3{m}-cp3{m}-win_amd64.whl" for m in range(5, 16)] + ["x-1.0.tar.gz"]  # 11 wheels
    result = wheelgap.check_package("x", "1.0", targets, files=files)
    lines = result.reasons[targets[0]].split("\n")
    assert len(lines) == 11 == len(result.rejected[targets[0]])  # not capped at --explain's 8
    assert lines == [f"{f}  {why}" for f, why in result.rejected[targets[0]]]  # no column padding


def test_no_reasons_for_unknown_or_bundled(fake_pypi, fixtures):
    lock = load_pyodide_lock(str(fixtures / "pyodide-lock.json"))
    linux, pyodide = wheelgap.make_targets(python="3.13", platforms=["linux-x86_64", "pyodide"])
    bundled = wheelgap.check_package(
        "MarkupSafe", "3.0.2", [linux, pyodide], files=["markupsafe-3.0.2.tar.gz"], pyodide_locks=[lock]
    )
    assert bundled.statuses == {linux: Status.SOURCE, pyodide: Status.BUNDLED}
    assert list(bundled.reasons) == list(bundled.rejected) == [linux]
    (unknown,) = wheelgap.check_packages(["does-not-exist"], [linux])
    assert unknown.statuses == {linux: Status.UNKNOWN}
    assert unknown.reasons == unknown.rejected == {}
    assert unknown.to_dict(explain=True)["rejected"] == {}


def test_yanked_wheel_is_named_not_missing(fake_pypi):
    # halfyanked 2.0: a yanked py3-none-any wheel and an sdist, so pip builds the sdist.
    (linux,) = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    result = wheelgap.check_package("halfyanked", "2.0", [linux])
    assert result.statuses == {linux: Status.SOURCE}
    assert result.reasons == {linux: "halfyanked-2.0-py3-none-any.whl  yanked"}


def test_via_is_empty_without_a_graph(fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n")
    (result,) = wheelgap.check_lockfile(tmp_path / "requirements.txt", wheelgap.make_targets("3.13", "linux-x86_64"))
    assert (
        result.via == {}
        and result.direct == {}
        and result.to_dict(explain=True)["via"] == {}
        and result.to_dict(explain=True)["direct"] == {}
    )
    assert list(result.reasons.values()) == ["no wheels published for this version"]
    assert wheelgap.check_package("nowheels", "0.9", wheelgap.make_targets("3.13", "linux-x86_64")).via == {}


def test_check_lockfile_pixi_environment(fake_pypi, fixtures, tmp_path):
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    default = {r.name for r in wheelgap.check_lockfile(fixtures / "pixiproj", targets)}
    assert default == {"fastlib", "purewheel"}  # winonly is n/a on linux-x86_64
    results = wheelgap.check_lockfile(fixtures / "pixiproj", targets, environment="test")
    assert [(r.name, r.statuses[targets[0]]) for r in results] == [("nowheels", Status.SOURCE)]
    # In a folder with another lockfile too, naming an environment reads pixi.lock.
    (tmp_path / "pixi.lock").write_bytes((fixtures / "pixiproj" / "pixi.lock").read_bytes())
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    assert [r.name for r in wheelgap.check_lockfile(tmp_path, targets, environment="test")] == ["nowheels"]
    with pytest.raises(SourceError, match="environment applies to pixi.lock only"):
        wheelgap.check_lockfile(fixtures / "uvproj", targets, environment="test")
    with pytest.raises(SourceError, match="the environment name is empty"):
        wheelgap.check_lockfile(fixtures / "pixiproj", targets, environment="")


def test_check_lockfile_pixi_failed_lookup_has_no_files(fixtures, monkeypatch):
    from wheelgap import pypi

    def down(url, timeout=20.0):
        raise OSError("no network")

    monkeypatch.setattr(pypi, "fetch_json", down)
    targets = wheelgap.make_targets(python="3.13", platforms="macos-arm64")
    (fastlib,) = [r for r in wheelgap.check_lockfile(fixtures / "pixiproj", targets) if r.name == "fastlib"]
    # Not the lock's two files, which are only what pixi picked for its platforms
    assert (fastlib.statuses[targets[0]], fastlib.files) == (Status.UNKNOWN, ())


def test_check_lockfile_requirements_format_control_explain(fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("--no-binary fastlib\n--only-binary nowheels\nfastlib==2.0.0\nnowheels==0.9\n", encoding="utf-8")
    (lin,) = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    results = {r.name: r for r in wheelgap.check_lockfile(req, [lin])}

    assert results["fastlib"].statuses[lin] is Status.SOURCE
    assert results["fastlib"].rejected[lin] == [
        ("fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "ignored (--no-binary)")
    ]
    assert "ignored (--no-binary)" in results["fastlib"].reasons[lin]
    assert results["fastlib"].to_dict(explain=True)["rejected"] == {
        lin.label(True): [
            {"file": "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "reason": "ignored (--no-binary)"}
        ]
    }

    assert results["nowheels"].statuses[lin] is Status.MISSING
    assert "--only-binary forbids building from source" in results["nowheels"].reasons[lin]


@pytest.mark.usefixtures("index")
def test_check_packages_deps_checks_what_the_packages_install():
    results = wheelgap.check_packages([("app", "1.0")], [WIN, LIN], deps=True)
    rows = {r.name: r for r in results}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"].statuses == {WIN: Status.SOURCE, LIN: Status.WHEEL}
    assert rows["crypt"].via == {WIN: ("app", "lib", "crypt")}  # as --explain: the path to each gap
    assert rows["crypt"].direct == {WIN: ("app",)}
    assert rows["crypt"].to_dict(explain=True)["via"] == {WIN.label(True): ["app", "lib", "crypt"]}
    assert rows["crypt"].to_dict(explain=True)["direct"] == {WIN.label(True): ["app"]}
    assert rows["winonly"].statuses == {WIN: Status.WHEEL, LIN: Status.NOT_NEEDED}  # only Windows installs it
    assert all(r.via == {} and r.direct == {} for r in wheelgap.check_packages([("app", "1.0")], [WIN, LIN]))
    assert [r.name for r in wheelgap.check_packages(["app"], [WIN])] == ["app"]


@pytest.mark.usefixtures("index")
def test_check_package_deps_returns_the_whole_tree():
    results = wheelgap.check_package("app", "1.0", [WIN], deps=True)
    assert sorted(r.name for r in results) == ["app", "crypt", "lib", "winonly"]
    assert isinstance(wheelgap.check_package("app", "1.0", [WIN]), wheelgap.Result)
    with pytest.raises(ValueError, match="deps=True looks the dependencies up on PyPI"):
        wheelgap.check_package("app", "1.0", [WIN], files=["app-1.0-py3-none-any.whl"], deps=True)


@pytest.mark.usefixtures("index")
def test_check_packages_deps_raises_the_source_error_the_cli_reports():
    with pytest.raises(SourceError, match="can't resolve c for Python 3.14 on win-arm64"):
        wheelgap.check_packages(["x", "y"], [WIN], deps=True)


@pytest.mark.usefixtures("index")
def test_check_packages_deps_refuses_a_url():
    with pytest.raises(ValueError, match="can't check a URL: foo @ https://example.org/foo-1.0.tar.gz"):
        wheelgap.check_packages(["foo @ https://example.org/foo-1.0.tar.gz"], deps=True)


def test_check_lockfile_direct_matches_cli_json_explain(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_ABI3, encoding="utf-8")
    targets = wheelgap.make_targets(python="3.13,3.14t", platforms=["linux-x86_64", "win-amd64"])
    api_results = {r.name: r for r in wheelgap.check_lockfile(tmp_path / "uv.lock", targets)}
    main(
        [
            str(tmp_path / "uv.lock"),
            "-p",
            "3.13,3.14t",
            "-t",
            "linux-x86_64,win-amd64",
            "--json",
            "--explain",
            "--offline",
        ]
    )
    cli_packages = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    for name, result in api_results.items():
        assert result.to_dict(explain=True)["direct"] == cli_packages[name]["direct"]


def test_result_issues_url():
    result = wheelgap.Result("x", "1.0", issues_url="https://github.com/example/x/issues")
    assert result.issues_url == "https://github.com/example/x/issues"
    assert "issues_url" not in result.to_dict()
    assert result.to_dict(explain=True)["issues_url"] == "https://github.com/example/x/issues"

    result_none = wheelgap.Result("x", "1.0")
    assert result_none.issues_url is None
    assert "issues_url" not in result_none.to_dict()
    assert result_none.to_dict(explain=True)["issues_url"] is None


def test_check_package_issues_url_from_pypi(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    result = wheelgap.check_package("gapwithissues", "1.0", targets=targets)
    assert result.issues_url == "https://github.com/example/gapwithissues/issues"
    assert "issues_url" not in result.to_dict()
    assert result.to_dict(explain=True)["issues_url"] == "https://github.com/example/gapwithissues/issues"


def test_check_package_issues_url_fallback_homepage(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    result = wheelgap.check_package("gapwithhome", "1.0", targets=targets)
    assert result.issues_url == "https://github.com/example/gapwithhome/issues"
    assert result.to_dict(explain=True)["issues_url"] == "https://github.com/example/gapwithhome/issues"


def test_check_package_issues_url_none_for_other_index(fake_pypi, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://other.example.com/simple")
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    result = wheelgap.check_package("gapwithissues", "1.0", targets=targets)
    assert result.issues_url is None
    assert result.to_dict(explain=True)["issues_url"] is None


def test_check_package_issues_url_none_with_pip_index_url(fake_pypi, monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://other.example.com/simple")
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    result = wheelgap.check_package("gapwithissues", "1.0", targets=targets)
    assert result.issues_url is None
    assert result.to_dict(explain=True)["issues_url"] is None


def test_check_lockfile_issues_url(fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_ABI3, encoding="utf-8")
    targets = wheelgap.make_targets(python="3.13", platforms="linux-x86_64")
    results = {r.name: r for r in wheelgap.check_lockfile(tmp_path, targets)}
    assert results["abi3lib"].issues_url is None
    assert results["abi3lib"].to_dict(explain=True)["issues_url"] is None


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_requirements(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("app==1.0\n", encoding="utf-8")
    results = wheelgap.check_lockfile(req, [WIN, LIN], deps=True)
    rows = {r.name: r for r in results}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"].statuses == {WIN: Status.SOURCE, LIN: Status.WHEEL}
    assert rows["crypt"].via == {WIN: ("app", "lib", "crypt")}
    assert rows["crypt"].direct == {WIN: ("app",)}


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "myproj"\nversion = "0.1.0"\ndependencies = ["app==1.0"]\n',
        encoding="utf-8",
    )
    results = wheelgap.check_lockfile(pyproject, [WIN, LIN], deps=True)
    rows = {r.name: r for r in results}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"].statuses == {WIN: Status.SOURCE, LIN: Status.WHEEL}
    assert rows["crypt"].via == {WIN: ("app", "lib", "crypt")}

    # Also works on a project directory containing only pyproject.toml
    dir_results = wheelgap.check_lockfile(tmp_path, [WIN, LIN], deps=True)
    dir_rows = {r.name: r for r in dir_results}
    assert sorted(dir_rows) == ["app", "crypt", "lib", "winonly"]


def test_check_lockfile_deps_refuses_lockfile(tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_ABI3, encoding="utf-8")
    with pytest.raises(SourceError, match="--deps applies to package names.*a lockfile already lists what it installs"):
        wheelgap.check_lockfile(tmp_path / "uv.lock", deps=True)


def test_check_lockfile_deps_refuses_offline(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("app==1.0\n", encoding="utf-8")
    with pytest.raises(SourceError) as e:
        wheelgap.check_lockfile(req, offline=True, deps=True)
    assert (
        str(e.value) == "offline=True: deps=True would look packages up on PyPI; pass deps=False, or set offline=False"
    )


@pytest.mark.parametrize(
    ("filename", "text", "message"),
    [
        ("requirements.txt", "--index-url https://example.invalid/simple\napp==1.0\n", "--deps uses only PyPI"),
        (
            "pyproject.toml",
            '[project]\nname = "w"\ndependencies = ["app"]\n[tool.uv]\nindex-url = "https://example.invalid/simple"\n',
            "configures an installer index or source",
        ),
        ("requirements.txt", "-c constraints.txt\napp\n", "--deps can't follow this the way pip does"),
    ],
)
def test_check_lockfile_deps_refuses_what_it_cant_resolve_on_pypi(index, tmp_path, filename, text, message):  # noqa: F811  (the index fixture)
    # Another index may hold private packages of the same names, and a constraint pip would apply differently
    # changes the tree: refuse rather than resolve something else, and send nothing to PyPI.
    (tmp_path / "constraints.txt").write_text("app==1.0\napp==2.0\n", encoding="utf-8")
    (tmp_path / filename).write_text(text, encoding="utf-8")
    with pytest.raises(SourceError, match=re.escape(message)):
        wheelgap.check_lockfile(tmp_path / filename, deps=True)
    assert index == []


def test_check_lockfile_deps_refuses_an_index_set_in_the_environment(index, tmp_path, monkeypatch):  # noqa: F811  (the index fixture)
    (tmp_path / "requirements.txt").write_text("app==1.0\n", encoding="utf-8")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    with pytest.raises(SourceError, match="PIP_INDEX_URL may point pip or uv at another index"):
        wheelgap.check_lockfile(tmp_path / "requirements.txt", deps=True)
    assert index == []


def test_check_lockfile_deps_checks_a_compiled_file_as_written(index, tmp_path):  # noqa: F811  (the index fixture)
    (tmp_path / "requirements.txt").write_text(
        "lib==2.0 \\\n    --hash=sha256:aa\n    # via app\ncrypt==1.0 \\\n    --hash=sha256:bb\n    # via lib\n",
        encoding="utf-8",
    )
    results = wheelgap.check_lockfile(tmp_path / "requirements.txt", [WIN], deps=True)
    assert sorted(r.name for r in results) == ["crypt", "lib"]  # nothing resolved: only what the file pins
    assert all(r.via == {} for r in results)
    assert all("/lib/" in url or "/crypt/" in url for url in index)


def test_check_lockfile_deps_refuses_groups_only_pyproject(fake_pypi, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[dependency-groups]\ntest = ["app"]\n', encoding="utf-8")
    with pytest.raises(SourceError, match=r"no \[project\] table"):
        wheelgap.check_lockfile(pyproject, [WIN], deps=True)
    assert not fake_pypi


def test_check_lockfile_deps_prefers_a_lockfile_beside_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "w"\ndependencies = ["app"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(LOCK_ABI3, encoding="utf-8")
    with pytest.raises(SourceError, match="a lockfile already lists what it installs"):
        wheelgap.check_lockfile(tmp_path, deps=True)


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_has_no_lock_to_recheck(tmp_path, monkeypatch):
    # A pyproject.toml, or a tree resolved from PyPI, has no lock made for other Pythons: no second, uncached
    # lookup of every package, as on the command line.
    import wheelgap.api

    rechecked = []
    monkeypatch.setattr(wheelgap.api, "recheck_lock", lambda packages: rechecked.append(packages))
    monkeypatch.setattr(wheelgap.api, "pythons_outside_lock", lambda source, targets: True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "w"\ndependencies = ["app==1.0"]\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("app==1.0\n", encoding="utf-8")
    wheelgap.check_lockfile(tmp_path / "pyproject.toml", [WIN, LIN], deps=True)
    wheelgap.check_lockfile(tmp_path / "requirements.txt", [WIN, LIN], deps=True)
    assert rechecked == []


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_uses_no_on_disk_cache(tmp_path, monkeypatch):
    # The Python API never reads or writes wheelgap's cache (docs/how-it-works.md), deps=True included.
    import wheelgap.api

    made = []
    real = wheelgap.api.Resolver

    def spy(*args, **kwargs):
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(wheelgap.api, "Resolver", spy)
    (tmp_path / "requirements.txt").write_text("app==1.0\n", encoding="utf-8")
    wheelgap.check_lockfile(tmp_path / "requirements.txt", [WIN], deps=True)
    assert made == [{}]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("-e .\napp==1.0\n", ". (editable)"),
        ("https://example.org/foo-1.0.tar.gz\napp==1.0\n", "https://example.org/foo-1.0.tar.gz (url)"),
        ("./vendor/pkg\napp==1.0\n", "./vendor/pkg (path)"),
        # Refused even where no target matches its marker, as the command line lists it as skipped.
        ('foo @ https://example.org/foo-1.0.tar.gz ; sys_platform == "linux"\napp==1.0\n', "foo (url)"),
        (
            "-e .\nhttps://example.org/foo-1.0.tar.gz\napp==1.0\n",
            ". (editable), https://example.org/foo-1.0.tar.gz (url)",
        ),
    ],
)
def test_check_lockfile_deps_refuses_non_index_requirements(index, tmp_path, text, names):  # noqa: F811  (the index fixture)
    (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
    message = (
        f"deps=True can't check what non-index packages install: {names}; "
        "pass allow_skipped=True to check the rest, or check a lockfile"
    )
    with pytest.raises(SourceError, match=re.escape(message) + "$"):
        wheelgap.check_lockfile(tmp_path / "requirements.txt", [WIN], deps=True)
    assert index == []  # refused before any lookup


def test_check_lockfile_deps_refuses_non_index_requirements_in_pyproject(index, tmp_path):  # noqa: F811  (the index fixture)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "w"\ndependencies = ["foo @ https://example.org/foo-1.0.tar.gz", "app==1.0"]\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=re.escape("non-index packages install: foo (url); pass allow_skipped")):
        wheelgap.check_lockfile(tmp_path / "pyproject.toml", [WIN], deps=True)
    assert index == []
    results = wheelgap.check_lockfile(tmp_path / "pyproject.toml", [WIN, LIN], deps=True, allow_skipped=True)
    assert sorted(r.name for r in results) == ["app", "crypt", "lib", "winonly"]


def test_check_lockfile_deps_checks_a_compiled_file_with_a_url_as_written(index, tmp_path):  # noqa: F811  (the index fixture)
    # Compiled with hashes, the file already pins what the URL requirement installs: nothing is left unchecked.
    (tmp_path / "requirements.txt").write_text(
        "lib==2.0 \\\n    --hash=sha256:aa\n    # via app\nfoo @ https://example.org/foo-1.0.tar.gz \\\n"
        "    --hash=sha256:cc\n    # via app\ncrypt==1.0 \\\n    --hash=sha256:bb\n    # via lib\n",
        encoding="utf-8",
    )
    results = wheelgap.check_lockfile(tmp_path / "requirements.txt", [WIN], deps=True)
    assert sorted(r.name for r in results) == ["crypt", "lib"]


@pytest.mark.usefixtures("index")
def test_check_lockfile_deps_allow_skipped_resolves_index_packages(tmp_path):
    # With allow_skipped=True, non-index requirements are ignored and index packages are resolved
    req_editable = tmp_path / "req_editable.txt"
    req_editable.write_text("-e .\napp==1.0\n", encoding="utf-8")
    res_editable = wheelgap.check_lockfile(req_editable, [WIN, LIN], deps=True, allow_skipped=True)
    rows_editable = {r.name: r for r in res_editable}
    assert sorted(rows_editable) == ["app", "crypt", "lib", "winonly"]
    assert rows_editable["crypt"].statuses == {WIN: Status.SOURCE, LIN: Status.WHEEL}

    req_url = tmp_path / "req_url.txt"
    req_url.write_text("https://example.org/foo-1.0.tar.gz\napp==1.0\n", encoding="utf-8")
    res_url = wheelgap.check_lockfile(req_url, [WIN, LIN], deps=True, allow_skipped=True)
    rows_url = {r.name: r for r in res_url}
    assert sorted(rows_url) == ["app", "crypt", "lib", "winonly"]
    assert rows_url["crypt"].statuses == {WIN: Status.SOURCE, LIN: Status.WHEEL}

    # allow_skipped=True on inputs with no skipped packages works normally
    req_clean = tmp_path / "req_clean.txt"
    req_clean.write_text("app==1.0\n", encoding="utf-8")
    res_clean = wheelgap.check_lockfile(req_clean, [WIN, LIN], deps=True, allow_skipped=True)
    assert sorted(r.name for r in res_clean) == ["app", "crypt", "lib", "winonly"]


def test_check_lockfile_names_a_pyproject_beside_the_lock_that_isnt_utf8(tmp_path, fake_pypi):
    # The command line reads [tool.wheelgap] from the first input's folder first, so it reaches this message only for
    # a later input; the API reads no [tool.wheelgap], so it always does.
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[manifest]\nrequirements = [{ name = "fastlib" }]\n'
        '[[package]]\nname = "fastlib"\nversion = "2.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_bytes("[tool.uv]\n# café\n".encode("latin-1"))
    with pytest.raises(SourceError, match=r"pyproject\.toml: isn't UTF-8, so its uv settings can't be read"):
        wheelgap.check_lockfile(tmp_path / "uv.lock", wheelgap.make_targets("3.13", "linux-x86_64"), offline=True)
    assert not fake_pypi


def test_check_lockfile_names_a_pyproject_beside_the_lock_that_isnt_utf8_when_selecting_groups(fixtures, tmp_path):
    # With a uv.toml beside the lock, the build restrictions come from it, so the default-groups read is the first
    # to open this pyproject.toml.
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvselect" / "uv.lock").read_bytes())
    (tmp_path / "uv.toml").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_bytes("[tool.uv]\n# caf\xe9\n".encode("latin-1"))
    with pytest.raises(SourceError, match=r"pyproject\.toml: isn't UTF-8, so its \[tool\.uv\] default-groups"):
        wheelgap.check_lockfile(tmp_path, wheelgap.make_targets("3.13", "linux-x86_64"), offline=True, groups=["docs"])
