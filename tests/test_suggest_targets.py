import json
import os
import tomllib

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import final_from
from wheelgap.suggest_targets import (
    _Flow,
    _NotRead,
    _runner_os,
    _same,
    _Scalar,
    _split_key,
    _Unsure,
    _value,
    runner_target,
    suggest,
)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


NOT_READ = "uses YAML this scanner doesn't read: "
JOB_NOT_READ = " (neither its runner nor its Python is read)"


def workflow(tmp_path, text, name="ci.yml"):
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")  # non-ASCII cases: Windows would write cp1252


def where(items):
    return [(f.value, f.path, f.line) for f in items]


def unreadable(found):
    return [(u.path, u.line, u.reason) for u in found.unreadable]


@pytest.mark.parametrize(
    ("label", "target"),
    [
        ("ubuntu-latest", "linux-x86_64"),
        ("ubuntu-24.04", "linux-x86_64"),
        ("ubuntu-22.04", "linux-x86_64"),
        ("ubuntu-slim", "linux-x86_64"),
        ("Ubuntu-Latest", "linux-x86_64"),  # labels are case-insensitive
        ("ubuntu-24.04-arm", "linux-aarch64"),
        ("ubuntu-22.04-arm", "linux-aarch64"),
        ("windows-latest", "win-amd64"),
        ("windows-2025", "win-amd64"),
        ("windows-2025-vs2026", "win-amd64"),
        ("windows-11-arm", "win-arm64"),
        ("windows-11-vs2026-arm", "win-arm64"),
        ("macos-13", "macos-x86_64"),
        ("macos-15-intel", "macos-x86_64"),
        ("macos-latest-large", "macos-x86_64"),
        ("macos-15-large", "macos-x86_64"),
        ("macos-14", "macos-arm64"),
        ("macos-15", "macos-arm64"),
        ("macos-26", "macos-arm64"),
        ("macos-latest", "macos-arm64"),
        ("macos-latest-xlarge", "macos-arm64"),
        ("macos-13-xlarge", "macos-arm64"),
        ("xcode-27", "macos-arm64"),
        ("xcode-27-xlarge", "macos-arm64"),
        # Not known for sure: never guessed.
        ("self-hosted", None),
        ("ubuntu-latest-arm", None),
        ("ubuntu-24.04-16core", None),
        ("windows-11", None),
        ("macos-10.15", None),
        ("xcode-latest", None),
        ("my-runner", None),
    ],
)
def test_runner_target(label, target):
    assert runner_target(label) == target


def test_literal_matrix_with_include_and_exclude(tmp_path):
    workflow(
        tmp_path,
        """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ${{ matrix.os }}  # from the matrix
    strategy:
      matrix:
        os: [ubuntu-latest, windows-11-arm, 'macos-latest']
        python-version:
          - "3.12"
          - "3.13"
        include:
          - os: ubuntu-24.04-arm
            python-version: "3.14t"
        exclude:
          - os: windows-11-arm
    steps:
      - uses: actions/checkout@v5
      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python-version }}
      - run: |
          echo "runs-on: windows-latest"
          echo "python-version: 3.9"
  lint:
    runs-on: "ubuntu-24.04"
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: '3.11.4'
  release:
    uses: ./.github/workflows/release.yml
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    # windows-11-arm is excluded from every combination, so it isn't suggested; the include adds an arm runner.
    assert where(found.targets) == [
        ("linux-x86_64", path, 8),
        ("macos-arm64", path, 8),
        ("linux-aarch64", path, 13),
        ("linux-x86_64", path, 27),
    ]
    assert where(found.pythons) == [
        ("3.12", path, 10),
        ("3.13", path, 11),
        ("3.14t", path, 14),
        ("3.11", path, 31),
    ]
    assert found.targets[0].source == "runs-on: ubuntu-latest (matrix.os, line 5)"
    assert found.unreadable == []
    assert found.config() == {
        "python": ["3.11", "3.12", "3.13", "3.14t"],
        "targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"],
    }


def test_matrix_it_cant_read_is_listed_not_guessed(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  plan:
    runs-on: ubuntu-latest
  wheels:
    needs: plan
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
  computed:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        runner: ${{ fromJSON(inputs.runners) }}
  multiline:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, [windows-latest]]
  nomatrix:
    runs-on: ${{ matrix.os }}
  expression:
    runs-on: ${{ inputs.os == 'arm' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }}
  self:
    runs-on: [self-hosted, linux, ARM64]
  group:
    runs-on:
      group: big-runners
  custom:
    runs-on: build-box-1
  container:
    runs-on: ubuntu-latest
    container: python:3.13-alpine
  ambiguous:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        exclude:
          - os: MACOS-LATEST
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    assert where(found.targets) == [("linux-x86_64", path, 3)]
    assert unreadable(found) == [
        (path, 8, "job wheels: the matrix is computed (${{ fromJSON(needs.plan.outputs.matrix) }})"),
        (path, 13, "job computed: matrix.runner is computed (${{ fromJSON(inputs.runners) }})"),
        (path, 17, "job multiline: the matrix has values that aren't a plain list"),
        (path, 20, "job nomatrix: the job has no strategy.matrix"),
        (
            path,
            22,
            "job expression: runs-on: ${{ inputs.os == 'arm' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }} "
            "is an expression",
        ),
        (path, 24, "job self: runs-on [self-hosted, linux, ARM64] isn't a single label"),
        (path, 26, "job group: runs-on with a group or labels isn't a single label"),
        (path, 29, "job custom: build-box-1 isn't a GitHub-hosted runner wheelgap knows"),
        (path, 32, "job container: runs in a container, whose platform isn't read"),
        (
            path,
            39,
            "job ambiguous: can't tell whether GitHub counts 'macos-latest' and 'MACOS-LATEST' as the same value "
            "(neither its runner nor its Python is read)",
        ),
    ]


def test_matrix_with_only_include(tmp_path):
    # With no other keys, each include entry is a combination of its own.
    workflow(
        tmp_path,
        """\
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: windows-latest
            arch: x64
          - os: macos-14
            arch: arm64
""",
    )
    found = suggest(tmp_path)
    assert where(found.targets) == [
        ("win-amd64", ".github/workflows/ci.yml", 7),
        ("macos-arm64", ".github/workflows/ci.yml", 9),
    ]


def test_matrix_with_flow_mappings(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest]
        python: ["3.12", "3.13"]
        include:
          - { os: macos-latest, python: "3.13" }
          - {os: windows-latest, python: '3.13'}
          - { os: macos-26-intel, python: 3.10 }
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    assert where(found.targets) == [
        ("linux-x86_64", path, 6),
        ("macos-arm64", path, 9),
        ("win-amd64", path, 10),
        ("macos-x86_64", path, 11),
    ]
    assert found.unreadable == []


def test_include_overwrites_what_an_earlier_include_added(tmp_path):
    # os isn't an original matrix key, so the second include overwrites the first on every combination:
    # GitHub never runs on windows-latest here.
    workflow(
        tmp_path,
        """\
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        python: ["3.12", "3.13"]
        include:
          - os: windows-latest
          - os: ubuntu-latest
""",
    )
    assert where(suggest(tmp_path).targets) == [("linux-x86_64", ".github/workflows/ci.yml", 9)]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("3.10", "unquoted 3.10 is the number 3.1 in YAML (quote it)"),
        ("'3.x'", "python-version 3.x isn't a CPython version wheelgap can read"),
        ("pypy3.10", "python-version pypy3.10 isn't a CPython version wheelgap can read"),
        ("'>=3.11'", "python-version >=3.11 isn't a CPython version wheelgap can read"),
        ("|\n            3.12\n            3.13", "python-version: a multi-line value isn't read"),
    ],
)
def test_python_versions_it_cant_read(tmp_path, value, reason):
    workflow(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: {value}
""",
    )
    found = suggest(tmp_path)
    assert found.pythons == []
    assert unreadable(found) == [(".github/workflows/ci.yml", 7, f"job test: {reason}")]


def test_setup_python_without_a_version(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
  b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version-file: pyproject.toml
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
""",
    )
    found = suggest(tmp_path)
    assert where(found.pythons) == [("3.13", ".github/workflows/ci.yml", 14)]  # the next step is still read
    assert unreadable(found) == [
        (
            ".github/workflows/ci.yml",
            5,
            "job a: setup-python with no python-version (it reads .python-version, or uses the runner's)",
        ),
        (".github/workflows/ci.yml", 11, "job b: setup-python reads pyproject.toml, which isn't read here"),
    ]


def test_multi_stage_dockerfile(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim AS build\n"
        "RUN pip wheel -w /wheels .\n"
        "FROM --platform=linux/arm64 \\\n"
        "    python:3.12-alpine AS base\n"
        "FROM base AS runtime\n"
        "COPY --from=build /wheels /wheels\n"
    )
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "Dockerfile.web").write_text("FROM python:3.13-bookworm\n")
    (tmp_path / "deploy" / "Dockerfile.dockerignore").write_text("*.pyc\n")
    (tmp_path / "deploy" / "worker.Dockerfile").write_text("FROM python:3.14-trixie\n")
    (tmp_path / "dockerfile.py").write_text("from __future__ import annotations\n")  # not a Dockerfile
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "Dockerfile").write_text("FROM python:3.9\n")
    found = suggest(tmp_path)
    # The final stage builds on `base`, so the line is base's FROM, where the image is named.
    assert where(found.targets) == [
        ("musl-aarch64", "Dockerfile", 3),
        ("linux-x86_64", "deploy/Dockerfile.web", 1),
        ("linux-x86_64", "deploy/worker.Dockerfile", 1),
    ]
    assert found.targets[0].source == "FROM --platform=linux/arm64 python:3.12-alpine (musl 1.2)"
    assert where(found.pythons) == [
        ("3.12", "Dockerfile", 3),
        ("3.13", "deploy/Dockerfile.web", 1),
        ("3.14", "deploy/worker.Dockerfile", 1),
    ]
    assert found.unreadable == []
    # The two Debian images name different glibc versions, so neither is suggested.
    assert found.config() == {
        "python": ["3.12", "3.13", "3.14"],
        "targets": ["linux-x86_64", "musl-aarch64"],
        "musl": "1.2",
    }


def test_dockerfile_it_cant_read(tmp_path):
    (tmp_path / "Dockerfile").write_text("ARG BASE=python:3.13\nFROM $BASE\n")
    found = suggest(tmp_path)
    assert found.targets == []
    assert unreadable(found) == [("Dockerfile", None, "$BASE: build arguments are not supported")]


def test_glibc_is_left_out_when_the_sources_disagree(tmp_path, capsys):
    workflow(tmp_path, "jobs:\n  test:\n    runs-on: ubuntu-latest\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-trixie\n")
    found = suggest(tmp_path)
    assert found.config() == {"python": ["3.13"], "targets": ["linux-x86_64"]}  # the runner's glibc isn't read
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert (
        "# glibc not set: the linux-* targets don't all name the same one "
        "(not named at .github/workflows/ci.yml:3; 2.41 at Dockerfile:1)"
    ) in out


def test_empty_project(tmp_path, capsys, fake_pypi):
    code, out, err = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert out == (
        "Targets: none found\nPythons: none found\n\n"
        "No targets found, so there's nothing to suggest. Nothing was changed or checked.\n"
    )
    assert err == ""
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out) == {"targets": [], "python": [], "unreadable": [], "config": {}}
    assert fake_pypi == []


def test_cli_prints_sources_and_a_snippet(tmp_path, capsys, monkeypatch, fake_pypi):
    workflow(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-24.04-arm, windows-latest]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
""",
        name="test.yaml",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nbogus = 1\n")  # not read: nothing is checked
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "--suggest-targets")
    assert code == 0
    assert err == ""
    assert out == (
        "Targets:\n"
        "  linux-aarch64\n"
        "    .github/workflows/test.yaml:6  runs-on: ubuntu-24.04-arm (matrix.os, line 3)\n"
        "  win-amd64\n"
        "    .github/workflows/test.yaml:6  runs-on: windows-latest (matrix.os, line 3)\n"
        "Pythons:\n"
        "  3.13\n"
        "    .github/workflows/test.yaml:10  python-version: 3.13\n"
        "\n"
        "Suggested settings for pyproject.toml (nothing was changed or checked). They combine every job, lint,\n"
        "release and bot jobs too, so keep only the platforms you ship to:\n"
        "\n"
        "[tool.wheelgap]\n"
        'python = ["3.13"]\n'
        'targets = ["linux-aarch64", "win-amd64"]\n'
        'glibc = "2.39"\n'
    )
    snippet = out.split("ship to:\n\n", 1)[1]
    assert tomllib.loads(snippet) == {
        "tool": {"wheelgap": {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"], "glibc": "2.39"}}
    }
    code, out, _ = run(capsys, "--suggest-targets", ".", "--json")
    data = json.loads(out)
    assert data["targets"][0] == {
        "target": "linux-aarch64",
        "file": ".github/workflows/test.yaml",
        "line": 6,
        "from": "runs-on: ubuntu-24.04-arm (matrix.os, line 3)",
    }
    assert data["config"] == {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"], "glibc": "2.39"}
    assert fake_pypi == []


def test_cli_errors(tmp_path, capsys):
    code, _, err = run(capsys, "--suggest-targets", str(tmp_path / "nope"))
    assert code == 2
    assert "nope: not a folder" in err
    with pytest.raises(SystemExit):
        main(["--suggest-targets", str(tmp_path), "--markdown"])
    assert "no Markdown form" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--suggest-targets"])
    assert "--suggest-targets DIR" in capsys.readouterr().err
    for flags in (["-p", "3.13"], ["-t", "linux"], ["--dockerfile", "Dockerfile"], ["--list-targets"]):
        with pytest.raises(SystemExit):
            main(["--suggest-targets", str(tmp_path), *flags])
        assert f"--suggest-targets only suggests, so it can't be used with {flags[0]}" in capsys.readouterr().err


def test_final_from_line_numbers():
    text = "# syntax=docker/dockerfile:1\nFROM python:3.12 AS a\nRUN <<EOF\nFROM python:3.9\nEOF\nFROM \\\n  a\n"
    assert final_from(text) == ("python:3.12", None, 2)
    assert final_from("FROM python:3.11\n\nFROM \\\n# comment\n python:3.13-slim\n") == ("python:3.13-slim", None, 3)


CI = ".github/workflows/ci.yml"


def read(tmp_path, text):
    workflow(tmp_path, text)
    found = suggest(tmp_path)
    return where(found.targets), [(line, reason) for _, line, reason in unreadable(found)], found


@pytest.mark.parametrize(
    ("label", "os_"),
    [
        ("ubuntu-22.04", ("2.35", None)),
        ("ubuntu-22.04-arm", ("2.35", None)),
        ("ubuntu-24.04", ("2.39", None)),
        ("ubuntu-24.04-arm", ("2.39", None)),
        ("ubuntu-latest", (None, None)),  # moves between releases
        ("ubuntu-26.04", (None, None)),  # not in the distro table yet
        ("macos-13", (None, "13.0")),
        ("macos-14", (None, "14.0")),
        ("macos-15-intel", (None, "15.0")),
        ("macos-15-xlarge", (None, "15.0")),
        ("macos-15-large", (None, "15.0")),
        ("macos-latest", (None, None)),
        ("windows-2025", (None, None)),
    ],
)
def test_runner_os(label, os_):
    assert _runner_os(label) == os_


# Each accepted case was checked against PyYAML 6.0.3 (`yaml.safe_load("x: " + text)`): where the reader gives a value,
# it's PyYAML's. Everything else is outside the YAML the scanner reads, valid YAML or not.
FLOWS = [
    ("[a, 'b, c', \"d\"]", ["a", "b, c", "d"]),
    ("[a,]", ["a"]),
    ("[a b, c]", ["a b", "c"]),
    ("[ a , b ]", ["a", "b"]),
    ("[]", []),
    ("{}", {}),
    ("[-x, a-b]", ["-x", "a-b"]),
    ('[{os: x, python: "3.13"}, {os: y}]', [{"os": "x", "python": "3.13"}, {"os": "y"}]),
    ("[{os: a}, b]", [{"os": "a"}, "b"]),
    ("{os: x, python: '3.13'}", {"os": "x", "python": "3.13"}),
    ("{\"A\": b, 'C': d}", {"A": "b", "C": "d"}),
    ("[a, [b]]", ["a", ["b"]]),
    ("{a: {b: [c]}}", {"a": {"b": ["c"]}}),
    # Not read:
    ("[it's]", None),  # a quote inside a plain item
    ('[a"b]', None),
    ("[a:b]", None),  # a colon in a plain item
    ("{a:b}", None),
    ('{"A":"b #"}', None),  # JSON's key:value with no space
    ("{A: x:'y}", None),
    ("{A: a - 'b}", None),
    ('{A: &a "b #"}', None),  # an anchor in a flow collection
    ('[!!str "b #"]', None),  # a tag
    ('{? "a #" : b}', None),
    ("[*x]", None),
    ("[k: v]", None),  # a mapping in a list
    ('["x": y]', None),
    ("[?b]", None),
    ("[a,#b]", None),
    ("[a #b]", None),
    ("[a,,b]", None),
    ('["a" b]', None),
    ("[- a]", None),
    ('["a\\tb"]', None),  # an escape
    ("['it''s']", None),
    ("[a", None),
    ("['a]", None),
    ("{a: b, a: c}", None),  # a key twice
]


def _plain_data(value):
    if isinstance(value, list):
        return [_plain_data(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain_data(v) for k, v in value.items()}
    return value.text


@pytest.mark.parametrize(("text", "expected"), FLOWS)
def test_flow(text, expected):
    if expected is None:
        with pytest.raises(_NotRead):
            flow = _Flow(text, 1)
            flow.collection()
            if flow.pos != len(text):
                raise _NotRead(1, text)
    else:
        flow = _Flow(text, 1)
        assert _plain_data(flow.collection()) == expected
        assert flow.pos == len(text)


@pytest.mark.parametrize(("text", "expected"), [(t, e) for t, e in FLOWS if e is not None])
def test_flow_matches_pyyaml(text, expected):
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load("x: " + text)["x"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Plain scalars: indicators only matter first; `: ` and ` #` anywhere.
        ("ubuntu-latest", ("ubuntu-latest", False)),
        ("actions/setup-python@v6", ("actions/setup-python@v6", False)),
        ("github.ref == 'refs/heads/main' && x", ("github.ref == 'refs/heads/main' && x", False)),
        ('echo "a" [b] {c} | d > e & f * g ! h % i', ('echo "a" [b] {c} | d > e & f * g ! h % i', False)),
        ("${{ matrix.os }}", ("${{ matrix.os }}", False)),
        ("a#b", ("a#b", False)),
        ("a # comment", ("a", False)),
        ("a\t# comment", ("a", False)),
        ("x:'y", ("x:'y", False)),
        ("-1", ("-1", False)),
        ("nbsp # not a comment", ("nbsp # not a comment", False)),
        ("'it'", ("it", True)),
        ('"a # b"  # c', ("a # b", True)),
        ("''", ("", True)),
        ("# just a comment", None),
        ("", None),
        # Not read:
        ("a: b", "refused"),
        ("a:", "refused"),
        ("&a value", "refused"),
        ("*a value", "refused"),
        ("!!str x", "refused"),
        ("? x", "refused"),
        (": x", "refused"),
        ("- x", "refused"),
        ("%x", "refused"),
        ("@x", "refused"),
        ("`x", "refused"),
        ("'it''s'", "refused"),
        ("'a'b", "refused"),
        ("'a'#c", "refused"),  # `#` straight after a quote isn't a comment
        ('"a', "refused"),
        ("[a]x", "refused"),
        ("[a]#c", "refused"),
        ("|x", "refused"),
        ("|-2x", "refused"),
        ("&a |", "refused"),
        ("!!str |", "refused"),
    ],
)
def test_value(text, expected):
    if expected == "refused":
        with pytest.raises(_NotRead):
            _value(text, 1)
        return
    value, _ = _value(text, 1)
    assert (value if value is None else (value.text, value.quoted)) == expected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("|", "a multi-line value"),
        (">-", "a multi-line value"),
        ("|2", "a multi-line value"),
        ("|+1  # c", "a multi-line value"),
        ("*anchor", "an alias"),
        ('"a\\tb"', "a quoted value with escapes"),
        ('"a\\"b" # c', "a quoted value with escapes"),
    ],
)
def test_values_that_are_never_read(text, kind):
    value, _ = _value(text, 1)
    assert value.what == kind


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("runs-on: x", ("runs-on", "x")),
        ("on:", ("on", "")),
        ("key:\tx", ("key", "x")),
        ('"on": push', ("on", "push")),
        ("'a b': c", ("a b", "c")),
        ("a:b", None),
        ("a b: c", None),
        ('"arch\\x69tecture": x86', None),  # escapes in a quoted key
        ("'it''s': x", None),
        ("<<: *defaults", None),
        ("? key", None),
        ("[a]: b", None),
        ("plain text", None),
    ],
)
def test_split_key(text, expected):
    assert _split_key(text) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (_Scalar("3.12", True, 1), _Scalar("3.12", True, 2), True),
        (_Scalar("3.12", False, 1), _Scalar("3.12", False, 2), True),
        (_Scalar("ubuntu", False, 1), _Scalar("windows", False, 2), False),
        (_Scalar("3.12", True, 1), _Scalar("3.12", False, 2), None),  # a string and a number
        (_Scalar("Linux", False, 1), _Scalar("linux", False, 2), None),  # case
        (_Scalar("true", False, 1), _Scalar("true", True, 2), None),  # a boolean and a string
        (_Scalar("0x10", False, 1), _Scalar("0x10", False, 2), None),  # 16 to YAML 1.2
        (_Scalar("yes", False, 1), _Scalar("yes", False, 2), None),  # true to YAML 1.1
    ],
)
def test_same(a, b, expected):
    if expected is None:
        with pytest.raises(_Unsure):
            _same(a, b)
    else:
        assert _same(a, b) is expected


JOB = "jobs:\n  test:\n    runs-on: ubuntu-latest\n"


@pytest.mark.parametrize(
    ("text", "targets", "refused"),
    [
        (("--- # a comment\n" + JOB), [("linux-x86_64", ".github/workflows/ci.yml", 4)], []),
        (("---\n" + JOB + "...\n"), [], [(5, "uses YAML this scanner doesn't read: ...")]),
        (("--- on: push\n" + JOB), [], [(1, "uses YAML this scanner doesn't read: --- on: push")]),
        ((JOB + "---\n" + JOB), [], [(4, "uses YAML this scanner doesn't read: ---")]),
        ((JOB + "...\nname: x\n"), [], [(4, "uses YAML this scanner doesn't read: ...")]),
        ((JOB + "jobs:\n  b:\n    runs-on: windows-latest\n"), [], [(4, NOT_READ + "jobs:")]),
        (("name: x\n"), [], [(None, "no jobs")]),
        (("jobs: {test: {runs-on: ubuntu-latest}}\n"), [("linux-x86_64", ".github/workflows/ci.yml", 1)], []),
        (("jobs:\n  - test\n"), [], [(1, "can't read the jobs")]),
        (("jobs:\n\ttest:\n"), [], [(2, "uses YAML this scanner doesn't read: test:")]),
        (
            (JOB + "  test:\n    runs-on: windows-latest\n  other:\n    runs-on: macos-14\n"),
            [],
            [(4, NOT_READ + "test:")],
        ),
        (('name: "a\n  b"\n' + JOB), [], [(1, "uses YAML this scanner doesn't read: name: \"a")]),
        (("env:\n  A: 'x\n    y'\n" + JOB), [], [(2, "uses YAML this scanner doesn't read: A: 'x")]),
        (
            ('jobs:\n  test:\n    runs-on: "ubuntu-\n      latest"\n'),
            [],
            [(3, "uses YAML this scanner doesn't read: runs-on: \"ubuntu-")],
        ),
        (("name: [a,\n  b\n"), [], [(1, "uses YAML this scanner doesn't read: name: [a,")]),
        (
            ("defs: &job\n  a: b\njobs:\n  test: *job\n"),
            [],
            [(4, "job test: can't read the job (neither its runner nor its Python is read)")],
        ),
        (
            ("jobs:\n  test:\n    <<: *defaults\n    runs-on: ubuntu-latest\n"),
            [],
            [(3, "uses YAML this scanner doesn't read: <<: *defaults")],
        ),
        (("jobs:\n  test:\n    steps: []\n"), [], [(2, "job test: no runs-on line")]),
        (("jobs:\n  call:\n    uses: ./.github/workflows/x.yml\n"), [], []),
        (
            ('jobs:\n  test:\n    name: "Build\n      it"\n    runs-on: windows-latest\n'),
            [],
            [(3, "uses YAML this scanner doesn't read: name: \"Build")],
        ),
        (
            (JOB + "    steps: [{uses: actions/setup-python@v6}]\n"),
            [],
            [
                (
                    4,
                    "job test: steps: [{uses: actions/setup-python@v6}] isn't read" + JOB_NOT_READ,
                )
            ],
        ),
        (
            ("defs: &steps\n  - run: x\n" + JOB + "    steps: *steps\n"),
            [],
            [(6, "job test: steps: *steps isn't read (neither its runner nor its Python is read)")],
        ),
        (
            (JOB + "    steps:\n      - { uses: actions/setup-python@v6, with: {python-version: '3.13'} }\n"),
            [("linux-x86_64", ".github/workflows/ci.yml", 3)],
            [],
        ),
        (
            (JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with: { python-version: '3.13' }\n"),
            [("linux-x86_64", ".github/workflows/ci.yml", 3)],
            [],
        ),
        (
            (
                JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
                '          cache: "pip\n          python-version: 3.9\n          "\n'
            ),
            [],
            [(7, "uses YAML this scanner doesn't read: cache: \"pip")],
        ),
        (
            (
                JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
                "          x: [a,\n          python-version: 3.9\n          ]\n"
            ),
            [],
            [(7, "uses YAML this scanner doesn't read: x: [a,")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: ubuntu-latest\n"
            ),
            [],
            [
                (
                    5,
                    "job test: the matrix has values that aren't a plain list" + JOB_NOT_READ,
                )
            ],
        ),
        (
            ("jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        - ubuntu-latest\n"),
            [],
            [(5, "job test: can't read the matrix (neither its runner nor its Python is read)")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os:\n          - ${{ inputs.os }}\n"
            ),
            [],
            [(7, "job test: the matrix has a computed value (${{ inputs.os }})")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        include: x\n"
            ),
            [],
            [(5, "job test: can't read the matrix's include")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        include:\n          - os: [windows-latest]\n"
            ),
            [],
            [(5, "job test: the matrix's include has values that aren't plain")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        include:\n          - os: ${{ inputs.os }}\n"
            ),
            [],
            [(8, "job test: the matrix has a computed value (${{ inputs.os }})")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        exclude:\n          - arch: x86\n"
            ),
            [],
            [(5, "job test: the matrix excludes a key it doesn't have (neither its runner nor its Python is read)")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        exclude:\n          - os: ubuntu-latest\n"
            ),
            [],
            [(5, "job test: the matrix is empty (neither its runner nor its Python is read)")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        python: ['3.13']\n        include:\n"
                "          - os: ubuntu-latest\n            python: '3.12'\n"
            ),
            [],
            [(3, "job test: matrix.os isn't set in every matrix combination")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        include:\n          - OS: windows-latest\n"
            ),
            [],
            [(5, "job test: the matrix has keys that differ only in case (OS, os)")],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        n: [0x10, 16]\n        exclude:\n          - n: 16\n"
            ),
            [],
            [
                (
                    7,
                    "job test: can't tell how GitHub reads the unquoted 0x10 (quote it)" + JOB_NOT_READ,
                )
            ],
        ),
        (
            (
                "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest]\n        python: ['3.12', 3.13]\n"
                "        exclude:\n          - python: '3.13'\n"
            ),
            [],
            [
                (
                    9,
                    "job test: can't tell whether GitHub counts '3.13' and '3.13' as the same value" + JOB_NOT_READ,
                )
            ],
        ),
        (
            ("jobs:\n  test:\n    runs-on: ${{ matrix['os'] }}\n"),
            [],
            [(3, "job test: runs-on: ${{ matrix['os'] }} is an expression")],
        ),
        (
            ("jobs:\n  test:\n    runs-on: >-\n      ubuntu-latest\n"),
            [],
            [(3, "job test: runs-on: a multi-line value isn't read")],
        ),
        (
            ("jobs:\n  test:\n    runs-on: 0x10\n"),
            [],
            [(3, "job test: can't tell how GitHub reads the unquoted 0x10 (quote it)")],
        ),
    ],
)
def test_refusals(tmp_path, text, targets, refused):
    got_targets, got_refused, _ = read(tmp_path, text)
    assert got_targets == targets
    assert got_refused == refused


def test_exclude_needs_every_value_to_match(tmp_path):
    # The exclude takes out windows with 3.12 only, so windows stays (with 3.13), and so does 3.12 (on ubuntu).
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.12", "3.13"]
        exclude:
          - os: windows-latest
            python: "3.12"
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    assert targets == [("linux-x86_64", CI, 6), ("win-amd64", CI, 6)]
    assert [f.value for f in found.pythons] == ["3.12", "3.13"]
    assert refused == []


def test_multi_line_flow_lists(tmp_path):
    # The one form read: `[` ending the key's line (or alone on the next one, as Prettier writes it), one item per
    # line with a comma after all but the last, and `]` on its own line. Each item keeps its own line.
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [
          ubuntu-latest,  # a comment
          "windows-latest",

          macos-14
        ]
        python:
          [
            "3.12",
            '3.13',
          ]
        include: [
          {os: ubuntu-24.04-arm, python: "3.14t"}
        ]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    assert refused == []
    assert targets == [
        ("linux-x86_64", CI, 7),
        ("win-amd64", CI, 8),
        ("macos-arm64", CI, 10),
        ("linux-aarch64", CI, 18),
    ]
    assert [(f.value, f.line) for f in found.pythons] == [("3.12", 14), ("3.13", 15), ("3.14t", 18)]


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ('["3.12",\n                 "3.13"]', 6),  # items on the line with `[`
        ("[\n          '3.12', '3.13',\n        ]", 7),  # two items on a line
        ("[\n          '3.12'\n          '3.13'\n        ]", 8),  # a missing comma
        ("[\n          '3.12',", 6),  # never closed
        ("[\n        '3.12',\n        ]", 7),  # an item no deeper than its key
        ("\n          ['3.12', '3.13']", 7),  # a one-line flow list on the next line
    ],
)
def test_flow_lists_over_several_lines_in_other_forms_are_refused(tmp_path, value, line):
    _, refused, _ = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        python: " + value + "\n",
    )
    assert refused[0][0] == line
    assert refused[0][1].startswith("uses YAML this scanner doesn't read")


def test_block_scalars_are_skipped(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: >-
          runs-on: windows-latest
            - uses: actions/setup-python@v6
      - name: check
        run: |
          python-version: 3.9
  docs:
    description: |+
      jobs:
        x:
          runs-on: macos-14

    runs-on: windows-latest
  odd:
    steps:
      - |
        - uses: actions/setup-python@v6
    runs-on: macos-14
""",
    )
    assert targets == [("linux-x86_64", CI, 3), ("win-amd64", CI, 17)]
    assert found.pythons == []
    assert refused == [(20, "job odd: can't read the step | (neither its runner nor its Python is read)")]


def test_uses_is_case_insensitive_and_versions_are_strict(tmp_path):
    _, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: Actions/Setup-Python@v6
        with:
          python-version: "3.13"
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14-dev"
      - uses: actions/setup-python
        with:
          python-version: "3.12.4"
""",
    )
    assert where(found.pythons) == [("3.13", CI, 7), ("3.12", CI, 13)]
    assert refused == [(10, "job test: python-version 3.14-dev isn't a CPython version wheelgap can read")]


@pytest.mark.parametrize(
    ("runner", "inputs", "targets", "refused"),
    [
        ("windows-latest", "architecture: x86", [("win32", CI, 3)], []),
        ("windows-2022", "architecture: x64", [("win-amd64", CI, 3)], []),
        ("macos-14", "architecture: x64", [("macos-x86_64", CI, 3)], []),
        ("macos-14", "architecture: arm64", [("macos-arm64", CI, 3)], []),
        ("macos-13", "architecture: x64", [("macos-x86_64", CI, 3)], []),
        ("ubuntu-24.04-arm", "architecture: arm64", [("linux-aarch64", CI, 3)], []),
        ("windows-11-arm", "architecture: arm64", [("win-arm64", CI, 3)], []),
        ("windows-latest", "Architecture: x86", [("win32", CI, 3)], []),  # input names are case-insensitive
        ("ubuntu-latest", "architecture: x64", [("linux-x86_64", CI, 3)], []),
        (
            "windows-latest",
            "architecture: arm64",
            [],
            [(3, "setup-python's architecture: arm64 on windows-latest isn't read")],
        ),
        (
            "windows-11-arm",
            "architecture: x64",
            [],
            [(3, "setup-python's architecture: x64 on windows-11-arm isn't read")],
        ),
        (
            "ubuntu-latest",
            "architecture: x86",
            [],
            [(3, "setup-python's architecture: x86 on ubuntu-latest isn't read")],
        ),
        (
            "windows-latest",
            "architecture: X86",
            [],
            [(3, "setup-python's architecture: X86 on windows-latest isn't read")],
        ),
        (
            "windows-latest",
            "architecture: ${{ matrix.arch }}",
            [],
            [(3, "the job has no strategy.matrix")],
        ),
    ],
)
def test_setup_python_architecture(tmp_path, runner, inputs, targets, refused):
    got_targets, got_refused, found = read(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: {runner}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          {inputs}
""",
    )
    assert got_targets == targets
    assert got_refused == [(line, f"job test: {reason}") for line, reason in refused]
    assert [f.value for f in found.pythons] == ["3.13"]


def test_architecture_on_a_matrix_of_runners(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          architecture: x86
""",
    )
    # The first setup-python uses the runner's own architecture, the second 32-bit x86, which only Windows has.
    assert targets == [("linux-x86_64", CI, 6), ("win-amd64", CI, 6), ("win32", CI, 6)]
    assert found.targets[2].source == "runs-on: windows-latest (matrix.os, line 3), architecture: x86"
    assert refused == [(6, "job test: setup-python's architecture: x86 on ubuntu-latest isn't read")]


@pytest.mark.parametrize(
    ("inputs", "pythons", "refused"),
    [
        ("freethreaded: true", ["3.13t"], []),
        ("freethreaded: 'True'", ["3.13t"], []),
        ("freethreaded: FALSE", ["3.13"], []),
        ("FreeThreaded: true", ["3.13t"], []),
        ("freethreaded: yes", [], [(8, "freethreaded: yes isn't true or false")]),
        ("freethreaded: ${{ matrix.ft }}", [], [(8, "freethreaded: ${{ matrix.ft }} is an expression")]),
    ],
)
def test_setup_python_freethreaded(tmp_path, inputs, pythons, refused):
    targets, got_refused, found = read(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          {inputs}
""",
    )
    assert [f.value for f in found.pythons] == pythons
    assert got_refused == [(line, f"job test: {reason}") for line, reason in refused]
    assert targets == [("linux-x86_64", CI, 3)]


def test_freethreaded_needs_313(tmp_path):
    _, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
          freethreaded: true
    strategy:
      matrix:
        python: ["3.12", "3.14", "3.14t"]
""",
    )
    assert [(f.value, f.source) for f in found.pythons] == [
        ("3.14t", "python-version: 3.14 (matrix.python, line 7), freethreaded: true"),
        ("3.14t", "python-version: 3.14t (matrix.python, line 7)"),
    ]
    assert refused == [
        (
            11,
            "job test: python-version: 3.12 (matrix.python, line 7), freethreaded: true asks for a free-threaded 3.12, "
            "but free-threading starts at 3.13",
        )
    ]


def test_setup_uv_python_arch(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: "3.13"
          python-arch: x86
""",
    )
    assert targets == []
    assert refused == [(8, "job test: setup-uv's python-arch: x86 isn't read, so the job's target isn't read")]
    assert [f.value for f in found.pythons] == ["3.13"]


def test_runner_versions_go_in_the_snippet_when_they_agree(tmp_path, capsys):
    workflow(
        tmp_path,
        """\
jobs:
  a:
    runs-on: ubuntu-24.04
  b:
    runs-on: ubuntu-24.04-arm
  c:
    runs-on: macos-14
  d:
    runs-on: macos-14-xlarge
""",
    )
    assert suggest(tmp_path).config() == {
        "targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"],
        "glibc": "2.39",
        "macos": "14.0",
    }
    workflow(tmp_path, "jobs:\n  e:\n    runs-on: ubuntu-22.04\n  f:\n    runs-on: macos-latest\n", name="more.yml")
    found = suggest(tmp_path)
    assert found.config() == {"targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"]}
    assert found.left_out() == [
        "# glibc not set: the linux-* targets don't all name the same one "
        "(2.39 at .github/workflows/ci.yml:3, .github/workflows/ci.yml:5; 2.35 at .github/workflows/more.yml:3)",
        "# macos not set: the macos-* targets don't all name the same one "
        "(14.0 at .github/workflows/ci.yml:7, .github/workflows/ci.yml:9; not named at .github/workflows/more.yml:5)",
    ]
    workflow(tmp_path, "jobs:\n" + "".join(f"  j{n}:\n    runs-on: ubuntu-latest\n" for n in range(4)), name="more.yml")
    assert (
        suggest(tmp_path)
        .left_out()[0]
        .endswith(
            "(2.39 at .github/workflows/ci.yml:3, .github/workflows/ci.yml:5; "
            "not named at .github/workflows/more.yml:3, .github/workflows/more.yml:5 and 2 more)"
        )
    )


def test_dockerfile_sources_and_skipped_folders(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim\n")
    (tmp_path / "Containerfile").write_text("FROM --platform=linux/arm64 python:3.13-alpine\n")
    (tmp_path / "win.Dockerfile").write_text("FROM python:3.13-windowsservercore\n")
    (tmp_path / "Dockerfile.j2").write_text("FROM {{ base }}\n")
    (tmp_path / "Dockerfile.template").write_text("FROM %%BASE%%\n")
    for skipped in ("tests", "fixtures", "examples", "docs", "target", "build", "dist", ".devcontainer"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "Dockerfile").write_text("FROM python:3.9\n")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (tmp_path / "venv" / "Dockerfile").write_text("FROM python:3.9\n")
    found = suggest(tmp_path)
    assert [(f.value, f.path, f.source) for f in found.targets] == [
        ("musl-aarch64", "Containerfile", "FROM --platform=linux/arm64 python:3.13-alpine (musl 1.2)"),
        ("linux-x86_64", "Dockerfile", "FROM python:3.13-slim (no --platform: read as linux/amd64, glibc 2.41)"),
        ("win-amd64", "win.Dockerfile", "FROM python:3.13-windowsservercore"),
    ]
    assert unreadable(found) == [
        ("Dockerfile.j2", None, "a template, filled in before it's built: not read"),
        ("Dockerfile.template", None, "a template, filled in before it's built: not read"),
    ]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs file permissions to apply")
def test_files_and_folders_it_cant_open(tmp_path):
    workflow(tmp_path, JOB)
    workflow(tmp_path, JOB, name="secret.yml")
    (tmp_path / ".github" / "workflows" / "secret.yml").chmod(0)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "Dockerfile").write_text("FROM python:3.13\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13\n")
    (tmp_path / "Dockerfile").chmod(0)
    (tmp_path / "locked").chmod(0)
    try:
        found = suggest(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o755)
        (tmp_path / "Dockerfile").chmod(0o644)
        (tmp_path / ".github" / "workflows" / "secret.yml").chmod(0o644)
    assert where(found.targets) == [("linux-x86_64", CI, 3)]
    assert unreadable(found) == [
        (".github/workflows/secret.yml", None, "couldn't read the file (Permission denied)"),
        ("locked", None, "couldn't open the folder (Permission denied)"),
        ("Dockerfile", None, "couldn't read the file (Permission denied)"),
    ]


def test_the_same_refusal_is_listed_once(tmp_path):
    _, refused, _ = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: ${{ fromJSON(inputs.matrix) }}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    assert refused == [(5, "job test: the matrix is computed (${{ fromJSON(inputs.matrix) }})")]


def test_cli_says_the_snippet_combines_every_job(tmp_path, capsys):
    workflow(tmp_path, JOB)
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert "They combine every job, lint,\nrelease and bot jobs too, so keep only the platforms you ship to:" in out


SETUP = "jobs:\n  test:\n    runs-on: windows-latest\n    steps:\n"
WHOLE_JOB = " (neither its runner nor its Python is read)"


@pytest.mark.parametrize(
    ("steps", "refused"),
    [
        # A setup step whose uses can't be read might set the architecture: the job isn't read at all.
        ("      - name: &sp\n          a: b\n      - uses: *sp\n", (7, "can't read the uses: *sp of a step")),
        ("      - uses: >-\n          actions/setup-python@v6\n", (5, "can't read the uses: >- of a step")),
        (
            "      - uses:\n          actions/setup-python@v6\n",
            (5, "can't read the uses: actions/setup-python@v6 of a step"),
        ),
        (
            '      - uses: "actions\\x2fsetup-python@v6"\n',
            (5, 'can\'t read the uses: "actions\\x2fsetup-python@v6" of a step'),
        ),
        (
            "      - uses: actions/setup-python@v6\n        with:\n"
            "          architecture: x64\n          Architecture: x86\n",
            (8, "with: sets architecture twice"),
        ),
        (
            "      - uses: actions/setup-python@v6\n        if: matrix.arch == 'x86'\n        with:\n"
            "          python-version: '3.13'\n          architecture: x86\n",
            (6, "the actions/setup-python step has if: matrix.arch == 'x86', so it may not run"),
        ),
        (
            "      - uses: astral-sh/setup-uv@v7\n        if: runner.os == 'Linux'\n        with:\n"
            "          python-version: '3.13'\n",
            (6, "the astral-sh/setup-uv step has if: runner.os == 'Linux', so it may not run"),
        ),
        ("      - uses: ${{ inputs.action }}\n", (5, "uses: ${{ inputs.action }} is an expression")),
    ],
)
def test_setup_steps_it_cant_read_refuse_the_job(tmp_path, steps, refused):
    # (An anchored or tagged uses: isn't in the grammar at all, so its file isn't read: see test_value.)
    targets, got, found = read(tmp_path, SETUP + steps)
    assert targets == []
    assert found.pythons == []
    assert got == [(refused[0], f"job test: {refused[1]}{WHOLE_JOB}")]


def test_step_keys_are_case_sensitive(tmp_path):
    # `Uses:` isn't `uses:` to Actions, so this isn't a setup-python step and its with: changes nothing.
    targets, refused, found = read(
        tmp_path, SETUP + "      - Uses: actions/setup-python@v6\n        with:\n          architecture: x86\n"
    )
    assert targets == [("win-amd64", CI, 3)]
    assert found.pythons == [] and refused == []


def test_a_step_with_if_and_no_interpreter_inputs_is_read(tmp_path):
    targets, refused, found = read(
        tmp_path,
        SETUP + "      - uses: actions/setup-python@v6\n        if: always()\n        with:\n          cache: pip\n",
    )
    assert targets == [("win-amd64", CI, 3)]
    assert refused == [
        (5, "job test: setup-python with no python-version (it reads .python-version, or uses the runner's)")
    ]


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('          "arch\\x69tecture": x86\n', '"arch\\x69tecture"'),
        ('          "free\\x74hreaded": true\n', '"free\\x74hreaded"'),
        ("          'it''s': x\n", "'it''s'"),
    ],
)
def test_quoted_keys_with_escapes_refuse_the_file(tmp_path, line, key):
    targets, refused, _ = read(tmp_path, SETUP + "      - uses: actions/setup-python@v6\n        with:\n" + line)
    assert targets == []
    assert refused == [(7, f"uses YAML this scanner doesn't read: {line.strip()}")]
    _, refused, _ = read(tmp_path, '"contain\\x65r": x\n' + JOB)
    assert refused == [(1, 'uses YAML this scanner doesn\'t read: "contain\\x65r": x')]


def test_free_threading_starts_at_313(tmp_path):
    _, refused, found = read(
        tmp_path,
        SETUP + "      - uses: actions/setup-python@v6\n        with:\n          python-version: 3.12t\n"
        "      - uses: actions/setup-python@v6\n        with:\n          python-version: 3.13t\n",
    )
    assert [f.value for f in found.pythons] == ["3.13t"]
    assert refused == [
        (7, "job test: python-version: 3.12t asks for a free-threaded 3.12t, but free-threading starts at 3.13")
    ]


def test_freethreaded_is_a_setup_python_input_only(tmp_path):
    _, _, found = read(
        tmp_path,
        SETUP
        + "      - uses: astral-sh/setup-uv@v7\n        with:\n"
        + "          python-version: '3.13'\n          freethreaded: true\n",
    )
    assert [f.value for f in found.pythons] == ["3.13"]


@pytest.mark.parametrize(
    ("header", "refused"),
    [
        ("%YAML 1.2\n---\n", []),
        ("%YAML 1.1\n---\n", [(1, "uses YAML this scanner doesn't read: %YAML 1.1")]),
        (
            "%TAG ! tag:example.com,2000:\n---\n",
            [(1, "uses YAML this scanner doesn't read: %TAG ! tag:example.com,2000:")],
        ),
        ("%YAML 1.2\n", [(2, "uses YAML this scanner doesn't read: jobs:")]),  # %YAML must be followed by ---
        ("  ---\n", [(1, "uses YAML this scanner doesn't read: ---")]),  # indented: not a document marker
        ("---x\n", [(1, "uses YAML this scanner doesn't read: ---x")]),
        ("----\n", [(1, "uses YAML this scanner doesn't read: ----")]),
        ("--- # a comment\n", []),
        ("? jobs\n", [(1, "uses YAML this scanner doesn't read: ? jobs")]),
    ],
)
def test_directives_and_markers(tmp_path, header, refused):
    targets, got, _ = read(tmp_path, header + JOB)
    assert got == refused
    assert targets == ([] if refused else [("linux-x86_64", CI, header.count("\n") + 3)])


def test_architecture_from_a_literal_matrix(tmp_path):
    # As in numpy's windows.yml: each combination has its own runner and architecture.
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: windows-2022
            architecture: x86
          - os: windows-11-arm
            architecture: arm64
          - os: ubuntu-latest
            architecture: x86
          - {os: macos-14, architecture: x64, name: intel}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{ matrix.architecture }}
""",
    )
    assert targets == [("win32", CI, 7), ("win-arm64", CI, 9), ("macos-x86_64", CI, 13)]
    assert found.targets[0].source == "runs-on: windows-2022 (matrix.os, line 3), architecture: x86 (matrix)"
    assert found.targets[2].macos == "14.0"  # the runner's macOS stays with the Intel target
    assert refused == [(11, "job test: setup-python's architecture: x86 on ubuntu-latest isn't read")]


def test_architecture_from_the_matrix_on_a_fixed_runner(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        arch: [x64, arm64]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{ matrix.arch }}
""",
    )
    assert targets == [("linux-x86_64", CI, 3)]
    assert found.targets[0].glibc == "2.39"
    assert refused == [(3, "job test: setup-python's architecture: arm64 on ubuntu-24.04 isn't read")]


@pytest.mark.parametrize(
    ("env", "refused"),
    [
        # Quoted values over several lines aren't in the grammar, whatever they hold.
        ('  A: "x \\" # not a comment\n    y"\n', (2, 'A: "x \\" # not a comment')),
        ("  B: 'it''s # not a comment\n    either'\n", (2, "B: 'it''s # not a comment")),
        ("  C: 'one\n    two'\n", (2, "C: 'one")),
        # A plain value goes on over more-indented lines: those are text, never keys.
        ('  D: echo building\n    "quoted\n    runs-on: windows-latest\n', (4, "runs-on: windows-latest")),
    ],
)
def test_values_over_several_lines(tmp_path, env, refused):
    _, got, _ = read(tmp_path, "env:\n" + env + JOB)
    assert got == [(refused[0], f"uses YAML this scanner doesn't read: {refused[1]}")]


def test_plain_values_over_several_lines_are_skipped(tmp_path):
    targets, refused, _ = read(
        tmp_path,
        """\
jobs:
  test:
    if: github.event_name == 'push' ||
      (github.event_name == 'pull_request' && "quoted
      - looks like an item
      [ or a list {
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-14]  # windows-latest
    steps:
      - run: pip install
          numpy
      - run:
          tox -e py
""",
    )
    assert refused == []
    assert targets == [("linux-x86_64", CI, 10), ("macos-arm64", CI, 10)]


def test_dockerfile_names(tmp_path):
    for name in ("dockerfile.dev", "Containerfile.prod", "Dockerfile.md", "dockerfile.py", "Dockerfile.dockerignore"):
        (tmp_path / name).write_text("FROM python:3.13-slim\n")
    for skipped in ("test", "example"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "Dockerfile").write_text("FROM python:3.9\n")
    assert [f.path for f in suggest(tmp_path).targets] == ["Containerfile.prod", "dockerfile.dev"]


def test_values_on_the_next_line(tmp_path):
    # Valid YAML that real workflows use (run:, key:, needs:): the value belongs to its key, never read as a line of
    # its own, and refused where wheelgap would read it.
    targets, refused, _ = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    needs:
      [
        lint,
      ]
    steps:
      - run:
          tox -e py -- -v
      - uses: actions/cache@v4
        with:
          key:
            ${{ runner.os }}-x
  other:
    runs-on:
      windows-latest
""",
    )
    assert targets == [("linux-x86_64", CI, 3)]
    assert refused == [(16, "job other: runs-on: a value over several lines isn't read")]


X86 = (
    "      - uses: actions/setup-python@v6\n        with:\n"
    "          python-version: '3.13'\n          architecture: x86\n"
)


@pytest.mark.parametrize(
    ("before", "refused"),
    [
        # A quote or bracket inside a plain block scalar is an ordinary character: read.
        ("    env:\n      A: x:'y # a comment\n", None),
        ("    env:\n      A: it's [not a list\n", None),
        # Further lines of a plain scalar are text, whatever they start with: read.
        ('    steps:\n      - run: echo building\n          "quoted\n', None),
        ("    steps:\n      - run: echo building\n          - 'quoted\n", None),
        ("    steps:\n      - run: echo building\n          [not, closed\n", None),
        ("    steps:\n      - run: echo building\n          {not closed\n", None),
        # (`: ` in a further line would make the YAML invalid, so that's refused.)
        ("    steps:\n      - run: echo building\n          {not: closed\n", (6, "{not: closed")),
        # Outside the grammar (the review's cases): the file isn't read.
        ("    env: {A: x:'y}\n", (4, "env: {A: x:'y}")),
        ('    env: {A: x:"y, B: z}\n', (4, 'env: {A: x:"y, B: z}')),
        ('    env: {"A":"b #"}\n', (4, 'env: {"A":"b #"}')),
        ("    env: {A: a - 'b}\n", (4, "env: {A: a - 'b}")),
        ('    env: {A: &a "b #"}\n', (4, 'env: {A: &a "b #"}')),
        ('    env: [!!str "b #"]\n', (4, 'env: [!!str "b #"]')),
        ('    env: {? "a #" : b}\n', (4, 'env: {? "a #" : b}')),
        ("    env: [a\n      'b]\n", (4, "env: [a")),
        ('    env:\n      A: &a "b\n', (5, 'A: &a "b')),
        ('    env: ""#K: \'\n', (4, 'env: ""#K: \'')),
    ],
)
def test_quotes_and_brackets_are_read_as_yaml_or_refused(tmp_path, before, refused):
    steps = "" if "steps:" in before else "    steps:\n"
    targets, got, found = read(tmp_path, SETUP.split("    steps:\n")[0] + before + steps + X86)
    if refused is None:
        assert targets == [("win32", CI, 3)]
        assert [f.value for f in found.pythons] == ["3.13"]
        assert got == []
    else:
        assert targets == [] and found.pythons == []
        assert got == [(refused[0], f"uses YAML this scanner doesn't read: {refused[1]}")]


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        ("    env:\n      A: &a |\n        jobs: x\n", (5, "uses YAML this scanner doesn't read: A: &a |")),
        ("    env:\n      A: !!str >-\n        jobs: x\n", (5, "uses YAML this scanner doesn't read: A: !!str >-")),
        ("    steps:\n      - &s !!str |\n        x\n", (5, "uses YAML this scanner doesn't read: - &s !!str |")),
        ("    env:\n      A:\n        |\n        x\n", (6, "uses YAML this scanner doesn't read: |")),
    ],
)
def test_block_scalars_with_properties_refuse_the_file(tmp_path, line, refused):
    targets, got, _ = read(tmp_path, SETUP.split("    steps:\n")[0] + line)
    assert targets == []
    assert got == [refused]


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        # NBSP isn't whitespace to YAML: it's part of the label, which no runner has.
        (
            "jobs:\n  test:\n    runs-on: windows-latest\u00a0\n",
            [(3, "job test: windows-latest\u00a0 isn't a GitHub-hosted runner wheelgap knows")],
        ),
        (
            "jobs:\n  test:\n    runs-on: windows-latest\u00a0# not a comment\n",
            [(3, "job test: windows-latest\u00a0# not a comment isn't a GitHub-hosted runner wheelgap knows")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{\u00a0matrix.os }}\n",
            [(3, "job test: runs-on: ${{\u00a0matrix.os }} is an expression")],
        ),
        # A line starting with NBSP is at column 0: a top-level key, so the job has no runs-on, as YAML reads it.
        (
            "jobs:\n  test:\n\u00a0   runs-on: windows-latest\n",
            [(3, "uses YAML this scanner doesn't read: \u00a0   runs-on: windows-latest")],
        ),
    ],
)
def test_unicode_spaces_are_ordinary_characters(tmp_path, text, refused):
    targets, got, _ = read(tmp_path, text)
    assert targets == []
    assert got == refused


@pytest.mark.parametrize("brk", ["\r", "\x85", "\u2028", "\u2029"])
def test_other_line_breaks_refuse_the_file(tmp_path, brk):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_bytes(
        ("jobs:\n  test:\n    runs-on: ubuntu-latest # a" + brk + "b\n").encode("utf-8")
    )
    found = suggest(tmp_path)
    assert found.targets == []
    assert unreadable(found) == [(CI, 3, f"uses YAML this scanner doesn't read: a line break other than \\n ({brk!r})")]


def test_crlf_and_tab_indented_comments_are_fine(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_bytes(
        b"jobs:\r\n\t# a comment\r\n  test:\r\n    runs-on: ubuntu-latest\r\n"
    )
    found = suggest(tmp_path)
    assert where(found.targets) == [("linux-x86_64", CI, 4)]
    assert found.unreadable == []


MATRIX_ARCH = """\
jobs:
  test:
    runs-on: ${{{{ matrix.os }}}}
    strategy:
      matrix:
        include:
{entries}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{{{ matrix.arch }}}}
"""


def test_a_combination_without_the_architecture_uses_the_runners_own(tmp_path):
    entries = [
        "          - {os: windows-latest, arch: x86}",
        "          - {os: windows-2025}",
        "          - {os: windows-11-arm, arch: ''}",
    ]
    targets, refused, found = read(tmp_path, MATRIX_ARCH.format(entries="\n".join(entries)))
    assert refused == []
    assert targets == [("win32", CI, 7), ("win-amd64", CI, 8), ("win-arm64", CI, 9)]
    # The same matrix in another order gives the same answer, line for line.
    workflow(tmp_path, MATRIX_ARCH.format(entries="\n".join(reversed(entries))))
    again = suggest(tmp_path)
    assert sorted(where(again.targets)) == sorted([("win32", CI, 9), ("win-amd64", CI, 8), ("win-arm64", CI, 7)])


def test_a_refusal_partway_through_the_matrix_keeps_no_targets(tmp_path):
    # The second combination has no os, so runs-on is '' there: nothing from the job is suggested, in either order.
    for entries in (
        ["          - {os: windows-latest}", "          - {arch: x86}"],
        ["          - {arch: x86}", "          - {os: windows-latest}"],
    ):
        targets, refused, _ = read(tmp_path, MATRIX_ARCH.format(entries="\n".join(entries)))
        assert targets == []
        assert refused == [(3, "job test: matrix.os isn't set in every matrix combination")]


@pytest.mark.parametrize(
    ("matrix", "refused"),
    [
        # Outside the grammar: the file isn't read.
        ("        os:\n          [ubuntu-latest, windows-latest]\n", (7, NOT_READ + "[ubuntu-latest, windows-latest]")),
        ("        os: ['ubuntu-latest,\n          windows-latest']\n", (6, NOT_READ + "os: ['ubuntu-latest,")),
        ("        os: [ubuntu-latest,\n          windows-latest]]\n", (6, NOT_READ + "os: [ubuntu-latest,")),
        ("        os:\n          - ubuntu-latest\n          arch: x64\n", (8, NOT_READ + "arch: x64")),
        ("        os: [{a : b}]\n", (6, NOT_READ + "os: [{a : b}]")),
        ('        os: [{a: "b" c}]\n', (6, NOT_READ + 'os: [{a: "b" c}]')),
        # Read, but not a matrix the scan can be sure of, so the job might run nothing: the job isn't read.
        (
            "        os: ubuntu-latest\n          windows-latest\n",
            (6, "job test: os: a value over several lines isn't read" + JOB_NOT_READ),
        ),
        ("        os:\n", (6, "job test: os has no value" + JOB_NOT_READ)),
        ("        os: []\n", (5, "job test: matrix.os is an empty list" + JOB_NOT_READ)),
        (
            "        include:\n          - os: ubuntu-latest\n            os: windows-latest\n",
            (8, NOT_READ + "os: windows-latest"),
        ),
        (
            "        os: [ubuntu-latest]\n        os: [windows-latest]\n",
            (7, NOT_READ + "os: [windows-latest]"),
        ),
        ("        os: {a: b}\n", (5, "job test: the matrix has values that aren't a plain list" + JOB_NOT_READ)),
        (
            "        os: [ubuntu-latest, windows-latest]\n        exclude:\n"
            "          - os: ubuntu-latest\n            arch: x64\n",
            (5, "job test: the matrix excludes a key it doesn't have" + JOB_NOT_READ),
        ),
        # Computed: refused where it's used (here, by runs-on).
        (
            "        os: [ubuntu-latest]\n        include: ${{ fromJson(inputs.extra) }}\n",
            (7, "job test: the matrix's include is computed (${{ fromJson(inputs.extra) }})"),
        ),
    ],
)
def test_matrix_values(tmp_path, matrix, refused):
    text = "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n" + matrix
    targets, got, _ = read(tmp_path, text)
    assert targets == []
    assert got == [refused]


def test_a_matrix_that_may_be_empty_refuses_a_literal_job(tmp_path):
    # runs-on doesn't use the matrix, but if its exclude empties it GitHub runs no job; it can't tell here.
    targets, refused, _ = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        py: [3.13]\n"
        "        exclude:\n          - py: '3.13'\n",
    )
    assert targets == []
    assert refused == [
        (8, "job test: can't tell whether GitHub counts '3.13' and '3.13' as the same value" + JOB_NOT_READ)
    ]
    # Values that aren't plain still give a combination for sure when nothing is excluded.
    targets, refused, _ = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        py: [{v: '3.13'}]\n",
    )
    assert targets == [("linux-x86_64", CI, 3)]
    assert refused == []


def test_a_stray_line_deep_in_a_job_refuses_the_file(tmp_path):
    _, refused, found = read(tmp_path, SETUP + "      - uses: actions/checkout@v5\n        with:\n          ? weird\n")
    assert found.targets == []
    assert refused == [(7, "uses YAML this scanner doesn't read: ? weird")]


def test_python_from_a_matrix_key_some_combinations_lack(tmp_path):
    _, refused, found = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        include:\n"
        "          - {python: '3.13'}\n          - {other: x}\n"
        "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
        "          python-version: ${{ matrix.python }}\n",
    )
    assert found.pythons == []
    assert refused == [(12, "job test: matrix.python isn't set in every matrix combination")]


def test_prettier_lists_on_the_next_line(tmp_path):
    # The review's case: Prettier puts a long flow list on the lines after its key.
    targets, refused, found = read(
        tmp_path,
        """\
on: push
jobs:
  b:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os:
          [
            windows-latest,
            ubuntu-24.04-arm, # c
            macos-latest,
          ]
        include:
          [
            { os: windows-latest, arch: x86 },
            { os: macos-latest, arch: x64 },
          ]
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          architecture: ${{ matrix.arch }}
""",
    )
    assert refused == []
    assert sorted(t for t, _, _ in targets) == ["linux-aarch64", "macos-x86_64", "win32"]
    assert [f.value for f in found.pythons] == ["3.12"]


def test_the_same_line_is_listed_once_per_value(tmp_path):
    # '3.10' and 3.10 on one line are different values: the quoted one is read, the plain one (3.1) refused.
    _, refused, found = read(
        tmp_path,
        "jobs:\n  b:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n"
        "        include: [{py: '3.10'}, {py: 3.10}]\n    steps:\n      - uses: actions/setup-python@v5\n"
        "        with:\n          python-version: ${{ matrix.py }}\n",
    )
    assert [f.value for f in found.pythons] == ["3.10"]
    assert refused == [(6, "job b: unquoted 3.10 is the number 3.1 in YAML (quote it)")]


def test_anchors_aliases_and_escapes_are_accepted_but_never_read(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
on:
  push:
    paths: &paths
      - src/**
  pull_request:
    paths: *paths
env:
  WINDOWS_PATH: "C:\\\\tools\\\\bin"
x-defs:
  runner: &runner
    - windows-latest
  strategy: &strategy
    matrix:
      os: [a]
jobs:
  test:
    runs-on: windows-latest
    env: {MATRIX: {a: [b, c]}, "QUOTED": 'x'}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: '3.13'
  aliased:
    runs-on: *runner
  strategy:
    runs-on: windows-latest
    strategy: *strategy
  escaped:
    runs-on: "windows\\u002dlatest"
""",
    )
    assert targets == [("win-amd64", CI, 17)]
    assert [f.value for f in found.pythons] == ["3.13"]
    assert refused == [
        (24, "job aliased: runs-on: an alias isn't read"),
        (
            27,
            "job strategy: strategy: *strategy is an alias, which isn't read" + JOB_NOT_READ,
        ),
        (29, "job escaped: runs-on: a quoted value with escapes isn't read"),
    ]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        # A line under a key or item that already has its value would be invalid YAML; it could also hide a key.
        ('jobs:\n  test:\n    runs-on: "windows-latest"\n      container: x\n', 4),
        ("jobs:\n  test:\n    runs-on: [windows-latest]\n      container: x\n", 4),
        ("jobs:\n  test:\n    runs-on: windows-latest\n    steps:\n      - 'x'\n        uses: y\n", 6),
        # Keys and items under one parent, or keys that don't line up.
        ("jobs:\n  test:\n    runs-on: windows-latest\n    - x\n", 4),
        ("jobs:\n  test:\n      runs-on: windows-latest\n    steps: []\n", 4),
    ],
)
def test_lines_that_dont_fit_the_tree_refuse_the_file(tmp_path, text, line):
    targets, refused, _ = read(tmp_path, text)
    assert targets == []
    assert [r[0] for r in refused] == [line]
    assert refused[0][1].startswith(NOT_READ)


def test_every_comparison_in_an_exclude_is_made(tmp_path):
    # The os already differs, but whether py matches can't be told (3.13 vs '3.13'): refused, not skipped.
    targets, refused, _ = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-latest]\n        py: [3.13]\n"
        "        exclude:\n          - {os: macos-latest, py: '3.13'}\n",
    )
    assert targets == []
    assert refused == [
        (9, "job test: can't tell whether GitHub counts '3.13' and '3.13' as the same value" + JOB_NOT_READ)
    ]


X86_ON_WINDOWS = "jobs:\n  test:\n    runs-on: windows-latest\n    steps:\n"


@pytest.mark.parametrize(
    ("steps", "line"),
    [
        # Nothing may be indented under `key: {...}` or `key: {}` (PyYAML rejects it, and it would add entries).
        (
            "      - uses: actions/setup-python@v6\n        with: {python-version: '3.12'}\n"
            "           architecture: x86\n",
            7,
        ),
        ("      - uses: actions/setup-python@v6\n        with: {}\n          architecture: x86\n", 7),
        # An alias with no anchor before it.
        ("      - uses: actions/setup-python@v6\n        with: *nope\n", 6),
        # A block scalar whose later line dedents below its first one, but not to the key.
        ("      - run: |\n          echo a\n        echo b\n", 7),
        ("      - run: |2\n          echo a\n         echo b\n", 7),  # |2: its lines are at the key's column + 2
        ("      - run: |4\n          echo a\n", 6),
        # A multi-line flow list closed with junk after `]`, or a tab where an item's indentation ends.
        ("      - run: x\n        env: [\n          a,\n        ]x\n", 8),
        ("      - run: x\n        env: [\n          \ta,\n        ]\n", 7),
        # A tab where a plain value's further line starts.
        ("      - run: echo\n          \tmore\n", 6),
    ],
)
def test_more_lines_outside_the_grammar(tmp_path, steps, line):
    targets, refused, _ = read(tmp_path, X86_ON_WINDOWS + steps)
    assert targets == []
    assert [r[0] for r in refused] == [line]
    assert refused[0][1].startswith(NOT_READ)


def test_an_alias_after_its_anchor_is_accepted(tmp_path):
    targets, refused, _ = read(tmp_path, "defs: &d\n  a: b\nalso: *d\n" + JOB)
    assert targets == [("linux-x86_64", CI, 6)]
    assert refused == []


def test_a_comment_after_the_last_item_of_a_multi_line_list(tmp_path):
    targets, refused, _ = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [\n"
        "          ubuntu-latest, # one\n          macos-latest # two\n        ]\n",
    )
    assert refused == []
    assert targets == [("linux-x86_64", CI, 7), ("macos-arm64", CI, 8)]


def test_block_scalars_are_skipped_up_to_the_key_column(tmp_path):
    # The block scalar ends at `uses:`, which is at the column of `run:` (not the `-`): the step after it is read.
    targets, refused, _ = read(
        tmp_path,
        X86_ON_WINDOWS + "      - run: |\n          echo hi\n        uses: actions/setup-python@v6\n"
        "        with:\n          python-version: '3.12'\n          architecture: x86\n",
    )
    assert targets == [("win32", CI, 3)]
    assert refused == []


@pytest.mark.parametrize(
    ("with_", "reason"),
    [
        ("        with: *w\n", "with: *w isn't read"),
        ('        with: "a\\tb"\n', 'with: "a\\tb" isn\'t read'),
        ("        with: |\n          architecture: x86\n", "with: | isn't read"),
    ],
)
def test_a_setup_step_whose_with_isnt_read_refuses_the_job(tmp_path, with_, reason):
    targets, refused, found = read(
        tmp_path, "defs: &w\n  a: b\n" + X86_ON_WINDOWS + "      - uses: actions/setup-python@v6\n" + with_
    )
    assert targets == [] and found.pythons == []
    assert refused == [(8, f"job test: {reason}{JOB_NOT_READ}")]


@pytest.mark.parametrize(
    ("architecture", "reason"),
    [
        ("*x86", "architecture: *x86 isn't read"),
        ('"x\\u0038\\u0036"', 'architecture: "x\\u0038\\u0036" isn\'t read'),
        ("|\n            x86", "architecture: | isn't read"),
    ],
)
def test_an_architecture_that_isnt_read_refuses_the_target(tmp_path, architecture, reason):
    targets, refused, found = read(
        tmp_path,
        "defs: &x86\n  a: b\n" + X86_ON_WINDOWS + "      - uses: actions/setup-python@v6\n        with:\n"
        f"          python-version: '3.12'\n          architecture: {architecture}\n",
    )
    assert targets == []
    assert [f.value for f in found.pythons] == ["3.12"]
    assert refused == [(10, f"job test: {reason}, so the job's target isn't read")]


@pytest.mark.parametrize(
    ("include", "targets", "refused"),
    [
        # An include of objects still makes a combination when it's a non-empty list...
        ("        include:\n          - {cfg: {a: b}}\n", [("linux-x86_64", CI, 3)], []),
        # ...but not when it isn't a list.
        ("        include: {cfg: {a: b}}\n", [], [(5, "job test: can't read the matrix's include" + JOB_NOT_READ)]),
    ],
)
def test_a_matrix_of_objects_runs_only_with_a_combination_for_sure(tmp_path, include, targets, refused):
    got_targets, got_refused, _ = read(
        tmp_path, "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n" + include
    )
    assert got_targets == targets
    assert got_refused == refused


@pytest.mark.parametrize(
    ("text", "line"),
    [
        # A key twice in one mapping, or an anchor defined twice: invalid YAML to PyYAML (GitHub's reading of it
        # isn't certain), so the file isn't read.
        (JOB + "  test:\n    runs-on: windows-latest\n", 4),
        (X86_ON_WINDOWS + "      - uses: actions/setup-python@v6\n        with: {}\n        with:\n", 7),
        ("a: &x\n  b: c\nd: &x\n  e: f\n" + JOB, 3),
    ],
)
def test_keys_and_anchors_twice_refuse_the_file(tmp_path, text, line):
    targets, refused, _ = read(tmp_path, text)
    assert targets == []
    assert [r[0] for r in refused] == [line]
    assert refused[0][1].startswith(NOT_READ)
