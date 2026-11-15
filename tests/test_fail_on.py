import json

from wheelgap.cli import main


def test_fail_on_missing_still_fails_when_a_package_cant_install(fixtures):
    armgap = str(fixtures / "armgap")
    assert main([armgap, "-p", "3.13", "-t", "musl-aarch64"]) == 1
    assert main([armgap, "-p", "3.13", "-t", "musl-aarch64", "--fail-on", "missing"]) == 1  # newglibc


def test_fail_on_missing_accepts_source_builds(capsys, fake_pypi):
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 1
    capsys.readouterr()
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing"]) == 0
    out = capsys.readouterr().out
    assert "● source" in out and "--fail-on missing" in out
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing", "--json"]) == 0


def test_fail_on_from_pyproject(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nfail-on = "missing"\n')
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 0
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "source"]) == 1  # flag wins
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nfail-on = "never"\n')
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 2
    assert "fail-on must be one of" in capsys.readouterr().err


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# fastlib 2.0.0 has a linux-x86_64 wheel and an sdist, so it builds from source on win-arm64.
FASTLIB = ["fastlib==2.0.0", "-p", "3.13", "-t", "linux-x86_64,win-arm64"]


def test_advisory_only_gap_exits_zero(capsys, fake_pypi):
    code, out, _ = run(capsys, *FASTLIB)
    assert code == 1
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "win-arm64")
    assert code == 0
    assert "win-arm64 (advisory)" in out  # the table's column and the per-target line
    assert "● source" in out  # still reported
    assert "1 gap in 1 package on advisory target win-arm64 doesn't fail the run." in out
    assert "Source builds are allowed" not in out  # passing has nothing to do with --fail-on
    # Aliases and Docker names work as in -t; quiet output says why a run with gaps passes.
    for name in ("windows-arm64", "windows/arm64"):
        code, out, err = run(capsys, *FASTLIB, "--advisory", name, "-q")
        assert code == 0, err
        assert "1 gap in 1 package on advisory target win-arm64 doesn't fail the run." in out


def test_advisory_gap_on_a_required_target_still_fails(capsys, fake_pypi):
    # nowheels has only an sdist, so it builds from source on linux-x86_64 too.
    code, out, _ = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "win-arm64")
    assert code == 1
    assert "2 gaps in 2 packages on advisory target win-arm64 don't fail the run." in out
    # Several advisory targets, given repeated or comma-separated, and a Docker name.
    code, _, err = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "win-arm64", "--advisory", "linux/amd64")
    assert code == 0, err
    code, out, _ = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "linux,win-arm64")
    assert code == 0
    assert "3 gaps in 2 packages on advisory targets linux-x86_64, win-arm64 don't fail the run." in out
    assert run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "win-arm64", "--exit-zero")[0] == 0


def test_advisory_json(capsys, fake_pypi):
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "win-arm64", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["advisory"] == ["win-arm64"]
    # Nothing is hidden: the gap is in the package results and the counts.
    assert data["packages"][0]["results"] == {"linux-x86_64": "wheel", "win-arm64": "source"}
    assert data["summary"]["with_gaps"] == 1
    assert data["summary"]["per_target"]["win-arm64"] == {"source": 1, "missing": 0, "unknown": 0}
    # Labels, so every Python on the platform is listed; and an empty list without --advisory.
    code, out, _ = run(capsys, *FASTLIB, "-p", "3.14t", "--advisory", "win-arm64", "--json")
    assert json.loads(out)["advisory"] == ["3.13 win-arm64", "3.14t win-arm64"]
    code, out, _ = run(capsys, *FASTLIB, "--json")
    assert code == 1 and json.loads(out)["advisory"] == []


def test_advisory_markdown(capsys, fake_pypi):
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "win-arm64", "--markdown")
    assert code == 0
    assert "| package | version | linux-x86\\_64 | win-arm64 (advisory) |" in out
    assert "- win-arm64 (advisory): 1 from source" in out
    assert "1 gap in 1 package on advisory target win-arm64 doesn't fail the run." in out
    assert "Source builds are allowed" not in out


def test_advisory_target_must_be_checked(capsys, fake_pypi):
    code, _, err = run(capsys, *FASTLIB, "--advisory", "macos-arm64")
    assert code == 2
    assert "--advisory macos-arm64: matches no checked target (this run checks linux-x86_64, win-arm64)" in err
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "win-arm64,macos", "--json")
    assert code == 2 and "--advisory macos: matches no checked target" in json.loads(out)["error"]["message"]
    # A group means the checked targets among its members, and is an error only when none of them is checked.
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "all", "--json")
    assert code == 0 and json.loads(out)["advisory"] == ["linux-x86_64", "win-arm64"]
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "everything", "--json")
    assert code == 0 and json.loads(out)["advisory"] == ["linux-x86_64", "win-arm64"]
    code, _, err = run(capsys, *FASTLIB, "--advisory", "mobile")
    assert code == 2 and "--advisory mobile: matches no checked target" in err
    code, _, err = run(capsys, *FASTLIB, "--advisory", "win-arm46")
    assert code == 2 and "--advisory: " in err and "did you mean 'win-arm64'" in err


def test_every_target_advisory_notes_that_the_run_cant_fail(capsys, fake_pypi):
    note = "every checked target is advisory (--advisory), so this run won't fail"
    code, _, err = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "all")
    assert code == 0 and note in err
    code, _, err = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "all", "-q")
    assert code == 0 and note not in err
    code, _, err = run(capsys, "nowheels==0.9", *FASTLIB, "--advisory", "win-arm64")
    assert code == 1 and note not in err


def test_advisory_from_pyproject(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nadvisory = ["win-arm64"]\n')
    code, _, err = run(capsys, *FASTLIB)
    assert code == 0, err
    assert run(capsys, *FASTLIB, "--advisory", "")[0] == 1  # the flag replaces the list, and "" clears it
    # A project default for runs that check other targets: a note, not an error.
    code, _, err = run(capsys, "nowheels==0.9", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1
    assert "advisory in [tool.wheelgap] names targets this run doesn't check: win-arm64" in err
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nadvisory = ["win-arm46"]\n')
    code, _, err = run(capsys, *FASTLIB)
    assert code == 2 and "advisory in [tool.wheelgap]: " in err
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nadvisory = 1\n")
    code, _, err = run(capsys, *FASTLIB)
    assert code == 2 and "advisory must be a list of strings" in err


def test_advisory_with_dockerfile_reads_docker_names_as_musl(capsys, fake_pypi, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    args = ["fastlib==2.0.0", "--dockerfile", str(dockerfile), "-t", "linux/amd64,linux/arm64", "--json"]
    code, out, _ = run(capsys, *args, "--advisory", "linux/arm64")
    assert code == 1  # fastlib builds from source on musl-x86_64 too
    assert json.loads(out)["advisory"] == ["musl-aarch64"]


def test_advisory_with_fail_on_missing(capsys, fake_pypi):
    # nowheels builds from source on both; winonly has a win-amd64 wheel and nothing for win-arm64.
    args = ["nowheels==0.9", "winonly==311", "-p", "3.13", "-t", "win-amd64,win-arm64", "--fail-on", "missing"]
    assert run(capsys, *args)[0] == 1
    code, out, _ = run(capsys, *args, "--advisory", "win-arm64")
    assert code == 0
    assert "Source builds are allowed (--fail-on missing), so this run passes." in out
    assert "2 gaps in 2 packages on advisory target win-arm64 don't fail the run." in out
    # Only a source build on an advisory target: the run passes because of --advisory, not --fail-on.
    code, out, _ = run(capsys, "winonly==311", *args[2:], "--advisory", "win-arm64")
    assert code == 0 and "Source builds are allowed" not in out


def test_advisory_and_ignore_name_target(capsys, fake_pypi):
    # The ignored gap isn't an advisory gap: it isn't counted anywhere.
    code, out, _ = run(capsys, *FASTLIB, "--advisory", "win-arm64", "--ignore", "fastlib:win-arm64", "--all")
    assert code == 0
    assert "● source (ignored)" in out and "win-arm64 (advisory)" in out
    assert "on advisory target" not in out
    # With a second, not ignored, advisory gap, only that one is counted.
    code, out, _ = run(
        capsys, "nowheels==0.9", *FASTLIB, "--advisory", "win-arm64", "--ignore", "fastlib:win-arm64,nowheels:linux"
    )
    assert code == 0
    assert "1 gap in 1 package on advisory target win-arm64 doesn't fail the run." in out
