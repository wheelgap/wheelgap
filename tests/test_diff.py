import json
import shutil
import subprocess
import sys

import pytest

from wheelgap.api import Result, make_targets
from wheelgap.check import Status
from wheelgap.diff import git_ref_input, main, merge, worse

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
    assert err.startswith("wheelgap: error: TargetError: ") and "unknown distro 'not-a-distro'" in err


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


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["{tmp}/missing.txt", "{new}"], "missing.txt"),
        (["{tmp}/empty", "{new}"], "no lockfile found"),
        (["{tmp}/notes.md", "{new}"], "SourceError: don't know how to read"),
        (["{tmp}/pyproject.toml", "{new}"], "pyproject.toml: a pyproject.toml lists direct dependencies only"),
        (["{new}", "{new}", "-t", "linux-aarch46"], "TargetError: "),
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
