"""--from-log: the packages a failed pip or uv install log says didn't build become explained checks."""

import io
import json
import sys

import pytest
from conftest import FIXTURES

from wheelgap.cli import main
from wheelgap.install_log import MAX_LOG_BYTES, failed_builds, read_log
from wheelgap.sources import SourceError


def log(name: str) -> str:
    return read_log(str(FIXTURES / name)).replace("\r\n", "\n")  # a Windows checkout may have CRLF


@pytest.mark.parametrize(
    "name,expected",
    [
        # pip 23: the version from `Collecting laggard==1.0`; the build's own `Failed to build the extension`,
        # indented under pip's error, isn't pip's summary.
        ("pip23-laggard.log", [("laggard", "1.0")]),
        # pip 24 and 25: `Failed to build installable wheels ... (fastlib, laggard)`; fastlib's version from the
        # `Downloading` line after an unpinned `Collecting`.
        ("pip24-two-failed.log", [("fastlib", "2.0.0"), ("laggard", "1.0")]),
        # uv's `×` tree, in colour.
        ("uv-ansi.log", [("nowheels", "0.9")]),
        # Older uv: `Failed to download and build: ...`, and the build's output unindented at column 0.
        ("uv-old.log", [("nowheels", "0.9")]),
        # Copied from GitHub Actions: timestamps, and an earlier run that pinned forked==0.9.
        ("gha-pip-mixed.log", [("forked", "1.0")]),
        # pip 25.2 and later: `× Failed to build installable wheels ...` with the names on the `╰─>` line.
        ("pip25-two-failed.log", [("fastlib", "2.0.0"), ("laggard", "1.0")]),
    ],
)
def test_failed_builds(name, expected):
    assert failed_builds(log(name)) == expected


@pytest.mark.parametrize(
    "summary",
    [
        # pip 24.1 to 25.1
        "ERROR: Failed to build installable wheels for some pyproject.toml based projects (fastlib, laggard)\n",
        # pip 24.1 to 24.3 print ERROR: twice
        "ERROR: ERROR: Failed to build installable wheels for some pyproject.toml based projects (fastlib, laggard)\n",
        # pip 25.2 and later
        "error: failed-wheel-build-for-install\n\n× Failed to build installable wheels for some pyproject.toml based "
        "projects\n╰─> fastlib, laggard\n",
        # pip 25.2 and later on a console that can't print `×` and `╰─>`
        "error: failed-wheel-build-for-install\n\nFailed to build installable wheels for some pyproject.toml based "
        "projects\n\nfastlib, laggard\n",
    ],
)
def test_pip_installable_summary_alone(summary):
    text = log("pip24-two-failed.log")
    text = text[: text.index("Failed to build fastlib laggard\n")] + summary
    assert failed_builds(text) == [("fastlib", "2.0.0"), ("laggard", "1.0")]


def test_pip_25_names_line_that_cant_be_read_is_refused():
    text = log("pip25-two-failed.log").replace("╰─> fastlib, laggard", "note: something else")
    text = text.replace("Failed to build fastlib laggard\n", "")
    with pytest.raises(SourceError, match="can't read pip's list"):
        failed_builds(text)


def test_column_0_text_that_names_no_built_package_is_not_pip_summary():
    text = log("pip23-laggard.log") + "Failed to build the extension\n"
    assert failed_builds(text) == [("laggard", "1.0")]


@pytest.mark.parametrize(
    "name,fragment",
    [
        ("pip-succeeded.log", "found no package that failed to build"),
        ("pip-unclear-version.log", "collects forked again without a version after naming one"),
        ("pip-two-versions.log", "forked failed to build at more than one version"),
        ("pip-git-url.log", "installs nowheels from a URL or a path"),
        ("uv-local-project.log", "not as NAME==VERSION"),
        # The log's start was cut off: no version to be found.
        ("pip-no-version.log", r"doesn't say which version of nowheels failed to build .*check nowheels==VERSION"),
    ],
)
def test_refuses_rather_than_guesses(name, fragment):
    with pytest.raises(SourceError, match=fragment):
        failed_builds(log(name))


BUILD_FAILED = """\
Building wheels for collected packages: nowheels
  Building wheel for nowheels (pyproject.toml) ... error
  ERROR: Failed building wheel for nowheels
Failed to build nowheels
"""


@pytest.mark.parametrize(
    "text",
    [
        # `pip install .` logs `Processing`, never `Collecting`: PyPI's nowheels may be another project.
        "Processing /home/me/nowheels\n  Installing build dependencies ... done\n" + BUILD_FAILED,
        # `pip install -q` hides `Collecting`, `Building wheel for` and `Failed to build`.
        "  ERROR: Failed building wheel for nowheels\nERROR: Failed to build installable wheels for some "
        "pyproject.toml based projects (nowheels)\n",
    ],
)
def test_no_version_is_refused(text):
    with pytest.raises(SourceError, match="doesn't say which version of nowheels failed to build"):
        failed_builds(text)


@pytest.mark.parametrize(
    "downloads",
    [
        "  Downloading nowheels-1.1.tar.gz (5 kB)\n  Downloading nowheels-1.0.tar.gz (5 kB)\n",
        # pip fetches a wheel's metadata alone while it resolves
        "  Downloading nowheels-1.1-py3-none-any.whl.metadata (2 kB)\n  Downloading nowheels-1.0.tar.gz (5 kB)\n",
    ],
)
def test_more_than_one_version_since_collecting_is_refused(downloads):
    with pytest.raises(SourceError, match="pip tried more than one version of nowheels"):
        failed_builds("Collecting nowheels\n" + downloads + BUILD_FAILED)


def test_the_same_version_twice_is_one_version():
    text = "Collecting nowheels==1.0\n  Downloading nowheels-1.0.0.tar.gz (5 kB)\n" + BUILD_FAILED
    assert failed_builds(text) == [("nowheels", "1.0")]


@pytest.mark.parametrize("backtracked", ["nowheels", "other[extra]"])
def test_backtracking_is_refused(backtracked):
    # Backtracking on any package changes the versions its dependencies are collected at.
    text = (
        "Collecting nowheels==1.0\n  Downloading nowheels-1.0.tar.gz (5 kB)\n"
        f"INFO: pip is looking at multiple versions of {backtracked} to determine which version is compatible with "
        "other requirements. This could take a while.\n" + BUILD_FAILED
    )
    with pytest.raises(SourceError, match="pip backtracked"):
        failed_builds(text)


def test_a_download_doesnt_turn_a_url_into_an_index_version():
    text = "Collecting nowheels @ https://example.com/nowheels-1.0.tar.gz\n  Downloading nowheels-1.0.tar.gz\n"
    with pytest.raises(SourceError, match="installs nowheels from a URL or a path"):
        failed_builds(text + BUILD_FAILED)


def test_a_build_dependency_install_under_pip_v_doesnt_reset_the_version():
    # Under `pip install -v`, the pip that installs oldscipy's build dependencies logs its own, indented, lines: its
    # numpy isn't the numpy==1.26.4 that failed.
    text = (
        "Collecting numpy==1.26.4\n"
        "  Using cached numpy-1.26.4.tar.gz (15.8 MB)\n"
        "Collecting oldscipy==1.0\n"
        "  Using cached oldscipy-1.0.tar.gz (40 MB)\n"
        "  Running command pip subprocess to install build dependencies\n"
        "  Collecting numpy>=2\n"
        "    Using cached numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (16.0 MB)\n"
        "  Installing collected packages: numpy\n"
        "  Successfully installed numpy-2.1.0\n"
        "  Installing build dependencies ... done\n"
        "Building wheels for collected packages: numpy\n"
        "  Building wheel for numpy (pyproject.toml) ... error\n"
        "  ERROR: Failed building wheel for numpy\n"
        "Failed to build numpy\n"
    )
    with pytest.raises(SourceError, match="pip tried more than one version of numpy"):
        failed_builds(text)
    # Without the subprocess's own download, it's the version this run collected.
    without = text.replace(
        "    Using cached numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (16.0 MB)\n", ""
    )
    assert failed_builds(without) == [("numpy", "1.26.4")]


RUN_1 = """\
Collecting nowheels==1.0
  Downloading nowheels-1.0.tar.gz (5 kB)
Building wheels for collected packages: nowheels
  Building wheel for nowheels (pyproject.toml) ... done
Successfully built nowheels
Installing collected packages: nowheels
Successfully installed nowheels-1.0
"""


@pytest.mark.parametrize(
    "run_2",
    [
        # `pip install -q -U nowheels` hides `Collecting`
        "  ERROR: Failed building wheel for nowheels\nERROR: Failed to build installable wheels for some "
        "pyproject.toml based projects (nowheels)\n",
        # `pip install .` logs `Processing`
        "Processing /home/me/nowheels\n  Installing build dependencies ... done\n" + BUILD_FAILED,
    ],
)
def test_a_version_from_an_earlier_pip_run_is_refused(run_2):
    with pytest.raises(SourceError, match="last `Collecting` line for nowheels is from an earlier pip run"):
        failed_builds(RUN_1 + run_2)


def test_a_later_pip_run_that_collects_the_package_again_gives_its_version():
    run_2 = "Collecting nowheels\n  Downloading nowheels-1.1.tar.gz (5 kB)\n" + BUILD_FAILED
    assert failed_builds(RUN_1 + run_2) == [("nowheels", "1.1")]


INDEXES = "Looking in indexes: https://pypi.org/simple, https://__token__:****@private.example/simple\n"


def test_another_index_is_refused():
    assert failed_builds("Looking in indexes: https://pypi.org/simple/\n" + log("pip23-laggard.log"))
    with pytest.raises(SourceError, match=r"another index than PyPI \(https://private\.example/simple\)") as e:
        failed_builds(INDEXES + log("pip23-laggard.log"))
    assert "token" not in str(e.value)
    assert failed_builds(INDEXES + log("pip23-laggard.log"), other_index=True) == [("laggard", "1.0")]


@pytest.mark.parametrize(
    "links",
    [
        "Looking in links: /wheels, https://alice@files.example/wheels\n",
        # --no-index: pip prints no `Looking in indexes:`, only the links
        "Looking in links: /wheels\n",
    ],
)
def test_find_links_is_refused(links):
    with pytest.raises(SourceError, match=r"the log used --find-links \(/wheels") as e:
        failed_builds(links + log("pip23-laggard.log"))
    assert "alice" not in str(e.value)
    assert failed_builds(links + log("pip23-laggard.log"), other_index=True) == [("laggard", "1.0")]


def test_uv_lines_count_only_after_uvs_prefixes():
    # A build's own output that happens to read like uv's error isn't uv's.
    text = log("uv-old.log") + "--- stdout:\nnote: Failed to build `nowheels==0.8`, retrying\n"
    assert failed_builds(text) == [("nowheels", "0.9")]


def test_a_summary_that_names_a_package_pip_never_built_is_refused():
    text = log("pip23-laggard.log").replace("\nFailed to build laggard\n", "\nFailed to build laggard ../evil\n")
    with pytest.raises(SourceError, match="can't read pip's list"):
        failed_builds(text)


def summary_of(count: int) -> str:
    names = [f"pkg{i}" for i in range(count)]
    built = "".join(
        f"Collecting {name}==1.0\n  Building wheel for {name} (pyproject.toml) ... error\n" for name in names
    )
    return f"{built}Failed to build {' '.join(names)}\n"


def test_at_most_50_failures():
    assert len(failed_builds(summary_of(50))) == 50
    with pytest.raises(SourceError, match="more than 50 packages"):
        failed_builds(summary_of(51))


def test_stdin(monkeypatch):
    data = (FIXTURES / "uv-ansi.log").read_bytes()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    assert failed_builds(read_log("-")) == [("nowheels", "0.9")]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"])
def test_powershell_utf16_and_a_bom(tmp_path, encoding):
    text = (FIXTURES / "pip23-laggard.log").read_text(encoding="utf-8")
    bom = "﻿" if encoding in ("utf-16-le", "utf-16-be") else ""  # utf-16 and utf-8-sig write their own
    path = tmp_path / "install.log"
    path.write_bytes((bom + text).encode(encoding))
    assert read_log(str(path)) == text
    assert failed_builds(read_log(str(path))) == [("laggard", "1.0")]


def test_a_long_log_keeps_its_end(tmp_path):
    path = tmp_path / "install.log"
    unreadable = b"error: Failed to build `x @ file:///x`\n"  # an error if it were read
    path.write_bytes(unreadable + b"x" * MAX_LOG_BYTES + b"\n" + (FIXTURES / "pip23-laggard.log").read_bytes())
    assert failed_builds(read_log(str(path))) == [("laggard", "1.0")]
    text = (FIXTURES / "pip23-laggard.log").read_text(encoding="utf-8")
    path.write_bytes(("y\n" * (MAX_LOG_BYTES // 2) + text).encode("utf-16"))
    assert failed_builds(read_log(str(path))) == [("laggard", "1.0")]


def test_undecodable_bytes_are_replaced(tmp_path):
    path = tmp_path / "install.log"
    path.write_bytes(b"\x80\x81 not UTF-8\n" + (FIXTURES / "uv-old.log").read_bytes())
    assert failed_builds(read_log(str(path))) == [("nowheels", "0.9")]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def from_log(name, *argv):
    return ["--from-log", str(FIXTURES / name), "-p", "3.13", *argv, "--no-cache"]


def test_checks_the_failed_package_with_explain_and_suggest(capsys, fake_pypi):
    code, out, err = run(capsys, *from_log("pip23-laggard.log", "-t", "win-amd64", "--json"))
    assert code == 1
    (pkg,) = json.loads(out)["packages"]
    assert (pkg["name"], pkg["version"], pkg["results"]) == ("laggard", "1.0", {"win-amd64": "source"})
    assert pkg["suggest"] == {"win-amd64": "1.1"}
    assert "rejected" in pkg
    assert fake_pypi == ["https://pypi.org/pypi/laggard/1.0/json", "https://pypi.org/pypi/laggard/json"]
    assert "wheelgap: note: the install log says these failed to build: laggard 1.0\n" in err
    assert "has a wheel" not in err


def test_text_output(capsys, fake_pypi):
    code, out, err = run(capsys, *from_log("uv-ansi.log", "-t", "win-arm64"))
    assert code == 1
    assert "nowheels" in out and "no wheels published for this version" in out
    assert "the install log says these failed to build: nowheels 0.9\n" in err


def test_no_suggest_turns_suggest_off(capsys, fake_pypi):
    _, out, _ = run(capsys, *from_log("pip23-laggard.log", "-t", "win-amd64", "--json", "--no-suggest"))
    (pkg,) = json.loads(out)["packages"]
    assert "suggest" not in pkg and "rejected" in pkg
    assert fake_pypi == ["https://pypi.org/pypi/laggard/1.0/json"]


def test_a_failed_package_with_a_wheel_for_the_target_gets_a_note(capsys, fake_pypi):
    code, out, err = run(capsys, *from_log("pip24-two-failed.log", "-t", "linux-x86_64", "--json"))
    assert code == 1  # laggard still builds from source
    results = {p["name"]: (p["version"], p["results"]) for p in json.loads(out)["packages"]}
    assert results == {
        "fastlib": ("2.0.0", {"linux-x86_64": "wheel"}),
        "laggard": ("1.0", {"linux-x86_64": "source"}),
    }
    assert "wheelgap: note: the install log says these failed to build: fastlib 2.0.0, laggard 1.0\n" in err
    assert (
        "wheelgap: note: the install log says fastlib failed to build, but fastlib 2.0.0 has a wheel for "
        "linux-x86_64: the log likely comes from another platform or Python than the one checked (-p, -t)\n"
    ) in err
    assert "says laggard failed to build, but" not in err


def test_a_package_named_like_a_folder_is_still_a_pypi_name(capsys, fake_pypi, tmp_path, monkeypatch):
    (tmp_path / "laggard").mkdir()
    (tmp_path / "laggard" / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, *from_log("pip23-laggard.log", "-t", "win-amd64", "--json"))
    assert code == 1
    (pkg,) = json.loads(out)["packages"]
    assert (pkg["name"], pkg["version"], pkg["results"]) == ("laggard", "1.0", {"win-amd64": "source"})
    assert not [url for url in fake_pypi if "purewheel" in url]  # never the folder's purewheel


def test_quiet_in_pyproject_doesnt_stop_it(capsys, fake_pypi, tmp_path, monkeypatch):
    # quiet = true is a project default; --from-log on the command line wins, as --no-quiet does.
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, *from_log("pip23-laggard.log", "-t", "win-amd64"))
    assert code == 1
    assert "laggard" in out and "the install log says these failed to build: laggard 1.0\n" in err


def test_quiet_on_the_command_line_is_an_error(capsys):
    with pytest.raises(SystemExit) as e:
        main(from_log("pip23-laggard.log", "-t", "win-amd64", "--quiet"))
    assert e.value.code == 2
    assert "--from-log explains each package, so it can't be used with --quiet\n" in capsys.readouterr().err


def test_a_log_from_another_index_is_checked_only_with_another_index(capsys, fake_pypi, tmp_path, monkeypatch):
    path = tmp_path / "install.log"
    path.write_text(INDEXES + log("pip23-laggard.log"), encoding="utf-8")
    code, out, err = run(capsys, "--from-log", str(path), "-p", "3.13", "-t", "win-amd64", "--no-cache")
    assert (code, out) == (2, "")
    assert "the log used another index than PyPI" in err
    assert fake_pypi == []
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    code, _, err = run(capsys, "--from-log", str(path), "-p", "3.13", "-t", "win-amd64", "--no-cache", "--json")
    assert code == 1 and "the log used another index" not in err
    assert fake_pypi and all(url.startswith("https://mirror.example.com/pypi/") for url in fake_pypi)


@pytest.mark.parametrize("given", [[], ["-p", "3.13"], ["-t", "win-amd64"]])
def test_needs_the_python_and_platform_the_install_ran_on(capsys, given):
    with pytest.raises(SystemExit) as e:
        main(["--from-log", str(FIXTURES / "pip23-laggard.log"), *given])
    assert e.value.code == 2
    assert "--from-log needs the Python and platform the install ran on: pass -p and -t" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra,flag",
    [
        (["laggard==1.0"], "INPUT"),
        (["--dist", "dist"], "--dist"),
        (["--deps"], "--deps"),
        (["--environment", "test"], "--environment"),
        (["--suggest-targets", "."], "--suggest-targets"),
        (["--list-targets"], "--list-targets"),
        (["--clear-cache"], "--clear-cache"),
    ],
)
def test_flags_it_cant_be_combined_with(capsys, extra, flag):
    with pytest.raises(SystemExit) as e:
        main(from_log("pip23-laggard.log", "-t", "win-amd64", *extra))
    assert e.value.code == 2
    assert f"--from-log checks the packages in the log, so it can't be used with {flag}\n" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name,fragment",
    [
        ("pip-unclear-version.log", "collects forked again without a version"),
        ("pip-succeeded.log", "found no package that failed to build"),
        ("pip-no-version.log", "doesn't say which version of nowheels failed to build"),  # never asks PyPI for it
    ],
)
def test_an_unreadable_log_is_an_input_error(capsys, fake_pypi, name, fragment):
    code, out, err = run(capsys, *from_log(name, "-t", "win-amd64"))
    assert (code, out) == (2, "")
    assert fragment in err
    assert fake_pypi == []


def test_a_missing_log_is_an_input_error(capsys, tmp_path):
    code, _, err = run(capsys, "--from-log", str(tmp_path / "nope.log"), "-p", "3.13", "-t", "win-amd64")
    assert code == 2 and f"--from-log: can't read {tmp_path / 'nope.log'}" in err
