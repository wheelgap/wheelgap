import json
import platform
import sys
import sysconfig
from pathlib import Path

import packaging
import pytest

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
    index = "https://mirror.example.com/simple"
    code, out, err = run(
        capsys, str(path), "--offline", "--index-url", index, "-p", "3.13", "-t", "linux-x86_64", "--json"
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


@pytest.mark.parametrize("as_json", [False, True])
def test_empty_python_in_config_is_an_error(capsys, fixtures, tmp_path, as_json):
    project = project_with_lock(tmp_path, fixtures)
    (project / "pyproject.toml").write_text('[tool.wheelgap]\npython = [""]\n', encoding="utf-8")
    args = [str(project), "-t", "linux-x86_64"]
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
    assert "purewheel  3.1      ✓             ✓\n" in out  # a wheel on an ignored target isn't marked
    assert "puresdist  1.0      ● source      ✗ missing (ignored)\n" in out


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
    known = {"$ref", "type", "enum", "minimum", "required", "properties", "additionalProperties", "items"}
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
            "build_requires",
            "notes",
            "bundled",
            "bundled_version_differs",
            "conda_forge",
            "suggest",
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
        if "build_requires" in pkg:
            reqs = pkg["build_requires"]
            assert reqs is None or (isinstance(reqs, list) and all(isinstance(r, str) for r in reqs))
        if "conda_forge" in pkg:  # a hint for some of the targets in rejected, never an empty one
            assert pkg["conda_forge"]["builds"] and set(pkg["conda_forge"]["builds"]) <= set(pkg["rejected"])
            assert all(pkg["conda_forge"]["builds"].values())
        for note in pkg.get("notes", []):
            assert set(note["targets"]) <= set(pkg.get("rejected", {})), "notes cover gap targets only"
        if "suggest" in pkg:  # a release for some of the targets in rejected
            assert set(pkg["suggest"]) <= set(pkg["rejected"]) and all(
                isinstance(v, str) for v in pkg["suggest"].values()
            )
    for item in data["skipped"]:
        assert set(item) == {"name", "reason"}
        assert isinstance(item["name"], str) and isinstance(item["reason"], str)
    summary = data["summary"]
    assert set(summary) == {"packages", "with_gaps", "per_target"}
    assert summary["packages"] == len(data["packages"])
    assert isinstance(summary["with_gaps"], int) and 0 <= summary["with_gaps"] <= summary["packages"]
    # with_gaps counts packages that are not all wheel/n/a/bundled, leaving out the targets ignored for them
    expected = sum(
        1
        for pkg in data["packages"]
        if any(
            s in {"source", "missing", "unknown"}
            for label, s in pkg["results"].items()
            if label not in ignored_on.get(pkg["name"], set())
        )
    )
    assert summary["with_gaps"] == expected
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
        (LIB_PYPROJECT, ["--group", "dev"], "don't apply to pyproject.toml"),
        (LIB_PYPROJECT, ["--no-dev"], "don't apply to pyproject.toml"),
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


def test_pyproject_self_reference_in_dependencies_is_not_looked_up(capsys, fake_pypi, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(LIB_PYPROJECT.replace('"fastlib==2.0.0", ', '"fastlib==2.0.0", "MyLib[test]", '), encoding="utf-8")
    code, out, _ = run(capsys, str(path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert code == 1
    rows = {row["name"]: row for row in json.loads(out)["packages"]}
    assert set(rows) == {"ranged", "fastlib", "nowheels"}  # MyLib[test] adds the test extra
    assert not any("mylib" in url.lower() for url in fake_pypi)


def test_directory_without_lockfile_suggests_pyproject(capsys, fake_pypi, tmp_path):
    (tmp_path / "pyproject.toml").write_text(LIB_PYPROJECT, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "no lockfile found" in err
    assert f"wheelgap {tmp_path / 'pyproject.toml'}" in err


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
        *[  # one per line: requirements.txt line 7 and more/extra.txt line 3
            "::error file=requirements.txt,title=ranged 1.9%3A builds from source::"
            "ranged 1.9 builds from source on linux-x86_64, win-amd64."
        ]
        * 2,
        "::error file=more/extra.txt,line=2,title=winonly 311%3A can't install::"
        "winonly 311 can't install on linux-x86_64.",
    ]


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
    # Options, editables, paths and URLs aren't requirements with a line; an include cycle is read once.
    req.write_text(
        "--index-url https://pypi.org/simple\n-e ./editable\n./local\n"
        "git+https://example.invalid/x.git#egg=gitpkg\nnamed @ https://example.invalid/named-1.0.tar.gz\n"
        "-r other.txt\nnowheels==0.9\n",
        encoding="utf-8",
    )
    (tmp_path / "other.txt").write_text("-r requirements.txt\npurewheel==3.1\n", encoding="utf-8")
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=7,"]
    # A continued requirement is on the first line with something on it.
    req.write_text("purewheel==3.1\n\\\n\\\nnowheels==0.9\n", encoding="utf-8")
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,line=4,"]


def test_github_annotations_requirements_cross_check(capsys, fake_pypi, tmp_path, monkeypatch):
    # The file is read twice; if the scan doesn't find what the parser read (here the file changed in between, so
    # every line moved down), no line is used.
    from wheelgap import github

    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    req = tmp_path / "requirements.txt"
    req.write_text("nowheels==0.9\n", encoding="utf-8")
    scan = github.requirement_lines

    def changed(path, seen=None):
        req.write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
        return scan(path, seen)

    monkeypatch.setattr(github, "requirement_lines", changed)
    assert annotate(capsys, tmp_path, str(req)) == ["::error file=requirements.txt,"]
    # An include the scan can't read gives no lines at all.
    req.write_text("-r gone.txt\n", encoding="utf-8")
    assert scan(req) is None


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
