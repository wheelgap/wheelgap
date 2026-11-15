import json
import urllib.error
from copy import deepcopy

import pytest
from conftest import FAKE_INDEX
from packaging.version import InvalidVersion
from test_cli import _assert_matches_output_schema, run

from wheelgap import pypi, suggest
from wheelgap.cli import main
from wheelgap.suggest import nearest_with_wheel
from wheelgap.targets import build_targets


def wheel(version, tag="py3-none-any", **metadata):
    return {"filename": f"demo-{version}-{tag}.whl", **metadata}


@pytest.mark.parametrize(
    "releases,pinned,expected",
    [
        ({"1.1": [wheel("1.1")]}, "1.0", "1.1"),
        ({"1.1": [{"filename": "demo-1.1.tar.gz"}], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": [{"filename": "demo-1.1.tar.gz"}]}, "1.0", None),
        ({}, "1.0", None),
        ({"1.1": []}, "1.0", None),
        ({"1.1": [wheel("1.1", yanked=True)]}, "1.0", None),
        ({"1.1": [wheel("1.1", yanked=True), {"filename": "demo-1.1.tar.gz"}]}, "1.0", None),
        ({"1.1": [wheel("1.1", yanked=True)], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": [wheel("1.1", "cp313-cp313-win_amd64", yanked=True), wheel("1.1")]}, "1.0", "1.1"),
        ({"1.1rc1": [wheel("1.1rc1")]}, "1.0", None),
        ({"1.1.dev1": [wheel("1.1.dev1")], "1.1": [wheel("1.1")]}, "1.0", "1.1"),
        ({"1.1rc2": [wheel("1.1rc2")], "1.1": [wheel("1.1")]}, "1.1rc1", "1.1rc2"),
        ({"1.1": [wheel("1.1")]}, "1.1rc1", "1.1"),
        ({"1.1.dev2": [wheel("1.1.dev2")]}, "1.1.dev1", "1.1.dev2"),
        ({"1.0": [wheel("1.0")], "0.9": [wheel("0.9")]}, "1.0", None),
        ({"1.0.0": [wheel("1.0.0")]}, "1.0", None),
        ({"1.10": [wheel("1.10")], "1.9": [wheel("1.9")]}, "1.8", "1.9"),
        ({"not-a-version": [wheel("2.0")], "2.0": [wheel("2.0")]}, "1.0", "2.0"),
        ({"1.1": [{"filename": "broken.whl"}], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": None, "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": [{"url": "x"}], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": ["demo-1.1-py3-none-any.whl"], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.1": [{"filename": None}], "1.2": [wheel("1.2")]}, "1.0", "1.2"),
        ({"1.0.post1": [wheel("1.0.post1")], "1.1": [wheel("1.1")]}, "1.0", "1.0.post1"),
        ({"v1.1": [wheel("1.1")]}, "1.0", "v1.1"),
    ],
)
def test_nearest_with_wheel(releases, pinned, expected):
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    original = deepcopy(releases)
    assert nearest_with_wheel(releases, pinned, target) == expected
    assert releases == original


@pytest.mark.parametrize(
    "python,platform,tag,expected",
    [
        ("3.13", "linux-x86_64", "cp313-cp313-win_amd64", None),
        ("3.13", "win-amd64", "cp313-cp313-win_amd64", "1.1"),
        ("3.13", "musl-aarch64", "cp313-cp313-manylinux_2_17_aarch64", None),
        ("3.13", "musl-aarch64", "cp313-cp313-musllinux_1_2_aarch64", "1.1"),
        ("3.13", "linux-x86_64", "cp312-cp312-manylinux_2_17_x86_64", None),
        ("3.13", "linux-x86_64", "cp312-abi3-manylinux_2_17_x86_64", "1.1"),
        ("3.13t", "linux-x86_64", "cp312-abi3-manylinux_2_17_x86_64", None),
        ("3.13t", "linux-x86_64", "cp313-cp313t-manylinux_2_17_x86_64", "1.1"),
    ],
)
def test_nearest_with_wheel_uses_target_compatibility(python, platform, tag, expected):
    (target,) = build_targets([python], [platform])
    assert nearest_with_wheel({"1.1": [wheel("1.1", tag)]}, "1.0", target) == expected


def test_nearest_with_wheel_invalid_pin():
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    with pytest.raises(InvalidVersion):
        nearest_with_wheel({"1.1": [wheel("1.1")]}, "not-a-version", target)


@pytest.mark.parametrize("python", ["3.13", "3.13t"])
@pytest.mark.parametrize(
    "requires_python,expected",
    [(">=3.14", "1.2"), ("<3.13", "1.2"), (">=3.13.1", "1.1"), (None, "1.1"), ("not a specifier", "1.1")],
)
def test_nearest_with_wheel_requires_python(python, requires_python, expected):
    (target,) = build_targets([python], ["linux-x86_64"])
    releases = {
        "1.1": [wheel("1.1", requires_python=requires_python)],
        "1.2": [wheel("1.2", requires_python=">=3.13")],
    }
    assert nearest_with_wheel(releases, "1.0", target) == expected


@pytest.mark.parametrize("compatible_file", [False, True])
def test_nearest_with_wheel_requires_python_per_file(compatible_file):
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    files = [wheel("1.1", requires_python=">=3.14"), {"filename": "demo-1.1.tar.gz", "requires_python": ">=3.8"}]
    if compatible_file:
        files.append(wheel("1.1", "cp313-cp313-manylinux_2_17_x86_64", requires_python=">=3.13"))
    assert nearest_with_wheel({"1.1": files}, "1.0", target) == ("1.1" if compatible_file else None)


def test_nearest_with_wheel_never_fetches(fake_pypi):
    (target,) = build_targets(["3.13"], ["linux-x86_64"])
    assert nearest_with_wheel({"1.1": [wheel("1.1")]}, "1.0", target) == "1.1"
    assert fake_pypi == []


# --suggest: the releases of laggard in FAKE_INDEX (tests/conftest.py).
LAGGARD = FAKE_INDEX["laggard"]
PAGE = "https://pypi.org/pypi/laggard/json"
FLAGS = ["-p", "3.13", "--explain", "--suggest", "--color", "never"]


def suggest_json(capsys, *args):
    code, out, err = run(capsys, *args, "--json")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    return code, {(p["name"], p["version"]): p.get("suggest", "absent") for p in data["packages"]}, err


@pytest.mark.parametrize(
    "platforms,expected",
    [
        (["win-amd64"], {"win-amd64": "1.1"}),
        # 1.1 is nearer for Windows, but 1.2 has a wheel for both.
        (["win-amd64", "musl-aarch64"], {"win-amd64": "1.2", "musl-aarch64": "1.2"}),
        # No release has both: each target's nearest.
        (["win-amd64", "macos-arm64"], {"win-amd64": "1.1", "macos-arm64": "1.3"}),
        # Only the yanked 1.4 and the pre-release 2.0rc1 have one, and neither counts.
        (["linux-riscv64"], {}),
        (["win-amd64", "linux-riscv64"], {"win-amd64": "1.1"}),
        # linux-riscv64 has none, so it's left out of the search: 1.2 has a wheel for the other two.
        (["win-amd64", "musl-aarch64", "linux-riscv64"], {"win-amd64": "1.2", "musl-aarch64": "1.2"}),
        # No release has a wheel for all three that have one: each target's nearest.
        (
            ["win-amd64", "macos-arm64", "musl-aarch64", "linux-riscv64"],
            {"win-amd64": "1.1", "macos-arm64": "1.3", "musl-aarch64": "1.2"},
        ),
    ],
)
def test_suggest_picks_one_release_for_every_target_else_the_nearest_each(platforms, expected):
    targets = build_targets(["3.13"], platforms)
    releases = {v: [{"filename": f} if isinstance(f, str) else f for f in fs] for v, fs in LAGGARD.items()}
    found = suggest.suggest(releases, "1.0", targets).found
    assert {t.platform: v for t, v in found.items()} == expected
    assert list(found) == [t for t in targets if t.platform in expected]  # in the targets' order


def test_suggest_keeps_pre_releases_for_a_pre_release_pin():
    targets = build_targets(["3.13"], ["linux-riscv64"])
    assert suggest.suggest({"2.0rc1": [wheel("2.0rc1")], "2.0rc2": [wheel("2.0rc2")]}, "2.0rc1", targets).found == {
        targets[0]: "2.0rc2"
    }


def test_a_release_must_keep_the_wheels_the_row_has():
    linux, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    linux_wheel, win_wheel = wheel("1.0", "cp313-cp313-manylinux_2_17_x86_64"), wheel("1.0", "cp313-cp313-win_amd64")
    releases = {"1.1": [win_wheel], "1.2": [linux_wheel, win_wheel]}
    assert suggest.suggest(releases, "1.0", [win], keep=[linux]) == suggest.Suggestion({win: "1.2"})
    assert suggest.suggest(releases, "1.0", [win]) == suggest.Suggestion({win: "1.1"})  # nothing to keep
    # 1.1 has a Windows wheel, but drops Linux's; 1.2 drops Linux's Python.
    releases = {"1.1": [win_wheel], "1.2": [{**linux_wheel, "requires_python": ">=3.14"}, win_wheel]}
    assert suggest.suggest(releases, "1.0", [win], keep=[linux]) == suggest.Suggestion({}, (win,))
    assert suggest.suggest({}, "1.0", [win], keep=[linux]) == suggest.Suggestion({})  # none has a wheel at all


def test_a_release_that_drops_a_wheel_is_never_named(capsys, fake_pypi):
    # wheeldrop 1.0 has a Linux wheel; 1.1 has a Windows one only; 1.2 has both.
    _, out, _ = run(capsys, "wheeldrop==1.0", "-t", "linux-x86_64,win-amd64", *FLAGS)
    assert "wheeldrop 1.0 -> 1.2 has wheels for win-amd64\n\n" in out and "-> 1.1" not in out
    _, found, _ = suggest_json(
        capsys, "wheeldrop==1.0", "-t", "linux-x86_64,win-amd64", "-p", "3.13", "--explain", "--suggest"
    )
    assert found == {("wheeldrop", "1.0"): {"win-amd64": "1.2"}}
    # wheellost 1.1 has a Windows wheel but none for Linux, and there's nothing newer.
    _, out, _ = run(capsys, "wheellost==1.0", "-t", "linux-x86_64,win-amd64,linux-riscv64", *FLAGS)
    assert (
        "wheellost 1.0: no newer release has a wheel for linux-riscv64\n"
        "wheellost 1.0: no newer release has a wheel for win-amd64 without losing one elsewhere\n\n"
    ) in out
    _, found, _ = suggest_json(
        capsys, "wheellost==1.0", "-t", "linux-x86_64,win-amd64", "-p", "3.13", "--explain", "--suggest"
    )
    assert found == {("wheellost", "1.0"): {}}


def test_text_after_the_explain_blocks(capsys, fake_pypi):
    args = ["laggard==1.0", "purewheel==3.1", "-t", "win-amd64,musl-x86_64,musl-aarch64,macos-arm64,linux-riscv64"]
    code_plain, out_plain, err_plain = run(capsys, *args, *FLAGS[:-3], "--color", "never")
    code, out, err = run(capsys, *args, *FLAGS)
    block = (
        "laggard 1.0 -> 1.1 has wheels for win-amd64\n"
        "laggard 1.0 -> 1.2 has wheels for musl-x86_64, musl-aarch64\n"
        "laggard 1.0 -> 1.3 has wheels for macos-arm64\n"
        "laggard 1.0: no newer release has a wheel for linux-riscv64\n\n"
    )
    assert "  no wheels published for this version\n\n" + block in out  # under laggard's last --explain block
    assert code == code_plain == 1  # no status, summary line or exit code changes
    assert out.replace(block, "") == out_plain and err == err_plain
    assert fake_pypi.count(PAGE) == 1 and not any("purewheel/json" in url for url in fake_pypi)  # gaps only


def test_one_line_for_one_release_and_python_labels(capsys, fake_pypi):
    _, out, _ = run(capsys, "laggard==1.0", "-t", "win-amd64,musl-aarch64", *FLAGS)
    assert "laggard 1.0 -> 1.2 has wheels for win-amd64, musl-aarch64\n\n" in out
    _, out, _ = run(capsys, "laggard==1.0", "-t", "win-amd64", "-p", "3.14", *FLAGS)
    # 1.3's pure wheel is for 3.14 on: no release has a wheel for both Pythons.
    assert "laggard 1.0 -> 1.1 has wheels for 3.13 win-amd64\nlaggard 1.0 -> 1.3 has wheels for 3.14 win-amd64\n" in out


def test_a_target_with_none_is_left_out_of_the_shared_release(capsys, fake_pypi):
    _, out, _ = run(capsys, "laggard==1.0", "-t", "win-amd64,musl-x86_64,musl-aarch64,linux-riscv64", *FLAGS)
    assert (
        "  no wheels published for this version\n\n"
        "laggard 1.0 -> 1.2 has wheels for win-amd64, musl-x86_64, musl-aarch64\n"
        "laggard 1.0: no newer release has a wheel for linux-riscv64\n\n"
    ) in out
    assert "-> 1.1" not in out  # Windows's nearest, but 1.2 covers every target that has a wheel at all


def test_json_shape(capsys, fake_pypi):
    args = [
        "laggard==1.0",
        "laggard==1.1",
        "purewheel==3.1",
        "-t",
        "win-amd64,musl-aarch64,linux-riscv64",
        "-p",
        "3.13",
    ]
    code, found, _ = suggest_json(capsys, *args, "--explain", "--suggest")
    assert found == {
        ("laggard", "1.0"): {"win-amd64": "1.2", "musl-aarch64": "1.2"},  # linux-riscv64 has none: left out
        ("laggard", "1.1"): {"musl-aarch64": "1.2"},  # win-amd64 isn't a gap
        ("purewheel", "3.1"): "absent",  # no gap
    }
    assert fake_pypi.count(PAGE) == 1  # one request per name, for both rows
    code_plain, plain, _ = suggest_json(capsys, *args, "--explain")
    assert code == code_plain == 1 and set(plain.values()) == {"absent"}
    _, found, _ = suggest_json(capsys, "laggard==1.0", "-t", "linux-riscv64", "-p", "3.13", "--explain", "--suggest")
    assert found == {("laggard", "1.0"): {}}  # looked up, and nothing newer has a wheel


def test_exit_code_is_unchanged(capsys, fake_pypi):
    for extra in ([], ["--exit-zero"], ["--fail-on", "missing"], ["--advisory", "win-amd64"]):
        args = ["laggard==1.0", "-t", "win-amd64", "-p", "3.13", "--explain", *extra]
        assert run(capsys, *args)[0] == run(capsys, *args, "--suggest")[0]


@pytest.mark.parametrize(
    "failure,why",
    [
        (urllib.error.HTTPError(PAGE, 503, "Unavailable", {}, None), "HTTP 503"),  # type: ignore[arg-type]
        (TimeoutError("timed out"), "no usable answer"),
        (ValueError("bad JSON"), "no usable answer"),
        (pypi.ResponseTooLarge("big"), "answer over 32 MB"),
        (urllib.error.URLError("unreachable"), suggest.DROPPED),
        ({"info": {}}, "no usable answer"),  # no release list
    ],
)
def test_a_failed_lookup_is_a_note_and_no_key(capsys, fake_pypi, monkeypatch, failure, why):
    served = pypi.fetch_json

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        if url == PAGE:
            fake_pypi.append(url)
            assert attempts == 1 and max_bytes == pypi.RELEASES_MAX_BYTES and deadline is not None
            if isinstance(failure, Exception):
                raise failure
            return failure
        return served(url, timeout, attempts, max_bytes, deadline)

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    args = ["laggard==1.0", "purewheel==3.1", "-t", "win-amd64", "-p", "3.13", "--explain"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, err = run(capsys, *args, "--suggest")
    assert (code, out) == (code_plain, out_plain) and PAGE in fake_pypi
    assert f"wheelgap: note: --suggest couldn't look up 1 package: laggard ({why}); no suggestion" in err
    code, found, err = suggest_json(capsys, *args, "--suggest")
    assert code == code_plain and found[("laggard", "1.0")] == "absent" and "couldn't look up" in err
    _, _, err = run(capsys, *args, "--suggest", "--json", "-q")
    assert "--suggest" not in err  # quiet drops the note, as it drops --conda-forge's


def test_a_connection_failure_stops_the_rest(monkeypatch):
    asked = []

    def fetch_releases(key, timeout, deadline):
        asked.append(key)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(pypi, "fetch_releases", fetch_releases)
    monkeypatch.setattr(suggest, "WORKERS", 1)
    targets = build_targets(["3.13"], ["win-amd64"])
    found, failed = suggest.look_up_all({"a": [("1.0", targets, [])], "b": [("1.0", targets, [])]})
    assert asked == ["a"] and found == {} and failed == {"a": suggest.DROPPED, "b": suggest.DROPPED}


def test_past_the_deadline_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(pypi, "fetch_releases", lambda *a, **k: pytest.fail("asked after the deadline"))
    targets = build_targets(["3.13"], ["win-amd64"])
    assert suggest.look_up_all({"a": [("1.0", targets, [])]}, deadline=0) == ({}, {"a": suggest.DROPPED})


def test_an_invalid_pin_is_a_failed_lookup(monkeypatch):
    monkeypatch.setattr(pypi, "fetch_releases", lambda *a, **k: {"1.1": [wheel("1.1")]})
    targets = build_targets(["3.13"], ["win-amd64"])
    found, failed = suggest.look_up_all({"a": [("not-a-version", targets, [])]})
    assert found == {} and failed == {"a": "a checked version isn't a valid version"}


def uv_lock(registry):
    return (
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "laggard" }]\n\n'
        f'[[package]]\nname = "laggard"\nversion = "1.0"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/laggard-1.0.tar.gz" }\n'
    )


@pytest.mark.parametrize(
    "name,text,sent",
    [
        ("uv.lock", uv_lock("https://pypi.org/simple"), True),
        ("uv.lock", uv_lock("https://pkgs.example.com/simple"), False),
        ("requirements.txt", "laggard==1.0\n", True),
        ("requirements.txt", "--index-url https://pkgs.example.com/simple\nlaggard==1.0\n", False),
    ],
)
def test_only_names_known_to_be_on_pypi_are_looked_up(capsys, fake_pypi, tmp_path, name, text, sent):
    (tmp_path / name).write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / name), "-t", "win-amd64", *FLAGS)
    assert code == 1 and "laggard 1.0 on win-amd64" in out  # a gap either way
    assert fake_pypi.count(PAGE) == sent
    assert ("laggard 1.0 -> 1.1 has wheels for win-amd64" in out) is sent
    assert ("--suggest didn't look up 1 package whose index isn't known to be PyPI (laggard)" in err) is not sent


def test_no_binary_packages_get_no_suggestion(capsys, fake_pypi, tmp_path):
    # pip would build every release of laggard from source, wheels or not.
    (tmp_path / "requirements.txt").write_text("--no-binary laggard\nlaggard==1.0\nnowheels==0.9\n", encoding="utf-8")
    code, found, _ = suggest_json(capsys, str(tmp_path / "requirements.txt"), "-t", "win-amd64", *FLAGS)
    assert code == 1 and found[("laggard", "1.0")] == "absent" and found[("nowheels", "0.9")] == {}
    assert PAGE not in fake_pypi and "https://pypi.org/pypi/nowheels/json" in fake_pypi


def test_other_index_runs_look_nothing_up(capsys, fake_pypi, fake_simple, monkeypatch):
    args = ["laggard==1.0", "-t", "win-amd64", *FLAGS]
    _, out, err = run(capsys, *args, "--index-url", "https://pkgs.example.com/simple")
    assert fake_simple.calls and PAGE not in fake_pypi and "has wheels for" not in out
    assert "wheelgap: note: --suggest looks nothing up when packages come from another index (--index-url)" in err
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    _, out, err = run(capsys, *args)
    assert fake_pypi and not any(url.endswith("/laggard/json") for url in fake_pypi) and "has wheels for" not in out
    assert "another index (WHEELGAP_INDEX_URL)" in err


def test_needs_explain_and_no_markdown(capsys):
    for args, message in (
        (["laggard==1.0", "--suggest"], "--suggest only adds to --explain; pass --explain too"),
        (["laggard==1.0", "--explain", "--suggest", "--markdown"], "--suggest has no Markdown form yet"),
    ):
        with pytest.raises(SystemExit) as e:
            main(args)
        assert e.value.code == 2 and message in capsys.readouterr().err


def test_offline_refuses_it(capsys, fake_pypi, fixtures, tmp_path):
    code, _, err = run(capsys, str(fixtures / "uvproj" / "uv.lock"), "--offline", *FLAGS)
    assert code == 2 and "--offline: --suggest would look packages up on PyPI; drop --suggest" in err
    assert not fake_pypi
    code, out, _ = run(capsys, str(fixtures / "uvproj" / "uv.lock"), "--offline", *FLAGS, "--json")
    assert code == 2 and json.loads(out)["error"]["type"] == "SourceError"


def test_suggest_targets_still_parses(capsys, tmp_path):
    # Before --suggest, argparse read `--suggest` as an abbreviation of --suggest-targets.
    for argv in (["--suggest-targets", str(tmp_path)], ["--suggest-target", str(tmp_path)]):
        code, out, _ = run(capsys, *argv, "--json")
        assert code == 0 and "targets" in json.loads(out)
    with pytest.raises(SystemExit):
        main(["--suggest", str(tmp_path)])  # now the flag of its own, which needs --explain
    assert "--suggest only adds to --explain" in capsys.readouterr().err
