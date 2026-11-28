"""--explain shows the coverage tracker's notes for a package with a gap (wheelgap.notes), from the fake tracker in
conftest.py: never the network."""

import json
import threading
import time
import urllib.error

import pytest

from wheelgap import cache, notes, origin
from wheelgap.check import Row, Status
from wheelgap.cli import Style, main, render_explain
from wheelgap.sources import Package
from wheelgap.targets import build_targets

PYPI = "https://pypi.org/simple"
NOTE = {
    "platforms": ["linux-x86_64"],
    "kind": "alternative",
    "text": "pkg has no Linux wheels; pkg-binary does: `pip install pkg-binary`.",
    "links": ["https://example.org/pkg#install"],
    "versions": None,
    "updated": "2026-11-11",
}
HEAD = "pkg 1.0: note from the wheelgap tracker for"
CHECKED = "(alternative, checked 2026-11-11)"


def note(**changes) -> dict:
    return {**NOTE, **changes}


def uv_lock(tmp_path, *packages: tuple[str, str, str]) -> str:
    """A uv.lock of sdist-only packages (a gap on every target), each (name, version, registry)."""
    text = 'version = 1\nrequires-python = ">=3.13"\n'
    for name, version, registry in packages or [("pkg", "1.0", PYPI)]:
        text += (
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "{registry}" }}\n'
            f'sdist = {{ url = "https://files.example/{name}-{version}.tar.gz", hash = "sha256:00", size = 1 }}\n'
        )
    (tmp_path / "uv.lock").write_text(text, encoding="utf-8")
    return str(tmp_path)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def explain_json(capsys, *argv) -> dict:
    code, out, err = run(capsys, *argv, "--json", "--explain", "--notes")
    assert code == 1 and not err
    return {p["name"]: p for p in json.loads(out)["packages"]}


def test_note_shown_for_a_matching_target_and_not_another(capsys, tmp_path, tracker):
    tracker.add("pkg", note(), note(platforms=["macos-arm64"], text="Only about macOS."))
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--explain", "--notes")
    assert code == 1 and not err
    # Under the explanation, once per package, with the gap targets it covers, its text as is, and its links.
    assert out.index("pkg 1.0 on win-amd64:") < out.index(HEAD)
    assert f"{HEAD} linux-x86_64 {CHECKED}\n  - {NOTE['text']}\n    https://example.org/pkg#install\n" in out
    assert out.count(NOTE["text"]) == 1
    assert "Only about macOS." not in out
    assert tracker.calls == [f"{notes.TRACKER}/pkg.json"]

    pkg = explain_json(capsys, folder, "-p", "3.13", "-t", "linux-x86_64,win-amd64")["pkg"]
    assert pkg["notes"] == [{**NOTE, "targets": ["linux-x86_64"]}]


@pytest.mark.parametrize("platform", ["ios-arm64", "linux-riscv64"])
def test_unsupported_tracker_platform_never_sends_name_or_hints(capsys, tmp_path, tracker, platform):
    folder = uv_lock(tmp_path)
    code, _, err = run(capsys, folder, "-p", "3.13", "-t", platform, "--explain", "--notes")
    assert code == 1 and not err and tracker.calls == []

    code, _, err = run(capsys, folder, "-p", "3.13", "-t", platform, "--explain")
    assert code == 1 and "add --notes" not in err


def test_supported_gap_still_sends_name_with_unsupported_gap(capsys, tmp_path, tracker):
    code, _, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "ios-arm64,linux-x86_64", "--explain", "--notes")
    assert code == 1 and not err
    assert tracker.calls == [f"{notes.TRACKER}/pkg.json"]


def test_note_for_another_platform_only_shows_nothing(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["macos-arm64"]))
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and HEAD not in out
    assert explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]["notes"] == []


def test_all_platforms_note(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["all"]))
    folder = uv_lock(tmp_path)
    code, out, _ = run(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64,win-amd64", "--explain", "--notes")
    assert f"{HEAD} linux-x86_64, win-amd64 {CHECKED}" in out
    pkg = explain_json(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64,win-amd64")["pkg"]
    assert pkg["notes"][0]["targets"] == [
        "3.13 linux-x86_64",
        "3.13 win-amd64",
        "3.14t linux-x86_64",
        "3.14t win-amd64",
    ]


def test_per_python_note_shown_for_matching_python_and_not_another(capsys, tmp_path, tracker):
    tracker.add("pkg", note(python=["3.13"]))
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and not err
    assert f"{HEAD} linux-x86_64 {CHECKED}\n  - {NOTE['text']}\n" in out
    pkg = explain_json(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64")["pkg"]
    assert pkg["notes"] == [{**NOTE, "python": ["3.13"], "targets": ["3.13 linux-x86_64"]}]

    code2, out2, err2 = run(capsys, folder, "-p", "3.14t", "-t", "linux-x86_64", "--explain", "--notes")
    assert code2 == 1 and not err2 and HEAD not in out2
    pkg2 = explain_json(capsys, folder, "-p", "3.14t", "-t", "linux-x86_64")["pkg"]
    assert pkg2["notes"] == []


def test_free_threaded_note_is_not_shown_for_the_regular_build(capsys, tmp_path, tracker):
    tracker.add("pkg", note(python=["3.14t"]))
    folder = uv_lock(tmp_path)
    pkg = explain_json(capsys, folder, "-p", "3.14,3.14t", "-t", "linux-x86_64")["pkg"]
    assert pkg["notes"] == [{**NOTE, "python": ["3.14t"], "targets": ["3.14t linux-x86_64"]}]


def test_note_without_python_shown_for_all_pythons(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path)
    pkg = explain_json(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64")["pkg"]
    assert pkg["notes"] == [{**NOTE, "targets": ["3.13 linux-x86_64", "3.14t linux-x86_64"]}]


@pytest.mark.parametrize(
    "checked,versions,shown",
    [
        ("1.0", None, True),  # no versions: every release
        ("1.0", ">=1", True),
        ("2.9.1", ">=2.8, <2.10", True),
        ("0.9", ">=1", False),  # the tracker scanned 1.0, but you pinned an older release it doesn't cover
        ("0.9", "<1", True),
        ("1.0", "==1.*", True),
        ("1.0rc1", "<2", False),  # the tracker can't match a pre-release, so neither does wheelgap
        ("1.0.post1", ">=1", False),
        ("1.0+local", ">=1", False),
        ("1.0", "not a specifier", False),
        ("1.0.dev1", ">=0", False),
        ("1!1.0", ">=1", False),
    ],
)
def test_versions(capsys, tmp_path, tracker, checked, versions, shown):
    tracker.add("pkg", note(versions=versions), version="1.0")
    data = explain_json(capsys, uv_lock(tmp_path, ("pkg", checked, PYPI)), "-p", "3.13", "-t", "linux-x86_64")
    assert bool(data["pkg"]["notes"]) is shown


def test_versions_need_a_known_version():
    (parsed,) = notes.parse({"schema": 1, "name": "pkg", "notes": [note(versions="<2")]}, "pkg")
    assert parsed.applies_to("1.0") and not parsed.applies_to(None)


PDM_LOCK = """[metadata]
groups = ["default"]

[[package]]
name = "pkg"
version = "1.0"
files = [{file = "pkg-1.0.tar.gz", hash = "sha256:00"}]
"""


@pytest.mark.parametrize(
    "name,text",
    [
        ("uv.lock", f'version = 1\n[[package]]\nname = "pkg"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'),
        ("pylock.toml", f'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\nindex = "{PYPI}/"\n'),
    ],
)
def test_looked_up_for_a_lock_that_says_pypi(capsys, tmp_path, tracker, name, text):
    sdist = 'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n'
    if name == "uv.lock":
        sdist = 'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hash = "sha256:00", size = 1 }\n'
    (tmp_path / name).write_text(text + sdist, encoding="utf-8")
    tracker.add("pkg", note())
    assert explain_json(capsys, str(tmp_path / name), "-p", "3.13", "-t", "linux-x86_64")["pkg"]["notes"]


def test_looked_up_for_a_name_wheelgap_found_on_pypi(capsys, tmp_path, tracker, fake_pypi):
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    tracker.add("nowheels", note())
    data = explain_json(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64")
    assert data["nowheels"]["notes"] and fake_pypi and tracker.calls


@pytest.mark.parametrize(
    "name,text",
    [
        # A private index: the name may be internal.
        ("uv.lock", None),
        # pdm.lock records no index, and neither does a pylock.toml entry without one.
        ("pdm.lock", PDM_LOCK),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\n'
            'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n',
        ),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\nindex = "https://private.example/simple"\n'
            'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n',
        ),
    ],
)
def test_nothing_fetched_for_a_package_not_known_to_be_on_pypi(capsys, tmp_path, tracker, name, text):
    if text is None:
        uv_lock(tmp_path, ("pkg", "1.0", "https://private.example/simple"))
    else:
        (tmp_path / name).write_text(text, encoding="utf-8")
    tracker.add("pkg", note())
    code, out, err = run(capsys, str(tmp_path / name), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and "pkg 1.0 on linux-x86_64" in out and HEAD not in out
    assert tracker.calls == []


def test_nothing_fetched_for_a_pdm_entry_wheelgap_looked_up(capsys, tmp_path, tracker, fake_pypi):
    """Even when wheelgap had to look a pdm.lock entry up on PyPI, its lock doesn't say it came from there."""
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default"]\n\n[[package]]\nname = "nowheels"\nversion = "0.9"\n', encoding="utf-8"
    )
    tracker.add("nowheels", note())
    code, out, _ = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and fake_pypi and "nowheels 0.9 on linux-x86_64" in out
    assert tracker.calls == []


@pytest.mark.parametrize(
    "flags",
    [
        ["--explain"],  # opt-in: --explain alone makes no requests
        ["--explain", "--notes", "--no-notes"],
        ["--explain", "--notes", "--offline"],
        ["--explain", "--notes", "--index-url", "https://private.example/simple"],
    ],
)
def test_nothing_fetched_with(capsys, tmp_path, tracker, flags):
    tracker.add("pkg", note())
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", *flags)
    assert code == 1 and HEAD not in out
    assert tracker.calls == []


def test_offline_in_config_implies_no_notes(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\nnotes = true\n", encoding="utf-8")
    run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert tracker.calls == []


@pytest.mark.parametrize(
    "respond",
    [
        None,  # a 404: not in the tracker's top 5,000
        lambda name: (_ for _ in ()).throw(urllib.error.HTTPError("u", 500, "Server Error", {}, None)),
        lambda name: (_ for _ in ()).throw(ValueError("not JSON")),
        lambda name: {"schema": 2, "name": name, "notes": [NOTE]},  # a format this version doesn't read
        lambda name: {"schema": 1, "name": "other", "notes": [NOTE]},
        lambda name: ["not", "an", "object"],
    ],
)
def test_failed_lookup_prints_nothing_extra(capsys, tmp_path, tracker, respond):
    folder = uv_lock(tmp_path)
    expected = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--no-notes")
    tracker.respond = respond
    assert run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes") == expected
    assert expected[0] == 1 and not expected[2]
    assert len(tracker.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "red \x1b[31mtext"},
        {"text": "two\nlines"},
        {"text": "x" * 500},
        {"text": " "},
        {"links": ["http://example.org/plain-http"]},
        {"links": ["https://user@example.org/"]},
        {"links": ["https://example.org/a b"]},
        {"links": []},
        {"platforms": ["all", "linux-x86_64"]},
        {"platforms": "linux-x86_64"},
        {"kind": "Alternative!"},
        {"updated": "yesterday"},
        {"versions": 3},
        {"extra": "key"},
        # As strict as the published format and the tracker's checker.
        {"kind": "tip"},
        {"updated": "2026-02-30"},
        {"updated": 20261111},
        {"platforms": ["linux-x86_64", "linux-x86_64"]},
        {"platforms": ["ios-arm64"]},  # a wheelgap target the tracker doesn't scan
        {"platforms": []},
        {"versions": "~=1.0"},
        {"versions": "===1.0"},
        {"versions": "==1!1.0"},
        {"versions": ">=1.0rc1"},
        {"versions": "<1.0.post1"},
        {"versions": "<1.0.dev0"},
        {"versions": "==1.0+local"},
        {"versions": ">=1.*"},  # .* only after == or !=
        {"versions": ""},
        {"links": [f"https://example.org/{i}" for i in range(6)]},
        {"links": ["https://example.org/" + "x" * 281]},  # 301 characters
        {"links": ["https://example.org/a", "https://example.org/a"]},
        {"links": ["https:///no-host"]},
        {"links": ["https://"]},
        # A bad python field: empty, not a list, a bare string, duplicates, or not matching ^3\.\d+t?$.
        {"python": []},
        {"python": "3.13"},
        {"python": 123},
        {"python": ["3.13", "3.13"]},
        {"python": ["py3"]},
        # GitHub Actions workflow commands, legacy (##[ anywhere) and v2 (:: starting a word).
        {"text": "x ##[set-output name=ok;]true"},
        {"text": "##[add-mask]secret"},
        {"text": "see ##[error]this"},
        {"links": ["https://example.org/##[warning]hi"]},
        {"text": "::warning::pwned"},
        {"text": "then ::error file=a::pwn"},
        {"text": "(::stop-commands::x)"},
    ],
)
def test_malformed_note_is_dropped(capsys, tmp_path, tracker, bad):
    tracker.add("pkg", note(**bad), note(text="The good one."))
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert [n["text"] for n in pkg["notes"]] == ["The good one."]


def test_markdown_note_cannot_inject_markdown_or_html(capsys, tmp_path, tracker):
    text = "```` </details><script>alert(1)</script> [click](https://evil.example) **bold** <!-- @mention"
    tracker.add("pkg", note(text=text))
    code, out, _ = run(
        capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--markdown"
    )
    assert code == 1
    lines = out.splitlines()
    fences = [i for i, line in enumerate(lines) if line and set(line) == {"`"}]
    assert len(fences) == 2 and len(lines[fences[0]]) == 5 == len(lines[fences[1]])  # longer than the note's ````
    (at,) = [i for i, line in enumerate(lines) if text in line]
    assert fences[0] < at < fences[1]  # inside the code block, where Markdown and HTML are shown as text
    assert lines[at] == f"  - {text}"  # and as written: nothing to escape there
    assert "<script>" not in "\n".join(lines[: fences[0]] + lines[fences[1] :])


def test_markdown_fence_stays_three_backticks_without_notes(capsys, tmp_path):
    code, out, _ = run(
        capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--markdown"
    )
    assert out.count("\n```\n") == 2


def test_text_output_escapes_nothing(capsys, tmp_path, tracker):
    text = "Use `pkg[binary]` *not* <pkg> & _pkg_ \\ | #1"
    tracker.add("pkg", note(text=text))
    _, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert f"  - {text}\n" in out


def test_one_short_attempt_per_request(capsys, tmp_path, tracker):
    folder = uv_lock(tmp_path, ("pkg", "1.0", PYPI), ("other", "2.0", PYPI))
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert sorted(tracker.calls) == [f"{notes.TRACKER}/other.json", f"{notes.TRACKER}/pkg.json"]
    assert all(attempts == 1 and timeout <= 10 for timeout, attempts in tracker.options)


def _refused(name):
    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


UNREACHABLE = "wheelgap: note: couldn't reach wheelgap.github.io, so some of the tracker's notes may be missing"
TIMED_OUT = "wheelgap: note: wheelgap.github.io didn't answer in time, so some of the tracker's notes may be missing"


@pytest.mark.parametrize(
    "error,message",
    [
        (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), UNREACHABLE),
        (urllib.error.URLError(OSError(8, "nodename nor servname provided")), UNREACHABLE),  # DNS
        (urllib.error.URLError(OSError("CERTIFICATE_VERIFY_FAILED")), UNREACHABLE),  # TLS
        (urllib.error.URLError(TimeoutError("timed out")), TIMED_OUT),
        (TimeoutError("timed out"), TIMED_OUT),
    ],
)
def test_connection_failure_wording(capsys, tmp_path, tracker, error, message):
    def fail(name):
        raise error

    tracker.respond = fail
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and err == message + "\n"


def test_stops_after_the_first_connection_failure(capsys, tmp_path, tracker, monkeypatch):
    monkeypatch.setattr(notes, "WORKERS", 1)
    tracker.respond = _refused
    folder = uv_lock(tmp_path, ("a", "1.0", PYPI), ("b", "1.0", PYPI), ("c", "1.0", PYPI))
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and len(tracker.calls) == 1
    assert err == UNREACHABLE + "\n"
    tracker.calls.clear()
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--json", "-q")
    assert code == 1 and json.loads(out) and err == ""  # -q drops the note


def test_connection_failure_doesnt_wait_for_the_other_lookups(capsys, tmp_path, tracker, monkeypatch):
    """More names than workers: the first failure ends the phase at once, without waiting for requests in flight."""
    monkeypatch.setattr(notes, "WORKERS", 2)
    release = threading.Event()

    def respond(name):
        if name == "a":
            _refused(name)
        release.wait(10)
        return {"schema": 1, "name": name, "notes": [NOTE]}

    tracker.respond = respond
    folder = uv_lock(tmp_path, *[(n, "1.0", PYPI) for n in "abcdef"])
    try:
        start = time.monotonic()
        code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
        assert time.monotonic() - start < 3
        assert code == 1 and err == UNREACHABLE + "\n" and "note from the wheelgap tracker" not in out
        assert f"{notes.TRACKER}/a.json" in tracker.calls and len(tracker.calls) <= 2
    finally:
        release.set()


def test_deadline_for_all_lookups(capsys, tmp_path, tracker, monkeypatch):
    monkeypatch.setattr(notes, "DEADLINE", 0.2)
    release = threading.Event()
    tracker.respond = lambda name: release.wait(10) and {"schema": 1, "name": name, "notes": [NOTE]}
    try:
        start = time.monotonic()
        code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
        assert time.monotonic() - start < 5
        assert code == 1 and HEAD not in out and err == TIMED_OUT + "\n"
    finally:
        release.set()


def test_cached_for_a_day(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path, ("pkg", "1.0", PYPI), ("gone", "1.0", PYPI))  # gone: a 404, cached as no notes
    for _ in range(2):
        _, out, _ = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
        assert HEAD in out
    assert len(tracker.calls) == 2
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--no-cache")
    assert len(tracker.calls) == 4


def test_note_with_python_survives_cache_round_trip(capsys, tmp_path, tracker):
    tracker.add("pkg", note(python=["3.13"]))
    folder = uv_lock(tmp_path)
    pkg1 = explain_json(capsys, folder, "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert len(tracker.calls) == 1
    assert pkg1["notes"] == [{**NOTE, "python": ["3.13"], "targets": ["linux-x86_64"]}]
    pkg2 = explain_json(capsys, folder, "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert len(tracker.calls) == 1
    assert pkg2["notes"] == [{**NOTE, "python": ["3.13"], "targets": ["linux-x86_64"]}]


def test_note_covers_python():
    from wheelgap.targets import Target

    t313 = Target(3, 13, False, "linux-x86_64")
    t314t = Target(3, 14, True, "linux-x86_64")
    t313_mac = Target(3, 13, False, "macos-arm64")

    n_py = notes.Note(("linux-x86_64",), "alternative", "txt", ("https://example.org/",), None, "2026-11-11", ("3.13",))
    assert n_py.covers(t313)
    assert not n_py.covers(t314t)
    assert not n_py.covers(t313_mac)

    n_all = notes.Note(("linux-x86_64",), "alternative", "txt", ("https://example.org/",), None, "2026-11-11", None)
    assert n_all.covers(t313)
    assert n_all.covers(t314t)
    assert not n_all.covers(t313_mac)


HINT = "wheelgap: add --notes to show the tracker's notes for these packages (sends their names to wheelgap.github.io)"


def test_explain_hints_at_notes_without_looking_them_up(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and HEAD not in out and err == HINT + "\n"
    assert tracker.calls == []
    # The same hint for --json and --markdown (on stderr), and --json has no notes to show.
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--json")
    assert err == HINT + "\n" and json.loads(out)["packages"][0]["notes"] == []
    # With colour, it's dim.
    _, _, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--color", "always")
    assert err == f"\033[2m{HINT}\033[0m\n"


@pytest.mark.parametrize(
    "flags,config,lock",
    [
        (["--json", "-q"], "", None),  # -q drops it
        (["--no-notes"], "", None),  # turned off: no need to suggest it
        ([], "notes = false", None),
        (["--offline"], "", None),  # --notes would do nothing here
        (["--index-url", "https://private.example/simple"], "", None),
        ([], "", ("pkg", "1.0", "https://private.example/simple")),  # no package --notes would look up
        ([], "", ("pkg", "1.0", "")),  # no gap: a package with a wheel (see below)
    ],
)
def test_no_hint(capsys, tmp_path, tracker, flags, config, lock):
    if config:
        (tmp_path / "pyproject.toml").write_text(f"[tool.wheelgap]\n{config}\n", encoding="utf-8")
    if lock and not lock[2]:
        (tmp_path / "uv.lock").write_text(
            f'version = 1\n[[package]]\nname = "pkg"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'
            'wheels = [{ url = "https://files.example/pkg-1.0-py3-none-any.whl", hash = "sha256:00", size = 1 }]\n',
            encoding="utf-8",
        )
        folder = str(tmp_path)
    else:
        folder = uv_lock(tmp_path, *([lock] if lock else []))
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", *flags)
    assert code in (0, 1) and HINT not in err
    assert tracker.calls == []


def test_notes_in_config(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nnotes = true\n", encoding="utf-8")
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and HEAD in out and not err
    tracker.calls.clear()
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--no-notes")
    assert code == 1 and HEAD not in out and not err and tracker.calls == []


def test_notes_in_config_must_be_a_boolean(capsys, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nnotes = "yes"\n', encoding="utf-8")
    code, _, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2 and "notes must be a boolean" in err


def test_a_note_may_mention_a_cpp_scope(capsys, tmp_path, tracker):
    tracker.add("pkg", note(text="Needs `std::vector` from a C++17 compiler."))
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert [n["text"] for n in pkg["notes"]] == ["Needs `std::vector` from a C++17 compiler."]


def test_note_text_never_starts_a_line():
    """Even a note that got past tracker.parse with a leading "::" can't be a workflow command: the runner only reads
    "::" as the first text on a line, and the text follows "  - "."""
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    row = Row(Package("pkg", "1.0", files=["pkg-1.0.tar.gz"]), {target: Status.SOURCE})
    rows = [row]
    sneaky = notes.Note(("linux-x86_64",), "alternative", "::warning::x", ("https://example.org/",), None, "2026-11-11")
    lines = render_explain(rows, [target], Style(False), notes={id(row): [(sneaky, [target])]})
    assert "  - ::warning::x" in lines
    assert not any(line.lstrip().startswith("::") for line in lines)


@pytest.mark.parametrize("schema", [True, 1.0, "1", 2])
def test_schema_must_be_the_integer_1(capsys, tmp_path, tracker, schema):
    tracker.files["pkg"] = {"schema": schema, "name": "pkg", "version": "1.0", "notes": [NOTE]}
    assert explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]["notes"] == []


def test_at_most_20_notes_per_package(capsys, tmp_path, tracker):
    tracker.add("pkg", *[note(text=f"Note {i}.") for i in range(25)])
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert [n["text"] for n in pkg["notes"]] == [f"Note {i}." for i in range(20)]


def test_all_means_the_trackers_platforms_only(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["all"]))
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64,linux-ppc64le,pyodide")["pkg"]
    assert set(pkg["rejected"]) >= {"linux-x86_64", "linux-ppc64le"}
    assert pkg["notes"][0]["targets"] == [
        "linux-x86_64",
        "pyodide",
    ]  # not linux-ppc64le, which the tracker doesn't scan


def test_only_gap_targets(capsys, tmp_path, tracker):
    """A package with a wheel on Linux and a gap on Windows: an "all" note covers the Windows gap only."""
    (tmp_path / "uv.lock").write_text(
        f'version = 1\n[[package]]\nname = "pkg"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'
        'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hash = "sha256:00", size = 1 }\n'
        'wheels = [{ url = "https://files.example/pkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:00",'
        " size = 1 }]\n",
        encoding="utf-8",
    )
    tracker.add("pkg", note(platforms=["all"]))
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--explain", "--notes")
    assert f"{HEAD} win-amd64 {CHECKED}" in out
    pkg = explain_json(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-amd64")["pkg"]
    assert pkg["notes"][0]["targets"] == ["win-amd64"]


def test_only_packages_with_a_gap_are_looked_up(capsys, tmp_path, tracker):
    lock = 'version = 1\nrequires-python = ">=3.13"\n'
    for name in ("gap", "ignored", "partly"):
        lock += (
            f'[[package]]\nname = "{name}"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'
            f'sdist = {{ url = "https://files.example/{name}-1.0.tar.gz", hash = "sha256:00", size = 1 }}\n'
        )
    lock += (
        f'[[package]]\nname = "fine"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'
        'wheels = [{ url = "https://files.example/fine-1.0-py3-none-any.whl", hash = "sha256:00", size = 1 }]\n'
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    args = (str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    run(capsys, *args, "--ignore", "ignored", "--ignore", "partly:linux-x86_64")
    assert tracker.calls == [f"{notes.TRACKER}/gap.json"]  # fine has no gap; the others' gaps are ignored


def test_failed_lookup_isnt_cached(capsys, tmp_path, tracker):
    folder = uv_lock(tmp_path)
    tracker.respond = lambda name: (_ for _ in ()).throw(urllib.error.HTTPError("u", 503, "Unavailable", {}, None))
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    tracker.respond = None
    tracker.add("pkg", note())
    _, out, _ = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert HEAD in out and len(tracker.calls) == 2


def test_no_cache_saves_nothing(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path)
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--no-cache")
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert len(tracker.calls) == 2


def test_cached_notes_are_checked_again(capsys, tmp_path, tracker):
    cache.save_notes(notes.TRACKER, "pkg", [note(text="::error::from a tampered cache"), note(text="Cached.")])
    cache.save_lab(notes.TRACKER, "pkg", [])
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert [n["text"] for n in pkg["notes"]] == ["Cached."] and tracker.calls == []


def test_only_normalized_names_are_looked_up(tracker):
    found, built, cut = notes.lookup(["../../evil", "Pkg", "a.b", "-x", "ok-name"], read_cache=False, write_cache=False)
    assert tracker.calls == [f"{notes.TRACKER}/ok-name.json"] and found == {"ok-name": []} and cut is None
    assert built == {"ok-name": []}


def test_notes_needs_explain(capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        main([uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--notes"])
    assert e.value.code == 2 and "--notes only adds to --explain" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags,env,why",
    [
        (["--offline"], {}, "--offline"),
        (["--index-url", "https://private.example/simple"], {}, "--index-url"),
        ([], {"WHEELGAP_INDEX_URL": "https://mirror.example/pypi"}, "WHEELGAP_INDEX_URL"),
    ],
)
def test_notes_off_says_so(capsys, tmp_path, tracker, monkeypatch, flags, env, why):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", *flags)
    assert code == 1 and HEAD not in out and tracker.calls == []
    assert (
        err == f"wheelgap: note: the tracker's notes are off with {why}, so no package names leave for "
        "wheelgap.github.io\n"
    )
    # No hint either, since --notes would do nothing; and -q drops the note.
    assert run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", *flags)[2] == ""
    assert (
        run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--json", "-q", *flags)[2] == ""
    )


# Where a name comes from. --notes sends a name only when it's positively a PyPI package: its lock says so (uv.lock,
# pylock.toml), or wheelgap found it on pypi.org and nothing names another index.


def _requirements(tmp_path, *lines: str, extra: dict[str, str] | None = None) -> str:
    (tmp_path / "requirements.txt").write_text("\n".join([*lines, "nowheels==0.9"]) + "\n", encoding="utf-8")
    for name, text in (extra or {}).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return str(tmp_path / "requirements.txt")


@pytest.mark.parametrize(
    "lines,extra,sent",
    [
        ([], None, True),
        (["--index-url https://private.example/simple"], None, False),
        (["--index-url=https://private.example/simple"], None, False),
        (["-i https://private.example/simple"], None, False),
        (["--extra-index-url https://private.example/simple"], None, False),
        (["-r more.txt"], {"more.txt": "--extra-index-url https://private.example/simple\n"}, False),
        (["-c constraints.txt"], {"constraints.txt": "-i https://private.example/simple\n"}, False),
        (["-r more.txt"], {"more.txt": "purewheel==3.1\n"}, True),
    ],
)
def test_requirements_naming_another_index(capsys, tmp_path, tracker, fake_pypi, lines, extra, sent):
    tracker.add("nowheels", note())
    code, out, _ = run(
        capsys, _requirements(tmp_path, *lines, extra=extra), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes"
    )
    assert code == 1 and fake_pypi  # found on pypi.org either way
    assert bool(tracker.calls) is sent


@pytest.mark.parametrize("variable", origin.INDEX_ENV)
def test_installer_index_in_the_environment(capsys, tmp_path, tracker, fake_pypi, monkeypatch, variable):
    monkeypatch.setenv(variable, "https://private.example/simple")
    tracker.add("nowheels", note())
    tracker.add("pkg", note())
    run(capsys, _requirements(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert tracker.calls == []  # found on pypi.org, but pip or uv may install it from elsewhere
    # A lock that says pypi.org is still positive.
    run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert tracker.calls == [f"{notes.TRACKER}/pkg.json"]


def test_wheelgap_index_url_mirror_sends_nothing(capsys, tmp_path, tracker, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example/pypi")
    tracker.add("pkg", note())
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and err == "" and tracker.calls == []  # no hint, and nothing looked up


def test_wheelgap_index_url_set_to_pypi_is_the_default(capsys, tmp_path, tracker, fake_pypi, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://pypi.org/pypi/")
    tracker.add("nowheels", note())
    run(capsys, _requirements(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert tracker.calls == [f"{notes.TRACKER}/nowheels.json"]


PIPFILE_LOCK = """{
    "_meta": {"sources": [%s]},
    "default": {"nowheels": {"version": "==0.9"}}
}
"""
PYPI_SOURCE = '{"name": "pypi", "url": "https://pypi.org/simple", "verify_ssl": true}'
PRIVATE_SOURCE = '{"name": "corp", "url": "https://private.example/simple", "verify_ssl": true}'


@pytest.mark.parametrize(
    "sources,sent",
    [
        ([PYPI_SOURCE], True),
        ([PRIVATE_SOURCE], False),  # the package has no "index": it comes from the default source, which is private
        ([PYPI_SOURCE, PRIVATE_SOURCE], False),
        ([], False),  # no sources recorded: unknown
    ],
)
def test_pipfile_lock_sources(capsys, tmp_path, tracker, fake_pypi, sources, sent):
    (tmp_path / "Pipfile.lock").write_text(PIPFILE_LOCK % ", ".join(sources), encoding="utf-8")
    tracker.add("nowheels", note())
    code, _, _ = run(capsys, str(tmp_path / "Pipfile.lock"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes")
    assert code == 1 and fake_pypi
    assert bool(tracker.calls) is sent


@pytest.mark.parametrize(
    "tool,sent",
    [
        ("", True),
        ('[[tool.uv.index]]\nname = "corp"\nurl = "https://private.example/simple"\nexplicit = true\n', False),
        # Another package's source: nowheels itself is still looked up on PyPI, but the input isn't PyPI-only.
        ('[tool.uv.sources]\nother = { index = "corp" }\n', False),
        ('[[tool.poetry.source]]\nname = "corp"\nurl = "https://private.example/simple"\n', False),
        ('[[tool.pdm.source]]\nname = "corp"\nurl = "https://private.example/simple"\n', False),
    ],
)
def test_pyproject_naming_another_index(capsys, tmp_path, tracker, fake_pypi, tool, sent):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["nowheels==0.9"]\n\n{tool}', encoding="utf-8"
    )
    tracker.add("nowheels", note())
    code, _, _ = run(
        capsys, str(tmp_path / "pyproject.toml"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes"
    )
    assert code == 1 and fake_pypi
    assert bool(tracker.calls) is sent


@pytest.mark.parametrize(
    "lock",
    [
        # poetry.lock never: even one whose source says pypi.org, or one wheelgap looks up itself (no files).
        '[[package]]\nname = "nowheels"\nversion = "0.9"\n'
        'files = [{file = "nowheels-0.9.tar.gz", hash = "sha256:00"}]\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pypi.org/simple"\nreference = "pypi"\n',
        '[[package]]\nname = "nowheels"\nversion = "0.9"\n',
    ],
)
def test_poetry_lock_never(capsys, tmp_path, tracker, fake_pypi, lock):
    (tmp_path / "poetry.lock").write_text(lock + '\n[metadata]\nlock-version = "2.0"\n', encoding="utf-8")
    tracker.add("nowheels", note())
    code, out, _ = run(
        capsys, str(tmp_path / "poetry.lock"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes"
    )
    assert code == 1 and "nowheels 0.9 on linux-x86_64" in out
    assert tracker.calls == []


def test_online_poetry_legacy_pypi_source_never_sends_name(capsys, tmp_path, tracker, fake_pypi):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "nowheels"\nversion = "0.9"\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pypi.org/simple"\nreference = "pypi"\n'
        '\n[metadata]\nlock-version = "2.0"\n',
        encoding="utf-8",
    )
    code, out, _ = run(
        capsys, str(tmp_path / "poetry.lock"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--online"
    )
    assert code == 1 and "nowheels 0.9 on linux-x86_64" in out
    assert fake_pypi and tracker.calls == []


def test_pylock_without_index_never_even_online(capsys, tmp_path, tracker, fake_pypi):
    """--online finds the entry on pypi.org, but a pylock.toml entry with no index doesn't say where it came from."""
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\n[[packages]]\nname = "nowheels"\nversion = "0.9"\n'
        'sdist = { url = "https://files.example/nowheels-0.9.tar.gz", hashes = { sha256 = "00" } }\n',
        encoding="utf-8",
    )
    tracker.add("nowheels", note())
    args = (str(tmp_path / "pylock.toml"), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--notes", "--online")
    code, out, _ = run(capsys, *args)
    assert code == 1 and fake_pypi and tracker.calls == []


def test_cached_notes_are_capped_too(capsys, tmp_path, tracker):
    cache.save_notes(notes.TRACKER, "pkg", [note(text=f"Note {i}.") for i in range(25)])
    cache.save_lab(notes.TRACKER, "pkg", [])
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert len(pkg["notes"]) == 20 and tracker.calls == []


def test_eligible_only_with_a_gap():
    """A row that isn't ok only because it's unknown (or whose gaps are all ignored) has nothing to explain."""
    from wheelgap.cli import notes_eligible
    from wheelgap.sources import Source

    linux, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    pkg = Package("pkg", "1.0", files=["pkg-1.0.tar.gz"], registry=PYPI)
    source = Source("uv.lock", [pkg], kind="uv")
    source_of = {id(pkg): source}
    assert notes_eligible(Row(pkg, {linux: Status.SOURCE, win: Status.WHEEL}), [linux, win], source_of)
    assert not notes_eligible(Row(pkg, {linux: Status.UNKNOWN, win: Status.WHEEL}), [linux, win], source_of)
    ignored = Row(pkg, {linux: Status.SOURCE, win: Status.WHEEL}, ignored=frozenset({linux}))
    assert not notes_eligible(ignored, [linux, win], source_of)
    assert not notes_eligible(Row(pkg, {linux: Status.SOURCE}), [linux], {})  # not from any input


def test_pixi_lock_never(capsys, fixtures, tracker, fake_pypi):
    tracker.add("fastlib", note(platforms=["all"]))
    code, out, _ = run(capsys, str(fixtures / "pixiproj"), "-p", "3.13", "-t", "linux-aarch64", "--explain", "--notes")
    assert code == 1 and "fastlib 2.0.0 on linux-aarch64" in out
    assert tracker.calls == []


# The build lab (the package file's `builds`): whether a release with no wheel for a target builds from its sdist on
# GitHub's runners, and imports.
BUILD = {
    "target": "3.14 win-arm64",
    "version": "1.0",
    "outcome": "built",
    "seconds": 87,
    "wheel": "cp314-cp314-win_arm64",
    "extensions": 3,
    "import": "ok",
    "gil": None,
    "reason": None,
    "excerpt": "",
    "toolchains": ["cl", "cmake"],
    "runner": "windows-11-arm",
    "python": "3.14.2",
    "date": "2026-11-15",
}
FT = {"target": "3.14t win-arm64", "wheel": "cp314-cp314t-win_arm64", "python": "3.14.2t", "gil": "kept-off"}
FAILED = {"outcome": "failed", "wheel": None, "extensions": None, "import": None, "reason": "needs a Rust toolchain"}
LAB = "pkg 1.0 on win-arm64: build lab (2026-11-15, windows-11-arm, Python 3.14.2): "


def build(**changes) -> dict:
    return {**BUILD, **changes}


def lab_run(capsys, tmp_path, *argv):
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64", *argv, "--explain", "--notes")
    assert code == 1 and not err
    return out


def test_build_lab_result_shown_for_the_checked_release_and_target(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["win-arm64"]), builds=[build()])
    out = lab_run(capsys, tmp_path)
    # After the package's notes, styled like them: once per source cell it speaks for.
    assert f"\n{LAB}builds from source in 87 s and imports\n\n" in out
    assert out.index(HEAD) < out.index(LAB)
    assert tracker.calls == [f"{notes.TRACKER}/pkg.json"]  # the same file as the notes: no other request

    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64")["pkg"]
    assert pkg["build_lab"] == {"win-arm64": {**BUILD, "verdict": "builds from source in 87 s and imports"}}


@pytest.mark.parametrize(
    "changes, verdict",
    [
        ({}, "builds from source in 87 s and imports"),
        ({"seconds": 89}, "builds from source in 89 s and imports"),
        ({"seconds": 90}, "builds from source in 2 min and imports"),
        ({"seconds": 1500}, "builds from source in 25 min and imports"),
        ({"seconds": 150}, "builds from source in 3 min and imports"),  # as the tracker site says it
        ({**FT}, "builds from source in 87 s and imports, GIL stays off"),
        ({**FT, "gil": "re-enabled"}, "builds in 87 s, but importing it turns the GIL back on"),
        (
            {"wheel": "py3-none-any", "extensions": 0},
            (
                "builds in 87 s as pure Python, with no compiled extension module "
                "(if it has an optional one, that was skipped)"
            ),
        ),
        ({"extensions": 0}, "builds in 87 s, but with no compiled extension module"),
        ({**FT, "extensions": 0, "gil": None}, "builds in 87 s, but with no compiled extension module"),
        ({"import": "failed"}, "builds in 87 s, but doesn't import"),
        ({**FT, "import": "failed", "gil": None}, "builds in 87 s, but doesn't import"),
        ({"import": "deps-failed"}, "builds in 87 s, but its dependencies don't install"),
        ({**FAILED}, "fails to build: needs a Rust toolchain"),
        ({**FAILED, "reason": "compile error", "excerpt": "error: C2065"}, "fails to build: compile error"),
        ({**FAILED, "reason": "other"}, "fails to build"),
        (
            {**FAILED, "reason": "unsupported platform"},
            "the package doesn't support this platform (its own build says so)",
        ),
        ({**FAILED, "outcome": "timeout", "reason": None, "seconds": 2400}, "didn't finish building in 40 min"),
    ],
)
def test_build_lab_verdicts(capsys, tmp_path, tracker, changes, verdict):
    result = build(**changes)
    tracker.add("pkg", builds=[result])
    python = "3.14t" if result["target"].startswith("3.14t") else "3.14"
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", python, "-t", "win-arm64", "--explain", "--notes")
    assert code == 1 and not err
    where = f"({result['date']}, {result['runner']}, Python {result['python']})"
    assert f"pkg 1.0 on win-arm64: build lab {where}: {verdict}\n" in out
    assert out.count("build lab") == 1
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", python, "-t", "win-arm64")["pkg"]
    assert pkg["build_lab"] == {"win-arm64": {**result, "verdict": verdict}}


def test_build_lab_never_calls_a_wheel_without_extensions_a_working_build(capsys, tmp_path, tracker):
    for wheel in ("py3-none-any", "cp314-cp314-win_arm64"):
        tracker.add("pkg", builds=[build(wheel=wheel, extensions=0)])
        out = lab_run(capsys, tmp_path, "--no-cache")
        assert LAB in out and "builds from source" not in out and "and imports" not in out


@pytest.mark.parametrize("version", ["1.1", "0.9", "1.0.0", "1.0.post1", "v1.0"])
def test_build_lab_result_for_another_release_shows_nothing(capsys, tmp_path, tracker, version):
    tracker.add("pkg", note(platforms=["win-arm64"]), builds=[build(version=version)])
    out = lab_run(capsys, tmp_path)
    assert HEAD in out and "build lab" not in out  # it says nothing about the checked release
    assert explain_json(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64")["pkg"]["build_lab"] == {}


@pytest.mark.parametrize(
    "python, platform, target",
    [
        ("3.14", "win-arm64", "3.14t win-arm64"),  # the free-threaded build says nothing about the regular one
        ("3.14t", "win-arm64", "3.14 win-arm64"),
        ("3.14", "win-amd64", "3.14 win-arm64"),
        ("3.13", "win-arm64", "3.14 win-arm64"),
        ("3.14", "musl-aarch64", "3.14 linux-aarch64"),
    ],
)
def test_build_lab_result_for_another_target_shows_nothing(capsys, tmp_path, tracker, python, platform, target):
    py = target.split()[0]
    tracker.add("pkg", builds=[build(target=target, python=f"{py.rstrip('t')}.2{'t' * py.endswith('t')}", gil=None)])
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", python, "-t", platform, "--explain", "--notes")
    assert code == 1 and not err and "build lab" not in out
    assert explain_json(capsys, folder, "-p", python, "-t", platform)["pkg"]["build_lab"] == {}


def test_build_lab_results_by_target_with_several_pythons(capsys, tmp_path, tracker):
    ft = build(**FT, seconds=200)
    tracker.add("pkg", builds=[build(), ft, build(target="3.14t linux-x86_64", python="3.14.0t", gil="re-enabled")])
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.14,3.14t", "-t", "win-arm64", "--explain", "--notes")
    assert code == 1 and not err
    assert (
        "pkg 1.0 on 3.14 win-arm64: build lab (2026-11-15, windows-11-arm, Python 3.14.2): "
        "builds from source in 87 s and imports\n"
        "pkg 1.0 on 3.14t win-arm64: build lab (2026-11-15, windows-11-arm, Python 3.14.2t): "
        "builds from source in 3 min and imports, GIL stays off\n\n"
    ) in out
    assert "linux-x86_64" not in out  # not a checked target
    pkg = explain_json(capsys, folder, "-p", "3.14,3.14t", "-t", "win-arm64")["pkg"]
    assert list(pkg["build_lab"]) == ["3.14 win-arm64", "3.14t win-arm64"]
    assert pkg["build_lab"]["3.14t win-arm64"]["verdict"] == "builds from source in 3 min and imports, GIL stays off"


def test_build_lab_only_for_source_cells(capsys, tmp_path, tracker):
    tracker.add("pkg", builds=[build()])
    folder = uv_lock(tmp_path)
    args = ("-p", "3.14", "-t", "win-arm64,linux-x86_64", "--ignore", "pkg:win-arm64")
    code, out, err = run(capsys, folder, *args, "--explain", "--notes")
    assert code == 1 and not err and "build lab" not in out  # win-arm64 is ignored, so not explained
    assert explain_json(capsys, folder, *args)["pkg"]["build_lab"] == {}


def test_build_lab_needs_a_known_version():
    from wheelgap.targets import Target

    (result,) = notes.parse_builds({"schema": 1, "name": "pkg", "builds": [build()]}, "pkg")
    target = Target(3, 14, False, "win-arm64")
    assert result.covers(target, "1.0") and not result.covers(target, None) and not result.covers(target, "1.0.0")


@pytest.mark.parametrize(
    "bad",
    [
        {**BUILD, "extra": 1},
        {k: v for k, v in BUILD.items() if k != "date"},
        {k: v for k, v in BUILD.items() if k != "import"} | {"imported": "ok"},
        "not an object",
        None,
        build(target="3.14 win_arm64"),
        build(target="3.14 ios-arm64"),
        build(target="3.14 pyodide"),
        build(target="3.14  win-arm64"),
        build(target="win-arm64"),
        build(version=""),
        build(version=1.0),
        build(version="1.0 "),
        build(outcome="built?"),
        build(outcome=None),
        build(wheel="cp314-cp314-linux_x86_64"),  # a wheel for another platform than the target's
        build(wheel="cp314-cp314-win_amd64"),
        build(runner="ubuntu-24.04"),  # a runner that can't build for win-arm64
        build(runner="windows-2025"),  # an x86-64 Windows runner
        build(seconds=86400),  # longer than the lab ever runs a build
        build(seconds=True),
        build(seconds=-1),
        build(seconds=87.0),
        build(seconds="87"),
        build(seconds=10**9),
        build(wheel=None),
        build(wheel="cp314-cp314-win_arm64\n"),
        build(wheel="py3-none-any", extensions=2),  # a pure Python wheel can't hold an extension module
        build(extensions=None),
        build(extensions=-1),
        build(extensions=False),
        build(**{"import": None}),
        build(**{"import": "maybe"}),
        build(gil="kept-off"),  # not a free-threaded target
        build(**FT, extensions=0),  # no extension module: the GIL wasn't checked
        build(**{**FT, "import": "failed"}),  # didn't import: the GIL wasn't checked
        build(**{**FT, "gil": None}),  # a free-threaded build that imported has its GIL checked
        build(**{**FT, "gil": "off"}),
        build(**FAILED, gil="kept-off"),
        build(reason="other"),  # built: no reason
        build(**{**FAILED, "reason": None}),
        build(**{**FAILED, "reason": "needs coffee"}),
        build(**{**FAILED, "wheel": "cp314-cp314-win_arm64"}),
        build(**{**FAILED, "extensions": 0}),
        build(**{**FAILED, "import": "failed"}),
        build(outcome="timeout", reason="other", wheel=None, extensions=None, **{"import": None}),
        build(excerpt="x" * 401),
        build(excerpt="caf\u00e9"),
        build(excerpt="line\nbreak"),
        build(excerpt="\x1b[31mred"),
        build(excerpt="##[error]injected"),  # the legacy workflow command form; "::" is fine (see below)
        build(excerpt="see ##[error]injected"),
        build(excerpt=None),
        build(toolchains=["gcc", "gcc"]),
        build(toolchains=["clang"]),
        build(toolchains="gcc"),
        build(runner="windows 11"),
        build(runner="::error::x"),
        build(runner=""),
        build(runner="r" * 65),
        build(python="3.14"),
        build(python="3.14.2t"),  # a free-threaded Python for the regular target
        build(python="3.13.9"),  # another Python than the target's
        build(python="3.14.2\u200b"),
        build(date="2026-02-30"),
        build(date="2026-11-15T00:00:00"),
        build(date=None),
    ],
)
def test_malformed_build_lab_result_is_dropped_and_notes_still_show(capsys, tmp_path, tracker, bad):
    other = build(**FT)  # a good result for another target, which still shows
    tracker.add("pkg", note(platforms=["win-arm64"]), builds=[bad, other])
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.14,3.14t", "-t", "win-arm64", "--explain", "--notes")
    assert code == 1 and not err
    assert HEAD in out and "pkg 1.0 on 3.14t win-arm64: build lab" in out
    assert "pkg 1.0 on 3.14 win-arm64: build lab" not in out  # dropped whole, never shown in part
    pkg = explain_json(capsys, folder, "-p", "3.14,3.14t", "-t", "win-arm64")["pkg"]
    assert list(pkg["build_lab"]) == ["3.14t win-arm64"] and len(pkg["notes"]) == 1


def test_two_build_lab_results_for_one_target_are_both_dropped(capsys, tmp_path, tracker):
    tracker.add("pkg", builds=[build(), build(seconds=5), build(**FT)])
    folder = uv_lock(tmp_path)
    pkg = explain_json(capsys, folder, "-p", "3.14,3.14t", "-t", "win-arm64")["pkg"]
    assert list(pkg["build_lab"]) == ["3.14t win-arm64"]  # which of the two would be a guess


@pytest.mark.parametrize("builds", [{"3.14 win-arm64": BUILD}, "built", 1])
def test_builds_that_arent_a_list_leave_the_notes(capsys, tmp_path, tracker, builds):
    tracker.add("pkg", note(platforms=["win-arm64"]), builds=builds)
    out = lab_run(capsys, tmp_path)
    assert HEAD in out and "build lab" not in out
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64")["pkg"]
    assert pkg["build_lab"] == {} and len(pkg["notes"]) == 1


@pytest.mark.parametrize("bad_notes", [{"note": NOTE}, "notes", [note(text="::error::x")]])
def test_malformed_notes_leave_the_build_lab_results(capsys, tmp_path, tracker, bad_notes):
    tracker.add("pkg", builds=[build()])
    tracker.files["pkg"]["notes"] = bad_notes
    out = lab_run(capsys, tmp_path)
    assert f"{LAB}builds from source in 87 s and imports" in out and HEAD not in out


def test_package_file_without_builds_has_no_results(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["win-arm64"]))  # published before the build lab
    assert "builds" not in tracker.files["pkg"]
    out = lab_run(capsys, tmp_path)
    assert HEAD in out and "build lab" not in out
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64")["pkg"]
    assert pkg["build_lab"] == {} and len(pkg["notes"]) == 1
    assert len(tracker.calls) == 1  # cached as no results, like a 404


def test_build_lab_needs_notes(capsys, tmp_path, tracker):
    tracker.add("pkg", builds=[build()])
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64", "--explain")
    assert code == 1 and "build lab" not in out and tracker.calls == []
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64", "--explain", "--json")
    assert json.loads(out)["packages"][0]["build_lab"] == {}


def test_build_lab_results_are_cached_and_checked_again(capsys, tmp_path, tracker):
    tracker.add("pkg", builds=[build()])
    for _ in range(2):
        assert LAB in lab_run(capsys, tmp_path)
    assert len(tracker.calls) == 1
    cache.save_lab(notes.TRACKER, "pkg", [build(runner="::error::tampered"), build(**FT)])
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.14,3.14t", "-t", "win-arm64")["pkg"]
    assert list(pkg["build_lab"]) == ["3.14t win-arm64"] and len(tracker.calls) == 1


def test_notes_cached_before_the_build_lab_are_looked_up_again(capsys, tmp_path, tracker):
    cache.save_notes(notes.TRACKER, "pkg", [note(platforms=["win-arm64"])])  # what 0.10.0 saved
    tracker.add("pkg", note(platforms=["win-arm64"]), builds=[build()])
    out = lab_run(capsys, tmp_path)
    assert HEAD in out and LAB in out and len(tracker.calls) == 1


def test_markdown_shows_build_lab_results(capsys, tmp_path, tracker):
    tracker.add("pkg", builds=[build()])
    code, out, err = run(
        capsys, uv_lock(tmp_path), "-p", "3.14", "-t", "win-arm64", "--explain", "--notes", "--markdown"
    )
    assert code == 1 and not err
    assert f"\n{LAB}builds from source in 87 s and imports\n" in out.split("```")[1]


@pytest.mark.parametrize(
    "excerpt",
    [
        "error: no member named 'x' in 'std::vector<int>::iterator'",
        "error: '::size_t' has not been declared",
        "error[E0599]: no function `<T as Foo>::bar`",
    ],
)
def test_a_compile_error_with_cpp_or_rust_paths_is_kept(excerpt):
    # Real compiler errors are full of "::"; dropping them would hide exactly the failures the lab exists to report.
    raw = build(**FAILED, excerpt=excerpt)
    (result,) = notes.parse_builds({"schema": 1, "name": "pkg", "builds": [raw]}, "pkg")
    assert result.excerpt == excerpt
    assert result.reason == "needs a Rust toolchain"
