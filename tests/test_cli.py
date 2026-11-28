import json
import platform
import shutil
import sys
import sysconfig
from pathlib import Path

import packaging
import pytest
from test_deps import index  # noqa: F401  (the fake PyPI --deps resolves against)

import wheelgap
from wheelgap import cli
from wheelgap.check import Status
from wheelgap.cli import main
from wheelgap.targets import ALIASES, DEFAULT_PLATFORMS, PLATFORMS

NOTES_HINT = (
    "wheelgap: add --notes to show the tracker's notes for these packages (sends their names to wheelgap.github.io)\n"
)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize(
    "markers,expected",
    [
        (["sys_platform == 'linux'"], ["linux-x86_64", "linux-aarch64", "musl-x86_64", "musl-aarch64"]),
        (["platform_machine == 'aarch64'"], ["linux-aarch64", "musl-aarch64"]),
        (
            ["sys_platform == 'darwin'", "sys_platform == 'win32'"],
            ["macos-arm64", "macos-x86_64", "win-amd64", "win-arm64"],
        ),
        (
            ["sys_platform == 'linux' and platform_machine == 'aarch64'", "platform_machine == 'aarch64'"],
            ["linux-aarch64", "musl-aarch64"],
        ),
    ],
)
@pytest.mark.parametrize("input_kind", ["directory", "file", "package"])
def test_uv_required_environments(capsys, tmp_path, monkeypatch, fake_pypi, markers, expected, input_kind):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = " + json.dumps(markers), encoding="utf-8"
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    item = {"directory": str(tmp_path), "file": str(requirements), "package": "purewheel==3.1"}[input_kind]
    code, out, err = run(capsys, item, "-p", "3.13", "--json")
    assert code == 0
    assert [t["platform"] for t in json.loads(out)["targets"]] == expected
    project_path = "pyproject.toml" if input_kind == "package" else str(tmp_path / "pyproject.toml")
    assert "[tool.uv] required-environments" in err and project_path in err
    assert all(name in err for name in expected)
    if "musl-aarch64" in expected:
        assert "glibc" in err and "musl" in err
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/3.1/json"]


@pytest.mark.parametrize(
    "config,image,flags,expected",
    [
        ("", False, ["-t", "win-amd64"], "win-amd64"),
        ('targets = ["macos-arm64"]', False, [], "macos-arm64"),
        ('targets = ["macos-arm64"]', True, [], "musl-x86_64"),
        ('targets = ["macos-arm64"]', True, ["-t", "win-amd64"], "win-amd64"),
    ],
)
def test_uv_required_environments_precedence(capsys, tmp_path, monkeypatch, fake_pypi, config, image, flags, expected):
    # Even invalid uv settings are irrelevant when another source selects the targets.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv]\nrequired-environments = ["invalid marker"]\n[tool.wheelgap]\n' + config,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    if image:
        (tmp_path / "Dockerfile").write_text("FROM python:3.13-alpine\n", encoding="utf-8")
        flags = [*flags, "--dockerfile", str(tmp_path / "Dockerfile")]
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--json", *flags)
    assert code == 0 and not err
    assert [t["platform"] for t in json.loads(out)["targets"]] == [expected]


@pytest.mark.parametrize("python_source", ["flag", "config", "version-file", "running", "multiple"])
def test_uv_required_environments_uses_selected_python(capsys, tmp_path, monkeypatch, fake_pypi, python_source):
    text = "[tool.uv]\nrequired-environments = [\"python_version >= '3.13' and sys_platform == 'win32'\"]\n"
    flags = []
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    if python_source == "config":
        text += '[tool.wheelgap]\npython = ["3.13"]\n'
    elif python_source == "version-file":
        (tmp_path / ".python-version").write_text("3.13\n", encoding="utf-8")
    elif python_source == "running":
        (tmp_path / ".python-version").unlink()
        monkeypatch.setattr(cli, "running_python", lambda: "3.13")
    else:
        flags = ["-p", "3.12,3.13"] if python_source == "multiple" else ["-p", "3.13"]
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "--json", *flags)
    assert code == 0 and "required-environments" in err
    expected_pys = ["3.12", "3.13"] if python_source == "multiple" else ["3.13"]
    assert [(t["python"], t["platform"]) for t in json.loads(out)["targets"]] == [
        (py, plat) for py in expected_pys for plat in ("win-amd64", "win-arm64")
    ]


@pytest.mark.parametrize(
    "setting,message",
    [
        ("required-environments = 42", "a string or a list of strings"),
        ('required-environments = "linux"', "invalid marker"),
        ("required-environments = [42]", "list of strings"),
        ('required-environments = ["sys_platform == \'linux\'", "invalid"]', "invalid marker"),
        ('required-environments = [""]', "invalid marker"),
    ],
)
def test_uv_required_environments_errors(capsys, tmp_path, monkeypatch, fake_pypi, setting, message):
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--json")
    assert code == 2 and not fake_pypi
    assert message in err and "required-environments" in err
    assert "error" in json.loads(out)


def test_uv_required_environments_accepts_a_single_string(capsys, tmp_path, monkeypatch, fake_pypi):
    # uv documents required-environments as a string or a list of strings.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = \"sys_platform == 'win32'\"", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--json")
    assert code == 0 and "required-environments" in err
    assert [t["platform"] for t in json.loads(out)["targets"]] == ["win-amd64", "win-arm64"]


@pytest.mark.parametrize(
    "setting",
    [
        "required-environments = [\"sys_platform == 'emscripten'\"]",
        "required-environments = [\"sys_platform == 'android'\"]",
        "required-environments = [\"python_version < '3.13'\"]",
    ],
)
def test_uv_required_environments_matching_no_target_falls_back_to_defaults(
    capsys, tmp_path, monkeypatch, fake_pypi, setting
):
    # A mobile-only project, or `wheelgap requests` run in such a folder, still gets an answer.
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--json")
    assert code == 0
    assert "matches none of the native targets" in err and "default targets" in err
    assert [t["platform"] for t in json.loads(out)["targets"]] == DEFAULT_PLATFORMS


def test_uv_required_environments_note_names_the_resolved_python(capsys, tmp_path, monkeypatch, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = [\"sys_platform == 'android'\"]\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "purewheel==3.1", "-p", "3.14,next", "--json")
    assert code == 0
    assert "native targets for Python 3.14, 3.15, so the default targets" in err


@pytest.mark.parametrize("setting", ["", "required-environments = []", "environments = [\"sys_platform == 'linux'\"]"])
def test_uv_required_environments_absent_or_empty(capsys, tmp_path, monkeypatch, fake_pypi, setting):
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--json")
    assert code == 0 and not err
    assert [t["platform"] for t in json.loads(out)["targets"]] == DEFAULT_PLATFORMS


@pytest.mark.parametrize("flags", [[], ["--markdown"]])
def test_uv_required_environments_note_is_on_stderr(capsys, tmp_path, monkeypatch, fake_pypi, flags):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = [\"sys_platform == 'linux'\"]", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", *flags)
    assert code == 0
    assert "required-environments" in err and "required-environments" not in out


def test_uv_required_environments_note_is_hidden_with_quiet(capsys, tmp_path, monkeypatch, fake_pypi):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv]\nrequired-environments = [\"sys_platform == 'linux'\"]", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "purewheel==3.1", "-p", "3.13", "--quiet")
    assert code == 0 and not err and not out


@pytest.mark.parametrize(
    "config,image,flags,expected",
    [
        ("", None, ["--distro", "ubuntu-24.04"], "wheel"),
        ("", None, ["--distro", "ubuntu-22.04"], "missing"),
        ('distro = "ubuntu-24.04"', None, [], "wheel"),
        ('distro = "ubuntu-24.04"\nglibc = "2.35"', None, [], "missing"),
        ('glibc = "2.35"', None, ["--distro", "ubuntu-24.04"], "wheel"),
        ('distro = "ubuntu-22.04"', None, ["--distro", "ubuntu-24.04"], "wheel"),
        ('distro = "ubuntu-24.04"', "3.13-bookworm", [], "missing"),
        ('distro = "ubuntu-22.04"', "3.13-trixie", [], "wheel"),
        ('glibc = "2.41"', "3.13-bookworm", ["--distro", "ubuntu-24.04"], "wheel"),
        ("", "3.13-trixie", ["--distro", "ubuntu-22.04"], "missing"),
        ("", "3.13-bookworm", ["--distro", "ubuntu-24.04", "--glibc", "2.35"], "missing"),
        ("", None, ["--glibc", "2.39", "--distro", "ubuntu-22.04"], "wheel"),
        ('distro = "ubuntu-24.04"', None, ["--glibc", "2.35"], "missing"),
        ('distro = "ubuntu-22.04"', "3.13", [], "wheel"),  # python:3.13 is trixie (2.41), which beats config distro
        ('distro = "ubuntu-22.04"', "3.13.1-slim", [], "missing"),  # a patch tag sets no glibc: config distro applies
    ],
)
def test_distro_glibc_precedence(capsys, fixtures, tmp_path, fake_pypi, config, image, flags, expected):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\n" + config + "\n", encoding="utf-8")
    args = [str(tmp_path), "-p", "3.13", "-t", "linux-aarch64", "--json", *flags]
    if image:
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(f"FROM python:{image}\n", encoding="utf-8")
        args += ["--dockerfile", str(dockerfile)]
    code, out, err = run(capsys, *args)
    assert not err and not fake_pypi
    data = json.loads(out)
    package = next(p for p in data["packages"] if p["name"] == "newglibc")
    assert package["results"] == {"linux-aarch64": expected}
    assert code == (0 if expected == "wheel" else 1)


@pytest.mark.parametrize(
    "config,image,flags,expected",
    [
        ('musl = "1.1"', None, ["--distro", "alpine"], "wheel"),
        ('distro = "alpine"', None, [], "wheel"),
        ('distro = "alpine"\nmusl = "1.1"', None, [], "source"),
        ("", "3.13-alpine3.12", ["--distro", "alpine"], "wheel"),
        ('distro = "alpine"', "3.13-alpine3.12", [], "source"),
        ("", None, ["--distro", "alpine", "--musl", "1.1"], "source"),
        ("", None, ["--musl", "1.1", "--distro", "alpine"], "source"),
        ('musl = "1.1"', None, ["--distro", "ubuntu-24.04"], "source"),
    ],
)
def test_distro_musl_precedence(capsys, fixtures, tmp_path, fake_pypi, config, image, flags, expected):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\n" + config + "\n", encoding="utf-8")
    args = [str(tmp_path), "-p", "3.13", "-t", "musl-x86_64", "--json", *flags]
    if image:
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(f"FROM python:{image}\n", encoding="utf-8")
        args += ["--dockerfile", str(dockerfile)]
    code, out, err = run(capsys, *args)
    assert code == 1 and not err and not fake_pypi  # newglibc is always missing on musl
    package = next(p for p in json.loads(out)["packages"] if p["name"] == "armlib")
    assert package["results"] == {"musl-x86_64": expected}


@pytest.mark.parametrize("distro", ["alpine", "ubuntu-22.04"])
@pytest.mark.parametrize("selection", [[], ["-t", "all"], ["-t", "win-amd64"], ["-t", "linux/arm64"]])
def test_distro_does_not_select_targets(capsys, fixtures, distro, selection):
    args = [str(fixtures / "uvproj"), "-p", "3.13", "--json", *selection]
    _, before, _ = run(capsys, *args)
    _, after, _ = run(capsys, *args, "--distro", distro)
    assert json.loads(before)["targets"] == json.loads(after)["targets"]


@pytest.mark.parametrize("flags", [[], ["--distro", "debian-12"]])
@pytest.mark.parametrize("config", ["distro = 22", 'distro = "typo"', 'distro = ""'])
def test_invalid_config_distro(capsys, tmp_path, fake_pypi, config, flags):
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\n" + config, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), *flags)
    assert code == 2 and not out and not fake_pypi
    assert "distro" in err and "ubuntu-22.04" in err


@pytest.mark.parametrize("extra", [[], ["--glibc", "2.35"], ["--list-targets", "--json"]])
def test_invalid_cli_distro(capsys, fake_pypi, extra):
    code, out, err = run(capsys, "--distro", "typo", *extra)
    assert code == 2 and not fake_pypi
    assert "unknown distro 'typo'" in err and "ubuntu-22.04" in err
    if "--json" in extra:  # with --json an input error is also printed as JSON on stdout
        assert json.loads(out)["error"]["type"] == "TargetError"
    else:
        assert not out


@pytest.mark.parametrize("as_json", [False, True])
def test_distro_list_targets_limits(capsys, as_json):
    args = ["--list-targets", "--distro", "amazonlinux-2"] + (["--json"] if as_json else [])
    code, out, err = run(capsys, *args)
    assert code == 0 and not err
    assert "glibc <= 2.26" in out
    if as_json:
        assert [row["name"] for row in json.loads(out)] == list(PLATFORMS)
    else:
        assert "ubuntu-22.04" in out and "glibc 2.35" in out and "rocky-9" in out


def test_gaps_exit_1(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64")
    assert code == 1
    assert "fastlib" in out and "● source" in out
    assert "purewheel" not in out  # fine everywhere, so hidden
    assert "have gaps" in out
    assert "skipped 2 non-index packages" in out
    assert "\033[" not in out  # no colour when not a TTY


def test_docker_platform_names_on_cli(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux/amd64,linux/arm64,linux/arm64/v8",
        "--json",
    )
    assert code == 1
    assert not err
    data = json.loads(out)
    assert [target["label"] for target in data["targets"]] == ["linux-x86_64", "linux-aarch64"]


def test_exit_zero_and_all(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--all", "--exit-zero")
    assert code == 0
    assert "purewheel" in out and "–" in out


def test_clean_exit_0(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "pylockproj"), "-p", "3.13", "-t", "linux-aarch64")
    assert code == 0
    assert "The 1 package has a wheel for 1 target." in out


def test_quiet_reports_only_the_gap_summary_and_names(capsys, fixtures):
    result = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--quiet", "--all")
    assert result == (
        1,
        "2 of 2 packages have gaps: 1 will build from source, 1 can't install at all.\ngaps: armlib, newglibc\n",
        "",
    )


def test_quiet_with_no_gaps_is_silent(capsys, fixtures):
    assert run(capsys, str(fixtures / "pylockproj"), "-p", "3.13", "-t", "linux-aarch64", "-q") == (0, "", "")


def test_quiet_preserves_json_and_exit_status(capsys, fixtures):
    args = (str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json")
    assert run(capsys, *args, "--quiet") == run(capsys, *args)


def test_uv_lock_dependency_edge_of_the_wrong_type_exits_2(capsys, tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "a"\nversion = "1.0"\ndependencies = [{ name = "b", marker = 5 }]\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and not out
    assert "package a has a dependency 'b' with a `marker` that isn't a string" in err


def test_quiet_preserves_list_targets_output(capsys):
    assert run(capsys, "--list-targets", "--quiet") == run(capsys, "--list-targets")


def test_quiet_still_reports_bad_input(capsys, fixtures):
    code, out, err = run(capsys, str(fixtures / "empty"), "--quiet")
    assert code == 2 and not out and "no lockfile found" in err


def test_requirements_line_pip_cant_split_exits_2(capsys, tmp_path, fake_pypi):
    req = tmp_path / "requirements.txt"
    req.write_text("fastlib==2.0.0\n-r 'unclosed\n", encoding="utf-8")
    code, out, err = run(capsys, str(req))
    assert code == 2 and not out and not fake_pypi
    assert "requirements.txt" in err and "-r 'unclosed" in err


def test_requirements_missing_include_exits_2_with_the_line(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("purewheel==3.1\n-r _base.txt\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert (code, out, err) == (2, "", "wheelgap: error: requirements.txt:2: -r _base.txt: file not found\n")
    assert fake_pypi == []
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 2
    assert json.loads(out)["error"]["message"] == "requirements.txt:2: -r _base.txt: file not found"


def test_requirement_line_no_binary_is_honoured_as_pip_does(capsys, fake_pypi, tmp_path):
    # pip 26.3 takes no wheel for either here: --no-binary after a requirement counts for the whole install.
    # (The fake purewheel has no sdist, so with its wheel ruled out nothing is left.)
    req = tmp_path / "requirements.txt"
    req.write_text("purewheel==3.1\nfastlib==2.0.0 --no-binary purewheel,fastlib\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    results = {p["name"]: p["results"]["linux-x86_64"] for p in json.loads(out)["packages"]}
    assert results == {"fastlib": "source", "purewheel": "missing"} and code == 1
    req.write_text("purewheel==3.1\nfastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    results = {p["name"]: p["results"]["linux-x86_64"] for p in json.loads(out)["packages"]}
    assert results == {"fastlib": "wheel", "purewheel": "wheel"} and code == 0


def test_requirements_name_required_twice_is_checked_once(capsys, fake_pypi, tmp_path):
    # pip installs the one release both requirements allow (pip 26.3: only numpy 1.26.4 for numpy==1.26.4 in a -r
    # file and numpy>=1.20), not the pin and the newest release side by side.
    (tmp_path / "base.txt").write_text("ranged==1.9\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("-r base.txt\nranged>=1.5\n", encoding="utf-8")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert [(p["name"], p["version"], p["results"]["linux-x86_64"]) for p in json.loads(out)["packages"]] == [
        ("ranged", "1.9", "source")
    ]
    assert code == 1


def _cells(out):
    """{(name, version): {target: result}} from --json, the rows no target needs left out."""
    return {
        (p["name"], p["version"]): {t: r for t, r in p["results"].items() if r != "n/a"}
        for p in json.loads(out)["packages"]
        if any(r != "n/a" for r in p["results"].values())
    }


@pytest.mark.parametrize(
    "lines, args, expected",
    [
        # Merged where both hold (Windows: ranged<2 and >=1.5 is 1.9, sdist only), plain elsewhere (2.0, a wheel).
        (
            ["ranged>=1.5", 'ranged<2 ; sys_platform == "win32"'],
            ["-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64"],
            {("ranged", "2.0"): {"linux-x86_64": "wheel"}, ("ranged", "1.9"): {"win-amd64": "source"}},
        ),
        # manim's scipy>=1.13.0 with scipy>=1.15.0 ; python_full_version >= '3.13' (#835 review): the newest
        # release meets both, so 3.13 checks the same release, in one row.
        (
            ["ranged>=1.5", "ranged>=1.9 ; python_full_version >= '3.13'"],
            ["-p", "3.12", "-p", "3.13", "-t", "linux-x86_64"],
            {("ranged", "2.0"): {"3.12 linux-x86_64": "wheel", "3.13 linux-x86_64": "wheel"}},
        ),
        # A patch release can't be decided on 3.13, but 2.0 meets the line either way...
        (
            ["ranged>=1.5", "ranged>=1.9 ; python_full_version >= '3.13.1'"],
            ["-p", "3.13", "-t", "linux-x86_64"],
            {("ranged", "2.0"): {"linux-x86_64": "wheel"}},
        ),
        # ...while here it may hold and leave out 2.0: unknown.
        (
            ["ranged>=1.5", "ranged<2 ; python_full_version >= '3.13.1'"],
            ["-p", "3.13", "-t", "linux-x86_64"],
            {("ranged", None): {"linux-x86_64": "unknown"}},
        ),
    ],
)
def test_requirements_name_required_under_markers_checked_per_target(
    capsys, fake_pypi, tmp_path, lines, args, expected
):
    req = tmp_path / "requirements.txt"
    req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), *args, "--json")
    assert _cells(out) == expected


@pytest.mark.parametrize(
    "lines, args, expected",
    [
        # A line naming a pre-release lets pip take pre-releases for the lines together: pip 26.3 installs
        # tensorflow 2.22.0rc1 for tensorflow>=2.15 with tensorflow>=2.16.0rc0 on Linux (and grpcio 1.86.0rc1 for
        # grpcio>=1.80 with grpcio>=1.85.0rc1 on macOS), not the newest final release.
        (
            ["prelib>=0.5", 'prelib>=1.0rc1 ; sys_platform == "linux"'],
            ["-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64"],
            {("prelib", "2.0rc1"): {"linux-x86_64": "source"}, ("prelib", "1.0"): {"win-amd64": "wheel"}},
        ),
        # Where that line may hold (a patch release on 3.13), pip may take 2.0rc1 or 1.0: unknown.
        (
            ["prelib>=0.5", 'prelib>=1.0rc1 ; python_full_version >= "3.13.1"'],
            ["-p", "3.13", "-t", "linux-x86_64"],
            {("prelib", None): {"linux-x86_64": "unknown"}},
        ),
    ],
)
def test_requirements_merged_lines_naming_a_pre_release_are_looked_up(
    capsys, fake_pypi, tmp_path, lines, args, expected
):
    req = tmp_path / "requirements.txt"
    req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), *args, "--json")
    assert _cells(out) == expected


def test_requirements_header_counts_a_name_required_under_markers_once(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("ranged==1.5 ; sys_platform == 'win32'\nranged==2.0 ; sys_platform != 'win32'\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--all")
    assert "· requirements.txt · 1 package · Python 3.13" in out
    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json")
    assert json.loads(out)["summary"]["packages"] == 1


def test_requirements_identical_line_under_a_marker_stays_checked_on_undecided_targets(capsys, fake_pypi, tmp_path):
    # vllm cpu.txt's `py-cpuinfo; platform_machine == "aarch64"` with common.txt's `py-cpuinfo` (#835 review): one
    # row, confident on linux-armv7l too, where platform_machine can't be decided.
    (tmp_path / "common.txt").write_text("unpinned\n", encoding="utf-8")
    req = tmp_path / "cpu.txt"
    req.write_text('-r common.txt\nunpinned; platform_machine == "aarch64"\n', encoding="utf-8")
    code, out, _ = run(capsys, str(req), "-p", "3.12", "-t", "linux-x86_64,linux-aarch64,linux-armv7l", "--json")
    assert _cells(out) == {
        ("unpinned", "4.2"): {"linux-x86_64": "wheel", "linux-aarch64": "wheel", "linux-armv7l": "wheel"}
    }
    assert code == 0


def test_pyproject_ranges_split_by_python_with_an_extra_are_checked(capsys, fake_pypi, tmp_path):
    # litellm's pydantic: three ranges split by python_version, and the same name unmarked in an extra.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0'\ndependencies = [\n"
        "  \"ranged>=1.5,<3; python_version < '3.14'\",\n"
        "  \"ranged>=1.9,<3; python_version == '3.14'\",\n"
        "  \"ranged>=2.0,<3; python_version >= '3.15'\",\n]\n"
        "[project.optional-dependencies]\nmcp = ['ranged>=1.5,<3']\n",
        encoding="utf-8",
    )
    code, out, _ = run(
        capsys,
        str(tmp_path / "pyproject.toml"),
        "--all-extras",
        "-p",
        "3.13",
        "-p",
        "3.14",
        "-t",
        "linux-x86_64",
        "--json",
    )
    cells = _cells(out)
    assert {cell for results in cells.values() for cell in results.values()} == {"wheel"}
    assert {version for _, version in cells} == {"2.0"} and code == 0


def test_requirements_ranges_that_allow_nothing_together_are_a_refusal(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("ranged>=2\nranged<2\n", encoding="utf-8")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    (pkg,) = json.loads(out)["packages"]
    assert pkg["results"]["linux-x86_64"] == "unknown" and code == 1
    assert pkg["error"].startswith("no release matches <2,>=2")
    assert pkg["error"].endswith("(the requirements for it allow no release together, so pip and uv refuse them)")


@pytest.mark.parametrize(
    "line, expected",
    [
        # The sha256 lists the Linux wheel: pip keeps only it where it fits (filter_unallowed_hashes). On Windows
        # no listed file fits, so pip checks its pick against the sha512 too: unknown.
        (
            f"hashpkg==1.0 --hash=sha256:{'1' * 64} --hash=sha512:{'ab' * 64}",
            {"linux-x86_64": "wheel", "win-amd64": "unknown"},
        ),
        # The sdist's sha256 fits everywhere: pip builds it, as with that hash alone.
        (
            f"hashpkg==1.0 --hash=sha256:{'3' * 64} --hash=sha512:{'ab' * 64}",
            {"linux-x86_64": "source", "win-amd64": "source"},
        ),
    ],
)
def test_requirements_sha256_beside_sha512_decides_where_a_listed_file_fits(
    capsys, fake_pypi, tmp_path, line, expected
):
    req = tmp_path / "requirements.txt"
    req.write_text(line + "\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json")
    (pkg,) = json.loads(out)["packages"]
    assert pkg["results"] == expected


@pytest.mark.parametrize("option", ["--cons", "--const", "--constrain="])
def test_requirements_abbreviated_constraint_is_read(capsys, fake_pypi, tmp_path, option):
    # pip 26.3 reads `--cons c.txt` as `--constraint c.txt` (optparse takes an unambiguous prefix).
    (tmp_path / "c.txt").write_text("ranged==1.9\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    sep = "" if option.endswith("=") else " "
    req.write_text(f"{option}{sep}c.txt\nranged\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert _cells(out) == {("ranged", "1.9"): {"linux-x86_64": "source"}}


@pytest.mark.parametrize(
    "line", ["--cons https://releases.openstack.org/constraints/upper/2023.1", "--const ${CDIR}/c.txt"]
)
def test_requirements_abbreviated_unread_constraint_is_refused(capsys, fake_pypi, tmp_path, line):
    req = tmp_path / "requirements.txt"
    req.write_text(f"{line}\nnumpy\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and f"requirements.txt:1: {line}: pip reads a constraints file" in err
    assert fake_pypi == []


def test_requirements_abbreviated_requirement_is_read(capsys, fake_pypi, tmp_path):
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("--requirem base.txt\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert _cells(out) == {("purewheel", "3.1"): {"linux-x86_64": "wheel"}}


def test_requirements_sha512_hashes_are_unknown_not_unhashed(capsys, fake_pypi, tmp_path):
    # pip 26.3 picks the wheel, checks it against the sha512 and refuses it; before, wheelgap dropped the hash and said
    # `wheel`.
    req = tmp_path / "requirements.txt"
    req.write_text(f"purewheel==3.1 --hash=sha512:{'ab' * 64}\n", encoding="utf-8")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "macos-arm64", "--json")
    (pkg,) = json.loads(out)["packages"]
    assert pkg["results"]["macos-arm64"] == "unknown" and code == 1
    assert "lists sha512 hashes for purewheel, which wheelgap can't match" in pkg["error"]
    assert fake_pypi == []


def test_requirements_hashes_filter_files_by_sha256(capsys, fake_pypi, tmp_path):
    # 1. only the sdist's hash -> reports source
    req1 = tmp_path / "req1.txt"
    req1.write_text("hashpkg==1.0 --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333\n")
    code, out, _ = run(capsys, str(req1), "-p", "3.13", "-t", "linux-x86_64", "--json")
    pkg = json.loads(out)["packages"][0]
    assert code == 1 and pkg["results"]["linux-x86_64"] == "source"

    # 2. the wheel's and the sdist's -> reports wheel (as today)
    req2 = tmp_path / "req2.txt"
    req2.write_text(
        "hashpkg==1.0 \\\n"
        "    --hash=sha256:1111111111111111111111111111111111111111111111111111111111111111 \\\n"
        "    --hash=sha256:3333333333333333333333333333333333333333333333333333333333333333\n"
    )
    code, out, _ = run(capsys, str(req2), "-p", "3.13", "-t", "linux-x86_64", "--json")
    pkg = json.loads(out)["packages"][0]
    assert code == 0 and pkg["results"]["linux-x86_64"] == "wheel"

    # 3. only a Windows wheel's hash, checked on Linux -> reports missing
    req3 = tmp_path / "req3.txt"
    req3.write_text("hashpkg==1.0 --hash=sha256:2222222222222222222222222222222222222222222222222222222222222222\n")
    code, out, _ = run(capsys, str(req3), "-p", "3.13", "-t", "linux-x86_64", "--json")
    pkg = json.loads(out)["packages"][0]
    assert code == 1 and pkg["results"]["linux-x86_64"] == "missing"

    # 4. no listed hash matches -> reports unknown with the message
    req4 = tmp_path / "req4.txt"
    req4.write_text("hashpkg==1.0 --hash=sha256:9999999999999999999999999999999999999999999999999999999999999999\n")
    code, out, _ = run(capsys, str(req4), "-p", "3.13", "-t", "linux-x86_64", "--json")
    pkg = json.loads(out)["packages"][0]
    assert code == 1 and pkg["results"]["linux-x86_64"] == "unknown"
    assert "none of the hashes in req4.txt matches a file of hashpkg 1.0" in pkg["error"]

    # 5. no hashes at all -> reports wheel (unchanged)
    req5 = tmp_path / "req5.txt"
    req5.write_text("hashpkg==1.0\n")
    code, out, _ = run(capsys, str(req5), "-p", "3.13", "-t", "linux-x86_64", "--json")
    pkg = json.loads(out)["packages"][0]
    assert code == 0 and pkg["results"]["linux-x86_64"] == "wheel"


def test_requirement_line_option_pip_ignores_is_a_note(capsys, fake_pypi, tmp_path):
    # xai-org/grok-1's requirements.txt has `jax[cuda12-pip]==0.4.25 -f https://...`: pip 26.3 ignores the -f there
    # and installs the requirement from PyPI, so wheelgap checks it on PyPI and says it left the option out.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "purewheel==3.1\nfastlib[extra]==2.0.0 -f https://user:secret@example.invalid/wheels.html\n"
        "nowheels==0.9 --find-links ./w\n",
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    results = {p["name"]: p["results"]["linux-x86_64"] for p in json.loads(out)["packages"]}
    assert results == {"purewheel": "wheel", "fastlib": "wheel", "nowheels": "source"} and code == 1
    assert err.count("wheelgap: note: requirements.txt:2: pip ignores -f after a requirement\n") == 1
    assert "--find-links" not in err and "secret" not in err and "example.invalid" not in err
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert code == 1 and "pip ignores" not in err


def _prelib(capsys, tmp_path, text, *argv):
    req = tmp_path / "requirements.txt"
    req.write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(req), "-t", "linux-x86_64", "--json", *argv)
    return code, json.loads(out)["packages"] if code in (0, 1) else None, err


@pytest.mark.parametrize("argv", [["-p", "3.13"], ["-p", "3.12,3.13"]])
@pytest.mark.parametrize(
    ("text", "version", "status"),
    [
        # pip 26.3: `--pre` then `demo>=0.5` installs demo-2.0rc1; without it, demo-1.0 (#634).
        ("prelib>=0.5\n", "1.0", "wheel"),
        ("--pre\nprelib>=0.5\n", "2.0rc1", "source"),
        ("prelib>=0.5\n--pre\n", "2.0rc1", "source"),
        ("prelib\n", "1.0", "wheel"),
        ("--pre\nprelib\n", "2.0rc1", "source"),  # a bare name too
        ("--all-releases prelib\nprelib\n", "2.0rc1", "source"),
        ("--pre\n--only-final prelib\nprelib>=0.5\n", "1.0", "wheel"),
        ("--pre\nprelib==1.0\n", "1.0", "wheel"),  # a pin isn't affected
        ("--pre -r base.txt\n", "1.0", "wheel"),  # pip ignores --pre on a -r line
        ("--pre -e ./local\nprelib>=0.5\n", "1.0", "wheel"),  # and on a -e line, a requirement line to pip
        ("-e ./local --all-releases prelib\nprelib>=0.5\n", "2.0rc1", "source"),  # its parser applies this one
    ],
)
def test_requirements_pre_picks_the_release_pip_installs(capsys, fake_pypi, tmp_path, text, version, status, argv):
    (tmp_path / "base.txt").write_text("prelib>=0.5\n", encoding="utf-8")
    code, packages, err = _prelib(capsys, tmp_path, text, *argv)
    (pkg,) = packages
    assert code == (1 if status == "source" else 0)
    labels = [label for label in pkg["results"]]
    assert {pkg["results"][label] for label in labels} == {status}
    versions = {pkg["version"]} if "versions" not in pkg else set(pkg["versions"].values())
    assert versions == {version}
    if text != "--pre\nprelib==1.0\n":
        assert "unpinned package (prelib), so the newest matching release was checked" in err


def test_requirements_pre_on_a_simple_index(capsys, fake_simple, tmp_path):
    code, packages, _ = _prelib(
        capsys, tmp_path, "--pre\nprelib>=0.5\n", "-p", "3.13", "--index-url", "https://simple.example/simple"
    )
    assert code == 1 and packages[0]["version"] == "2.0rc1" and packages[0]["results"]["linux-x86_64"] == "source"
    code, packages, _ = _prelib(
        capsys, tmp_path, "prelib>=0.5\n", "-p", "3.13", "--index-url", "https://simple.example/simple"
    )
    assert code == 0 and packages[0]["version"] == "1.0"


def test_requirements_no_binary_on_an_editable_line(capsys, fake_pypi, tmp_path):
    # pip 26.3's parser callback applies `-e ./local --no-binary demo` to the whole install, though the line is a
    # requirement to pip: with only wheels on the index it finds no demo.
    code, packages, _ = _prelib(capsys, tmp_path, "-e ./local --no-binary prelib\nprelib==1.0\n", "-p", "3.13")
    (pkg,) = [p for p in packages if p["name"] == "prelib"]
    assert code == 1 and pkg["results"]["linux-x86_64"] == "source"


def test_requirements_only_final_refuses_a_pinned_pre_release(capsys, fake_pypi, tmp_path):
    code, packages, _ = _prelib(capsys, tmp_path, "--only-final :all:\nprelib==2.0rc1\n", "-p", "3.13")
    assert code == 1 and packages[0]["results"]["linux-x86_64"] == "unknown"
    assert "pip installs only final releases of prelib here (--only-final)" in packages[0]["error"]


@pytest.mark.parametrize(
    ("text", "noted"),
    [
        ("--prefer-binary\nprelib>=0.5\n", True),
        ("--prefer-binary\nprelib\n", True),
        ("--prefer-binary\nprelib==1.0\n", False),  # pinned: pip installs that release, wheel or not
        ("prelib>=0.5\n", False),
        ("prelib>=0.5 --prefer-binary\n", False),  # pip ignores it after a requirement (another note says so)
    ],
)
def test_requirements_prefer_binary_note_only_for_unpinned(capsys, fake_pypi, tmp_path, text, noted):
    code, _, err = _prelib(capsys, tmp_path, text, "-p", "3.13")
    note = (
        "wheelgap: note: requirements.txt:1 sets --prefer-binary: for an unpinned requirement pip may take an older "
        "release that has a wheel, and wheelgap checked the newest\n"
    )
    assert code == 0 and (note in err) is noted
    _, _, err = _prelib(capsys, tmp_path, text, "-p", "3.13", "-q")
    assert "--prefer-binary:" not in err


@pytest.mark.parametrize(
    ("line", "named"),
    [("--pre", "base.txt:1 sets --pre"), ("--prefer-binary", "base.txt:1 sets --prefer-binary")],
)
def test_requirements_deps_refuses_pre_and_prefer_binary(capsys, fake_pypi, tmp_path, line, named):
    (tmp_path / "base.txt").write_text(f"{line}\nprelib>=0.5\n", encoding="utf-8")
    code, _, err = _prelib(capsys, tmp_path, "-r base.txt\n", "-p", "3.13", "--deps")
    assert code == 2
    assert (
        f"requirements.txt: --deps can't follow this the way pip does ({named}, which changes the releases pip "
        "picks for dependencies), so check a lockfile instead"
    ) in err


_INDEX_NOTE = (
    "wheelgap: note: requirements.txt sets {}, which wheelgap doesn't read: its packages were checked on PyPI only, "
    "so a package pip finds elsewhere may install differently\n"
)


@pytest.mark.parametrize(
    ("lines", "named"),
    [
        # PyTorch's install instructions: pip 26.3 installs torch-2.9.0+cpu for win_arm64 from the other index.
        (["--extra-index-url https://user:secret@example.invalid/whl/cpu"], "--extra-index-url"),
        (["-i https://user:secret@example.invalid/simple"], "--index-url"),
        (["-ihttps://user:secret@example.invalid/simple"], "--index-url"),
        (["--index-url=https://user:secret@example.invalid/simple"], "--index-url"),
        (["--extra https://user:secret@example.invalid/simple"], "--extra-index-url"),  # optparse's abbreviation
        (["--find-links ./wheels"], "--find-links"),
        (["-f https://user:secret@example.invalid/wheels.html"], "--find-links"),
        (["--no-index", "--find-links=./wheels"], "--no-index and --find-links"),
        (
            ["-i https://user:secret@example.invalid/a", "--extra-index-url https://example.invalid/b", "-f ./w"],
            "--index-url, --extra-index-url and --find-links",
        ),
        (
            ["--extra-index-url https://example.invalid/a", "--extra-index-url https://example.invalid/b"],
            "--extra-index-url",
        ),
        (["--pre --extra-index-url https://user:secret@example.invalid/simple"], "--extra-index-url"),
        (["--pypi-url https://user:secret@example.invalid/simple"], "--pypi-url"),  # pip's other name for -i
    ],
)
def test_requirements_index_option_is_a_note(capsys, fake_pypi, tmp_path, lines, named):
    # pip reads these lines and installs from the index or folder they name; wheelgap checks PyPI only (#632). The
    # report is unchanged, and stderr says so, by the option's name only: its value may hold credentials.
    req = tmp_path / "requirements.txt"
    req.write_text("\n".join([*lines, "purewheel==3.1", ""]), encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0 and json.loads(out)["packages"][0]["results"]["linux-x86_64"] == "wheel"
    assert err.count(_INDEX_NOTE.format(named)) == 1
    assert "secret" not in err and "example.invalid" not in err and "./w" not in err
    code, _, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert code == 0 and "doesn't read" not in err  # -q drops it, as it drops the unpinned-package note


def test_requirements_index_option_in_an_included_file_is_a_note(capsys, fake_pypi, tmp_path):
    (tmp_path / "base.txt").write_text("--find-links https://user:secret@example.invalid/w.html\npurewheel==3.1\n")
    req = tmp_path / "requirements.txt"
    req.write_text("-r base.txt\nfastlib==2.0.0\n", encoding="utf-8")
    code, _, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0 and err.count(_INDEX_NOTE.format("--find-links")) == 1  # one note per input, named by its label
    assert "secret" not in err


def test_requirements_without_index_option_has_no_index_note(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    # --trusted-host names no index, nor does its value; an -f after a requirement is ignored by pip (its own note
    # says so), and so is an index option on a line with -r or -c: pip 26.3 reads the file and drops the rest.
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("", encoding="utf-8")
    req.write_text(
        "--trusted-host -ifoo\n--find-links ./w -r base.txt\n-c c.txt --extra-index-url https://example.invalid/s\n"
        "fastlib==2.0.0 -f ./w\n",
        encoding="utf-8",
    )
    code, _, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0 and "doesn't read" not in err
    assert "requirements.txt:4: pip ignores -f after a requirement" in err


def test_requirements_index_note_not_given_when_the_run_names_an_index(
    capsys, fake_pypi, fake_simple, tmp_path, monkeypatch
):
    # With wheelgap's own --index-url (or a WHEELGAP_INDEX_URL mirror) the run doesn't check PyPI, so the note,
    # which says it does, would be wrong.
    req = tmp_path / "requirements.txt"
    req.write_text("--extra-index-url https://example.invalid/simple\npurewheel==3.1\n", encoding="utf-8")
    code, _, err = run(
        capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--index-url", "https://simple.example/simple"
    )
    assert code in (0, 1) and "doesn't read" not in err
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    code, _, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0 and "doesn't read" not in err


def test_requirements_include_cycle_exits_2(capsys, tmp_path, monkeypatch):
    # pip refuses an include cycle, so wheelgap does too: exit code 2, and the same error with
    # --github-annotations, since the file is parsed before any line is looked up (#603).
    c1 = tmp_path / "c1.txt"
    c1.write_text("-r c2.txt\nrequests==2.32.5\n", encoding="utf-8")
    (tmp_path / "c2.txt").write_text("-r c1.txt\nidna==3.11\n", encoding="utf-8")
    message = (
        "c2.txt: -r c1.txt includes a file that is already being read (c1.txt -> c2.txt -> c1.txt); pip refuses this"
    )
    code, out, err = run(capsys, str(c1))
    assert code == 2 and not out and message in err
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, err = run(capsys, str(c1), "--github-annotations")
    assert code == 2 and not out and message in err


def test_quiet_preserves_fail_on_missing(capsys, fake_pypi):
    code, out, err = run(capsys, "nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing", "--quiet")
    assert code == 0 and not err
    summary, names = out.splitlines()
    assert "will build from source" in summary
    assert names == "gaps: nowheels"


def test_quiet_from_pyproject(capsys, fixtures, tmp_path):
    (tmp_path / "pylock.toml").write_bytes((fixtures / "pylockproj" / "pylock.toml").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    assert run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-aarch64") == (0, "", "")


def test_pyproject_quiet_must_be_boolean(capsys, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nquiet = "true"\n', encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path))
    assert code == 2 and not out and "quiet must be a boolean" in err


@pytest.mark.parametrize("from_config", [False, True])
def test_quiet_conflicts_with_explain(capsys, tmp_path, from_config):
    args = ["--explain"]
    if from_config:
        (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
        args.append(str(tmp_path))
    else:
        args.append("--quiet")
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
    error = capsys.readouterr().err.splitlines()[-1]
    assert "--quiet" in error and "--explain" in error


def test_json(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13,3.14t", "-t", "win-amd64", "--json")
    assert code == 1
    data = json.loads(out)
    assert [t["label"] for t in data["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]
    fastlib = next(p for p in data["packages"] if p["name"] == "fastlib")
    assert fastlib["results"] == {"3.13 win-amd64": "wheel", "3.14t win-amd64": "source"}
    assert {"name": "gitdep", "reason": "git"} in data["skipped"]
    assert data["summary"]["with_gaps"] >= 1


def test_color_always(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--color", "always")
    assert "\033[33m" in out


def test_adhoc_packages(capsys, fake_pypi):
    code, out, _ = run(capsys, "nowheels", "purewheel", "-p", "3.13", "-t", "linux")
    assert code == 1
    assert "nowheels" in out and "0.9" in out


@pytest.mark.parametrize("input_name", ["pipenvproj", "pipenvproj/Pipfile.lock"])
def test_pipfile_lock_json(capsys, fixtures, fake_pypi, input_name):
    code, out, err = run(
        capsys, str(fixtures / input_name), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64,win-amd64", "--json"
    )
    assert code == 1
    assert not err
    data = json.loads(out)
    assert data["sources"] == ["Pipfile.lock"]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["fastlib"]["version"] == "2.0.0"
    assert packages["fastlib"]["results"] == {"linux-x86_64": "wheel", "musl-aarch64": "source", "win-amd64": "source"}
    assert packages["winonly"]["results"] == {"linux-x86_64": "n/a", "musl-aarch64": "n/a", "win-amd64": "wheel"}
    assert packages["purewheel"]["results"] == {"linux-x86_64": "wheel", "musl-aarch64": "wheel", "win-amd64": "wheel"}
    assert data["skipped"] == [
        {"name": "gitdep", "reason": "git"},
        {"name": "localpkg", "reason": "path"},
        {"name": "archivepkg", "reason": "file"},
        {"name": "editablepkg", "reason": "editable"},
    ]
    assert sorted(fake_pypi) == [
        "https://pypi.org/pypi/fastlib/2.0.0/json",
        "https://pypi.org/pypi/purewheel/3.1/json",
        "https://pypi.org/pypi/winonly/311/json",
    ]


@pytest.mark.parametrize("input_name", ["pdmproj", "pdmproj/pdm.lock"])
def test_pdm_lock_json(capsys, fixtures, fake_pypi, input_name):
    code, out, err = run(capsys, str(fixtures / input_name), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64", "--json")
    assert code == 1
    assert not err
    data = json.loads(out)
    assert data["sources"] == ["pdm.lock"]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["fastlib"]["results"] == {"linux-x86_64": "source", "musl-aarch64": "wheel"}
    assert "backport" not in packages  # marker excludes it on 3.13
    assert data["skipped"] == [{"name": "localpkg", "reason": "path"}]
    assert not fake_pypi


def test_pdm_lock_entry_without_files_is_looked_up(capsys, fake_pypi, tmp_path):
    (tmp_path / "pdm.lock").write_text(
        '[[package]]\nname = "purewheel"\nversion = "3.1"\n\n'
        '[[package]]\nname = "nowheels"\nversion = "0.9"\nfiles = []\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["purewheel"]["results"] == {"linux-x86_64": "wheel"}  # filled in from PyPI
    assert packages["nowheels"]["results"] == {"linux-x86_64": "missing"}  # an empty list is taken as recorded
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/3.1/json"]
    assert code == 1


def test_pdm_lock_marker_on_older_python(capsys, fixtures, fake_pypi):
    code, out, err = run(capsys, str(fixtures / "pdmproj"), "-p", "3.12", "-t", "linux-x86_64", "--json")
    assert code == 1
    assert not err
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["backport"]["results"] == {"linux-x86_64": "source"}
    assert not fake_pypi


def test_pipfile_lock_in_current_directory(capsys, fixtures, fake_pypi, monkeypatch):
    monkeypatch.chdir(fixtures / "pipenvproj")
    code, out, err = run(capsys, "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert not err
    assert "skipped 4 non-index packages" in out
    assert "editablepkg (editable)" in out
    assert "All 2 packages have a wheel for 1 target." in out


def test_pipfile_lock_private_index_is_not_looked_up(capsys, tmp_path, fake_pypi):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "_meta": {
            "sources": [
                {"name": "pypi", "url": "https://pypi.org/simple"},
                {"name": "internal", "url": "https://pkgs.example.com/simple"},
            ]
        },
        "default": {
            "acme-utils": {"version": "==2.1.0", "index": "internal"},
            "fastlib": {"version": "==2.0.0", "index": "pypi"},
        },
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["skipped"] == [{"name": "acme-utils", "reason": "private index"}]
    assert [p["name"] for p in data["packages"]] == ["fastlib"]
    assert fake_pypi and not any("acme" in url for url in fake_pypi)


def _mirror_pipfile_lock(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "_meta": {"sources": [{"name": "mirror", "url": "https://mirror.example.com/simple", "verify_ssl": True}]},
        "default": {
            "fastlib": {"version": "==2.0.0", "index": "mirror"},
            "purewheel": {"version": "==3.1", "index": "mirror"},
        },
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


def test_pipfile_lock_mirror_is_looked_up_on_wheelgap_index_url(capsys, tmp_path, fake_pypi, monkeypatch):
    # #146: WHEELGAP_INDEX_URL on the same index as the lock's source: the names go nowhere new.
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    code, out, err = run(capsys, str(_mirror_pipfile_lock(tmp_path)), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["skipped"] == []
    assert [p["name"] for p in data["packages"]] == ["fastlib", "purewheel"]
    assert sorted(fake_pypi) == [
        "https://mirror.example.com/pypi/fastlib/2.0.0/json",
        "https://mirror.example.com/pypi/purewheel/3.1/json",
    ]


def test_pipfile_lock_mirror_is_skipped_without_wheelgap_index_url(capsys, tmp_path, fake_pypi):
    code, out, err = run(capsys, str(_mirror_pipfile_lock(tmp_path)), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["skipped"] == [
        {"name": "fastlib", "reason": "private index"},
        {"name": "purewheel", "reason": "private index"},
    ]
    assert not data["packages"]
    assert fake_pypi == []


def test_offline_refuses_a_pipfile_lock_mirror_on_wheelgap_index_url(capsys, tmp_path, fake_pypi, monkeypatch):
    # The mirror would be asked, so --offline counts those packages; without the variable they're skipped instead.
    path = _mirror_pipfile_lock(tmp_path)
    code, out, err = run(capsys, str(path), "--offline", "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    assert len(json.loads(out)["skipped"]) == 2
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    code, _, err = run(capsys, str(path), "--offline", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "Pipfile.lock needs the index for 2 packages (fastlib, purewheel)" in err
    # --index-url replaces the JSON API, so WHEELGAP_INDEX_URL's host doesn't count and they're skipped again.
    index_url = "https://mirror.example.com/simple"
    code, out, err = run(
        capsys, str(path), "--offline", "--index-url", index_url, "-p", "3.13", "-t", "linux-x86_64", "--json"
    )
    assert code == 0, err
    assert len(json.loads(out)["skipped"]) == 2
    assert fake_pypi == []


def test_poetry_lock_private_index_with_no_files_is_not_looked_up(capsys, tmp_path, fake_pypi):
    path = tmp_path / "poetry.lock"
    path.write_text(
        '[[package]]\nname = "acme-utils"\nversion = "2.1.0"\n\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pkgs.example.com/simple"\nreference = "private"\n\n'
        '[metadata]\nlock-version = "1.1"\npython-versions = ">=3.8"\n\n'
        '[metadata.hashes]\nacme-utils = ["sha256:00"]\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["skipped"] == [{"name": "acme-utils", "reason": "private index"}]
    assert not data["packages"]
    assert not any("acme" in url for url in fake_pypi)


def test_poetry_lock_empty_files_are_looked_up_on_the_index(capsys, tmp_path, fake_pypi):
    # Poetry writes `files = []` when the source gave it no hashes, and then installs the best file the index has.
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "fastlib"\nversion = "2.0.0"\ndescription = ""\noptional = false\n'
        'python-versions = ">=3.9"\ngroups = ["main"]\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err  # used to be "missing", as if the release had no files at all
    assert json.loads(out)["packages"][0]["results"] == {"linux-x86_64": "wheel"}
    assert any("fastlib" in url for url in fake_pypi)


def test_poetry_lock_empty_files_from_a_private_index_are_skipped(capsys, tmp_path, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "fastlib"\nversion = "2.0.0"\ngroups = ["main"]\nfiles = []\n\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pkgs.example.com/simple"\nreference = "private"\n\n'
        '[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    assert json.loads(out)["skipped"] == [{"name": "fastlib", "reason": "private index"}]
    assert fake_pypi == []


def test_offline_allows_a_private_index_package_with_no_files(capsys, tmp_path, fake_pypi):
    # fill_all skips it without contacting any index, so --offline has nothing to refuse.
    path = tmp_path / "poetry.lock"
    path.write_text(
        '[[package]]\nname = "acme-utils"\nversion = "2.1.0"\n\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pkgs.example.com/simple"\nreference = "private"\n\n'
        '[metadata]\nlock-version = "1.1"\npython-versions = ">=3.8"\n\n'
        '[metadata.hashes]\nacme-utils = ["sha256:00"]\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(path), "--offline", "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0, err
    assert json.loads(out)["skipped"] == [{"name": "acme-utils", "reason": "private index"}]
    assert fake_pypi == []


def test_malformed_pipfile_lock_exits_2(capsys, tmp_path, fake_pypi):
    path = tmp_path / "Pipfile.lock"
    path.write_text("{invalid", encoding="utf-8")
    code, _, err = run(capsys, str(path))
    assert code == 2
    assert "JSONDecodeError" in err
    assert not fake_pypi


def test_errors_exit_2(capsys, fixtures):
    code, _, err = run(capsys, str(fixtures / "empty"))
    assert code == 2 and "no lockfile found" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-t", "solaris")
    assert code == 2 and "unknown target" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-p", "four")
    assert code == 2


def test_list_targets(capsys):
    code, out, err = run(capsys, "--list-targets")
    assert code == 0 and not err
    lines = out.splitlines()
    assert lines[0].split() == ["name", "aliases", "accepts"]
    alpine = next(line for line in lines if line.startswith("musl-x86_64 "))
    assert "alpine" in alpine and "musl <= 1.2" in alpine
    assert "linux/arm64" in out


def test_list_targets_json_includes_every_platform_and_alias(capsys):
    code, out, err = run(capsys, "--list-targets", "--json")
    assert code == 0 and not err
    rows = json.loads(out)
    assert [row["name"] for row in rows] == list(PLATFORMS)
    assert {alias: row["name"] for row in rows for alias in row["aliases"]} == ALIASES
    by_name = {row["name"]: row for row in rows}
    assert "glibc <= 2.36" in by_name["linux-x86_64"]["accepts"]
    assert "macOS <= 14.0" in by_name["macos-arm64"]["accepts"]
    assert by_name["pyodide"]["accepts"] == "pyodide wasm32, Python 3.11, 3.12, 3.13, 3.14 (not in -t all)"
    assert by_name["linux-s390x"]["accepts"] == "manylinux s390x, glibc 2.17 to 2.36 (not in -t all)"
    assert by_name["linux-aarch64"]["accepts"] == "manylinux aarch64, glibc 2.17 to 2.36"


def test_list_targets_respects_compatibility_overrides(capsys):
    flags = ("--list-targets", "--glibc", "2.28", "--musl", "1.1", "--macos", "13.0")
    code, out, err = run(capsys, *flags)
    assert code == 0 and not err
    assert "glibc <= 2.28" in out

    code, out, err = run(capsys, *flags, "--json")
    assert code == 0 and not err
    by_name = {row["name"]: row["accepts"] for row in json.loads(out)}
    assert "glibc <= 2.28" in by_name["linux-x86_64"] and "glibc 2.17 to 2.28" in by_name["linux-aarch64"]
    assert "musl <= 1.1" in by_name["musl-aarch64"]
    assert "macOS <= 13.0" in by_name["macos-x86_64"]


def test_mobile_flags_and_config(capsys, fake_pypi, tmp_path, monkeypatch):
    code, out, _ = run(capsys, "--list-targets", "--ios", "16.4", "--android-api", "30", "--json")
    by_name = {row["name"]: row["accepts"] for row in json.loads(out)}
    assert by_name["ios-arm64"] == "ios arm64_iphoneos, iOS <= 16.4 (wheels only; -t mobile, not in -t all)"
    assert by_name["android-x86_64"] == "android x86_64, API level <= 30 (wheels only; -t mobile, not in -t all)"

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nios = "15.0"\nandroid-api = 28\n')  # an int is fine
    by_name = {row["name"]: row["accepts"] for row in json.loads(run(capsys, "--list-targets", "--json")[1])}
    assert "iOS <= 15.0" in by_name["ios-arm64"] and "API level <= 28" in by_name["android-arm64"]
    code, out, _ = run(capsys, "nowheels==0.9", "purewheel==3.1", "-p", "3.13", "-t", "mobile", "--json")
    data = json.loads(out)
    assert code == 1
    statuses = {p["name"]: set(p["results"].values()) for p in data["packages"]}
    assert statuses == {"nowheels": {"missing"}, "purewheel": {"wheel"}}
    code, _, err = run(capsys, "--list-targets", "--ios", "11.0")
    assert code == 2 and "iOS 11.0 is too old" in err
    code, _, err = run(capsys, "six", "-p", "3.12", "-t", "android")
    assert code == 2 and "need Python 3.13" in err
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nandroid-api = true\n")
    code, _, err = run(capsys, "--list-targets")
    assert code == 2 and 'android-api must be an integer or a string like "24"' in err


def test_list_targets_rejects_invalid_compatibility_version(capsys):
    code, out, err = run(capsys, "--list-targets", "--glibc", "bad")
    assert code == 2 and not out
    assert "can't parse glibc version" in err

    code, out, err = run(capsys, "--list-targets", "--glibc", "2")
    assert code == 2 and not out
    assert "glibc version '2' needs a minor version too" in err


def test_defaults_from_pyproject(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "configproj"), "--json")
    assert code == 1
    assert [t["label"] for t in json.loads(out)["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]


def test_flags_beat_pyproject(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "configproj"), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert [t["label"] for t in json.loads(out)["targets"]] == ["linux-x86_64"]


def test_bad_pyproject_exit_2(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nglibc = 2.31\n")
    code, _, err = run(capsys, str(tmp_path))
    assert code == 2 and "glibc must be a string" in err


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_requirements_in_is_not_a_pypi_package(capsys, tmp_path, monkeypatch, fake_pypi, as_json):
    # A mistyped .in file is a missing input, not a requirement to query on PyPI.
    monkeypatch.chdir(tmp_path)
    args = ["requirements-dev.in", "-p", "3.13", "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert "requirements-dev.in: no such file" in err
    assert not fake_pypi
    if as_json:
        assert json.loads(out)["error"] == {
            "type": "SourceError",
            "message": "requirements-dev.in: no such file",
        }
    else:
        assert not out


@pytest.mark.parametrize("as_json", [False, True])
def test_dockerfile_missing_path(capsys, tmp_path, monkeypatch, fake_pypi, as_json):
    monkeypatch.chdir(tmp_path)
    args = ["--dockerfile", "nope"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert err == "wheelgap: error: --dockerfile nope: no such file\n"
    assert not fake_pypi
    if as_json:
        assert json.loads(out)["error"] == {
            "message": "--dockerfile nope: no such file",
            "type": "DockerfileError",
        }
    else:
        assert not out


@pytest.mark.parametrize("as_json", [False, True])
def test_dockerfile_folder(capsys, tmp_path, monkeypatch, fake_pypi, as_json):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker").mkdir()
    args = ["--dockerfile", "docker"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    expected = "--dockerfile docker: is a folder; pass the Dockerfile in it, e.g. docker/Dockerfile"
    assert err == f"wheelgap: error: {expected}\n"
    assert not fake_pypi
    if as_json:
        assert json.loads(out)["error"] == {
            "message": expected,
            "type": "DockerfileError",
        }
    else:
        assert not out


def test_json_reports_input_errors_on_stdout(capsys, fixtures, tmp_path):
    def json_error(*argv):
        code, out, err = run(capsys, *argv, "--json")
        assert code == 2 and err.startswith("wheelgap: error: ")  # stderr and exit code as without --json
        return json.loads(out)["error"]

    def plain_out(*argv):
        code, out, _ = run(capsys, *argv)
        assert code == 2
        return out

    missing = str(tmp_path / "nonexistent.lock")
    error = json_error(missing)
    assert set(error) == {"message", "type"} and "nonexistent.lock" in error["message"]
    assert plain_out(missing) == ""

    missing_dockerfile = str(tmp_path / "nonexistent_dockerfile")
    error = json_error("--dockerfile", missing_dockerfile)
    assert error == {
        "message": f"--dockerfile {missing_dockerfile}: no such file",
        "type": "DockerfileError",
    }
    assert plain_out("--dockerfile", missing_dockerfile) == ""

    (tmp_path / "docker_dir").mkdir()
    error = json_error("--dockerfile", str(tmp_path / "docker_dir"))
    assert set(error) == {"message", "type"}
    assert error["type"] == "DockerfileError"
    assert "is a folder; pass the Dockerfile in it" in error["message"]
    assert plain_out("--dockerfile", str(tmp_path / "docker_dir")) == ""

    error = json_error("six", "-t", "nosuchplatform")
    assert error["type"] == "TargetError" and "nosuchplatform" in error["message"]
    assert plain_out("six", "-t", "nosuchplatform") == ""

    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nglibc = 2.31\n")
    error = json_error(str(tmp_path))
    assert error["type"] == "SourceError" and "glibc must be a string" in error["message"]
    assert plain_out(str(tmp_path)) == ""

    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap\n")  # malformed TOML: the catch-all branch
    error = json_error(str(tmp_path))
    assert error["type"] == "TOMLDecodeError"
    assert plain_out(str(tmp_path)) == ""

    error = json_error("--list-targets", "--glibc", "bad")  # --list-targets --json normally prints a list
    assert error["type"] == "TargetError" and "glibc" in error["message"]


def test_running_python_reports_freethreaded(monkeypatch):
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: 1 if name == "Py_GIL_DISABLED" else None)
    assert cli.running_python() == f"{sys.version_info.major}.{sys.version_info.minor}t"


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="needs a free-threaded build")
def test_default_python_on_freethreaded_build(capsys, fixtures):
    code = main([str(fixtures / "pylockproj"), "-t", "linux-aarch64", "--json"])
    assert code in (0, 1)
    assert json.loads(capsys.readouterr().out)["targets"][0]["python"].endswith("t")


def project_with_lock(tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    return tmp_path


def test_python_version_file_sets_default(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    code, out, err = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert code == 1 and not err
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.14t"]


def test_python_version_file_several_lines_and_patch_versions(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.13.1\n# both builds\n\n3.14t\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.13", "3.14t"]


def test_flags_beat_python_version_file(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-p", "3.12", "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_pyproject_beats_python_version_file(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[tool.wheelgap]\npython = ["3.12"]\n', encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_python_version_file_skips_unusable_lines(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("system\npypy@3.10\n3.13\n", encoding="utf-8")
    code, out, err = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert code == 1
    assert "ignoring 'system'" in err and "ignoring 'pypy@3.10'" in err
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.13"]


def test_python_version_file_with_nothing_usable_uses_running_python(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("system\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == [cli.running_python()]


@pytest.mark.parametrize("flag", ["", ","])
@pytest.mark.parametrize("as_json", [False, True])
def test_empty_python_flag_is_an_error(capsys, fixtures, flag, as_json):
    args = [str(fixtures / "uvproj"), "-p", flag, "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert "wheelgap: error: no Python version given (-p is empty)" in err
    if as_json:
        assert json.loads(out)["error"] == {
            "message": "no Python version given (-p is empty)",
            "type": "TargetError",
        }
    else:
        assert not out


@pytest.mark.parametrize("python", ['""', '["3.13", ""]', '","', '["3.13", " , "]'])
@pytest.mark.parametrize("as_json", [False, True])
def test_empty_python_in_config_is_an_error(capsys, fixtures, tmp_path, as_json, python):
    project = project_with_lock(tmp_path, fixtures)
    (project / "pyproject.toml").write_text(f"[tool.wheelgap]\npython = {python}\n", encoding="utf-8")
    args = [str(project), "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    # The setting is the cause, so the message names it: the run never sees -p.
    message = '[tool.wheelgap] python is empty: remove it to use the default, or list versions like ["3.13"]'
    assert f"wheelgap: error: {project / 'pyproject.toml'}: {message}" in err
    if as_json:
        assert json.loads(out)["error"] == {
            "message": f"{project / 'pyproject.toml'}: {message}",
            "type": "SourceError",
        }
    else:
        assert not out


def test_empty_python_list_in_config_takes_the_default(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / "pyproject.toml").write_text("[tool.wheelgap]\npython = []\n", encoding="utf-8")
    # An empty list is "not set", as before: the run takes the default Python rather than failing.
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == [cli.running_python()]


@pytest.mark.parametrize("as_json", [False, True])
def test_free_threaded_python_before_3_13_is_an_error(capsys, fixtures, fake_pypi, as_json):
    # No cp312t wheel can exist: this used to report every compiled package as a source build and exit 1.
    args = [str(fixtures / "uvproj"), "-p", "3.12t", "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert "wheelgap: error: free-threading starts at Python 3.13 (not 3.12t)" in err
    if as_json:
        assert json.loads(out)["error"] == {
            "message": "free-threading starts at Python 3.13 (not 3.12t)",
            "type": "TargetError",
        }
    else:
        assert not out
    assert fake_pypi == []


@pytest.mark.parametrize("where", ["config", "python-version"])
def test_free_threaded_python_before_3_13_in_project_files_is_an_error(capsys, fixtures, tmp_path, where):
    project = project_with_lock(tmp_path, fixtures)
    if where == "config":
        (project / "pyproject.toml").write_text('[tool.wheelgap]\npython = ["3.13", "3.12t"]\n', encoding="utf-8")
    else:
        (project / ".python-version").write_text("3.12t\n", encoding="utf-8")
    code, out, err = run(capsys, str(project), "-t", "linux-x86_64")
    assert code == 2
    assert "wheelgap: error: free-threading starts at Python 3.13 (not 3.12t)" in err
    assert not out


def test_text_summary_lists_only_targets_with_gaps(capsys, fixtures):
    code, out, _ = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64,musl-aarch64,win-arm64",
        "--glibc",
        "2.39",
    )
    assert code == 1
    lines = out.splitlines()
    summary_index = next(i for i, line in enumerate(lines) if "have gaps:" in line)
    assert lines[summary_index - 2 : summary_index] == [
        "musl-aarch64  1 from source, 1 missing",
        "win-arm64     1 from source, 1 missing",
    ]
    assert not any(line.startswith("linux-aarch64 ") for line in lines)


def test_single_target_has_no_per_target_summary(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64")
    assert code == 1
    assert "have gaps:" in out
    assert not any(line.startswith("musl-aarch64 ") for line in out.splitlines())


ARMGAP_THREE_TARGETS = ("-p", "3.13", "-t", "linux-aarch64,musl-aarch64,win-arm64", "--glibc", "2.39")


def test_json_summary_per_target(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "armgap"), *ARMGAP_THREE_TARGETS, "--json")
    assert code == 1
    assert json.loads(out)["summary"]["per_target"] == {
        "linux-aarch64": {"source": 0, "missing": 0, "unknown": 0},
        "musl-aarch64": {"source": 1, "missing": 1, "unknown": 0},
        "win-arm64": {"source": 1, "missing": 1, "unknown": 0},
    }
    code, out, _ = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json")
    assert code == 1
    summary = json.loads(out)["summary"]
    assert summary["with_gaps"] == 2
    assert summary["per_target"] == {"musl-aarch64": {"source": 1, "missing": 1, "unknown": 0}}


def test_summary_per_target_counts_unknown(capsys, fake_pypi):
    # notonindex isn't on the fake index, so it's unknown on both targets; nowheels builds from source on both.
    args = ("notonindex==1.0", "nowheels==0.9", "-p", "3.13", "-t", "linux-x86_64,win-amd64")
    code, out, _ = run(capsys, *args, "--json")
    assert code == 1
    assert json.loads(out)["summary"]["per_target"] == {
        "linux-x86_64": {"source": 1, "missing": 0, "unknown": 1},
        "win-amd64": {"source": 1, "missing": 0, "unknown": 1},
    }
    code, out, _ = run(capsys, *args, "--color", "never")
    assert "linux-x86_64  1 from source, 1 unknown" in out.splitlines()
    code, out, _ = run(capsys, *args, "--markdown")
    assert "- linux-x86\\_64: 1 from source, 1 unknown" in out.splitlines()  # labels are escaped like the table's
    assert "- win-amd64: 1 from source, 1 unknown" in out.splitlines()


def test_markdown_summary_per_target(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "armgap"), *ARMGAP_THREE_TARGETS, "--markdown")
    assert code == 1
    lines = out.splitlines()
    summary_index = lines.index("2 of 2 packages have gaps: 1 will build from source, 1 can't install at all.")
    assert lines[summary_index - 3 : summary_index] == [
        "- musl-aarch64: 1 from source, 1 missing",
        "- win-arm64: 1 from source, 1 missing",
        "",
    ]
    assert "- linux-aarch64" not in out
    code, out, _ = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--markdown")
    assert code == 1
    assert "have gaps:" in out
    assert not any(line.startswith("- ") for line in out.splitlines())


UPGRADE = ("newpylag==1.0", "nowheels==0.9", "purewheel==3.1", "-t", "linux-x86_64,win-amd64", "--color", "never")
NEXT_NOTE = "wheelgap: note: next is Python 3.15, the newest CPython this wheelgap version knows\n"


def test_python_next_names_what_blocks_the_upgrade(capsys, fake_pypi):
    # newpylag has a 3.14 wheel on Linux but not a 3.15 one; nowheels builds from source on both, so it doesn't block.
    code, out, err = run(capsys, *UPGRADE, "-p", "3.14", "-p", "next")
    assert code == 1
    assert err == NEXT_NOTE
    assert "Python 3.14, 3.15" in out
    lines = out.splitlines()
    summary = lines.index("2 of 3 packages have gaps: 2 will build from source.")
    assert lines[summary + 1] == "blocking 3.15: newpylag"
    # The order of -p doesn't matter, and next-t blocks the same way.
    code, out, err = run(capsys, *UPGRADE, "-p", "next-t,3.14")
    assert "blocking 3.15t: newpylag" in out.splitlines()
    assert err == NEXT_NOTE.replace("next is Python 3.15", "next-t is Python 3.15t")

    code, out, err = run(capsys, *UPGRADE, "-p", "3.14", "-p", "next", "--json")
    assert code == 1
    assert err == NEXT_NOTE
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert data["blocking_newer_python"] == ["newpylag"]
    assert {t["python"] for t in data["targets"]} == {"3.14", "3.15"}

    code, out, _ = run(capsys, *UPGRADE, "-p", "3.14", "-p", "next", "--markdown")
    assert "blocking 3.15: newpylag" in out.splitlines()

    code, out, err = run(capsys, *UPGRADE, "-p", "3.14", "-p", "next", "-q")
    assert out.splitlines()[-1] == "blocking 3.15: newpylag"
    assert not err  # -q drops the note too


def test_blocking_newer_python_only_counts_new_gaps(capsys, fake_pypi):
    # Ignoring newpylag on Linux leaves only nowheels, which has gaps on 3.14 too.
    code, out, _ = run(capsys, *UPGRADE, "-p", "3.14,3.15", "--ignore", "newpylag:linux-x86_64")
    assert "blocking 3.15: none (every gap on 3.15 is on 3.14 too)" in out.splitlines()
    code, out, _ = run(capsys, *UPGRADE, "-p", "3.14,3.15", "--ignore", "newpylag:linux-x86_64", "--json")
    assert json.loads(out)["blocking_newer_python"] == []
    # Nothing is printed when there are no gaps at all.
    code, out, _ = run(capsys, "purewheel==3.1", "-p", "3.14", "-p", "3.15", "--color", "never")
    assert code == 0 and "blocking" not in out


def _blocking(capsys, tmp_path, requirements, *args):
    """The blocking line of the text report and blocking_newer_python of --json, for a requirements file."""
    path = tmp_path / "requirements.txt"
    path.write_text(requirements, encoding="utf-8")
    argv = [str(path), "-p", "3.14,next", "-t", "linux-x86_64", "--color", "never", *args]
    _, out, _ = run(capsys, *argv)
    line = next((ln for ln in out.splitlines() if ln.startswith("blocking ")), None)
    _, out, _ = run(capsys, *argv, "--json")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    return line, data["blocking_newer_python"]


@pytest.mark.parametrize(
    "requirements,line,names",
    [
        # Unknown on the newer Python only, or on both: not listed, and "none" would be untrue.
        (
            'does-not-exist==1.0; python_version >= "3.15"\nnowheels==0.9\n',
            "blocking 3.15: none known (1 package couldn't be checked)",
            [],
        ),
        ("does-not-exist==1.0\nnowheels==0.9\n", "blocking 3.15: none known (1 package couldn't be checked)", []),
        (
            "does-not-exist==1.0\nnewpylag==1.0\n",
            "blocking 3.15: newpylag (1 package couldn't be checked)",
            ["newpylag"],
        ),
        # n/a on the older Python counts as fine there, so a source build on the newer one blocks it.
        ('nowheels==0.9; python_version >= "3.15"\n', "blocking 3.15: nowheels", ["nowheels"]),
        # missing (a wheel for 3.14, no sdist) blocks, like source.
        ("cp314only==1.0\n", "blocking 3.15: cp314only", ["cp314only"]),
        # Forked versions: 3.14 builds forked from source too, so it doesn't block 3.15.
        (
            'forked==0.9; python_version < "3.15"\nforked==1.0; python_version >= "3.15"\n',
            "blocking 3.15: none (every gap on 3.15 is on 3.14 too)",
            [],
        ),
    ],
)
def test_blocking_newer_python_cases(capsys, fake_pypi, tmp_path, requirements, line, names):
    assert _blocking(capsys, tmp_path, requirements) == (line, names)


def test_blocking_newer_python_names_a_package_once(capsys, fake_pypi, tmp_path):
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "requirements.txt").write_text("newpylag==1.0\n", encoding="utf-8")
    argv = [str(tmp_path / "a" / "requirements.txt"), str(tmp_path / "b" / "requirements.txt"), "-p", "3.14,3.15"]
    _, out, _ = run(capsys, *argv, "-t", "linux-x86_64", "--color", "never")
    assert "blocking 3.15: newpylag" in out.splitlines()
    _, out, _ = run(capsys, *argv, "-t", "linux-x86_64", "--json")
    assert json.loads(out)["blocking_newer_python"] == ["newpylag"]


def test_blocking_newer_python_bundled_on_the_older_python(capsys, fake_pypi, fixtures):
    # Pyodide 0.29 (Python 3.13) bundles fastlib; there's no 3.14 Pyodide wheel, and Pyodide can't build the sdist.
    args = ("fastlib==2.0.0", "-p", "3.13,3.14", "-t", "pyodide", "--pyodide-lock", str(fixtures / "pyodide-lock.json"))
    _, out, _ = run(capsys, *args, "--json")
    data = json.loads(out)
    assert data["packages"][0]["results"] == {"3.13 pyodide": "bundled", "3.14 pyodide": "missing"}
    assert data["blocking_newer_python"] == ["fastlib"]


@pytest.mark.parametrize("pythons", [["3.15"], ["3.15", "3.15t"], ["3.13", "3.14", "3.15"]])
def test_no_blocking_summary_without_exactly_two_pythons_one_newer(capsys, fake_pypi, pythons):
    args = [arg for p in pythons for arg in ("-p", p)]
    code, out, _ = run(capsys, *UPGRADE, *args)
    assert code == 1 and "blocking" not in out
    code, out, _ = run(capsys, *UPGRADE, *args, "--json")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert "blocking_newer_python" not in data
    code, out, _ = run(capsys, *UPGRADE, *args, "--markdown")
    assert "blocking" not in out


def test_python_next_in_pyproject(capsys, fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("newpylag==1.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\npython = ["3.14", "next"]\ntargets = ["linux-x86_64"]\n', encoding="utf-8"
    )
    code, out, err = run(capsys, str(tmp_path / "requirements.txt"), "--color", "never")
    assert code == 1
    assert err == NEXT_NOTE
    assert "blocking 3.15: newpylag" in out.splitlines()


def test_gap_summary_counts_each_package_once():
    from wheelgap.check import Row, Status
    from wheelgap.cli import Style, gap_summary
    from wheelgap.sources import Package
    from wheelgap.targets import build_targets

    lin, pyo = build_targets(["3.13"], ["linux", "pyodide"])
    a = Row(Package("a", "1"), {lin: Status.SOURCE, pyo: Status.MISSING})
    b = Row(Package("b", "1"), {lin: Status.UNKNOWN, pyo: Status.UNKNOWN})
    c = Row(Package("c", "1"), {lin: Status.SOURCE, pyo: Status.WHEEL})
    assert gap_summary([a, b, c], 3, Style(False)) == (
        "3 of 3 packages have gaps: 1 will build from source, 1 can't install at all, 1 unknown."
    )
    # singular when only one package in total
    assert gap_summary([a], 1, Style(False)) == ("1 of 1 package has gaps: 1 can't install at all.")


def test_quiet_from_pyproject_allows_json_explain(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "musl-aarch64", "--json", "--explain")
    assert code == 1 and not err  # used to exit 2 with a usage error
    assert "rejected" in json.loads(out)["packages"][0]


def test_no_quiet_overrides_pyproject(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "musl-aarch64", "--no-quiet", "--explain")
    assert code == 1 and "via: app → armlib" in out and "is a glibc wheel" in out  # full --explain output


def test_quiet_still_says_why_a_package_is_unknown(capsys, fake_pypi):
    code, out, err = run(capsys, "does-not-exist==1.0", "-p", "3.13", "-t", "linux", "-q")
    assert code == 1
    assert "1 unknown" in out
    assert "could not check does-not-exist: not on index" in err


def test_python_version_file_with_a_bom(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("﻿3.12\n", encoding="utf-8")  # as Notepad saves it
    code, out, err = run(capsys, str(tmp_path), "-t", "win-amd64", "--json")
    assert not err and [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_list_targets_uses_pyproject_limits(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nglibc = "2.17"\n', encoding="utf-8")
    code, out, _ = run(capsys, "--list-targets")
    assert code == 0 and "glibc <= 2.17" in out


def test_simple_index_url_exits_2(capsys, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://pypi.org/simple")
    code, _, err = run(capsys, "requests==2.32.3", "-t", "linux")
    assert code == 2 and "needs the JSON API" in err


def test_output_is_utf8_on_a_cp1252_pipe(monkeypatch, capsys):
    import io
    import sys as _sys

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(_sys, "stdout", stream)
    assert main(["--list-targets"]) == 0
    stream.flush()
    assert stream.encoding.lower().replace("-", "") == "utf8"


def test_markdown_report(capsys, fake_pypi, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")  # as in the Action: Markdown must still have no colour codes
    code, out, err = run(
        capsys,
        "fastlib==2.0.0",
        "nowheels==0.9",
        "purewheel==3.1",
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-amd64",
        "--markdown",
    )
    assert code == 1 and not err
    lines = out.splitlines()
    assert lines[0] == "### wheelgap: 2 of 3 packages have gaps"
    assert "| package | version | linux-x86\\_64 | win-amd64 |" in lines
    assert "| --- | --- | --- | --- |" in lines
    assert "| fastlib | 2.0.0 | ✓ | ● source |" in lines
    assert "| nowheels | 0.9 | ● source | ● source |" in lines
    assert "purewheel" not in out  # hidden like the text table, unless --all
    assert "2 of 3 packages have gaps: 2 will build from source." in lines
    assert "\033[" not in out and "<details>" not in out


def test_markdown_all_explain_and_quiet(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n")
    code, out, err = run(
        capsys, "fastlib==2.0.0", "purewheel==3.1", "-p", "3.13", "-t", "win-amd64", "--markdown", "--all", "--explain"
    )
    assert code == 1 and not err  # quiet = true doesn't clash with --explain, as with --json
    assert "| purewheel | 3.1 | ✓ |" in out.splitlines()
    assert "<details><summary>Why (rejected wheels)</summary>" in out
    assert "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" in out


def test_markdown_explain_suggest(capsys, fake_pypi):
    code, out, _ = run(
        capsys,
        "laggard==1.0",
        "-t",
        "win-amd64,musl-x86_64,musl-aarch64,linux-riscv64",
        "-p",
        "3.13",
        "--markdown",
        "--explain",
        "--suggest",
    )
    assert code == 1
    assert "<details><summary>Why (rejected wheels)</summary>" in out
    assert "- laggard 1.0 -\\> 1.2 has wheels for win-amd64, musl-x86\\_64, musl-aarch64" in out
    assert "- laggard 1.0: no newer release has a wheel for linux-riscv64" in out

    code, out, _ = run(
        capsys,
        "wheellost==1.0",
        "-t",
        "linux-x86_64,win-amd64,linux-riscv64",
        "-p",
        "3.13",
        "--markdown",
        "--explain",
        "--suggest",
    )
    assert code == 1
    assert "- wheellost 1.0: no newer release has a wheel for linux-riscv64" in out
    assert "- wheellost 1.0: no newer release has a wheel for win-amd64 without losing one elsewhere" in out


def test_markdown_clean_run(capsys, fake_pypi):
    code, out, _ = run(
        capsys, "purewheel==3.1", "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--markdown", "--exit-zero"
    )
    assert code == 0
    assert out.splitlines()[0] == "### wheelgap: every package has a wheel for all 2 targets"
    assert "|" not in out


def test_markdown_and_json_conflict(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["six", "--json", "--markdown"])
    assert exc.value.code == 2
    assert "--json and --markdown" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--list-targets", "--json", "--markdown"])
    assert "--json and --markdown" in capsys.readouterr().err


def test_markdown_escapes_and_fail_on_missing(capsys, fake_pypi, tmp_path):
    req = tmp_path / "req_[a]#b.txt"  # characters every OS allows in a filename
    req.write_text("nowheels==0.9\nhttps://files.example.com/my_pkg-1.0.tar.gz\n")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--fail-on", "missing")
    assert code == 0
    assert "`req_[a]#b.txt`" in out  # paths and URLs go in code spans, shown exactly and never linked
    assert "`https://files.example.com/my_pkg-1.0.tar.gz` (url)" in out
    assert "Source builds are allowed (fail-on missing), so this run passes." in out
    # The Action's summary run adds --exit-zero; the note depends on --fail-on, not on that.
    code, out2, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--exit-zero")
    assert "Source builds are allowed" not in out2
    _, out3, _ = run(
        capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--fail-on", "missing", "--exit-zero"
    )
    assert "Source builds are allowed" in out3


def test_markdown_helpers():
    from wheelgap.cli import _code, _md

    assert _md("./vendor/__x__/ a|b R&amp;D") == "./vendor/\\_\\_x\\_\\_/ a\\|b R\\&amp;D"
    assert _code("a`b") == "``a`b``"
    assert _code("`x`") == "`` `x` ``"
    assert _code("a|b") == "`a|b`"
    assert _code("") == '`""`'


def test_markdown_lookup_errors_and_empty_names(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("nosuchpkg==1.0\n-e git+https://x.example/r.git#egg=\n")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown")
    lines = out.splitlines()
    assert any(ln.startswith("Could not check nosuchpkg: `") for ln in lines)
    assert any(
        ln.startswith('Skipped 1 non-index package: `""`') or '`""`' in ln for ln in lines if ln.startswith("Skipped")
    )


# ranged: 1.5 wheel, 1.9 sdist only, 2.0 wheel. Unconstrained, 2.0 is checked (wheel).
@pytest.mark.parametrize("constraint", ["ranged==1.9", "ranged<2"])
def test_requirements_constraint_picks_the_checked_release(tmp_path, fake_pypi, capsys, constraint):
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text(constraint + "\n", encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1
    assert "1.9" in out and "source" in out


def test_requirements_requirement_inside_constraints_file_is_checked(tmp_path, fake_pypi, capsys):
    # A -r inside a constraints file is requirements, not more constraints (#602): psycopg2 is
    # checked (and builds from source here) instead of being silently skipped.
    path = tmp_path / "a.txt"
    path.write_text("-c cons.txt\nrequests==2.32.5\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r extra.txt\nurllib3<3\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.14", "-t", "linux-x86_64", "--json")
    assert code == 1
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["psycopg2"]["results"] == {"linux-x86_64": "source"}
    assert packages["psycopg2"]["version"] == "2.9.11"
    assert set(packages) == {"psycopg2", "requests"}


def test_requirements_marker_constraint_is_unknown(tmp_path, fake_pypi, capsys):
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text('ranged<2; sys_platform == "win32"\n', encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1
    (row,) = json.loads(out)["packages"]
    assert row["version"] is None  # not looked up: the latest release isn't what pip would install
    assert "marker" in row["error"]


# requires-python ==3.12.*, so uv kept only fastlib's cp312 wheel; PyPI (fake_pypi) has a cp313 one.
LOCK_312_ONLY = (
    'version = 1\nrequires-python = "==3.12.*"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\ndependencies = [{ name = "fastlib" }]\n\n'
    '[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/p/fastlib-2.0.0.tar.gz" }\n'
    'wheels = [{ url = "https://files.pythonhosted.org/p/fastlib-2.0.0-cp312-cp312-manylinux_2_17_x86_64.whl" }]\n'
)


def test_lock_for_another_python_is_rechecked_online(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0, out  # PyPI's cp313 wheel counts; the lock alone would say "source"
    assert "uv.lock is locked for Python ==3.12.*, so it may be missing wheels for Python 3.13" in err
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]


def test_lock_recheck_uses_the_lookup_cache(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    for _ in range(2):
        code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
        assert code == 0, out
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]  # the second run's re-check was cached


def test_lock_inside_requires_python_stays_offline(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64")
    assert code == 0 and "note:" not in err
    assert fake_pypi == []


def environments_lock(*markers):
    """A uv.lock as uv writes it for `[tool.uv] environments = [markers]`: the wheels for other platforms are left
    out (uv 0.13.1 keeps only the Linux x86_64 ones for the first marker in LINUX_ONLY)."""
    supported = "supported-markers = [\n" + "".join(f"    {json.dumps(m)},\n" for m in markers) + "]\n"
    return LOCK_312_ONLY.replace('"==3.12.*"\n', '">=3.12"\n' + supported)


LINUX_ONLY = ("platform_machine == 'x86_64' and sys_platform == 'linux'",)


def test_lock_environments_refuse_a_target_outside_them(capsys, fake_pypi, tmp_path):
    # Without the check, win-amd64 would read `source`: a gap that only says the lock left Windows out.
    (tmp_path / "uv.lock").write_text(environments_lock(*LINUX_ONLY), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64,win-amd64,macos-arm64")
    assert code == 2 and out == ""
    assert "uv.lock: this lock doesn't cover win-amd64, macos-arm64: it was locked for `[tool.uv] environments`" in err
    assert "(platform_machine == 'x86_64' and sys_platform == 'linux'), so `uv sync` fails there" in err
    assert "of the targets asked for it covers linux-x86_64 (" in err
    assert fake_pypi == []
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "win-amd64", "--json")
    assert code == 2 and "it covers none of the targets asked for" in err
    assert "this lock doesn't cover win-amd64" in json.loads(out)["error"]["message"]
    # --online looks the locked packages up again, but not the ones the lock left out.
    assert run(capsys, str(tmp_path), "-p", "3.12", "-t", "win-amd64", "--online")[0] == 2


def test_lock_environments_in_a_lock_uv_wrote(capsys, fake_pypi, fixtures):
    code, out, err = run(capsys, str(fixtures / "uvenvironments"), "-p", "3.13", "--json")
    data = json.loads(out)
    assert code == 0 and [t["platform"] for t in data["targets"]] == ["linux-x86_64"]
    assert "which leave out linux-aarch64, macos-arm64, win-amd64: checking the default targets it covers" in err
    assert [(p["name"], p["results"]) for p in data["packages"]] == [("markupsafe", {"linux-x86_64": "wheel"})]
    assert fake_pypi == []


def test_lock_environments_leave_covered_targets_alone(capsys, fake_pypi, tmp_path):
    answers = []
    for lock in (environments_lock(*LINUX_ONLY), environments_lock()):  # the second has no limit
        (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
        code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64", "--json")
        assert code == 0 and "note:" not in err
        answers.append(json.loads(out))
    assert answers[0] == answers[1]
    assert answers[0]["packages"][0]["results"] == {"linux-x86_64": "wheel"}


def test_lock_environments_narrow_the_default_targets(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(environments_lock("sys_platform == 'linux'"), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "--json")
    assert [t["platform"] for t in json.loads(out)["targets"]] == ["linux-x86_64", "linux-aarch64"]
    assert (
        "wheelgap: note: uv.lock is locked for `[tool.uv] environments` only, which leave out macos-arm64, "
        "win-amd64: checking the default targets it covers (linux-x86_64, linux-aarch64)\n"
    ) in err
    assert code == 1  # the lock's own gap on linux-aarch64 still counts
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "--json", "-q")
    assert [t["platform"] for t in json.loads(out)["targets"]] == ["linux-x86_64", "linux-aarch64"] and err == ""
    # The same platforms asked for by name are an error, and so is a lock that covers no default target.
    assert run(capsys, str(tmp_path), "-p", "3.12", "-t", ",".join(DEFAULT_PLATFORMS))[0] == 2
    (tmp_path / "uv.lock").write_text(environments_lock("sys_platform == 'emscripten'"), encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12")
    assert code == 2 and "it covers none of the targets asked for" in err and "note:" not in err


def test_lock_environments_narrow_the_default_targets_in_diff(capsys, fake_pypi, tmp_path):
    old, new = tmp_path / "old" / "uv.lock", tmp_path / "new" / "uv.lock"
    for lock in (old, new):
        lock.parent.mkdir()
        lock.write_text(environments_lock("sys_platform == 'linux'"), encoding="utf-8")
    code, _, err = run(capsys, "diff", str(old), str(new), "-p", "3.12")
    assert code == 0 and "checking the default targets it covers (linux-x86_64, linux-aarch64)" in err
    # A platform the new lock gives up is refused, not dropped.
    old.write_text(environments_lock(), encoding="utf-8")
    code, _, err = run(capsys, "diff", str(old), str(new), "-p", "3.12")
    assert code == 2 and "this lock doesn't cover macos-arm64, win-amd64" in err
    # A side that isn't a uv.lock leaves out nothing, so nothing is dropped: what the lock gives up is refused.
    req = tmp_path / "requirements.txt"
    req.write_text("markupsafe==3.0.2\n", encoding="utf-8")
    old.write_text(environments_lock("sys_platform == 'linux'"), encoding="utf-8")
    assert run(capsys, "diff", str(req), str(new), "-p", "3.12")[0] == 2
    assert run(capsys, "diff", str(new), str(req), "-p", "3.12")[0] == 2


def test_lock_environments_leave_out_advisory_targets(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(environments_lock(*LINUX_ONLY), encoding="utf-8")
    args = ("-p", "3.12", "-t", "linux-x86_64,win-amd64", "--advisory", "win-amd64", "--json")
    code, out, err = run(capsys, str(tmp_path), *args)
    assert code == 0 and [t["platform"] for t in json.loads(out)["targets"]] == ["linux-x86_64"]
    assert "which leave out the advisory targets win-amd64: not checking them" in err
    # Asked for and required, it's still an error.
    assert run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64,win-amd64")[0] == 2


def test_lock_environments_narrowing_keeps_advisory_targets(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(environments_lock("sys_platform == 'linux'"), encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.12", "--json", "--advisory", "linux-aarch64,win-amd64")
    assert code == 0 and json.loads(out)["advisory"] == ["linux-aarch64"]


@pytest.mark.parametrize(
    "marker,python,covered",
    [
        ("python_full_version < '3.14' and sys_platform == 'linux'", "3.13", True),
        ("python_full_version < '3.14' and sys_platform == 'linux'", "3.14", False),
        ("python_full_version >= '3.14'", "3.13", False),
        ("python_full_version >= '3.14'", "3.14t", True),
        ("platform_python_implementation == 'CPython'", "3.13", True),
        ("platform_python_implementation == 'PyPy'", "3.13", False),
        ("implementation_name == 'cpython' and os_name == 'posix'", "3.13", True),
        ("platform_machine == 'aarch64'", "3.13", False),
        ("sys_platform != 'emscripten'", "3.13", True),
        ("(sys_platform == 'darwin' or sys_platform == 'linux') and python_version < '3.14'", "3.13", True),
    ],
)
def test_lock_environment_markers_are_evaluated_for_the_target(capsys, fake_pypi, tmp_path, marker, python, covered):
    (tmp_path / "uv.lock").write_text(environments_lock(marker), encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", python, "-t", "linux-x86_64")
    assert ("this lock doesn't cover linux-x86_64" in err) is not covered
    assert (code == 2) is not covered


def test_lock_environments_cover_a_target_if_any_marker_holds(capsys, fake_pypi, tmp_path):
    lock = environments_lock("sys_platform == 'win32'", "sys_platform == 'linux'")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12,3.13", "-t", "linux-x86_64,win-amd64,macos-arm64")
    assert code == 2 and "this lock doesn't cover 3.12 macos-arm64, 3.13 macos-arm64: " in err
    assert "it covers 3.12 linux-x86_64, 3.12 win-amd64, 3.13 linux-x86_64, 3.13 win-amd64 (" in err


def test_lock_environments_cover_a_default_platform_for_some_pythons_only(capsys, fake_pypi, tmp_path):
    # Python 3.14 was asked for, so leaving it out quietly would hide that `uv sync` fails there.
    lock = environments_lock("python_full_version < '3.14' and sys_platform == 'linux'")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.13,3.14")
    assert code == 2 and "which leave out macos-arm64, win-amd64: checking" in err
    assert "this lock doesn't cover 3.14 linux-x86_64, 3.14 linux-aarch64: " in err


def test_pdm_lock_targets_refuse_a_target_outside_them(capsys, fake_pypi, fixtures):
    # What PDM 2.29.7 writes for `pdm lock --platform linux --python "==3.13.*"`. MarkupSafe has macOS and aarch64
    # wheels on PyPI, but the lock kept only the x86_64 Linux ones: these used to be false `source` gaps.
    lock = fixtures / "pdmtargets" / "pdm.lock"
    code, out, err = run(capsys, str(lock), "-p", "3.13", "-t", "macos-arm64,linux-aarch64,linux-x86_64", "--json")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert message == (
        "pdm.lock: this lock doesn't cover 3.13 macos-arm64, 3.13 linux-aarch64: it was locked for `pdm lock` targets "
        "only (requires_python ==3.13.*, platform manylinux_2_40_x86_64), so `pdm install` fails there and the lock "
        "leaves out the packages and wheels other environments need; of the targets asked for it covers 3.13 "
        "linux-x86_64 (to check the others, lock for them too with `pdm lock --append --platform ... --python ...`)"
    )
    assert err == f"wheelgap: error: {message}\n"
    # glibc 2.36 is older than the lock's manylinux_2_40, which PDM installs anyway (with a warning).
    code, out, err = run(capsys, str(lock), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0 and json.loads(out)["packages"][0]["results"] == {"linux-x86_64": "wheel"}
    assert run(capsys, str(lock), "-p", "3.14", "-t", "linux-x86_64")[0] == 2  # outside requires_python
    # The default platforms are narrowed to the ones it covers, as for a uv.lock.
    code, out, err = run(capsys, str(lock), "-p", "3.13", "--json")
    assert code == 0 and [t["platform"] for t in json.loads(out)["targets"]] == ["linux-x86_64"]
    assert (
        "wheelgap: note: pdm.lock is locked for `pdm lock` targets only, which leave out linux-aarch64, "
        "macos-arm64, win-amd64: checking the default targets it covers (linux-x86_64)\n"
    ) in err
    assert fake_pypi == []


def test_pdm_lock_targets_name_the_default_python_when_it_is_the_cause(
    capsys, fake_pypi, fixtures, tmp_path, monkeypatch
):
    # No -p, `python` setting or .python-version: the running Python, which the lock's ==3.13.* leaves out.
    monkeypatch.setattr(cli, "running_python", lambda: "3.15")
    (tmp_path / "pdm.lock").write_bytes((fixtures / "pdmtargets" / "pdm.lock").read_bytes())
    code, out, err = run(capsys, str(tmp_path / "pdm.lock"))
    assert code == 2
    assert "this lock doesn't cover 3.15 linux-x86_64, 3.15 linux-aarch64, 3.15 macos-arm64, 3.15 win-amd64: " in err
    assert err.rstrip().endswith(
        "; Python 3.15 is the default, the one wheelgap runs on (no -p, `python` setting or .python-version): "
        "pass -p 3.13 for a Python the lock covers, or run `pdm lock --append --python 3.15` to lock for it"
    )
    # With -p the Python was asked for, so there's no hint, but the message still names it.
    code, out, err = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.15", "-t", "linux-x86_64")
    assert code == 2 and "doesn't cover 3.15 linux-x86_64: " in err and "is the default" not in err


def test_pdm_lock_targets_say_when_free_threading_is_the_cause(capsys, fake_pypi, fixtures, tmp_path):
    text = (fixtures / "pdmtargets" / "pdm.lock").read_text(encoding="utf-8")
    text = text.replace(
        'platform = "manylinux_2_40_x86_64"\n',
        'platform = "manylinux_2_40_x86_64"\nimplementation = "cpython"\ngil_disabled = false\n',
    )
    (tmp_path / "pdm.lock").write_text(text.replace("==3.13.*", ">=3.13"), encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.14t", "-t", "linux-x86_64")
    assert code == 2 and "doesn't cover 3.14t linux-x86_64: " in err
    assert "(requires_python >=3.13, platform manylinux_2_40_x86_64, implementation cpython, gil_disabled false)" in err
    assert run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.14", "-t", "linux-x86_64")[0] == 0


def test_pdm_lock_targets_that_a_target_cannot_decide_are_not_guessed(capsys, fake_pypi, fixtures, tmp_path):
    text = (fixtures / "pdmtargets" / "pdm.lock").read_text(encoding="utf-8")
    (tmp_path / "pdm.lock").write_text(text.replace('"==3.13.*"', '">=3.13.2"'), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and out == ""
    assert "can't tell whether this lock covers 3.13 linux-x86_64: it was locked for `pdm lock` targets only" in err
    assert "`pdm install` fails outside them; check targets the lock targets decide" in err


@pytest.mark.parametrize("targets", ['"linux"', '["linux"]', "[{requires_python = 3}]", '[{gil_disabled = "no"}]'])
def test_pdm_lock_targets_must_be_tables(capsys, tmp_path, targets):
    (tmp_path / "pdm.lock").write_text(f"[metadata]\ntargets = {targets}\n", encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and "`metadata.targets`" in err


@pytest.mark.parametrize(
    "marker,target",
    [
        ("platform_release == '6.1'", "linux-x86_64"),  # nothing a target fixes
        ("platform_machine == 'AMD64'", "win32"),  # 32-bit Python on 64-bit Windows says AMD64 or ARM64
        ("python_full_version >= '3.12.4'", "linux-x86_64"),  # 3.12.0 no, 3.12.9 yes
        ("not a marker", "linux-x86_64"),
    ],
)
def test_lock_environments_that_a_target_cannot_decide_are_not_guessed(capsys, fake_pypi, tmp_path, marker, target):
    (tmp_path / "uv.lock").write_text(environments_lock(marker), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", target)
    assert code == 2 and out == ""
    assert f"uv.lock: can't tell whether this lock covers {target}: it was locked for" in err
    # A marker that does hold settles it, whatever the others say.
    (tmp_path / "uv.lock").write_text(environments_lock(marker, "os_name != 'java'"), encoding="utf-8")
    assert run(capsys, str(tmp_path), "-p", "3.12", "-t", target)[0] != 2


def test_lock_environments_must_be_a_list_of_markers(capsys, tmp_path):
    lock = LOCK_312_ONLY.replace("version = 1\n", "version = 1\nsupported-markers = \"sys_platform == 'linux'\"\n", 1)
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64")
    assert code == 2 and "uv.lock: `supported-markers` isn't a list of strings" in err


def test_failed_recheck_keeps_the_lock_answers(capsys, tmp_path, monkeypatch):
    import urllib.error

    from wheelgap import pypi

    def offline(url, timeout=20.0):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(pypi, "fetch_json", offline)
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12,3.13", "-t", "linux-x86_64", "--json")
    rows = {p["name"]: p["results"] for p in json.loads(out)["packages"]}
    # 3.12 is still answered from the lock; 3.13 falls back to the lock's (possibly incomplete) files.
    assert rows["fastlib"] == {"3.12 linux-x86_64": "wheel", "3.13 linux-x86_64": "source"}
    assert "1 that couldn't be looked up may show false gaps" in err and code == 1


def test_recheck_leaves_packages_from_other_indexes_alone(capsys, fake_pypi, tmp_path):
    lock = LOCK_312_ONLY.replace('registry = "https://pypi.org/simple"', 'registry = "https://download.example/whl"')
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12,3.13", "-t", "linux-x86_64")
    assert fake_pypi == [] and "1 from other indexes may show false gaps" in err


def test_list_targets_shows_the_manylinux_floor_outside_x86(capsys):
    code, out, _ = run(capsys, "--list-targets", "--json", "--glibc", "2.12")
    assert code == 0
    by_name = {row["name"]: row["accepts"] for row in json.loads(out)}
    assert by_name["linux-x86_64"] == "manylinux x86_64, glibc <= 2.12"
    assert by_name["linux-aarch64"] == "manylinux aarch64, no manylinux (glibc 2.12 is below the 2.17 floor)"


def test_quiet_hides_the_lock_note_unless_lookups_failed(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert code == 0 and err == ""
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]  # still re-checked, just quietly


def test_pipfile_lock_that_is_not_a_json_object_is_a_clear_error(capsys, tmp_path):
    (tmp_path / "Pipfile.lock").write_text("[]", encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "Pipfile.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and "not a Pipfile.lock (expected a JSON object)" in err


def test_quiet_still_prints_the_lock_note_when_the_answer_may_be_wrong(capsys, tmp_path, monkeypatch):
    import urllib.error

    from wheelgap import pypi

    def offline(url, timeout=20.0):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(pypi, "fetch_json", offline)
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    _, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert "couldn't be looked up may show false gaps" in err

    other = LOCK_312_ONLY.replace('registry = "https://pypi.org/simple"', 'registry = "https://download.example/whl"')
    (tmp_path / "uv.lock").write_text(other, encoding="utf-8")
    _, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert "from other indexes may show false gaps" in err


@pytest.mark.parametrize("color", ["never", "always"])
def test_ignore_known_gap_text(capsys, fixtures, color):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "NewGlibc",
        "--all",
        "--explain",
        "--color",
        color,
    )
    assert code == 0 and not err
    assert "The 1 package has a wheel for 1 target." in out
    assert "armlib" in out
    assert out.count("newglibc") == 1  # only the disclosure, never a table or explanation row
    assert "ignored: newglibc" in out
    if color == "always":
        assert "\033[2mignored: newglibc\033[0m" in out


def test_ignore_known_gap_json(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "NewGlibc",
        "--json",
        "--explain",
    )
    data = json.loads(out)
    assert code == 0 and not err
    assert data["ignored"] == ["newglibc"]
    assert data["summary"] == {
        "packages": 1,
        "with_gaps": 0,
        "per_target": {"linux-aarch64": {"source": 0, "missing": 0, "unknown": 0}},
    }
    assert [p["name"] for p in data["packages"]] == ["armlib"]


def test_ignore_known_gap_markdown(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "NewGlibc",
        "--markdown",
        "--all",
        "--explain",
    )
    assert code == 0 and not err
    assert "Ignored: `newglibc`" in out
    assert out.count("newglibc") == 1
    assert "| armlib |" in out
    assert "1 package · Python 3.13" in out
    assert "every package has a wheel" in out


def test_ignore_repeated_comma_separated_and_duplicate_names(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "musl-aarch64",
        "--ignore",
        "NewGlibc, ArmLib",
        "--ignore",
        "NEWGLIBC,not-installed",
        "--json",
    )
    data = json.loads(out)
    assert code == 0
    assert "wheelgap: note: --ignore matches no package: not-installed" in err
    assert data["ignored"] == ["armlib", "newglibc"]
    assert data["packages"] == []
    assert data["summary"] == {
        "packages": 0,
        "with_gaps": 0,
        "per_target": {"musl-aarch64": {"source": 0, "missing": 0, "unknown": 0}},
    }


def test_ignore_canonicalizes_punctuation(capsys, fixtures, tmp_path):
    lock = (fixtures / "armgap" / "uv.lock").read_text().replace("newglibc", "new_glibc_core")
    (tmp_path / "uv.lock").write_text(lock)
    code, out, err = run(
        capsys,
        str(tmp_path),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "NEW.GLIBC--CORE",
        "--json",
    )
    data = json.loads(out)
    assert code == 0 and not err
    assert data["ignored"] == ["new_glibc_core"]  # reported as the lockfile names it
    assert [p["name"] for p in data["packages"]] == ["armlib"]


def test_ignore_does_not_hide_other_gaps(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "musl-aarch64",
        "--ignore",
        "NewGlibc",
        "--json",
    )
    data = json.loads(out)
    assert code == 1 and not err
    assert data["ignored"] == ["newglibc"]
    assert data["summary"] == {
        "packages": 1,
        "with_gaps": 1,
        "per_target": {"musl-aarch64": {"source": 1, "missing": 0, "unknown": 0}},
    }
    assert data["packages"][0]["results"] == {"musl-aarch64": "source"}


def test_ignore_is_disclosed_in_quiet_output(capsys, fixtures):
    assert run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "NewGlibc",
        "--quiet",
    ) == (0, "ignored: newglibc\n", "")


@pytest.mark.parametrize("output", ["--json", "--markdown", "--color=never"])
def test_no_ignore_keeps_gaps(capsys, fixtures, output):
    code, out, err = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64", output)
    assert code == 1 and not err
    if output == "--json":
        data = json.loads(out)
        assert data["ignored"] == []
        assert data["summary"] == {
            "packages": 2,
            "with_gaps": 1,
            "per_target": {"linux-aarch64": {"source": 0, "missing": 1, "unknown": 0}},
        }
        assert [p["name"] for p in data["packages"]] == ["armlib", "newglibc"]
    else:
        assert "1 of 2 packages has gaps" in out
        assert "newglibc" in out
        assert "ignored" not in out.lower()


@pytest.mark.parametrize("output", ["--json", "--markdown", "--color=never", "--quiet"])
def test_ignore_config_matches_cli(capsys, fixtures, tmp_path, output):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    args = (str(tmp_path), "-p", "3.13", "-t", "linux-aarch64", output)
    expected = run(capsys, *args, "--ignore", "NewGlibc")
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nignore = ["NewGlibc"]\n')
    actual = run(capsys, *args)
    assert actual == expected
    assert actual[0] == 0 and not actual[2]
    assert "newglibc" in actual[1]


@pytest.mark.parametrize("value", ['["NewGlibc, ArmLib", "NEWGLIBC"]', '"NewGlibc, ArmLib"'])
def test_ignore_config_accepts_list_or_string(capsys, fixtures, tmp_path, value):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text(f"[tool.wheelgap]\nignore = {value}\n")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "musl-aarch64", "--json")
    assert code == 0 and not err
    data = json.loads(out)
    assert data["ignored"] == ["armlib", "newglibc"]
    assert data["packages"] == []
    assert data["summary"] == {
        "packages": 0,
        "with_gaps": 0,
        "per_target": {"musl-aarch64": {"source": 0, "missing": 0, "unknown": 0}},
    }


@pytest.mark.parametrize("override, ignored, total", [("ArmLib", ["armlib"], 1), ("", [], 2)])
def test_ignore_cli_replaces_config(capsys, fixtures, tmp_path, override, ignored, total):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nignore = ["NewGlibc"]\n')
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-aarch64", "--ignore", override, "--json")
    assert code == 1 and not err  # the configured newglibc ignore was replaced, not extended
    data = json.loads(out)
    assert data["ignored"] == ignored
    assert data["summary"] == {
        "packages": total,
        "with_gaps": 1,
        "per_target": {"linux-aarch64": {"source": 0, "missing": 1, "unknown": 0}},
    }
    assert "newglibc" in [p["name"] for p in data["packages"]]


def test_ignore_config_empty_list_keeps_gaps(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    args = (str(tmp_path), "-p", "3.13", "-t", "linux-aarch64", "--json")
    expected = run(capsys, *args)
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nignore = []\n")
    assert run(capsys, *args) == expected
    assert expected[0] == 1


@pytest.mark.parametrize("value", ["true", "1", '["newglibc", 1]', '{name = "newglibc"}'])
def test_ignore_config_rejects_invalid_types(capsys, fixtures, tmp_path, value):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text(f"[tool.wheelgap]\nignore = {value}\n")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-aarch64")
    assert code == 2 and not out
    assert "[tool.wheelgap] ignore must be a list of strings" in err


# #142: --ignore NAME:TARGET. In armgap, armlib builds from source on musl-aarch64 only; newglibc is missing everywhere.
ARMGAP_TWO = ("-p", "3.13", "-t", "linux-aarch64,musl-aarch64")


@pytest.mark.parametrize("target", ["musl-aarch64", "alpine-arm64", "MUSL_ARM64"])
def test_ignore_on_one_target_text(capsys, fixtures, target):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "--color", "never", "--all", "--explain")
    code, out, err = run(capsys, *args, "--ignore", f"ArmLib:{target},newglibc")
    assert code == 0 and not err
    assert "ignored: armlib (musl-aarch64), newglibc" in out
    assert "● source (ignored)" in out  # the cell is still shown, marked as accepted
    assert "armlib 1.4.0 on" not in out  # and not explained as a gap
    # A clean run that only passes because of the ignore doesn't claim a wheel everywhere.
    assert "✓ The 1 package has a wheel for every one of 2 targets, apart from 1 ignored gap." in out


def test_ignore_on_one_target_keeps_other_targets(capsys, fixtures):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "--color", "never", "--explain")
    code, out, err = run(capsys, *args, "--ignore", "newglibc:musl-aarch64")
    assert code == 1 and err == NOTES_HINT  # the only stderr line: --notes would look armlib up
    assert "ignored: newglibc (musl-aarch64)" in out
    # Per-target counts leave the ignored cell out; newglibc still counts on linux-aarch64.
    assert "linux-aarch64  1 missing\n" in out
    assert "musl-aarch64   1 from source\n" in out
    assert "2 of 2 packages have gaps: 1 will build from source, 1 can't install at all." in out
    assert "newglibc 2.0 on linux-aarch64:" in out
    assert "newglibc 2.0 on musl-aarch64:" not in out
    assert "armlib 1.4.0 on musl-aarch64:" in out
    # Markdown and --json count per target the same way.
    code, out, _ = run(capsys, str(fixtures / "armgap"), *ARMGAP_TWO, "--markdown", "--ignore", "newglibc:musl-aarch64")
    assert code == 1
    assert "- linux-aarch64: 1 missing\n- musl-aarch64: 1 from source\n" in out
    code, out, _ = run(capsys, str(fixtures / "armgap"), *ARMGAP_TWO, "--json", "--ignore", "newglibc:musl-aarch64")
    assert json.loads(out)["summary"]["per_target"] == {
        "linux-aarch64": {"source": 0, "missing": 1, "unknown": 0},
        "musl-aarch64": {"source": 1, "missing": 0, "unknown": 0},
    }


def test_ignore_on_one_target_json(capsys, fixtures):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "--json", "--explain")
    code, out, err = run(capsys, *args, "--ignore", "armlib:musl-aarch64,newglibc:linux-aarch64")
    assert code == 1 and err == NOTES_HINT
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert data["ignored"] == []  # still whole-package names only
    assert data["ignored_targets"] == [
        {"name": "armlib", "platforms": ["musl-aarch64"]},
        {"name": "newglibc", "platforms": ["linux-aarch64"]},
    ]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["armlib"]["results"] == {"linux-aarch64": "wheel", "musl-aarch64": "source"}  # real statuses
    assert packages["armlib"]["rejected"] == {}
    assert list(packages["newglibc"]["rejected"]) == ["musl-aarch64"]
    assert data["summary"] == {
        "packages": 2,
        "with_gaps": 1,
        # Only newglibc's musl cell counts: armlib's musl source build and newglibc on linux-aarch64 are ignored.
        "per_target": {
            "linux-aarch64": {"source": 0, "missing": 0, "unknown": 0},
            "musl-aarch64": {"source": 0, "missing": 1, "unknown": 0},
        },
    }


def test_ignore_on_one_target_markdown(capsys, fixtures):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "--markdown", "--all", "--explain")
    code, out, err = run(capsys, *args, "--ignore", "armlib:musl-aarch64,NewGlibc")
    assert code == 0 and not err
    assert "Ignored: `armlib` (musl-aarch64), `newglibc`" in out
    assert "| armlib | 1.4.0 | ✓ | ● source (ignored) |" in out
    assert out.startswith("### wheelgap: every package has a wheel for all 2 targets, apart from 1 ignored gap\n")
    assert "Why (rejected wheels)" not in out


@pytest.mark.parametrize("output", ["--markdown", "--color=never"])
def test_ignore_on_one_target_hidden_and_clean_lines(capsys, fixtures, output):
    # newglibc's gaps (missing on both targets) are ignored where it's checked; armlib's musl gap still counts.
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, output, "--ignore", "newglibc:linux-aarch64,newglibc:musl-aarch64")
    code, out, err = run(capsys, *args)
    assert code == 1 and not err
    assert "1 package with only ignored gaps hidden" in out
    assert "with wheels for every target" not in out
    # Both gaps ignored and no --all: nothing is hidden as wheel-only, and the clean line says what was ignored.
    code, out, err = run(capsys, *args[:-1], "newglibc:all,armlib:musl-aarch64")
    assert code == 0 and not err
    if output == "--markdown":
        assert "### wheelgap: every package has a wheel for all 2 targets, apart from 3 ignored gaps\n" in out
    else:
        assert "✓ All 2 packages have a wheel for every one of 2 targets, apart from 3 ignored gaps." in out


def test_ignored_missing_with_counted_source_passes_fail_on_missing(capsys, fixtures):
    # puresdist and nativeext build from source on Linux (counted) and are missing on iOS (ignored), so
    # --fail-on missing passes and the summary names only the source builds. purewheel has a wheel on iOS.
    ignore = "puresdist:ios-arm64,nativeext:ios-arm64,fastlib:ios-arm64,purewheel:ios-arm64"
    args = (str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,ios-arm64", "--color", "never", "--all")
    code, out, err = run(capsys, *args, "--ignore", ignore, "--fail-on", "missing")
    assert code == 0, err
    assert "2 of 5 packages have gaps: 2 will build from source." in out
    assert "can't install" not in out
    assert "Source builds are allowed (--fail-on missing), so this run passes." in out
    # The added "via" hints widen the package column; assert cells rather than fixed padding.
    assert any(line.split() == ["purewheel", "3.1", "✓", "✓"] for line in out.splitlines())
    assert any(
        line.split() == ["puresdist", "1.0", "●", "source", "✗", "missing", "(ignored)"] for line in out.splitlines()
    )


def test_cell_marks_only_ignored_gaps():
    from wheelgap.check import Row
    from wheelgap.sources import Package
    from wheelgap.targets import build_targets

    lin, ios = build_targets(["3.13"], ["linux-x86_64", "ios-arm64"])
    row = Row(Package("a", "1"), {lin: Status.UNKNOWN, ios: Status.UNKNOWN}, ignored=frozenset({ios}))
    assert cli.cell(row, ios) == ("? unknown (ignored)", "dim")
    assert cli.cell(row, lin) == ("? unknown", "dim")
    row = Row(Package("a", "1"), {lin: Status.SOURCE, ios: Status.WHEEL}, ignored=frozenset({ios}))
    assert cli.cell(row, ios) == ("✓", "green")
    assert cli.cell(row, lin) == ("● source", "yellow")


def test_hidden_line_counts_wheels_and_ignored_gaps_apart():
    from wheelgap.check import Row
    from wheelgap.sources import Package
    from wheelgap.targets import build_targets

    lin, musl = build_targets(["3.13"], ["linux-aarch64", "musl-aarch64"])
    clean = Row(Package("a", "1"), {lin: Status.WHEEL, musl: Status.WHEEL})
    accepted = Row(Package("b", "1"), {lin: Status.WHEEL, musl: Status.SOURCE}, ignored=frozenset({musl}))
    wheel_on_ignored = Row(Package("c", "1"), {lin: Status.WHEEL, musl: Status.WHEEL}, ignored=frozenset({musl}))
    assert cli.hidden_line([clean]) == "1 package with wheels for every target hidden"
    assert cli.hidden_line([clean, wheel_on_ignored]) == "2 packages with wheels for every target hidden"
    assert cli.hidden_line([accepted, accepted]) == "2 packages with only ignored gaps hidden"
    assert cli.hidden_line([clean, clean, accepted]) == (
        "2 packages with wheels for every target and 1 with only ignored gaps hidden"
    )
    # A range split per Python: its clean release is hidden while its other release is shown with a gap.
    split = Package("s", None)
    old_release = Row(Package("s", "1"), {lin: Status.WHEEL, musl: Status.WHEEL}, input_package=split)
    new_release = Row(Package("s", "2"), {lin: Status.WHEEL, musl: Status.SOURCE}, input_package=split)
    assert cli.hidden_line([old_release], [new_release]) == "1 other release of a package with gaps hidden"
    assert cli.hidden_line([clean, old_release], [new_release]) == (
        "1 package with wheels for every target and 1 other release of a package with gaps hidden"
    )
    assert cli.hidden_line([old_release], []) == "1 package with wheels for every target hidden"


def test_ignore_on_one_target_quiet(capsys, fixtures):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "-q", "--color", "never")
    assert run(capsys, *args, "--ignore", "armlib:musl-aarch64,newglibc") == (
        0,
        "ignored: armlib (musl-aarch64), newglibc\n",
        "",
    )
    code, out, err = run(capsys, *args, "--ignore", "armlib:linux-aarch64,newglibc")
    assert code == 1 and not err
    assert out.splitlines() == [
        "1 of 1 package has gaps: 1 will build from source.",
        "gaps: armlib",
        "ignored: armlib (linux-aarch64), newglibc",
    ]


@pytest.mark.parametrize(
    "extra, code",
    [
        (("-t", "linux-aarch64", "--ignore", "newglibc:linux-aarch64"), 0),
        (("-t", "linux-aarch64", "--ignore", "newglibc:linux/arm64"), 0),  # a Docker platform name, as -t takes
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:linux-aarch64"), 1),
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:linux-aarch64", "--fail-on", "missing"), 1),
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:linux-aarch64", "--exit-zero"), 0),
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:all"), 1),  # armlib still builds from source
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:all", "--fail-on", "missing"), 0),
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:all,armlib:musl"), 1),  # musl-x86_64
        (("-t", "linux-aarch64", "--ignore", "newglibc:musl-aarch64"), 1),
        # A platform is ignored for every Python checked on it.
        (("-p", "3.14", "-t", "musl-aarch64", "--ignore", "newglibc,armlib:musl-aarch64"), 0),
        # A plain name still wins over a per-target entry for the same package.
        (("-t", "linux-aarch64,musl-aarch64", "--ignore", "newglibc:linux-aarch64,newglibc,armlib:musl-aarch64"), 0),
    ],
)
def test_ignore_on_one_target_exit_code(capsys, fixtures, extra, code):
    assert run(capsys, str(fixtures / "armgap"), "-p", "3.13", *extra, "--json")[0] == code


def test_ignore_on_one_target_config_matches_cli(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    args = (str(tmp_path), *ARMGAP_TWO, "--json")
    expected = run(capsys, *args, "--ignore", "armlib:musl-aarch64,newglibc")
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nignore = ["armlib:musl-aarch64", "newglibc"]\n')
    assert run(capsys, *args) == expected
    assert expected[0] == 0 and json.loads(expected[1])["ignored_targets"] == [
        {"name": "armlib", "platforms": ["musl-aarch64"]}
    ]


@pytest.mark.parametrize(
    "value, message",
    [
        ("armlib:linux-aarch46", "--ignore 'armlib:linux-aarch46': unknown target 'linux-aarch46'; did you mean"),
        ("armlib:", "--ignore 'armlib:': expected NAME or NAME:TARGET"),
        (":musl-aarch64", "--ignore ':musl-aarch64': expected NAME or NAME:TARGET"),
    ],
)
@pytest.mark.parametrize("output", ["--color=never", "--json"])
def test_ignore_unknown_target_is_an_error(capsys, fixtures, value, message, output):
    code, out, err = run(capsys, str(fixtures / "armgap"), *ARMGAP_TWO, "--ignore", value, output)
    assert code == 2
    assert f"wheelgap: error: {message}" in err
    if output == "--json":
        assert json.loads(out)["error"]["type"] == "TargetError"
    else:
        assert not out


@pytest.mark.parametrize(
    "value, entry",
    [
        ("psycopg2:linux,musl", "musl"),
        ("armlib:linux-aarch64, MUSL-ARM64", "MUSL-ARM64"),
        ("armlib:linux-aarch64,newglibc,linux/amd64", "linux/amd64"),
        ("armlib:linux-aarch64,all", "all"),
    ],
)
def test_ignore_second_target_after_comma_is_an_error(capsys, fixtures, tmp_path, value, entry):
    # Split on commas first, the last entry would be a plain package name: ignored everywhere, silently.
    code, out, err = run(capsys, str(fixtures / "armgap"), *ARMGAP_TWO, "--ignore", value)
    assert code == 2 and not out
    assert f"wheelgap: error: --ignore {value!r}: {entry!r} is a target, but each NAME:TARGET takes one target" in err
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text(f"[tool.wheelgap]\nignore = [{json.dumps(value)}]\n")
    code, _, err = run(capsys, str(tmp_path), *ARMGAP_TWO)
    assert code == 2 and f"ignore in [tool.wheelgap] {value!r}: {entry!r} is a target" in err


@pytest.mark.parametrize(
    "ignore",
    [
        ("musl-aarch64,newglibc:linux-aarch64",),  # a target-like name before any NAME:TARGET is a package name
        ("newglibc:linux-aarch64", "musl-aarch64"),  # and so is one in its own --ignore value
        ("newglibc:linux-aarch64,armlib",),  # a package name after NAME:TARGET is fine
    ],
)
def test_ignore_target_like_names_outside_a_target_list(capsys, fixtures, ignore):
    args = [str(fixtures / "armgap"), *ARMGAP_TWO, "--json"]
    for value in ignore:
        args += ["--ignore", value]
    code, out, err = run(capsys, *args)
    assert code in (0, 1) and "error" not in json.loads(out)


def test_ignore_unknown_target_in_config_is_an_error(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nignore = ["armlib:solaris"]\n')
    code, out, err = run(capsys, str(tmp_path), *ARMGAP_TWO)
    assert code == 2 and not out
    assert "wheelgap: error: ignore in [tool.wheelgap] 'armlib:solaris': unknown target 'solaris'" in err


def test_ignore_unchecked_target_prints_note(capsys, fixtures):
    args = (str(fixtures / "armgap"), *ARMGAP_TWO, "--json")
    plain = run(capsys, *args)
    code, out, err = run(capsys, *args, "--ignore", "armlib:win-amd64,nosuchpkg:musl-aarch64")
    assert (code, out) == plain[:2]  # nothing on the checked targets is ignored, so nothing is reported
    assert json.loads(out)["ignored_targets"] == []
    assert "wheelgap: note: --ignore matches no package: nosuchpkg:musl-aarch64\n" in err
    assert "wheelgap: note: --ignore matches no checked target: armlib:win-amd64\n" in err
    _, _, err = run(capsys, *args, "--ignore", "armlib:win-amd64", "-q")
    assert err == ""


def test_ignore_on_one_target_with_alpine_dockerfile(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM --platform=linux/arm64 python:3.13-alpine\n")
    # With an Alpine image, linux/arm64 means musl-aarch64, for -t and for --ignore alike.
    args = (str(tmp_path), "--dockerfile", str(dockerfile), "--json")
    code, out, err = run(capsys, *args, "--ignore", "armlib:linux/arm64,newglibc")
    assert code == 0 and not err
    assert json.loads(out)["ignored_targets"] == [{"name": "armlib", "platforms": ["musl-aarch64"]}]


# #114: a uv.lock package the lock says came from a private index, whose only locked wheel is a Windows one.
PRIVATE_UV_LOCK = (
    'version = 1\nrequires-python = ">=3.12"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
    'dependencies = [{ name = "purewheel" }]\n\n'
    '[[package]]\nname = "purewheel"\nversion = "3.1"\n'
    'source = { registry = "https://pkgs.example.com/simple" }\n'
    'wheels = [{ url = "https://pkgs.example.com/purewheel-3.1-cp313-cp313-win_amd64.whl" }]\n'
)


# The same package in a poetry.lock, recorded through its [package.source] table.
PRIVATE_POETRY_LOCK = (
    '[[package]]\nname = "purewheel"\nversion = "3.1"\ndescription = ""\noptional = false\n'
    'python-versions = ">=3.9"\ngroups = ["main"]\n'
    "files = [\n"
    '    {file = "purewheel-3.1-cp313-cp313-win_amd64.whl", hash = "sha256:00"},\n'
    "]\n\n"
    "[package.source]\n"
    'type = "legacy"\n'
    'url = "https://pkgs.example.com/simple"\n'
    'reference = "private"\n\n'
    '[metadata]\nlock-version = "2.1"\npython-versions = ">=3.9"\ncontent-hash = "00"\n'
)


# And in a pylock.toml, through the package's index key.
PRIVATE_PYLOCK = (
    'lock-version = "1.0"\ncreated-by = "pip"\n\n'
    '[[packages]]\nname = "purewheel"\nversion = "3.1"\nindex = "https://pkgs.example.com/simple"\n'
    'wheels = [{ name = "purewheel-3.1-cp313-cp313-win_amd64.whl", '
    'url = "https://pkgs.example.com/purewheel-3.1-cp313-cp313-win_amd64.whl", hashes = { sha256 = "00" } }]\n'
)


@pytest.mark.parametrize(
    ("name", "lock"),
    [("uv.lock", PRIVATE_UV_LOCK), ("poetry.lock", PRIVATE_POETRY_LOCK), ("pylock.toml", PRIVATE_PYLOCK)],
)
def test_online_keeps_private_index_packages_off_pypi(capsys, fake_pypi, tmp_path, name, lock):
    (tmp_path / name).write_text(lock, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "--online", "-p", "3.13", "-t", "linux-x86_64")
    assert not [url for url in fake_pypi if "purewheel" in url], fake_pypi
    # Its locked files still count: the only locked wheel is a Windows one, so there's a gap.
    assert code == 1
    assert "purewheel" in out
    assert "--online looks up PyPI packages only; kept the locked files of 1 package from other indexes" in err


def test_online_quiet_hides_the_private_index_note(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(PRIVATE_UV_LOCK, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "--online", "-q", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1 and err == ""


def test_online_still_warns_about_private_packages_locked_for_other_pythons(capsys, fake_pypi, tmp_path):
    # --online can't refresh a private-index package, so a lock made for 3.12 may lack its 3.13 wheels.
    lock = LOCK_312_ONLY.replace('registry = "https://pypi.org/simple"', 'registry = "https://download.example/whl"')
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "--online", "-q", "-p", "3.13", "-t", "linux-x86_64")
    assert fake_pypi == [] and "1 from other indexes may show false gaps" in err


def test_offline_uses_the_lock_and_never_contacts_the_index(capsys, fixtures, fake_pypi):
    args = [str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64", "--json"]
    expected = run(capsys, *args)
    assert fake_pypi == []  # a uv.lock inside its requires-python needs no lookups
    assert run(capsys, *args, "--offline") == expected
    assert fake_pypi == [] and json.loads(expected[1])["packages"]


def test_offline_refuses_inputs_that_need_the_index(capsys, fixtures, fake_pypi):
    code, out, err = run(capsys, str(fixtures / "reqproj" / "requirements.txt"), "--offline", "-p", "3.13")
    assert code == 2 and not out
    assert err == (
        "wheelgap: error: --offline: requirements.txt needs the index for 4 packages "
        "(fastlib, unpinned, winonly, …); use a lockfile that lists each package's files, or drop --offline\n"
    )
    assert fake_pypi == []


def test_offline_names_at_most_three_packages(capsys, tmp_path, fake_pypi):
    req = tmp_path / "requirements.txt"
    req.write_text("a==1\nb==1\nc==1\nd==1\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "--offline", "-p", "3.13", "--json")
    assert code == 2 and fake_pypi == []
    message = (
        "--offline: requirements.txt needs the index for 4 packages (a, b, c, …); "
        "use a lockfile that lists each package's files, or drop --offline"
    )
    assert json.loads(out)["error"]["message"] == message


def test_offline_skips_the_requires_python_recheck(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "--offline", "-q", "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == []
    rows = {p["name"]: p["results"] for p in json.loads(out)["packages"]}
    assert rows["fastlib"] == {"linux-x86_64": "source"}  # only the lock's cp312 wheel, as the note warns
    assert "uv.lock is locked for Python ==3.12.*, so it may be missing wheels for Python 3.13" in err
    assert "with --offline its packages weren't re-checked on the index, so they may show false gaps" in err


def test_offline_refuses_a_pyodide_lock_url(capsys, fixtures, fake_pypi):
    url = "https://cdn.example.com/pyodide-lock.json"
    code, out, err = run(capsys, str(fixtures / "uvproj"), "--offline", "--pyodide-lock", url, "-p", "3.13")
    assert code == 2 and not out and fake_pypi == []
    assert f"--offline: {url} would be downloaded" in err


def test_offline_and_online_are_a_usage_error(capsys, fixtures):
    with pytest.raises(SystemExit) as excinfo:
        main([str(fixtures / "uvproj"), "--offline", "--online"])
    assert excinfo.value.code == 2
    assert "--offline and --online can't be used together" in capsys.readouterr().err


def test_offline_from_config_never_contacts_the_index(capsys, tmp_path, fake_pypi):
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13")
    assert code == 2 and not out and fake_pypi == []
    assert err == (
        "wheelgap: error: offline = true in [tool.wheelgap]: requirements.txt needs the index for 1 package "
        "(fastlib); use a lockfile that lists each package's files, or pass --online for this run\n"
    )


def test_offline_from_config_skips_the_requires_python_recheck(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-q", "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == []
    assert "with offline = true in [tool.wheelgap] its packages weren't re-checked on the index" in err


def test_online_flag_overrides_offline_in_config(capsys, tmp_path, fake_pypi):
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "requirements.txt"), "--online", "-p", "3.13", "--json")
    assert code != 2 and fake_pypi  # looked up, not refused
    assert json.loads(out)["packages"][0]["name"] == "fastlib"


def test_offline_in_config_must_be_a_boolean(capsys, tmp_path, fixtures):
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\noffline = "yes"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13")
    assert code == 2 and "[tool.wheelgap] offline must be a boolean" in err


def test_ignore_matches_no_package_prints_note(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "newglibc,nosuchpkg",
    )
    assert code == 0
    assert "wheelgap: note: --ignore matches no package: nosuchpkg" in err
    note_line = err.split("matches no package:")[-1]
    assert "newglibc" not in note_line


def test_ignore_all_names_match_no_note(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "newglibc",
    )
    assert "matches no package" not in err


def test_ignore_unmatched_is_hidden_with_quiet(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64",
        "--ignore",
        "nosuchpkg",
        "--quiet",
    )
    assert err == ""
    assert code == 1


def test_ignore_unmatched_does_not_change_exit_code(capsys, fixtures):
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64"]
    code_plain, _, _ = run(capsys, *args)
    code_unmatched, _, err = run(capsys, *args, "--ignore", "nosuchpkg")
    assert "matches no package: nosuchpkg" in err
    assert code_unmatched == code_plain == 1


OUTPUT_SCHEMA = Path(__file__).parent.parent / "docs" / "wheelgap-output.schema.json"
JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}


def _check_schema(value, schema, root, where="$"):
    """Validate `value` against the JSON Schema keywords the output schema uses, and no others."""
    known = {"$ref", "type", "enum", "minimum", "required", "properties", "additionalProperties", "items", "minItems"}
    annotations = {"$schema", "$id", "$defs", "title", "description"}
    assert set(schema) <= known | annotations, f"{where}: the test doesn't check {set(schema) - known - annotations}"
    if "$ref" in schema:
        prefix, _, name = schema["$ref"].rpartition("/")
        assert prefix == "#/$defs", schema["$ref"]
        _check_schema(value, root["$defs"][name], root, where)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        # bool is an int in Python, but not an integer in JSON Schema
        if isinstance(value, bool):
            assert "boolean" in names, (where, value)
        else:
            assert isinstance(value, tuple(JSON_TYPES[n] for n in names)), (where, value)
    if "enum" in schema:
        assert value in schema["enum"], (where, value)
    if "minimum" in schema:
        assert value >= schema["minimum"], (where, value)
    if "minItems" in schema and isinstance(value, list):
        assert len(value) >= schema["minItems"], (where, value)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value), (where, sorted(value))
        for key, item in value.items():
            if key in properties:
                _check_schema(item, properties[key], root, f"{where}.{key}")
            else:
                extra = schema.get("additionalProperties", True)
                assert extra is not False, f"{where}: {key} isn't in the schema"
                if isinstance(extra, dict):
                    _check_schema(item, extra, root, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check_schema(item, schema["items"], root, f"{where}[{i}]")


def _assert_matches_output_schema(data):
    """Check report_json() output against docs/wheelgap-output.schema.json.

    The file is read with a small hand-written validator (no schema library, so
    pyproject.toml / uv.lock stay unchanged), then a few things a schema can't say.
    """
    schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    _check_schema(data, schema, schema)
    statuses = set(schema["$defs"]["status"]["enum"])
    assert statuses == {s.value for s in Status}
    assert isinstance(data, dict)
    for key in ("version", "sources", "targets", "packages", "skipped", "ignored", "ignored_targets", "summary"):
        assert key in data
    if "fix_summary" in data:
        fs = data["fix_summary"]
        assert fs["gaps"] == sum(v for k, v in fs.items() if k != "gaps")
        kinds = [
            e.get("reason") if e.get("reason") in ("not_looked_up", "uv_may_fork") else e["kind"]
            for p in data["packages"]
            for e in p.get("fix", {}).values()
        ]
        assert fs == {"gaps": len(kinds), **{k: kinds.count(k) for k in set(fs) - {"gaps"}}}
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["sources"], list) and all(isinstance(s, str) for s in data["sources"])
    assert isinstance(data["ignored"], list) and all(isinstance(s, str) for s in data["ignored"])
    labels = []
    for t in data["targets"]:
        assert set(t) == {"label", "python", "platform"}
        assert all(isinstance(t[k], str) and t[k] for k in t)
        labels.append(t["label"])
    ignored_on = {}
    for item in data["ignored_targets"]:
        assert set(item) == {"name", "platforms"} and item["platforms"]
        assert item["name"] not in data["ignored"]
        assert set(item["platforms"]) <= {t["platform"] for t in data["targets"]}
        ignored_on[item["name"]] = {t["label"] for t in data["targets"] if t["platform"] in item["platforms"]}
    assert len(labels) == len(set(labels))
    assert set(data.get("advisory", [])) <= set(labels)
    for pkg in data["packages"]:
        assert {"name", "version", "error", "results"} <= set(pkg)
        assert set(pkg) <= {
            "name",
            "version",
            "error",
            "results",
            "rejected",
            "via",
            "direct",
            "issues_url",
            "build_requires",
            "notes",
            "build_lab",
            "bundled",
            "bundled_version_differs",
            "conda_forge",
            "piwheels",
            "suggest",
            "last_with_wheel",
            "gap_wheel",
            "fix",
            "pip_refuses",
        }
        assert isinstance(pkg["name"], str) and pkg["name"]
        assert pkg["version"] is None or isinstance(pkg["version"], str)
        assert pkg["error"] is None or isinstance(pkg["error"], str)
        assert isinstance(pkg["results"], dict) and set(pkg["results"]) == set(labels)
        assert set(pkg["results"].values()) <= statuses
        if "rejected" in pkg:
            assert isinstance(pkg["rejected"], dict)
            for label, items in pkg["rejected"].items():
                assert label in labels
                assert isinstance(items, list)
                for item in items:
                    assert set(item) == {"file", "reason"}
                    assert isinstance(item["file"], str) and isinstance(item["reason"], str)
        if "via" in pkg:
            assert isinstance(pkg["via"], dict)
            for label, path in pkg["via"].items():
                assert label in labels
                assert isinstance(path, list) and all(isinstance(s, str) for s in path)
        if "direct" in pkg:  # gap targets only, each with at least one name
            assert isinstance(pkg["direct"], dict) and set(pkg["direct"]) <= set(pkg["rejected"])
            assert all(names and all(isinstance(n, str) for n in names) for names in pkg["direct"].values())
        if "issues_url" in pkg:
            assert pkg["issues_url"] is None or isinstance(pkg["issues_url"], str)
        if "build_requires" in pkg:
            reqs = pkg["build_requires"]
            assert reqs is None or (isinstance(reqs, list) and all(isinstance(r, str) for r in reqs))
        if "conda_forge" in pkg:  # a hint for some of the targets in rejected, never an empty one
            assert pkg["conda_forge"]["builds"] and set(pkg["conda_forge"]["builds"]) <= set(pkg["rejected"])
            assert all(pkg["conda_forge"]["builds"].values())
        if "piwheels" in pkg:  # a hint for some of the targets in rejected, never an empty one
            assert set(pkg["piwheels"]) == {"name", "version", "wheels", "apt"}
            assert all(isinstance(a, str) for a in pkg["piwheels"]["apt"])
            assert pkg["piwheels"]["wheels"] and set(pkg["piwheels"]["wheels"]) <= set(pkg["rejected"])
            assert all(pkg["piwheels"]["wheels"].values())
        for note in pkg.get("notes", []):
            assert set(note["targets"]) <= set(pkg.get("rejected", {})), "notes cover gap targets only"
        for label, result in pkg.get("build_lab", {}).items():  # source cells only, for the checked release
            assert pkg["results"][label] == "source" and result["version"] == pkg["version"]
        if "suggest" in pkg:  # a release for some of the targets in rejected
            assert set(pkg["suggest"]) <= set(pkg["rejected"]) and all(
                isinstance(v, str) for v in pkg["suggest"].values()
            )
        if "fix" in pkg:  # --fix: one entry per gap target, with the keys of its kind
            gap_labels = {k for k, v in pkg["results"].items() if v in ("source", "missing")}
            assert set(pkg["fix"]) <= gap_labels
            keys = {
                "upgrade": {"version", "prerelease", "exclude_newer", "how", "constraint", "gap_wheel"},
                "gap_wheel": {"wheel", "url", "sha256", "unofficial"},
                "none": {"reason", "abi3_only", "build_packages"},
            }
            for entry in pkg["fix"].values():
                assert set(entry) == {"kind"} | keys[entry["kind"]]
                if (build := entry.get("build_packages")) is not None:  # --build-info, a Linux source cell
                    assert set(build) == {"distro", "command", "packages", "needs", "distro_python"}
                    assert pkg["results"][next(k for k, v in pkg["fix"].items() if v is entry)] == "source"
        if "gap_wheel" in pkg:
            assert set(pkg["gap_wheel"]) <= set(pkg.get("rejected", {}))
            for label, wheel in pkg["gap_wheel"].items():
                assert pkg["results"][label] in ("source", "missing")
                assert {"wheel", "url", "sha256"} <= set(wheel) <= {"wheel", "url", "sha256", "install"}
                assert wheel["sha256"] and wheel["wheel"].endswith(".whl")
                assert wheel.get("install", "pip install --extra-index-url ").startswith(
                    "pip install --extra-index-url "
                )
        if "pip_refuses" in pkg:  # files uv installs from a uv.lock and pip would refuse: never an empty object
            assert pkg["pip_refuses"] and all(isinstance(why, str) and why for why in pkg["pip_refuses"].values())
        if "last_with_wheel" in pkg:  # an older release for gap targets no newer one has a wheel for
            assert "suggest" in pkg and not set(pkg["last_with_wheel"]) & set(pkg["suggest"])
            assert set(pkg["last_with_wheel"]) <= set(pkg["rejected"])
    for item in data["skipped"]:
        assert set(item) == {"name", "reason"}
        assert isinstance(item["name"], str) and isinstance(item["reason"], str)
    summary = data["summary"]
    # complete: only with --dockerfile-inputs, and false exactly when dockerfile_warnings lists a line.
    assert set(summary) - {"complete"} == {"packages", "with_gaps", "per_target"}
    assert ("complete" in summary) == ("dockerfile_warnings" in data)
    if "complete" in summary and data["dockerfile_warnings"]:
        assert summary["complete"] is False  # also false when -p/-t stood in for an unread image
    distinct_names = {pkg["name"] for pkg in data["packages"]}
    assert len(distinct_names) <= summary["packages"] <= len(data["packages"])
    assert isinstance(summary["with_gaps"], int) and 0 <= summary["with_gaps"] <= summary["packages"]
    # with_gaps counts packages that are not all wheel/n/a/bundled, leaving out the targets ignored for them
    gap_rows = [
        pkg
        for pkg in data["packages"]
        if any(
            s in {"source", "missing", "unknown"}
            for label, s in pkg["results"].items()
            if label not in ignored_on.get(pkg["name"], set())
        )
    ]
    assert len({pkg["name"] for pkg in gap_rows}) <= summary["with_gaps"] <= len(gap_rows)
    if len(distinct_names) == len(data["packages"]):
        assert summary["packages"] == len(data["packages"])
        assert summary["with_gaps"] == len(gap_rows)
    # per_target counts each target's gap cells, for every target (zeros included), leaving out ignored cells
    assert summary["per_target"] == {
        label: {
            s: sum(
                pkg["results"][label] == s and label not in ignored_on.get(pkg["name"], set())
                for pkg in data["packages"]
            )
            for s in ("source", "missing", "unknown")
        }
        for label in labels
    }


def test_json_output_matches_schema(capsys, fixtures):
    """--json on the uvproj and armgap fixtures matches the published schema."""
    cases = [
        [str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json"],
        [str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--json"],
        [str(fixtures / "uvproj"), "-p", "3.13,3.14t", "-t", "win-amd64", "--json"],
        [str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json", "--explain"],
        [str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json", "--ignore", "winonly"],
        [str(fixtures / "armgap"), "-p", "3.13,3.14", "-t", "linux-aarch64,musl-aarch64", "--json", "--explain"]
        + ["--ignore", "armlib:musl-aarch64,newglibc:linux-aarch64"],
        [str(fixtures / "armgap"), "-p", "3.13,3.14", "-t", "linux-aarch64,musl-aarch64", "--json"]
        + ["--advisory", "musl-aarch64"],
    ]
    for args in cases:
        code, out, err = run(capsys, *args)
        assert code in (0, 1), err
        data = json.loads(out)
        assert "error" not in data, data
        _assert_matches_output_schema(data)
        if "--explain" in args:
            assert all("rejected" in pkg and "via" in pkg and "notes" in pkg for pkg in data["packages"])
        else:
            assert all("notes" not in pkg for pkg in data["packages"])
        assert not any("build_requires" in pkg for pkg in data["packages"])  # only with --build-info
        if "winonly" in args:
            assert "winonly" in data["ignored"]
            assert all(pkg["name"] != "winonly" for pkg in data["packages"])


def test_json_build_info_matches_schema(capsys, tmp_path, fake_pypi, fake_files):
    import io
    import tarfile

    pyproject = b'[build-system]\nrequires = ["maturin>=1"]\n'
    with tarfile.open(tmp_path / "s.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("nowheels-0.9/pyproject.toml")
        info.size = len(pyproject)
        tar.addfile(info, io.BytesIO(pyproject))
    fake_files.files["nowheels-0.9.tar.gz"] = (tmp_path / "s.tar.gz").read_bytes()
    args = ["nowheels==0.9", "fastlib==2.0.0", "-p", "3.13", "-t", "linux-x86_64", "--all", "--json", "--explain"]
    code, out, _ = run(capsys, *args, "--build-info")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert {p["name"]: p["build_requires"] for p in data["packages"]} == {"fastlib": None, "nowheels": ["maturin>=1"]}


def test_json_build_lab_matches_schema(capsys, tmp_path, tracker):
    """--json --explain --notes: the tracker's build lab results, by target label, with each verdict."""
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.13"\n\n[[package]]\nname = "pkg"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hash = "sha256:00", size = 1 }\n',
        encoding="utf-8",
    )
    common = {"version": "1.0", "runner": "windows-11-arm", "date": "2026-11-15", "toolchains": ["cl"]}
    built = {"outcome": "built", "seconds": 87, "excerpt": "", "reason": None}
    regular = {
        **common,
        **built,
        "target": "3.14 win-arm64",
        "python": "3.14.2",
        "wheel": "py3-none-any",
        "extensions": 0,
        "import": "ok",
        "gil": None,
    }
    free = {
        **common,
        **built,
        "target": "3.14t win-arm64",
        "python": "3.14.2t",
        "wheel": "cp314-cp314t-win_arm64",
        "extensions": 2,
        "import": "ok",
        "gil": "re-enabled",
    }
    failed = {
        **common,
        "runner": "ubuntu-24.04",
        "target": "3.14 linux-x86_64",
        "python": "3.14.2",
        "outcome": "failed",
        "seconds": 12,
        "wheel": None,
        "extensions": None,
        "import": None,
        "gil": None,
        "reason": "needs OpenSSL",
        "excerpt": "fatal error: openssl/ssl.h: No such file or directory",
    }
    tracker.files["pkg"] = {
        "schema": 1,
        "name": "pkg",
        "version": "1.0",
        "notes": [],
        "builds": [regular, free, failed],
    }
    args = [str(tmp_path), "-p", "3.14,3.14t", "-t", "win-arm64,linux-x86_64", "--json", "--explain", "--notes"]
    code, out, err = run(capsys, *args)
    assert code == 1 and not err
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert data["packages"][0]["build_lab"] == {
        "3.14 win-arm64": {
            **regular,
            "verdict": (
                "builds in 87 s as pure Python, with no compiled extension module "
                "(if it has an optional one, that was skipped)"
            ),
        },
        "3.14t win-arm64": {**free, "verdict": "builds in 87 s, but importing it turns the GIL back on"},
        "3.14 linux-x86_64": {**failed, "verdict": "fails to build: needs OpenSSL"},
    }
    # The schema rejects a result in another shape.
    schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    entry = data["packages"][0]["build_lab"]["3.14 win-arm64"]
    lab = schema["$defs"]["package"]["properties"]["build_lab"]
    for bad in ({**entry, "outcome": "maybe"}, {**entry, "seconds": -1}, {**entry, "extra": 1}):
        with pytest.raises(AssertionError):
            _check_schema({"3.14 win-arm64": bad}, lab, schema)


def test_json_output_with_pyodide_lock_matches_schema(capsys, fixtures, fake_pypi):
    lock = str(fixtures / "pyodide-lock.json")
    code, out, _ = run(
        capsys, "fastlib==2.0.0", "-p", "3.13", "-t", "pyodide,musl-aarch64", "--pyodide-lock", lock, "--json"
    )
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert data["packages"][0]["bundled"] == "2.0.0"


def test_output_schema_rejects_other_shapes():
    schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    package = {"name": "x", "version": None, "error": None, "results": {"linux-x86_64": "wheel"}}
    good = {
        "version": "1",
        "sources": [],
        "targets": [],
        "packages": [package],
        "skipped": [],
        "ignored": [],
        "ignored_targets": [{"name": "x", "platforms": ["linux-x86_64"]}],
        "summary": {"packages": 1, "with_gaps": 0, "per_target": {}},
    }
    _check_schema(good, schema, schema)
    # Released versions (0.8.0 and older) have no per_target or ignored_targets, and the schema on main must still
    # accept their output.
    _check_schema(good | {"summary": {"packages": 1, "with_gaps": 0}}, schema, schema)
    _check_schema({k: v for k, v in good.items() if k != "ignored_targets"}, schema, schema)
    _check_schema(
        good | {"summary": good["summary"] | {"per_target": {"win-arm64": {"source": 1, "missing": 0, "unknown": 0}}}},
        schema,
        schema,
    )
    for bad in (
        {k: v for k, v in good.items() if k != "summary"},  # a required key is missing
        good | {"extra": 1},  # an unknown key
        good | {"packages": [package | {"results": {"linux-x86_64": "maybe"}}]},  # not a Status
        good | {"packages": [package | {"bundled": {}}]},  # the bundled version is a string
        good | {"summary": {"packages": True, "with_gaps": 0, "per_target": {}}},  # not an integer
        good | {"summary": good["summary"] | {"per_target": {"win-arm64": {"source": 1}}}},  # every count is there
        good | {"ignored_targets": [{"name": "x", "platforms": "linux-x86_64"}]},  # platforms is a list
        good | {"ignored": [{"name": "x", "platforms": ["linux-x86_64"]}]},  # ignored stays a list of names
    ):
        with pytest.raises(AssertionError):
            _check_schema(bad, schema, schema)


def test_requirements_note_names_only_unpinned_packages(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("ranged\nfastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert "requirements.txt has 1 unpinned package (ranged)" in err
    assert "fastlib" not in err.split("unpinned package", 1)[1]
    assert "newest matching release was checked" in err
    assert "uv pip compile" in err
    # Notes stay on stderr, including under --json.
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 0
    assert "unpinned package (ranged)" in err
    assert "unpinned" not in out


def test_requirements_note_omitted_for_a_pinned_file(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert "unpinned" not in err


def test_requirements_note_omitted_for_command_line_names(capsys, fake_pypi):
    code, out, err = run(capsys, "ranged", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert "unpinned" not in err


def test_requirements_note_hidden_with_quiet(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("ranged\nfastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert code == 0
    assert "unpinned" not in err


def test_requirements_note_follows_constraints_includes_and_errors(capsys, fake_pypi, tmp_path):
    # ranged is pinned by a constraint (not unpinned); pythonranged appears in two files (named once);
    # fastlib's constraint has a marker, so it's unknown, not "the newest release was checked".
    (tmp_path / "constraints.txt").write_text('ranged==1.9\nfastlib<3; sys_platform == "win32"\n', encoding="utf-8")
    (tmp_path / "dev.txt").write_text("pythonranged>=1\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("-c constraints.txt\n-r dev.txt\nranged\npythonranged\nfastlib\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert "requirements.txt has 1 unpinned package (pythonranged)" in err


def test_requirements_note_hidden_with_quiet_config(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("ranged\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert "unpinned" not in err


def test_version_includes_packaging(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    out, err = capsys.readouterr()
    free_threaded = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
    python = f"{platform.python_implementation()} {platform.python_version()}{free_threaded}"
    assert out == f"wheelgap {wheelgap.__version__} (packaging {packaging.__version__}, {python})\n"
    assert not err


# ranged: 1.5 wheel, 1.9 sdist only, 2.0 wheel. nowheels has only an sdist.
LIB_PYPROJECT = """\
[project]
name = "mylib"
dependencies = ["ranged<2", "fastlib==2.0.0", "winonly; sys_platform == 'win32'"]

[project.optional-dependencies]
test = ["nowheels"]
"""


def test_pyproject_checks_direct_dependencies(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(LIB_PYPROJECT, encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1  # ranged<2 picks 1.9, which has only an sdist
    rows = {row["name"]: row for row in json.loads(out)["packages"]}
    assert set(rows) == {"ranged", "fastlib"}  # winonly's marker doesn't hold on Linux
    assert rows["ranged"]["version"] == "1.9"
    assert rows["ranged"]["results"] == {"linux-x86_64": "source"}
    assert "pyproject.toml lists direct dependencies only; their own dependencies aren't checked" in err
    assert "use a lockfile for that" in err
    assert "ranges were checked at the newest matching release" in err
    assert "direct dependencies" not in out  # the note stays out of --json
    # The note is printed even with --quiet.
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "-q")
    assert "lists direct dependencies only" in err


def _write_requires_python_pyproject(tmp_path, requires_python):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[project]\nname = "mylib"\ndependencies = []\nrequires-python = "{requires_python}"\n',
        encoding="utf-8",
    )
    return path


def test_pyproject_requires_python_note_names_unsupported_pythons(capsys, fake_pypi, tmp_path):
    path = _write_requires_python_pyproject(tmp_path, ">=3.11")
    code, out, err = run(capsys, str(path), "-p", "3.10", "-t", "linux-x86_64", "--json")
    assert code == 0
    assert "requires Python >=3.11" in err
    assert "leaves out Python 3.10" in err
    assert "is locked for Python" not in err  # the uv.lock wording doesn't apply
    assert "requires Python" not in out  # notes stay out of --json


def test_pyproject_requires_python_note_absent_when_supported(capsys, fake_pypi, tmp_path):
    path = _write_requires_python_pyproject(tmp_path, ">=3.11")
    code, _, err = run(capsys, str(path), "-p", "3.12", "-t", "linux-x86_64")
    assert code == 0
    assert "leaves out Python" not in err


@pytest.mark.parametrize(
    ("requires_python", "pythons", "left_out"),
    [
        ("!=3.12.*", ["3.12", "3.13"], "3.12"),  # pip applies every clause, unlike uv.lock's bounds-only rule
        (">=3.11,!=3.12.*", ["3.10", "3.12", "3.13"], "3.10, 3.12"),
        ("!=3.12.0", ["3.12"], None),  # 3.12.1 and later are fine
        ("<3.13", ["3.12", "3.13", "3.14t"], "3.13, 3.14t"),
        (">=3.11", ["3.14t"], None),
    ],
)
def test_pyproject_requires_python_clauses(capsys, fake_pypi, tmp_path, requires_python, pythons, left_out):
    path = _write_requires_python_pyproject(tmp_path, requires_python)
    args = [arg for p in pythons for arg in ("-p", p)]
    code, _, err = run(capsys, str(path), *args, "-t", "linux-x86_64")
    assert code == 0
    if left_out is None:
        assert "leaves out Python" not in err
    else:
        assert f"leaves out Python {left_out};" in err


def test_pyproject_requires_python_notes_respect_quiet(capsys, fake_pypi, tmp_path):
    for spec in (">=3.11", "3.11"):  # left out, and invalid
        path = _write_requires_python_pyproject(tmp_path, spec)
        _, _, err = run(capsys, str(path), "-p", "3.10", "-t", "linux-x86_64", "-q")
        assert "requires-python" not in err and "requires Python" not in err


def test_pyproject_invalid_requires_python_is_ignored_with_a_note(capsys, fake_pypi, tmp_path):
    path = _write_requires_python_pyproject(tmp_path, "3.11")
    code, _, err = run(capsys, str(path), "-p", "3.12", "-t", "linux-x86_64")
    assert code == 0
    assert "ignoring requires-python '3.11'" in err
    assert "is locked for Python" not in err


@pytest.mark.parametrize("flags", [["--extra", "test"], ["--all-extras"]])
def test_pyproject_extra_adds_optional_dependencies(capsys, fake_pypi, tmp_path, flags):
    path = tmp_path / "pyproject.toml"
    path.write_text(LIB_PYPROJECT, encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json", *flags)
    assert code == 1
    rows = {row["name"]: row for row in json.loads(out)["packages"]}
    assert rows["nowheels"]["results"] == {"linux-x86_64": "source"}


@pytest.mark.parametrize(
    "text,flags,error",
    [
        (LIB_PYPROJECT, ["--extra", "docs"], "unknown extras: docs"),
        (LIB_PYPROJECT, ["--group", "dev"], "unknown groups: dev"),
        (LIB_PYPROJECT, ["--group", "zeta", "--only-group", "alpha"], "unknown groups: alpha, zeta"),
        (LIB_PYPROJECT, ["--no-dev"], "--no-dev doesn't apply to pyproject.toml"),
        ('[project]\nname = "x"\ndynamic = ["dependencies"]\n', [], "dependencies is dynamic"),
        (
            '[project]\nname = "x"\ndependencies = []\ndynamic = ["optional-dependencies"]\n',
            ["--all-extras"],
            "optional-dependencies is dynamic",
        ),
    ],
)
def test_pyproject_input_errors_exit_2(capsys, fake_pypi, tmp_path, text, flags, error):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", *flags)
    assert code == 2
    assert error in err
    assert fake_pypi == []  # nothing was looked up


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], {"ranged", "fastlib"}),
        (["--group", "test"], {"ranged", "fastlib", "nowheels"}),
        (["--only-group", "test"], {"nowheels"}),
        (["--only-group", "test", "--group", "docs"], {"nowheels", "purewheel"}),
        (["--group", "QA_Group"], {"ranged", "fastlib", "nowheels", "purewheel"}),
        # A requirement on the project itself installs the project: its dependencies, plus any extra it names.
        (["--only-group", "self"], {"ranged", "fastlib", "nowheels"}),
        (["--only-group", "bare-self"], {"ranged", "fastlib"}),
        (["--only-group", "via-alias"], {"nowheels", "purewheel"}),  # a non-normalized include-group name
    ],
)
def test_pyproject_dependency_groups(capsys, fake_pypi, tmp_path, flags, expected):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        LIB_PYPROJECT + "\n[dependency-groups]\n"
        'test = ["nowheels", "nowheels"]\ndocs = ["purewheel==3.1"]\n'
        'qa-group = [{include-group = "test"}, {include-group = "docs"}]\n'
        'self = ["MyLib[test]"]\nbare-self = ["mylib"]\nvia-alias = [{include-group = "QA_Group"}]\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json", *flags)
    assert code == 1
    rows = json.loads(out)["packages"]
    assert {row["name"] for row in rows} == expected
    assert len(rows) == len(expected)
    assert not any("mylib" in url.lower() for url in fake_pypi)


GROUPS_ONLY_PYPROJECT = """\
[dependency-groups]
test = ["purewheel==3.1", {include-group = "shared"}]
shared = ["fastlib==2.0.0"]
"""


@pytest.mark.parametrize("flag", ["--group", "--only-group"])
def test_pyproject_groups_without_project(capsys, fake_pypi, tmp_path, flag):
    path = tmp_path / "pyproject.toml"
    path.write_text(GROUPS_ONLY_PYPROJECT, encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json", flag, "test")
    assert code == 0, err
    assert {row["name"] for row in json.loads(out)["packages"]} == {"purewheel", "fastlib"}


@pytest.mark.parametrize(
    "text,flags,error",
    [
        (GROUPS_ONLY_PYPROJECT, [], "pick one with --group or --only-group: shared, test"),
        ("[dependency-groups]\n", [], "no groups to select"),
        (GROUPS_ONLY_PYPROJECT, ["--extra", "test"], "no [project] optional-dependencies"),
        (GROUPS_ONLY_PYPROJECT, ["--all-extras"], "no [project] optional-dependencies"),
        (GROUPS_ONLY_PYPROJECT, ["--only-group", "test", "--all-extras"], "no [project] optional-dependencies"),
        ("[tool.wheelgap]\n", [], "no [project] table"),
        ('project = "invalid"\n' + GROUPS_ONLY_PYPROJECT, ["--group", "test"], "no [project] table"),
        (
            '[tool.poetry.dependencies]\npython = "^3.10"\nnumpy = "*"\n' + GROUPS_ONLY_PYPROJECT,
            ["--group", "test"],
            "[tool.poetry.dependencies]",
        ),
    ],
)
def test_pyproject_groups_without_project_errors(capsys, fake_pypi, tmp_path, text, flags, error):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", *flags)
    assert code == 2 and error in err
    assert not fake_pypi


def test_pyproject_groups_error_lists_only_valid_group_names(capsys, fake_pypi, tmp_path):
    # The group names are text from the file: a line break and an escape code in a quoted key stay out of the error.
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[dependency-groups]\ntest = ["purewheel==3.1"]\n"a\\nrun this instead" = []\n"\\u001b[2J" = []\n',
        encoding="utf-8",
    )
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and "pick one with --group or --only-group: test" in err
    assert "run this instead" not in err and err.rstrip("\n").isprintable()
    assert not fake_pypi


def test_pyproject_groups_error_without_a_valid_group_name(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[dependency-groups]\n"\\u001b[2J" = []\n', encoding="utf-8")
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and "no [project] table and no groups to select" in err
    assert "\x1b" not in err and err.rstrip("\n").isprintable()
    assert not fake_pypi


@pytest.mark.parametrize("flag", ["--group", "--only-group"])
@pytest.mark.parametrize(
    "text,beside",
    [
        (GROUPS_ONLY_PYPROJECT, "setup.py"),
        (GROUPS_ONLY_PYPROJECT, "setup.cfg"),
        (
            '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
            + GROUPS_ONLY_PYPROJECT,
            None,
        ),
        ('[tool.flit.metadata]\nmodule = "mypkg"\nrequires = ["numpy"]\n' + GROUPS_ONLY_PYPROJECT, None),
        ('[tool.pdm]\ndependencies = ["numpy"]\n' + GROUPS_ONLY_PYPROJECT, None),
    ],
    ids=["setup-py", "setup-cfg", "build-system", "flit-metadata", "pdm-dependencies"],
)
def test_pyproject_groups_without_project_in_a_buildable_project_errors(
    capsys, fake_pypi, tmp_path, text, beside, flag
):
    # The project's own dependencies live outside [project], so checking only the group would be a wrong answer.
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    if beside:
        (tmp_path / beside).write_text("", encoding="utf-8")
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", flag, "test")
    assert code == 2 and "no [project] table" in err and "setup.py/setup.cfg" in err
    assert not fake_pypi


@pytest.mark.parametrize(
    "groups,flags,error",
    [
        ('test = [{include-group = "other"}]\nother = [{include-group = "test"}]', [], "cycle"),
        ('test = [{include-group = "absent"}]', [], "dependency group 'test' includes unknown group 'absent'"),
        ('test = [{include-group = "test"}]', [], "cycle"),
        ('test = ["-e ."]', [], "can't parse requirement '-e .'"),
        ("test = [42]", [], "entry"),
        ("test = [{include-group = 42}]", [], "entry"),
        ('test = [{include-group = "other", unexpected = true}]', [], "entry"),
        ('test = "nowheels"', [], "list"),
        ("test = []\nTest = []", [], "once normalized"),
        ("test = []", ["--only-group", "test", "--extra", "test"], "cannot be combined"),
    ],
)
def test_pyproject_dependency_group_errors(capsys, fake_pypi, tmp_path, groups, flags, error):
    path = tmp_path / "pyproject.toml"
    path.write_text(LIB_PYPROJECT + "\n[dependency-groups]\n" + groups, encoding="utf-8")
    code, _, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--group", "test", *flags)
    assert code == 2 and error in err
    assert not fake_pypi


def test_pyproject_dependency_groups_are_validated_only_when_selected(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        LIB_PYPROJECT + "\n[dependency-groups]\n"
        'test = ["nowheels"]\nbroken = [42]\nnotalist = "x"\nloop = [{include-group = "loop"}]\n'
        'missing = [{include-group = "absent"}]\nunparsable = ["-e ."]\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json", "--group", "test")
    assert code == 1, err
    assert {row["name"] for row in json.loads(out)["packages"]} == {"ranged", "fastlib", "nowheels"}


def test_pyproject_dependency_group_marker_is_checked_per_target(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "mylib"\ndependencies = []\n[dependency-groups]\n'
        'win = ["winonly; sys_platform == \'win32\'", "purewheel==3.1"]\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json", "--group", "win")
    assert code == 0
    rows = {row["name"]: row for row in json.loads(out)["packages"]}
    assert rows["winonly"]["results"] == {"linux-x86_64": "n/a", "win-amd64": "wheel"}
    assert rows["purewheel"]["results"] == {"linux-x86_64": "wheel", "win-amd64": "wheel"}


def test_pyproject_and_uv_lock_take_the_same_group(capsys, fake_pypi, fixtures, tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "locked").mkdir()
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path / "locked")
    path = tmp_path / "lib" / "pyproject.toml"
    path.write_text(
        LIB_PYPROJECT + '\n[dependency-groups]\ndocs = ["purewheel==3.1"]\ntest = ["nowheels"]\n', encoding="utf-8"
    )
    lock = tmp_path / "locked" / "uv.lock"
    code, out, err = run(capsys, str(path), str(lock), "-p", "3.13", "-t", "linux-x86_64", "--json", "--group", "docs")
    assert code == 1, err
    names = {row["name"] for row in json.loads(out)["packages"]}
    assert {"ranged", "fastlib", "purewheel", "common", "docstool"} <= names
    assert not names & {"nowheels", "gpu"}  # neither the pyproject.toml's test group nor the lock's gpu extra


@pytest.mark.usefixtures("index")
@pytest.mark.parametrize("project", ['[project]\nname = "mylib"\n', ""], ids=["with-project", "groups-only"])
def test_pyproject_dependency_group_resolves_tree(capsys, tmp_path, project):
    path = tmp_path / "pyproject.toml"
    path.write_text(project + '[dependency-groups]\ntest = ["app"]\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "--group", "test", "--deps", "-p", "3.14", "-t", "win-arm64", "--json")
    assert code == 1, err
    assert {row["name"] for row in json.loads(out)["packages"]} == {"app", "lib", "crypt", "winonly"}


def test_pyproject_self_reference_in_dependencies_is_not_looked_up(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(LIB_PYPROJECT.replace('"fastlib==2.0.0", ', '"fastlib==2.0.0", "MyLib[test]", '), encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1
    rows = {row["name"]: row for row in json.loads(out)["packages"]}
    assert set(rows) == {"ranged", "fastlib", "nowheels"}  # MyLib[test] adds the test extra
    assert not any("mylib" in url.lower() for url in fake_pypi)


def test_directory_without_lockfile_checks_pyproject(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text(LIB_PYPROJECT, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1  # as `wheelgap DIR/pyproject.toml` (tests/test_directory.py has the rest)
    assert {row["name"] for row in json.loads(out)["packages"]} == {"ranged", "fastlib"}
    assert f"so {tmp_path / 'pyproject.toml'} was checked" in err


def test_directory_with_groups_only_pyproject_suggests_group(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text(GROUPS_ONLY_PYPROJECT, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "no lockfile found" in err
    assert f"wheelgap {tmp_path / 'pyproject.toml'} --group NAME` (groups: shared, test)." in err
    assert not fake_pypi


def test_directory_with_groups_only_pyproject_in_a_buildable_project_has_no_group_hint(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text(GROUPS_ONLY_PYPROJECT, encoding="utf-8")
    (tmp_path / "setup.py").write_text("", encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "no lockfile found" in err and "--group" not in err


# --github-annotations


def annotation_lines(text):
    return [line for line in text.splitlines() if line.startswith("::")]


def test_github_annotations_uv_lock(capsys, fake_pypi, fixtures, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    code, out, _ = run(
        capsys, str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,win-arm64", "--github-annotations"
    )
    assert code == 1
    lines = out.splitlines()
    assert lines[:4] == [  # before the normal output
        "::error file=uvproj/uv.lock,line=23,title=fastlib 2.0.0%3A builds from source::"
        "fastlib 2.0.0 builds from source on win-arm64.",
        "::error file=uvproj/uv.lock,line=37,title=nativeext 0.3%3A builds from source::"
        "nativeext 0.3 builds from source on linux-x86_64, win-arm64.",
        "::error file=uvproj/uv.lock,line=43,title=puresdist 1.0%3A builds from source::"
        "puresdist 1.0 builds from source on linux-x86_64, win-arm64.",
        "::error file=uvproj/uv.lock,line=58,title=winonly 311%3A can't install::"
        "winonly 311 can't install on win-arm64.",
    ]
    assert lines[4].startswith("wheelgap ")
    assert len(annotation_lines(out)) == 4
    # Without the flag, nothing changes.
    _, plain, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,win-arm64")
    assert plain.splitlines() == lines[4:]


def test_github_annotations_requirements(capsys, fake_pypi, tmp_path, monkeypatch):
    (tmp_path / "more").mkdir()
    (tmp_path / "requirements.txt").write_text(
        "# pinned, with hashes on the next line\n"
        "fastlib==2.0.0 \\\n"
        "    --hash=sha256:00\n"
        "-r more/extra.txt\n"
        "nowheels==0.9 ; sys_platform == 'linux'\n"
        "nowheels==0.9 ; sys_platform == 'win32'\n"
        "ranged>=1.9,<2  # 1.9 has only an sdist\n",
        encoding="utf-8",
    )
    (tmp_path / "more" / "extra.txt").write_text(
        "--index-url https://pypi.org/simple\nwinonly==311\nranged==1.9\n", encoding="utf-8"
    )
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--github-annotations"
    )
    assert code == 1
    # The first line of a continued requirement; the same name and markers on two lines (ranged) get no line.
    assert annotation_lines(out) == [
        "::error file=requirements.txt,line=2,title=fastlib 2.0.0%3A builds from source::"
        "fastlib 2.0.0 builds from source on win-amd64.",
        "::error file=requirements.txt,line=5,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on linux-x86_64.",
        "::error file=requirements.txt,line=6,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on win-amd64.",
        # requirements.txt line 7 and more/extra.txt line 3 are one package, as pip installs one ranged 1.9 for both
        "::error file=requirements.txt,title=ranged 1.9%3A builds from source::"
        "ranged 1.9 builds from source on linux-x86_64, win-amd64.",
        "::error file=more/extra.txt,line=2,title=winonly 311%3A can't install::"
        "winonly 311 can't install on linux-x86_64.",
    ]


def test_github_annotations_symlinked_requirements_file(capsys, fake_pypi, tmp_path, monkeypatch):
    # The requirements file is a link into real/, and both folders have a base.txt: the annotation for the
    # package pip reads (nowheels, from link/base.txt) is on link/base.txt, not real/base.txt (#604). The linked
    # file's own line is on the file the link points to: git keeps link/reqs.txt as a link, so it has no line 2.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    (tmp_path / "real" / "reqs.txt").write_text("-r base.txt\nranged==1.9\n", encoding="utf-8")
    (tmp_path / "real" / "base.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    (tmp_path / "link" / "base.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    try:
        # A Path, so that Windows gets backslashes: a link written with forward slashes can't be opened there.
        (tmp_path / "link" / "reqs.txt").symlink_to(Path("../real/reqs.txt"))
    except OSError:
        pytest.skip("symlinks aren't available here")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys, str(tmp_path / "link" / "reqs.txt"), "-p", "3.13", "-t", "linux-x86_64", "--github-annotations"
    )
    assert code == 1
    assert annotation_lines(out) == [
        "::error file=link/base.txt,line=1,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on linux-x86_64.",
        "::error file=real/reqs.txt,line=2,title=ranged 1.9%3A builds from source::"
        "ranged 1.9 builds from source on linux-x86_64.",
    ]


def test_github_annotations_deps_keeps_a_direct_requirements_line(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    # --deps checks the resolved tree, but a direct requirement's gap is still on its own line (#432).
    # app pulls in more than the file names (and crypt again, through lib[crypto]): crypt is still its own line's.
    (tmp_path / "requirements.txt").write_text("crypt==1.0\napp==1.0\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys,
        str(tmp_path / "requirements.txt"),
        "--deps",
        "-p",
        "3.14",
        "-t",
        "win-arm64",
        "--github-annotations",
    )
    assert code == 1
    assert (
        "::error file=requirements.txt,line=1,title=crypt 1.0%3A builds from source::"
        "crypt 1.0 builds from source on win-arm64."
    ) in annotation_lines(out)


def test_github_annotations_deps_a_marked_requirement_keeps_its_line(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "requirements.txt").write_text('crypt==1.0; sys_platform == "win32"\napp==1.0\n', encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _, out, _ = run(
        capsys, str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--github-annotations"
    )
    (line,) = [ln for ln in annotation_lines(out) if "title=crypt" in ln]
    assert line.startswith("::error file=requirements.txt,line=1,")


def test_github_annotations_deps_direct_on_one_python_pulled_in_on_another(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    # crypt is the file's own requirement on 3.14 but comes through parenta on 3.13: neither line is right for
    # both, so the annotation names the file only, and says the via applies to some targets.
    (tmp_path / "requirements.txt").write_text('crypt==1.0; python_version >= "3.14"\nparenta==1.0\n', encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _, out, _ = run(
        capsys,
        str(tmp_path / "requirements.txt"),
        "--deps",
        "-p",
        "3.13",
        "-p",
        "3.14",
        "-t",
        "win-arm64",
        "--github-annotations",
    )
    (line,) = [ln for ln in annotation_lines(out) if "title=crypt" in ln]
    assert line.startswith("::error file=requirements.txt,title=crypt 1.0%3A builds from source::")
    assert "(via parenta on some targets)." in line


@pytest.mark.parametrize(
    "text",
    [
        # crypt's own line is Linux-only: on Windows parenta and parentb pull it in, so that line is the wrong one.
        'crypt==1.0; sys_platform == "linux"\nparenta==1.0\nparentb==1.0\n',
        # One puller, but on two lines: neither is certain, and crypt's Linux-only line is still the wrong one.
        'crypt==1.0; sys_platform == "linux"\n'
        'parenta==1.0; python_version >= "3"\nparenta==1.0; python_version < "3"\n',
    ],
)
def test_github_annotations_deps_never_uses_a_line_that_isnt_where_it_comes_from(
    index,  # noqa: F811  (the index fixture)
    capsys,
    tmp_path,
    monkeypatch,
    text,
):
    (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _, out, _ = run(
        capsys, str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--github-annotations"
    )
    (line,) = [ln for ln in annotation_lines(out) if "title=crypt" in ln]
    assert line.startswith("::error file=requirements.txt,title=crypt 1.0%3A builds from source::")


def test_github_annotations_deps_a_transitive_gap_names_its_requirement(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    # crypt is pulled in by parenta alone: the annotation sits on parenta's line and says via whom (#432).
    (tmp_path / "requirements.txt").write_text("parenta==1.0\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys,
        str(tmp_path / "requirements.txt"),
        "--deps",
        "-p",
        "3.14",
        "-t",
        "win-arm64",
        "--github-annotations",
    )
    assert code == 1
    assert annotation_lines(out) == [
        "::error file=requirements.txt,line=1,title=crypt 1.0%3A builds from source::"
        "crypt 1.0 builds from source on win-arm64 (via parenta)."
    ]


def test_github_annotations_deps_two_pullers_stay_file_only(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    # parenta and parentb both pull crypt in: no line is safe, so the file alone carries both names (#432).
    (tmp_path / "requirements.txt").write_text("parenta==1.0\nparentb==1.0\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys,
        str(tmp_path / "requirements.txt"),
        "--deps",
        "-p",
        "3.14",
        "-t",
        "win-arm64",
        "--github-annotations",
    )
    assert code == 1
    (line,) = annotation_lines(out)
    assert line.startswith("::error file=requirements.txt,title=crypt 1.0%3A builds from source::")
    assert "builds from source on win-arm64 (via parenta, parentb)." in line


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("pdmproj", [("fastlib", 9)]),
        ("poetryproj", [("fastlib", 4)]),
        ("pylockproj", [("fastlib", 5)]),
        ("pipenvproj", [("fastlib", 8)]),  # the package's key in "default"
    ],
)
def test_github_annotations_lockfile_lines(capsys, fake_pypi, fixtures, monkeypatch, folder, expected):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures / folder))
    code, out, _ = run(capsys, str(fixtures / folder), "-p", "3.13", "-t", "win-amd64", "--github-annotations")
    assert code == 1
    found = []
    for line in annotation_lines(out):
        props = dict(item.split("=", 1) for item in line.split("::")[1].removeprefix("error ").split(","))
        found.append((props["title"].split()[0], int(props["line"])))
        assert props["file"] in ("pdm.lock", "poetry.lock", "pylock.toml", "Pipfile.lock")
    assert found == expected


def uv_lock(packages: list[tuple[str, str]], extra: str = "") -> str:
    """A uv.lock whose project depends on each (name, version), all with only an sdist."""
    deps = "".join(f'    {{ name = "{name}" }},\n' for name, _ in packages)
    text = (
        'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
        f'source = {{ editable = "." }}\n{extra}dependencies = [\n{deps}]\n'
    )
    for name, version in packages:
        text += (
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            f'sdist = {{ url = "https://files.example/{name}-{version}.tar.gz", hash = "sha256:00" }}\n'
        )
    return text


@pytest.mark.parametrize("count", [10, 11, 12])
@pytest.mark.parametrize("fail_on,level", [("source", "error"), ("missing", "warning")])
def test_github_annotations_cap(capsys, fake_pypi, tmp_path, monkeypatch, fail_on, level, count):
    # GitHub shows 10 errors and 10 warnings per step: 10 packages fit, and past that the 10th says how many more.
    names = [f"pkg{i:02}" for i in range(count)]
    (tmp_path / "uv.lock").write_text(uv_lock([(name, "1.0") for name in names]), encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--github-annotations", "--fail-on", fail_on
    )
    assert code == (1 if fail_on == "source" else 0)
    lines = annotation_lines(out)
    assert len(lines) == 10
    shown = 10 if count == 10 else 9
    assert all(line.startswith(f"::{level} file=uv.lock,line=") for line in lines[:shown])
    assert [line.split("title=")[1].split("%3A")[0] for line in lines[:shown]] == [f"{n} 1.0" for n in names[:shown]]
    if count > 10:
        more = count - 9
        assert lines[9] == (
            f"::{level} title=wheelgap%3A {more} more packages with gaps::"
            f"{more} more packages with gaps; the log lists them all."
        )
    if level == "warning":
        assert lines[0].endswith("Source builds are allowed (fail-on missing), so this doesn't fail the run.")


def test_github_annotations_fail_on_missing_mixes_levels(capsys, fake_pypi, fixtures, monkeypatch):
    # A package that can't install still fails the run, so it stays an error; source builds become warnings.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    code, out, _ = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-arm64",
        "--github-annotations",
        "--fail-on",
        "missing",
    )
    assert code == 1
    levels = [(line.split()[0], line.split("title=")[1].split()[0]) for line in annotation_lines(out)]
    assert levels == [
        ("::error", "winonly"),
        ("::warning", "fastlib"),
        ("::warning", "nativeext"),
        ("::warning", "puresdist"),
    ]


@pytest.fixture
def dockerfile_provenance_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    Path("Dockerfile").write_text(
        "FROM --platform=linux/amd64 python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt second.txt ./\n"
        "RUN pip install -r requirements.txt\n"
        "RUN pip install -r second.txt\n",
        encoding="utf-8",
    )
    Path("requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    Path("second.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")


@pytest.mark.parametrize("flags", [(), ("--markdown",)])
def test_dockerfile_input_provenance_in_report_headers(capsys, fake_pypi, dockerfile_provenance_inputs, flags):
    code, out, err = run(capsys, "--dockerfile", "Dockerfile", "--dockerfile-inputs", *flags)
    assert code == 1
    assert "requirements.txt (Dockerfile:4)" in out
    assert "second.txt (Dockerfile:5)" in out
    assert "Dockerfile:4: RUN pip install -r reads requirements.txt" in err
    assert "Dockerfile:5: RUN pip install -r reads second.txt" in err


def test_dockerfile_input_provenance_in_json_and_annotations(capsys, fake_pypi, dockerfile_provenance_inputs):
    code, out, err = run(capsys, "--dockerfile", "Dockerfile", "--dockerfile-inputs", "--json", "--github-annotations")
    assert code == 1
    report = json.loads(out)
    _assert_matches_output_schema(report)
    assert report["sources"] == ["requirements.txt", "second.txt"]
    assert report["dockerfile_inputs"] == [
        {"dockerfile": "Dockerfile", "line": 4, "file": "requirements.txt", "constraints": [], "includes": []},
        {"dockerfile": "Dockerfile", "line": 5, "file": "second.txt", "constraints": [], "includes": []},
    ]
    assert "::error file=requirements.txt,line=1," in err
    assert "nowheels 0.9 builds from source on linux-x86_64 (installed by Dockerfile:4)." in err
    assert "::error file=second.txt,line=1," in err
    assert "psycopg2 2.9.11 builds from source on linux-x86_64 (installed by Dockerfile:5)." in err


def test_dockerfile_input_provenance_names_the_line_a_continued_run_starts_on(capsys, fake_pypi, tmp_path, monkeypatch):
    # Docker numbers an instruction by the line it starts on; the -r here is three lines further down, and the
    # Dockerfile sits in a folder of its own, with the build context given.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    Path("docker").mkdir()
    Path("docker/Dockerfile").write_text(
        "FROM --platform=linux/amd64 python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt ./\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y gcc\n"
        "RUN pip install \\\n"
        "    # the pinned set\n"
        "    --no-cache-dir \\\n"
        "    -r requirements.txt\n",
        encoding="utf-8",
    )
    Path("requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    args = ["--dockerfile", "docker/Dockerfile", "--dockerfile-inputs", "--dockerfile-context", "."]
    code, out, err = run(capsys, *args)
    assert code == 1 and " · requirements.txt (docker/Dockerfile:6) · 1 package · Python 3.13\n" in out
    assert "docker/Dockerfile:6: RUN pip install -r reads requirements.txt" in err
    code, out, err = run(capsys, *args, "--json", "--github-annotations")
    report = json.loads(out)
    _assert_matches_output_schema(report)
    assert report["sources"] == ["requirements.txt"]
    assert report["dockerfile_inputs"] == [
        {"dockerfile": "docker/Dockerfile", "line": 6, "file": "requirements.txt", "constraints": [], "includes": []}
    ]
    assert "::error file=requirements.txt,line=1," in err
    assert "nowheels 0.9 builds from source on linux-x86_64 (installed by docker/Dockerfile:6)." in err


def test_dockerfile_inputs_file_is_relative_to_the_build_context(capsys, fake_pypi, tmp_path, monkeypatch):
    # The Dockerfile's folder is the build context, given or not, and absolute or not: `file` is the same.
    monkeypatch.chdir(tmp_path)
    Path("docker/reqs").mkdir(parents=True)
    Path("docker/Dockerfile").write_text(
        "FROM --platform=linux/amd64 python:3.13-slim\nWORKDIR /app\nCOPY reqs/base.txt ./\n"
        "RUN pip install -r base.txt\n",
        encoding="utf-8",
    )
    Path("docker/reqs/base.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    for context in ([], ["--dockerfile-context", str(tmp_path / "docker")]):
        _, out, _ = run(capsys, "--dockerfile", "docker/Dockerfile", "--dockerfile-inputs", "--json", *context)
        report = json.loads(out)
        _assert_matches_output_schema(report)
        assert report["dockerfile_inputs"] == [
            {"dockerfile": "docker/Dockerfile", "line": 4, "file": "reqs/base.txt", "constraints": [], "includes": []}
        ]


def test_no_dockerfile_inputs_key_without_the_flag(capsys, fake_pypi, dockerfile_provenance_inputs):
    _, out, err = run(capsys, "requirements.txt", "--dockerfile", "Dockerfile", "--json", "--github-annotations")
    assert "dockerfile_inputs" not in json.loads(out) and "installed by" not in err


@pytest.mark.parametrize("flags", [(), ("--markdown",), ("--json",), ("--github-annotations",)])
def test_plain_dockerfile_run_has_no_install_provenance(capsys, fake_pypi, dockerfile_provenance_inputs, flags):
    plain = run(capsys, "requirements.txt", "-p", "3.13", "-t", "linux-x86_64", *flags)
    assert run(capsys, "requirements.txt", "--dockerfile", "Dockerfile", *flags) == plain


def test_github_annotations_advisory_targets_are_warnings(capsys, fake_pypi, fixtures, monkeypatch):
    # A package whose gaps are all on --advisory targets doesn't fail the run, so it's a warning that says why;
    # advisory targets are marked in every message.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    code, out, _ = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-arm64",
        "--github-annotations",
        "--advisory",
        "win-arm64",
    )
    assert code == 1
    lines = annotation_lines(out)
    levels = [(line.split()[0], line.split("title=")[1].split()[0]) for line in lines]
    assert levels == [
        ("::error", "nativeext"),
        ("::error", "puresdist"),
        ("::warning", "fastlib"),
        ("::warning", "winonly"),
    ]
    assert lines[0].endswith("builds from source on linux-x86_64, win-arm64 (advisory).")
    assert lines[3].endswith("can't install on win-arm64 (advisory). Gaps on advisory targets don't fail the run.")
    assert "fail-on missing" not in out


def test_github_annotations_ignore_and_exit_zero(capsys, fake_pypi, fixtures, monkeypatch):
    # --exit-zero only changes the exit code; ignored packages and targets get no annotation.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    code, out, _ = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-arm64",
        "--github-annotations",
        "--exit-zero",
        "--ignore",
        "nativeext,winonly:win-arm64,fastlib:win-arm64",
    )
    assert code == 0
    assert annotation_lines(out) == [
        "::error file=uvproj/uv.lock,line=43,title=puresdist 1.0%3A builds from source::"
        "puresdist 1.0 builds from source on linux-x86_64, win-arm64.",
    ]


@pytest.mark.parametrize("flag", ["--json", "--markdown"])
def test_github_annotations_keep_json_and_markdown_clean(capsys, fake_pypi, fixtures, monkeypatch, flag):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    argv = [str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64", flag]
    code, out, err = run(capsys, *argv, "--github-annotations")
    assert code == 1
    assert "::" not in out.split("\n", 1)[0]
    assert (code, out) == run(capsys, *argv)[:2]  # stdout is exactly what it is without the flag
    assert [line.split(",")[0] for line in annotation_lines(err)] == ["::error file=uvproj/uv.lock"] * 2


def test_github_annotations_without_a_line(capsys, fake_pypi, tmp_path, monkeypatch):
    # Two entries of one name and version in a uv.lock could be either: the file, but no line.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "uv.lock").write_text(uv_lock([("twice", "1.0"), ("twice", "1.0"), ("once", "1.0")]), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--github-annotations")
    files = [line.split("title=")[0] for line in annotation_lines(out)]
    assert files == [
        "::error file=uv.lock,line=27,",
        "::error file=uv.lock,",
        "::error file=uv.lock,",
    ]  # once, twice x2

    # A [[package]] header inside a string throws the count off, so no line in the file is trusted.
    trap = 'description = """\n[[package]]\nname = "once"\n"""\n'
    (tmp_path / "uv.lock").write_text(uv_lock([("once", "1.0")], extra=trap), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--github-annotations")
    assert [line.split("title=")[0] for line in annotation_lines(out)] == ["::error file=uv.lock,"]

    # A file outside the workspace can't be shown in the diff, and package names have no file at all.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "elsewhere"))
    _, out, _ = run(capsys, str(tmp_path), "nowheels==0.9", "-p", "3.13", "-t", "linux-x86_64", "--github-annotations")
    assert annotation_lines(out) == [
        "::error title=nowheels 0.9%3A builds from source::nowheels 0.9 builds from source on linux-x86_64.",
        "::error title=once 1.0%3A builds from source::"
        f"once 1.0 builds from source on linux-x86_64. (in {tmp_path / 'uv.lock'})",
    ]


def test_github_annotations_escaping():
    from wheelgap.github import workflow_command

    # As @actions/core escapes them: %, CR and LF everywhere, and : and , in properties too.
    assert workflow_command("warning", "50% done\r\nnext: a, b", "dir,x/a:b.txt", 3, "t%: 1,\n2") == (
        "::warning file=dir%2Cx/a%3Ab.txt,line=3,title=t%25%3A 1%2C%0A2::50%25 done%0D%0Anext: a, b"
    )
    assert workflow_command("error", "plain") == "::error::plain"


def test_github_annotations_pipfile_lock_entry_the_parser_kept(capsys, fake_pypi, tmp_path, monkeypatch):
    # Listed in develop first and in default too: the parser keeps default's entry, so that's the line. A _meta
    # key with the same name ("hash") isn't a package.
    (tmp_path / "Pipfile.lock").write_text(
        '{"_meta": {"hash": {"sha256": "00"}, "sources": [{"name": "pypi", "url": "https://pypi.org/simple"}]},\n'
        ' "develop": {"nowheels": {"version": "==0.9"}},\n'
        ' "default": {"purewheel": {"version": "==3.1"},\n'
        '             "nowheels": {"version": "==0.9", "markers": "os_name == \\"posix\\""}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--github-annotations")
    assert [line.split(",title=")[0] for line in annotation_lines(out)] == ["::error file=Pipfile.lock,line=4"]


UV_ROOT = (
    'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
    'source = { editable = "." }\n'
)


def sdist_block(name, version="1.0", header="[[package]]"):
    return (
        f'\n{header}\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        f'sdist = {{ url = "https://files.example/{name}-{version}.tar.gz", hash = "sha256:00" }}\n'
    )


def annotate(capsys, tmp_path, *argv, targets="linux-x86_64"):
    """Run with --github-annotations from tmp_path as the workspace; the annotation lines up to their title."""
    code, out, _ = run(capsys, *argv, "-p", "3.13", "-t", targets, "--github-annotations")
    return [line.split("title=")[0] for line in annotation_lines(out)]


@pytest.mark.parametrize(
    "lock",
    [
        # A name line inside a multi-line string, before the block's real one.
        UV_ROOT
        + 'dependencies = [{ name = "a" }]\n\n[[package]]\nsummary = """\nname = "a"\n"""\n'
        + sdist_block("a").removeprefix("\n[[package]]\n"),
        # A [[package]] inside a string, balanced by a real header the scan doesn't read ([["package"]]).
        UV_ROOT
        + 'dependencies = [{ name = "a" }]\n'
        + sdist_block("b")
        + "notes = '''\n[[package]]\nname = \"a\"\n'''\n"
        + sdist_block("a", header='[["package"]]'),
    ],
)
def test_github_annotations_multiline_strings_give_no_line(capsys, fake_pypi, tmp_path, monkeypatch, lock):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    assert annotate(capsys, tmp_path, str(tmp_path)) == ["::error file=uv.lock,"]


def test_github_annotations_toml_names_are_cross_checked(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    # The block's name is a quoted key the scan doesn't read; the wheel table's `name` after it isn't the block's.
    lock = (
        UV_ROOT
        + 'dependencies = [{ name = "a" }]\n\n[[package]]\n"name" = "a"\nversion = "1.0"\n'
        + 'source = { registry = "https://pypi.org/simple" }\n'
        + 'sdist = { url = "https://files.example/a-1.0.tar.gz", hash = "sha256:00" }\n\n'
        + '[[package.wheels]]\nname = "a"\nurl = "https://files.example/a-1.0-py3-none-win_amd64.whl"\n'
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    assert annotate(capsys, tmp_path, str(tmp_path)) == ["::error file=uv.lock,"]

    # When the scan's names don't match the parsed array, name for name, no line in the file is used.
    from wheelgap import github

    (tmp_path / "uv.lock").write_text(UV_ROOT + sdist_block("a") + sdist_block("b"), encoding="utf-8")
    assert github.toml_lines(tmp_path / "uv.lock", "package") == {
        ("demo", "0.1.0"): [5],
        ("a", "1.0"): [10],
        ("b", "1.0"): [16],
    }
    real = github.tomllib.loads
    monkeypatch.setattr(github.tomllib, "loads", lambda text: {"package": real(text)["package"][::-1]})
    assert github.toml_lines(tmp_path / "uv.lock", "package") == {}


def test_github_annotations_versions_of_one_name(capsys, fake_pypi, tmp_path, monkeypatch):
    # A forked uv.lock with two versions of a name: each on its own line.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    lock = (
        UV_ROOT
        + "dependencies = [\n"
        + '    { name = "a", version = "1.0", source = { registry = "https://pypi.org/simple" }, '
        + "marker = \"sys_platform == 'linux'\" },\n"
        + '    { name = "a", version = "2.0", source = { registry = "https://pypi.org/simple" }, '
        + "marker = \"sys_platform == 'win32'\" },\n]\n"
        + sdist_block("a", "1.0")
        + sdist_block("a", "2.0")
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    assert annotate(capsys, tmp_path, str(tmp_path), targets="linux-x86_64,win-amd64") == [
        "::error file=uv.lock,line=14,",
        "::error file=uv.lock,line=20,",
    ]


def test_github_annotations_pdm_lock_first_entry(capsys, fake_pypi, tmp_path, monkeypatch):
    # PDM writes one entry per extras variant; the parser keeps the first, so that's the line.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    entry = (
        '\n[[package]]\nname = "a"\nversion = "1.0"\n{extras}groups = ["default"]\n'
        'files = [{{file = "a-1.0.tar.gz", hash = "sha256:00"}}]\n'
    )
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default"]\nstrategy = ["inherit_metadata"]\n'
        + entry.format(extras="")
        + entry.format(extras='extras = ["x"]\n'),
        encoding="utf-8",
    )
    assert annotate(capsys, tmp_path, str(tmp_path)) == ["::error file=pdm.lock,line=6,"]


@pytest.mark.parametrize(
    "text",
    [
        '{"default": {"nowheels": {"version": "==0.9"}, "nowheels": {"version": "==0.9"}}}',  # a duplicate key
        # "default" twice: json.loads keeps the second, so the nowheels on line 1 isn't the entry the parser read.
        '{"default": {"nowheels": {"version": "==0.9"}},\n "default": {"purewheel": {"version": "==3.1"}},\n'
        ' "develop": {"nowheels": {"version": "==0.9"}}}',
        '{"_meta": {},\r"default": {\n"nowheels": {"version": "==0.9"}}}',  # a CR on its own
    ],
)
def test_github_annotations_pipfile_lock_without_a_line(capsys, fake_pypi, tmp_path, monkeypatch, text):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "Pipfile.lock").write_bytes(text.encode())
    assert annotate(capsys, tmp_path, str(tmp_path)) == ["::error file=Pipfile.lock,"]


def test_github_annotations_crlf(capsys, fake_pypi, tmp_path, monkeypatch):
    # GitHub counts lines as LF does, CRLF included.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "uv.lock").write_bytes(uv_lock([("a", "1.0")]).replace("\n", "\r\n").encode())
    assert annotate(capsys, tmp_path, str(tmp_path)) == ["::error file=uv.lock,line=13,"]
    (tmp_path / "requirements.txt").write_bytes(
        b"# a comment\r\npurewheel==3.1\r\nnowheels==0.9 \\\r\n    --hash=sha256:00\r\n"
    )
    assert annotate(capsys, tmp_path, str(tmp_path / "requirements.txt")) == ["::error file=requirements.txt,line=3,"]


@pytest.mark.parametrize("brk", ["\x0c", "\x85", " "])
def test_github_annotations_requirements_other_line_breaks(capsys, fake_pypi, tmp_path, monkeypatch, brk):
    # The parser splits lines on these too (str.splitlines), and GitHub doesn't: no line is safe.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "requirements.txt").write_text(f"purewheel==3.1{brk}\nnowheels==0.9\n", encoding="utf-8")
    assert annotate(capsys, tmp_path, str(tmp_path / "requirements.txt")) == ["::error file=requirements.txt,"]


def test_github_annotations_requirements_lines(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "requirements.txt"
    # Options, editables, paths and URLs aren't requirements with a line.
    req.write_text(
        "--index-url https://pypi.org/simple\n-e ./editable\n./local\n"
        "git+https://example.invalid/x.git#egg=gitpkg\nnamed @ https://example.invalid/named-1.0.tar.gz\n"
        "-r other.txt\nnowheels==0.9\n",
        encoding="utf-8",
    )
    (tmp_path / "other.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=7,"]
    # A continued requirement is on the first line with something on it.
    req.write_text("purewheel==3.1\n\\\n\\\nnowheels==0.9\n", encoding="utf-8")
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=4,"]


def test_github_annotations_requirement_with_options_keeps_its_line(capsys, fake_pypi, tmp_path, monkeypatch):
    # The requirement ends where the parser ends it, before its options, so the line still matches.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "requirements.txt"
    for line in ("nowheels==0.9 -f https://example.invalid/w.html", "nowheels==0.9 --no-binary nowheels"):
        req.write_text(f"purewheel==3.1\n{line}\n", encoding="utf-8")
        assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=2,"]


def test_github_annotations_requirements_include_after_options(capsys, fake_pypi, tmp_path, monkeypatch):
    # The scan follows a -r where the parser does (after other options, or a quoted path), so lines still match.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "requirements.txt"
    (tmp_path / "dir with space").mkdir()
    (tmp_path / "dir with space" / "other.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    for line in ('--pre -r "dir with space/other.txt"', '-r "dir with space/other.txt" -r gone.txt'):
        req.write_text(f"{line}\nnowheels==0.9\n", encoding="utf-8")
        assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=2,"]


@pytest.mark.parametrize("include", ["-c cons.txt", '--pre --constraint="cons.txt"'])
def test_github_annotations_requirements_inside_constraints(tmp_path, include):
    from wheelgap.github import Locator
    from wheelgap.sources import load_file

    # Both the top-level and nested requirement keep their locations (#614).
    req = tmp_path / "a.txt"
    req.write_text(f"requests==2.32.5\n{include}\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r extra.txt\n", encoding="utf-8")
    extra = tmp_path / "extra.txt"
    extra.write_text("nowheels==0.9\n", encoding="utf-8")
    source = load_file(req)
    locator = Locator([source])
    assert [(p.name, locator.locate(p)) for p in source.packages] == [
        ("requests", (req, 1)),
        ("nowheels", (extra, 1)),
    ]


def test_github_annotations_requirements_nested_constraints(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "a.txt"
    req.write_text("ranged==1.9\n-c sub/cons.txt\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "cons.txt").write_text("purewheel==3.1\n-r extra.txt\n", encoding="utf-8")
    (sub / "extra.txt").write_text("-c deep.txt\nnowheels==0.9\n", encoding="utf-8")
    (sub / "deep.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    # -c -> -r -> -c: only requirement lines get annotations, never the constraints themselves.
    assert annotate(capsys, tmp_path, str(req)) == [
        "::error file=sub/extra.txt,line=2,",
        "::error file=a.txt,line=1,",
    ]


@pytest.mark.parametrize("includes", ["-c shared.txt\n-r shared.txt\n", "-r shared.txt\n-c shared.txt\n"])
def test_github_annotations_requirements_both_roles(capsys, fake_pypi, tmp_path, monkeypatch, includes):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "a.txt"
    req.write_text(includes + "-r extra.txt\n", encoding="utf-8")
    (tmp_path / "shared.txt").write_text("-r extra.txt\nranged==1.9\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    # Reading a file as constraints mustn't hide its own requirements when reached through -r.
    # The requirement shared by all paths is still read only once.
    assert annotate(capsys, tmp_path, str(req)) == [
        "::error file=extra.txt,line=1,",
        "::error file=shared.txt,line=2,",
    ]


def test_github_annotations_deps_inside_constraints(index, capsys, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "a.txt"
    req.write_text("-c cons.txt\napp==1.0\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r extra.txt\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("crypt==1.0\n", encoding="utf-8")
    code, out, _ = run(capsys, str(req), "--deps", "-p", "3.14", "-t", "win-arm64", "--github-annotations")
    assert code == 1
    (line,) = [line for line in annotation_lines(out) if "title=crypt" in line]
    assert line.startswith("::error file=extra.txt,line=1,")


@pytest.mark.parametrize("target", ["https://example.invalid/cons.txt", "${CONSTRAINTS}"])
def test_requirements_unread_constraints_are_refused(capsys, fake_pypi, tmp_path, target):
    req = tmp_path / "a.txt"
    req.write_text(f"-c {target}\nnumpy\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and out == ""
    assert f"a.txt:1: -c {target}: pip reads a constraints file" in err
    assert fake_pypi == []  # nothing is checked at its newest release


@pytest.mark.parametrize("lines", [None, ["purewheel==3.1", "nowheels==0.9"]])
def test_github_annotations_requirements_constraints_cross_check(capsys, fake_pypi, tmp_path, monkeypatch, lines):
    from wheelgap import github

    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "a.txt"
    req.write_text("ranged==1.9\n-c cons.txt\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r extra.txt\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    read = github._lines
    monkeypatch.setattr(github, "_lines", lambda path: lines if path.name == "extra.txt" else read(path))
    # If a nested file disappears or changes between parsing and locating, neither package gets a line.
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=a.txt,", "::error file=a.txt,"]


def test_github_annotations_symlinked_requirements_inside_constraints(capsys, fake_pypi, tmp_path, monkeypatch):
    # The requirements file is a link into real/, and both folders have a cons.txt and an extra.txt. pip reads
    # the -c from the link's folder (#604) and the -r inside it as requirements (#614): nowheels is on
    # link/extra.txt, not real/extra.txt, and the constraints file's own ranged line gives no annotation. The
    # linked file's own line is on the file the link points to, which is the one git has lines for.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    (tmp_path / "real" / "reqs.txt").write_text("-c cons.txt\nranged==1.9\n", encoding="utf-8")
    (tmp_path / "real" / "cons.txt").write_text("-r extra.txt\n", encoding="utf-8")
    (tmp_path / "real" / "extra.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    (tmp_path / "link" / "cons.txt").write_text("ranged==1.9\n-r extra.txt\n", encoding="utf-8")
    (tmp_path / "link" / "extra.txt").write_text("# read through link/cons.txt\nnowheels==0.9\n", encoding="utf-8")
    try:
        # A Path, so that Windows gets backslashes: a link written with forward slashes can't be opened there.
        (tmp_path / "link" / "reqs.txt").symlink_to(Path("../real/reqs.txt"))
    except OSError:
        pytest.skip("symlinks aren't available here")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    assert annotate(capsys, tmp_path, str(tmp_path / "link" / "reqs.txt")) == [
        "::error file=link/extra.txt,line=2,",
        "::error file=real/reqs.txt,line=2,",
    ]


def test_github_annotations_requirements_cross_check(capsys, fake_pypi, tmp_path, monkeypatch):
    # The file is read twice; if the scan doesn't find what the parser read (here the file changed in between, so
    # every line moved down), no line is used.
    from wheelgap import github

    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "requirements.txt"
    req.write_text("nowheels==0.9\n", encoding="utf-8")
    scan = github.requirement_lines

    def changed(path, seen=None, constraint_paths=()):
        req.write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
        return scan(path, seen, constraint_paths)

    monkeypatch.setattr(github, "requirement_lines", changed)
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,"]
    # An include the scan can't read gives no lines at all.
    req.write_text("-r gone.txt\n", encoding="utf-8")
    assert scan(req) is None


def test_github_annotations_keep_lines_when_included_file_is_reached_via_symlink_in_another_folder(
    capsys, fake_pypi, tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "shared.txt").write_text("-r more.txt\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / "more.txt").write_text("unpinned==4.2\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "more.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    try:
        # A Path, so Windows gets ..\shared.txt: a / in a relative link target dangles there.
        (sub / "link.txt").symlink_to(Path("../shared.txt"))
    except OSError:
        pytest.skip("symlinks unavailable on this platform/user")
    req = tmp_path / "requirements.txt"
    req.write_text("-r shared.txt\n-r sub/link.txt\nnowheels==0.9\n", encoding="utf-8")

    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "musl-aarch64", "--github-annotations")
    assert code == 1, err
    lines = annotation_lines(out)
    assert any(line.startswith("::error file=requirements.txt,line=3,title=nowheels 0.9") for line in lines)
    assert any(line.startswith("::error file=sub/more.txt,line=1,title=fastlib 2.0.0") for line in lines)


def test_github_annotations_keep_lines_for_repeated_include_and_handle_cycles(capsys, fake_pypi, tmp_path, monkeypatch):
    from wheelgap import github

    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    (tmp_path / "shared.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("-r shared.txt\n-r shared.txt\nnowheels==0.9\n", encoding="utf-8")

    _, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "musl-aarch64", "--github-annotations")
    lines = annotation_lines(out)
    assert any(line.startswith("::error file=requirements.txt,line=3,title=nowheels 0.9") for line in lines)
    assert any(line.startswith("::error file=shared.txt,line=1,title=fastlib 2.0.0") for line in lines)

    # An include cycle must return None rather than hang.
    (tmp_path / "a.txt").write_text("-r b.txt\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("-r a.txt\n", encoding="utf-8")
    assert github.requirement_lines(tmp_path / "a.txt") is None


def test_github_annotations_messages(capsys, fake_pypi, fixtures, tmp_path, monkeypatch):
    # A file outside the workspace loses its line too; several Pythons label targets with the Python.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    code, out, _ = run(
        capsys, str(fixtures / "uvproj"), "-p", "3.13", "-p", "3.14", "-t", "linux-aarch64", "--github-annotations"
    )
    assert annotation_lines(out)[0] == (
        "::error title=fastlib 2.0.0%3A builds from source::fastlib 2.0.0 builds from source on "
        f"3.13 linux-aarch64, 3.14 linux-aarch64. (in {fixtures / 'uvproj' / 'uv.lock'})"
    )
    # A lookup error is quoted, and the title names the worst status of several.
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\nnotreal==1.0\n", encoding="utf-8")
    code, out, _ = run(
        capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64,ios-arm64", "--github-annotations"
    )
    assert annotation_lines(out) == [
        "::error file=requirements.txt,line=2,title=notreal 1.0%3A couldn't be checked::"
        "notreal 1.0 couldn't be checked on linux-x86_64, ios-arm64. Lookup error: not on index.",
        "::error file=requirements.txt,line=1,title=nowheels 0.9%3A can't install::"
        "nowheels 0.9 can't install on ios-arm64; builds from source on linux-x86_64.",
    ]


def test_github_annotations_partial_ignore(capsys, fake_pypi, fixtures, monkeypatch):
    # An ignored target leaves the package's other gaps annotated, without the ignored target.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    code, out, _ = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64,win-arm64",
        "--github-annotations",
        "--ignore",
        "fastlib:win-arm64",
    )
    assert annotation_lines(out)[0] == (
        "::error file=uvproj/uv.lock,line=23,title=fastlib 2.0.0%3A builds from source::"
        "fastlib 2.0.0 builds from source on linux-aarch64."
    )


def test_pixi_lock_json(capsys, fixtures, fake_pypi):
    code, out, err = run(
        capsys, str(fixtures / "pixiproj"), "-p", "3.13", "-t", "linux-x86_64,macos-arm64,win-amd64", "--json"
    )
    assert code == 1 and not err
    data = json.loads(out)
    assert data["sources"] == ["pixi.lock"]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["fastlib"]["results"] == {"linux-x86_64": "wheel", "macos-arm64": "source", "win-amd64": "source"}
    # Locked for win-64 only, so not installed on the lock's other platforms (with the same Python)
    assert packages["winonly"]["results"] == {"linux-x86_64": "n/a", "macos-arm64": "n/a", "win-amd64": "wheel"}
    assert data["skipped"] == [
        {"name": "internal", "reason": "private index"},
        {"name": "gitdep", "reason": "git"},
        {"name": "localpkg", "reason": "conda"},
        {"name": "openssl", "reason": "conda"},
        {"name": "python", "reason": "conda"},
    ]
    # Every PyPI package is looked up, since the lock records only pixi's pick per platform; the one from another
    # index never is.
    assert sorted(fake_pypi) == [
        "https://pypi.org/pypi/fastlib/2.0.0/json",
        "https://pypi.org/pypi/purewheel/3.1/json",
        "https://pypi.org/pypi/winonly/311/json",
    ]


def test_pixi_lock_files_are_looked_up_for_other_targets(capsys, fixtures, tmp_path, fake_pypi):
    # pixi locked fastlib's sdist (say, for an older glibc), but PyPI also has a wheel the target takes.
    lock = (fixtures / "pixiproj" / "pixi.lock").read_text(encoding="utf-8")
    lock = lock.replace(
        "packages/aa/bb/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "packages/ee/ff/fastlib-2.0.0.tar.gz", 1
    )
    (tmp_path / "pixi.lock").write_text(lock, encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["fastlib"]["results"] == {"linux-x86_64": "wheel"}
    assert "https://pypi.org/pypi/fastlib/2.0.0/json" in fake_pypi


def test_pixi_lock_failed_lookup_is_unknown(capsys, fixtures, monkeypatch):
    from wheelgap import pypi

    def down(url, timeout=20.0):
        raise OSError("no network")

    monkeypatch.setattr(pypi, "fetch_json", down)
    code, out, _ = run(capsys, str(fixtures / "pixiproj"), "-p", "3.13", "-t", "macos-arm64", "--json")
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    # Not the sdist the lock records for macOS: the release may have a wheel pixi didn't pick.
    assert packages["fastlib"]["results"] == {"macos-arm64": "unknown"}


def test_pixi_lock_environment_option(capsys, fixtures, fake_pypi):
    code, out, _ = run(
        capsys, str(fixtures / "pixiproj"), "--environment", "test", "-p", "3.13", "-t", "linux", "--json"
    )
    assert [p["name"] for p in json.loads(out)["packages"]] == ["nowheels"]
    code, out, err = run(capsys, str(fixtures / "pixiproj"), "--environment", "docs", "-p", "3.13")
    assert code == 2 and not out
    assert err == (
        "wheelgap: error: pixi.lock: no environment 'docs' (it has: default, test); pick one with --environment\n"
    )
    code, out, err = run(capsys, str(fixtures / "uvproj"), "--environment", "test", "-p", "3.13")
    assert code == 2 and err == "wheelgap: error: --environment applies to pixi.lock inputs only\n"


def test_pixi_lock_offline_needs_the_index(capsys, fixtures, fake_pypi):
    code, out, err = run(capsys, str(fixtures / "pixiproj"), "--offline", "-p", "3.13")
    assert code == 2 and not out
    assert err == (
        "wheelgap: error: --offline: pixi.lock (it records only the file pixi picked for each platform) needs the "
        "index for 3 packages (fastlib, purewheel, winonly); use a lockfile that lists each package's files, "
        "or drop --offline\n"
    )
    assert fake_pypi == []


MIRROR_PIXI = """\
version: 7
platforms:
- name: linux-64
environments:
  default:
    channels:
    - url: https://conda.anaconda.org/conda-forge/
    indexes:
    - https://pypi.org/simple
    - https://mirror.example.com/simple
    packages:
      linux-64:
      - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.13.9-hc97d973_101_cp313.conda
      - pypi: https://mirror.example.com/files/fastlib-2.0.0.tar.gz
packages:
- conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.13.9-hc97d973_101_cp313.conda
- pypi: https://mirror.example.com/files/fastlib-2.0.0.tar.gz
  name: fastlib
  version: 2.0.0
  index: https://mirror.example.com/simple
"""


@pytest.mark.parametrize("online", [False, True])
def test_pixi_lock_mirror_package_is_looked_up_never_kept(capsys, tmp_path, fake_pypi, monkeypatch, online):
    # The lock's one file (an sdist) can't answer the target; the mirror the lock names has a wheel for it.
    (tmp_path / "pixi.lock").write_text(MIRROR_PIXI, encoding="utf-8")
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    args = ["-p", "3.13", "-t", "linux-x86_64", "--json"] + ["--online"] * online
    code, out, _ = run(capsys, str(tmp_path), *args)
    assert {p["name"]: p["results"] for p in json.loads(out)["packages"]} == {"fastlib": {"linux-x86_64": "wheel"}}
    assert fake_pypi == ["https://mirror.example.com/pypi/fastlib/2.0.0/json"]
    assert code == 0


def test_pixi_lock_mirror_package_is_skipped_without_its_index(capsys, tmp_path, fake_pypi):
    (tmp_path / "pixi.lock").write_text(MIRROR_PIXI, encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--online", "--json")
    assert json.loads(out)["skipped"] == [
        {"name": "fastlib", "reason": "private index"},
        {"name": "python", "reason": "conda"},
    ]
    assert fake_pypi == [] and code == 0


def test_pixi_lock_next_to_another_lockfile(capsys, fixtures, tmp_path, fake_pypi):
    (tmp_path / "pixi.lock").write_bytes((fixtures / "pixiproj" / "pixi.lock").read_bytes())
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert json.loads(out)["sources"] == ["uv.lock"]
    assert err == (
        f"wheelgap: note: {tmp_path / 'uv.lock'} was checked; {tmp_path / 'pixi.lock'} is in the same folder too "
        "(pass it, or --environment, to check it instead)\n"
    )
    code, out, err = run(
        capsys, str(tmp_path), "--environment", "default", "-p", "3.13", "-t", "linux-x86_64", "--json"
    )
    assert json.loads(out)["sources"] == ["pixi.lock"] and not err
    code, out, err = run(capsys, str(tmp_path), "-q", "-p", "3.13", "-t", "linux-x86_64")
    assert not err


def test_pixi_lock_empty_environment_is_an_error(capsys, fixtures, fake_pypi):
    code, out, err = run(capsys, str(fixtures / "pixiproj"), "--environment", "", "-p", "3.13")
    assert (code, out, err) == (2, "", "wheelgap: error: --environment needs a name\n")


def test_pixi_lock_counts_conda_packages_in_text(capsys, tmp_path, fake_pypi):
    names = ["bzip2", "libffi", "ncurses", "openssl", "readline", "tk"]
    selectors = [f"      - conda: https://conda.anaconda.org/conda-forge/linux-64/{n}-1.0-h0_0.conda" for n in names]
    text = MIRROR_PIXI.replace(
        "      - pypi: https://mirror.example.com/files/fastlib-2.0.0.tar.gz\n", "\n".join(selectors) + "\n"
    )
    (tmp_path / "pixi.lock").write_text(text, encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert "skipped 7 non-index packages: 7 conda packages (not on PyPI)" in out
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--markdown")
    assert "Skipped 7 non-index packages: 7 conda packages (not on PyPI)" in out
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert len(json.loads(out)["skipped"]) == 7  # --json lists each one
    # Up to five are listed by name, after any others
    (tmp_path / "pixi.lock").write_text(
        text.replace(selectors[-1] + "\n", "").replace(selectors[-2] + "\n", ""), encoding="utf-8"
    )
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert "skipped 5 non-index packages: bzip2 (conda), libffi (conda), ncurses (conda), openssl (conda)," in out
    # Exactly six are counted; any other skipped package is still named before the count.
    gitdep = "git+https://github.com/example/gitdep.git#aaaa"
    six = text.replace(selectors[-1] + "\n", f"      - pypi: {gitdep}\n") + f"- pypi: {gitdep}\n  name: gitdep\n"
    (tmp_path / "pixi.lock").write_text(six, encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert "skipped 7 non-index packages: gitdep (git), 6 conda packages (not on PyPI)\n" in out


def test_dependency_needed_from_a_later_patch_release_is_checked(capsys, fake_pypi, tmp_path):
    # #281: -p 3.13 covers 3.13.1 and later too, so a dependency gated on them is checked, not hidden as n/a
    marker = "python_full_version >= '3.13.1'"
    lock = UV_ROOT + f'dependencies = [{{ name = "a", marker = "{marker}" }}]\n' + sdist_block("a")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert not err and not fake_pypi
    package = next(p for p in json.loads(out)["packages"] if p["name"] == "a")
    assert package["results"] == {"linux-x86_64": "source"}
    assert code == 1


def test_requirements_no_binary_and_only_binary(capsys, tmp_path, fake_pypi):
    req = tmp_path / "requirements.txt"
    # :all: --no-binary, then overridden for fastlib by :none: / --only-binary in included file
    req.write_text(
        "--no-binary :all:\npurewheel==3.1\nfastlib==2.0.0\n",
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--no-notes")
    assert code == 1 and not err
    assert "ignored (--no-binary)" in out
    assert "purewheel-3.1-py3-none-any.whl" in out
    assert "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" in out

    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json", "--explain", "--no-notes")
    assert code == 1 and not err
    data = json.loads(out)
    pkgs = {p["name"]: p for p in data["packages"]}
    assert pkgs["fastlib"]["results"] == {"linux-x86_64": "source"}
    assert pkgs["purewheel"]["results"] == {"linux-x86_64": "missing"}
    assert pkgs["fastlib"]["rejected"]["linux-x86_64"] == [
        {"file": "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "reason": "ignored (--no-binary)"}
    ]

    # Reset with :none: and switch to --only-binary via -r included file; named package vs :all:
    inc = tmp_path / "extra.txt"
    inc.write_text("--only-binary=:all:\n--only-binary=:none:\n--only-binary=fastlib\n", encoding="utf-8")
    req.write_text(
        "--no-binary :all:\n--no-binary :none:\n-r extra.txt\nfastlib==2.0.0\nnowheels==0.9\n",
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--explain", "--no-notes")
    assert code == 1 and not err
    assert "--only-binary forbids building from source" in out

    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--json")
    assert code == 1 and not err
    pkgs = {p["name"]: p for p in json.loads(out)["packages"]}
    assert pkgs["fastlib"]["results"] == {"win-amd64": "missing"}
    assert pkgs["nowheels"]["results"] == {"win-amd64": "source"}

    # Backslash-continued quoted and unquoted comma lists join without adding whitespace, matching pip.
    req.write_text(
        '--no-binary "fastlib,\\\npurewheel"\n--only-binary fastlib,\\\nnowheels\npurewheel==3.1\nnowheels==0.9\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1 and not err
    pkgs = {p["name"]: p for p in json.loads(out)["packages"]}
    assert pkgs["purewheel"]["results"] == {"linux-x86_64": "missing"}
    assert pkgs["nowheels"]["results"] == {"linux-x86_64": "missing"}


@pytest.mark.parametrize("markdown", [False, True])
def test_empty_requirements_says_no_packages_to_check(capsys, fake_pypi, tmp_path, markdown):
    req = tmp_path / "requirements.txt"
    req.write_text("", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", *(["--markdown"] if markdown else []))
    assert code == 0
    assert ("### wheelgap: no packages to check" if markdown else "No packages to check.") in out
    assert "have a wheel" not in out and "has a wheel" not in out


def test_only_editable_requirements_says_no_packages_to_check(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("-e .\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    skipped = next(i for i, line in enumerate(lines) if "skipped" in line)
    assert lines.index("No packages to check.") > skipped  # the reason comes first
    assert "All 0" not in out


@pytest.mark.parametrize("markdown", [False, True])
def test_ignore_every_package_says_no_packages_to_check(capsys, fake_pypi, tmp_path, markdown):
    req = tmp_path / "requirements.txt"
    req.write_text("purewheel==3.1\nfastlib==2.0.0\n", encoding="utf-8")
    code, out, err = run(
        capsys,
        str(req),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64",
        "--ignore",
        "purewheel,fastlib",
        *(["--markdown"] if markdown else []),
    )
    assert code == 0
    assert "have a wheel" not in out and "has a wheel" not in out
    if markdown:
        assert "### wheelgap: no packages to check" in out
        assert "Ignored: `fastlib`, `purewheel`" in out
    else:
        lines = [line for line in out.splitlines() if line.strip()]
        ignored = next(i for i, line in enumerate(lines) if "ignored" in line)
        assert lines[ignored] == "ignored: fastlib, purewheel"
        assert lines.index("No packages to check.") > ignored  # the reason comes first


def test_range_split_clean_counts_as_one_package(capsys, fake_pypi):
    base = ("pythonranged>=1", "-p", "3.9,3.13", "-t", "linux-x86_64")

    code, out, _ = run(capsys, *base, "--color", "never")
    assert code == 0
    assert "· 1 package ·" in out
    assert "The 1 package has a wheel for every one of 2 targets." in out

    code, out, _ = run(capsys, *base, "--markdown")
    assert code == 0
    assert "### wheelgap: every package has a wheel for all 2 targets" in out
    assert "· 1 package ·" in out

    code, out, _ = run(capsys, *base, "--json")
    assert code == 0
    data = json.loads(out)
    assert len(data["packages"]) == 2
    assert data["summary"]["packages"] == 1
    assert data["summary"]["with_gaps"] == 0
    _assert_matches_output_schema(data)

    # A second distinct input package counts as 2, and both clean releases of pythonranged count once when hidden.
    both_clean_plus_gap = ("pythonranged>=1", "nowheels==0.9", "-p", "3.9,3.13", "-t", "linux-x86_64")
    code, out, _ = run(capsys, *both_clean_plus_gap, "--color", "never")
    assert code == 1
    assert "· 2 packages ·" in out
    assert "1 package with wheels for every target hidden (--all to show)" in out
    assert "1 of 2 packages has gaps: 1 will build from source." in out

    code, out, _ = run(capsys, "pythonranged>=1", "purewheel==3.1", "-p", "3.9,3.13", "-t", "linux-x86_64", "--json")
    assert code == 0
    data = json.loads(out)
    assert len(data["packages"]) == 3
    assert data["summary"]["packages"] == 2
    assert data["summary"]["with_gaps"] == 0


def test_range_split_one_clean_one_gap_counts_as_one_package(capsys, fake_pypi, monkeypatch, tmp_path):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(
        FAKE_INDEX,
        "splitgap",
        {
            "1.0": [{"filename": "splitgap-1.0-py3-none-any.whl", "requires_python": "<3.10"}],
            "2.0": [{"filename": "splitgap-2.0.tar.gz", "requires_python": ">=3.10"}],
        },
    )
    base = ("splitgap>=1", "-p", "3.9,3.13", "-t", "linux-x86_64")

    code, out, _ = run(capsys, *base, "--color", "never")
    assert code == 1
    assert "· 1 package ·" in out
    # The clean 3.9 release is the same requirement, not another package with wheels everywhere.
    assert "1 other release of a package with gaps hidden (--all to show)" in out
    assert "package with wheels for every target" not in out
    assert "1 of 1 package has gaps: 1 will build from source." in out

    code, out, _ = run(capsys, *base, "-q", "--color", "never")
    assert code == 1
    assert out.splitlines() == [
        "1 of 1 package has gaps: 1 will build from source.",
        "gaps: splitgap",
        "blocking 3.13: splitgap",
    ]

    code, out, _ = run(capsys, *base, "--markdown")
    assert code == 1
    assert "### wheelgap: 1 of 1 package has gaps" in out
    assert "· 1 package ·" in out
    assert "1 other release of a package with gaps hidden (`--all` to show)." in out
    assert "1 of 1 package has gaps: 1 will build from source." in out

    code, out, _ = run(capsys, *base, "--json")
    assert code == 1
    data = json.loads(out)
    assert len(data["packages"]) == 2
    assert data["summary"] == {
        "packages": 1,
        "with_gaps": 1,
        "per_target": {
            "3.9 linux-x86_64": {"source": 0, "missing": 0, "unknown": 0},
            "3.13 linux-x86_64": {"source": 1, "missing": 0, "unknown": 0},
        },
    }
    _assert_matches_output_schema(data)

    # When both releases are hidden via --ignore, the one input package counts once in hidden_line.
    code, out, _ = run(
        capsys, "splitgap>=1", "nowheels==0.9", *base[1:], "--ignore", "splitgap:linux-x86_64", "--color", "never"
    )
    assert code == 1
    assert "· 2 packages ·" in out
    assert "1 package with only ignored gaps hidden (--all to show)" in out
    assert "1 of 2 packages has gaps: 1 will build from source." in out

    # When splitgap is checked on its own with gaps ignored, the clean summary counts it as 1 package.
    code, out, _ = run(capsys, *base, "--ignore", "splitgap:linux-x86_64", "--color", "never")
    assert code == 0
    assert "The 1 package has a wheel for every one of 2 targets, apart from 1 ignored gap." in out

    # Two requirements for one name under different markers are one package too, though each keeps its row.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "splitgap==1.0; python_version < '3.10'\nsplitgap==2.0; python_version >= '3.10'\n", encoding="utf-8"
    )
    code, out, _ = run(capsys, str(req), "-p", "3.9,3.13", "-t", "linux-x86_64", "--json")
    assert code == 1
    data = json.loads(out)
    assert len(data["packages"]) == 2
    assert data["summary"]["packages"] == 1
    assert data["summary"]["with_gaps"] == 1


def test_range_split_both_releases_with_different_gaps_counts_as_one_package(capsys, fake_pypi, monkeypatch):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(
        FAKE_INDEX,
        "bothgaps",
        {
            "1.0": [{"filename": "bothgaps-1.0.tar.gz", "requires_python": "<3.10"}],
            "2.0": [{"filename": "bothgaps-2.0-cp313-cp313-win_amd64.whl", "requires_python": ">=3.10"}],
        },
    )
    base = ("bothgaps>=1", "-p", "3.9,3.13", "-t", "linux-x86_64")

    code, out, _ = run(capsys, *base, "--color", "never")
    assert code == 1
    assert "· 1 package ·" in out
    assert "3.9 linux-x86_64   1 from source" in out
    assert "3.13 linux-x86_64  1 missing" in out
    assert "1 of 1 package has gaps: 1 can't install at all." in out

    code, out, _ = run(capsys, *base, "-q", "--color", "never")
    assert code == 1
    assert out.splitlines() == [
        "1 of 1 package has gaps: 1 can't install at all.",
        "gaps: bothgaps",
        "blocking 3.13: none (every gap on 3.13 is on 3.9 too)",
    ]

    code, out, _ = run(capsys, *base, "--markdown")
    assert code == 1
    assert "### wheelgap: 1 of 1 package has gaps" in out
    assert "1 of 1 package has gaps: 1 can't install at all." in out

    code, out, _ = run(capsys, *base, "--json")
    assert code == 1
    data = json.loads(out)
    assert len(data["packages"]) == 2
    assert data["summary"] == {
        "packages": 1,
        "with_gaps": 1,
        "per_target": {
            "3.9 linux-x86_64": {"source": 1, "missing": 0, "unknown": 0},
            "3.13 linux-x86_64": {"source": 0, "missing": 1, "unknown": 0},
        },
    }
    _assert_matches_output_schema(data)

    code, out, _ = run(capsys, *base, "--advisory", "linux-x86_64", "--color", "never")
    assert code == 0
    assert "2 gaps in 1 package on advisory targets 3.9 linux-x86_64, 3.13 linux-x86_64 don't fail the run." in out


@pytest.mark.parametrize(
    "setting,package,expected,policy_label",
    [
        ("no-build = true", "puresdist", "missing", "[tool.uv] no-build"),
        ('no-binary-package = ["fastlib"]', "fastlib", "source", "[tool.uv] no-binary-package"),
        ("no-binary = true", "fastlib", "source", "[tool.uv] no-binary"),
    ],
)
def test_uv_build_policy_changes_cli_status_and_explanation(
    tmp_path, capsys, fake_pypi, setting, package, expected, policy_label
):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[manifest]\nrequirements = [{ name = "fastlib" }, { name = "puresdist" }]\n'
        '[[package]]\nname = "fastlib"\nversion = "2.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n'
        'sdist = { url = "https://files.example/fastlib-2.0.0.tar.gz" }\n'
        '[[package]]\nname = "puresdist"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/puresdist-1.0.tar.gz" }\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting + "\n", encoding="utf-8")
    code = main([str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    packages = {item["name"]: item for item in json.loads(capsys.readouterr().out)["packages"]}
    assert code == 1
    assert packages[package]["results"]["linux-x86_64"] == expected
    assert main([str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain"]) == 1
    output = capsys.readouterr().out
    assert policy_label in output
    assert fake_pypi == []


BAD_UTF8_LOCK = (
    'version = 1\n[manifest]\nrequirements = [{ name = "fastlib" }]\n'
    '[[package]]\nname = "fastlib"\nversion = "2.0.0"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
    'wheels = [{ url = "https://files.example/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n'
)


@pytest.mark.parametrize("as_json", [False, True])
def test_pyproject_beside_the_lock_that_isnt_utf8_is_an_input_error(capsys, fake_pypi, tmp_path, as_json):
    (tmp_path / "uv.lock").write_text(BAD_UTF8_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_bytes("[tool.wheelgap]\n# café\n".encode("latin-1"))
    args = [str(tmp_path), "-p", "3.13", "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert "pyproject.toml" in err
    assert "isn't UTF-8" in err
    assert "Traceback" not in err
    assert fake_pypi == []
    if as_json:
        error = json.loads(out)["error"]
        assert error["type"] == "SourceError"
        assert "pyproject.toml" in error["message"] and "isn't UTF-8" in error["message"]


@pytest.mark.parametrize("as_json", [False, True])
def test_uv_toml_beside_the_lock_that_isnt_utf8_is_an_input_error(capsys, fake_pypi, tmp_path, as_json):
    (tmp_path / "uv.lock").write_text(BAD_UTF8_LOCK, encoding="utf-8")
    (tmp_path / "uv.toml").write_bytes("no-build = true\n# café\n".encode("latin-1"))
    args = [str(tmp_path), "-p", "3.13", "-t", "linux-x86_64"]
    if as_json:
        args.append("--json")
    code, out, err = run(capsys, *args)
    assert code == 2
    assert "uv.toml" in err
    assert "isn't UTF-8" in err
    assert "Traceback" not in err
    assert fake_pypi == []
    if as_json:
        error = json.loads(out)["error"]
        assert error["type"] == "SourceError"
        assert "uv.toml" in error["message"] and "isn't UTF-8" in error["message"]


def test_a_pyproject_with_a_bom_is_read(tmp_path, capsys, fake_pypi):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[manifest]\nrequirements = [{ name = "fastlib" }, { name = "puresdist" }]\n'
        '[[package]]\nname = "fastlib"\nversion = "2.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n'
        'sdist = { url = "https://files.example/fastlib-2.0.0.tar.gz" }\n'
        '[[package]]\nname = "puresdist"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/puresdist-1.0.tar.gz" }\n',
        encoding="utf-8",
    )
    # No -p or -t: the Python comes from [tool.wheelgap] and the targets from [tool.uv] required-environments,
    # so all three readers of this pyproject.toml have to get past the BOM.
    (tmp_path / "pyproject.toml").write_bytes(
        (
            "\ufeff[tool.uv]\nno-build = true\nrequired-environments = [\"sys_platform == 'win32'\"]\n\n"
            '[tool.wheelgap]\npython = ["3.13"]\n'
        ).encode("utf-8")
    )
    code, out, err = run(capsys, str(tmp_path), "--json")
    assert code == 1
    data = json.loads(out)
    # the [tool.wheelgap] python key and the required-environments targets apply despite the BOM
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "win-amd64"), ("3.13", "win-arm64")]
    # and so does the [tool.uv] no-build policy: sdist-only puresdist is missing, not buildable from source
    packages = {item["name"]: item for item in data["packages"]}
    assert packages["puresdist"]["results"] == {"win-amd64": "missing", "win-arm64": "missing"}
    assert fake_pypi == []


def test_a_pyproject_with_a_bom_sets_uv_default_groups(capsys, fake_pypi, fixtures, tmp_path):
    shutil.copy(fixtures / "uvselect" / "uv.lock", tmp_path)
    (tmp_path / "pyproject.toml").write_bytes('\ufeff[tool.uv]\ndefault-groups = ["docs"]\n'.encode("utf-8"))
    # --extra makes wheelgap read [tool.uv] default-groups, which replaces the dev group with docs
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json", "--extra", "gpu")
    assert code in (0, 1), err
    names = {row["name"] for row in json.loads(out)["packages"]}
    assert {"docstool", "gpu"} <= names
    assert "devtool" not in names


def test_explain_issues_url_text_output(capsys, fake_pypi):
    code, out, _ = run(capsys, "gapwithissues==1.0", "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1
    assert "ask for a wheel: https://github.com/example/gapwithissues/issues" in out


def test_explain_issues_url_markdown_output(capsys, fake_pypi):
    code, out, _ = run(capsys, "gapwithissues==1.0", "-p", "3.13", "-t", "linux-x86_64", "--markdown", "--explain")
    assert code == 1
    assert "<details><summary>Why (rejected wheels)</summary>" in out
    assert "ask for a wheel: https://github.com/example/gapwithissues/issues" in out


def test_explain_issues_url_json_output(capsys, fake_pypi):
    code, out, _ = run(capsys, "gapwithissues==1.0", "-p", "3.13", "-t", "linux-x86_64", "--json", "--explain")
    assert code == 1
    data = json.loads(out)
    pkg = next(p for p in data["packages"] if p["name"] == "gapwithissues")
    assert pkg["issues_url"] == "https://github.com/example/gapwithissues/issues"


def test_explain_issues_url_homepage_fallback(capsys, fake_pypi):
    code, out, _ = run(capsys, "gapwithhome==1.0", "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1
    assert "ask for a wheel: https://github.com/example/gapwithhome/issues" in out


def test_explain_issues_url_insecure_is_rejected(capsys, fake_pypi):
    code, out, _ = run(capsys, "gapinsecure==1.0", "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1
    assert "ask for a wheel:" not in out

    code, out, _ = run(capsys, "gapinsecure==1.0", "-p", "3.13", "-t", "linux-x86_64", "--json", "--explain")
    assert code == 1
    data = json.loads(out)
    pkg = next(p for p in data["packages"] if p["name"] == "gapinsecure")
    assert pkg["issues_url"] is None


def _write_transitive_table_lock(tmp_path, *, several=False, marked=False):
    deps = ['{ name = "mcp" }']
    if several:
        deps.extend(['{ name = "requests" }', '{ name = "click" }'])
    if marked:
        deps.append('{ name = "platformer", marker = "sys_platform == \'win32\'" }')
    text = (
        "version = 1\n"
        '[[package]]\nname = "app"\nversion = "1"\nsource = { editable = "." }\n'
        f"dependencies = [{', '.join(deps)}]\n"
    )
    for name in ("mcp", "requests", "click", "platformer"):
        if name != "mcp" and not (several or (marked and name == "platformer")):
            continue
        if name == "platformer" and not marked:
            continue
        text += (
            f'[[package]]\nname = "{name}"\nversion = "1"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            'dependencies = [{ name = "cryptography" }]\n'
            f'wheels = [{{ url = "https://files.example/{name}-1-py3-none-any.whl" }}]\n'
        )
    text += (
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n'
    )
    (tmp_path / "uv.lock").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("markdown", [False, True])
def test_transitive_gap_table_names_its_direct_parent(capsys, tmp_path, fake_pypi, markdown):
    _write_transitive_table_lock(tmp_path)
    args = ["--markdown"] if markdown else []
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", *args)
    assert code == 1 and not err
    assert "cryptography (via mcp)" in out
    assert "mcp (via" not in out
    assert fake_pypi == []


@pytest.mark.parametrize("markdown", [False, True])
def test_transitive_gap_table_counts_distinct_direct_parents(capsys, tmp_path, fake_pypi, markdown):
    _write_transitive_table_lock(tmp_path, several=True)
    args = ["--markdown"] if markdown else []
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", *args)
    assert code == 1 and not err
    assert "cryptography (via mcp +2)" in out
    assert fake_pypi == []


def test_transitive_gap_table_respects_marker_and_selected_graph(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path, marked=True)
    for target, expected in (
        ("linux-x86_64", "cryptography (via mcp)"),
        ("win-amd64", "cryptography (via mcp +1)"),
    ):
        code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", target, "--no-dev", "--offline")
        assert code == 1 and not err
        assert expected in out
    assert fake_pypi == []


def test_transitive_gap_table_no_graph_quiet_and_json_unchanged(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path)
    base = (str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    code, out, _ = run(capsys, *base, "-q")
    assert code == 1 and "via" not in out
    code, out, _ = run(capsys, *base, "--json", "--explain")
    assert code == 1
    crypto = next(p for p in json.loads(out)["packages"] if p["name"] == "cryptography")
    assert crypto["via"]["linux-x86_64"] == ["app", "mcp", "cryptography"]
    assert "via_direct" not in crypto
    code, out, _ = run(capsys, "nowheels", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1 and "nowheels" in out and "(via " not in out
    assert fake_pypi == ["https://pypi.org/pypi/nowheels/json"]


def test_poetry_1x_transitive_gap_table_uses_direct_dependency(capsys, tmp_path, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "mcp"\nversion = "1"\n'
        'files = [{ file = "mcp-1-py3-none-any.whl" }]\n'
        '[package.dependencies]\ncryptography = "*"\n\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'files = [{ file = "cryptography-50.0.2.tar.gz" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via mcp)" in out
    assert fake_pypi == []


# click needs colorama on Windows only, and the project depends on both (#815). colorama has only an sdist here.
POETRY_1X_CLICK_COLORAMA = (
    '[[package]]\nname = "click"\nversion = "8.1.7"\n'
    'files = [{ file = "click-8.1.7-py3-none-any.whl" }]\n\n[package.dependencies]\n'
    'colorama = {version = "*", markers = "platform_system == \\"Windows\\""}\n\n'
    '[[package]]\nname = "colorama"\nversion = "0.4.6"\nfiles = [{ file = "colorama-0.4.6.tar.gz" }]\n\n'
    '[metadata]\nlock-version = "2.0"\n'
)


def test_poetry_1x_lock_without_its_pyproject_is_unknown_where_it_could_hide_a_gap(capsys, tmp_path, fake_pypi):
    (tmp_path / "poetry.lock").write_text(POETRY_1X_CLICK_COLORAMA, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64", "--json")
    assert code == 1  # used to be a pass, with colorama hidden on Linux and no note
    rows = {p["name"]: p for p in json.loads(out)["packages"]}
    assert rows["colorama"]["results"] == {"linux-x86_64": "unknown", "win-amd64": "source"}  # not ✓
    (reason,) = rows["colorama"]["unknown_reasons"].values()
    assert "the pyproject.toml beside the lock is needed to tell" in reason
    assert "(there is no pyproject.toml beside poetry.lock)" in reason
    assert rows["click"]["results"] == {"linux-x86_64": "wheel", "win-amd64": "wheel"}
    assert "unknown_reasons" not in rows["click"]
    assert (
        "wheelgap: note: a Poetry 1.x lock doesn't record the project's own dependencies, and there is no "
        "pyproject.toml beside poetry.lock: colorama is `unknown` where no dependency installs it"
    ) in err
    # --explain says why, and --ignore takes it like any other unknown.
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert "colorama 0.4.6 on linux-x86_64: ? unknown\n  no dependency installs it here" in out
    for ignore in ("colorama", "colorama:linux-x86_64"):  # and the note doesn't name it then
        code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--ignore", ignore)
        assert code == 0 and "Poetry 1.x" not in err, err


def test_poetry_1x_lock_without_its_pyproject_answers_the_rest(capsys, tmp_path, fake_pypi):
    # pywin32 has Windows wheels only, and only app-lib's Windows marker installs it: maybe a direct dependency too.
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-lib"\nversion = "1.0"\nfiles = [{ file = "app_lib-1.0-py3-none-any.whl" }]\n\n'
        '[package.dependencies]\npywin32 = {version = ">=300", markers = "sys_platform == \\"win32\\""}\n'
        'nowheels = "*"\n\n'
        '[[package]]\nname = "pywin32"\nversion = "306"\n'
        'files = [{ file = "pywin32-306-cp313-cp313-win_amd64.whl" }]\n\n'
        '[[package]]\nname = "nowheels"\nversion = "0.9"\nfiles = [{ file = "nowheels-0.9.tar.gz" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--json")
    assert code == 1, err
    rows = {p["name"]: p for p in json.loads(out)["packages"]}
    assert rows["pywin32"]["results"] == {"linux-x86_64": "unknown", "win-amd64": "wheel"}
    assert list(rows["pywin32"]["unknown_reasons"]) == ["linux-x86_64"]
    assert rows["nowheels"]["results"] == {"linux-x86_64": "source", "win-amd64": "source"}
    assert "unknown_reasons" not in rows["nowheels"]
    # The Python API gives the same answer and reason.
    lin, win = wheelgap.make_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    results = {r.name: r for r in wheelgap.check_lockfile(tmp_path / "poetry.lock", [lin, win])}
    assert results["pywin32"].statuses == {lin: Status.UNKNOWN, win: Status.WHEEL}
    assert "pyproject.toml beside the lock is needed" in results["pywin32"].unknown_reasons[lin]
    assert results["pywin32"].to_dict()["unknown_reasons"] == {
        "3.13 linux-x86_64": results["pywin32"].unknown_reasons[lin]
    }


def test_poetry_1x_lock_without_its_pyproject_names_at_most_five_packages(capsys, tmp_path, fake_pypi):
    names = [f"nvidia-lib{i}" for i in range(7)]
    lock = '[[package]]\nname = "torch"\nversion = "2"\nfiles = [{ file = "torch-2-py3-none-any.whl" }]\n\n'
    lock += "[package.dependencies]\n" + "".join(
        f'{n} = {{version = "*", markers = "sys_platform == \\"linux\\""}}\n' for n in names
    )
    wheel = "{}-1-py3-none-manylinux_2_17_x86_64.whl"
    lock += "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "1"\nfiles = [{{ file = "{wheel.format(n.replace("-", "_"))}" }}]\n'
        for n in names
    )
    (tmp_path / "poetry.lock").write_text(lock + '\n[metadata]\nlock-version = "2.0"\n', encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64")
    assert code == 1
    assert ": nvidia-lib0, nvidia-lib1, nvidia-lib2, nvidia-lib3, nvidia-lib4 and 2 more are `unknown`" in err


@pytest.mark.parametrize(
    ("pyproject", "why"),
    [
        ("[tool.ruff]\nline-length = 100\n", "the pyproject.toml beside poetry.lock declares no dependencies"),
        ("[tool.poetry\n", "the pyproject.toml beside poetry.lock can't be read"),
    ],
)
def test_poetry_1x_lock_with_a_pyproject_that_declares_nothing_is_unknown_too(tmp_path, fake_pypi, pyproject, why):
    (tmp_path / "poetry.lock").write_text(POETRY_1X_CLICK_COLORAMA, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    # The Python API: the command line refuses a pyproject.toml that isn't TOML for its [tool.wheelgap] table first.
    (lin,) = wheelgap.make_targets(["3.13"], ["linux-x86_64"])
    results = {r.name: r for r in wheelgap.check_lockfile(tmp_path / "poetry.lock", [lin])}
    assert results["colorama"].statuses == {lin: Status.UNKNOWN}  # a [tool.ruff]-only one used to give ✓
    assert results["colorama"].unknown_reasons[lin].endswith(f"({why})")


def test_poetry_1x_lock_with_its_pyproject_checks_direct_dependencies_everywhere(capsys, tmp_path, fake_pypi):
    (tmp_path / "poetry.lock").write_text(POETRY_1X_CLICK_COLORAMA, encoding="utf-8")
    pyproject = '[tool.poetry.dependencies]\npython = "^3.11"\nclick = "^8.1"\ncolorama = "^0.4"\n'
    (tmp_path / "pyproject.toml").write_bytes(b"\xef\xbb\xbf" + pyproject.encode())  # a BOM is still readable
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1, err
    (row,) = [p for p in json.loads(out)["packages"] if p["name"] == "colorama"]
    assert row["results"]["linux-x86_64"] == "source" and "unknown_reasons" not in row
    assert "note:" not in err


def test_poetry_1x_lock_without_its_pyproject_is_checked_when_nothing_left_out_has_a_gap(capsys, tmp_path, fake_pypi):
    # colorama has a pure wheel: needed on Linux or not, the answer is the same.
    lock = POETRY_1X_CLICK_COLORAMA.replace("colorama-0.4.6.tar.gz", "colorama-0.4.6-py2.py3-none-any.whl")
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64")
    assert code == 0 and "note:" not in err, err


def test_poetry_1x_lock_without_its_pyproject_counts_another_installed_version(capsys, tmp_path, fake_pypi):
    # On 3.12, app-lib installs numpy 1.26.4 (with a wheel); the left-out 1.24.4 isn't another package to doubt.
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-lib"\nversion = "1.0"\nfiles = [{ file = "app_lib-1.0-py3-none-any.whl" }]\n\n'
        '[package.dependencies]\nnumpy = [{version = "1.24.4", markers = "python_version < \\"3.9\\""}, '
        '{version = ">=1.26", markers = "python_version >= \\"3.9\\""}]\n\n'
        '[[package]]\nname = "numpy"\nversion = "1.24.4"\nfiles = [{ file = "numpy-1.24.4.tar.gz" }]\n\n'
        '[[package]]\nname = "numpy"\nversion = "1.26.4"\n'
        'files = [{ file = "numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64", "--json")
    assert code == 0 and "note:" not in err, err
    assert {(p["name"], p["version"]): p["results"] for p in json.loads(out)["packages"]} == {
        ("app-lib", "1.0"): {"linux-x86_64": "wheel"},
        ("numpy", "1.26.4"): {"linux-x86_64": "wheel"},
    }


def test_a_failing_via_hint_never_costs_the_check(capsys, tmp_path, fake_pypi, monkeypatch):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "mcp"\nversion = "1"\n'
        'files = [{ file = "mcp-1-py3-none-any.whl" }]\n'
        '[package.dependencies]\ncryptography = "*"\n\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'files = [{ file = "cryptography-50.0.2.tar.gz" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )

    def broken(*args, **kwargs):
        raise KeyError("a lockfile shape the hint code didn't expect")

    monkeypatch.setattr("wheelgap.check.direct_dependencies", broken)
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography" in out and "(via" not in out


def test_gap_directly_required_is_not_labelled_via(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path)
    lock = tmp_path / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            'dependencies = [{ name = "mcp" }]',
            'dependencies = [{ name = "mcp" }, { name = "cryptography" }]',
        ),
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and "cryptography" in out and "cryptography (via" not in out
    assert fake_pypi == []


def test_selected_uv_extra_is_attributed_only_to_the_direct_requester(capsys, tmp_path, fake_pypi):
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        '[[package]]\nname = "app"\nversion = "1"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "mcp" }, { name = "plain" }]\n'
        '[[package]]\nname = "mcp"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared", extra = ["crypto"] }]\n'
        'wheels = [{ url = "https://files.example/mcp-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "plain"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared" }]\n'
        'wheels = [{ url = "https://files.example/plain-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "shared"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/shared-1-py3-none-any.whl" }]\n'
        '[package.optional-dependencies]\ncrypto = [{ name = "cryptography" }]\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "--no-dev", "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via mcp)" in out
    assert "cryptography (via mcp +1)" not in out
    assert fake_pypi == []


def test_default_uv_extra_is_attributed_only_to_the_direct_requester(capsys, tmp_path, fake_pypi):
    # With no --extra/--group/--no-dev the hint walks extras per owner too: shared[crypto] is only mcp's.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        '[[package]]\nname = "app"\nversion = "1"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "plain" }, { name = "mcp" }]\n'
        '[[package]]\nname = "mcp"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared", extra = ["crypto"] }]\n'
        'wheels = [{ url = "https://files.example/mcp-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "plain"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared" }]\n'
        'wheels = [{ url = "https://files.example/plain-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "shared"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/shared-1-py3-none-any.whl" }]\n'
        '[package.optional-dependencies]\ncrypto = [{ name = "cryptography" }]\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via mcp)" in out
    assert "via plain" not in out and "(via mcp +1)" not in out
    assert fake_pypi == []


def test_whole_lock_hints_gap_reached_through_second_member_extra(capsys, tmp_path, fake_pypi):
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[manifest]\n"
        'members = ["second"]\n'
        "[[package]]\n"
        'name = "app"\n'
        'version = "1"\n'
        'source = { editable = "." }\n'
        "[[package]]\n"
        'name = "second"\n'
        'version = "1"\n'
        'source = { editable = "packages/second" }\n'
        "[package.optional-dependencies]\n"
        'extra = [{ name = "cryptography" }]\n'
        "[[package]]\n"
        'name = "cryptography"\n'
        'version = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via second)" in out
    assert fake_pypi == []
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--explain")
    assert code == 1 and "via: second → cryptography" in out
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--no-dev", "--offline")
    assert code == 0 and "cryptography" not in out


def test_whole_lock_hints_gap_reached_through_non_default_group(capsys, tmp_path, fake_pypi):
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "app"\n'
        'version = "1"\n'
        'source = { editable = "." }\n'
        "[package.dev-dependencies]\n"
        'lint = [{ name = "tool" }]\n'
        "[[package]]\n"
        'name = "tool"\n'
        'version = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "cryptography" }]\n'
        'wheels = [{ url = "https://files.example/tool-1-py3-none-any.whl" }]\n'
        "[[package]]\n"
        'name = "cryptography"\n'
        'version = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via tool)" in out
    assert fake_pypi == []


def test_whole_lock_hints_gap_in_second_member_group_name_the_group_dependency(capsys, tmp_path, fake_pypi):
    # --explain starts a group's path at the group, not the member, so the hint names the group's dependency.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[manifest]\n"
        'members = ["second"]\n'
        "[[package]]\n"
        'name = "app"\n'
        'version = "1"\n'
        'source = { editable = "." }\n'
        "[[package]]\n"
        'name = "second"\n'
        'version = "1"\n'
        'source = { editable = "packages/second" }\n'
        "[package.dev-dependencies]\n"
        'lint = [{ name = "tool" }]\n'
        "[[package]]\n"
        'name = "tool"\n'
        'version = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "cryptography" }]\n'
        'wheels = [{ url = "https://files.example/tool-1-py3-none-any.whl" }]\n'
        "[[package]]\n"
        'name = "cryptography"\n'
        'version = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via tool)" in out and "via second" not in out
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--explain")
    assert code == 1 and "via: (lint group) → tool → cryptography" in out
    assert fake_pypi == []


def test_whole_lock_explain_path_goes_through_the_edge_that_asks_for_the_extra(capsys, tmp_path, fake_pypi):
    # app needs alpha and beta; only beta asks for alpha[x], which pulls in cryptography. The path must go through beta.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "app"\n'
        'version = "1"\n'
        'source = { editable = "." }\n'
        'dependencies = [{ name = "alpha" }, { name = "beta" }]\n'
        "[[package]]\n"
        'name = "alpha"\n'
        'version = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/alpha-1-py3-none-any.whl" }]\n'
        "[package.optional-dependencies]\n"
        'x = [{ name = "cryptography" }]\n'
        "[[package]]\n"
        'name = "beta"\n'
        'version = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "alpha", extra = ["x"] }]\n'
        'wheels = [{ url = "https://files.example/beta-1-py3-none-any.whl" }]\n'
        "[[package]]\n"
        'name = "cryptography"\n'
        'version = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--explain")
    assert code == 1 and not err
    assert "cryptography (via beta)" in out
    assert "via: app → beta → alpha → cryptography" in out
    assert fake_pypi == []


def test_whole_lock_hints_in_virtual_workspace_name_the_direct_dependency(capsys, tmp_path, fake_pypi):
    # With no project at the root, every member is where uv sync starts, so hints name the dependency, not the member.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[manifest]\n"
        'members = ["api"]\n'
        "[[package]]\n"
        'name = "api"\n'
        'version = "1"\n'
        'source = { editable = "packages/api" }\n'
        'dependencies = [{ name = "tool" }]\n'
        "[[package]]\n"
        'name = "tool"\n'
        'version = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "cryptography" }]\n'
        'wheels = [{ url = "https://files.example/tool-1-py3-none-any.whl" }]\n'
        "[[package]]\n"
        'name = "cryptography"\n'
        'version = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert code == 1 and not err
    assert "cryptography (via tool)" in out and "via api" not in out
    assert fake_pypi == []


def test_poetry_extra_edge_credits_nobody(capsys, tmp_path, fake_pypi):
    # Poetry's graph doesn't record which parent asked for an extra, so a package behind one gets no (via ...) hint.
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "plain"\nversion = "1"\n'
        'files = [{ file = "plain-1-py3-none-any.whl" }]\n'
        '[package.dependencies]\nshared = "*"\n\n'
        '[[package]]\nname = "shared"\nversion = "1"\n'
        'files = [{ file = "shared-1-py3-none-any.whl" }]\n'
        '[package.dependencies]\ncryptography = { version = "*", optional = true, markers = \'extra == "crypto"\' }\n\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'files = [{ file = "cryptography-50.0.2.tar.gz" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline")
    assert "via plain" not in out


def test_explain_issues_url_once_per_release(capsys, fake_pypi):
    args = ("gapwithissues==1.0", "-p", "3.12,3.13", "-t", "linux-x86_64,linux-aarch64", "--explain")
    code, out, _ = run(capsys, *args)
    assert code == 1
    assert out.count("ask for a wheel: https://github.com/example/gapwithissues/issues") == 1


def test_explain_issues_url_survives_the_cache_with_two_pythons(capsys, fake_pypi):
    # With several Pythons the metadata lands on per-Python copies; the cached rerun must still show the link.
    args = ("gapwithissues==1.0", "-p", "3.12,3.13", "-t", "linux-x86_64", "--explain")
    for _ in range(2):  # the second run reads the pinned release from the lookup cache
        code, out, _err = run(capsys, *args)
        assert code == 1
        assert "ask for a wheel: https://github.com/example/gapwithissues/issues" in out


def _direct(capsys, *args):
    code, out, err = run(capsys, *args, "--offline", "--json", "--explain")
    assert code == 1, err
    return {p["name"]: p["direct"] for p in json.loads(out)["packages"]}


def test_json_explain_direct_names_the_direct_dependency(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path)
    direct = _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert direct["cryptography"] == {"linux-x86_64": ["mcp"]}
    assert direct["mcp"] == {}  # no gap
    assert fake_pypi == []


def test_json_explain_direct_lists_every_direct_dependency_that_leads_to_it(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path, several=True)
    direct = _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert sorted(direct["cryptography"]["linux-x86_64"]) == ["click", "mcp", "requests"]


def test_json_explain_direct_is_per_target_and_gap_targets_only(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path, marked=True)
    direct = _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--no-dev")
    assert {k: sorted(v) for k, v in direct["cryptography"].items()} == {
        "linux-x86_64": ["mcp"],
        "win-amd64": ["mcp", "platformer"],
    }
    # A target where the package has a wheel gets no entry, like `via`.
    lock = tmp_path / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
            'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n'
            'wheels = [{ url = "https://files.example/cryptography-50.0.2-cp313-cp313-win_amd64.whl" }]\n',
        ),
        encoding="utf-8",
    )
    direct = _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--no-dev")
    assert direct["cryptography"] == {"linux-x86_64": ["mcp"]}


def test_json_explain_direct_is_empty_for_a_direct_dependency(capsys, tmp_path, fake_pypi):
    _write_transitive_table_lock(tmp_path)
    lock = tmp_path / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            'dependencies = [{ name = "mcp" }]',
            'dependencies = [{ name = "mcp" }, { name = "cryptography" }]',
        ),
        encoding="utf-8",
    )
    assert _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["cryptography"] == {}


def test_json_explain_direct_from_poetry_1x(capsys, tmp_path, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "mcp"\nversion = "1"\n'
        'files = [{ file = "mcp-1-py3-none-any.whl" }]\n'
        '[package.dependencies]\ncryptography = "*"\n\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'files = [{ file = "cryptography-50.0.2.tar.gz" }]\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    direct = _direct(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert direct["cryptography"] == {"linux-x86_64": ["mcp"]}


def test_json_explain_direct_extra_edge_is_only_the_requesters(capsys, tmp_path, fake_pypi):
    # shared[crypto] pulls cryptography in for mcp only; plain needs shared without the extra.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        '[[package]]\nname = "app"\nversion = "1"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "mcp" }, { name = "plain" }]\n'
        '[[package]]\nname = "mcp"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared", extra = ["crypto"] }]\n'
        'wheels = [{ url = "https://files.example/mcp-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "plain"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "shared" }]\n'
        'wheels = [{ url = "https://files.example/plain-1-py3-none-any.whl" }]\n'
        '[[package]]\nname = "shared"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.example/shared-1-py3-none-any.whl" }]\n'
        '[package.optional-dependencies]\ncrypto = [{ name = "cryptography" }]\n'
        '[[package]]\nname = "cryptography"\nversion = "50.0.2"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cryptography-50.0.2.tar.gz" }\n',
        encoding="utf-8",
    )
    direct = _direct(capsys, str(tmp_path), "--no-dev", "-p", "3.13", "-t", "linux-x86_64")
    assert direct["cryptography"] == {"linux-x86_64": ["mcp"]}


def test_json_explain_direct_without_a_graph_and_without_explain(capsys, tmp_path, fake_pypi):
    code, out, _ = run(capsys, "nowheels", "-p", "3.13", "-t", "linux-x86_64", "--json", "--explain")
    assert code == 1
    assert json.loads(out)["packages"][0]["direct"] == {}
    _write_transitive_table_lock(tmp_path)
    code, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--json")
    assert code == 1
    assert all("direct" not in p for p in json.loads(out)["packages"])


SCRIPT_METADATA = '# /// script\n# dependencies = ["nowheels==0.9"]\n# requires-python = ">=3.13"\n# ///\n'


def test_script_input_checks_dependencies(capsys, fake_pypi, tmp_path):
    script = tmp_path / "example.py"
    script.write_text(SCRIPT_METADATA + 'print("hello")\n', encoding="utf-8")
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 1
    assert "nowheels" in out.lower()
    assert "direct dependencies only" in err
    assert "add --deps" in err


@pytest.mark.parametrize(
    "text, error",
    [
        ('print("hello")\n', "no # /// script block"),
        (SCRIPT_METADATA + "\n" + SCRIPT_METADATA, "more than one # /// script block"),
        ("# /// script\n# dependencies = [\n# ///\n", "invalid TOML in the # /// script block"),
        ("# /// script\n# dependencies = 123\n# ///\n", "the # /// script block's dependencies must be a list"),
    ],
)
def test_script_input_errors(capsys, fake_pypi, tmp_path, text, error):
    script = tmp_path / "example.py"
    script.write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert error in err
    assert "[project]" not in err and "pyproject.toml" not in err
    assert fake_pypi == []


def test_script_block_text_inside_another_block_isnt_read(capsys, fake_pypi, tmp_path):
    # The specification's regex matches whole blocks: a `# /// script` line in another block's content is content.
    script = tmp_path / "example.py"
    script.write_text(
        "# /// example\n"
        "# This is how a script declares its dependencies:\n"
        "# /// script\n"
        '# dependencies = ["private-name"]\n'
        "# ///\n"
        "# ///\n"
        "\n" + SCRIPT_METADATA,
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 1
    assert "nowheels" in out.lower()
    assert all("private-name" not in url for url in fake_pypi)


def test_script_with_a_bom_is_read(capsys, fake_pypi, tmp_path):
    script = tmp_path / "example.py"
    script.write_bytes(b"\xef\xbb\xbf" + SCRIPT_METADATA.encode("utf-8"))
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 1
    assert "nowheels" in out.lower()


def test_script_that_isnt_utf8_is_an_input_error(capsys, fake_pypi, tmp_path):
    script = tmp_path / "example.py"
    script.write_bytes(SCRIPT_METADATA.encode("utf-8") + "# café\n".encode("latin-1"))
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert "isn't UTF-8" in err
    assert "Traceback" not in err
    assert fake_pypi == []


@pytest.mark.parametrize(
    "flag", [["--group", "dev"], ["--only-group", "dev"], ["--no-dev"], ["--extra", "test"], ["--all-extras"]]
)
def test_script_refuses_extras_and_groups(capsys, fake_pypi, tmp_path, flag):
    script = tmp_path / "example.py"
    script.write_text(SCRIPT_METADATA, encoding="utf-8")
    code, out, err = run(capsys, str(script), *flag, "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert "don't apply to a script" in err
    assert "pyproject.toml" not in err
    assert fake_pypi == []


def test_script_input_requires_python_note(capsys, fake_pypi, tmp_path):
    script = tmp_path / "example.py"
    script.write_text(SCRIPT_METADATA, encoding="utf-8")
    code, out, err = run(capsys, str(script), "-p", "3.12", "-p", "3.13", "-t", "win-arm64")
    assert code == 1
    assert "requires Python >=3.13, which leaves out Python 3.12" in err


def test_script_input_private_index_is_not_pypi_only(tmp_path):
    from wheelgap.sources import load_file

    script = tmp_path / "example.py"
    script.write_text(
        SCRIPT_METADATA.replace("# ///\n", '# [[tool.uv.index]]\n# url = "https://private.example/simple"\n# ///\n'),
        encoding="utf-8",
    )
    assert not load_file(script).pypi_only


def test_missing_py_name_is_still_a_package(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "discord.py", "-p", "3.13", "-t", "win-arm64")
    assert code == 1  # not on the fake index: unknown, not an input error
    assert "https://pypi.org/pypi/discord-py/json" in fake_pypi


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_script_metadata_ignores_unclosed_and_other_blocks(capsys, fake_pypi, tmp_path, newline):
    script = tmp_path / "example.py"
    text = (
        '# /// other\n# dependencies = ["private-name"]\n# ///\n\n' + SCRIPT_METADATA + "\n# /// script\n# incomplete"
    )
    script.write_bytes(text.replace("\n", newline).encode("utf-8"))
    code, out, err = run(capsys, str(script), "-p", "3.13", "-t", "win-arm64")
    assert code == 1
    assert "nowheels" in out.lower()
    assert all("private-name" not in url for url in fake_pypi)


def test_script_deps_refuses_private_index(capsys, fake_pypi, tmp_path):
    script = tmp_path / "example.py"
    script.write_text(
        SCRIPT_METADATA.replace("# ///\n", '# [[tool.uv.index]]\n# url = "https://private.example/simple"\n# ///\n'),
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(script), "--deps", "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert "the script configures an installer index or source; --deps uses only PyPI" in err
    assert "--deps uses only PyPI, so check its lock (uv lock --script) instead" in err
    assert fake_pypi == []


@pytest.mark.parametrize("script", [False, True])
@pytest.mark.parametrize("cutoff", ["2025-01-01", "2025-01-02T00:00:00Z"])
def test_project_exclude_newer_limits_range_resolution(fake_pypi, capsys, tmp_path, monkeypatch, script, cutoff):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(
        FAKE_INDEX,
        "dated",
        {
            "1.0": [{"filename": "dated-1.0-py3-none-any.whl", "upload_time_iso_8601": "2024-01-01T00:00:00Z"}],
            "2.0": [{"filename": "dated-2.0.tar.gz", "upload_time_iso_8601": "2025-02-01T00:00:00Z"}],
        },
    )
    if script:
        path = tmp_path / "example.py"
        path.write_text(
            f'# /// script\n# dependencies = ["dated>=1"]\n# [tool.uv]\n# exclude-newer = "{cutoff}"\n# ///\n',
            encoding="utf-8",
        )
    else:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            f'[project]\ndependencies = ["dated>=1"]\n[tool.uv]\nexclude-newer = "{cutoff}"\n', encoding="utf-8"
        )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 0
    assert json.loads(out)["packages"][0]["version"] == "1.0"


@pytest.mark.parametrize(
    "value",
    [
        '"1 month"',  # uv refuses months and years: they have no fixed length
        '"P1Y"',
        '"3 fortnights"',
        '"3 dayz"',
        '" 3 days"',
        '"1 day and 2 hours"',
        '"5000000 days"',  # further back than uv's earliest timestamp
        '"2025-02-30"',
        '"2025-01-01T00:00:00"',
        "123",
        "true",
        '{ timestamp = "2025-01-01T00:00:00Z", span = "P3D" }',  # uv's table form, which wheelgap doesn't read
    ],
)
def test_project_exclude_newer_rejects_invalid_cutoffs(fake_pypi, capsys, tmp_path, value):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\ndependencies = ["nowheels"]\n[tool.uv]\nexclude-newer = {value}\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert f"{path}: [tool.uv] exclude-newer must be a date, an RFC 3339 timestamp, false or a duration: " in err
    assert fake_pypi == []


RELEASED = {  # 2.0 came out on 2025-02-01; the tests below run "now" a few days later
    "1.0": [{"filename": "dated-1.0-py3-none-any.whl", "upload_time_iso_8601": "2024-01-01T00:00:00Z"}],
    "2.0": [{"filename": "dated-2.0-py3-none-any.whl", "upload_time_iso_8601": "2025-02-01T00:00:00Z"}],
}


@pytest.fixture
def now_is(monkeypatch):
    """Set the time uv's relative exclude-newer durations count back from."""
    from datetime import datetime

    from wheelgap import cutoff

    def set_now(text):
        monkeypatch.setattr(cutoff, "current_time", lambda: datetime.fromisoformat(text))

    return set_now


@pytest.mark.parametrize("script", [False, True])
@pytest.mark.parametrize(
    "duration, version, cutoff",
    [
        ("3 days", "1.0", "2025-01-31T00:00:00Z"),
        ("P3D", "1.0", "2025-01-31T00:00:00Z"),
        ("1 week", "1.0", "2025-01-27T00:00:00Z"),
        ("1 day", "2.0", "2025-02-02T00:00:00Z"),
        ("24 hours", "2.0", "2025-02-02T00:00:00Z"),
        ("PT36H", "2.0", "2025-02-01T12:00:00Z"),
        ("2 days", "1.0", "2025-02-01T00:00:00Z"),  # a file uploaded at the cutoff itself is left out, as by uv
        ("47h 59m 59.999s", "2.0", "2025-02-01T00:00:00.001000Z"),
        ("-3 days", "1.0", "2025-01-31T00:00:00Z"),  # uv takes the duration's absolute value
        ("3 days ago", "1.0", "2025-01-31T00:00:00Z"),
        ("0 days", "2.0", "2025-02-03T00:00:00Z"),
    ],
)
def test_project_exclude_newer_duration_counts_back_from_now(
    fake_pypi, capsys, tmp_path, monkeypatch, now_is, script, duration, version, cutoff
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", RELEASED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    now_is("2025-02-03T00:00:00+00:00")
    if script:
        path = tmp_path / "example.py"
        text = f'# /// script\n# dependencies = ["dated>=1"]\n# [tool.uv]\n# exclude-newer = "{duration}"\n# ///\n'
    else:
        path = tmp_path / "pyproject.toml"
        text = f'[project]\ndependencies = ["dated>=1"]\n[tool.uv]\nexclude-newer = "{duration}"\n'
    path.write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 0
    assert json.loads(out)["packages"][0]["version"] == version
    assert (
        f"wheelgap: note: {path.name}: a duration counts back from the time of this run, as uv counts it back from "
        f'the time it resolves: exclude-newer = "{duration}" is {cutoff}\n'
    ) in err
    # -q drops the note, not the cutoff.
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json", "-q")
    assert json.loads(out)["packages"][0]["version"] == version and "a duration counts back" not in err


def test_project_exclude_newer_duration_moves_with_the_clock(fake_pypi, capsys, tmp_path, monkeypatch, now_is):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", RELEASED)
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\ndependencies = ["dated"]\n[tool.uv]\nexclude-newer = "1 week"\n', encoding="utf-8")
    versions = []
    for now in ("2025-02-07T23:59:59+00:00", "2025-02-08T00:00:00+00:00", "2025-02-08T00:00:00.001+00:00"):
        now_is(now)
        code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
        versions.append(json.loads(out)["packages"][0]["version"])
    assert versions == ["1.0", "1.0", "2.0"]  # 2.0 is a week old one millisecond after 2025-02-08T00:00:00Z


@pytest.mark.parametrize(
    "settings, version, notes",
    [
        ('exclude-newer = "1 week"\nexclude-newer-package = { Dated = "1 day" }', "2.0", 2),
        ('exclude-newer = "1 day"\nexclude-newer-package = { dated = "P1W" }', "1.0", 2),
        ('exclude-newer = "2025-03-01"\nexclude-newer-package = { dated = "1 week" }', "1.0", 1),
        ('exclude-newer = "1 week"\nexclude-newer-package = { dated = false }', "2.0", 1),
        ('exclude-newer = "1 week"\nexclude-newer-package = { other = "1 day" }', "1.0", 2),
        ("exclude-newer = false", "2.0", 0),  # uv: no cutoff, whatever the user's own settings say
        ('exclude-newer = false\nexclude-newer-package = { dated = "30 days" }', "1.0", 1),
    ],
)
def test_project_exclude_newer_durations_per_package(
    fake_pypi, capsys, tmp_path, monkeypatch, now_is, settings, version, notes
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", RELEASED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    now_is("2025-02-03T00:00:00+00:00")
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\ndependencies = ["dated"]\n[tool.uv]\n{settings}\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 0 and json.loads(out)["packages"][0]["version"] == version
    assert err.count("a duration counts back") == bool(notes) and err.count('" is 2025-') == notes


def test_exclude_newer_duration_in_a_uv_toml_beside_it(fake_pypi, capsys, tmp_path, monkeypatch, now_is):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", RELEASED)
    now_is("2025-02-03T00:00:00+00:00")
    (tmp_path / "uv.toml").write_text('exclude-newer = "3 days"\n', encoding="utf-8")
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\ndependencies = ["dated"]\n[tool.uv]\nexclude-newer = "1 day"\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 0 and json.loads(out)["packages"][0]["version"] == "1.0"
    assert 'exclude-newer = "3 days" is 2025-01-31T00:00:00Z' in err


DATED = {
    "1.0": [
        {"filename": "dated-1.0.tar.gz", "upload_time_iso_8601": "2024-01-01T00:00:00Z"},
        # A wheel added to 1.0 after the cutoff, as projects do for a new Python: uv leaves it out.
        {"filename": "dated-1.0-cp313-cp313-win_arm64.whl", "upload_time_iso_8601": "2025-02-01T00:00:00Z"},
    ],
    "2.0": [{"filename": "dated-2.0-py3-none-any.whl", "upload_time_iso_8601": "2025-02-01T00:00:00Z"}],
}


@pytest.mark.parametrize(
    "requirement, result, error",
    [
        ("dated", "source", None),
        ("dated==1.0", "source", None),  # an exact pin too: uv filters every file, pinned or not
        ("dated==2.0", "unknown", "2.0 was uploaded after exclude-newer (2025-01-01T00:00:00Z), so uv won't install"),
    ],
)
def test_project_exclude_newer_applies_to_exact_pins(
    fake_pypi, capsys, tmp_path, monkeypatch, requirement, result, error
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[project]\ndependencies = ["{requirement}"]\n[tool.uv]\nexclude-newer = "2025-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 1
    package = json.loads(out)["packages"][0]
    assert package["results"] == {"win-arm64": result}
    assert (error in package["error"]) if error else not package.get("error")


@pytest.mark.parametrize(
    "settings, expected",
    [
        ('exclude-newer = "2025-01-01T00:00:00Z"\nexclude-newer-package = { dated = false }', "2.0"),
        ('exclude-newer = "2025-01-01T00:00:00Z"\nexclude-newer-package = { Dated = "2025-03-01" }', "2.0"),
        ('exclude-newer = "2025-03-01"\nexclude-newer-package = { dated = "2025-01-01T00:00:00Z" }', "1.0"),
        ('exclude-newer-package = { dated = "2025-01-01T00:00:00Z" }', "1.0"),
        ('exclude-newer-package = { other = "2025-01-01T00:00:00Z" }', "2.0"),
    ],
)
def test_project_exclude_newer_package_overrides_the_cutoff(
    fake_pypi, capsys, tmp_path, monkeypatch, settings, expected
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\ndependencies = ["dated"]\n[tool.uv]\n{settings}\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == expected
    # Only the --deps note mentions the cutoff: nothing about the settings themselves.
    assert "the newest matching release uploaded before exclude-newer" in err
    assert err.count("exclude-newer") == 1


@pytest.mark.parametrize(
    "settings",
    [
        'exclude-newer-package = { dated = "7 months" }',
        'exclude-newer-package = { dated = "7 dys" }',
        "exclude-newer-package = { dated = true }",
        'exclude-newer-package = "2025-01-01"',
        'exclude-newer-package = { dated = "2025-01-01", Dated = "2025-01-02" }',
    ],
)
def test_project_exclude_newer_package_rejects_what_it_cannot_read(fake_pypi, capsys, tmp_path, settings):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\ndependencies = ["dated"]\n[tool.uv]\n{settings}\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert "exclude-newer-package" in err
    assert fake_pypi == []


def test_project_exclude_newer_is_read_from_a_uv_toml_beside_it(fake_pypi, capsys, tmp_path, monkeypatch):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    # uv reads a uv.toml beside pyproject.toml in place of its [tool.uv].
    (tmp_path / "uv.toml").write_text('exclude-newer = "2025-01-01T00:00:00Z"\n', encoding="utf-8")
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\ndependencies = ["dated"]\n[tool.uv]\nexclude-newer = "2025-03-01T00:00:00Z"\n', encoding="utf-8"
    )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == "1.0"


@pytest.mark.parametrize("script", [False, True])
def test_exclude_newer_wheelgap_does_not_read_is_a_note(fake_pypi, capsys, tmp_path, monkeypatch, script):
    (tmp_path / "uv.toml").write_text('exclude-newer = "2025-01-01T00:00:00Z"\n', encoding="utf-8")
    member = tmp_path / "member"
    member.mkdir()
    if script:
        path = member / "example.py"
        path.write_text('# /// script\n# dependencies = ["purewheel"]\n# ///\n', encoding="utf-8")
    else:
        path = member / "pyproject.toml"
        path.write_text('[project]\ndependencies = ["purewheel"]\n', encoding="utf-8")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "2025-01-01")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "-q")
    assert code == 0
    applies = (
        "which uv may apply here too; wheelgap reads only the script's own block"
        if script
        else "which uv applies here, as this pyproject.toml has no uv settings of its own"
    )
    assert f"{(tmp_path / 'uv.toml').resolve()} sets exclude-newer, {applies}" in err
    assert "UV_EXCLUDE_NEWER is set" in err


def test_project_with_own_uv_settings_gets_no_note_about_a_folder_above(fake_pypi, capsys, tmp_path, monkeypatch):
    # uv reads the nearest settings only: a project with a [tool.uv] of its own never takes the folder above's.
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    (tmp_path / "uv.toml").write_text('exclude-newer = "2025-01-01T00:00:00Z"\n', encoding="utf-8")
    (tmp_path / "app").mkdir()
    path = tmp_path / "app" / "pyproject.toml"
    path.write_text('[project]\ndependencies = ["purewheel"]\n[tool.uv]\npackage = false\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "-q")
    assert code == 0
    assert "exclude-newer" not in err


def test_project_uv_toml_beside_it_that_cannot_be_read_is_an_error(fake_pypi, capsys, tmp_path, monkeypatch):
    (tmp_path / "uv.toml").write_text("", encoding="utf-8")
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\ndependencies = ["purewheel"]\n', encoding="utf-8")
    read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self.name == "uv.toml":
            raise PermissionError(13, "Permission denied")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    assert f"{tmp_path / 'uv.toml'}: can't be read" in err
    assert fake_pypi == []


WORKSPACE_ROOT = '[project]\nname = "root"\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'


def _workspace_member(tmp_path, member_text, root_text=WORKSPACE_ROOT, folder="packages/app"):
    (tmp_path / "pyproject.toml").write_text(root_text, encoding="utf-8")
    member = tmp_path / folder
    member.mkdir(parents=True)
    path = member / "pyproject.toml"
    path.write_text('[project]\nname = "app"\ndependencies = ["dated"]\n' + member_text, encoding="utf-8")
    return path


@pytest.mark.parametrize("beside", [False, True])
def test_workspace_member_exclude_newer_is_ignored_as_uv_does(fake_pypi, capsys, tmp_path, monkeypatch, beside):
    # uv loads a workspace member's settings from the workspace root alone (FilesystemOptions::find on the
    # workspace's install path), so the member's own cutoff, in [tool.uv] or a uv.toml beside it, isn't applied.
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    cutoff = 'exclude-newer = "2025-01-01T00:00:00Z"\n'
    path = _workspace_member(tmp_path, "" if beside else "[tool.uv]\n" + cutoff)
    if beside:
        (path.parent / "uv.toml").write_text(cutoff, encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == "2.0"
    config = path.parent / "uv.toml" if beside else path
    assert (
        f"{config} sets exclude-newer, which uv ignores in a workspace member: it uses the workspace root's "
        f"settings ({tmp_path / 'pyproject.toml'}), and so does wheelgap"
    ) in err
    assert "the newest matching release uploaded before" not in err


@pytest.mark.parametrize("root_uv_toml", [False, True])
@pytest.mark.parametrize("member_text", ["", '[tool.uv]\nexclude-newer = "2025-03-01T00:00:00Z"\n'])
def test_workspace_member_takes_exclude_newer_from_the_root(
    fake_pypi, capsys, tmp_path, monkeypatch, root_uv_toml, member_text
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    # The root's uv.toml is read in place of its [tool.uv], whose later cutoff would let 2.0 in.
    root_cutoff = "2025-03-01T00:00:00Z" if root_uv_toml else "2025-01-01T00:00:00Z"
    root_text = (
        f'[project]\nname = "root"\n[tool.uv]\nexclude-newer = "{root_cutoff}"\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    path = _workspace_member(tmp_path, member_text, root_text)
    if root_uv_toml:
        (tmp_path / "uv.toml").write_text('exclude-newer = "2025-01-01T00:00:00Z"\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == "1.0"
    assert ("which uv ignores in a workspace member" in err) == bool(member_text)
    assert "which uv applies here" not in err and "may apply" not in err


@pytest.mark.parametrize(
    "root_text, folder",
    [
        (WORKSPACE_ROOT, "examples/demo"),  # not in members
        (WORKSPACE_ROOT, "packages/app/nested"),  # `*` stays within one folder name
        (WORKSPACE_ROOT + 'exclude = ["packages/app"]\n', "packages/app"),
        ('[project]\nname = "outer"\n[tool.uv]\nexclude-newer = "2025-03-01T00:00:00Z"\n', "packages/app"),
    ],
)
def test_project_outside_a_workspace_keeps_its_own_exclude_newer(
    fake_pypi, capsys, tmp_path, monkeypatch, root_text, folder
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    path = _workspace_member(tmp_path, '[tool.uv]\nexclude-newer = "2025-01-01T00:00:00Z"\n', root_text, folder)
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == "1.0"
    assert "workspace" not in err and "folder above" not in err


@pytest.mark.parametrize("members", ['["packages/**"]', '["../*/packages/*"]', '"packages/*"'])
def test_workspace_membership_wheelgap_cannot_match_is_refused_with_a_cutoff(
    fake_pypi, capsys, tmp_path, monkeypatch, members
):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(FAKE_INDEX, "dated", DATED)
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    root_text = f'[project]\nname = "root"\n[tool.uv.workspace]\nmembers = {members}\n'
    path = _workspace_member(tmp_path, '[tool.uv]\nexclude-newer = "2025-01-01T00:00:00Z"\n', root_text)
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64")
    assert code == 2
    root = tmp_path / "pyproject.toml"
    assert f"{path}: wheelgap can't tell whether this is a member of the workspace at {root}" in err
    assert fake_pypi == []
    # With no cutoff at the member or the root, membership doesn't change the answer.
    path.write_text('[project]\nname = "app"\ndependencies = ["dated"]\n', encoding="utf-8")
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert json.loads(out)["packages"][0]["version"] == "2.0"


def test_project_exclude_newer_refuses_unknown_upload_time(fake_pypi, capsys, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\ndependencies = ["ranged>=1"]\n[tool.uv]\nexclude-newer = "2025-01-01"\n', encoding="utf-8"
    )
    code, out, err = run(capsys, str(path), "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 1
    package = json.loads(out)["packages"][0]
    assert package["results"] == {"win-arm64": "unknown"}
    assert "no valid upload_time_iso_8601" in package["error"]


def test_project_exclude_newer_applies_to_selected_extra(fake_pypi, capsys, tmp_path, monkeypatch):
    from conftest import FAKE_INDEX

    monkeypatch.setitem(
        FAKE_INDEX,
        "dated",
        {
            "1.0": [{"filename": "dated-1.0-py3-none-any.whl", "upload_time_iso_8601": "2024-01-01T00:00:00Z"}],
            "2.0": [{"filename": "dated-2.0.tar.gz", "upload_time_iso_8601": "2025-02-01T00:00:00Z"}],
        },
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\ndependencies = ["dated>=1"]\n[project.optional-dependencies]\ntest = ["dated>=1"]\n'
        '[tool.uv]\nexclude-newer = "2025-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    code, out, err = run(capsys, str(path), "--extra", "test", "-p", "3.13", "-t", "win-arm64", "--json")
    assert code == 0
    assert [(p["name"], p["version"]) for p in json.loads(out)["packages"]] == [("dated", "1.0")]


LOCKED_PYTHON_FILES = {
    "poetry.lock": ("[[package]]", "python-versions", 'files = [{file = "purewheel-3.1-py3-none-any.whl"}]'),
    # inherit_metadata: the lock records each package's marker, which the range needs (see the test below)
    "pdm.lock": (
        "[[package]]",
        "requires_python",
        'files = [{file = "purewheel-3.1-py3-none-any.whl"}]\n\n[metadata]\nstrategy = ["inherit_metadata"]',
    ),
    "pylock.toml": ("[[packages]]", "requires-python", 'wheels = [{name = "purewheel-3.1-py3-none-any.whl"}]'),
}


@pytest.mark.parametrize("lockfile", LOCKED_PYTHON_FILES)
def test_offline_locked_package_python_range(capsys, tmp_path, fake_pypi, lockfile, monkeypatch):
    from conftest import FAKE_INDEX

    from wheelgap import check_lockfile, make_targets

    table, key, files = LOCKED_PYTHON_FILES[lockfile]
    path = tmp_path / lockfile
    path.write_text(f'{table}\nname = "purewheel"\nversion = "3.1"\n{key} = "<3.13"\n{files}\n', encoding="utf-8")
    args = (str(path), "--offline", "--explain", "-t", "linux-x86_64", "-p", "3.12,3.13")
    code, out, _ = run(capsys, *args, "--json")
    assert code == 1
    packages = json.loads(out)["packages"]
    assert [r["results"] for r in packages] == [
        {"3.12 linux-x86_64": "wheel", "3.13 linux-x86_64": "n/a"},
        {"3.12 linux-x86_64": "n/a", "3.13 linux-x86_64": "missing"},
    ]
    assert packages[1]["rejected"]["3.13 linux-x86_64"] == [
        {"file": "purewheel-3.1-py3-none-any.whl", "reason": "requires Python <3.13"}
    ]
    assert "requires Python <3.13" in run(capsys, *args)[1]
    targets = make_targets(["3.12", "3.13"], "linux-x86_64")
    results = check_lockfile(path, targets, offline=True)
    assert results[0].statuses[targets[0]] is Status.WHEEL
    assert results[1].statuses[targets[1]] is Status.MISSING
    assert fake_pypi == []
    assert run(capsys, str(path), "--online", "--no-cache", "-t", "linux-x86_64", "-p", "3.13")[0] == 0
    monkeypatch.setitem(
        FAKE_INDEX,
        "purewheel",
        {"3.1": [{"filename": "purewheel-3.1-py3-none-any.whl", "requires_python": "<3.13"}]},
    )
    assert run(capsys, str(path), "--online", "--no-cache", "-t", "linux-x86_64", "-p", "3.13")[0] == 1


@pytest.mark.parametrize("order", ["3.13,3.12", "3.12,3.13"])
def test_offline_locked_python_range_without_a_version(capsys, tmp_path, fake_pypi, order):
    """pylock.toml lets a package leave `version` out. Each Python still gets its own answer, whatever the -p order."""
    path = tmp_path / "pylock.toml"
    path.write_text(
        '[[packages]]\nname = "purewheel"\nrequires-python = "<3.13"\n'
        'wheels = [{name = "purewheel-3.1-py3-none-any.whl"}]\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(path), "--offline", "-t", "linux-x86_64", "-p", order, "--json")
    assert code == 1
    statuses = {
        target: status
        for package in json.loads(out)["packages"]
        for target, status in package["results"].items()
        if status != "n/a"
    }
    assert statuses == {"3.12 linux-x86_64": "wheel", "3.13 linux-x86_64": "missing"}
    assert fake_pypi == []


@pytest.mark.parametrize(
    ("lockfile", "key", "metadata"),
    [
        # Before lock-version 2.1, a direct dependency's Python marker is only in pyproject.toml.
        ("poetry.lock", "python-versions", '[metadata]\nlock-version = "2.0"\n'),
        # Without inherit_metadata, a pdm.lock has no markers on its packages.
        ("pdm.lock", "requires_python", ""),
        ("pdm.lock", "requires_python", '[metadata]\nstrategy = ["cross_platform"]\n'),
    ],
)
def test_locked_python_range_needs_markers_on_packages(capsys, tmp_path, fake_pypi, lockfile, key, metadata):
    """Poetry and PDM install a package only on the Pythons its markers select. A lock that doesn't record them per
    package can't say whether the range leaves out a Python the package is installed on, so it's read as before."""
    path = tmp_path / lockfile
    path.write_text(
        f'[[package]]\nname = "purewheel"\nversion = "3.1"\n{key} = "<3.13"\n'
        'files = [{file = "purewheel-3.1-py3-none-any.whl"}]\n\n' + metadata,
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(path), "--offline", "-t", "linux-x86_64", "-p", "3.12,3.13", "--json")
    assert code == 0
    assert [p["results"] for p in json.loads(out)["packages"]] == [
        {"3.12 linux-x86_64": "wheel", "3.13 linux-x86_64": "wheel"}
    ]
    assert fake_pypi == []


@pytest.mark.parametrize("restriction", ["", 'python-versions = "*"', 'python-versions = "^3.12"'])
def test_offline_locked_package_python_range_unrestricted(capsys, tmp_path, fake_pypi, restriction):
    path = tmp_path / "poetry.lock"
    path.write_text(
        '[[package]]\nname = "purewheel"\nversion = "3.1"\n'
        'files = [{file = "purewheel-3.1-py3-none-any.whl"}]\n' + restriction,
        encoding="utf-8",
    )
    assert run(capsys, str(path), "--offline", "-t", "linux-x86_64", "-p", "3.13")[0] == 0
    assert fake_pypi == []


@pytest.mark.parametrize("lockname", ["uv.lock", "app.py.lock"])
@pytest.mark.parametrize("requires_python", [">=3.10,<3.15", "==3.14.*", "~=3.14.0", ">=3.10,!=3.15.*"])
def test_uv_lock_online_uses_python_lower_bound(capsys, tmp_path, fake_pypi, monkeypatch, lockname, requires_python):
    from conftest import FAKE_INDEX

    from wheelgap import check_lockfile, make_targets

    filename = "purewheel-3.1-py3-none-any.whl"
    monkeypatch.setitem(FAKE_INDEX, "purewheel", {"3.1": [{"filename": filename, "requires_python": requires_python}]})
    lock = tmp_path / lockname
    manifest = '[manifest]\nrequirements = [{ name = "purewheel" }]\n' if lockname.endswith(".py.lock") else ""
    lock.write_text(
        'version = 1\nrequires-python = ">=3.12"\n' + manifest + "\n[[package]]\n"
        'name = "purewheel"\nversion = "3.1"\nsource = {registry = "https://pypi.org/simple"}\n'
        f'wheels = [{{url = "https://files.pythonhosted.org/{filename}"}}]\n',
        encoding="utf-8",
    )
    args = (str(lock), "--no-cache", "-p", "3.15", "-t", "linux-x86_64")
    assert run(capsys, *args, "--offline")[0] == 0
    code, out, err = run(capsys, *args, "--online", "--json", "--explain")
    assert code == 0
    _assert_matches_output_schema(json.loads(out))
    package = json.loads(out)["packages"][0]
    assert package["results"] == {"linux-x86_64": "wheel"}
    assert package["pip_refuses"] == {filename: f"requires Python {requires_python}"}
    assert "note: 1 package is checked as uv installs it, though pip would refuse it" in err
    assert (
        "purewheel 3.1 on linux-x86_64: checked as uv installs it (uv checks the Requires-Python lower bound); "
        f"pip refuses it: requires Python {requires_python}"
    ) in run(capsys, *args, "--online", "--explain")[1]
    targets = make_targets("3.15", "linux-x86_64")
    result = check_lockfile(lock, targets, online=True)[0]
    assert result.statuses[targets[0]] is Status.WHEEL
    assert result.to_dict()["pip_refuses"] == package["pip_refuses"]
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("purewheel==3.1\n", encoding="utf-8")
    code, out, _ = run(capsys, str(requirements), *args[1:], "--online", "--json")
    assert code == 1
    assert json.loads(out)["packages"][0]["results"] == {"linux-x86_64": "missing"}


def test_uv_lock_online_python_upper_bound_is_per_python(capsys, tmp_path, fake_pypi, monkeypatch):
    """With several Pythons, only the one pip refuses gets its files back, and only its entry has pip_refuses."""
    from conftest import FAKE_INDEX

    filename = "purewheel-3.1-py3-none-any.whl"
    monkeypatch.setitem(FAKE_INDEX, "purewheel", {"3.1": [{"filename": filename, "requires_python": ">=3.10,<3.15"}]})
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\n'
        'name = "purewheel"\nversion = "3.1"\nsource = {registry = "https://pypi.org/simple"}\n'
        f'wheels = [{{url = "https://files.pythonhosted.org/{filename}"}}]\n',
        encoding="utf-8",
    )
    args = (str(lock), "--no-cache", "--online", "--json", "-p", "3.14,3.15", "-t", "linux-x86_64")
    code, out, err = run(capsys, *args)
    assert code == 0
    data = json.loads(out)
    _assert_matches_output_schema(data)
    assert [(p["results"], p.get("pip_refuses")) for p in data["packages"]] == [
        ({"3.14 linux-x86_64": "wheel", "3.15 linux-x86_64": "n/a"}, None),
        ({"3.14 linux-x86_64": "n/a", "3.15 linux-x86_64": "wheel"}, {filename: "requires Python >=3.10,<3.15"}),
    ]
    assert "note: 1 package is checked as uv installs it" in err
    assert "pip would refuse" not in run(capsys, *args, "-q")[2]


@pytest.mark.parametrize(
    ("requires", "python", "ignores"),
    [
        (">=3.10,<3.15", "3.15", True),
        ("<=3.14", "3.15", True),
        ("<3.15", "3.14", False),
        (">=3.10,<3.15", "3.9", False),
        ("==3.14.*", "3.15", True),
        ("==3.16.*", "3.15", False),
        (">=3.16", "3.15", False),
        ("==3.14.*,<3.15", "3.15", True),
        ("~=3.14.0", "3.15", True),
        ("~=3.16.0", "3.15", False),
        ("~=3.13.0,<3.15", "3.15", True),
        ("!=3.15.*", "3.15", True),
        ("!=3.15.*,<3.15", "3.15", True),
        (">=3.10,!=3.15.*", "3.15", True),
        (">=3.15,!=3.15.*", "3.15", False),
        (">=3.14,!=3.14.*,!=3.15.*", "3.15", False),
        (">=3.14,!=3.14.*,<3.16", "3.15", False),
        (">=3.15.99,!=3.15.99", "3.15", False),
        (">=3.16,<3.12", "3.15", True),
        ("==3.14,!=3.14", "3.15", True),
        # Checked against uv 0.13.9 itself (uv pip install --dry-run on wheels with these Requires-Python).
        ("==3.14.*", "3.13", False),
        ("~=3.14.0", "3.13", False),
        ("~=3.13.0", "3.15", True),
        ("==3.14", "3.15", True),
        ("===3.14", "3.15", True),
        ("==3.15.0", "3.15", True),
        # pip takes these, so there is nothing for uv to restore.
        (">=3.10,!=3.15.*", "3.14", False),
        ("!=3.15.*", "3.14", False),
        ("invalid", "3.15", False),
    ],
)
def test_uv_ignores_python_rejection_when_lower_bound_allows(requires, python, ignores):
    from wheelgap.sources import uv_ignores_python_rejection

    assert uv_ignores_python_rejection(requires, python) is ignores


def test_requirements_file_conflicting_with_constraint_fails_without_lookup(capsys, tmp_path, fake_pypi):
    req = tmp_path / "requirements.txt"
    req.write_text("-c cons.txt\nidna==3.6\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("idna==3.7\n", encoding="utf-8")
    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1
    assert "unknown" in out
    expected = "could not check idna: requirement idna==3.6 conflicts with the constraint idna==3.7 (pip refuses this)"
    assert expected in out
    assert fake_pypi == []


def test_pyproject_non_table_tool_exits_with_source_error(capsys, tmp_path, fake_pypi):
    req = tmp_path / "requirements.txt"
    req.write_text("idna==3.10\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("tool = 1\n", encoding="utf-8")

    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and not out
    assert "[tool] must be a table" in err
    assert "AttributeError" not in err

    code, out, err = run(capsys, str(req), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 2
    payload = json.loads(out)["error"]
    assert set(payload) == {"message", "type"}
    assert "[tool] must be a table" in payload["message"]
    assert payload["type"] == "SourceError"
    assert fake_pypi == []


@pytest.mark.parametrize(
    ("metadata_toml", "expected"),
    [
        ("[package.metadata]\nprovides-extras = [1]\n", "provides-extras must be a list of strings"),
        ("metadata = []\n", "metadata must be a table"),
        ('[package.metadata]\nprovides-extras = "abc"\n', "provides-extras must be a list of strings"),
        ("[package.metadata]\nrequires-dev = []\n", "requires-dev must be a table"),
    ],
)
def test_uv_lock_wrong_type_package_metadata_exits_2_with_json_error(
    capsys, tmp_path, fake_pypi, metadata_toml, expected
):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0.1.0"\n', encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\nrequires-python = ">=3.13"\n\n'
        '[[package]]\nname = "purewheel"\nversion = "3.1"\nsource = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "proj"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "purewheel" }]\n' + metadata_toml,
        encoding="utf-8",
    )

    # Online, plain and with a selection: the lock is refused before anything is looked up.
    for extra in ([], ["--all-extras"], ["--group", "dev"]):
        code, out, err = run(capsys, str(lock), "-p", "3.13", "-t", "linux-x86_64", *extra)
        assert code == 2 and not out
        assert f"uv.lock: package proj: {expected}" in err
        assert "AttributeError" not in err
        assert fake_pypi == []

    code, out, err = run(capsys, str(lock), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--json")
    assert code == 2
    payload = json.loads(out)["error"]
    assert payload["type"] == "SourceError"
    assert expected in payload["message"]
    assert fake_pypi == []
