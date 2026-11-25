"""--from-log: the packages a failed pip, uv, Poetry or PDM install log says didn't build become explained checks."""

import io
import json
import sys

import pytest
from conftest import FIXTURES

from wheelgap.cli import main
from wheelgap.install_log import (
    FIND_LINKS,
    MAX_LOG_BYTES,
    NO_FAILURE,
    OTHER_INDEX,
    LogError,
    failed_builds,
    read_log,
)
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
        # Captured with Poetry 2.5.1 and PDM 2.29.7; see install-log-captures.md.
        ("poetry-normal.log", [("pyaudio", "0.2.14")]),
        ("poetry-verbose.log", [("pyaudio", "0.2.14")]),
        ("pdm-verbose.log", [("pyaudio", "0.2.14")]),
        # Real logs from public issues (#735, #736, #737): a Docker build's excerpt, with BuildKit's `28.56 ` prefix
        # and the version from the Dockerfile's `RUN pip install`; the version from a shell prompt's `pip install`;
        # and pip's `Getting requirements to build wheel` failure, for the sdist collected last.
        ("docker-tokenizers.log", [("tokenizers", "0.20.0")]),
        ("pip-grpcio-prompt.log", [("grpcio", "1.73.1")]),
        ("pip-pyyaml-build-requirements.log", [("pyyaml", "5.4")]),
    ],
)
def test_failed_builds(name, expected):
    assert failed_builds(log(name)) == expected


POETRY_NOTE = (
    "Note: This error originates from the build backend, and is likely not a problem with poetry but one of the "
    "following issues with"
)
OTHER_PACKAGE_OUTPUT = ["Failed to build other_package", "  ✖ Install other_package 9.9 failed"]


@pytest.mark.parametrize(
    "name,line,prefix",
    [
        # What the backend writes to stderr is at column 0 in Poetry's log, as the BuildBackendWarning is.
        ("poetry-normal.log", "/tmp/redacted BuildBackendWarning: License classifiers are deprecated.", ""),
        # PDM -v prefixes the build's output with `pdm.termui: `.
        ("pdm-verbose.log", "pdm.termui: building 'pyaudio._portaudio' extension", "pdm.termui: "),
    ],
)
def test_project_tool_build_output_is_not_another_failed_package(name, line, prefix):
    text = log(name)
    assert line in text
    text = text.replace(line, "\n".join([line, *(prefix + out for out in OTHER_PACKAGE_OUTPUT)]))
    assert failed_builds(text) == [("pyaudio", "0.2.14")]


def test_poetry_note_in_build_output_needs_an_operation_line():
    # Build output at column 0 with Poetry's own header and note for a package no `  - Installing` line names.
    line = "/tmp/redacted BuildBackendWarning: License classifiers are deprecated."
    nested = f"PEP517 build of a dependency failed\n\n{POETRY_NOTE} other-package (9.9)\n"
    text = log("poetry-normal.log").replace(line, f"{line}\n{nested}")
    assert "other-package (9.9)" in text
    assert failed_builds(text) == [("pyaudio", "0.2.14")]


@pytest.mark.parametrize("operation", ["Updating pyaudio (0.2.13 -> 0.2.14)", "Downgrading pyaudio (0.3.0 -> 0.2.14)"])
@pytest.mark.parametrize("status", ["", ": Failed", ": Pending..."])
def test_poetry_update_operation_names_the_new_version(operation, status):
    text = log("poetry-normal.log").replace("  - Installing pyaudio (0.2.14)", f"  - {operation}{status}")
    assert failed_builds(text) == [("pyaudio", "0.2.14")]


def test_poetry_note_for_another_version_than_the_operation_is_refused():
    text = log("poetry-normal.log").replace("issues with pyaudio (0.2.14)", "issues with pyaudio (0.2.13)")
    with pytest.raises(SourceError, match="Poetry"):
        failed_builds(text)


def test_project_tool_logs_in_colour():
    poetry = (
        log("poetry-normal.log")
        .replace(
            "  - Installing pyaudio (0.2.14)",
            "  \x1b[34;1m-\x1b[39;22m Installing \x1b[36mpyaudio\x1b[39m (\x1b[39;1m0.2.14\x1b[39;22m): "
            "\x1b[31;1mFailed\x1b[39;22m",
        )
        .replace("PEP517 build of a dependency failed", "\x1b[31;1mPEP517 build of a dependency failed\x1b[39;22m")
    )
    note = next(line for line in poetry.splitlines() if line.startswith("Note: "))
    poetry = poetry.replace(note, f"\x1b[33m{note}\x1b[39m")
    assert failed_builds(poetry) == [("pyaudio", "0.2.14")]
    pdm = log("pdm-verbose.log").replace(
        "  ✖ Install pyaudio 0.2.14 failed",
        "  \x1b[31m✖\x1b[0m Install \x1b[1mpyaudio\x1b[0m \x1b[36m0.2.14\x1b[0m failed",
    )
    assert "\x1b[31m✖" in pdm
    assert failed_builds(pdm) == [("pyaudio", "0.2.14")]


@pytest.mark.parametrize("name", ["poetry-normal.log", "pdm-normal.log", "pdm-verbose.log"])
def test_project_tool_incomplete_diagnostic_refuses_partial_results(name):
    text = log("uv-ansi.log") + log(name)
    if name.startswith("poetry"):
        text = text.replace("issues with pyaudio (0.2.14)", "issues with pyaudio (not-a-version)")
    else:
        text = text.replace("pdm.exceptions.BuildError:", "pdm.exceptions.InstallationError:")
    with pytest.raises(SourceError, match="Poetry|PDM"):
        failed_builds(text)


def test_pdm_failure_without_build_error_is_not_a_build_failure():
    text = "  ✖ Install pyaudio 0.2.14 failed\nERRORS:\nadd pyaudio failed:\n"
    text += "Traceback (most recent call last):\nOSError: download failed\n"
    with pytest.raises(SourceError, match="PDM"):
        failed_builds(text)


def test_pdm_failure_without_install_version_is_refused():
    text = log("pdm-normal.log").replace("  ✖ Install pyaudio 0.2.14 failed\n", "")
    with pytest.raises(SourceError, match="PDM"):
        failed_builds(text)


def test_pdm_normal_output_cannot_distinguish_an_index_package_from_a_local_one():
    with pytest.raises(SourceError, match="index source.*pdm install -v"):
        failed_builds(log("pdm-normal.log"))


@pytest.mark.parametrize(
    "source,named",
    [
        ("https://private.example/simple/pyaudio/", "https://private.example/simple"),
        ("https://user:secret@private.example/simple/pyaudio/", "https://private.example/simple"),
        ("file:///local/pyaudio/", "file:///local/pyaudio"),
    ],
)
def test_pdm_other_source_is_refused(source, named):
    text = log("pdm-verbose.log").replace("https://pypi.org/simple/pyaudio/", source)
    with pytest.raises(SourceError) as e:
        failed_builds(text)
    assert isinstance(e.value, LogError) and e.value.kind == OTHER_INDEX
    message = str(e.value)
    assert f"another index than PyPI ({named})" in message
    assert "--index-url" in message
    assert "pdm install -v" not in message
    assert "secret" not in message and "user" not in message


def test_pdm_verbose_log_without_build_link_is_refused():
    text = "\n".join(
        line for line in log("pdm-verbose.log").splitlines() if "Running PEP 517 backend to build a wheel" not in line
    )
    with pytest.raises(SourceError, match="which index pyaudio 0.2.14 was built from") as e:
        failed_builds(text)
    assert "pdm install -v" not in str(e.value)


def test_pdm_explicit_other_index_is_allowed():
    text = log("pdm-verbose.log").replace("https://pypi.org/simple/pyaudio/", "https://private.example/simple/pyaudio/")
    assert failed_builds(text, other_index=True) == [("pyaudio", "0.2.14")]


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
        "Obtaining file:///home/me/nowheels\n" + BUILD_FAILED,  # `pip install -e .`
    ],
)
def test_no_version_for_a_local_project_is_refused(text):
    with pytest.raises(SourceError, match="doesn't say which version of nowheels failed to build.*local project"):
        failed_builds(text)


PIP_Q_FAILED = (
    "  ERROR: Failed building wheel for nowheels\nERROR: Failed to build installable wheels for some "
    "pyproject.toml based projects (nowheels)\n"
)


@pytest.mark.parametrize(
    "text",
    [
        PIP_Q_FAILED,  # `pip install -q` hides `Collecting`, `Building wheel for` and `Failed to build`
        log("pip-no-version.log"),  # the log's start was cut off
        "$ pip install -q 'nowheels>=0.5'\n" + PIP_Q_FAILED,  # a range isn't a version
        "$ pip install -q nowheels==0.*\n" + PIP_Q_FAILED,  # nor is a wildcard
        # A cached wheel pip installs isn't a local project.
        "Processing /root/.cache/pip/wheels/ab/cd/other-1.0-py3-none-any.whl\n" + PIP_Q_FAILED,
    ],
)
def test_no_version_anywhere_is_the_latest(text):
    # The caller checks the latest release and says so.
    assert failed_builds(text) == [("nowheels", None)]


@pytest.mark.parametrize(
    "command",
    [
        "$ pip install -q nowheels==0.9",
        "RUN pip install --no-cache-dir -r requirements.txt nowheels==0.9",
        "RUN uv pip install --system 'nowheels == 0.9' other",
        "PS C:\\app> python -m pip install -i https://pypi.org/simple nowheels===0.9",
        "#5 [2/2] RUN /venv/bin/pip3 install nowheels[extra]==0.9",
        # Docker's error quotes the command: what follows `pip install` has an unclosed quote.
        'ERROR: failed to solve: process "/bin/sh -c pip install nowheels==0.9" did not complete successfully',
    ],
)
def test_the_install_command_gives_the_version(command):
    assert failed_builds(command + "\n" + PIP_Q_FAILED) == [("nowheels", "0.9")]


def test_install_commands_that_pin_two_versions_are_refused():
    text = "$ pip install nowheels==0.9\n$ pip install nowheels==1.0\n" + PIP_Q_FAILED
    with pytest.raises(SourceError, match="install commands pin nowheels to more than one version"):
        failed_builds(text)


def test_pips_own_lines_beat_the_install_command():
    # `pip install nowheels` then a `pip install` of the dependency at another version: pip's `Collecting` wins.
    text = "$ pip install nowheels==1.0\nCollecting nowheels==0.9\n" + BUILD_FAILED
    assert failed_builds(text) == [("nowheels", "0.9")]


@pytest.mark.parametrize(
    "prefix",
    [
        "#12 28.56 ",  # `docker build --progress=plain`
        "28.56 ",  # the failed step's excerpt at the end of `docker build`
        "2024-09-03T10:00:00.1234567Z #12 28.56 ",  # a docker build in GitHub Actions
    ],
)
def test_docker_buildkit_prefixes_are_skipped(prefix):
    text = "".join(prefix + line + "\n" for line in ("Collecting nowheels==0.9", *BUILD_FAILED.splitlines()))
    assert failed_builds(text + prefix.rstrip() + "\n") == [("nowheels", "0.9")]  # and a prefix alone on a line


def test_a_step_number_without_seconds_is_not_a_prefix():
    # `#12 [2/2] RUN ...` and `#12 DONE 0.5s` are BuildKit's own lines: only `#N SECONDS ` is stripped.
    text = "#12 DONE 0.5s\nCollecting nowheels==0.9\n" + BUILD_FAILED.replace("\nFailed", "\n#12 Failed")
    with pytest.raises(LogError, match="found no package"):
        failed_builds(text)


PREPARE_FAILED = [
    "  × Getting requirements to build wheel did not run successfully.",
    "× Getting requirements to build editable did not run successfully.",
    "  × Preparing metadata (pyproject.toml) did not run successfully.",
    "error: metadata-generation-failed",
    "× Encountered error while generating package metadata.",
]


@pytest.mark.parametrize("error", PREPARE_FAILED)
def test_a_failure_before_building_is_the_sdist_collected_last(error):
    text = (
        "Collecting fastlib\n  Downloading fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl (1 MB)\n"
        "Collecting nowheels>=0.5 (from fastlib)\n  Downloading nowheels-0.9.tar.gz (5 kB)\n"
        "  Installing build dependencies ... done\n"
        f"{error}\n"
    )
    assert failed_builds(text) == [("nowheels", "0.9")]


@pytest.mark.parametrize(
    "before",
    [
        "Collecting nowheels\n  Downloading nowheels-0.9-py3-none-any.whl (5 kB)\n",  # a wheel, not an sdist
        "Collecting nowheels==0.9\n",  # no sdist named
        # A local project after an sdist that was prepared fine.
        "Collecting nowheels==0.9\n  Downloading nowheels-0.9.tar.gz (5 kB)\nProcessing ./myproject\n",
        # A build dependency's sdist, under `pip install -v`, isn't the package being collected.
        "Collecting fastlib\n    Downloading nowheels-0.9.tar.gz (5 kB)\n",
        "",
    ],
)
def test_a_failure_before_building_without_its_sdist_is_refused(before):
    with pytest.raises(SourceError, match="doesn't name its sdist after the last `Collecting` line"):
        failed_builds(before + "  Getting requirements to build wheel ... error\n" + PREPARE_FAILED[0] + "\n")


def test_a_failure_before_building_in_a_build_dependencys_pip_is_not_this_runs():
    # The pip that installs build dependencies indents its own output further.
    text = "Collecting nowheels==0.9\n  Downloading nowheels-0.9.tar.gz (5 kB)\n      " + PREPARE_FAILED[0].strip()
    with pytest.raises(LogError, match="found no package"):
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


@pytest.mark.parametrize(
    "name,prefix,kind",
    [
        ("pip23-laggard.log", INDEXES, OTHER_INDEX),
        ("pip23-laggard.log", "Looking in links: /wheels\n", FIND_LINKS),
        ("pip-succeeded.log", "", NO_FAILURE),
    ],
)
def test_refusals_a_caller_may_word_itself_have_a_kind(name, prefix, kind):
    # The MCP tool writes its own message for these, since the first two quote the log's URLs.
    with pytest.raises(LogError) as e:
        failed_builds(prefix + log(name))
    assert e.value.kind == kind
    assert isinstance(e.value, SourceError)  # the command line reports it as before


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
        ("pip-two-versions.log", "forked failed to build at more than one version"),  # never asks PyPI for it
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


def test_no_version_in_the_log_checks_the_latest_release_with_a_note(capsys, fake_pypi):
    code, out, err = run(capsys, *from_log("pip-no-version.log", "-t", "win-amd64", "--json"))
    assert code == 1
    (pkg,) = json.loads(out)["packages"]
    assert (pkg["name"], pkg["version"], pkg["results"]) == ("nowheels", "0.9", {"win-amd64": "source"})
    assert fake_pypi[0] == "https://pypi.org/pypi/nowheels/json"
    assert "these failed to build: nowheels (the log doesn't name the version)\n" in err
    assert (
        "wheelgap: note: the log doesn't name the version of nowheels that failed to build; checked nowheels 0.9, "
        "the latest release" in err
    )


def test_a_pinned_log_has_no_latest_note(capsys, fake_pypi):
    _, _, err = run(capsys, *from_log("uv-ansi.log", "-t", "win-arm64"))
    assert "the latest release" not in err


_FOO_FAILED = "  ERROR: Failed building wheel for foo\nFailed to build foo\n"


@pytest.mark.parametrize(
    ("log", "refusal"),
    [
        ("Run pip install -q .\n" + _FOO_FAILED, "local project"),
        ("$ pip install -q -e .\n" + _FOO_FAILED, "local project"),
        ("PS C:\\src> pip install -q C:\\src\\foo\n" + _FOO_FAILED, "local project"),
        ("$ pip install -q foo==1.0\n$ pip install -q --upgrade foo\n" + _FOO_FAILED, "more than one version"),
        (
            "#7 1.2 Collecting foo==1.0\n#7 1.3   Downloading foo-1.0.tar.gz\n#8 1.4 Collecting foo==2.0\n"
            "#8 1.5   Downloading foo-2.0.tar.gz\n#7 9.0   Building wheel for foo (pyproject.toml): started\n"
            "#7 9.1   ERROR: Failed building wheel for foo\n#7 9.2 Failed to build foo\n",
            "more than one Docker build step",
        ),
    ],
)
def test_install_commands_and_buildkit_steps_that_make_the_version_unclear_are_refused(log, refusal):
    with pytest.raises(SourceError, match=refusal):
        failed_builds(log)


@pytest.mark.parametrize(
    "prefix", ["# RUN pip install foo==1.0\n", "echo 'pip install foo==1.0'\n", "      hint: pip install foo==0.9\n"]
)
def test_a_commented_or_echoed_pin_is_not_the_version(prefix):
    assert failed_builds(prefix + _FOO_FAILED) == [("foo", None)]


def test_the_latest_release_note_is_in_json_and_markdown(capsys, fake_pypi):
    # A report read without stderr must not show the latest release as the log's version.
    _, out, _ = run(capsys, *from_log("pip-no-version.log", "-t", "win-amd64", "--json"))
    (note,) = json.loads(out)["from_log_notes"]
    assert (note["name"], note["version"], note["kind"]) == ("nowheels", "0.9", "latest")
    assert note["message"].startswith("the log doesn't name the version of nowheels that failed to build")
    _, out, _ = run(capsys, *from_log("pip-no-version.log", "-t", "win-amd64", "--markdown"))
    assert "> **Note:** the log doesn't name the version of nowheels that failed to build; checked nowheels 0.9" in out


def test_a_pinned_log_has_empty_from_log_notes(capsys, fake_pypi):
    _, out, _ = run(capsys, *from_log("pip23-laggard.log", "-t", "win-amd64", "--json"))
    assert json.loads(out)["from_log_notes"] == []


def test_uv_failures_are_read_when_buildkit_steps_interleave():
    # uv names the version on its failure line, so other steps' lines in between don't matter.
    text = "#7 1.0 Resolved 1 package\n#8 1.1 Collecting other==1.0\n#7 2.0 × Failed to build `nowheels==0.9`\n"
    assert failed_builds(text) == [("nowheels", "0.9")]


def test_a_failure_before_building_is_refused_when_buildkit_steps_interleave():
    text = (
        "#7 1.0 Collecting nowheels==0.9\n#7 1.1   Downloading nowheels-0.9.tar.gz (5 kB)\n#8 1.2 other output\n"
        "#7 2.0   × Getting requirements to build wheel did not run successfully.\n"
    )
    with pytest.raises(SourceError, match="more than one Docker build step"):
        failed_builds(text)
