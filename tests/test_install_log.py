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
        # The log's start was cut off: no version to be found.
        ("pip-no-version.log", [("nowheels", None)]),
    ],
)
def test_failed_builds(name, expected):
    assert failed_builds(log(name)) == expected


def test_pip_24_summary_alone():
    text = log("pip24-two-failed.log").replace("\nFailed to build fastlib laggard\n", "\n")
    assert "Failed to build fastlib" not in text
    assert failed_builds(text) == [("fastlib", "2.0.0"), ("laggard", "1.0")]


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
    ],
)
def test_refuses_rather_than_guesses(name, fragment):
    with pytest.raises(SourceError, match=fragment):
        failed_builds(log(name))


def test_a_summary_that_names_a_package_pip_never_built_is_refused():
    text = log("pip23-laggard.log").replace("\nFailed to build laggard\n", "\nFailed to build laggard ../evil\n")
    with pytest.raises(SourceError, match="can't read pip's list"):
        failed_builds(text)


def summary_of(count: int) -> str:
    names = [f"pkg{i}" for i in range(count)]
    built = "".join(f"  Building wheel for {name} (pyproject.toml) ... error\n" for name in names)
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
    (tmp_path / "nowheels").mkdir()
    (tmp_path / "nowheels" / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, *from_log("pip-no-version.log", "-t", "win-amd64", "--json"))
    assert code == 1
    (pkg,) = json.loads(out)["packages"]
    assert (pkg["name"], pkg["version"], pkg["results"]) == ("nowheels", "0.9", {"win-amd64": "source"})
    assert set(fake_pypi) == {"https://pypi.org/pypi/nowheels/json"}  # never the folder's purewheel
    assert "the install log doesn't say which version of nowheels failed; checking its newest release\n" in err


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
