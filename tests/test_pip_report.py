"""pip installation reports (pip install --dry-run --report) as pinned inputs."""

import json

import pytest

from wheelgap.cli import main
from wheelgap.sources import SourceError, load_inputs


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
    "payload,part",
    [
        ({}, "expected a pip installation report"),
        ({"version": "2", "install": []}, "expected a pip installation report"),
        ({"version": "1", "install": [{}]}, "has no metadata"),
        ({"version": "1", "install": [{"metadata": {"name": "x"}}]}, "has no version"),
        ({"version": "1", "install": [{"metadata": {"name": "bad name", "version": "1"}}]}, "valid package name"),
        ({"version": "1", "install": [{"metadata": {"name": "x", "version": "not-a-version"}}]}, "invalid version"),
    ],
)
def test_report_rejects_malformed_and_ambiguous_data(tmp_path, payload, part):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceError, match=part):
        load_inputs([str(path)])


def test_report_checks_pip_pins_with_existing_gap_engine(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(path, [install("fastlib"), install("nowheels", "0.9")])
    code = main([str(path), "-p", "3.13", "-t", "win-amd64", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["sources"] == ["report.json"]
    assert {item["name"] for item in report["packages"]} == {"fastlib", "nowheels"}
    assert any("nowheels" in url for url in fake_pypi)


def test_report_notes_other_platforms_only_on_stderr(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(
        path,
        [install("fastlib")],
        {"python_version": "3.13", "sys_platform": "linux", "platform_machine": "x86_64"},
    )
    main([str(path), "-p", "3.13", "-t", "win-amd64", "--json"])
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "other targets may install additional dependencies" in captured.err
    assert fake_pypi


def test_missing_report_file_is_an_input_error(tmp_path, capsys):
    code = main([str(tmp_path / "report.json"), "--json"])
    assert code == 2
    assert "no such file" in capsys.readouterr().err


def test_report_never_queries_an_entry_without_origin(tmp_path, capsys, fake_pypi):
    path = tmp_path / "report.json"
    make_report(
        path,
        [{"metadata": {"name": "private-project", "version": "1.0"}}],
    )
    code = main([str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["skipped"] == [{"name": "private-project", "reason": "url"}]
    assert fake_pypi == []
