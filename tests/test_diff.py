import json
import shutil
import subprocess
import sys

import pytest
from conftest import FAKE_INDEX

from wheelgap.api import Result, make_targets
from wheelgap.check import Status
from wheelgap.diff import Package, compare, git_ref_input, main, merge, worse, worse_on

TARGET = ["-p", "3.13", "-t", "linux-x86_64"]
TWO_TARGETS = ["-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64"]


def write_pair(tmp_path, old, new):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old_path, new_path = tmp_path / "old" / "requirements.txt", tmp_path / "new" / "requirements.txt"
    old_path.write_text(old)
    new_path.write_text(new)
    return str(old_path), str(new_path)


def test_diff_reports_a_package_that_lost_its_wheel(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\npurewheel==3.1\n", "ranged==1.9\npurewheel==3.1\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → 1.9",
        "  linux-x86_64: ✓ → ● source",
        "1 package got worse.",
    ]


def test_diff_reports_a_new_package_with_gaps(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\nNoWheels==0.9\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "NoWheels 0.9 (new)",
        "  linux-x86_64: ● source",
        "1 new package has gaps.",
    ]


def test_diff_ignores_new_packages_with_wheels_and_improvements(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.9\n", "ranged==2.0\npurewheel==3.1\n")
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_matches_packages_by_canonical_name(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "nowheels==0.9\n", "NoWheels==0.9\n")
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_of_identical_files_exits_0(fake_pypi, tmp_path, capsys):
    text = "ranged==1.9\nnowheels==0.9\npurewheel==3.1\n"
    old, new = write_pair(tmp_path, text, text)
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_labels_targets_with_python_when_there_are_several(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64"]) == 1
    assert capsys.readouterr().out.splitlines()[1:3] == [
        "  3.13 linux-x86_64: ✓ → ● source",
        "  3.14 linux-x86_64: ✓ → ● source",
    ]


def test_diff_takes_next(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    assert main([old, new, "-p", "next", "-t", "linux-x86_64", "--json"]) == 1
    out, err = capsys.readouterr()
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.15"]
    assert "wheelgap: note: next is Python 3.15, the newest CPython this wheelgap version knows" in err


# Platform flags (#286): the new release's only Linux wheel is manylinux_2_34, the old one's is manylinux_2_28, so the
# move is fine at the default glibc (2.36) but worse when the deployment floor is lower, as on RHEL 8 (glibc 2.28).
NEW_GLIBC_PAIR = ("glibclag==1.0\n", "glibclag==1.1\n")
GLIBCLAG_WORSE = [
    "glibclag 1.0 → 1.1",
    "  linux-x86_64: ✓ → ● source",
    "1 package got worse.",
]


def test_diff_default_glibc_ignores_a_manylinux_floor_the_target_is_above(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, *NEW_GLIBC_PAIR)
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_glibc_flag_counts_a_lost_manylinux_wheel(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, *NEW_GLIBC_PAIR)
    assert main([old, new, *TARGET, "--glibc", "2.28"]) == 1
    assert capsys.readouterr().out.splitlines() == GLIBCLAG_WORSE


def test_diff_musl_flag_counts_a_lost_musllinux_wheel(fake_pypi, tmp_path, capsys):
    # The new release's only musl wheel is musllinux_1_2, the old one's is musllinux_1_1.
    old, new = write_pair(tmp_path, "musllag==1.0\n", "musllag==1.1\n")
    assert main([old, new, "-p", "3.13", "-t", "musl-x86_64"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    assert main([old, new, "-p", "3.13", "-t", "musl-x86_64", "--musl", "1.1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "musllag 1.0 → 1.1",
        "  musl-x86_64: ✓ → ● source",
        "1 package got worse.",
    ]


def test_diff_macos_flag_counts_a_wheel_above_the_floor(fake_pypi, tmp_path, capsys):
    # The new release's only macOS wheel is macosx_14_0, the old one's is macosx_13_0.
    old, new = write_pair(tmp_path, "maclag==1.0\n", "maclag==1.1\n")
    assert main([old, new, "-p", "3.13", "-t", "macos-arm64"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    assert main([old, new, "-p", "3.13", "-t", "macos-arm64", "--macos", "13.0"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "maclag 1.0 → 1.1",
        "  macos-arm64: ✓ → ● source",
        "1 package got worse.",
    ]


def test_diff_ios_flag_counts_a_wheel_above_the_floor(fake_pypi, tmp_path, capsys, monkeypatch):
    # The new release's only iOS wheel is ios_15_0, above the default 13.0 floor.
    monkeypatch.setitem(
        FAKE_INDEX,
        "ioslag",
        {
            "1.0": ["ioslag-1.0-cp313-cp313-ios_13_0_arm64_iphoneos.whl", "ioslag-1.0.tar.gz"],
            "1.1": ["ioslag-1.1-cp313-cp313-ios_15_0_arm64_iphoneos.whl", "ioslag-1.1.tar.gz"],
        },
    )
    old, new = write_pair(tmp_path, "ioslag==1.0\n", "ioslag==1.1\n")
    assert main([old, new, "-p", "3.13", "-t", "ios-arm64"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ioslag 1.0 → 1.1",
        "  ios-arm64: ✓ → ✗ missing",
        "1 package got worse.",
    ]
    assert main([old, new, "-p", "3.13", "-t", "ios-arm64", "--ios", "15.0"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    assert main([old, new, "-p", "3.13", "-t", "ios-arm64", "--ios", "not-a-version"]) == 2
    assert "wheelgap: error:" in capsys.readouterr().err


def test_diff_android_api_flag_counts_a_wheel_above_the_floor(fake_pypi, tmp_path, capsys, monkeypatch):
    # The new release's only Android wheel is android_26, above the default API 24 floor.
    monkeypatch.setitem(
        FAKE_INDEX,
        "androidlag",
        {
            "1.0": ["androidlag-1.0-cp313-cp313-android_24_arm64_v8a.whl", "androidlag-1.0.tar.gz"],
            "1.1": ["androidlag-1.1-cp313-cp313-android_26_arm64_v8a.whl", "androidlag-1.1.tar.gz"],
        },
    )
    old, new = write_pair(tmp_path, "androidlag==1.0\n", "androidlag==1.1\n")
    assert main([old, new, "-p", "3.13", "-t", "android-arm64"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "androidlag 1.0 → 1.1",
        "  android-arm64: ✓ → ✗ missing",
        "1 package got worse.",
    ]
    assert main([old, new, "-p", "3.13", "-t", "android-arm64", "--android-api", "26"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    assert main([old, new, "-p", "3.13", "-t", "android-arm64", "--android-api", "abc"]) == 2
    assert "wheelgap: error:" in capsys.readouterr().err


def test_diff_distro_supplies_the_libc_version(fake_pypi, tmp_path, capsys):
    # rhel-8 is glibc 2.28, so it must see what --glibc 2.28 sees; the alias rocky-8 resolves the same way.
    old, new = write_pair(tmp_path, *NEW_GLIBC_PAIR)
    assert main([old, new, *TARGET, "--distro", "rhel-8"]) == 1
    assert capsys.readouterr().out.splitlines() == GLIBCLAG_WORSE
    assert main([old, new, *TARGET, "--distro", "rocky-8", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    (item,) = data["worse"]
    assert item["name"] == "glibclag" and item["targets"]["linux-x86_64"]["new_status"] == "source"


def test_diff_distro_does_not_select_targets(fake_pypi, tmp_path, capsys):
    # --distro supplies a libc version only: with -t win-amd64 the Linux glibc floor is never checked (glibclag is
    # source on both sides there). Had --distro selected linux-x86_64, the move would be reported.
    old, new = write_pair(tmp_path, *NEW_GLIBC_PAIR)
    assert main([old, new, "-p", "3.13", "-t", "win-amd64", "--distro", "rhel-8"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_explicit_libc_wins_over_distro(fake_pypi, tmp_path, capsys):
    glibc_dir = tmp_path / "glibc"
    musl_dir = tmp_path / "musl"
    glibc_dir.mkdir()
    musl_dir.mkdir()
    old, new = write_pair(glibc_dir, *NEW_GLIBC_PAIR)
    assert main([old, new, *TARGET, "--distro", "ubuntu-24.04", "--glibc", "2.28"]) == 1
    assert capsys.readouterr().out.splitlines() == GLIBCLAG_WORSE
    old_m, new_m = write_pair(musl_dir, "musllag==1.0\n", "musllag==1.1\n")
    assert main([old_m, new_m, "-p", "3.13", "-t", "musl-x86_64", "--distro", "alpine", "--musl", "1.1"]) == 1
    assert "musllag 1.0 → 1.1" in capsys.readouterr().out


def test_diff_unknown_distro_exits_2(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\n")
    assert main([old, new, *TARGET, "--distro", "not-a-distro"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("wheelgap: error: unknown distro 'not-a-distro'")


def test_diff_counts_a_package_it_can_no_longer_check_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==9.9\n")  # 9.9 isn't on the index
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines()[:2] == ["ranged 1.5 → 9.9", "  linux-x86_64: ✓ → ? unknown"]


def test_diff_notes_packages_unknown_on_both_sides(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "does-not-exist==1.0\n", "does-not-exist==1.0\n")
    assert main([old, new, *TARGET]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["Nothing got worse."]
    assert "couldn't check does-not-exist in either lockfile" in err


def test_diff_counts_unknown_at_a_new_version_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "does-not-exist==1.0\n", "does-not-exist==2.0\n")
    assert main([old, new, *TARGET]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "does-not-exist 1.0 → 2.0",
        "  linux-x86_64: ? unknown → ? unknown",
        "1 package got worse.",
    ]
    assert "couldn't check" not in err


@pytest.mark.parametrize("index", ["https://pypi.org/simple", "https://mirror.example.com/simple/"])
@pytest.mark.parametrize(
    "pair, flags",
    [
        (("ranged==1.5\n", "ranged==1.9\n"), []),
        (("ranged==1.5\n", "ranged==1.9\n"), ["--json"]),
        (None, ["--deps"]),
    ],
)
def test_diff_refuses_a_simple_wheelgap_index_url_before_any_lookup(
    fake_pypi, tmp_path, capsys, monkeypatch, index, pair, flags
):
    from wheelgap.cli import main as cli_main

    monkeypatch.setenv("WHEELGAP_INDEX_URL", index)
    assert cli_main(["purewheel==3.1", *TARGET]) == 2
    main_error = capsys.readouterr().err.strip().splitlines()[-1]
    error, _ = main_error.removeprefix("wheelgap: error: ").split(" To use a simple index", 1)
    old, new = write_pair(tmp_path, *pair) if pair else ("ranged==1.5", "ranged==1.9")
    assert main([old, new, *TARGET, *flags]) == 2
    out, err = capsys.readouterr()
    assert fake_pypi == []
    # The main command's error and JSON API advice; its --index-url advice is for a flag diff doesn't have.
    message = (
        f"{error} wheelgap diff can't use a simple index (it has no --index-url): "
        "check each lockfile with wheelgap --index-url instead."
    )
    assert error.startswith("WHEELGAP_INDEX_URL points at a simple index") and "JSON API" in error
    assert err == f"wheelgap: error: {message}\n"
    if "--json" in flags:
        assert json.loads(out)["error"] == {"message": message, "type": "SourceError"}
    else:
        assert out == ""


def test_diff_counts_a_package_now_installed_from_git_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged @ git+https://github.com/example/ranged@v1.9\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → (url)",
        "  linux-x86_64: ✓ → skipped (url)",
        "1 package got worse.",
    ]


def test_diff_counts_a_package_now_from_a_private_index_as_worse(fake_pypi, tmp_path, capsys):
    old, _ = write_pair(tmp_path, "ranged==1.5\n", "")
    new = tmp_path / "new" / "Pipfile.lock"
    lock = {
        "_meta": {"sources": [{"name": "internal", "url": "https://pkgs.example.com/simple"}]},
        "default": {"ranged": {"version": "==1.9", "index": "internal"}},
    }
    new.write_text(json.dumps(lock), encoding="utf-8")
    assert main([old, str(new), *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → 1.9",
        "  linux-x86_64: ✓ → skipped (private index)",
        "1 package got worse.",
    ]


@pytest.mark.parametrize(
    "new",
    [
        "purewheel==3.1\n",  # removed
        'ranged @ git+https://github.com/example/ranged@v1.9 ; sys_platform == "win32"\n',  # not on this target
    ],
)
def test_diff_ignores_a_package_new_no_longer_installs(fake_pypi, tmp_path, capsys, new):
    old, new = write_pair(tmp_path, "ranged==1.5\npurewheel==3.1\n", new)
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_compares_a_package_both_checked_and_skipped_per_target(fake_pypi, tmp_path, capsys):
    new = (
        'ranged==1.5 ; sys_platform == "win32"\n'
        'ranged @ git+https://github.com/example/ranged@v1 ; sys_platform == "linux"\n'
    )
    old, new = write_pair(tmp_path, "ranged==1.5\n", new)
    assert main([old, new, *TWO_TARGETS]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5",
        "  linux-x86_64: ✓ → skipped (url)",
        "1 package got worse.",
    ]


def test_diff_reports_a_new_skipped_package_as_a_new_gap(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\nfoo @ git+https://github.com/example/foo@v1\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "foo (url) (new)",
        "  linux-x86_64: skipped (url)",
        "1 new package has gaps.",
    ]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("foo @ git+https://github.com/example/foo@v1\n", "foo @ git+https://github.com/example/foo@v2\n"),
        ("-e ./foo\n", "-e ./foo\n"),
    ],
)
def test_diff_leaves_a_package_skipped_on_both_sides_alone(fake_pypi, tmp_path, capsys, old, new):
    old, new = write_pair(tmp_path, old, new)
    assert main([old, new, *TARGET]) == 0


def test_diff_matches_a_requirements_path_by_its_path(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "./ranged\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "./ranged (path) (new)",
        "  linux-x86_64: skipped (path)",
        "1 new package has gaps.",
    ]


UV_HEAD = 'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
UV_PUREWHEEL = (
    '[[package]]\nname = "purewheel"\nversion = "3.1"\nsource = { registry = "https://pypi.org/simple" }\n'
    'wheels = [\n    { url = "https://files.pythonhosted.org/packages/aa/purewheel-3.1-py3-none-any.whl", '
    'hash = "sha256:00", size = 1 },\n]\n\n'
)
GIT_SOURCE = '{ git = "https://github.com/e/purewheel?rev=abc#abc" }'
UV_GIT = f'[[package]]\nname = "purewheel"\nversion = "3.2"\nsource = {GIT_SOURCE}\n\n'


def uv_lock(deps):
    root = '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { editable = "." }\ndependencies = [\n'
    return UV_HEAD + root + "".join(f"    {d},\n" for d in deps) + "]\n\n"


def test_diff_follows_the_uv_lock_graph_for_skipped_packages(fake_pypi, tmp_path, capsys):
    # The git build is only reached on Linux (the marker is on the edge, not the package), so Windows is unchanged.
    old = uv_lock(['{ name = "purewheel" }']) + UV_PUREWHEEL
    new = (
        uv_lock(
            [
                """{ name = "purewheel", version = "3.1", source = { registry = "https://pypi.org/simple" }, """
                """marker = "sys_platform == 'win32'" }""",
                f"""{{ name = "purewheel", version = "3.2", source = {GIT_SOURCE}, """
                """marker = "sys_platform != 'win32'" }""",
            ]
        )
        + UV_PUREWHEEL
        + UV_GIT
    )
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    (tmp_path / "old" / "uv.lock").write_text(old)
    (tmp_path / "new" / "uv.lock").write_text(new)
    assert main([str(tmp_path / "old"), str(tmp_path / "new"), *TWO_TARGETS]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "purewheel 3.1 → 3.1, 3.2",
        "  linux-x86_64: ✓ → skipped (git)",
        "1 package got worse.",
    ]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((Status.WHEEL, Status.SOURCE), Status.SOURCE),
        ((Status.MISSING, Status.SOURCE), Status.MISSING),
        ((Status.MISSING, Status.UNKNOWN), Status.UNKNOWN),
        ((Status.NOT_NEEDED, Status.WHEEL), Status.WHEEL),
        ((Status.NOT_NEEDED, Status.NOT_NEEDED), Status.NOT_NEEDED),
    ],
)
def test_merge_takes_the_worst_status_per_target(statuses, expected):
    (t,) = make_targets("3.13", "linux-x86_64")
    results = [Result("Ranged", str(i), {t: s}) for i, s in enumerate(statuses)]
    (pkg,) = merge(results, [t]).values()
    assert pkg.statuses[t] is expected
    assert pkg.version == "0, 1"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (Status.WHEEL, Status.SOURCE, True),
        (Status.SOURCE, Status.MISSING, True),
        (Status.NOT_NEEDED, Status.SOURCE, True),
        (Status.BUNDLED, Status.WHEEL, False),
        (Status.WHEEL, Status.NOT_NEEDED, False),
        (Status.MISSING, Status.SOURCE, False),
        (Status.SOURCE, Status.SOURCE, False),
        # unknown isn't ranked against the others: only a known-good result after it is "not worse".
        (Status.WHEEL, Status.UNKNOWN, True),
        (Status.MISSING, Status.UNKNOWN, True),
        (Status.UNKNOWN, Status.SOURCE, True),
        (Status.UNKNOWN, Status.MISSING, True),
        (Status.UNKNOWN, Status.WHEEL, False),
        (Status.UNKNOWN, Status.NOT_NEEDED, False),
        (Status.UNKNOWN, Status.UNKNOWN, False),
    ],
)
def test_worse(old, new, expected):
    assert worse(old, new) is expected


INDEX_URL_CONFIG = '[tool.wheelgap]\nindex-url = "https://nonexistent.invalid/simple"\n'
INDEX_URL_REFUSAL = (
    "sets index-url in [tool.wheelgap], which wheelgap diff doesn't read yet, so it would check PyPI instead of that "
    "index; check each lockfile with wheelgap instead, which uses it"
)


@pytest.mark.parametrize("side", ["old", "new"])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_diff_refuses_index_url_in_either_project_before_any_lookup(fake_pypi, tmp_path, capsys, side, flags):
    # The main command would check these packages on that index; diff checked PyPI and said "Nothing got worse."
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\n")
    pyproject = tmp_path / side / "pyproject.toml"
    pyproject.write_text(INDEX_URL_CONFIG, encoding="utf-8")
    assert main([old, new, *TARGET, *flags]) == 2
    out, err = capsys.readouterr()
    assert fake_pypi == []
    where = str(pyproject) if side == "new" else f"the pyproject.toml beside {old}"
    assert err == f"wheelgap: error: {where} {INDEX_URL_REFUSAL}\n"
    assert "nonexistent" not in err  # the URL may hold credentials
    if flags:
        assert json.loads(out)["error"] == {"message": f"{where} {INDEX_URL_REFUSAL}", "type": "SourceError"}
    else:
        assert out == ""


def test_diff_deps_refuses_index_url_in_the_project(fake_pypi, tmp_path, capsys, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(INDEX_URL_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["ranged==1.5", "ranged==1.9", "--deps", *TARGET]) == 2
    assert fake_pypi == [] and INDEX_URL_REFUSAL in capsys.readouterr().err


STATUSES = [Status.WHEEL, Status.BUNDLED, Status.NOT_NEEDED, Status.SOURCE, Status.MISSING, Status.UNKNOWN, "skipped"]
# Every (old, new) pair that counts as worse at any version (and unknown → unknown at a new one): the rest don't.
WORSE_PAIRS = {
    *((old, new) for old in (Status.WHEEL, Status.BUNDLED, Status.NOT_NEEDED) for new in STATUSES[3:]),
    *((Status.SOURCE, new) for new in STATUSES[4:]),
    (Status.MISSING, Status.UNKNOWN),
    (Status.MISSING, "skipped"),
    *((old, new) for old in (Status.UNKNOWN, "skipped") for new in (Status.SOURCE, Status.MISSING)),
    (Status.UNKNOWN, "skipped"),
    ("skipped", Status.UNKNOWN),
}


@pytest.mark.parametrize("new_version", ["1.0", "2.0"])
@pytest.mark.parametrize("new", STATUSES, ids=str)
@pytest.mark.parametrize("old", STATUSES, ids=str)
def test_worse_on_every_pair(old, new, new_version):
    (t,) = make_targets("3.13", "linux-x86_64")

    def package(status, version):
        if status == "skipped":
            return Package("x", [version], {t: Status.NOT_NEEDED}, skips={t: "git"})
        return Package("x", [version], {t: status})

    expected = (old, new) in WORSE_PAIRS or (old is new is Status.UNKNOWN and new_version != "1.0")
    assert worse_on(package(old, "1.0"), package(new, new_version), t) is expected


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["{tmp}/missing.txt", "{new}"], "missing.txt"),
        (["{tmp}/empty", "{new}"], "no lockfile found"),
        (["{tmp}/notes.md", "{new}"], "don't know how to read"),
        (["{tmp}/pyproject.toml", "{new}"], "pyproject.toml: a pyproject.toml lists direct dependencies only"),
        (["{new}", "{new}", "-t", "linux-aarch46"], "unknown target 'linux-aarch46'"),
        (["{new}", "{new}", "-p", "three"], "can't parse Python version"),
    ],
)
def test_diff_bad_input_exits_2(fake_pypi, tmp_path, capsys, args, message):
    (tmp_path / "notes.md").write_text("hi\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["purewheel==3.1"]\n')
    (tmp_path / "empty").mkdir()
    new = tmp_path / "requirements.txt"
    new.write_text("purewheel==3.1\n")
    argv = [a.format(tmp=tmp_path, new=new) for a in args]
    if "-p" not in argv:
        argv += ["-p", "3.13"]
    assert main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("wheelgap: error: ") and message in err


@pytest.mark.parametrize("side", ["old", "new"])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_diff_missing_file_names_it_as_typed(fake_pypi, tmp_path, capsys, monkeypatch, side, flags):
    # As the main command says it, with the argument as typed: not FileNotFoundError and an absolute path (#728).
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    sides = ["nope.txt", "requirements.txt"] if side == "old" else ["requirements.txt", "nope.txt"]
    assert main([*sides, *TARGET, *flags]) == 2
    out, err = capsys.readouterr()
    assert err == "wheelgap: error: nope.txt: no such file\n"
    if flags:
        assert json.loads(out)["error"] == {"message": "nope.txt: no such file", "type": "SourceError"}
    else:
        assert out == ""
    assert fake_pypi == []  # and neither side is looked up


def test_diff_json(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\npurewheel==3.1\n", "ranged==1.9\nNoWheels==0.9\n")
    assert main([old, new, *TWO_TARGETS, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [t["label"] for t in data["targets"]] == ["linux-x86_64", "win-amd64"]
    assert data["worse"] == [
        {
            "name": "ranged",
            "old_versions": ["1.5"],
            "new_versions": ["1.9"],
            "targets": {
                "linux-x86_64": {"old_status": "wheel", "new_status": "source"},
                "win-amd64": {"old_status": "wheel", "new_status": "source"},
            },
        }
    ]
    assert data["new"] == [
        {
            "name": "NoWheels",
            "versions": ["0.9"],
            "targets": {"linux-x86_64": {"status": "source"}, "win-amd64": {"status": "source"}},
        }
    ]
    assert data["summary"] == {"worse": 1, "new": 1} and data["unchecked"] == []


def test_diff_json_nothing_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.9\n", "ranged==2.0\n")
    assert main([old, new, *TARGET, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worse"] == [] and data["new"] == [] and data["summary"] == {"worse": 0, "new": 0}


def test_diff_json_skipped_cells_say_why(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel @ git+https://example.com/purewheel\n")
    assert main([old, new, *TARGET, "--json"]) == 1
    (item,) = json.loads(capsys.readouterr().out)["worse"]
    assert item["old_versions"] == ["3.1"] and item["new_versions"] == []  # not "(url)"
    assert item["targets"]["linux-x86_64"] == {"old_status": "wheel", "new_status": "skipped", "new_skip_reason": "url"}


def test_diff_json_bad_input(fake_pypi, tmp_path, capsys):
    assert main([str(tmp_path / "nope.lock"), str(tmp_path / "nope.lock"), *TARGET, "--json"]) == 2
    out, err = capsys.readouterr()
    error = json.loads(out)["error"]
    assert error["type"] and "nope.lock" in error["message"] and err.startswith("wheelgap: error:")


def test_wheelgap_diff_subcommand(fake_pypi, tmp_path, capsys, monkeypatch):
    from wheelgap.cli import main as cli_main

    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    monkeypatch.chdir(tmp_path)
    assert cli_main(["diff", old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "1 package got worse."
    with pytest.raises(SystemExit):
        cli_main(["diff", "--help"])
    assert "wheelgap diff" in capsys.readouterr().out


def test_a_folder_named_diff_is_still_an_input(fake_pypi, tmp_path, capsys, monkeypatch):
    from wheelgap.cli import main as cli_main

    (tmp_path / "diff").mkdir()
    (tmp_path / "diff" / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli_main(["diff", *TARGET]) == 0  # the subcommand would exit 2: it needs OLD and NEW
    assert "reading ./diff as an input" in capsys.readouterr().err


def test_diff_json_unknown_version_is_an_empty_list(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\nno-such-package\n")
    assert main([old, new, *TARGET, "--json"]) == 1
    (item,) = json.loads(capsys.readouterr().out)["new"]
    assert item["versions"] == [] and item["targets"]["linux-x86_64"] == {"status": "unknown"}


# REF:PATH inputs (#199): `wheelgap diff origin/main:uv.lock uv.lock` reads the old side with `git show`.
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")
UV_OLD = uv_lock(['{ name = "purewheel" }']) + UV_PUREWHEEL
UV_NEW = (
    uv_lock(
        [
            """{ name = "purewheel", version = "3.1", source = { registry = "https://pypi.org/simple" }, """
            """marker = "sys_platform == 'win32'" }""",
            f"""{{ name = "purewheel", version = "3.2", source = {GIT_SOURCE}, """
            """marker = "sys_platform != 'win32'" }""",
        ]
    )
    + UV_PUREWHEEL
    + UV_GIT
)


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo whose HEAD has UV_OLD as uv.lock, with UV_NEW in the working copy."""
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "uv.lock").write_text(UV_OLD)
    git("add", "uv.lock", cwd=tmp_path)
    git("commit", "-q", "-m", "old", cwd=tmp_path)
    (tmp_path / "uv.lock").write_text(UV_NEW)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@needs_git
def test_diff_reads_a_git_ref(fake_pypi, repo, capsys):
    assert main(["HEAD:uv.lock", "uv.lock", *TWO_TARGETS]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "purewheel 3.1 → 3.1, 3.2",
        "  linux-x86_64: ✓ → skipped (git)",
        "1 package got worse.",
    ]
    # The same ref on both sides: nothing changed, and no temporary file is left in the repo.
    assert main(["HEAD:uv.lock", "HEAD:./uv.lock", *TWO_TARGETS]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    assert sorted(p.name for p in repo.iterdir()) == [".git", "uv.lock"]


@needs_git
def test_diff_reads_a_git_ref_json(fake_pypi, repo, capsys):
    assert main(["HEAD:uv.lock", "uv.lock", *TWO_TARGETS, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in data["worse"]] == ["purewheel"]
    assert data["summary"] == {"worse": 1, "new": 0}


@needs_git
@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("nosuchbranch:uv.lock", "invalid object name 'nosuchbranch'"),
        ("HEAD:poetry.lock", "path 'poetry.lock' does not exist in 'HEAD'"),
        # A ref that looks like an option stays a ref: git doesn't write the file.
        ("--output=out.txt:uv.lock", "can't read --output=out.txt:uv.lock from git"),
    ],
)
def test_diff_bad_git_ref_exits_2(fake_pypi, repo, capsys, ref, message):
    assert main([*TARGET, "--", ref, "uv.lock"]) == 2  # `--`: argparse would read --output=... as its own option
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("wheelgap: error: GitInputError: ") and message in err
    assert sorted(p.name for p in repo.iterdir()) == [".git", "uv.lock"]  # git wrote nothing


@needs_git
def test_diff_bad_git_ref_json_error(fake_pypi, repo, capsys):
    assert main(["nosuchbranch:uv.lock", "uv.lock", *TARGET, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "GitInputError"


@needs_git
def test_diff_refuses_index_url_in_the_ref_copy(fake_pypi, repo, capsys):
    # The pyproject.toml at the ref comes along with its lockfile, so its index-url counts, as for a file.
    (repo / "pyproject.toml").write_text(INDEX_URL_CONFIG, encoding="utf-8")
    git("add", "pyproject.toml", cwd=repo)
    git("commit", "-q", "-m", "index", cwd=repo)
    (repo / "pyproject.toml").unlink()
    assert main(["HEAD:uv.lock", "uv.lock", *TARGET]) == 2
    assert fake_pypi == []
    assert capsys.readouterr().err == (f"wheelgap: error: the pyproject.toml beside HEAD:uv.lock {INDEX_URL_REFUSAL}\n")


@needs_git
def test_diff_names_a_ref_copy_whose_tool_isnt_a_table_as_typed(fake_pypi, repo, capsys):
    (repo / "pyproject.toml").write_text("tool = 1\n", encoding="utf-8")
    git("add", "pyproject.toml", cwd=repo)
    git("commit", "-q", "-m", "tool", cwd=repo)
    (repo / "pyproject.toml").unlink()
    assert main(["HEAD:uv.lock", "uv.lock", *TARGET]) == 2
    assert fake_pypi == []
    assert (
        capsys.readouterr().err == "wheelgap: error: the pyproject.toml beside HEAD:uv.lock: [tool] must be a table\n"
    )


def test_diff_git_ref_without_git_exits_2(fake_pypi, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert main(["origin/main:uv.lock", str(tmp_path), *TARGET]) == 2
    assert "origin/main:uv.lock looks like a git ref, but git isn't on PATH" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("origin/main:uv.lock", ("origin/main", "uv.lock")),
        ("HEAD~1:sub/dir/poetry.lock", ("HEAD~1", "sub/dir/poetry.lock")),
        ("uv.lock", None),
        ("HEAD:", None),
        (":uv.lock", None),
        ("HEAD:sub/", None),
        ("C:\\proj\\uv.lock", None),
        ("C:/proj/uv.lock", None),
    ],
)
def test_git_ref_input(arg, expected):
    assert git_ref_input(arg) == expected


@pytest.mark.skipif(
    sys.platform == "win32", reason="':' isn't allowed in a Windows file name (a:uv.lock is on drive A:)"
)
def test_git_ref_input_prefers_an_existing_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a:uv.lock").mkdir()
    assert git_ref_input("a:uv.lock") is None


POETRY_1X_LOCK = """\
[[package]]
name = "click"
version = "8.1.7"
description = ""
optional = false
python-versions = ">=3.7"
files = [{file = "click-8.1.7-py3-none-any.whl", hash = "sha256:00"}]

[package.dependencies]
colorama = {version = "*", markers = "platform_system == \\"Windows\\""}

[[package]]
name = "colorama"
version = "0.4.6"
description = ""
optional = false
python-versions = "*"
files = [{file = "colorama-0.4.6.tar.gz", hash = "sha256:00"}]

[metadata]
lock-version = "2.0"
python-versions = "^3.11"
content-hash = "0"
"""


@needs_git
def test_diff_git_ref_brings_the_pyproject_along(fake_pypi, tmp_path, monkeypatch, capsys):
    # A Poetry lock older than 2.1 takes its direct dependencies (here colorama, which click only needs on
    # Windows) from the pyproject.toml beside it. Without it at the ref, colorama would look like a new gap.
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "poetry.lock").write_text(POETRY_1X_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.11"\nclick = "*"\ncolorama = "*"\n', encoding="utf-8"
    )
    git("add", "poetry.lock", "pyproject.toml", cwd=tmp_path)
    git("commit", "-q", "-m", "lock", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["HEAD:poetry.lock", "poetry.lock", *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


@needs_git
def test_diff_git_ref_brings_uv_toml_along(fake_pypi, tmp_path, monkeypatch, capsys):
    # uv.toml's no-build makes an sdist-only package missing on both sides; left behind, the old side would say
    # source and the new one missing, which isn't a change.
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.11"\n\n[[package]]\nname = "puresdist"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.pythonhosted.org/packages/puresdist-1.0.tar.gz", hash = "sha256:00" }\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.toml").write_text("no-build = true\n", encoding="utf-8")
    git("add", "uv.lock", "uv.toml", cwd=tmp_path)
    git("commit", "-q", "-m", "lock", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["HEAD:uv.lock", "uv.lock", *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_reports_origins_only_for_worsened_targets():
    targets = make_targets("3.13", ["linux-x86_64", "win-amd64"])
    linux, windows = targets
    was = merge([Result("transitive", "1.0", {linux: Status.WHEEL, windows: Status.WHEEL})], targets)
    now = merge(
        [
            Result(
                "transitive",
                "1.1",
                {linux: Status.SOURCE, windows: Status.WHEEL},
                direct={linux: ("parent",), windows: ("another-parent",)},
            )
        ],
        targets,
    )
    report = compare(was, now, targets)
    assert report.lines[0] == "transitive (via parent) 1.0 → 1.1"
    assert report.worse[0]["direct"] == {"linux-x86_64": ["parent"]}
    # No hint for unchanged targets, even if they have provenance.
    assert "win-amd64" not in report.worse[0]["direct"]


def test_diff_hint_keeps_the_lockfile_spelling_and_counts_other_parents():
    (target,) = make_targets("3.13", "linux-x86_64")
    now = merge(
        [Result("NoWheels", "0.9", {target: Status.SOURCE}, direct={target: ("Parent_A", "parent-b")})],
        [target],
    )
    report = compare({}, now, [target])
    assert report.lines[0] == "NoWheels (via Parent_A +1) 0.9 (new)"
    assert report.new[0]["direct"] == {"linux-x86_64": ["Parent_A", "parent-b"]}


def test_diff_direct_package_does_not_invent_provenance():
    (target,) = make_targets("3.13", "linux-x86_64")
    report = compare(
        {},
        merge([Result("direct", "1.0", {target: Status.SOURCE})], [target]),
        [target],
    )
    assert report.lines[0] == "direct 1.0 (new)"
    assert "direct" not in report.new[0]


def test_diff_uv_lock_new_transitive_gap_names_its_direct_owner(fake_pypi, tmp_path, capsys):
    old = uv_lock(['{ name = "purewheel" }']) + UV_PUREWHEEL
    newly_depends = UV_PUREWHEEL.replace("wheels = [", 'dependencies = [{ name = "NoWheels" }]\nwheels = [', 1)
    no_wheels = (
        '[[package]]\nname = "NoWheels"\nversion = "0.9"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.pythonhosted.org/packages/nowheels-0.9.tar.gz", '
        'hash = "sha256:00", size = 1 }\n'
    )
    new = uv_lock(['{ name = "purewheel" }']) + newly_depends + no_wheels
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "uv.lock").write_text(old, encoding="utf-8")
    (new_dir / "uv.lock").write_text(new, encoding="utf-8")
    assert main([str(old_dir), str(new_dir), *TARGET, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["new"]) == 1
    assert report["new"][0]["direct"] == {"linux-x86_64": ["purewheel"]}
    assert main([str(old_dir), str(new_dir), *TARGET]) == 1
    assert "NoWheels (via purewheel) 0.9 (new)" in capsys.readouterr().out


def test_diff_reads_tool_wheelgap_config_and_cli_flags_win(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, *NEW_GLIBC_PAIR)
    # Put [tool.wheelgap] beside the NEW input, including keys diff ignores (ignore, advisory, groups).
    (tmp_path / "new" / "pyproject.toml").write_text(
        "[tool.wheelgap]\n"
        'python = ["3.13"]\n'
        'targets = ["linux-x86_64"]\n'
        'distro = "rhel-8"\n'
        'ignore = ["somepkg"]\n'
        'advisory = ["win-arm64"]\n'
        'groups = ["dev"]\n',
        encoding="utf-8",
    )
    # Config targets + distro (rhel-8 = glibc 2.28) apply with no -p / -t / --distro flags.
    assert main([old, new]) == 1
    assert capsys.readouterr().out.splitlines() == GLIBCLAG_WORSE
    # Explicit --glibc wins over config distro.
    assert main([old, new, "--glibc", "2.36"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]
    # Explicit -t wins over config targets.
    assert main([old, new, "-t", "win-amd64"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_reads_python_version_file(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    (tmp_path / "new" / ".python-version").write_text("3.13.2\n", encoding="utf-8")
    assert main([old, new, "-t", "linux-x86_64", "--json"]) == 1
    assert [t["python"] for t in json.loads(capsys.readouterr().out)["targets"]] == ["3.13"]
    # Explicit -p wins over .python-version.
    assert main([old, new, "-p", "3.14", "-t", "linux-x86_64", "--json"]) == 1
    assert [t["python"] for t in json.loads(capsys.readouterr().out)["targets"]] == ["3.14"]


def test_diff_reads_uv_required_environments_as_default_targets(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    (tmp_path / "new" / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = [\"sys_platform == 'win32' and platform_machine == 'AMD64'\"]\n",
        encoding="utf-8",
    )
    assert main([old, new, "-p", "3.13", "--json"]) == 1
    out, err = capsys.readouterr()
    assert [t["platform"] for t in json.loads(out)["targets"]] == ["win-amd64"]
    assert "[tool.uv] required-environments" in err


@pytest.mark.parametrize(
    "config, flags, expected",
    [
        ('python = ["3.13"]\ntargets = ["linux-aarch64"]', [], [("3.13", "linux-aarch64")]),
        ('python = ["3.13"]\ntargets = ["linux-aarch64"]', ["-t", "win-amd64"], [("3.13", "win-amd64")]),
        ('python = ["3.13"]\ntargets = ["linux-aarch64"]', ["-p", "3.14"], [("3.14", "linux-aarch64")]),
    ],
)
def test_diff_uses_new_project_defaults(fake_pypi, tmp_path, capsys, config, flags, expected):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\n")
    (tmp_path / "new" / "pyproject.toml").write_text("[tool.wheelgap]\n" + config, encoding="utf-8")
    # OLD's project settings are never read: only NEW's.
    (tmp_path / "old" / "pyproject.toml").write_text('[tool.wheelgap]\npython = ["3.12"]', encoding="utf-8")
    assert main([old, new, *flags, "--json"]) == 0
    targets = json.loads(capsys.readouterr().out)["targets"]
    assert [(t["python"], t["platform"]) for t in targets] == expected


def test_diff_config_python_wins_over_python_version_file(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\n")
    (tmp_path / "new" / ".python-version").write_text("3.12\n", encoding="utf-8")
    (tmp_path / "new" / "pyproject.toml").write_text('[tool.wheelgap]\npython = ["3.13"]\n', encoding="utf-8")
    assert main([old, new, "-t", "win-amd64", "--json"]) == 0
    assert [t["python"] for t in json.loads(capsys.readouterr().out)["targets"]] == ["3.13"]


def test_diff_rejects_malformed_project_config(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\n")
    (tmp_path / "new" / "pyproject.toml").write_text("[tool.wheelgap]\ntargets = 12", encoding="utf-8")
    assert main([old, new, *TARGET, "--json"]) == 2
    out, err = capsys.readouterr()
    assert "targets must be a list of strings" in err
    assert json.loads(out)["error"]["type"] == "SourceError"


@pytest.mark.parametrize(
    "config, flags, pair, expected_code",
    [
        ('glibc = "2.28"', [], NEW_GLIBC_PAIR, 1),
        ('glibc = "2.28"', ["--glibc", "2.36"], NEW_GLIBC_PAIR, 0),
        ('glibc = "2.36"', ["--distro", "rhel-8"], NEW_GLIBC_PAIR, 1),
        ('distro = "rhel-8"', ["--glibc", "2.36"], NEW_GLIBC_PAIR, 0),
        ('musl = "1.1"', ["-t", "musl-x86_64"], ("musllag==1.0\n", "musllag==1.1\n"), 1),
        ('macos = "13.0"', ["-t", "macos-arm64"], ("maclag==1.0\n", "maclag==1.1\n"), 1),
        ('macos = "13.0"', ["-t", "macos-arm64", "--macos", "14.0"], ("maclag==1.0\n", "maclag==1.1\n"), 0),
    ],
)
def test_diff_project_platform_floors(fake_pypi, tmp_path, capsys, config, flags, pair, expected_code):
    old, new = write_pair(tmp_path, *pair)
    (tmp_path / "new" / "pyproject.toml").write_text("[tool.wheelgap]\n" + config, encoding="utf-8")
    assert main([old, new, *TARGET, *flags]) == expected_code


# Each new release's only mobile wheel needs iOS 13.0 / Android API level 24 (the defaults); the old one's needs less.
MOBILE_LAG = {
    "ioslag": {
        "1.0": ["ioslag-1.0-cp313-cp313-ios_12_0_arm64_iphoneos.whl"],
        "1.1": ["ioslag-1.1-cp313-cp313-ios_13_0_arm64_iphoneos.whl"],
    },
    "androidlag": {
        "1.0": ["androidlag-1.0-cp313-cp313-android_21_arm64_v8a.whl"],
        "1.1": ["androidlag-1.1-cp313-cp313-android_24_arm64_v8a.whl"],
    },
}


@pytest.mark.parametrize(
    "config, name, platform, expected_code",
    [
        ("", "ioslag", "ios-arm64", 0),
        ('ios = "12.0"', "ioslag", "ios-arm64", 1),
        ("", "androidlag", "android-arm64", 0),
        ("android-api = 21", "androidlag", "android-arm64", 1),
    ],
)
def test_diff_project_mobile_floors(fake_pypi, tmp_path, capsys, monkeypatch, config, name, platform, expected_code):
    for package, releases in MOBILE_LAG.items():
        monkeypatch.setitem(FAKE_INDEX, package, releases)
    old, new = write_pair(tmp_path, f"{name}==1.0\n", f"{name}==1.1\n")
    (tmp_path / "new" / "pyproject.toml").write_text("[tool.wheelgap]\n" + config, encoding="utf-8")
    assert main([old, new, "-p", "3.13", "-t", platform]) == expected_code


@pytest.mark.parametrize(
    "config, flags, name, platform",
    [
        ('ios = "12.0"', ["--ios", "13.0"], "ioslag", "ios-arm64"),
        ("android-api = 21", ["--android-api", "24"], "androidlag", "android-arm64"),
    ],
)
def test_diff_mobile_flags_win_over_config(fake_pypi, tmp_path, capsys, monkeypatch, config, flags, name, platform):
    # As in the main command, --ios / --android-api beat [tool.wheelgap]: the newer floor admits both releases.
    for package, releases in MOBILE_LAG.items():
        monkeypatch.setitem(FAKE_INDEX, package, releases)
    old, new = write_pair(tmp_path, f"{name}==1.0\n", f"{name}==1.1\n")
    (tmp_path / "new" / "pyproject.toml").write_text("[tool.wheelgap]\n" + config, encoding="utf-8")
    assert main([old, new, "-p", "3.13", "-t", platform, *flags]) == 0


@needs_git
def test_diff_git_ref_uses_working_tree_config(fake_pypi, repo, capsys):
    config = repo / "pyproject.toml"
    config.write_text('[tool.wheelgap]\npython = ["3.12"]\ntargets = ["linux-x86_64"]', encoding="utf-8")
    git("add", "pyproject.toml", cwd=repo)
    git("commit", "-q", "-m", "old config", cwd=repo)
    config.write_text('[tool.wheelgap]\npython = ["3.13"]\ntargets = ["win-arm64"]', encoding="utf-8")
    assert main(["HEAD:uv.lock", "HEAD:uv.lock", "--json"]) == 0
    assert [(t["python"], t["platform"]) for t in json.loads(capsys.readouterr().out)["targets"]] == [
        ("3.13", "win-arm64")
    ]


@needs_git
def test_diff_git_ref_config_when_lockfile_is_absent_from_working_tree(fake_pypi, repo, capsys):
    folder = repo / "project"
    folder.mkdir()
    (folder / "uv.lock").write_text(UV_OLD, encoding="utf-8")
    git("add", "project/uv.lock", cwd=repo)
    git("commit", "-q", "-m", "nested lock", cwd=repo)
    (folder / "uv.lock").unlink()
    (folder / "pyproject.toml").write_text(
        '[tool.wheelgap]\npython = ["3.13"]\ntargets = ["win-arm64"]\nignore = ["purewheel"]', encoding="utf-8"
    )
    assert main(["HEAD:project/uv.lock", "HEAD:project/uv.lock", "--json"]) == 0
    assert [(t["python"], t["platform"]) for t in json.loads(capsys.readouterr().out)["targets"]] == [
        ("3.13", "win-arm64")
    ]


@pytest.mark.parametrize("side", ["old", "new"])
def test_diff_pyproject_non_table_tool_exits_with_source_error(capsys, tmp_path, fake_pypi, side):
    old, new = write_pair(tmp_path, "idna==3.10\n", "idna==3.10\n")
    (tmp_path / side / "pyproject.toml").write_text("tool = 1\n", encoding="utf-8")

    assert main([old, new, "-p", "3.13", "-t", "linux-x86_64"]) == 2
    err = capsys.readouterr().err
    assert "[tool] must be a table" in err
    assert "AttributeError" not in err
    assert fake_pypi == []


def test_diff_refuses_a_non_table_tool_wheelgap_beside_the_old_side(capsys, tmp_path, fake_pypi):
    # The NEW side's file goes through load_config(); the OLD side's was read for index-url only and skipped quietly.
    old, new = write_pair(tmp_path, "idna==3.10\n", "idna==3.10\n")
    (tmp_path / "old" / "pyproject.toml").write_text("[tool]\nwheelgap = 1\n", encoding="utf-8")

    assert main([old, new, "-p", "3.13", "-t", "linux-x86_64"]) == 2
    assert capsys.readouterr().err == (
        f"wheelgap: error: the pyproject.toml beside {old}: [tool.wheelgap] must be a table\n"
    )
    assert fake_pypi == []


MIRROR = "https://mirrors.example/pypi/simple"


def moved_lock(registry, requires=">=3.14", wheel="fastlib-2.0.0-cp314-cp314-manylinux_2_17_x86_64.whl"):
    """A uv.lock shaped like jumpserver/jumpserver#17168's (#807): locked for Python 3.14 and newer, so it keeps only
    the wheels that range can use, with its one package from `registry`."""
    return (
        f'version = 1\nrevision = 3\nrequires-python = "{requires}"\n\n'
        '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { editable = "." }\n'
        'dependencies = [\n    { name = "fastlib" },\n]\n\n'
        f'[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/fastlib-2.0.0.tar.gz", hash = "sha256:00", size = 1 }\n'
        f'wheels = [\n    {{ url = "https://files.example/{wheel}", hash = "sha256:01", size = 1 }},\n]\n'
    )


def write_locks(tmp_path, old, new):
    for side, text in (("old", old), ("new", new)):
        (tmp_path / side).mkdir()
        (tmp_path / side / "uv.lock").write_text(text, encoding="utf-8")
    return str(tmp_path / "old"), str(tmp_path / "new")


def test_diff_skips_a_python_the_lock_leaves_out_after_a_move_to_a_mirror(fake_pypi, tmp_path, capsys):
    # On 3.13 the PyPI side is looked up again and finds fastlib's cp313 wheel, while the mirror side can't be, so it
    # keeps the lock's cp314 wheel only and would look like a lost wheel. Neither lock is made for 3.13: not compared.
    old, new = write_locks(tmp_path, moved_lock("https://pypi.org/simple"), moved_lock(MIRROR))
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["Nothing got worse on the compared targets."]
    assert "this change drops" not in err
    assert (
        "wheelgap: note: didn't compare 3.13 linux-x86_64: both lockfiles are locked for Python >=3.14, which leaves "
        "out Python 3.13. A lock isn't made for a Python outside its requires-python"
    ) in err
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worse"] == [] and data["new"] == [] and data["summary"] == {"worse": 0, "new": 0}
    assert data["not_compared"] == [
        {
            "target": "3.13 linux-x86_64",
            "sides": ["old", "new"],
            "python": "3.13",
            "reason": "both lockfiles are locked for Python >=3.14, which leaves out Python 3.13",
        }
    ]
    assert [t["label"] for t in data["targets"]] == ["3.13 linux-x86_64", "3.14 linux-x86_64"]


def test_diff_still_compares_the_pythons_both_locks_are_made_for(fake_pypi, tmp_path, capsys):
    old = moved_lock("https://pypi.org/simple")
    new = moved_lock(MIRROR, wheel="fastlib-2.0.0-cp315-cp315-manylinux_2_17_x86_64.whl")
    old, new = write_locks(tmp_path, old, new)
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["worse"] == [
        {
            "name": "fastlib",
            "old_versions": ["2.0.0"],
            "new_versions": ["2.0.0"],
            "targets": {"3.14 linux-x86_64": {"old_status": "wheel", "new_status": "source"}},
        }
    ]
    assert [e["target"] for e in data["not_compared"]] == ["3.13 linux-x86_64"]


def test_diff_says_the_change_dropped_a_python_only_the_new_lock_leaves_out(fake_pypi, tmp_path, capsys):
    # The bump stopped the lock serving 3.13 (uv sync there now fails): still a note, not exit 1, but said as the
    # change's doing, apart from a Python both locks leave out (3.11), and the last line doesn't claim the whole run.
    old = moved_lock("https://pypi.org/simple", ">=3.12", "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl")
    old, new = write_locks(tmp_path, old, moved_lock(MIRROR))
    argv = [old, new, "-p", "3.11", "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["Nothing got worse on the compared targets."]
    notes = [line for line in err.splitlines() if "compare" in line]
    assert notes == [
        "wheelgap: note: this change drops Python 3.13 support: the new lockfile no longer supports Python 3.13 "
        "(old: >=3.12, new: >=3.14), so installing the new lock there now fails (uv sync refuses a lock on a Python "
        "outside its requires-python). Not compared: 3.13 linux-x86_64; the drop doesn't count as worse",
        "wheelgap: note: didn't compare 3.11 linux-x86_64: the old lockfile is locked for Python >=3.12 and the new "
        "lockfile is locked for Python >=3.14, which leaves out Python 3.11. A lock isn't made for a Python outside "
        "its requires-python (it may lack the wheels that Python needs, and packages from other indexes can't be "
        "looked up again), so nothing there counts as worse",
    ]
    assert main([*argv, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["not_compared"][1] == {
        "target": "3.13 linux-x86_64",
        "sides": ["new"],
        "python": "3.13",
        "reason": "the new lockfile no longer supports Python 3.13 (old: >=3.12, new: >=3.14)",
    }
    assert main([old, new, "-p", "3.13", "-t", "linux-x86_64"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing was compared."]


def test_diff_says_a_widened_range_hides_the_new_locks_gaps(fake_pypi, tmp_path, capsys):
    new = moved_lock("https://pypi.org/simple", ">=3.12", "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl")
    old = moved_lock("https://pypi.org/simple", ">=3.14", "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl")
    old, new = write_locks(tmp_path, old, new)
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64", "--json"]) == 0
    out, err = capsys.readouterr()
    assert [(e["target"], e["sides"]) for e in json.loads(out)["not_compared"]] == [("3.13 linux-x86_64", ["old"])]
    assert "this change drops" not in err
    assert err.rstrip().endswith(
        "where only the old lockfile leaves a Python out, the new one's gaps there aren't shown"
    )


def test_diff_names_each_sides_range_when_they_differ(fake_pypi, tmp_path, capsys):
    old, new = write_locks(tmp_path, moved_lock("https://pypi.org/simple", ">=3.14"), moved_lock(MIRROR, ">=3.14.1"))
    assert main([old, new, "-p", "3.13", "-t", "linux-x86_64", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)["not_compared"]
    assert entry["reason"] == (
        "the old lockfile is locked for Python >=3.14 and the new lockfile is locked for Python >=3.14.1, "
        "which leaves out Python 3.13"
    )
