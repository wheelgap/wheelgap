import http.client
import io
import json
import urllib.error
import urllib.request

import pytest
from conftest import FAKE_INDEX

import wheelgap
from wheelgap import Status, pypi
from wheelgap.cli import main
from wheelgap.pyodide import PyodideLockError, load_pyodide_lock
from wheelgap.sources import Package, SourceError, load_inputs


@pytest.mark.parametrize("python,expected", [("3.9", "1.0"), ("3.13", "2.0"), (None, "2.0")])
def test_range_requires_python(fake_pypi, python, expected):
    pkg = pypi.fill(Package("pythonranged", None, spec=">=1,<3"), python=python)
    assert pkg.error is None and pkg.version == expected
    assert pkg.files == [f"pythonranged-{expected}-py3-none-any.whl"]
    assert fake_pypi == ["https://pypi.org/pypi/pythonranged/json"]


@pytest.mark.parametrize("python,expected", [("3.9", "1.0"), ("3.13", "2.0"), ("3.13,3.13t", "2.0")])
def test_range_requires_python_cli(fake_pypi, capsys, tmp_path, monkeypatch, python, expected):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged>=1,<3", "-p", python, "-t", "linux,windows", "--json"]) == 0
    output = capsys.readouterr()
    assert not output.err
    (package,) = json.loads(output.out)["packages"]
    assert package["version"] == expected
    assert set(package["results"].values()) == {"wheel"}


@pytest.mark.parametrize("python,expected", [("3.9", "1.0"), ("3.13", "2.0")])
def test_range_requires_python_lockfile_api(fake_pypi, tmp_path, python, expected):
    path = tmp_path / "requirements.txt"
    path.write_text("pythonranged>=1,<3\n", encoding="utf-8")
    targets = wheelgap.make_targets(python=python, platforms="linux,windows")
    (result,) = wheelgap.check_lockfile(path, targets)
    assert result.version == expected and result.ok
    assert fake_pypi == ["https://pypi.org/pypi/pythonranged/json"]


@pytest.mark.parametrize("simple", [False, True])
@pytest.mark.parametrize("python", ["3.9,3.13,3.13t", "3.13t,3.13,3.9"])
def test_range_multiple_pythons_cli(fake_pypi, fake_simple, capsys, tmp_path, monkeypatch, simple, python):
    monkeypatch.chdir(tmp_path)
    args = ["--index-url", "https://private.example/simple"] if simple else []
    assert main(["pythonranged>=1,<3", "-p", python, "-t", "linux,windows", "--json", *args]) == 0
    rows = json.loads(capsys.readouterr().out)["packages"]
    assert len(rows) == 2
    assert {row["version"] for row in rows} == {"1.0", "2.0"}
    for row in rows:
        assert row["error"] is None
        for target, status in row["results"].items():
            chosen = "1.0" if target.startswith("3.9 ") else "2.0"
            assert status == ("wheel" if row["version"] == chosen else "n/a")
    assert len(fake_simple.calls if simple else fake_pypi) == 1


def test_range_multiple_pythons_lockfile_api(fake_pypi, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("pythonranged>=1,<3; sys_platform == 'linux'\n", encoding="utf-8")
    targets = wheelgap.make_targets(python="3.9,3.13", platforms="linux,windows")
    results = wheelgap.check_lockfile(path, targets)
    assert len(results) == 2
    for result in results:
        assert result.ok
        assert result.files == (f"pythonranged-{result.version}-py3-none-any.whl",)
        for target, status in result.statuses.items():
            chosen = "1.0" if target.minor == 9 else "2.0"
            expected = (
                Status.WHEEL if target.platform == "linux-x86_64" and result.version == chosen else Status.NOT_NEEDED
            )
            assert status is expected
        assert result.to_dict()["version"] == result.version
    assert fake_pypi == ["https://pypi.org/pypi/pythonranged/json"]


@pytest.mark.parametrize("simple", [False, True])
def test_range_multiple_pythons_failure_is_scoped(fake_pypi, fake_simple, capsys, tmp_path, monkeypatch, simple):
    monkeypatch.chdir(tmp_path)
    args = ["--index-url", "https://private.example/simple"] if simple else []
    assert main(["pythonranged>=2", "-p", "3.9,3.13", "-t", "linux", "--json", *args]) == 1
    rows = json.loads(capsys.readouterr().out)["packages"]
    failed = next(row for row in rows if row["error"])
    passed = next(row for row in rows if not row["error"])
    assert failed["error"] == "no release matches >=2 for Python 3.9"
    assert failed["version"] is None
    assert failed["results"] == {"3.9 linux-x86_64": "unknown", "3.13 linux-x86_64": "n/a"}
    assert passed["version"] == "2.0"
    assert passed["results"] == {"3.9 linux-x86_64": "n/a", "3.13 linux-x86_64": "wheel"}


def test_range_multiple_pythons_coalesces_identical_resolutions(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged>=1", "-p", "3.11,3.13", "-t", "linux", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["packages"]
    assert row["version"] == "2.0" and set(row["results"].values()) == {"wheel"}
    assert len(fake_pypi) == 1


@pytest.mark.parametrize("simple", [False, True])
@pytest.mark.parametrize("yanked", [False, True])
def test_range_multiple_pythons_keep_eligible_files_separate(
    fake_pypi, fake_simple, capsys, tmp_path, monkeypatch, simple, yanked
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(
        FAKE_INDEX,
        "mixed",
        {
            "1.0": ["mixed-1.0-py3-none-any.whl"],
            "2.0": [
                {"filename": "mixed-2.0-py3-none-any.whl", "requires_python": ">=3.10"},
                {"filename": "mixed-2.0.tar.gz", "requires_python": ">=3.8", "yanked": yanked},
            ],
        },
    )
    args = ["--index-url", "https://private.example/simple"] if simple else []
    assert main(["mixed>=1", "-p", "3.9,3.13", "-t", "linux", "--json", "--explain", *args]) == (0 if yanked else 1)
    older, newer = json.loads(capsys.readouterr().out)["packages"]
    assert older["version"] == ("1.0" if yanked else "2.0")
    assert newer["version"] == "2.0"
    assert older["results"] == {"3.9 linux-x86_64": "wheel" if yanked else "source", "3.13 linux-x86_64": "n/a"}
    assert newer["results"] == {"3.9 linux-x86_64": "n/a", "3.13 linux-x86_64": "wheel"}
    if not yanked:
        assert older["rejected"] == {"3.9 linux-x86_64": []}  # the ineligible wheel must not leak into --explain
    assert len(fake_simple.calls if simple else fake_pypi) == 1


def test_range_multiple_pythons_api_checks_selected_release_and_abi(fake_pypi, tmp_path, monkeypatch):
    monkeypatch.setitem(
        FAKE_INDEX,
        "native",
        {
            "1.0": [{"filename": "native-1.0-py3-none-any.whl", "requires_python": "<3.10"}],
            "2.0": [
                {"filename": "native-2.0-cp313-cp313-win_amd64.whl", "requires_python": ">=3.10"},
                {"filename": "native-2.0.tar.gz", "requires_python": ">=3.10"},
            ],
        },
    )
    path = tmp_path / "requirements.txt"
    path.write_text("native>=1\n", encoding="utf-8")
    targets = wheelgap.make_targets(python="3.9,3.13,3.13t", platforms="linux,windows")
    older, newer = wheelgap.check_lockfile(path, targets)
    assert older.version == "1.0" and older.ok
    assert newer.version == "2.0" and not newer.ok
    assert newer.files == ("native-2.0-cp313-cp313-win_amd64.whl", "native-2.0.tar.gz")
    for target in targets:
        if target.minor == 9:
            assert newer.statuses[target] is Status.NOT_NEEDED
        elif target.python == "3.13" and target.platform == "win-amd64":
            assert newer.statuses[target] is Status.WHEEL
        else:
            assert newer.statuses[target] is Status.SOURCE
            assert [file for file, _ in newer.rejected[target]] == [newer.files[0]]
            assert "native-1.0" not in newer.reasons[target]
    assert len(fake_pypi) == 1


@pytest.mark.parametrize("markdown", [False, True])
def test_range_multiple_pythons_text_versions(fake_pypi, capsys, tmp_path, monkeypatch, markdown):
    monkeypatch.chdir(tmp_path)
    args = ["--markdown"] if markdown else ["--color", "never"]
    assert main(["pythonranged>=1", "-p", "3.9,3.13", "-t", "linux", "--all", *args]) == 0
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith(("pythonranged", "| pythonranged"))
    ]
    assert len(lines) == 2
    assert "1.0" in lines[0] and "2.0" in lines[1]


def test_range_multiple_pythons_marker_excludes_incompatible_python(fake_pypi, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("pythonranged>=2; python_version >= '3.10'\n", encoding="utf-8")
    targets = wheelgap.make_targets(python="3.9,3.13", platforms="linux")
    (result,) = wheelgap.check_lockfile(path, targets)
    assert result.version == "2.0" and result.ok and result.error is None
    assert list(result.statuses.values()) == [Status.NOT_NEEDED, Status.WHEEL]


def test_range_multiple_pythons_keeps_bare_names(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged", "-p", "3.9,3.13", "-t", "linux", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["packages"]
    assert row["version"] == "2.0" and set(row["results"].values()) == {"wheel"}
    assert len(fake_pypi) == 1


@pytest.mark.parametrize("simple", [False, True])
def test_pin_multiple_pythons_resolves_per_python(fake_pypi, fake_simple, capsys, tmp_path, monkeypatch, simple):
    monkeypatch.chdir(tmp_path)
    args = ["--index-url", "https://private.example/simple"] if simple else []
    assert main(["pythonranged==2.0", "-p", "3.9,3.13", "-t", "linux", "--json", "--explain", *args]) == 1
    rows = json.loads(capsys.readouterr().out)["packages"]
    assert len(rows) == 2
    for row in rows:
        assert row["version"] == "2.0"
    missing = next(r for r in rows if r["results"]["3.9 linux-x86_64"] == "missing")
    wheel = next(r for r in rows if r["results"]["3.13 linux-x86_64"] == "wheel")
    assert missing["results"] == {"3.9 linux-x86_64": "missing", "3.13 linux-x86_64": "n/a"}
    assert missing["rejected"] == {
        "3.9 linux-x86_64": [{"file": "pythonranged-2.0-py3-none-any.whl", "reason": "requires Python >=3.10"}]
    }
    assert wheel["results"] == {"3.9 linux-x86_64": "n/a", "3.13 linux-x86_64": "wheel"}
    assert wheel["rejected"] == {}
    assert len(fake_simple.calls if simple else fake_pypi) == 1


def test_pin_multiple_pythons_coalesces_when_both_pythons_satisfy(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged==2.0", "-p", "3.11,3.13", "-t", "linux", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["packages"]
    assert row["version"] == "2.0" and set(row["results"].values()) == {"wheel"}
    assert len(fake_pypi) == 1


def test_range_multiple_pythons_pyproject(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\ndependencies = ["pythonranged>=1"]\n', encoding="utf-8")
    assert main([str(path), "-p", "3.9,3.13", "-t", "linux", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["packages"]
    assert [row["version"] for row in rows] == ["1.0", "2.0"]


@pytest.mark.parametrize("simple", [False, True])
def test_range_multiple_pythons_refresh(fake_pypi, fake_simple, simple):
    pkg = Package("pythonranged", None, spec=">=1")
    options = {"simple_index": "https://private.example/simple"} if simple else {}
    pypi.fill_all([pkg], pythons=("3.9", "3.13"), **options)
    assert {py: p.version for py, p in pkg.resolutions.items()} == {"3.9": "1.0", "3.13": "2.0"}
    pypi.fill_all([pkg], pythons=("3.9", "3.13"), **options)
    assert len(fake_simple.calls if simple else fake_pypi) == 1
    pypi.fill_all([pkg], refresh=True, pythons=("3.9", "3.13"), **options)
    assert {py: p.version for py, p in pkg.resolutions.items()} == {"3.9": "1.0", "3.13": "2.0"}
    assert len(fake_simple.calls if simple else fake_pypi) == 2


@pytest.mark.parametrize("simple", [False, True])
def test_range_multiple_pythons_failed_refresh_discards_old_answers(fake_pypi, fake_simple, monkeypatch, simple):
    from wheelgap.check import check
    from wheelgap.sources import Source

    pkg = Package("pythonranged", None, spec=">=1")
    options = {"simple_index": "https://private.example/simple"} if simple else {}
    pypi.fill_all([pkg], pythons=("3.9", "3.13"), **options)
    assert pkg.resolutions

    def unavailable(*args):
        raise TimeoutError

    monkeypatch.setattr(pypi, "fetch_simple" if simple else "fetch_json", unavailable)
    pypi.fill_all([pkg], refresh=True, pythons=("3.9", "3.13"), **options)
    targets = wheelgap.make_targets(python="3.9,3.13", platforms="linux")
    (row,) = check([Source("test", packages=[pkg], kind="requirements")], targets)
    assert row.package.error == "lookup failed (TimeoutError)"
    assert set(row.results.values()) == {Status.UNKNOWN}


@pytest.mark.parametrize("simple", [False, True])
def test_range_multiple_pythons_explanation_provenance(
    fake_pypi, fake_simple, fake_files, tracker, capsys, tmp_path, monkeypatch, simple
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "requirements.txt"
    path.write_text("# dependency\nranged>=1,<2\n", encoding="utf-8")
    args = ["--index-url", "https://private.example/simple"] if simple else []
    assert (
        main(
            [
                str(path),
                "-p",
                "3.9,3.13",
                "-t",
                "linux",
                "--json",
                "--explain",
                "--notes",
                "--conda-forge",
                "--build-info",
                "--github-annotations",
                *args,
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    (row,) = json.loads(output.out)["packages"]
    assert row["version"] == "1.9" and set(row["results"].values()) == {"source"}
    assert "file=requirements.txt,line=2" in output.err
    if simple:
        assert not fake_pypi and not tracker.calls and not fake_files.calls
    else:
        assert "https://pypi.org/pypi/ranged/1.9/json" in fake_pypi
        assert "https://api.anaconda.org/package/conda-forge/ranged" in fake_pypi
        assert len(tracker.calls) == 1
        assert fake_files.calls == ["https://files.example/ranged-1.9.tar.gz"]


@pytest.mark.parametrize("requires_python", [None, "", "not a specifier", 42])
def test_range_requires_python_missing_or_invalid(monkeypatch, requires_python):
    entry = {"filename": "x-2-py3-none-any.whl", "requires_python": requires_python}
    monkeypatch.setattr(pypi, "fetch_json", lambda url: {"releases": {"2": [entry]}})
    pkg = pypi.fill(Package("x", None, spec=">=1"), python="3.9")
    assert pkg.version == "2" and not pkg.error


@pytest.mark.parametrize(
    "requires_python,python,ok",
    [(">=3.8.1", "3.8", True), ("!=3.9.0", "3.9", True), ("<3.9.5", "3.9", False), (">=3.10", "3.9", False)],
)
def test_range_requires_python_patch_level_specifiers(monkeypatch, requires_python, python, ok):
    # A target "3.8" stands for a current 3.8.x interpreter, not 3.8.0 (flake8 6 requires >=3.8.1).
    entry = {"filename": "x-2-py3-none-any.whl", "requires_python": requires_python}
    monkeypatch.setattr(pypi, "fetch_json", lambda url: {"releases": {"2": [entry]}})
    pkg = pypi.fill(Package("x", None, spec=">=1"), python=python)
    assert (pkg.version == "2") is ok


@pytest.mark.parametrize("yanked,expected", [(False, "2"), (True, "1")])
def test_range_requires_python_mixed_files(monkeypatch, yanked, expected):
    older = {"filename": "x-1-py3-none-any.whl", "requires_python": ">=3.8"}
    newer = [
        {"filename": "x-2-py3-none-any.whl", "requires_python": ">=3.10"},
        {"filename": "x-2.tar.gz", "requires_python": ">=3.8", "yanked": yanked},
    ]
    monkeypatch.setattr(pypi, "fetch_json", lambda url: {"releases": {"1": [older], "2": newer}})
    pkg = pypi.fill(Package("x", None, spec=">=1"), python="3.9")
    assert pkg.version == expected and not pkg.error
    assert pkg.files == (["x-2.tar.gz"] if expected == "2" else [older["filename"]])


def test_range_requires_python_no_compatible_release(fake_pypi):
    pkg = pypi.fill(Package("pythonranged", None, spec=">=2"), python="3.9")
    assert pkg.version is None and pkg.files is None
    assert pkg.error == "no release matches >=2 for Python 3.9"


@pytest.mark.parametrize("python,expected", [("3.9", None), ("3.13", "2.0")])
def test_range_requires_python_mirror_fallback(monkeypatch, python, expected):
    data = {"info": {"version": "2.0"}, "urls": [{"filename": "x-2.0.tar.gz", "requires_python": ">=3.10"}]}
    monkeypatch.setattr(pypi, "fetch_json", lambda url: data)
    pkg = pypi.fill(Package("x", None, spec=">=1"), python=python)
    assert pkg.version == expected
    if expected is None:
        assert pkg.error == "unexpected response from the index" and pkg.files is None
    else:
        assert not pkg.error and pkg.files == ["x-2.0.tar.gz"]


def test_range_requires_python_keeps_bare_names_unchanged(fake_pypi):
    pkg = pypi.fill(Package("pythonranged", None), python="3.9")
    assert pkg.version == "2.0" and not pkg.error and pkg.files == ["pythonranged-2.0-py3-none-any.whl"]


def test_pin_requires_python_drops_incompatible_files(fake_pypi):
    pkg = pypi.fill(Package("pythonranged", "2.0"), python="3.9")
    assert pkg.version == "2.0" and not pkg.error
    assert pkg.files == []
    assert pkg.requires_python_rejected == {"pythonranged-2.0-py3-none-any.whl": "requires Python >=3.10"}


def test_pin_with_no_metadata_is_unrestricted(fake_pypi, capsys, tmp_path, monkeypatch):
    pkg = pypi.fill(Package("purewheel", "3.1"), python="3.9")
    assert pkg.version == "3.1" and not pkg.error and pkg.files == ["purewheel-3.1-py3-none-any.whl"]
    assert pkg.requires_python_rejected == {}
    monkeypatch.chdir(tmp_path)
    assert main(["purewheel==3.1", "-p", "3.9", "-t", "linux", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["packages"]
    assert row["results"] == {"linux-x86_64": "wheel"}


def test_pin_requires_python_cli_missing_and_explain(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged==2.0", "-p", "3.9", "-t", "linux-x86_64", "--explain"]) == 1
    out = capsys.readouterr().out
    assert "pythonranged 2.0 on linux-x86_64: ✗ missing" in out
    assert "pythonranged-2.0-py3-none-any.whl  requires Python >=3.10" in out
    assert main(["pythonranged==2.0", "-p", "3.9", "-t", "linux-x86_64", "--json", "--explain"]) == 1
    data = json.loads(capsys.readouterr().out)
    pkg = data["packages"][0]
    assert pkg["results"] == {"linux-x86_64": "missing"}
    assert pkg["rejected"] == {
        "linux-x86_64": [{"file": "pythonranged-2.0-py3-none-any.whl", "reason": "requires Python >=3.10"}]
    }


def test_pin_multiple_pythons_lockfile_api(fake_pypi, tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("pythonranged==2.0\n", encoding="utf-8")
    targets = wheelgap.make_targets(python="3.9,3.13", platforms="linux")
    results = wheelgap.check_lockfile(path, targets)
    assert len(results) == 2
    missing = next(r for r in results if r.statuses[targets[0]] is Status.MISSING)
    wheel = next(r for r in results if r.statuses[targets[1]] is Status.WHEEL)
    assert missing.rejected[targets[0]] == [("pythonranged-2.0-py3-none-any.whl", "requires Python >=3.10")]
    assert missing.reasons[targets[0]] == "pythonranged-2.0-py3-none-any.whl  requires Python >=3.10"
    assert wheel.ok


def test_pin_requires_python_read_from_cache(fake_pypi, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["pythonranged==2.0", "-p", "3.13", "-t", "linux"]) == 0
    assert len(fake_pypi) == 1
    assert main(["pythonranged==2.0", "-p", "3.9", "-t", "linux", "--explain"]) == 1
    assert len(fake_pypi) == 1
    out = capsys.readouterr().out
    assert "pythonranged 2.0 on linux-x86_64: ✗ missing" in out
    assert "pythonranged-2.0-py3-none-any.whl  requires Python >=3.10" in out


@pytest.mark.parametrize("python,expected", [("3.9", "3.9"), ("3.13,3.13t", "3.13"), ("3.9,3.13", None)])
def test_range_requires_python_package_api_forwarding(fake_pypi, monkeypatch, python, expected):
    from wheelgap import api

    calls = []

    def fill_all(packages, **kwargs):
        calls.append(kwargs)
        return pypi.fill_all(packages, **kwargs)

    monkeypatch.setattr(api, "fill_all", fill_all)
    targets = wheelgap.make_targets(python=python, platforms="linux,windows")
    (result,) = wheelgap.check_packages(["purewheel"], targets, workers=2)
    assert result.ok
    assert calls == [{"workers": 2, "python": expected}]


def test_version_range_checks_the_newest_matching_release(fake_pypi, tmp_path):
    # ranged has 1.5 (wheel), 1.9 (sdist only) and 2.0 (wheel); "<2" must check 1.9, not 2.0.
    (tmp_path / "requirements.txt").write_text("ranged>=1.5,<2\n", encoding="utf-8")
    (src,) = load_inputs([str(tmp_path / "requirements.txt")])
    pypi.fill_all(src.packages)
    (pkg,) = src.packages
    assert (pkg.version, pkg.files) == ("1.9", ["ranged-1.9.tar.gz"])


def test_version_range_with_no_match(fake_pypi):
    pkg = Package("ranged", None, spec=">=3")
    pypi.fill(pkg)
    assert pkg.error == "no release matches >=3"


def test_empty_index_url_means_pypi(monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "")
    assert pypi.index_url() == pypi.DEFAULT_INDEX


def test_simple_index_url_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://pypi.org/simple/")
    with pytest.raises(SourceError, match="needs the JSON API"):
        pypi.check_index_url()


@pytest.mark.parametrize("response", [None, [], {"urls": "x"}, {"urls": [{"name": "no-filename.whl"}]}])
def test_odd_index_responses_are_unknown_not_a_crash(monkeypatch, response):
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: response)
    pkg = pypi.fill(Package("x", "1.0"))
    assert pkg.error == "unexpected response from the index" and pkg.files is None


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_json_retries_transient_failures_only(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise ConnectionResetError("reset by peer")  # a dropped connection is worth a retry
        if len(calls) == 2:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": "2"}, None)
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(pypi._OPENER, "open", urlopen)
    assert pypi.fetch_json("https://pypi.org/pypi/x/json") == {"ok": True}
    assert sleeps == [0.5, 2.0]  # backoff, then the server's Retry-After

    calls.clear()

    def forbidden(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)

    monkeypatch.setattr(pypi._OPENER, "open", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        pypi.fetch_json("https://pypi.org/pypi/x/json")
    assert calls == [1]  # a 403 won't change on retry


def test_redirect_handler_refuses_https_downgrades():
    handler = pypi._HTTPSRedirectHandler()
    fp = io.BytesIO()
    headers = http.client.HTTPMessage()
    req_https = urllib.request.Request("https://cdn.example/pyodide-lock.json")
    with pytest.raises(
        pypi.RedirectRefused,
        match=r"refused redirect from https://cdn\.example/pyodide-lock\.json to http://mirror\.example/lock\.json",
    ):
        handler.redirect_request(req_https, fp, 302, "Found", headers, "http://mirror.example/lock.json")

    redirected = handler.redirect_request(req_https, fp, 302, "Found", headers, "https://mirror.example/lock.json")
    assert redirected is not None and redirected.full_url == "https://mirror.example/lock.json"

    req_http = urllib.request.Request("http://local.example/pypi/x/json")
    plain = handler.redirect_request(req_http, fp, 302, "Found", headers, "http://local.example/v2/x/json")
    assert plain is not None and plain.full_url == "http://local.example/v2/x/json"


def test_opener_uses_the_https_redirect_handler():
    handlers = [h for h in pypi._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(handlers) == 1 and isinstance(handlers[0], pypi._HTTPSRedirectHandler)
    headers = http.client.HTTPMessage()
    headers["Location"] = "http://mirror.example/lock.json"
    req = urllib.request.Request("https://cdn.example/pyodide-lock.json")
    with pytest.raises(pypi.RedirectRefused):  # the real 3xx path: Location header, urljoin, then the check
        handlers[0].http_error_302(req, io.BytesIO(), 302, "Found", headers)


def test_refused_redirect_is_not_retried_and_surfaces_cleanly(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    def redirect_to_http(req, timeout):
        calls.append(req.full_url)
        handler = pypi._HTTPSRedirectHandler()
        handler.redirect_request(
            req, io.BytesIO(), 302, "Found", http.client.HTTPMessage(), "http://evil.example/lock.json"
        )

    monkeypatch.setattr(pypi._OPENER, "open", redirect_to_http)
    url = "https://cdn.example/pyodide-lock.json"
    with pytest.raises(
        PyodideLockError,
        match=f"couldn't fetch {url}: refused redirect from {url} to http://evil.example/lock.json",
    ):
        load_pyodide_lock(url)
    assert calls == [url] and not sleeps

    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    result = wheelgap.check_package("fastlib", "2.0.0", targets)
    assert result.error == "lookup failed (RedirectRefused)" and result.statuses[targets[0]] is Status.UNKNOWN


def test_pinned_version_missing_is_named(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    result = wheelgap.check_package("fastlib", "9.9", targets)
    assert result.error == "version 9.9 not on index" and result.statuses[targets[0]] is Status.UNKNOWN


def test_range_on_a_mirror_without_a_release_list(monkeypatch):
    data = {"info": {"version": "2.0"}, "urls": [{"filename": "x-2.0-py3-none-any.whl"}]}
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: data)
    assert pypi.fill(Package("x", None, spec=">=1")).version == "2.0"  # latest matches: fine
    assert pypi.fill(Package("x", None, spec="<2")).error == "unexpected response from the index"


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "soon"])
def test_bad_retry_after_values_are_ignored(value):
    err = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": value}, None)
    assert pypi._retry_after(err) is None


def test_missing_version_when_the_project_check_fails(monkeypatch):
    def fetch(url, timeout=20.0):
        code = 404 if "/1.0/" in url else 503
        raise urllib.error.HTTPError(url, code, "x", {}, None)

    monkeypatch.setattr(pypi, "fetch_json", fetch)
    assert pypi.fill(Package("x", "1.0")).error == "version 1.0 not on index (couldn't check the project: HTTP 503)"


def test_mirror_fallback_with_no_usable_files_is_unknown(monkeypatch):
    data = {"info": {"version": "2.0"}, "urls": [{"filename": "x-2.0.tar.gz", "yanked": True}]}
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: data)
    pkg = pypi.fill(Package("x", None, spec=">=1"))
    assert pkg.error == "unexpected response from the index" and pkg.files is None


def test_fill_all_skips_a_private_package_with_no_files(fake_pypi):
    pkg = Package(name="purewheel", version="3.1", files=None, registry="https://pkgs.example.com/simple")
    assert pypi.fill_all([pkg]) == 0
    assert pkg.skip == "private index"
    assert not [url for url in fake_pypi if "purewheel" in url], fake_pypi


def test_fill_all_still_looks_up_a_pypi_package_with_no_files(fake_pypi):
    pkg = Package(name="purewheel", version="3.1", files=None)
    assert pypi.fill_all([pkg]) == 0
    assert pkg.skip is None and pkg.files == ["purewheel-3.1-py3-none-any.whl"]
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/3.1/json"]


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        (None, True),
        ("https://pypi.org/simple", True),
        ("https://pypi.org/simple/", True),
        ("https://pkgs.example.com/simple", False),
        ("https://pypi.python.org/simple", False),  # the same exact rule recheck_lock() used before
        ("", False),  # an empty registry says nothing
        ("/", False),
        ("https://pypi.org/simple-corp", False),
        ("https://pypi.org/simple/corp", False),
    ],
)
def test_is_pypi(registry, expected):
    assert pypi.is_pypi(registry) is expected


MIRROR = "https://mirror.example.com/simple"


@pytest.mark.parametrize(
    ("registry", "index", "simple_index", "expected"),
    [
        (None, None, None, True),
        ("https://pypi.org/simple", "https://mirror.example.com/pypi", None, True),
        (MIRROR, None, None, False),  # WHEELGAP_INDEX_URL unset: PyPI, as before
        (MIRROR, "https://pypi.org/pypi", None, False),
        (MIRROR, "https://mirror.example.com/pypi", None, True),
        (MIRROR, "https://mirror.example.com/pypi/", None, True),
        ("https://mirror.example.com/simple/", "https://mirror.example.com/pypi", None, True),
        (MIRROR, "https://Mirror.Example.COM:443/pypi", None, True),  # hostnames ignore case; 443 is https's port
        ("https://MIRROR.example.com:443/simple", "https://mirror.example.com/pypi", None, True),
        ("https://user:tok@mirror.example.com/simple", "https://mirror.example.com/pypi", None, True),
        ("https://${USER}:${PASS}@mirror.example.com/simple", "https://mirror.example.com/pypi", None, True),
        (MIRROR, "https://mirror.example.com/pypi", "https://mirror.example.com/simple", False),  # --index-url
        ("https://mirror.example.com:8443/simple", "https://mirror.example.com/pypi", None, False),
        ("http://mirror.example.com/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com@evil.example/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com.evil.example/simple", "https://mirror.example.com/pypi", None, False),
        ("https://pkgs.example.com/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com:99999/simple", "https://mirror.example.com/pypi", None, False),
        ("https://[bad/simple", "https://mirror.example.com/pypi", None, False),
        ("/simple", "https://mirror.example.com/pypi", None, False),
        # One host, many repositories (Artifactory, Nexus): only the same repository matches.
        (
            "https://artifactory.example/api/pypi/pypi-remote/simple",
            "https://artifactory.example/api/pypi/pypi-remote/pypi",
            None,
            True,
        ),
        (
            "https://artifactory.example/api/pypi/internal/simple",
            "https://artifactory.example/api/pypi/pypi-remote/pypi",
            None,
            False,
        ),
        ("https://mirror.example.com/Repo/simple", "https://mirror.example.com/repo/pypi", None, False),
        ("https://devpi.example/root/pypi/+simple/", "https://devpi.example/root/pypi/pypi", None, False),
        # WHEELGAP_INDEX_URL unset: only is_pypi()'s exact URL, as before, not every way of writing pypi.org.
        ("https://PyPI.org/simple", None, None, False),
        ("https://pypi.org:443/simple", None, None, False),
        # Parsers disagree on these (pip may see host h where urlsplit sees mirror.example.com): no match.
        ("https://h\\@mirror.example.com/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com\\simple", "https://mirror.example.com/pypi", None, False),
        (MIRROR, "https://mirror.example.com\\pypi", None, False),
        ("https://mirror.example.com/simple ", "https://mirror.example.com/pypi", None, False),
        (" https://mirror.example.com/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.exa\tmple.com/simple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com/sim\nple", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com/simple\x00", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com/simple\x7f", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com/simple\u200b", "https://mirror.example.com/pypi", None, False),
        (MIRROR, "https://mirror.example.com/py\rpi", None, False),
        ("https://mirror.example.com:0/simple", "https://mirror.example.com/pypi", None, False),
        (MIRROR, "https://mirror.example.com:0/pypi", None, False),
        ("https://mirror.example.com/simple#top", "https://mirror.example.com/pypi", None, False),
        (MIRROR, "https://mirror.example.com/pypi#top", None, False),
        # Not plainly <repo>/simple and <repo>/pypi: no match.
        ("https://mirror.example.com/", "https://mirror.example.com/pypi", None, False),
        ("https://mirror.example.com/simple?x=1", "https://mirror.example.com/pypi", None, False),
        (MIRROR, "https://mirror.example.com/json", None, False),
        (MIRROR, "https://mirror.example.com", None, False),
    ],
)
def test_may_look_up(monkeypatch, registry, index, simple_index, expected):
    if index is not None:
        monkeypatch.setenv("WHEELGAP_INDEX_URL", index)
    assert pypi.may_look_up(registry, simple_index) is expected


def test_fill_all_looks_up_a_mirror_package_on_wheelgap_index_url(monkeypatch, fake_pypi):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    pkg = Package(name="purewheel", version="3.1", files=None, registry=MIRROR)
    assert pypi.fill_all([pkg]) == 0
    assert pkg.skip is None and pkg.files == ["purewheel-3.1-py3-none-any.whl"]
    assert fake_pypi == ["https://mirror.example.com/pypi/purewheel/3.1/json"]


def test_online_keeps_a_mirror_package_s_locked_files(monkeypatch, fake_pypi):
    # A lock that lists its files (uv.lock, poetry.lock) keeps them under --online, mirror or not: as before.
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    pkg = Package(name="purewheel", version="3.1", files=["purewheel-3.1.tar.gz"], registry=MIRROR)
    assert pypi.fill_all([pkg], refresh=True) == 1
    assert pkg.files == ["purewheel-3.1.tar.gz"] and fake_pypi == []


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        (Package("kept", "1.0", files=["kept-1.0.whl"], registry="https://pkgs.example.com/simple"), True),
        (Package("onpypi", "1.0", files=["onpypi-1.0.whl"]), False),
        (
            Package("onpypi-explicit", "1.0", files=["onpypi_explicit-1.0.whl"], registry="https://pypi.org/simple"),
            False,
        ),
        (Package("nofiles", None), False),
        (Package("skipped", "1.0", files=["skipped-1.0.whl"], skip="path"), False),
    ],
)
def test_keeps_locked_files(package, expected):
    assert pypi.keeps_locked_files(package) is expected


def test_fill_all_reports_progress_only_when_asked(fake_pypi):
    seen = []
    packages = [Package("purewheel", "3.1"), Package("nowheels", "0.9"), Package("doesnotexist", "1.0")]
    with pypi.reporting_progress(lambda done, total: seen.append((done, total))):
        pypi.fill_all(packages, workers=2)
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert [p.error for p in packages] == [None, None, "not on index"]  # a failed lookup still counts as done
    seen.clear()
    pypi.fill_all([Package("winonly", "311")])  # outside the context nothing is reported, as in the CLI
    assert seen == []
