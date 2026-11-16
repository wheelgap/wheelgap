"""pip installation reports (pip install --dry-run --report) as pinned inputs."""

import json

import pytest

import wheelgap
from wheelgap.cli import main
from wheelgap.sources import SourceError, check_report_targets, load_inputs

LINUX_313 = {
    "implementation_name": "cpython",
    "python_version": "3.13",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}
WIN_313 = {
    "implementation_name": "cpython",
    "python_version": "3.13",
    "sys_platform": "win32",
    "platform_machine": "AMD64",
}


def make_report(path, items, environment=None):
    path.write_text(
        json.dumps({"version": "1", "install": items, "environment": environment or {}}),
        encoding="utf-8",
    )


def install(name, version="2.0.0", url="https://files.pythonhosted.org/packages/pkg.whl", **extra):
    return {"metadata": {"name": name, "version": version}, "download_info": {"url": url}, **extra}


def test_report_parses_pins_and_skips_non_index_sources(tmp_path):
    path = tmp_path / "report.json"
    make_report(
        path,
        [
            install("fastlib"),
            install("remote", url="https://example.com/file.whl", is_direct=True),
            install("local", url="file:///tmp/local.whl"),
            {
                "metadata": {"name": "checkout", "version": "1.0"},
                "download_info": {"url": "https://github.com/example/repo.git", "vcs_info": {"vcs": "git"}},
            },
        ],
    )
    (source,) = load_inputs([str(path)])
    assert source.kind == "pip-report"
    assert [(p.name, p.version, p.skip) for p in source.packages] == [
        ("fastlib", "2.0.0", None),
        ("remote", "2.0.0", "url"),
        ("local", "2.0.0", "path"),
        ("checkout", "1.0", "git"),
    ]
    assert source.pypi_only is False


@pytest.mark.parametrize(
    "entry,skip",
    [
        (
            {
                "metadata": {"name": "proj", "version": "1.0"},
                "download_info": {"url": "file:///src/proj", "dir_info": {"editable": True}},
            },
            "path",
        ),
        (install("proj", "1.0", is_direct=True), "url"),  # a direct URL, even to a file on PyPI
        (install("proj", "1.0", url="https://mirror.example/proj-1.0.whl", is_direct=True), "url"),
    ],
)
def test_report_skips_entries_not_from_the_index(tmp_path, entry, skip):
    path = tmp_path / "report.json"
    make_report(path, [entry])
    (source,) = load_inputs([str(path)])
    assert [(p.name, p.skip) for p in source.packages] == [("proj", skip)]


def test_report_from_pypi_only_is_pypi_only(tmp_path):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib"), install("nowheels", "0.9")])
    (source,) = load_inputs([str(path)])
    assert [p.skip for p in source.packages] == [None, None]
    assert source.pypi_only is True


@pytest.mark.parametrize(
    "payload,part",
    [
        ({}, "isn't a pip installation report"),
        ({"name": "app", "dependencies": {}}, "isn't a pip installation report"),  # package.json, say
        ([], "isn't a pip installation report"),
        ({"version": "2", "install": []}, "version '2' isn't supported"),
        ({"version": "1", "install": {}}, "`install` isn't a list"),
        ({"version": "1", "install": [{}]}, "has no metadata"),
        ({"version": "1", "install": [{"metadata": {"name": "x"}}]}, "has no version"),
        ({"version": "1", "install": [{"metadata": {"name": "bad name", "version": "1"}}]}, "valid package name"),
        ({"version": "1", "install": [{"metadata": {"name": "x", "version": "not-a-version"}}]}, "invalid version"),
        ({"version": "1", "install": [install("Foo_Bar"), install("foo-bar")]}, "duplicate install entry for foo-bar"),
        (
            {"version": "1", "install": [{"metadata": {"name": "x", "version": "1"}, "download_info": []}]},
            "download_info",
        ),
        ({"version": "1", "install": [install("x", url=7)]}, "invalid download URL"),
        ({"version": "1", "install": [install("x", url="https://[::1/x.whl")]}, "invalid download URL"),
        ({"version": "1", "install": [], "environment": {"python_version": 3.13}}, "invalid pip report environment"),
        ({"version": "1", "install": [], "environment": ["linux"]}, "invalid pip report environment"),
    ],
)
def test_report_rejects_malformed_and_ambiguous_data(tmp_path, payload, part):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceError, match=part):
        load_inputs([str(path)])


def test_report_too_deeply_nested_is_an_input_error(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    # Some Pythons' json raises RecursionError here and some parse it; either way it's an input error, not a crash.
    with pytest.raises(SourceError, match="invalid pip installation report|isn't a pip installation report"):
        load_inputs([str(path)])


def test_report_from_a_mirror_is_refused_rather_than_passing_empty(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    url = "https://user:secret@artifactory.example.com/api/pypi/pypi/packages/fastlib-2.0.0.whl"
    make_report(path, [install("fastlib", url=url), install("nowheels", "0.9", url=url)], LINUX_313)
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    err = capsys.readouterr().err
    assert code == 2
    assert "artifactory.example.com, not PyPI" in err
    assert "secret" not in err
    assert fake_pypi == []


def test_report_checks_pip_pins_with_existing_gap_engine(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib"), install("nowheels", "0.9")], WIN_313)
    code = main([str(path), "-p", "3.13", "-t", "win-amd64", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["sources"] == ["report.json"]
    assert {item["name"] for item in report["packages"]} == {"fastlib", "nowheels"}
    assert any("nowheels" in url for url in fake_pypi)


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "3.13", "-t", "win-amd64"],  # another platform
        ["-p", "3.13", "-t", "linux-aarch64"],  # another machine
        ["-p", "3.14", "-t", "linux-x86_64"],  # another Python
        ["-p", "3.13", "-t", "linux-x86_64,win-amd64"],  # one target matches, the other doesn't
    ],
)
def test_report_refuses_targets_it_was_not_resolved_for(tmp_path, capsys, fake_pypi, args):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib")], LINUX_313)
    code = main([str(path), *args, "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["error"]["type"] == "SourceError"
    assert (
        "sys_platform linux, platform_machine x86_64, python_version 3.13, implementation_name cpython" in captured.err
    )
    assert "check that target only, or make a report on each target" in captured.err
    assert fake_pypi == []


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {**LINUX_313, "implementation_name": "pypy"},
        {key: value for key, value in LINUX_313.items() if key != "platform_machine"},
    ],
)
def test_report_refuses_an_environment_that_is_missing_or_another(tmp_path, capsys, fake_pypi, environment):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib")], environment)
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    assert code == 2
    assert "report" in capsys.readouterr().err
    assert fake_pypi == []


def test_report_on_its_own_target_checks_with_no_note(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib")], LINUX_313)
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert [p["name"] for p in json.loads(captured.out)["packages"]] == ["fastlib"]
    assert captured.err == ""


@pytest.mark.parametrize(
    ("platform", "environment"),
    [
        ("win32", WIN_313),  # 32-bit Python on 64-bit Windows reports AMD64
        ("win32", {**WIN_313, "platform_machine": "ARM64"}),
        ("linux-i686", LINUX_313),  # a 32-bit container on an x86_64 host
        ("linux-armv7l", {**LINUX_313, "platform_machine": "aarch64"}),
        ("linux-armv7l", {**LINUX_313, "platform_machine": "armv8l"}),
    ],
)
def test_report_is_not_refused_for_a_machine_wheelgap_cannot_pin_down(tmp_path, platform, environment):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib")], environment)
    (source,) = load_inputs([str(path)])
    check_report_targets(source, wheelgap.make_targets(python="3.13", platforms=platform))
    # The other keys still count.
    with pytest.raises(SourceError, match="check that target only"):
        check_report_targets(source, wheelgap.make_targets(python="3.14", platforms=platform))


def test_api_refuses_a_report_on_another_target(tmp_path, fake_pypi):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib")], LINUX_313)
    with pytest.raises(SourceError, match="check that target only"):
        wheelgap.check_lockfile(path, wheelgap.make_targets(python="3.13", platforms="win-amd64"))
    assert fake_pypi == []
    (result,) = wheelgap.check_lockfile(path, wheelgap.make_targets(python="3.13", platforms="linux-x86_64"))
    assert result.name == "fastlib"


def test_missing_report_file_is_an_input_error(tmp_path, capsys):
    code = main([str(tmp_path / "report.json"), "--json"])
    assert code == 2
    assert "no such file" in capsys.readouterr().err


def test_report_never_queries_an_entry_without_origin(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(
        path,
        [{"metadata": {"name": "private-project", "version": "1.0"}}],
        LINUX_313,
    )
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["skipped"] == [{"name": "private-project", "reason": "url"}]
    assert fake_pypi == []
