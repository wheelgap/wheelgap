"""Real lockfiles from open-source projects (#227), read offline: tests/fixtures/real/ and its sources.toml.

scripts/check_real_lockfiles.py has the checks; the weekly lockfile-corpus workflow runs them on each project's
current lockfile.
"""

import http.client
import importlib.util
import io
import re
import sys
import urllib.error
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

import wheelgap
from wheelgap.check import Status, check
from wheelgap.sources import SourceError, file_kind, load_file


def _script():
    path = Path(__file__).parents[1] / "scripts" / "check_real_lockfiles.py"
    spec = importlib.util.spec_from_file_location("wheelgap_check_real_lockfiles", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()
REAL = Path(__file__).parent / "fixtures" / "real"


class Expect(NamedTuple):
    packages: int  # packages wheelgap reads
    files: int  # files it reads from them, and how many are wheels
    wheels: int
    na: int  # `n/a` cells over script.TARGETS: a misread marker or dependency graph moves this
    package_markers: int  # packages it reads markers for
    edge_markers: int  # dependency edges (uv.lock, Poetry 1.x) it reads markers for
    skipped: dict[str, str]  # package: why
    known: list[tuple[str, str]]  # (name, version) pairs it must read


# The file counts match the URLs in each lock, except that webcolors' pdm.lock lists coverage twice (as coverage and
# coverage[toml]), and wheelgap reads it once. A Pipfile.lock records no filenames.
EXPECTED = {
    "pluggy/uv.lock": Expect(
        22, 264, 243, 198, 0, 4, {"pluggy": "editable"}, [("pytest", "9.1.1"), ("pre-commit", "4.6.0")]
    ),
    "iniconfig/uv.lock": Expect(
        14, 230, 217, 270, 0, 5, {"iniconfig": "editable"}, [("hypothesis", "6.168.5"), ("hypothesis", "6.170.15")]
    ),
    "tomlkit/poetry.lock": Expect(54, 485, 431, 518, 12, 0, {}, [("filelock", "3.19.1"), ("filelock", "4.0.12")]),
    "cleo/poetry.lock": Expect(53, 376, 323, 318, 0, 11, {}, [("pytest", "8.0.2"), ("towncrier", "23.11.0")]),
    "webcolors/pdm.lock": Expect(10, 150, 140, 198, 4, 0, {}, [("pytest", "8.4.2"), ("coverage", "7.11.0")]),
    "pdfly/pylock.toml": Expect(
        104, 103, 102, 0, 0, 0, {"pdfly": "directory"}, [("cryptography", "46.0.5"), ("pypdf", "6.9.0")]
    ),
    "pipenv/Pipfile.lock": Expect(85, 0, 0, 1004, 81, 0, {}, [("virtualenv", "21.15.1"), ("sphinx", "9.0.4")]),
}
LICENCE_TEXT = {
    "MIT": "Permission is hereby granted",
    "BSD-2-Clause": "Redistribution and use",
    "BSD-3-Clause": "Redistribution and use",
    "Apache-2.0": "Apache License",
}


def test_manifest_lists_every_copy_with_its_provenance():
    entries = script.manifest()
    assert sorted(e["file"] for e in entries) == sorted(EXPECTED)
    on_disk = sorted(p.relative_to(REAL).as_posix() for p in REAL.glob("*/*") if p.name != "LICENSE")
    assert on_disk == sorted(EXPECTED)
    for e in entries:
        assert re.fullmatch(r"[0-9a-f]{40}", e["commit"]), e
        assert Path(e["path"]).name == Path(e["file"]).name, e
        assert (REAL / e["file"]).stat().st_size < 100_000, e  # keep the copies small
        # The project's licence file, from the same commit.
        assert LICENCE_TEXT[e["licence"]] in (REAL / e["file"]).with_name("LICENSE").read_text(encoding="utf-8"), e
    # Two uv.lock, two poetry.lock (Poetry 1.x and 2.x), a pdm.lock, a pylock.toml and a Pipfile.lock.
    kinds = Counter(file_kind(REAL / e["file"]) for e in entries)
    assert kinds == {"uv": 2, "poetry": 2, "pdm": 1, "pylock": 1, "pipfile": 1}
    generated = {(REAL / f).read_text(encoding="utf-8").split("\n")[0] for f in EXPECTED if f.endswith("poetry.lock")}
    assert sorted(major for line in generated for major in re.findall(r"by Poetry (\d)\.", line)) == ["1", "2"]


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_real_lockfile_reads_cleanly(file):
    expect = EXPECTED[file]
    report = script.inspect(REAL / file)
    assert report.findings == []
    source = report.source
    assert len(source.packages) == expect.packages
    files = [f for p in source.packages for f in p.files or []]
    assert (len(files), sum(f.endswith(".whl") for f in files)) == (expect.files, expect.wheels)
    rows = check([source], script.TARGETS)
    assert sum(s is Status.NOT_NEEDED for r in rows for s in r.results.values()) == expect.na
    assert sum(p.markers is not None for p in source.packages) == expect.package_markers
    assert sum(bool(m) for _, _, m, _ in script._edges(source)) == expect.edge_markers
    assert {p.name: p.skip for p in source.packages if p.skip} == expect.skipped
    assert set(expect.known) <= {(p.key, p.version) for p in source.packages}


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_real_lockfile_offline_has_no_unknowns(file, fake_pypi):
    if file.endswith("Pipfile.lock"):  # it records hashes, not filenames: every package needs the index
        with pytest.raises(SourceError, match="needs the index for 85 packages"):
            wheelgap.check_lockfile(REAL / file, script.TARGETS, offline=True)
    else:
        targets = script.TARGETS
        if file == "webcolors/pdm.lock":  # its [[metadata.targets]] say >=3.10, and `pdm install` refuses older ones
            with pytest.raises(SourceError, match="this lock doesn't cover 3.8 linux-x86_64, .*`pdm install` fails"):
                wheelgap.check_lockfile(REAL / file, targets, offline=True)
            targets = [t for t in targets if (t.major, t.minor) >= (3, 10)]
        results = wheelgap.check_lockfile(REAL / file, targets, offline=True)
        assert results
        assert not [r.name for r in results if Status.UNKNOWN in r.statuses.values()]
    assert fake_pypi == []


UV_HEAD = 'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
UV_ROOT = '[[package]]\nname = "app"\nversion = "0.1.0"\nsource = { editable = "." }\n'
UV_A = '\n[[package]]\nname = "a"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
A_WHEEL = 'wheels = [{ url = "https://x/a-1.0-py3-none-any.whl" }]\n'
PYLOCK_A = 'lock-version = "1.0"\n[[packages]]\nname = "a"\nversion = "1.0"\n'
PIPFILE_META = '"_meta": {"pipfile-spec": 6}'
MISREAD, CHANGED, WARNING = "misread", "upstream changed", "warning"


@pytest.mark.parametrize(
    ("name", "text", "kind", "finding"),
    [
        ("uv.lock", "this isn't = = toml", MISREAD, "can't read it: TOMLDecodeError"),
        ("uv.lock", UV_HEAD, MISREAD, "no packages"),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n' + UV_A,
            CHANGED,
            "'a' '1.0': the lock lists no files for it",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n' + UV_A.replace('"1.0"', '"one"') + A_WHEEL,
            MISREAD,
            "'a': version 'one' doesn't parse",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a", marker = "os_name =! \'nt\'" }]\n' + UV_A + A_WHEEL,
            MISREAD,
            "dependency on 'a': marker \"os_name =! 'nt'\" doesn't parse",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + UV_A + A_WHEEL,  # nothing depends on it
            MISREAD,
            "'a' '1.0': n/a on every target, with no marker on the way in to it",
        ),
        (
            "uv.lock",  # PyPy only: no target installs it, rightly
            UV_HEAD
            + UV_ROOT
            + 'dependencies = [{ name = "a", marker = "implementation_name == \'pypy\'" }]\n'
            + UV_A
            + A_WHEEL,
            WARNING,
            "'a' '1.0': n/a on every target, but only through markers",
        ),
        (
            "uv.lock",  # `wheels` renamed
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n' + UV_A + A_WHEEL.replace("wheels", "wheel"),
            MISREAD,
            "the file has 1 wheels, but wheelgap read 0",
        ),
        (
            "uv.lock",  # `marker` renamed
            UV_HEAD
            + UV_ROOT
            + 'dependencies = [{ name = "a", markers = "sys_platform == \'win32\'" }]\n'
            + UV_A
            + A_WHEEL,
            MISREAD,
            "the file has 1 dependency markers, but wheelgap read 0",
        ),
        (
            "uv.lock",
            UV_HEAD.replace("revision = 3", "revision = 9")
            + UV_ROOT
            + 'dependencies = [{ name = "a" }]\n'
            + UV_A
            + A_WHEEL,
            CHANGED,
            "revision '9' isn't one the tests cover",
        ),
        (
            "pylock.toml",
            PYLOCK_A + '[[packages.wheels]]\nurl = "https://x/a-1.0.whl"\n',
            MISREAD,
            "'a' '1.0': wheel name 'a-1.0.whl' doesn't parse",
        ),
        (
            "pylock.toml",
            PYLOCK_A + '[packages.sdist]\nurl = "https://x/a-1.0.exe"\n',
            MISREAD,
            "'a' '1.0': 'a-1.0.exe' is neither a wheel nor an sdist",
        ),
        (
            "pylock.toml",  # `marker` renamed
            PYLOCK_A
            + "markers = \"sys_platform == 'win32'\"\n"
            + '[[packages.wheels]]\nurl = "https://x/a-1.0-py3-none-any.whl"\n',
            MISREAD,
            "the file has 1 package markers, but wheelgap read 0",
        ),
        (
            "pdm.lock",
            '[metadata]\nlock_version = "4.5.0"\n[[package]]\nname = "a"\nversion = "1.0"\ngit = "https://x/a.git"\n',
            CHANGED,
            "'a' '1.0': skipped as 'git'",
        ),
        (
            "poetry.lock",
            '[[package]]\nname = "a"\nversion = "1.0"\nmarkers = "python_version >> \'3\'"\n'
            'files = [{ file = "a-1.0-py3-none-any.whl", hash = "sha256:00" }]\n'
            '[metadata]\nlock-version = "2.1"\n',
            MISREAD,
            "'a' '1.0': marker \"python_version >> '3'\" doesn't parse",
        ),
        (
            "Pipfile.lock",
            "{" + PIPFILE_META + ', "default": {"a": {"version": "==1.0"}, "b": {"version": "==1.0", "index": "x"}}}',
            CHANGED,
            "'b' '1.0': skipped as 'private index'",
        ),
        (
            "Pipfile.lock",  # `markers` renamed
            "{" + PIPFILE_META + ', "default": {"a": {"version": "==1.0", "marker": "os_name == \'nt\'"}}}',
            MISREAD,
            "the file has 1 package markers, but wheelgap read 0",
        ),
        ("Pipfile.lock", '{"default": {"a": {"version": "==1.0"}}}', CHANGED, "pipfile-spec 'None' isn't one"),
    ],
)
def test_inspect_finds(tmp_path, name, text, kind, finding):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    report = script.inspect(path)
    assert any(m.startswith(finding) for m in report.of(kind)), report.findings


def test_inspect_flags_a_drop_in_wheels_against_the_pinned_copy(tmp_path):
    live = tmp_path / "uv.lock"  # the same project, with its wheels lost under a key wheelgap and the counts miss
    live.write_text(
        UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n' + UV_A + 'sdist = { url = "https://x/a-1.0.tar.gz" }\n'
        'binaries = [{ url = "https://x/a-1.0-py3-none-any.whl" }]\n',
        encoding="utf-8",
    )
    assert script.inspect(live).findings == []
    report = script.inspect(live, pinned=REAL / "pluggy/uv.lock")
    assert "0.0 wheels per package, down from 11.6 in the pinned copy" in report.of(MISREAD)


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_inspect_against_its_own_pinned_copy_finds_nothing(file):
    assert script.inspect(REAL / file, pinned=REAL / file).findings == []


# Keys renamed to names the raw counts don't know (#264): each passes the counts, so only the pinned copy catches it.
@pytest.mark.parametrize(
    ("file", "old", "new", "findings"),
    [
        (
            "pluggy/uv.lock",
            "marker = ",
            "when = ",
            [
                "0.0 markers per package, down from 0.2 in the pinned copy",
                "0% of cells n/a, down from 13% in the pinned copy",
            ],
        ),
        (
            "pluggy/uv.lock",  # the project's root isn't found, so uv's graph has nowhere to start
            'editable = "."',
            'editable = "./"',
            [
                "21 packages, but wheelgap read no dependency graph",
                "0.0 dependency edges per package, down from 1.2 in the pinned copy",
                "0.0 markers per package, down from 0.2 in the pinned copy",
                "0% of cells n/a, down from 13% in the pinned copy",
            ],
        ),
        ("pluggy/uv.lock", "sdist = ", "source-dist = ", ["0.0 sdists per package, down from 1.0 in the pinned copy"]),
        (
            "pluggy/uv.lock",
            "\nrequires-python = ",
            "\npython-requires = ",
            ["no requires-python read, but the pinned copy has '>=3.10'"],
        ),
        (
            "cleo/poetry.lock",  # Poetry 1.x
            "[package.dependencies]",
            "[package.requires]",
            [
                "53 packages, but wheelgap read no dependency graph",
                "0.0 dependency edges per package, down from 1.1 in the pinned copy",
                "0.0 markers per package, down from 0.2 in the pinned copy",
                "0% of cells n/a, down from 8% in the pinned copy",
            ],
        ),
    ],
)
def test_inspect_flags_a_renamed_key_against_the_pinned_copy(tmp_path, file, old, new, findings):
    live = tmp_path / Path(file).name
    text = (REAL / file).read_text(encoding="utf-8")
    assert old in text
    live.write_text(text.replace(old, new), encoding="utf-8")
    assert script.inspect(live, pinned=REAL / file).findings == [(MISREAD, f) for f in findings]


def test_inspect_lets_poetry_drop_its_graph_with_a_warning():
    # cleo's live lock moved from Poetry 1.x to lock-version 2.1, which records markers on packages, not edges.
    report = script.inspect(REAL / "tomlkit/poetry.lock", pinned=REAL / "cleo/poetry.lock")
    assert report.findings == [
        (
            WARNING,
            "lock-version '2.1', with no dependency graph: refreshing the pinned copy would lose the corpus's "
            "Poetry 1.x lock (pick another project for it)",
        )
    ]


@pytest.mark.parametrize(
    ("name", "text", "finding"),
    [
        (
            "uv.lock",  # two packages and no root: no graph, so every package looks needed
            UV_HEAD + UV_A + A_WHEEL + UV_A.replace('"a"', '"b"') + A_WHEEL,
            "2 packages, but wheelgap read no dependency graph",
        ),
        (
            "uv.lock",
            UV_HEAD.replace('requires-python = ">=3.10"', "requires-python = 3.10") + UV_ROOT + UV_A + A_WHEEL,
            "the file's requires-python is '3.1', but wheelgap read 'None'",
        ),
        (
            "pylock.toml",
            'requires-python = [">=3.10"]\n'
            + PYLOCK_A
            + '[[packages.wheels]]\nurl = "https://x/a-1.0-py3-none-any.whl"\n',
            "the file's requires-python is \"['>=3.10']\", but wheelgap read 'None'",
        ),
    ],
)
def test_inspect_flags_a_missing_graph_or_requires_python(tmp_path, name, text, finding):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    assert finding in script.inspect(path).of(MISREAD)


@pytest.mark.parametrize("field", ["name", "version"])  # wheelgap refuses the name; the version gets printed
def test_inspect_escapes_lockfile_text(tmp_path, monkeypatch, capsys, field):
    hostile = "a\\n::error::pwned ```\\n"
    fields = {"name": "a", "version": "1.0", field: hostile}
    path = tmp_path / "pylock.toml"
    path.write_text(
        f'lock-version = "1.0"\n[[packages]]\nname = "{fields["name"]}"\nversion = "{fields["version"]}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(script, "download", lambda repo, p, folder: path)
    script.main(["pdfly"])
    out = capsys.readouterr().out
    assert "pwned" in out
    assert not [line for line in out.splitlines() if line.lstrip().startswith("::")]
    assert "`" not in out


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(*outcomes):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    return urlopen, calls


def _http(code):
    return urllib.error.HTTPError("https://x", code, "no", {}, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("outcomes", "sleeps"),
    [
        ([_http(503), http.client.RemoteDisconnected("bye"), b"lock"], [5, 10]),
        ([urllib.error.URLError("no route"), b"lock"], [5]),
        ([b"lock"], []),
    ],
)
def test_download_retries(tmp_path, monkeypatch, outcomes, sleeps):
    urlopen, calls = _urlopen(*outcomes)
    slept = []
    monkeypatch.setattr(script.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(script, "_sleep", slept.append)
    path = script.download("pytest-dev/pluggy", "uv.lock", tmp_path)
    assert path.read_bytes() == b"lock"
    assert calls == ["https://raw.githubusercontent.com/pytest-dev/pluggy/HEAD/uv.lock"] * len(outcomes)
    assert slept == sleeps


@pytest.mark.parametrize(
    ("outcomes", "error", "match"),
    [
        ([_http(429)] * 3, OSError, "HTTP 429"),
        ([_http(403)], OSError, "HTTP 403"),
        ([_http(404)], script.Gone, "gone"),
        ([b"x" * 11], OSError, "bigger than 10 bytes"),
    ],
)
def test_download_gives_up(tmp_path, monkeypatch, outcomes, error, match):
    urlopen, calls = _urlopen(*outcomes)
    monkeypatch.setattr(script.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(script, "_sleep", lambda s: None)
    monkeypatch.setattr(script, "MAX_BYTES", 10)
    with pytest.raises(error, match=match):
        script.download("pytest-dev/pluggy", "uv.lock", tmp_path)
    assert len(calls) == len(outcomes)


def test_main_names_each_failing_project_and_carries_on(tmp_path, monkeypatch, capsys):
    def download(repo, path, folder):
        broken = {
            "python-poetry/tomlkit": '[[package]]\nname = "a"\nversion = "x.y"\n[metadata]\nlock-version = "2.1"\n',
            "ubernostrum/webcolors": '[metadata]\nlock_version = "4.5.0"\n[[package]]\nname = "a"\nversion = "1"\n'
            'marker = 5\nfiles = [{ file = "a-1-py3-none-any.whl" }]\n',  # wheelgap refuses a marker that isn't text
        }
        if repo == "pytest-dev/pluggy":
            raise urllib.error.URLError("no route")
        if repo == "python-poetry/cleo":
            raise script.Gone("https://x/poetry.lock is gone (HTTP 404)")
        if repo in broken:
            (folder / Path(path).name).write_text(broken[repo], encoding="utf-8")
            return folder / Path(path).name
        return REAL / next(e["file"] for e in script.manifest() if e["repo"] == repo)

    inspect = script.inspect

    def crashing_inspect(path, pinned=None):  # a lockfile odd enough to break the checks themselves
        if pinned is not None and pinned.parent.name == "iniconfig":
            raise RuntimeError("boom")
        return inspect(path, pinned)

    monkeypatch.setattr(script, "download", download)
    monkeypatch.setattr(script, "inspect", crashing_inspect)
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert (
        "FAIL pytest-dev/pluggy uv.lock (default branch): format unknown\n"
        "    couldn't check: can't download it: <urlopen error no route>\n"
    ) in out
    assert (
        "FAIL python-poetry/tomlkit poetry.lock (default branch): lock-version '2.1'; 1 packages\n"
        "    misread: 'a': version 'x.y' doesn't parse\n"
    ) in out
    assert (
        "FAIL pytest-dev/iniconfig uv.lock (default branch): format unknown\n"
        "    misread: checking it failed: RuntimeError: 'boom'\n"
    ) in out
    assert "FAIL python-poetry/cleo poetry.lock (default branch): format unknown\n    upstream changed: " in out
    assert (
        "FAIL ubernostrum/webcolors pdm.lock (default branch): format unknown\n"
        "    misread: can't read it: SourceError: \"pdm.lock: package a has a marker that isn't a string\"\n"
    ) in out
    assert "ok   pypa/pipenv Pipfile.lock (default branch): pipfile-spec '6'; 85 packages\n" in out
    assert out.endswith(
        "5 lockfiles failed:\n"
        "    pytest-dev/pluggy uv.lock (default branch): couldn't check\n"
        "    pytest-dev/iniconfig uv.lock (default branch): misread\n"
        "    python-poetry/tomlkit poetry.lock (default branch): misread\n"
        "    python-poetry/cleo poetry.lock (default branch): upstream changed\n"
        "    ubernostrum/webcolors pdm.lock (default branch): misread\n"
        "An upstream change needs the corpus refreshed: see AGENTS.md (Tests, Real lockfiles).\n"
        "A misread is a wheelgap bug: fix the reader in src/wheelgap/sources.py.\n"
    )


def test_main_pinned_reads_the_copies_offline(monkeypatch, capsys):
    monkeypatch.setattr(script, "download", lambda *a: pytest.fail("--pinned downloaded something"))
    assert script.main(["--pinned", "cleo", "pdfly"]) == 0
    assert capsys.readouterr().out == (
        "ok   python-poetry/cleo poetry.lock (pinned copy): lock-version '2.0'; 53 packages\n"
        "ok   py-pdf/pdfly pylock.toml (pinned copy): lock-version '1.0'; 104 packages\n"
    )


def test_gated_matches_uv_edges_by_source_too(tmp_path):
    # Two torch 2.5.0 entries from different indexes: each is gated by its own edge's marker, not by the other's.
    cpu, pypi = '{ registry = "https://download.pytorch.org/whl/cpu" }', '{ registry = "https://pypi.org/simple" }'
    path = tmp_path / "uv.lock"
    path.write_text(
        UV_HEAD + UV_ROOT + "dependencies = [\n"
        f'    {{ name = "torch", version = "2.5.0", source = {cpu}, marker = "sys_platform == \'linux\'" }},\n'
        '    { name = "lib" },\n]\n\n'
        f'[[package]]\nname = "lib"\nversion = "1.0"\nsource = {pypi}\n'
        f'dependencies = [{{ name = "torch", version = "2.5.0", source = {pypi} }}]\n\n'
        f'[[package]]\nname = "torch"\nversion = "2.5.0"\nsource = {cpu}\n\n'
        f'[[package]]\nname = "torch"\nversion = "2.5.0"\nsource = {pypi}\n',
        encoding="utf-8",
    )
    source = load_file(path)
    on_cpu, on_pypi = (i for i, p in enumerate(source.packages) if p.key == "torch")
    assert source.uv_packages[on_cpu]["source"]["registry"].endswith("/cpu")
    assert script._gated(source, on_cpu)  # only the marked edge from app names this entry
    assert not script._gated(source, on_pypi)  # lib's edge has no marker, and lib has none on the way in
