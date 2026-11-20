"""scripts/check_against_pip.py (#226), offline: the pip arguments it builds, how it reads pip's answer (with a fake
subprocess.run) and how it reports (with a fake pip)."""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from wheelgap import Result, make_targets
from wheelgap.check import Status


def _script():
    path = Path(__file__).parents[1] / "scripts" / "check_against_pip.py"
    spec = importlib.util.spec_from_file_location("wheelgap_check_against_pip", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()


def _target(platform, python="3.14", **versions):
    return make_targets(python, platform, **versions)[0]


def test_pip_platforms_for_linux_x86_64_are_agents_md_example():
    # AGENTS.md: `seq 36 -1 5`, then manylinux2014, manylinux2010 and manylinux1, and plain linux_<arch>.
    expected = [f"manylinux_2_{m}_x86_64" for m in range(36, 4, -1)]
    expected += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"]
    assert script.pip_platforms(_target("linux-x86_64")) == expected


@pytest.mark.parametrize(
    ("platform", "versions", "expected"),
    [
        (
            "linux-aarch64",
            {},
            [f"manylinux_2_{m}_aarch64" for m in range(36, 16, -1)] + ["manylinux2014_aarch64", "linux_aarch64"],
        ),
        (
            "linux-x86_64",
            {"glibc": "2.17"},
            [f"manylinux_2_{m}_x86_64" for m in range(17, 4, -1)]
            + ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"],
        ),
        (
            "linux-x86_64",
            {"glibc": "2.12"},
            [f"manylinux_2_{m}_x86_64" for m in range(12, 4, -1)]
            + ["manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"],
        ),
        ("linux-armv7l", {"glibc": "2.17"}, ["manylinux_2_17_armv7l", "manylinux2014_armv7l", "linux_armv7l"]),
        ("musl-aarch64", {}, ["musllinux_1_2_aarch64", "musllinux_1_1_aarch64", "musllinux_1_0_aarch64"]),
        ("musl-x86_64", {"musl": "1.1"}, ["musllinux_1_1_x86_64", "musllinux_1_0_x86_64"]),
        ("win-arm64", {}, ["win_arm64"]),
        ("win-amd64", {}, ["win_amd64"]),
        ("macos-arm64", {}, ["macosx_14_0_arm64"]),
        ("macos-arm64", {"macos": "13.5"}, ["macosx_13_5_arm64"]),
    ],
)
def test_pip_platforms(platform, versions, expected):
    assert script.pip_platforms(_target(platform, **versions)) == expected


def test_pip_platforms_match_what_wheelgap_accepts_on_linux():
    # Written out independently, but they must end up with the same tags as targets.py (#79 was a difference here).
    for platform in ("linux-x86_64", "linux-aarch64", "musl-x86_64", "musl-aarch64"):
        for glibc in ("2.17", "2.28", "2.36"):
            t = _target(platform, glibc=glibc)
            assert sorted(script.pip_platforms(t)) == sorted(t.platform_tags()), t


@pytest.mark.parametrize("platform", ["linux-riscv64", "pyodide"])
def test_pip_platforms_refuses_targets_agents_md_has_no_tags_for(platform):
    with pytest.raises(ValueError):
        script.pip_platforms(_target(platform, python="3.13"))


def test_pip_args_free_threaded():
    args = script.pip_args("numpy", "2.3.4", _target("win-amd64", python="3.14t"))
    assert args[:5] == ["install", "--dry-run", "--ignore-installed", "--no-deps", "--only-binary=:all:"]
    assert args[-1] == "numpy==2.3.4"
    assert args[args.index("--abi") + 1] == "cp314t"
    assert args[args.index("--python-version") + 1] == "3.14"
    assert args[args.index("--index-url") + 1] == "https://pypi.org/simple"


def test_default_targets():
    targets = script.default_targets()
    labels = [script.label(t) for t in targets]
    assert len(set(labels)) == len(labels) == 9
    assert "3.14 linux-x86_64 glibc 2.17" in labels
    free = [t for t in targets if t.python == "3.14t"]
    assert len(free) == 1 and free[0].freethreaded
    assert script.pip_args("x", "1", free[0])[-2] == "cp314t"
    for t in targets:
        script.pip_platforms(t)  # every default target has pip tags


# run_pip, with subprocess.run faked: pip's exit code, its log, and the --report it writes.

FETCHED = "Fetched page https://pypi.org/simple/my-pkg/ as application/vnd.pypi.simple.v1+json"
NO_MATCH = "ERROR: No matching distribution found for My_Pkg==1.0"


def _report(*urls):
    return {"version": "1", "install": [{"download_info": {"url": url}} for url in urls]}


def _fake_run(monkeypatch, returncode=0, stdout="", stderr="", report=None, timeout=False):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if report is not None:
            Path(cmd[cmd.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(script.subprocess, "run", run)
    return calls


def _run_pip():
    return script.run_pip("My_Pkg", script.pip_args("My_Pkg", "1.0", _target("win-arm64")))


def test_run_pip_wheel(monkeypatch):
    url = "https://files.pythonhosted.org/packages/ab/cd/my_pkg-1.0-cp314-cp314-win_arm64.whl"
    calls = _fake_run(monkeypatch, stdout=f"{FETCHED}\nWould install My_Pkg-1.0", report=_report(url))
    assert _run_pip() == script.InstallerAnswer(wheel="my_pkg-1.0-cp314-cp314-win_arm64.whl")
    cmd, kwargs = calls[0]
    assert cmd[1:3] == ["-m", "pip"]
    assert {"--isolated", "--no-cache-dir", "--disable-pip-version-check", "-vv"} <= set(cmd)
    assert not Path(cmd[cmd.index("--report") + 1]).parent.exists()  # the report's folder is deleted


def test_run_pip_no_wheel_needs_the_project_page(monkeypatch):
    _fake_run(monkeypatch, returncode=1, stdout=FETCHED, stderr=NO_MATCH)
    assert _run_pip() == script.InstallerAnswer()


@pytest.mark.parametrize(
    ("stdout", "stderr", "why"),
    [
        # pip retried, then gave up and said no match: an outage, not an answer.
        (
            "WARNING: Retrying (Retry(total=4, connect=None, read=None)) after connection broken by "
            "'NewConnectionError(...)': /simple/my-pkg/",
            NO_MATCH,
            "Retrying",
        ),
        # A 404 or 429 on the index page: pip skips it and says no match.
        (
            "Could not fetch URL https://pypi.org/simple/my-pkg/: 429 Client Error: Too Many Requests - skipping",
            NO_MATCH,
            "Could not fetch URL",
        ),
        # Even with the page fetched, a retry on the way means pip may not have seen everything.
        (f"{FETCHED}\nWARNING: Retrying (Retry(total=4)) after ...: /simple/my-pkg/", NO_MATCH, "Retrying"),
        # No sign the page was fetched (another project's page doesn't count).
        (
            "Fetched page https://pypi.org/simple/other/ as application/vnd.pypi.simple.v1+json",
            NO_MATCH,
            "didn't fetch",
        ),
        # Some other failure.
        (FETCHED, "ERROR: Exception: something broke", "something broke"),
    ],
)
def test_run_pip_errors_instead_of_no_wheel(monkeypatch, stdout, stderr, why):
    _fake_run(monkeypatch, returncode=1, stdout=stdout, stderr=stderr)
    answer = _run_pip()
    assert answer.wheel is None and answer.error and why in answer.error


def test_run_pip_timeout(monkeypatch):
    _fake_run(monkeypatch, timeout=True)
    assert _run_pip() == script.InstallerAnswer(error=f"pip timed out after {script.PIP_TIMEOUT} s")


@pytest.mark.parametrize(
    ("returncode", "report"),
    [
        (1, _report("https://files.example/my_pkg-1.0-py3-none-any.whl")),  # failed, whatever the report says
        (0, None),  # succeeded without a report
        (0, _report()),  # succeeded, installing nothing
        (0, _report("https://files.example/my_pkg-1.0.tar.gz")),  # an sdist, despite --only-binary
        (0, _report("https://files.example/a-1-py3-none-any.whl", "https://files.example/b-1-py3-none-any.whl")),
    ],
)
def test_run_pip_unexpected_results_are_errors(monkeypatch, returncode, report):
    _fake_run(monkeypatch, returncode=returncode, stdout=FETCHED, report=report)
    answer = _run_pip()
    assert answer.wheel is None and answer.error


def test_run_pip_ignores_pip_environment(monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("pip_no_cache_dir", "0")
    calls = _fake_run(monkeypatch, returncode=1, stdout=FETCHED, stderr=NO_MATCH)
    _run_pip()
    env = calls[0][1]["env"]
    assert not [key for key in env if key.upper().startswith("PIP_")]
    assert env.get("PATH") == __import__("os").environ.get("PATH")


# compare() and report(), with a fake pip.


def _fake_pip(wheels):
    """A pip that picks a wheel for the (package, platform) pairs in `wheels`; None means pip gives no answer."""
    asked = []

    def pip(name, args):
        assert args[-1].startswith(f"{name}==")
        platform = args[args.index("--platform") + 1]
        asked.append((name, platform))
        answer = wheels.get((name, platform), False)
        if answer is None:
            return script.InstallerAnswer(error="pip: connection reset")
        return script.InstallerAnswer(wheel=f"{name}-1.whl") if answer else script.InstallerAnswer()

    pip.asked = asked
    return pip


def _run(packages, targets, wheels):
    pip = _fake_pip(wheels)
    out = io.StringIO()
    code = script.report(script.compare(packages, targets, pip, workers=2), out=out)
    return code, out.getvalue(), pip.asked


def test_agreement_exits_0(fake_pypi):
    # fastlib 2.0.0 has a cp313 manylinux_2_17_x86_64 wheel and an sdist; nowheels is sdist-only.
    targets = make_targets("3.13", ["linux-x86_64", "win-amd64"])
    code, out, asked = _run(["fastlib", "nowheels"], targets, {("fastlib", "manylinux_2_36_x86_64"): True})
    assert code == 0
    assert out.startswith("4 checks: 4 agree with pip (1 wheel, 3 no wheel), 0 disagree, 0 couldn't be compared.")
    assert sorted(asked) == [
        ("fastlib", "manylinux_2_36_x86_64"),
        ("fastlib", "win_amd64"),
        ("nowheels", "manylinux_2_36_x86_64"),
        ("nowheels", "win_amd64"),
    ]


def test_wheelgap_is_asked_about_the_pinned_release(fake_pypi):
    # yanked 1.0's only file is a yanked wheel. Unpinned, wheelgap skips it (missing); pinned with ==, as pip is
    # asked here, pip installs it (PEP 592), and so does wheelgap when it's asked pinned too.
    targets = make_targets("3.13", ["linux-x86_64"])
    code, out, _ = _run(["yanked"], targets, {("yanked", "manylinux_2_36_x86_64"): True})
    assert code == 0, out
    assert any(url.endswith("/yanked/1.0/json") for url in fake_pypi)


def test_yanked_wheel_taken_only_because_of_only_binary_is_not_compared(fake_pypi):
    # halfyanked 2.0 has a yanked wheel and a live sdist: pip builds the sdist (wheelgap: source), but with
    # --only-binary it can only take the yanked wheel. That's not a disagreement, and not an agreement either.
    targets = make_targets("3.13", ["linux-x86_64"])
    checks = script.compare(
        ["halfyanked"], targets, lambda name, args: script.InstallerAnswer(wheel="halfyanked-2.0-py3-none-any.whl")
    )
    out = io.StringIO()
    assert script.report(checks, out=out) == 2
    assert "--only-binary hides the sdist" in out.getvalue()


def test_disagreement_exits_1_with_table_and_pip_command(fake_pypi):
    targets = make_targets("3.13", ["linux-x86_64", "win-amd64"])
    # pip finds a wheel wheelgap doesn't see on Windows, and none where wheelgap sees one on Linux.
    code, out, _ = _run(["fastlib"], targets, {("fastlib", "win_amd64"): True})
    assert code == 1
    assert "2 checks: 0 agree with pip (0 wheel, 0 no wheel), 2 disagree" in out
    assert "fastlib  2.0.0    3.13 linux-x86_64  wheel     no wheel" in out
    assert "fastlib  2.0.0    3.13 win-amd64     source    fastlib-1.whl" in out
    assert (
        "pip install --dry-run --ignore-installed --no-deps --only-binary=:all: --index-url https://pypi.org/simple "
        "--platform win_amd64 --python-version 3.13 --implementation cp --abi cp313 fastlib==2.0.0"
    ) in out


def test_missing_is_not_a_wheel(fake_pypi):
    # winonly has no sdist: missing on Linux. pip finding a wheel there is a disagreement.
    targets = make_targets("3.13", ["linux-x86_64"])
    code, out, _ = _run(["winonly"], targets, {("winonly", "manylinux_2_36_x86_64"): True})
    assert code == 1
    assert "winonly  311      3.13 linux-x86_64  missing   winonly-1.whl" in out


def test_unknown_is_never_compared():
    target = _target("win-amd64")
    check = script.Check("x", "1", target, Status.UNKNOWN, script.InstallerAnswer())
    assert not check.compared  # even though (UNKNOWN is WHEEL) == (no wheel) would "agree"
    out = io.StringIO()
    assert script.report([check], out=out) == 2
    assert "wheelgap says unknown" in out.getvalue()


def test_unchecked_exits_2_and_skips_pip_for_failed_lookups(fake_pypi):
    targets = make_targets("3.13", ["win-amd64"])
    code, out, asked = _run(["not-on-index", "fastlib"], targets, {("fastlib", "win_amd64"): None})
    assert code == 2
    assert "0 agree with pip (0 wheel, 0 no wheel), 0 disagree, 2 couldn't be compared" in out
    assert "not-on-index  -        3.13 win-amd64  not on index" in out
    assert "pip: connection reset" in out
    assert asked == [("fastlib", "win_amd64")]  # pip isn't asked about a package wheelgap couldn't look up


def test_disagreement_wins_over_unchecked(fake_pypi):
    targets = make_targets("3.13", ["win-amd64"])
    code, _, _ = _run(["not-on-index", "fastlib"], targets, {("fastlib", "win_amd64"): True})
    assert code == 1


# main()


@pytest.fixture
def pip_installed(monkeypatch):
    monkeypatch.setattr(
        script.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "pip 26.3 from x\n", "")
    )


@pytest.mark.parametrize(("checks", "code"), [("agree", 0), ("disagree", 1), ("unknown", 2)])
def test_main_exit_codes(monkeypatch, capsys, pip_installed, checks, code):
    target = _target("win-arm64")
    answers = {
        "agree": script.InstallerAnswer(),
        "disagree": script.InstallerAnswer(wheel="x-1.whl"),
        "unknown": script.InstallerAnswer(error="pip: boom"),
    }
    seen = {}

    def compare(packages, targets, workers, installer):
        seen.update(packages=packages, targets=targets, workers=workers)
        return [script.Check("x", "1", target, Status.SOURCE, answers[checks])]

    monkeypatch.setattr(script, "compare", compare)
    assert script.main(["-t", "win-arm64", "--workers", "3"]) == code
    assert seen["packages"] == script.PACKAGES
    assert [script.label(t) for t in seen["targets"]] == ["3.14 win-arm64"]
    assert seen["workers"] == 3


def test_main_needs_pip(monkeypatch, capsys):
    monkeypatch.setattr(script.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no pip"))
    with pytest.raises(SystemExit) as e:
        script.main(["numpy"])
    assert e.value.code == 2
    assert "uv run --with pip" in capsys.readouterr().err


def test_main_unknown_target(capsys):
    with pytest.raises(SystemExit) as e:
        script.main(["-t", "linux-riscv64"])
    assert e.value.code == 2
    assert "3.14 linux-x86_64 glibc 2.17" in capsys.readouterr().err


def test_crash_exits_2(monkeypatch, capsys):
    def crash(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(script, "main", crash)
    assert script.run([]) == 2
    assert "RuntimeError: boom" in capsys.readouterr().err


# uv 0.13.8 uses this format with --color never and redirected output.
UV_NO_SOLUTION = "error: No solution found when resolving dependencies\n"
UV_NO_WHEEL = (
    UV_NO_SOLUTION + "  cause: Because docopt==0.6.2 has no usable wheels and you require docopt==0.6.2, "
    "we can conclude that your requirements are unsatisfiable.\n"
)
UV_WRONG_ABI = (
    UV_NO_SOLUTION + "  cause: Because pywin32==301 has no wheels with a matching Python ABI tag (e.g., `cp314`) "
    "and you require pywin32==301, we can conclude that your requirements are unsatisfiable.\n"
)


def test_uv_platforms_for_default_targets():
    expected = [
        "x86_64-manylinux_2_36",
        "aarch64-manylinux_2_36",
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
        "aarch64-apple-darwin",
        None,
        "x86_64-manylinux_2_17",
    ]
    for target, platform in zip(script.default_targets(), expected, strict=True):
        assert script.uv_platform(target) == platform
        if platform is not None:
            cmd = script.uv_command(target, platform)
            assert cmd[cmd.index("--python-platform") + 1] == platform
            assert cmd[cmd.index("--python-version") + 1] == "3.14"


@pytest.mark.parametrize(
    ("platform", "versions"),
    [("musl-aarch64", {"musl": "1.1"}), ("linux-aarch64", {"glibc": "2.30"}), ("linux-armv7l", {})],
)
def test_uv_platforms_refuse_unsupported_libcs_and_arches(platform, versions):
    assert script.uv_platform(_target(platform, **versions)) is None


def test_run_uv_compiles_the_pin_in_isolation(monkeypatch):
    monkeypatch.setenv("UV_INDEX", "https://example.invalid/simple")
    monkeypatch.setenv("uv_offline", "true")
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "99.0")
    monkeypatch.setenv("COLUMNS", "40")  # uv wraps its diagnostics to it, which classify_uv() can't read
    calls = _fake_run(monkeypatch, stdout="my-pkg==1.0\n")
    target = _target("macos-arm64", macos="14.5")
    assert script.run_uv("My_Pkg", "1.0", target, "aarch64-apple-darwin").wheel == "wheel"
    cmd, kwargs = calls[0]
    assert cmd[:4] == ["uv", "pip", "compile", "-"]
    assert {"--only-binary", ":all:", "--no-cache", "--no-config", "--no-deps"} <= set(cmd)
    assert cmd[cmd.index("--default-index") + 1] == script.INDEX
    assert kwargs["input"] == "my-pkg==1.0\n"
    assert kwargs["env"]["MACOSX_DEPLOYMENT_TARGET"] == "14.5"
    # None of the caller's UV_* settings: only the one that stops uv wrapping its diagnostics.
    assert {key: value for key, value in kwargs["env"].items() if key.upper().startswith("UV_")} == {"UV_NO_WRAP": "1"}
    assert "COLUMNS" not in {key.upper() for key in kwargs["env"]}
    # uv's tags take their implementation and free-threaded ABI from the interpreter: it gets this one.
    assert cmd[cmd.index("--python") + 1] == sys.executable


@pytest.mark.parametrize(
    ("pin", "log"),
    [
        ("docopt==0.6.2", UV_NO_WHEEL),
        ("pywin32==301", UV_WRONG_ABI),
        (
            "pyzmq==27.3.0",
            "WARN Fixing invalid version specifier by inserting missing dot (before: `>=2.7,!=3.0*`, "
            "after: `>=2.7,!=3.0.*`)\n" + UV_NO_WHEEL.replace("docopt==0.6.2", "pyzmq==27.3.0"),
        ),
    ],
)
def test_uv_no_wheel_requires_a_positive_diagnostic(pin, log):
    assert script.classify_uv(pin, 1, "", log) == script.InstallerAnswer()


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (2, "", "error: Request failed after 3 retries\n  cause: Connection reset by peer"),
        (1, "", UV_NO_SOLUTION + "  cause: Because docopt was not found in the package registry"),
        (1, "", UV_NO_WHEEL.replace("docopt==0.6.2", "other==1")),
        (1, "", "error: Request failed after 3 retries\n" + UV_NO_WHEEL),
        (0, "", ""),
        (0, "other==1\n", ""),
    ],
)
def test_uv_transport_and_unexpected_output_are_errors(code, stdout, stderr):
    answer = script.classify_uv("docopt==0.6.2", code, stdout, stderr)
    assert answer.wheel is None and answer.error


def test_uv_wrapped_diagnostic_is_an_error_not_no_wheel():
    # What uv prints for a release without wheels when COLUMNS is narrow; run_uv() keeps uv from wrapping.
    wrapped = (
        UV_NO_SOLUTION + "  cause: Because docopt==0.6.2 has no usable wheels and you require\n"
        "         docopt==0.6.2, we can conclude that your requirements are unsatisfiable.\n"
    )
    answer = script.classify_uv("docopt==0.6.2", 1, "", wrapped)
    assert answer.wheel is None and answer.error


def test_uv_error_keeps_uvs_cause():
    log = UV_NO_SOLUTION + "  cause: Because docopt was not found in the package registry and you require docopt\n"
    answer = script.classify_uv("docopt==0.6.2", 1, "", log)
    assert answer.error is not None
    assert answer.error.startswith("uv: error: No solution found when resolving dependencies (cause: Because docopt")
    assert "was not found in the package registry" in answer.error


def test_run_uv_timeout(monkeypatch):
    _fake_run(monkeypatch, timeout=True)
    answer = script.run_uv("docopt", "0.6.2", _target("win-amd64"), "x86_64-pc-windows-msvc")
    assert answer == script.InstallerAnswer(error=f"uv timed out after {script.UV_TIMEOUT} s")


def test_uv_report_lists_unsupported_targets_and_ignores_yanked_only_binary_wheels(fake_pypi, monkeypatch):
    normal = _target("linux-x86_64", python="3.13")
    threaded = _target("linux-x86_64", python="3.14t")
    calls = _fake_run(
        monkeypatch, stdout="halfyanked==2.0\n", stderr='warning: `halfyanked==2.0` is yanked (reason: "test")'
    )
    checks = script.compare(["halfyanked"], [normal, threaded], installer="uv")
    assert len(calls) == 1
    out = io.StringIO()
    assert script.report(checks, out=out, installer="uv") == 2
    assert "uv took a yanked wheel because --only-binary hides the sdist" in out.getvalue()
    assert "uv pip compile cannot target free-threaded Python" in out.getvalue()


def test_uv_report_emits_a_reproduction_command(fake_pypi, monkeypatch):
    macos = _target("macos-arm64", python="3.13")
    _fake_run(monkeypatch, stdout="nowheels==0.9\n")
    checks = script.compare(["nowheels"], [macos], installer="uv")
    out = io.StringIO()
    assert script.report(checks, out=out, installer="uv") == 1
    assert "uv commands:" in out.getvalue()
    assert "printf '%s\\n' nowheels==0.9 | MACOSX_DEPLOYMENT_TARGET=14.0 uv pip compile" in out.getvalue()


def test_uv_skips_work_when_none_of_the_selected_targets_are_supported(monkeypatch):
    calls = _fake_run(monkeypatch)
    checks = script.compare(["numpy"], [_target("linux-x86_64", python="3.14t")], installer="uv")
    out = io.StringIO()
    assert script.report(checks, out=out, installer="uv") == 2
    assert "uv pip compile cannot target free-threaded Python" in out.getvalue()
    assert not calls


# A pin is a requirements-style input, which wheelgap answers as pip installs it. uv reads Requires-Python as a
# lower bound only, so it takes a wheel that pip leaves out for an upper bound: uv answering as uv, not an error.

CAPPED = {"capped-1.0-py3-none-any.whl": "requires Python >=3.9,<3.14"}


def _always(answer):
    return lambda name, version, target: answer


def test_uv_taking_a_wheel_past_requires_pythons_upper_bound_is_not_a_disagreement():
    target = _target("linux-x86_64")
    capped = Result("capped", "1.0", {target: Status.MISSING}, requires_python_rejected=CAPPED)
    fine = Result("fine", "1.0", {target: Status.WHEEL})
    wheel = _always(script.InstallerAnswer(wheel="wheel"))
    checks = [*script.check_one(capped, [target], wheel, "uv"), *script.check_one(fine, [target], wheel, "uv")]
    assert [c.compared for c in checks] == [False, True]
    out = io.StringIO()
    assert script.report(checks, out=out, installer="uv") == 0  # listed, but it doesn't fail the run
    text = out.getvalue()
    assert text.startswith("2 checks: 1 agree with uv (1 wheel, 0 no wheel), 0 disagree, 1 couldn't be compared.")
    assert "wheelgap answers a pin as pip installs it" in text
    assert "uv took a wheel that requires Python >=3.9,<3.14: uv ignores Requires-Python's upper bound" in text
    assert "Disagreements" not in text


@pytest.mark.parametrize(
    ("installer", "rejected", "status", "wheel"),
    [
        ("pip", CAPPED, Status.MISSING, True),  # pip is the referee for a pin: nothing excuses a difference from it
        ("uv", {"capped-1.0-py3-none-any.whl": "requires Python >=3.15"}, Status.MISSING, True),  # uv reads a floor
        ("uv", {"capped-1.0.tar.gz": "requires Python >=3.9,<3.14"}, Status.MISSING, True),  # no wheel was left out
        ("uv", {}, Status.SOURCE, True),
        ("uv", CAPPED, Status.WHEEL, False),  # uv found no wheel where wheelgap found one
    ],
)
def test_other_differences_from_an_installer_stay_disagreements(installer, rejected, status, wheel):
    target = _target("linux-x86_64")
    result = Result("capped", "1.0", {target: status}, requires_python_rejected=rejected)
    answer = script.InstallerAnswer(wheel="wheel" if wheel else None)
    (check,) = script.check_one(result, [target], _always(answer), installer)
    assert check.skipped is None and check.compared and not check.agrees
    out = io.StringIO()
    assert script.report([check], out=out, installer=installer) == 1
    if installer == "uv":
        assert "Disagreements (a pin follows pip, so check the pip run before calling one a wheelgap error)" in (
            out.getvalue()
        )


def test_main_uv_needs_a_default_cpython(monkeypatch, capsys):
    # On a free-threaded build (or PyPy) uv would answer for that interpreter's tags, not the target's.
    monkeypatch.setattr(script, "default_cpython", lambda: False)
    calls = _fake_run(monkeypatch)
    with pytest.raises(SystemExit) as e:
        script.main(["--installer", "uv", "numpy"])
    assert e.value.code == 2
    assert "default (GIL) build of CPython" in capsys.readouterr().err
    assert not calls
