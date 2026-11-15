import json

import pytest

from wheelgap.pyodide import load_pyodide_lock, parse_pyodide_lock
from wheelgap.scan import DEFAULT_PYTHONS, main, scan_targets


def write_top(tmp_path, names):
    path = tmp_path / "top.json"
    rows = [{"project": n, "download_count": 1000 - i} for i, n in enumerate(names)]
    path.write_text(json.dumps({"last_update": "2026-11-01 06:41:05", "rows": rows}))
    return path


def test_scan_targets_add_pyodide_only_where_it_exists():
    labels = [t.label(True) for t in scan_targets("3.13,3.14t")]
    assert "3.13 pyodide" in labels
    assert "3.14t pyodide" not in labels  # no free-threaded Pyodide
    assert sum(1 for x in labels if x.startswith("3.13 ")) == 9
    assert sum(1 for x in labels if x.startswith("3.14t ")) == 8


def test_scan_targets_removes_duplicate_versions_in_order():
    assert scan_targets("3.14t,3.13,cp314t,3.13") == scan_targets("3.14t,3.13")


def test_scan_defaults_include_python_315(fake_pypi, tmp_path, capsys):
    top = write_top(tmp_path, ["purewheel"])
    assert main(["--top-packages", str(top)]) == 0
    labels = [target["label"] for target in json.loads(capsys.readouterr().out)["targets"]]
    assert labels == [target.label(True) for target in scan_targets(DEFAULT_PYTHONS)]
    for version in ("3.13", "3.14", "3.14t", "3.15", "3.15t"):
        assert f"{version} linux-x86_64" in labels
    assert "3.15 pyodide" not in labels
    assert "3.15t pyodide" not in labels


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--top", "-5", "must be a positive integer"),
        ("--top", "0", "must be a positive integer"),
        ("--workers", "0", "must be a positive integer"),
        ("--workers", "-1", "must be a positive integer"),
        ("--top", "1.5", "must be a positive integer"),
        ("--workers", "abc", "must be a positive integer"),
        ("--max-error-rate", "nan", "must be a number from 0 to 1"),
        ("--max-error-rate", "inf", "must be a number from 0 to 1"),
        ("--max-error-rate", "-0.1", "must be a number from 0 to 1"),
        ("--max-error-rate", "1.5", "must be a number from 0 to 1"),
        ("--max-error-rate", "abc", "must be a number from 0 to 1"),
        ("--python", " , ", "provide at least one Python version"),
        ("--python", "invalid", "can't parse Python version"),
    ],
)
def test_scan_rejects_invalid_arguments_before_loading_data(tmp_path, capsys, flag, value, message):
    with pytest.raises(SystemExit) as error:
        main([flag, value, "--top-packages", str(tmp_path / "missing.json")])
    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_scan_accepts_a_zero_max_error_rate(fake_pypi, tmp_path):
    top = write_top(tmp_path, ["purewheel"])
    out = tmp_path / "coverage.json"
    assert (
        main(["--top", "1", "--python", "3.13", "--top-packages", str(top), "--out", str(out), "--max-error-rate", "0"])
        == 0
    )
    assert json.loads(out.read_text())["lookup_errors"] == 0


def test_scan_writes_the_tracker_data(fake_pypi, tmp_path, capsys):
    top = write_top(tmp_path, ["purewheel", "fastlib", "nowheels", "does-not-exist"])
    out = tmp_path / "coverage.json"
    assert main(["--top", "3", "--python", "3.13,cp313", "--top-packages", str(top), "--out", str(out)]) == 0
    data = json.loads(out.read_text())

    assert data["schema"] == 1
    assert data["top_packages"]["last_update"] == "2026-11-01 06:41:05"
    labels = [t["label"] for t in data["targets"]]
    assert labels[0] == "3.13 linux-x86_64" and labels[-1] == "3.13 pyodide"
    assert [p["name"] for p in data["packages"]] == ["purewheel", "fastlib", "nowheels"]  # --top 3

    by_name = {p["name"]: p for p in data["packages"]}
    assert by_name["purewheel"]["pure"] and set(by_name["purewheel"]["results"]) == {"wheel"}
    fastlib = dict(zip(labels, by_name["fastlib"]["results"], strict=True))
    assert fastlib["3.13 linux-x86_64"] == "wheel"
    assert fastlib["3.13 linux-aarch64"] == "source"
    assert fastlib["3.13 pyodide"] == "missing"  # Pyodide can't build the sdist
    assert by_name["nowheels"]["rank"] == 3 and by_name["nowheels"]["downloads"] == 998

    assert data["summary"]["3.13 linux-x86_64"] == {"wheel": 2, "source": 1, "missing": 0, "unknown": 0, "bundled": 0}
    assert data["summary"]["3.13 pyodide"] == {"wheel": 1, "source": 0, "missing": 2, "unknown": 0, "bundled": 0}
    assert data["pyodide_locks"] == []
    err = capsys.readouterr().err
    assert "scanned 3 packages x 9 targets" in err and "--pyodide-lock" not in err


def test_scan_reports_lookup_errors(fake_pypi, tmp_path, capsys):
    top = write_top(tmp_path, ["does-not-exist"])
    main(["--top", "1", "--python", "3.13", "--top-packages", str(top), "--max-error-rate", "1"])
    out, err = capsys.readouterr()
    pkg = json.loads(out)["packages"][0]
    assert pkg["error"] == "not on index" and set(pkg["results"]) == {"unknown"}
    assert "(1 lookup errors)" in err


def test_scan_refuses_to_write_when_too_many_lookups_fail(fake_pypi, tmp_path, capsys):
    top = write_top(tmp_path, ["does-not-exist", "purewheel"])
    out = tmp_path / "coverage.json"
    assert main(["--top", "2", "--python", "3.13", "--top-packages", str(top), "--out", str(out)]) == 1
    assert not out.exists()  # the last good dataset stays in place
    assert "too many lookup errors (1 of 2)" in capsys.readouterr().err
    assert (
        main(
            ["--top", "2", "--python", "3.13", "--top-packages", str(top), "--out", str(out), "--max-error-rate", "0.5"]
        )
        == 0
    )
    data = json.loads(out.read_text())
    assert data["lookup_errors"] == 1
    assert data["targets"][0]["glibc"] == "2.36" and data["targets"][0]["musl"] == "1.2"


def test_scan_counts_what_pyodide_bundles(fake_pypi, fixtures, tmp_path, capsys):
    top = write_top(tmp_path, ["purewheel", "fastlib", "nowheels", "markupsafe"])
    lock = str(fixtures / "pyodide-lock.json")  # Python 3.13
    args = ["--python", "3.13,3.14", "--top-packages", str(top), "--pyodide-lock", lock, "--max-error-rate", "1"]
    assert main(args) == 0
    out, err = capsys.readouterr()
    data = json.loads(out)
    labels = [t["label"] for t in data["targets"]]
    fastlib = dict(zip(labels, data["packages"][1]["results"], strict=True))
    assert fastlib["3.13 pyodide"] == "bundled" and fastlib["3.14 pyodide"] == "missing"
    assert fastlib["3.13 linux-x86_64"] == "wheel"
    # markupsafe is in the lock but its lookup failed: it stays unknown.
    assert data["summary"]["3.13 pyodide"] == {"wheel": 1, "source": 0, "missing": 1, "unknown": 1, "bundled": 1}
    assert data["summary"]["3.14 pyodide"]["bundled"] == 0
    assert data["pyodide_locks"] == [{"python": "3.13", "pyodide_version": "0.29.0", "source": lock}]
    assert "note: no --pyodide-lock for Python 3.14;" in err


def test_scan_reports_the_bundled_version(fake_pypi, fixtures, tmp_path, capsys):
    top = write_top(tmp_path, ["fastlib", "purewheel"])
    lock = str(fixtures / "pyodide-lock.json")
    assert main(["--python", "3.13", "--top-packages", str(top), "--pyodide-lock", lock]) == 0
    data = json.loads(capsys.readouterr().out)
    labels = [t["label"] for t in data["targets"]]
    assert labels == [t.label(True) for t in scan_targets("3.13")]
    fastlib, purewheel = data["packages"]
    assert fastlib["bundled_version"] == "2.0.0"
    assert fastlib["bundled_versions"] == {"3.13": "2.0.0"}
    assert fastlib["bundled_version_differs"] is False
    assert len(fastlib["results"]) == len(labels)
    assert {"rank", "name", "version", "downloads", "pure", "error", "results"} <= fastlib.keys()
    assert not {"bundled_version", "bundled_versions", "bundled_version_differs"} & purewheel.keys()


def test_scan_reports_when_the_bundled_version_differs(fake_pypi, tmp_path, capsys):
    lock_path = tmp_path / "pyodide-lock-0.30.0.json"
    lock_path.write_text(
        json.dumps(
            {
                "info": {"abi_version": "2025_0", "python": "3.13.2", "version": "0.30.0"},
                "packages": {"fastlib": {"name": "fastlib", "version": "3.0.0"}},
            }
        ),
        encoding="utf-8",
    )
    top = write_top(tmp_path, ["fastlib"])
    args = ["--python", "3.13", "--top-packages", str(top), "--pyodide-lock", str(lock_path)]
    assert main(args) == 0
    package = json.loads(capsys.readouterr().out)["packages"][0]
    assert package["version"] == "2.0.0"
    assert package["bundled_version"] == "3.0.0"
    assert package["bundled_version_differs"] is True


def test_scan_reports_each_locks_bundled_version(fake_pypi, tmp_path, capsys):
    top = write_top(tmp_path, ["fastlib"])
    first = tmp_path / "pyodide-lock-0.29.0.json"
    second = tmp_path / "pyodide-lock-0.30.0.json"
    first.write_text(
        json.dumps(
            {
                "info": {"abi_version": "2025_0", "python": "3.13.2", "version": "0.29.0"},
                "packages": {"fastlib": {"name": "fastlib", "version": "2.0.0"}},
            }
        ),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "info": {"abi_version": "2026_0", "python": "3.14.2", "version": "0.30.0"},
                "packages": {"fastlib": {"name": "fastlib", "version": "3.0.0"}},
            }
        ),
        encoding="utf-8",
    )
    base = ["--python", "3.13,3.14", "--top-packages", str(top)]

    # The locks ship different versions, so there's no single bundled_version, whatever order they're passed in.
    for order in ([first, second], [second, first]):
        locks = [arg for lock in order for arg in ("--pyodide-lock", str(lock))]
        assert main([*base, *locks]) == 0
        package = json.loads(capsys.readouterr().out)["packages"][0]
        assert package["bundled_version"] is None
        assert package["bundled_versions"] == {"3.13": "2.0.0", "3.14": "3.0.0"}
        assert package["bundled_version_differs"] is True  # the scan checked 2.0.0; 3.14's Pyodide ships 3.0.0


def lock_without_release(fixtures):
    data = json.loads((fixtures / "pyodide-lock.json").read_text(encoding="utf-8"))
    del data["info"]["version"]
    return data


def test_pyodide_release_prefers_info_version(fixtures):
    data = lock_without_release(fixtures)
    data["info"]["version"] = "0.29.0"
    lock = parse_pyodide_lock(data, "https://cdn.jsdelivr.net/pyodide/v0.30.0/full/pyodide-lock.json")
    assert lock.pyodide_version == "0.29.0"


@pytest.mark.parametrize(
    "source",
    [
        "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/pyodide-lock.json",
        "https://github.com/pyodide/pyodide/releases/download/0.29.5/pyodide-lock.json",
        "https://example.com/locks/pyodide-lock-0.29.5.json",
    ],
)
def test_pyodide_release_from_url(fixtures, source):
    assert parse_pyodide_lock(lock_without_release(fixtures), source).pyodide_version == "0.29.5"


def test_pyodide_release_is_unknown_when_url_and_filename_disagree(fixtures):
    source = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/pyodide-lock-0.28.0.json"
    assert parse_pyodide_lock(lock_without_release(fixtures), source).pyodide_version == "?"


def test_pyodide_release_ignores_paths_on_other_hosts(fixtures):
    # Only the file name counts away from Pyodide's own release URLs.
    source = "https://mirror.example/pyodide/v0.29.5/full/pyodide-lock-0.28.0.json"
    assert parse_pyodide_lock(lock_without_release(fixtures), source).pyodide_version == "0.28.0"


@pytest.mark.parametrize(
    "name",
    ["pyodide-lock-0.29.5.json", "pyodide-lock.0.29.5.json", "pyodide-0.29.5-lock.json", "0.29.5-pyodide-lock.json"],
)
def test_pyodide_release_from_filename(fixtures, tmp_path, name):
    path = tmp_path / name
    path.write_text(json.dumps(lock_without_release(fixtures)), encoding="utf-8")
    assert load_pyodide_lock(str(path)).pyodide_version == "0.29.5"


@pytest.mark.parametrize(
    "source",
    [
        "pyodide-lock.json",
        "https://example.com/2024/05/pyodide-lock.json",
        "https://example.com/pyodide/v0.29/full/pyodide-lock.json",
        "pyodide-lock-2024-05.json",
        # Another project's release, or a mirror's own numbering: not Pyodide's release.
        "https://github.com/acme/webapp/releases/download/2.1.0/pyodide-lock.json",
        "https://mirror.example/pyodide/1.0.0/pyodide-lock.json",
        "https://cdn.jsdelivr.net/npm/acme/v1.2.3/pyodide-lock.json",
    ],
)
def test_pyodide_release_stays_unknown(fixtures, source):
    assert parse_pyodide_lock(lock_without_release(fixtures), source).pyodide_version == "?"


def test_scan_reads_the_release_from_the_lock_filename(fake_pypi, fixtures, tmp_path, capsys):
    lock = tmp_path / "pyodide-lock-0.29.5.json"
    lock.write_text(json.dumps(lock_without_release(fixtures)), encoding="utf-8")
    top = write_top(tmp_path, ["fastlib"])
    args = ["--python", "3.13", "--top-packages", str(top), "--pyodide-lock", str(lock)]
    assert main(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pyodide_locks"][0]["pyodide_version"] == "0.29.5"


@pytest.mark.parametrize(
    ("spec", "python", "message"),
    [
        ("pyodide-lock.json", "3.14", "no pyodide target uses Python 3.13"),
        ("pyodide-lock.json", "3.13t", "no pyodide target uses Python 3.13"),
        ("missing-lock.json", "3.13", "No such file or directory"),
        ("http://example.com/pyodide-lock.json", "3.13", "plain http:// URLs are not allowed"),
    ],
)
def test_scan_rejects_pyodide_locks_before_loading_data(fixtures, tmp_path, capsys, spec, python, message):
    lock = str(fixtures / spec) if not spec.startswith("http") else spec
    with pytest.raises(SystemExit) as error:
        main(["--python", python, "--pyodide-lock", lock, "--top-packages", str(tmp_path / "missing.json")])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
