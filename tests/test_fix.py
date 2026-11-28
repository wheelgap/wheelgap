"""--fix: which gaps a wheel on wheelgap's gap wheel index fills (wheelgap.gapwheels), from the fake list in
conftest.py (the autouse gap_wheels fixture): never the network."""

import json
import tomllib
import urllib.error

import pytest
from conftest import FAKE_INDEX
from packaging.requirements import Requirement
from test_cli import _assert_matches_output_schema, run

from wheelgap import cache, cli, fix, gapwheels, pypi
from wheelgap.check import Row, Status
from wheelgap.sources import Package, Source, load_inputs
from wheelgap.targets import build_targets

INDEX = "https://wheelgap.github.io/wheels/simple/"
DOWNLOAD = "https://github.com/wheelgap/wheels/releases/download/gap-wheels/"
FT_WHEEL = "nowheels-0.9-cp314-cp314t-win_arm64.whl"
NOWHEELS = ["nowheels==0.9", "-p", "3.14t", "-t", "win-arm64"]
NOTHING_FITS = "wheelgap: note: --fix: no wheel on the gap wheel list fetched at "
UNREADABLE = "wheelgap: note: couldn't read the gap wheel list, so --fix suggests no gap wheels"
FIX_HINT = (
    "Run with --fix for a way out: an upgrade where a newer release has the wheel, else wheelgap's gap wheel index."
)


def summary(gaps, upgrade, gap_wheel, none, packages=1, unchecked=0):
    """The first line of the --fix section: only upgrades count as fixed."""
    line = (
        f"--fix: {gaps} gap{'s' * (gaps != 1)} ({packages} package{'s' * (packages != 1)} × "
        f"{'its' if packages == 1 else 'their'} targets): {upgrade} can be fixed by upgrading to an official "
        f"release; {gap_wheel} only by an unofficial gap wheel; {none} {'has' if none == 1 else 'have'} no fix yet"
    )
    if unchecked:
        line += f"; {unchecked} {'was' if unchecked == 1 else 'were'}n't checked for an upgrade"
    return line + "."


def without_fix_hint(output):
    return output.replace(FIX_HINT + "\n", "")


def uv_lock(registry):
    return (
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "nowheels" }]\n\n'
        f'[[package]]\nname = "nowheels"\nversion = "0.9"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/nowheels-0.9.tar.gz" }\n'
    )


def gap_wheels_json(capsys, *args):
    code, out, err = run(capsys, *args, "--json")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    return code, data, err


def test_pip_way_for_package_names_and_the_same_exit_code(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL, target="3.14t win-arm64")
    code_plain, out_plain, err_plain = run(capsys, *NOWHEELS)
    assert gap_wheels.calls == 0  # no request without --fix
    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    assert code == code_plain == 1 and err == err_plain
    section = (
        "\n"
        f"{summary(1, 0, 1, 0)}\n"
        "\n"
        f"{gapwheels.HEAD}\n"
        "  nowheels 0.9: win-arm64\n"
        "\n"
        "To install them with pip, add this line to your requirements file:\n"
        f"  --extra-index-url {INDEX}\n"
        "or pass it to pip install:\n"
        f"  pip install --extra-index-url {INDEX} ...\n"
        "If the requirements pin hashes, add each package's hashes to its existing line, keeping the hashes it has "
        "(on a second line for the same package, pip won't use them):\n"
        "  add to the nowheels==0.9 line:\n"
        f"    --hash=sha256:{'ab' * 32}\n"
        f"{gapwheels.PIP_CAVEAT}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    assert out == without_fix_hint(out_plain) + section
    assert "[tool.uv" not in out


def test_pip_way_for_a_requirements_file(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1 and "  nowheels 0.9: win-arm64\n" in out and f"  --extra-index-url {INDEX}\n" in out


# A package a gap wheel fills on two targets: pip allows only the hashes that every line for a package shares, so
# both wheels' hashes go on the package's one existing line, never one line per wheel (#695).
TWO_TARGETS = ["-p", "3.14t", "-t", "win-arm64,win-amd64", "--fix"]
HASHES = f"--hash=sha256:{'ab' * 32} --hash=sha256:{'cd' * 32}"


def _two_target_wheels(gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    gap_wheels.add("nowheels", "0.9", "nowheels-0.9-cp314-cp314t-win_amd64.whl", sha256="cd" * 32)


def test_hashes_of_a_package_on_two_targets_go_on_its_one_line(capsys, fake_pypi, gap_wheels, tmp_path):
    _two_target_wheels(gap_wheels)
    reqs = tmp_path / "requirements.txt"
    reqs.write_text(f"nowheels==0.9 --hash=sha256:{'00' * 32}\n", encoding="utf-8")
    code, out, _ = run(capsys, str(reqs), *TWO_TARGETS)
    assert code == 1
    assert out.endswith(
        "If the requirements pin hashes, add each package's hashes to its existing line, keeping the hashes it has "
        "(on a second line for the same package, pip won't use them):\n"
        "  add to the nowheels==0.9 line:\n"
        f"    {HASHES}\n"
        f"{gapwheels.PIP_CAVEAT}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    assert out.count("--hash=") == 2 and "nowheels==0.9 --hash" not in out  # no line pip would take as a requirement


def test_hashes_of_a_package_on_two_targets_in_markdown(capsys, fake_pypi, gap_wheels):
    _two_target_wheels(gap_wheels)
    code, out, _ = run(capsys, "nowheels==0.9", *TWO_TARGETS, "--markdown")
    assert code == 1
    assert (
        "If the requirements pin hashes, add each package's hashes to its existing line, keeping the hashes it has "
        "(on a second line for the same package, pip won't use them):\n"
        "\n"
        f"- add to the `nowheels==0.9` line: `{HASHES}`\n"
        "\n"
    ) in out
    assert out.count("--hash=") == 2 and "nowheels==0.9 --hash" not in out


def test_hashes_of_a_package_on_two_targets_in_the_snippet(capsys, fake_pypi, gap_wheels, tmp_path):
    _two_target_wheels(gap_wheels)
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("nowheels==0.9\n", encoding="utf-8")
    code, out, _ = run(capsys, str(reqs), *TWO_TARGETS, "--print-gap-wheel-snippet")
    assert code == 1
    assert out.endswith(
        f"--extra-index-url {INDEX}\n"
        "# If the requirements pin hashes, add these to the existing nowheels==0.9 line, keeping the hashes it has "
        f"(not as a line of their own, which pip won't use): {HASHES}\n"
    )


def test_pip_way_for_a_conda_environment_file(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    env = tmp_path / "environment.yml"
    env.write_text("dependencies:\n  - python=3.14\n  - pip:\n    - nowheels==0.9\n", encoding="utf-8")
    code, out, _ = run(capsys, str(env), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1 and "  nowheels 0.9: win-arm64\n" in out
    assert f"To install them with pip, add this line to your requirements file:\n  --extra-index-url {INDEX}\n" in out
    assert "lock again" not in out and "[tool.uv" not in out
    code, out, _ = run(capsys, str(env), "-p", "3.14t", "-t", "win-arm64", "--fix", "--explain")
    assert f"pip install --extra-index-url {INDEX} nowheels==0.9" in out


def test_uv_way_for_a_uv_lock_with_direct_and_transitive_packages(capsys, fake_pypi, fixtures, gap_wheels):
    # puresdist is a direct dependency of the uvproj project; nativeext comes in through fastlib.
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    gap_wheels.add("nativeext", "0.3", "nativeext-0.3-cp314-cp314t-win_arm64.whl")
    args = [str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    assert out.removeprefix(without_fix_hint(out_plain)) == (
        "\n"
        f"{summary(4, 0, 2, 2, packages=4)}\n"
        "\n"
        "No fix yet:\n"
        "  fastlib 2.0.0: no newer release has a wheel for win-arm64\n"
        "  winonly 311: no newer release has a wheel for win-arm64\n"
        "\n"
        # Not in FAKE_INDEX: a 404, as pypi.org gives for a name it doesn't have.
        "Newer releases of nativeext, puresdist weren't looked up: the lookup failed (HTTP 404).\n"
        "\n"
        f"{gapwheels.HEAD}\n"
        "  nativeext 0.3: win-arm64\n"
        "  puresdist 1.0: win-arm64\n"
        "\n"
        "To install them with uv, add this to pyproject.toml, then run uv lock:\n"
        "\n"
        "[[tool.uv.index]]\n"
        'name = "wheelgap"\n'
        f'url = "{INDEX}"\n'
        "explicit = true\n"
        "\n"
        "[tool.uv.sources]\n"
        'nativeext = { index = "wheelgap" }\n'
        'puresdist = { index = "wheelgap" }\n'
        "\n"
        "nativeext comes in through fastlib; uv only applies [tool.uv.sources] to direct dependencies, so also list "
        '"nativeext" where fastlib is (the same dependencies, dependency group or extra).\n'
        f"{gapwheels.UV_MERGE}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    assert "--extra-index-url" not in out


def test_uv_note_when_the_lock_cant_tell_whether_a_package_is_direct():
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    wheel = gapwheels.parse(
        {"schema": 1, "wheels": [{"name": "x", "version": "1", "wheel": "x-1-py3-none-any.whl", "sha256": "0" * 64}]}
    )[0]  # type: ignore[index]
    row = Row(Package("x", "1"), {target: Status.SOURCE})  # no dependency path
    fill = gapwheels.Fill(row, target, wheel, Source("uv.lock", kind="uv"))
    assert (
        "wheelgap can't tell whether x is a direct dependency; uv only applies [tool.uv.sources] to direct "
        'dependencies, so to be safe also list "x" as one.'
    ) in gapwheels.text_lines([fill], False)
    assert "- wheelgap can't tell whether `x` is a direct dependency; uv only applies `[tool.uv.sources]`" in "\n".join(
        gapwheels.markdown_lines([fill], False)
    )


def test_package_names_that_need_quoting_in_toml():
    assert gapwheels._toml_key("zope.interface") == '"zope.interface"'
    assert gapwheels._toml_key("rpds-py") == "rpds-py"


@pytest.mark.parametrize(
    ("args", "entry", "note"),
    [
        # Another version of the package.
        (NOWHEELS, ("nowheels", "0.8", "nowheels-0.8-cp314-cp314t-win_arm64.whl"), True),
        # A cp314 wheel doesn't install on free-threaded 3.14t.
        (NOWHEELS, ("nowheels", "0.9", "nowheels-0.9-cp314-cp314-win_arm64.whl"), True),
        # A 3.14t wheel doesn't install on 3.14.
        (["nowheels==0.9", "-p", "3.14", "-t", "win-arm64"], ("nowheels", "0.9", FT_WHEEL), True),
        # Another platform.
        (NOWHEELS, ("nowheels", "0.9", "nowheels-0.9-cp314-cp314t-win_amd64.whl"), True),
        # An ignored gap isn't a gap.
        ([*NOWHEELS, "--ignore", "nowheels:win-arm64"], ("nowheels", "0.9", FT_WHEEL), False),
        # A wheel cell: purewheel has a wheel everywhere, so nothing is asked.
        (
            ["purewheel==3.1", "-p", "3.14t", "-t", "win-arm64"],
            ("purewheel", "3.1", "purewheel-3.1-cp314-cp314t-win_arm64.whl"),
            False,
        ),
    ],
)
def test_no_suggestion(capsys, fake_pypi, gap_wheels, args, entry, note):
    gap_wheels.add(*entry)
    code_plain, _, _ = run(capsys, *args)
    code, out, err = run(capsys, *args, "--fix")
    assert code == code_plain and "gap wheel index has" not in out
    assert (NOTHING_FITS in err) is note
    _, data, _ = gap_wheels_json(capsys, *args, "--fix")
    assert data["gap_wheels"] == []


def test_entry_requires_python_must_admit_the_target(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL, requires_python=">=3.15")
    _, data, err = gap_wheels_json(capsys, *NOWHEELS, "--fix")
    assert data["gap_wheels"] == [] and NOTHING_FITS in err


def test_entry_requires_python_null_means_no_limit(capsys, fake_pypi, gap_wheels):
    # The wheels pipeline writes null when PyPI states no Requires-Python.
    gap_wheels.add("nowheels", "0.9", FT_WHEEL, requires_python=None)
    _, data, _ = gap_wheels_json(capsys, *NOWHEELS, "--fix")
    assert [g["wheel"] for g in data["gap_wheels"]] == [FT_WHEEL]
    (entry,) = gapwheels.parse({"schema": 1, "wheels": [{**GOOD, "requires_python": None}]})
    assert entry.requires_python is None


def test_private_package_gets_no_suggestion_and_no_request(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "uv.lock").write_text(uv_lock("https://pkgs.example.com/simple"), encoding="utf-8")
    code, data, err = gap_wheels_json(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1 and data["gap_wheels"] == [] and gap_wheels.calls == 0
    # Nor a command-line name that the lock places on a private index: it may be that same package.
    args = [str(tmp_path / "uv.lock"), "nowheels==0.9", "-p", "3.14t", "-t", "win-arm64", "--fix"]
    _, data, _ = gap_wheels_json(capsys, *args)
    assert data["gap_wheels"] == [] and gap_wheels.calls == 0


def test_no_binary_gets_no_suggestion(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "requirements.txt").write_text("--no-binary nowheels\nnowheels==0.9\n", encoding="utf-8")
    _, data, _ = gap_wheels_json(capsys, str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["gap_wheels"] == []  # pip wouldn't use the gap wheel either


def test_json_key_only_with_fix(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", "nowheels-0.9-cp314-cp314t-win_arm64.whl", sha256="cd" * 32)
    code_plain, plain, _ = gap_wheels_json(capsys, *NOWHEELS, "-p", "3.14")
    code, data, _ = gap_wheels_json(capsys, *NOWHEELS, "-p", "3.14", "--fix")
    assert "gap_wheels" not in plain and code == code_plain == 1
    assert data["gap_wheels"] == [
        {
            "name": "nowheels",
            "version": "0.9",
            "target": "3.14t win-arm64",  # the target label, as in "targets"
            "wheel": FT_WHEEL,
            "url": DOWNLOAD + FT_WHEEL,
            "sha256": "cd" * 32,
        }
    ]
    assert data["fix_summary"] == {
        "gaps": 2,
        "upgrade": 0,
        "gap_wheel": 1,
        "none": 1,
        "not_looked_up": 0,
        "uv_may_fork": 0,
    }
    assert [p["fix"] for p in data["packages"]] == [
        {
            "3.14t win-arm64": {
                "kind": "gap_wheel",
                "wheel": FT_WHEEL,
                "url": DOWNLOAD + FT_WHEEL,
                "sha256": "cd" * 32,
                "unofficial": True,
            },
            "3.14 win-arm64": {
                "kind": "none",
                "reason": "no_newer_release",
                "abi3_only": False,
                "build_packages": None,
            },
        }
    ]
    for p in data["packages"]:
        del p["fix"]
    # Nothing else changes.
    assert {k: v for k, v in data.items() if k not in ("gap_wheels", "fix_summary")} == plain


def test_markdown_section(capsys, fixtures, gap_wheels):
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    _, out, _ = run(
        capsys, str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--fix", "--markdown"
    )
    assert "#### Gap wheels\n" in out and "- `puresdist 1.0: win-arm64`\n" in out
    assert FIX_HINT not in out
    assert '```toml\n[[tool.uv.index]]\nname = "wheelgap"\n' in out and 'puresdist = { index = "wheelgap" }\n```' in out
    _, out, _ = run(capsys, *NOWHEELS, "--markdown")
    assert "Gap wheels" not in out


def test_fix_hint_in_text_and_markdown_never_reads_the_list(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    code, out, _ = run(capsys, *NOWHEELS)
    assert code == 1 and f"Run with --explain to see which wheels were rejected and why.\n{FIX_HINT}\n" in out
    code, out, _ = run(capsys, *NOWHEELS, "--explain")
    assert code == 1 and FIX_HINT in out
    code, out, _ = run(capsys, *NOWHEELS, "--markdown")
    assert code == 1 and out.rstrip().endswith(FIX_HINT)
    assert gap_wheels.calls == 0


COUNTED_HINT = (
    "Run with --fix for a way out: an upgrade where a newer release has the wheel, else wheelgap's gap wheel index, "
    "which has a wheel for 1 of these gaps."
)


def fix_hint_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("Run with --fix")]


def cache_gap_wheel_list(gap_wheels, *wheels: str, name: str = "nowheels", version: str = "0.9") -> None:
    """Leave a gap wheel list with these wheels in the cache, as a --fix run would, and make any fetch after it fail:
    a run without --fix must not make one (gap_wheels.calls counts them)."""
    for wheel in wheels:
        gap_wheels.add(name, version, wheel)
    cache.save_gap_wheels(pypi.GAP_WHEELS_URL, gap_wheels.doc)
    gap_wheels.doc = urllib.error.URLError("a run without --fix must never fetch the gap wheel list")


@pytest.mark.parametrize("format_flags", [(), ("--markdown",)])
@pytest.mark.parametrize(
    ("list_state", "cache_flags", "expected"),
    [
        ("fitting", (), COUNTED_HINT),
        ("other-platform", (), None),
        ("absent", (), FIX_HINT),
        ("fitting", ("--no-cache",), FIX_HINT),
        ("other-platform", ("--no-cache",), FIX_HINT),
        ("other-platform", ("--online",), FIX_HINT),
    ],
)
def test_fix_hint_uses_only_the_cached_list(
    capsys, fake_pypi, gap_wheels, format_flags, list_state, cache_flags, expected
):
    if list_state == "absent":
        gap_wheels.doc = urllib.error.URLError("a run without --fix must never fetch the gap wheel list")
    else:
        other = "nowheels-0.9-cp314-cp314t-win_amd64.whl"
        cache_gap_wheel_list(gap_wheels, FT_WHEEL if list_state == "fitting" else other)
    _, out, _ = run(capsys, *NOWHEELS, *format_flags, *cache_flags)
    assert fix_hint_lines(out) == ([expected] if expected else [])
    assert gap_wheels.calls == 0


def test_fix_hint_counts_the_cells_the_cached_list_fills(capsys, fake_pypi, gap_wheels):
    cache_gap_wheel_list(gap_wheels, FT_WHEEL, "nowheels-0.9-cp314-cp314t-win_amd64.whl")
    _, out, _ = run(capsys, "nowheels==0.9", "-p", "3.14t", "-t", "win-arm64,win-amd64,linux-x86_64")
    assert fix_hint_lines(out) == [COUNTED_HINT.replace("for 1 of", "for 2 of")]
    assert gap_wheels.calls == 0


def test_fix_hint_goes_back_to_the_plain_line_when_the_cached_list_is_too_old(
    capsys, fake_pypi, gap_wheels, monkeypatch
):
    now = 1_700_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    cache_gap_wheel_list(gap_wheels, "nowheels-0.9-cp314-cp314t-win_amd64.whl")  # fills nothing of this run
    now += cache.GAP_WHEELS_MAX_AGE - 60
    _, out, _ = run(capsys, *NOWHEELS)
    assert fix_hint_lines(out) == []
    now += 120  # past the list's hour: it says nothing about the index any more
    _, out, _ = run(capsys, *NOWHEELS)
    assert fix_hint_lines(out) == [FIX_HINT]
    assert gap_wheels.calls == 0


def test_fix_hint_goes_back_to_the_plain_line_when_the_cached_list_is_unusable(
    capsys, fake_pypi, gap_wheels, lookup_cache
):
    cache_gap_wheel_list(gap_wheels, FT_WHEEL)
    entries = list(lookup_cache.glob("*.json"))
    assert entries
    for entry in entries:
        entry.write_bytes(b"not json")
    _, out, _ = run(capsys, *NOWHEELS)
    assert fix_hint_lines(out) == [FIX_HINT] and gap_wheels.calls == 0


@pytest.mark.parametrize("flags", [("-q",), ("--json",), ("--index-url", "https://pkgs.example.com/simple")])
def test_no_fix_hint_from_a_cached_list_where_the_plain_hint_is_omitted(
    capsys, fake_pypi, fake_simple, gap_wheels, flags
):
    cache_gap_wheel_list(gap_wheels, FT_WHEEL)
    _, out, _ = run(capsys, *NOWHEELS)
    assert fix_hint_lines(out) == [COUNTED_HINT]
    _, out, _ = run(capsys, *NOWHEELS, *flags)
    assert "Run with --fix" not in out and gap_wheels.calls == 0


def test_no_fix_hint_from_a_cached_list_when_offline(capsys, fixtures, gap_wheels):
    # The uvproj lock records its files, so --offline can read it; the cached list fills puresdist's gap there.
    lock = str(fixtures / "uvproj" / "uv.lock")
    cache_gap_wheel_list(gap_wheels, "puresdist-1.0-cp314-cp314t-win_arm64.whl", name="puresdist", version="1.0")
    _, out, _ = run(capsys, lock, "-p", "3.14t", "-t", "win-arm64")
    assert fix_hint_lines(out) == [COUNTED_HINT]
    _, out, _ = run(capsys, lock, "-p", "3.14t", "-t", "win-arm64", "--offline")
    assert "Run with --fix" not in out and gap_wheels.calls == 0


def test_no_fix_hint_from_a_cached_list_with_a_mirror(capsys, fake_pypi, gap_wheels, monkeypatch):
    cache_gap_wheel_list(gap_wheels, FT_WHEEL)
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    _, out, _ = run(capsys, *NOWHEELS)
    assert "Run with --fix" not in out and gap_wheels.calls == 0


def test_no_fix_hint_from_a_cached_list_for_a_no_binary_or_private_package(capsys, fake_pypi, gap_wheels, tmp_path):
    cache_gap_wheel_list(gap_wheels, FT_WHEEL)
    req = tmp_path / "requirements.txt"
    req.write_text("--no-binary nowheels\nnowheels==0.9\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.14t", "-t", "win-arm64")
    assert "Run with --fix" not in out
    (tmp_path / "uv.lock").write_text(uv_lock("https://pkgs.example.com/simple"), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--markdown")
    assert "Run with --fix" not in out and gap_wheels.calls == 0


@pytest.mark.parametrize("flags", [("--fix",), ("-q",), ("--json",)])
def test_fix_hint_omitted_for_other_output_modes(capsys, fake_pypi, gap_wheels, flags):
    _, out, _ = run(capsys, *NOWHEELS, *flags)
    assert FIX_HINT not in out


def test_fix_hint_omitted_when_offline_or_another_index(capsys, fixtures, fake_pypi, fake_simple, gap_wheels, tmp_path):
    lock = fixtures / "uvproj" / "uv.lock"
    _, out, _ = run(capsys, str(lock), "-p", "3.14t", "-t", "win-arm64", "--offline")
    assert FIX_HINT not in out
    (tmp_path / "uv.lock").write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64")
    assert FIX_HINT not in out
    _, out, _ = run(capsys, *NOWHEELS, "--index-url", "https://pkgs.example.com/simple")
    assert FIX_HINT not in out and gap_wheels.calls == 0


def test_fix_hint_omitted_for_no_binary_or_private_package(capsys, fake_pypi, gap_wheels, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("--no-binary nowheels\nnowheels==0.9\n", encoding="utf-8")
    _, out, _ = run(capsys, str(req), "-p", "3.14t", "-t", "win-arm64")
    assert FIX_HINT not in out
    (tmp_path / "uv.lock").write_text(uv_lock("https://pkgs.example.com/simple"), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--markdown")
    assert FIX_HINT not in out and gap_wheels.calls == 0


def test_quiet_keeps_the_section_and_drops_the_nothing_fits_note(capsys, fake_pypi, gap_wheels):
    _, out, err = run(capsys, *NOWHEELS, "--fix", "-q")
    assert "gap wheel index" not in out and NOTHING_FITS not in err
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    _, out, _ = run(capsys, *NOWHEELS, "--fix", "-q")
    assert "  nowheels 0.9: win-arm64\n" in out


@pytest.mark.parametrize(
    "doc",
    [
        urllib.error.URLError("unreachable"),
        urllib.error.HTTPError(pypi.GAP_WHEELS_URL, 404, "Not Found", {}, None),  # type: ignore[arg-type]
        json.JSONDecodeError("Expecting value", "<html>", 0),
        TimeoutError("timed out"),
        [],
        {"schema": 2, "wheels": []},
        {"schema": 1, "wheels": {}},
        {"schema": 1},
    ],
)
def test_unreadable_list_is_a_note_with_the_same_exit_code(capsys, fake_pypi, gap_wheels, doc):
    gap_wheels.doc = doc
    code_plain, out_plain, _ = run(capsys, *NOWHEELS)
    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert code == code_plain == 1
    assert out == without_fix_hint(out_plain) + (
        f"\n{summary(1, 0, 0, 1)}\n\nNo fix yet:\n  nowheels 0.9: no newer release has a wheel for win-arm64\n"
    )
    assert UNREADABLE in err and NOTHING_FITS not in err
    _, out, err = run(capsys, *NOWHEELS, "--fix", "-q")
    assert UNREADABLE in err  # even with -q: otherwise it would read as "nothing fits"


GOOD = {"name": "nowheels", "version": "0.9", "wheel": FT_WHEEL, "sha256": "ab" * 32}


@pytest.mark.parametrize(
    "bad",
    [
        "not an entry",
        {**GOOD, "name": "no wheels"},
        {**GOOD, "name": 1},
        {k: v for k, v in GOOD.items() if k != "name"},
        {**GOOD, "version": "not a version"},
        {**GOOD, "version": 0.9},
        {**GOOD, "wheel": "nowheels-0.9.tar.gz"},
        {**GOOD, "wheel": "nowheels-0.9-cp314t-win_arm64.whl"},  # no ABI tag
        {**GOOD, "wheel": "other-0.9-cp314-cp314t-win_arm64.whl"},  # another project
        {**GOOD, "wheel": "nowheels-1.0-cp314-cp314t-win_arm64.whl"},  # another version
        {**GOOD, "wheel": "nowheels-0.9-cp314-cp314t-../../win_arm64.whl"},
        {**GOOD, "sha256": "AB" * 32},
        {**GOOD, "sha256": "ab" * 31},
        {**GOOD, "sha256": None},
        {**GOOD, "requires_python": ">=three"},
        {**GOOD, "requires_python": 3.9},
    ],
)
def test_malformed_entries_are_dropped_whole(capsys, fake_pypi, gap_wheels, bad):
    assert gapwheels.parse({"schema": 1, "wheels": [bad]}) == []
    good = gapwheels.parse({"schema": 1, "wheels": [bad, GOOD]})
    assert good is not None and [w.wheel for w in good] == [FT_WHEEL]
    gap_wheels.doc = {"schema": 1, "wheels": [bad]}
    _, data, err = gap_wheels_json(capsys, *NOWHEELS, "--fix")
    assert data["gap_wheels"] == [] and NOTHING_FITS in err and UNREADABLE not in err


def test_equal_versions_match_and_the_entry_target_is_not_used(capsys, fake_pypi, gap_wheels):
    # 0.9.0 == 0.9; the entry's own target text is ignored: the wheel's tags decide.
    gap_wheels.add("NoWheels", "0.9.0", "nowheels-0.9.0-cp314-cp314t-win_arm64.whl", target="3.13 linux-x86_64")
    _, data, _ = gap_wheels_json(capsys, *NOWHEELS, "--fix")
    assert [(g["name"], g["target"], g["wheel"]) for g in data["gap_wheels"]] == [
        ("nowheels", "win-arm64", "nowheels-0.9.0-cp314-cp314t-win_arm64.whl")
    ]


def test_offline_refuses_fix(capsys, fixtures, gap_wheels, tmp_path):
    code, _, err = run(capsys, str(fixtures / "uvproj" / "uv.lock"), "--offline", "--fix")
    assert code == 2 and "--offline: --fix would read the gap wheel list from wheelgap.github.io" in err
    (tmp_path / "uv.lock").write_text((fixtures / "uvproj" / "uv.lock").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "uv.lock"), "--fix", "--json")
    assert code == 2 and "offline = true in [tool.wheelgap]: --fix would read" in err and "pass --online" in err
    assert json.loads(out)["error"]["type"] == "SourceError"
    assert gap_wheels.calls == 0


def test_another_index_makes_no_request(capsys, fake_pypi, fake_simple, gap_wheels, monkeypatch, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    code_plain, _, _ = run(capsys, *NOWHEELS, "--index-url", "https://pkgs.example.com/simple")
    code, data, err = gap_wheels_json(capsys, *NOWHEELS, "--fix", "--index-url", "https://pkgs.example.com/simple")
    assert code == code_plain == 1 and data["gap_wheels"] == [] and gap_wheels.calls == 0
    assert "wheelgap: note: --fix is off with --index-url: packages may come from another index" in err
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\nindex-url = "https://pkgs.example.com/simple"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    _, out, err = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "--fix is off with index-url in [tool.wheelgap]" in err and "gap wheel index has" not in out
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    _, out, err = run(capsys, *NOWHEELS, "--fix")
    assert "--fix is off with WHEELGAP_INDEX_URL" in err and "gap wheel index has" not in out
    assert gap_wheels.calls == 0
    _, _, err = run(capsys, *NOWHEELS, "--fix", "-q")
    assert "--fix is off" not in err


def test_fetch_reads_the_whole_list_once_with_a_cap(gap_wheels, monkeypatch):
    seen = []

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        seen.append((url, attempts, max_bytes))
        return {"schema": 1, "wheels": []}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    assert gap_wheels.real() == {"schema": 1, "wheels": []}
    assert seen == [("https://wheelgap.github.io/wheels/data/gap-wheels.json", 2, pypi.GAP_WHEELS_MAX_BYTES)]


def _fill(kind, label="input", via=None):
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    wheel = gapwheels.parse(
        {"schema": 1, "wheels": [{"name": "x", "version": "1", "wheel": "x-1-py3-none-any.whl", "sha256": "0" * 64}]}
    )[0]  # type: ignore[index]
    row = Row(Package("x", "1"), {target: Status.SOURCE})
    if via is not None:
        row.via[target] = via
    return gapwheels.Fill(row, target, wheel, Source(label, kind=kind))


def test_packages_it_couldnt_place_on_pypi_get_a_note_not_nothing_fits(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    req = tmp_path / "requirements.txt"
    req.write_text("--extra-index-url https://pkgs.example.com/simple\nnowheels==0.9\n", encoding="utf-8")
    code, data, err = gap_wheels_json(capsys, str(req), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["gap_wheels"] == [] and gap_wheels.calls == 0
    assert "--fix didn't check nowheels: wheelgap can't tell they come from PyPI" in err
    assert NOTHING_FITS not in err


def test_lockfiles_get_lock_advice_not_requirements_advice():
    text = "\n".join(gapwheels.text_lines([_fill("pylock", "pylock.toml")], False))
    assert "--extra-index-url" not in text and "[tool.uv" not in text
    assert f"pylock.toml pins each file, so add {INDEX} as an index" in text
    md = "\n".join(gapwheels.markdown_lines([_fill("pdm", "pdm.lock")], False))
    assert f"`pdm.lock` pins each file, so add `{INDEX}` as an index" in md and "--extra-index-url" not in md


def test_dev_group_package_is_listed_where_its_parent_is_not_in_project_dependencies():
    text = "\n".join(gapwheels.text_lines([_fill("uv", "uv.lock", ["(dev group)", "pytest-thing", "x"])], False))
    assert 'also list "x" where pytest-thing is (the same dependencies, dependency group or extra)' in text
    assert "[project] dependencies" not in text


def test_the_same_target_from_two_inputs_is_listed_once():
    lines = gapwheels.text_lines([_fill("requirements", "a.txt"), _fill("requirements", "b.txt")], False)
    assert "  x 1: win-arm64" in lines
    assert lines.count("  add to the x==1 line:") == 1 and sum("--hash=" in line for line in lines) == 1


def test_no_binary_package_isnt_blamed_on_its_origin(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    req = tmp_path / "requirements.txt"
    req.write_text("--no-binary nowheels\nnowheels==0.9\n", encoding="utf-8")
    _, data, err = gap_wheels_json(capsys, str(req), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["gap_wheels"] == [] and "can't tell they come from PyPI" not in err


def test_uv_way_for_a_script_goes_in_its_script_block(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    script = tmp_path / "example.py"
    script.write_text('# /// script\n# dependencies = ["nowheels==0.9"]\n# ///\nprint("hi")\n', encoding="utf-8")
    args = [str(script), "-p", "3.14t", "-t", "win-arm64"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    assert out.removeprefix(without_fix_hint(out_plain)) == (
        "\n"
        f"{summary(1, 0, 1, 0)}\n"
        "\n"
        f"{gapwheels.HEAD}\n"
        "  nowheels 0.9: win-arm64\n"
        "\n"
        "To install them with uv run, add this at the end of example.py's # /// script block:\n"
        "\n"
        "# [[tool.uv.index]]\n"
        '# name = "wheelgap"\n'
        f'# url = "{INDEX}"\n'
        "# explicit = true\n"
        "#\n"
        "# [tool.uv.sources]\n"
        '# nowheels = { index = "wheelgap" }\n'
        "\n"
        f"{gapwheels.SCRIPT_MERGE}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    # No lockfile advice and no pip line: uv run reads the block.
    assert "pins each file" not in out and "lock again" not in out and "--extra-index-url" not in out


def script_lock(registry="https://pypi.org/simple"):
    """A lock as `uv lock --script example.py` writes it: the script lists parent, which brings in nowheels."""
    return (
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n[manifest]\nrequirements = [{ name = "parent" }]\n\n'
        f'[[package]]\nname = "parent"\nversion = "1"\nsource = {{ registry = "{registry}" }}\n'
        'dependencies = [{ name = "nowheels" }]\n'
        'wheels = [{ url = "https://files.example/parent-1-py3-none-any.whl" }]\n\n'
        f'[[package]]\nname = "nowheels"\nversion = "0.9"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/nowheels-0.9.tar.gz" }\n'
    )


def test_uv_way_for_a_script_lock_goes_in_the_scripts_block(capsys, fake_pypi, gap_wheels, tmp_path):
    # A pyproject.toml beside it is another project's: uv run doesn't take the script's sources from it.
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0"\n', encoding="utf-8")
    lock = tmp_path / "example.py.lock"
    lock.write_text(script_lock(), encoding="utf-8")
    args = [str(lock), "-p", "3.14t", "-t", "win-arm64"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    assert out.removeprefix(without_fix_hint(out_plain)) == (
        "\n"
        f"{summary(1, 0, 1, 0)}\n"
        "\n"
        f"{gapwheels.HEAD}\n"
        "  nowheels 0.9: win-arm64\n"
        "\n"
        "To install them with uv run, add this at the end of example.py's # /// script block, then run "
        f"uv lock --script {tmp_path / 'example.py'}:\n"
        "\n"
        "# [[tool.uv.index]]\n"
        '# name = "wheelgap"\n'
        f'# url = "{INDEX}"\n'
        "# explicit = true\n"
        "#\n"
        "# [tool.uv.sources]\n"
        '# nowheels = { index = "wheelgap" }\n'
        "\n"
        'uv only applies [tool.uv.sources] to a script\'s own dependencies, so also list "nowheels" in its '
        "dependencies.\n"
        f"{gapwheels.SCRIPT_MERGE}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    assert "pyproject.toml" not in out and "pins each file" not in out and "--extra-index-url" not in out
    # Markdown, from the same parsed lock.
    (source,) = load_inputs([str(lock)])
    fill = _fill("uv")
    md = "\n".join(gapwheels.markdown_lines([gapwheels.Fill(fill.row, fill.target, fill.wheel, source)], False))
    assert f"`example.py`'s `# /// script` block, then run `uv lock --script {tmp_path / 'example.py'}`:" in md
    assert '- uv only applies `[tool.uv.sources]` to a script\'s own dependencies, so also list `"x"`' in md
    assert "`pyproject.toml`" not in md


def test_script_fix_says_to_list_a_transitive_package_and_renders_in_markdown():
    fill = _fill("script", "example.py", ["parent", "x"])
    fill.source.specs.append("parent>=1")
    text = gapwheels.text_lines([fill], False)
    assert "To install them with uv run, add this at the end of example.py's # /// script block:" in text
    assert '# x = { index = "wheelgap" }' in text
    assert (
        'uv only applies [tool.uv.sources] to a script\'s own dependencies, so also list "x" in its dependencies.'
    ) in text
    assert not any("pins each file" in line for line in text)
    md = "\n".join(gapwheels.markdown_lines([fill], False))
    assert "add this at the end of `example.py`'s `# /// script` block:\n\n```python\n# [[tool.uv.index]]\n" in md
    assert '- uv only applies `[tool.uv.sources]` to a script\'s own dependencies, so also list `"x"`' in md
    assert "pins each file" not in md
    fill.source.specs.append("x==1")  # listed in the block: no note
    assert not any("also list" in line for line in gapwheels.text_lines([fill], False))


def test_lock_advice_carries_the_private_index_caveat():
    assert gapwheels.LOCK_CAVEAT in gapwheels.text_lines([_fill("poetry", "poetry.lock")], False)


def test_explain_fix_hints_are_per_target_and_preserve_exit_code(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    base_code, base_out, _ = run(capsys, *NOWHEELS, "--explain")
    assert gap_wheels.calls == 0  # opt-in only
    code, output, _ = run(capsys, *NOWHEELS, "--explain", "--fix")
    assert code == base_code == 1
    assert (
        f"  gap wheel for win-arm64: {gapwheels.UNOFFICIAL}\n    pip install --extra-index-url {INDEX} nowheels==0.9\n"
    ) in output
    assert "not from the package's maintainers" in output
    code, data, _ = gap_wheels_json(capsys, *NOWHEELS, "--explain", "--fix")
    assert code == 1
    item = next(p for p in data["packages"] if p["name"] == "nowheels")
    assert item["gap_wheel"] == {
        "win-arm64": {
            "wheel": FT_WHEEL,
            "url": DOWNLOAD + FT_WHEEL,
            "sha256": "ab" * 32,
            "install": f"pip install --extra-index-url {INDEX} nowheels==0.9",
        }
    }
    assert gap_wheels.calls == 1  # second invocation uses the validated list cache

    _, plain_data, _ = gap_wheels_json(capsys, *NOWHEELS, "--explain")
    assert "gap_wheel" not in next(p for p in plain_data["packages"] if p["name"] == "nowheels")


@pytest.mark.parametrize(
    "name,version,wheel",
    [
        ("nowheels", "9.9", FT_WHEEL),
        ("other", "0.9", "other-0.9-cp314-cp314t-win_arm64.whl"),
        ("nowheels", "0.9", "nowheels-0.9-cp313-cp313-win_arm64.whl"),
    ],
)
def test_explain_fix_never_suggests_wrong_wheels(capsys, fake_pypi, gap_wheels, name, version, wheel):
    gap_wheels.add(name, version, wheel)
    _, data, _ = gap_wheels_json(capsys, *NOWHEELS, "--explain", "--fix")
    row = next(p for p in data["packages"] if p["name"] == "nowheels")
    assert row.get("gap_wheel") == {}


def test_explain_fix_unreadable_index_does_not_crash_or_guess(capsys, fake_pypi, gap_wheels):
    gap_wheels.doc = {"schema": 1, "wheels": "malformed"}
    code, data, err = gap_wheels_json(capsys, *NOWHEELS, "--explain", "--fix")
    assert code == 1
    assert data["packages"][0].get("gap_wheel") == {}
    assert "couldn't read the gap wheel list" in err


def test_gap_wheel_cache_opt_out_and_validation(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix")
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    run(capsys, *NOWHEELS, "--fix", "--no-cache")
    assert gap_wheels.calls == 2


def test_explain_fix_hint_once_per_release_naming_its_targets(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    gap_wheels.add("nowheels", "0.9", "nowheels-0.9-cp314-cp314t-win_amd64.whl")
    _, out, _ = run(capsys, "nowheels==0.9", "-p", "3.14t", "-t", "win-arm64,win-amd64", "--explain", "--fix")
    (line,) = [ln for ln in out.splitlines() if "gap wheel for" in ln]
    assert "win-arm64" in line and "win-amd64" in line
    assert out.count(f"pip install --extra-index-url {INDEX} nowheels==0.9") == 1


def test_explain_fix_hint_for_a_uv_lock_points_to_the_fix_section(capsys, fake_pypi, fixtures, gap_wheels):
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    args = [str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--explain", "--fix"]
    code, out, _ = run(capsys, *args)
    assert code == 1
    hint = f"  gap wheel for win-arm64: {gapwheels.UNOFFICIAL}\n    see the gap wheel section below for how to use it\n"
    assert out.count(hint) == 1
    assert out.index(hint) < out.index("To install them with uv, add this to pyproject.toml")
    assert "--extra-index-url" not in out and "pip install" not in out  # pip would ignore an index for a uv.lock
    _, data, _ = gap_wheels_json(capsys, *args)
    item = next(p for p in data["packages"] if p["name"] == "puresdist")
    assert item["gap_wheel"] == {
        "win-arm64": {
            "wheel": "puresdist-1.0-cp314-cp314t-win_arm64.whl",
            "url": DOWNLOAD + "puresdist-1.0-cp314-cp314t-win_arm64.whl",
            "sha256": "ab" * 32,
        }
    }


def test_a_corrupt_cached_gap_wheel_list_is_fetched_again(capsys, fake_pypi, gap_wheels, lookup_cache):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    entries = list(lookup_cache.glob("*.json"))
    assert entries
    for entry in entries:  # the list's entry among them; a corrupt pinned lookup is only looked up again
        entry.write_bytes(b"not json")
    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2
    assert code == 1 and "  nowheels 0.9: win-arm64\n" in out and UNREADABLE not in err


def test_no_cache_does_not_save_the_gap_wheel_list(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix", "--no-cache")
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2  # the --no-cache run saved nothing for this one to read
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2  # but this one saved it


def test_cached_list_without_wheel_and_live_list_with_it_fetches_once(capsys, fake_pypi, gap_wheels, monkeypatch):
    from wheelgap import cache

    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
    monkeypatch.setattr(cache, "_now", lambda: now)

    gap_wheels.add("psycopg2", "2.9.11", "psycopg2-2.9.11-cp314-cp314t-win_arm64.whl")
    code, out, err = run(capsys, "psycopg2==2.9.11", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert gap_wheels.calls == 1
    assert "psycopg2 2.9.11: win-arm64" in out

    # Past the 5-minute recheck window, an empty-fill cached list refreshes once.
    now += cache.GAP_WHEELS_RECHECK_AGE + 60
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2
    assert "nowheels 0.9: win-arm64" in out
    assert NOTHING_FITS not in err


def test_cached_list_recheck_window_for_empty_fill(capsys, fake_pypi, gap_wheels, monkeypatch):
    from wheelgap import cache

    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
    monkeypatch.setattr(cache, "_now", lambda: now)

    gap_wheels.add("psycopg2", "2.9.11", "psycopg2-2.9.11-cp314-cp314t-win_arm64.whl")
    _, _, err0 = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    assert "wheelgap: note: --fix: no wheel on the gap wheel list fetched at 22:13 UTC fills these gaps\n" in err0

    # 1 minute old: answers from cached list without fetching.
    now += 60
    _, _, err1 = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    assert "wheelgap: note: --fix: no wheel on the gap wheel list fetched at 22:13 UTC fills these gaps\n" in err1

    # --no-cache always fetches even within the 5-minute window.
    _, _, err_nocache = run(capsys, *NOWHEELS, "--fix", "--no-cache")
    assert gap_wheels.calls == 2
    assert (
        "wheelgap: note: --fix: no wheel on the gap wheel list fetched at 22:14 UTC fills these gaps\n" in err_nocache
    )

    # 6 minutes old (past GAP_WHEELS_RECHECK_AGE): fetches again and reports the fresh fetch time.
    now += 5 * 60
    _, _, err6 = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 3
    assert "wheelgap: note: --fix: no wheel on the gap wheel list fetched at 22:19 UTC fills these gaps\n" in err6


def test_cached_list_that_fills_the_gap_makes_no_fetch(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1

    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    assert "nowheels 0.9: win-arm64" in out
    assert NOTHING_FITS not in err


def test_cached_list_without_wheel_and_failing_fetch_notes_cached_time(capsys, fake_pypi, gap_wheels, monkeypatch):
    import urllib.error

    from wheelgap import cache

    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
    monkeypatch.setattr(cache, "_now", lambda: now)

    gap_wheels.add("psycopg2", "2.9.11", "psycopg2-2.9.11-cp314-cp314t-win_arm64.whl")
    run(capsys, "psycopg2==2.9.11", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert gap_wheels.calls == 1

    now += cache.GAP_WHEELS_RECHECK_AGE + 60
    gap_wheels.doc = urllib.error.URLError("temporary failure")
    code, out, err = run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2
    expected_note = (
        "wheelgap: note: --fix: no wheel on the gap wheel list fetched at 22:13 UTC fills these gaps; "
        "the list couldn't be refreshed"
    )
    assert expected_note in err
    assert UNREADABLE not in err

    # Even with -q, like the unreadable-list note: the index couldn't be asked, so this isn't "nothing fits".
    _, _, err_quiet = run(capsys, *NOWHEELS, "--fix", "-q")
    assert gap_wheels.calls == 3
    assert expected_note in err_quiet and UNREADABLE not in err_quiet


def test_online_fetches_the_list_even_when_the_cached_one_fills_the_gap(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1
    _, out, _ = run(capsys, *NOWHEELS, "--fix", "--online")
    assert gap_wheels.calls == 2 and "  nowheels 0.9: win-arm64\n" in out
    # --online doesn't read the cached list, so a failed fetch has no list to answer from.
    gap_wheels.doc = urllib.error.URLError("temporary failure")
    _, out, err = run(capsys, *NOWHEELS, "--fix", "--online")
    assert gap_wheels.calls == 3 and "  nowheels 0.9: win-arm64\n" not in out
    assert UNREADABLE in err and "fetched at" not in err


def test_cached_list_older_than_limit_is_fetched_again(capsys, fake_pypi, gap_wheels, monkeypatch):
    from wheelgap import cache

    now = 1_700_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)

    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1

    now += cache.GAP_WHEELS_MAX_AGE - 60
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 1

    now += 120
    run(capsys, *NOWHEELS, "--fix")
    assert gap_wheels.calls == 2


SNIPPET = ["-p", "3.14t", "-t", "win-arm64", "--fix", "--print-gap-wheel-snippet"]
ABOUT = f"# Gap wheels are {gapwheels.UNOFFICIAL}: https://wheelgap.github.io/wheels/\n"


def test_print_gap_wheel_snippet_for_a_requirements_file(capsys, fake_pypi, gap_wheels, tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("nowheels==0.9\n", encoding="utf-8")
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    code, out, _ = run(capsys, str(requirements), *SNIPPET)
    assert code == 1  # the check's own exit code
    assert out == (
        "# requirements.txt (pip): add this line to it, or pass it to pip install.\n"
        f"{ABOUT}"
        "# fills: nowheels 0.9: win-arm64\n"
        f"# {gapwheels.PIP_CAVEAT}\n"
        f"--extra-index-url {INDEX}\n"
        "# If the requirements pin hashes, add these to the existing nowheels==0.9 line, keeping the hashes it has "
        f"(not as a line of their own, which pip won't use): --hash=sha256:{'ab' * 32}\n"
    )
    assert requirements.read_text(encoding="utf-8") == "nowheels==0.9\n"
    assert list(tmp_path.iterdir()) == [requirements]


def test_print_gap_wheel_snippet_for_package_names(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    code, out, _ = run(capsys, "nowheels==0.9", *SNIPPET)
    assert code == 1
    assert out.startswith(
        "# command line (pip): add this line to a requirements file, or pass it to pip install.\n"
        f"{ABOUT}"
        "# fills: nowheels 0.9: win-arm64\n"
    )
    assert f"\n--extra-index-url {INDEX}\n" in out


def test_print_gap_wheel_snippet_for_a_uv_lock(capsys, fixtures, gap_wheels):
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    gap_wheels.add("nativeext", "0.3", "nativeext-0.3-cp314-cp314t-win_arm64.whl")
    root = fixtures / "uvproj"
    original_lock = (root / "uv.lock").read_bytes()
    assert not (root / "pyproject.toml").exists()
    code, out, _ = run(capsys, str(root / "uv.lock"), *SNIPPET)
    assert code == 1
    assert out == (
        f"# {root / 'pyproject.toml'} (uv): add this, then run uv lock.\n"
        f"{ABOUT}"
        "# fills: nativeext 0.3: win-arm64\n"
        "# fills: puresdist 1.0: win-arm64\n"
        "[[tool.uv.index]]\n"
        'name = "wheelgap"\n'
        f'url = "{INDEX}"\n'
        "explicit = true\n"
        "\n"
        "[tool.uv.sources]\n"
        'nativeext = { index = "wheelgap" }\n'
        'puresdist = { index = "wheelgap" }\n'
        "# nativeext comes in through fastlib; uv only applies [tool.uv.sources] to direct dependencies, so also list "
        '"nativeext" where fastlib is (the same dependencies, dependency group or extra).\n'
        f"# {gapwheels.UV_MERGE}\n"
    )
    assert tomllib.loads(out)["tool"]["uv"]["sources"]["puresdist"] == {"index": "wheelgap"}
    assert (root / "uv.lock").read_bytes() == original_lock
    assert not (root / "pyproject.toml").exists()


def test_print_gap_wheel_snippet_is_empty_when_no_wheel_fits(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.8", "nowheels-0.8-cp314-cp314t-win_arm64.whl")
    code, out, _ = run(capsys, "nowheels==0.9", *SNIPPET)
    assert code == 1 and out == ""


@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        (lambda fixtures, tmp: [str(fixtures / "poetryproj" / "poetry.lock")], "poetry.lock"),
        (lambda fixtures, tmp: [str(tmp / "pyproject.toml")], "pyproject.toml"),
        (lambda fixtures, tmp: [str(tmp / "environment.yml")], "environment.yml"),
        (
            lambda fixtures, tmp: [str(tmp / "requirements.txt"), str(fixtures / "uvproj" / "uv.lock")],
            "requirements.txt, uv.lock",
        ),
    ],
)
def test_print_gap_wheel_snippet_refuses_other_inputs_before_any_lookup(
    capsys, fake_pypi, gap_wheels, fixtures, tmp_path, inputs, named
):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["nowheels==0.9"]\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    (tmp_path / "environment.yml").write_text("dependencies:\n  - pip:\n    - nowheels==0.9\n", encoding="utf-8")
    code, out, err = run(capsys, *inputs(fixtures, tmp_path), *SNIPPET)
    assert code == 2 and out == ""
    assert err == (
        f"wheelgap: error: --print-gap-wheel-snippet has no snippet for {named}: it takes one uv.lock "
        "(not a script's), one requirements file or package names on the command line (--fix alone covers the others)\n"
    )
    assert gap_wheels.calls == 0 and fake_pypi == []  # decided from the inputs alone, so the same every run


def test_print_gap_wheel_snippet_refuses_a_script_lock(capsys, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    lock = tmp_path / "example.py.lock"
    lock.write_text(script_lock(), encoding="utf-8")
    code, out, err = run(capsys, str(lock), *SNIPPET)
    assert code == 2 and out == ""
    assert "--print-gap-wheel-snippet has no snippet for example.py.lock: it takes one uv.lock (not a script's)" in err
    assert gap_wheels.calls == 0


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["nowheels==0.9", "--print-gap-wheel-snippet"], "needs --fix"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--json"], "--json"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--explain", "--conda-forge"], "--explain"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--all"], "--all"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--markdown"], "--markdown"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--junit", "out.xml"], "--junit"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--sarif", "out.sarif"], "--sarif"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--badge", "badge.json"], "--badge"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--github-annotations"], "--github-annotations"),
        ([*NOWHEELS, "--fix", "--print-gap-wheel-snippet", "--write-constraints", "c.txt"], "--write-constraints"),
        (["--from-log", "LOG", *SNIPPET], "--from-log"),
        (["--dist", "dist", *SNIPPET], "--dist"),
    ],
)
def test_print_gap_wheel_snippet_refused_combinations(capsys, gap_wheels, fixtures, argv, flag):
    argv = [str(fixtures / "pip25-two-failed.log") if a == "LOG" else a for a in argv]
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2
    err = capsys.readouterr().err
    assert "--print-gap-wheel-snippet" in err and flag in err
    assert gap_wheels.calls == 0


# --github-annotations --fix


def _annotations(text):
    return [line for line in text.splitlines() if line.startswith("::")]


def test_github_annotation_says_a_gap_wheel_fills_the_gap_with_the_pip_command(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\nfastlib==2.0.0\n", encoding="utf-8")
    args = [str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64,win-amd64", "--github-annotations"]
    code_plain, out_plain, _ = run(capsys, *args)
    plain = _annotations(out_plain)
    assert plain == [
        "::error file=requirements.txt,line=2,title=fastlib 2.0.0%3A builds from source::"
        "fastlib 2.0.0 builds from source on win-arm64, win-amd64.",
        "::error file=requirements.txt,line=1,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on win-arm64, win-amd64.",
    ]
    assert gap_wheels.calls == 0
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    assert (
        _annotations(out)
        == [
            plain[0],  # no fitting gap wheel: today's message
            # The level, title and line are the same; only win-arm64 has a gap wheel.
            plain[1] + " A gap wheel on wheelgap's gap wheel index fills win-arm64 (unofficial, built by wheelgap from "
            f"the PyPI sdist). Install it with: pip install --extra-index-url {INDEX} nowheels==0.9",
        ]
    )


def test_github_annotation_gap_wheel_pip_command_ends_with_the_version(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    args = [str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--github-annotations", "--fix"]
    code, out, _ = run(capsys, *args)
    assert code == 1
    (line,) = _annotations(out)
    # Nothing after the version: a copied command must not end in a full stop pip would reject.
    assert line.endswith("nowheels==0.9")
    assert "Install it with: " in line
    text = line.split("Install it with: ", 1)[1]
    Requirement(text.split()[-1])  # raises if the last word is not a valid requirement


def test_github_annotation_gap_wheel_pip_command_is_last_for_a_file_outside_the_workspace(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch
):
    # A file GitHub can't show in the diff is named in the message, "(in FILE)". That comes before the gap wheel
    # sentence, so the pip command still ends the annotation (#609).
    (tmp_path / "repo").mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "repo"))
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    req = tmp_path / "elsewhere" / "requirements.txt"
    req.write_text("nowheels==0.9\n", encoding="utf-8")
    code, out, _ = run(capsys, str(req), "-p", "3.14t", "-t", "win-arm64", "--github-annotations", "--fix")
    assert code == 1
    (line,) = _annotations(out)
    assert line.startswith("::error title=")  # no file= property: it's outside the workspace
    assert " (in " in line and line.index(" (in ") < line.index(" A gap wheel on wheelgap's gap wheel index")
    assert line.endswith(f"Install it with: pip install --extra-index-url {INDEX} nowheels==0.9")


def test_github_annotation_for_a_uv_lock_points_to_the_logs_gap_wheel_section(
    capsys, fake_pypi, fixtures, gap_wheels, monkeypatch
):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures))
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    args = [str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--github-annotations"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    plain, fixed = _annotations(out_plain), _annotations(out)
    (was,) = [line for line in plain if "::puresdist 1.0 " in line]
    assert was.startswith("::error file=uvproj/uv.lock,line=43,title=puresdist 1.0%3A builds from source::")
    assert fixed == [
        line
        if line != was
        else was
        + " A gap wheel on wheelgap's gap wheel index fills win-arm64 (unofficial, built by wheelgap from the PyPI "
        "sdist); see the gap wheel section of the log for how to use it."
        for line in plain
    ]
    assert len(fixed) > 1 and "pip install" not in "".join(fixed) and "--extra-index-url" not in "".join(fixed)
    assert "To install them with uv, add this to pyproject.toml" in out  # the section the sentence points to


def test_github_annotation_warning_keeps_its_level_with_a_gap_wheel(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    args = [*NOWHEELS, "--fail-on", "missing", "--github-annotations"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 0
    (was,) = _annotations(out_plain)
    (now,) = _annotations(out)
    assert was.startswith("::warning title=nowheels 0.9%3A builds from source::")
    assert now == was + (
        " A gap wheel on wheelgap's gap wheel index fills win-arm64 (unofficial, built by wheelgap from the PyPI "
        f"sdist). Install it with: pip install --extra-index-url {INDEX} nowheels==0.9"
    )


# --fix, upgrade first: fixme (tests/conftest.py) has no wheel at 1.0; 1.3 adds a Windows on ARM one, 1.4 a Windows x64
# one too. The yanked 1.1, the pre-release 2.0rc1 and 1.2 (whose Requires-Python leaves 3.14 out) never count.
FIXME_WHEEL = "fixme-1.0-cp314-cp314t-win_arm64.whl"
BOTH_WINDOWS = ["-p", "3.14t", "-t", "win-arm64,win-amd64"]


def _fixme_requirements(tmp_path, text="fixme==1.0\nnowheels==0.9\n"):
    reqs = tmp_path / "requirements.txt"
    reqs.write_text(text, encoding="utf-8")
    return str(reqs)


def _fixme_gap_wheels(gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    gap_wheels.add("fixme", "1.0", FIXME_WHEEL)  # an upgrade fixes this cell too: the gap wheel is the fallback


def test_fix_leads_with_the_upgrade_and_keeps_the_exit_code(capsys, fake_pypi, gap_wheels, tmp_path):
    _fixme_gap_wheels(gap_wheels)
    reqs = _fixme_requirements(tmp_path)
    code_plain, out_plain, _ = run(capsys, reqs, *BOTH_WINDOWS)
    code, out, _ = run(capsys, reqs, *BOTH_WINDOWS, "--fix")
    assert code == code_plain == 1
    section = out.removeprefix(without_fix_hint(out_plain))
    assert section.startswith(
        "\n"
        f"{summary(4, 2, 1, 1, packages=2)}\n"
        "\n"
        "Upgrade: a newer official release has the wheel:\n"
        "  fixme 1.0 -> 1.4: win-arm64, win-amd64\n"
        "In requirements.txt, change the pins to fixme==1.4.\n"
        f"{fix.RESOLVE_NOTE}\n"
        "\n"
        "No fix yet:\n"
        "  nowheels 0.9: no newer release has a wheel for win-amd64\n"
        "\n"
        f"{gapwheels.FALLBACK_HEAD}\n"
        "  fixme 1.0: win-arm64 (if you can't upgrade fixme)\n"
        "  nowheels 0.9: win-arm64\n"
    )
    # The gap wheel stays installable for whoever can't upgrade: it's for the pinned version, so it never conflicts.
    assert f"  add to the fixme==1.0 line:\n    --hash=sha256:{'ab' * 32}\n" in section
    assert section.endswith("How these wheels are built and checked: https://wheelgap.github.io/wheels/\n")


@pytest.mark.parametrize(
    "targets,version",
    [
        # 1.1's only wheel is yanked and 1.2's Requires-Python excludes 3.14: the nearest is 1.3.
        (["-p", "3.14t", "-t", "win-arm64"], "1.3"),
        # 1.3 has no Windows x64 wheel; 2.0rc1's pure wheel is a pre-release, and the pin isn't one.
        (["-p", "3.14t", "-t", "win-amd64"], "1.4"),
    ],
)
def test_fix_names_only_a_live_release_with_the_wheel(capsys, fake_pypi, gap_wheels, targets, version):
    _, out, _ = run(capsys, "fixme==1.0", *targets, "--fix")
    assert f"  fixme 1.0 -> {version}: " in out and "Use fixme==" + version + " instead." in out
    assert not any(f"-> {v}" in out for v in ("1.1", "1.2", "2.0rc1") if v != version)


def test_fix_json_has_each_cells_fix_and_a_summary(capsys, fake_pypi, gap_wheels, tmp_path):
    _fixme_gap_wheels(gap_wheels)
    reqs = _fixme_requirements(tmp_path)
    _, plain, _ = gap_wheels_json(capsys, reqs, *BOTH_WINDOWS)
    assert "fix_summary" not in plain and not any("fix" in p for p in plain["packages"])
    code, data, _ = gap_wheels_json(capsys, reqs, *BOTH_WINDOWS, "--fix")
    assert code == 1
    assert data["fix_summary"] == {
        "gaps": 4,
        "upgrade": 2,
        "gap_wheel": 1,
        "none": 1,
        "not_looked_up": 0,
        "uv_may_fork": 0,
    }
    fixes = {p["name"]: p["fix"] for p in data["packages"]}
    fallback = {"wheel": FIXME_WHEEL, "url": DOWNLOAD + FIXME_WHEEL, "sha256": "ab" * 32, "unofficial": True}
    how = "In requirements.txt, change the pins to fixme==1.4."
    assert fixes["fixme"] == {
        "win-arm64": {
            "kind": "upgrade",
            "version": "1.4",
            "prerelease": False,
            "exclude_newer": None,
            "how": how,
            "constraint": None,
            "gap_wheel": fallback,
        },
        "win-amd64": {
            "kind": "upgrade",
            "version": "1.4",
            "prerelease": False,
            "exclude_newer": None,
            "how": how,
            "constraint": None,
            "gap_wheel": None,
        },
    }
    assert fixes["nowheels"] == {
        "win-arm64": {"kind": "gap_wheel", **fallback, "wheel": FT_WHEEL, "url": DOWNLOAD + FT_WHEEL},
        "win-amd64": {"kind": "none", "reason": "no_newer_release", "abi3_only": False, "build_packages": None},
    }
    # The existing keys are unchanged: every filled cell is in gap_wheels, the fallback one too.
    assert [(w["name"], w["target"]) for w in data["gap_wheels"]] == [("fixme", "win-arm64"), ("nowheels", "win-arm64")]


def test_fix_markdown_leads_with_the_upgrade(capsys, fake_pypi, gap_wheels, tmp_path):
    _fixme_gap_wheels(gap_wheels)
    code, out, _ = run(capsys, _fixme_requirements(tmp_path), *BOTH_WINDOWS, "--fix", "--markdown")
    assert code == 1
    assert (
        "#### Fixes\n"
        "\n"
        f"{summary(4, 2, 1, 1, packages=2)}\n"
        "\n"
        "Upgrade: a newer official release has the wheel:\n"
        "\n"
        "- `fixme 1.0 -> 1.4`: win-arm64, win-amd64\n"
        "\n"
        "In `requirements.txt`, change the pins to `fixme==1.4`.\n"
        "\n"
        f"{fix.RESOLVE_NOTE}\n"
        "\n"
        "No fix yet:\n"
        "\n"
        "- `nowheels 0.9`: no newer release has a wheel for win-amd64\n"
        "\n"
        "#### Gap wheels\n"
        "\n"
        f"{gapwheels.FALLBACK_HEAD}\n"
        "\n"
        "- `fixme 1.0: win-arm64 (if you can't upgrade fixme)`\n"
    ) in out


def test_fix_and_suggest_look_each_package_up_once(capsys, fake_pypi, gap_wheels):
    _, out_suggest, _ = run(capsys, "fixme==1.0", *BOTH_WINDOWS, "--explain", "--suggest")
    fake_pypi.clear()
    _, out, _ = run(capsys, "fixme==1.0", *BOTH_WINDOWS, "--explain", "--suggest", "--fix")
    assert fake_pypi.count("https://pypi.org/pypi/fixme/json") == 1
    assert "fixme 1.0 -> 1.4 has wheels for win-arm64, win-amd64" in out_suggest
    # --suggest's own lines are unchanged; --explain --fix adds the upgrade under each target it fixes.
    assert set(without_fix_hint(out_suggest).splitlines()) <= set(out.splitlines())
    assert "  upgrade: fixme 1.4 has the wheel (an official release; see the --fix section)\n" in out


def test_fix_upgrade_for_hash_pinned_requirements_says_to_regenerate(capsys, fake_pypi, gap_wheels, tmp_path):
    reqs = _fixme_requirements(tmp_path, f"fixme==1.0 --hash=sha256:{'0' * 64}\n")
    _, out, _ = run(capsys, reqs, "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert (
        "Upgrade: a newer official release has the wheel:\n"
        "  fixme 1.0 -> 1.3: win-arm64\n"
        "requirements.txt pins hashes, so instead of editing the pins to fixme==1.3, regenerate it: "
        "pip-compile --upgrade-package fixme==1.3, or uv pip compile --upgrade-package fixme==1.3 with the options "
        "you made it with.\n"
    ) in out


def test_fix_says_whether_a_ranged_requirement_allows_the_upgrade(capsys, fake_pypi, gap_wheels, tmp_path):
    _, out, _ = run(capsys, _fixme_requirements(tmp_path, "fixme<1.3\n"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  fixme 1.2 -> 1.3: win-arm64 (needs requirements.txt's fixme<1.3 relaxed)\n" in out


def test_fix_upgrade_for_a_name_required_under_two_markers(capsys, fake_pypi, gap_wheels, tmp_path):
    # Its `unknown` stand-in is never looked up; the name is still pypi.org's, so --fix suggests the upgrade as it
    # does for one line (#835 review).
    path = _fixme_requirements(tmp_path, "fixme>=1.0\nfixme<1.3 ; sys_platform == 'win32'\n")
    _, out, err = run(capsys, path, "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  fixme 1.2 -> 1.3: win-arm64 (needs requirements.txt's fixme<1.3 relaxed)\n" in out
    assert "come from PyPI" not in out + err


def fixme_uv_lock(specifier):
    return (
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "fixme" }]\n\n'
        f'[package.metadata]\nrequires-dist = [{{ name = "fixme", specifier = "{specifier}" }}]\n\n'
        '[[package]]\nname = "fixme"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/fixme-1.0.tar.gz" }\n'
    )


@pytest.mark.parametrize(
    "specifier,said",
    [
        (">=1.0", "within your pyproject.toml constraint fixme>=1.0"),
        ("", "your pyproject.toml puts no version bound on it"),
        ("==1.0", "needs pyproject.toml's fixme==1.0 relaxed"),
    ],
)
def test_fix_upgrade_for_a_uv_lock(capsys, fake_pypi, gap_wheels, tmp_path, specifier, said):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0"\ndependencies = ["fixme{specifier}"]\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(fixme_uv_lock(specifier), encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path / "uv.lock"), *BOTH_WINDOWS, "--fix")
    assert code == 1
    assert f"  fixme 1.0 -> 1.4: win-arm64, win-amd64 ({said})\n" in out
    assert (
        f"In {tmp_path}, run uv lock --upgrade-package fixme, then uv sync: uv moves each to the newest release your "
        "constraints allow, so check that it's at least fixme==1.4, then run wheelgap again.\n"
    ) in out
    _, data, _ = gap_wheels_json(capsys, str(tmp_path / "uv.lock"), *BOTH_WINDOWS, "--fix")
    constraint = data["packages"][0]["fix"]["win-arm64"]["constraint"]
    assert constraint == {
        "file": "pyproject.toml",
        "requirements": [f"fixme{specifier}"] if specifier else [],
        "allows": "==" not in specifier,
        "via": "requirement",
    }


def test_fix_says_nothing_about_a_constraint_it_doesnt_read():
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    row = Row(Package("x", "1"), {target: Status.SOURCE})  # a transitive dependency: no requirement of the project
    cell = fix.Cell(row, target, Source("uv.lock", kind="uv", uv_packages=[]), fix.UPGRADE, version="2")
    assert fix.constraint(cell) is None
    lock = Source("poetry.lock", kind="poetry")
    assert fix.constraint(fix.Cell(Row(Package("x", "1", spec=">=1"), {}), target, lock, fix.UPGRADE, "2")) is None


@pytest.mark.parametrize(
    "kind,label,expected",
    [
        (
            "poetry",
            "poetry.lock",
            "Run poetry update x for poetry.lock: it stays within pyproject.toml's constraints and picks the newest "
            "release they allow, so check that it's at least x==2 (widen the constraint if not), then run wheelgap "
            "again.",
        ),
        ("pdm", "pdm.lock", "Run pdm update x for pdm.lock: it stays within pyproject.toml's constraints"),
        ("pylock", "pylock.toml", "Upgrade to x==2 with the tool that made pylock.toml, then lock again."),
        ("pipfile", "Pipfile.lock", "Upgrade to x==2 with the tool that made Pipfile.lock, then lock again."),
        ("conda-env", "environment.yml", "In environment.yml's pip: list, change the pins to x==2."),
        ("script", "example.py", "In example.py's # /// script block, change the requirements to x==2."),
        ("pyproject", "pyproject.toml", "In pyproject.toml, change the requirements so they allow x==2."),
        (None, "x==1", "Use x==2 instead."),
    ],
)
def test_fix_upgrade_how_per_input(kind, label, expected):
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    cell = fix.Cell(Row(Package("x", "1"), {target: Status.SOURCE}), target, Source(label, kind=kind), fix.UPGRADE, "2")
    assert fix.how(cell.source, [cell])[0].startswith(expected)


def test_fix_uv_script_lock_upgrade_command():
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    source = Source("example.py.lock", kind="uv", uv_script="tools/example.py")
    cell = fix.Cell(Row(Package("x", "1"), {target: Status.SOURCE}), target, source, fix.UPGRADE, "2")
    assert fix.how(source, [cell]) == [
        "For example.py's lock, run uv lock --script tools/example.py --upgrade-package x: uv moves each to the "
        "newest release your constraints allow, so check that it's at least x==2, then run wheelgap again."
    ]


def test_fix_private_package_gets_no_upgrade_lookup(capsys, fake_pypi, gap_wheels, tmp_path):
    reqs = _fixme_requirements(tmp_path, "--extra-index-url https://pkgs.example.com/simple\nfixme==1.0\n")
    code, out, _ = run(capsys, reqs, "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1 and not [url for url in fake_pypi if url.endswith("/fixme/json")]
    assert "->" not in out and f"\n{summary(1, 0, 0, 0, unchecked=1)}\n" in out
    assert "No fix yet" not in out  # not known to have no fix: it wasn't checked
    assert (
        "Newer releases of fixme weren't looked up: wheelgap can't tell they come from PyPI, so their names stay "
        "private (see how-it-works.md#pypi-origin).\n"
    ) in out


def test_fix_with_another_index_makes_no_upgrade_lookup(capsys, fake_pypi, fake_simple, gap_wheels):
    code, out, _ = run(capsys, "fixme==1.0", *BOTH_WINDOWS, "--fix", "--index-url", "https://pkgs.example.com/simple")
    assert code == 1 and not fake_pypi and "->" not in out
    assert f"\n{summary(2, 0, 0, 0, unchecked=2)}\n" in out
    assert (
        "Newer releases of fixme weren't looked up: packages may come from another index (--index-url), so no names "
        "went to pypi.org.\n"
    ) in out


def test_fix_no_binary_has_no_fix_and_no_lookup(capsys, fake_pypi, gap_wheels, tmp_path):
    reqs = _fixme_requirements(tmp_path, "--no-binary fixme\nfixme==1.0\n")
    _, out, _ = run(capsys, reqs, "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  fixme 1.0: --no-binary, so pip uses no wheel for it for win-arm64\n" in out
    assert not [url for url in fake_pypi if url.endswith("/fixme/json")]


def test_fix_names_each_release_when_no_one_release_covers_every_target(capsys, fake_pypi, gap_wheels):
    # laggard (tests/conftest.py): 1.1 has the Windows wheel, only 1.3 the macOS one, and 1.3 has no Windows wheel.
    _, out, _ = run(capsys, "laggard==1.0", "-p", "3.13", "-t", "win-amd64,macos-arm64", "--fix")
    assert "  laggard 1.0 -> 1.1: win-amd64\n  laggard 1.0 -> 1.3: macos-arm64\n" in out
    assert (
        "No one release of laggard has wheels for all of these targets (laggard==1.1 for win-amd64; laggard==1.3 for "
        "macos-arm64): pick the one for the targets you need.\n" in out
    )
    assert "Use laggard==" not in out  # one input can't pin both


def test_fix_never_offers_an_upgrade_that_drops_a_wheel(capsys, fake_pypi, gap_wheels):
    # wheellost 1.1 has the Windows wheel but drops 1.0's Linux one.
    _, out, _ = run(capsys, "wheellost==1.0", "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--fix")
    assert "->" not in out
    assert (
        "  wheellost 1.0: every newer release with a wheel drops one it has elsewhere for win-amd64 (1.1 has none for "
        "linux-x86_64)\n"
    ) in out
    assert "ask for a wheel" not in out  # upstream has one: asking doesn't help


def test_fix_says_when_only_abi3_wheels_block_free_threaded_python(capsys, fake_pypi, gap_wheels):
    code, out, _ = run(capsys, "abi3only==1.0", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1
    assert (
        "  abi3only 1.0: no newer release has a wheel for win-arm64; only abi3 wheels for win-arm64, which "
        "free-threaded Python can't use: ask abi3only for a cp314t wheel at https://github.com/example/abi3only/issues\n"
    ) in out
    _, data, _ = gap_wheels_json(capsys, "abi3only==1.0", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["packages"][0]["fix"]["win-arm64"] == {
        "kind": "none",
        "reason": "no_newer_release",
        "abi3_only": True,
        "build_packages": None,
    }
    _, out, _ = run(capsys, "nowheels==0.9", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "abi3" not in out  # only when that's the reason


def test_fix_annotation_leads_with_the_upgrade(capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _fixme_gap_wheels(gap_wheels)
    reqs = _fixme_requirements(tmp_path)
    _, out, _ = run(capsys, reqs, *BOTH_WINDOWS, "--fix", "--github-annotations")
    (line,) = [ln for ln in _annotations(out) if "fixme 1.0" in ln and ln.startswith("::error")]
    assert (
        "Upgrade: fixme 1.4 has the wheel for win-arm64, win-amd64 (an official release); the --fix section of the log "
        "says how. If you can't upgrade: a gap wheel on wheelgap's gap wheel index fills win-arm64"
    ) in line
    (other,) = [ln for ln in _annotations(out) if "nowheels 0.9" in ln]
    assert "Upgrade" not in other and " A gap wheel on wheelgap's gap wheel index fills win-arm64" in other


def test_fix_names_the_file_that_pins_the_package(capsys, fake_pypi, gap_wheels, tmp_path):
    (tmp_path / "base.txt").write_text("fixme==1.0\n", encoding="utf-8")
    (tmp_path / "top.txt").write_text("-r base.txt\n", encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "top.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "In base.txt, change the pins to fixme==1.3.\n" in out


def test_fix_says_a_constraints_file_pin_holds_the_upgrade_back(capsys, fake_pypi, gap_wheels, tmp_path):
    (tmp_path / "c.txt").write_text("fixme==1.0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c c.txt\nfixme\n", encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  fixme 1.0 -> 1.3: win-arm64 (needs fixme==1.0 in the -c constraints of requirements.txt relaxed)\n" in out
    assert "In requirements.txt, change the requirements to fixme==1.3.\n" in out  # its line is a bare name


def test_fix_counts_packages_not_looked_up_apart_in_json(capsys, fake_pypi, gap_wheels, tmp_path):
    reqs = _fixme_requirements(tmp_path, "--extra-index-url https://pkgs.example.com/simple\nfixme==1.0\n")
    _, data, _ = gap_wheels_json(capsys, reqs, *BOTH_WINDOWS, "--fix")
    assert data["fix_summary"] == {
        "gaps": 2,
        "upgrade": 0,
        "gap_wheel": 0,
        "none": 0,
        "not_looked_up": 2,
        "uv_may_fork": 0,
    }
    assert data["packages"][0]["fix"]["win-arm64"] == {
        "kind": "none",
        "reason": "not_looked_up",
        "abi3_only": False,
        "build_packages": None,
    }


def pyfork_uv_lock():
    return (
        'version = 1\nrequires-python = ">=3.13"\n\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "pyfork" }]\n\n'
        '[[package]]\nname = "pyfork"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/pyfork-1.0.tar.gz" }\n'
        'wheels = [{ url = "https://files.example/pyfork-1.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n'
    )


def test_fix_says_uv_may_fork_where_only_another_pythons_wheel_is_lost(capsys, fake_pypi, gap_wheels, tmp_path):
    # pyfork 2.0 has the 3.14 wheel but none for 3.13, which 1.0 has: pip would lose it, uv may fork per Python.
    (tmp_path / "uv.lock").write_text(pyfork_uv_lock(), encoding="utf-8")
    args = ["-p", "3.13,3.14", "-t", "linux-x86_64", "--fix"]
    code, out, _ = run(capsys, str(tmp_path / "uv.lock"), *args)
    assert code == 1
    assert (
        "Maybe, with uv:\n"
        "  pyfork 1.0: every newer release with a wheel drops one it has on another Python for 3.14 linux-x86_64 (2.0 "
        "has none for 3.13 linux-x86_64); uv may fork pyfork per Python: try uv lock --upgrade-package pyfork in "
        f"{tmp_path} and run wheelgap again\n"
    ) in out
    assert "No fix yet" not in out and "; 1 may be fixed by an upgrade uv locks per Python." in out
    _, data, _ = gap_wheels_json(capsys, str(tmp_path / "uv.lock"), *args)
    assert data["fix_summary"]["uv_may_fork"] == 1 and data["fix_summary"]["none"] == 0
    # pip has no forks: a requirements file pins one release for every Python.
    _, out, _ = run(capsys, "pyfork==1.0", *args)
    assert "No fix yet:\n  pyfork 1.0: every newer release with a wheel drops one it has elsewhere for 3.14" in out
    assert "uv may fork" not in out


def test_fix_shows_a_ranged_requirement_as_written(capsys, fake_pypi, gap_wheels, tmp_path):
    reqs = _fixme_requirements(tmp_path, "fixme>=1.0,<1.3\n")  # packaging's own order would be <1.3,>=1.0
    _, out, _ = run(capsys, reqs, "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  fixme 1.2 -> 1.3: win-arm64 (needs requirements.txt's fixme>=1.0,<1.3 relaxed)\n" in out
    assert "In requirements.txt, change the requirements to fixme==1.3.\n" in out  # a range, not a pin


def test_fix_gap_wheels_are_a_fallback_only_after_an_upgrade(capsys, fake_pypi, gap_wheels):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    _, out, _ = run(capsys, *NOWHEELS, "--fix")
    assert f"\n{gapwheels.HEAD}\n" in out and "As a fallback" not in out


def test_explain_fix_leads_with_the_upgrade_under_each_target(capsys, fake_pypi, gap_wheels, tmp_path):
    _fixme_gap_wheels(gap_wheels)
    _, out, _ = run(capsys, _fixme_requirements(tmp_path), *BOTH_WINDOWS, "--fix", "--explain")
    assert (
        "  upgrade: fixme 1.4 has the wheel (an official release; see the --fix section)\n"
        f"  if you can't upgrade, gap wheel for win-arm64: {gapwheels.UNOFFICIAL}\n"
    ) in out
    assert f"  gap wheel for win-arm64: {gapwheels.UNOFFICIAL}\n" in out  # nowheels: no upgrade, so no "if"


def test_fix_says_no_uv_fork_when_the_newer_release_still_admits_the_older_python(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch
):
    # 2.0 drops the 3.13 wheel but its Requires-Python admits 3.13: uv would build it there, so no fork helps.
    monkeypatch.setitem(
        FAKE_INDEX["pyfork"],
        "2.0",
        ["pyfork-2.0-cp314-cp314-manylinux_2_17_x86_64.whl", "pyfork-2.0.tar.gz"],
    )
    (tmp_path / "uv.lock").write_text(pyfork_uv_lock(), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13,3.14", "-t", "linux-x86_64", "--fix")
    assert "uv may fork" not in out
    assert "No fix yet:\n  pyfork 1.0: every newer release with a wheel drops one it has elsewhere for 3.14" in out


def test_fix_uv_may_fork_only_where_the_project_allows_the_release(capsys, fake_pypi, gap_wheels, tmp_path):
    lock = pyfork_uv_lock().replace(
        'dependencies = [{ name = "pyfork" }]\n',
        'dependencies = [{ name = "pyfork" }]\n\n[package.metadata]\n'
        'requires-dist = [{ name = "pyfork", specifier = "<2" }]\n',
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13,3.14", "-t", "linux-x86_64", "--fix")
    assert "uv may fork" not in out
    assert (
        "No fix yet:\n  pyfork 1.0: every newer release with a wheel drops one it has elsewhere for 3.14 linux-x86_64 "
        "(2.0 has none for 3.13 linux-x86_64) (pyproject.toml's pyfork<2 leaves out 2.0)\n"
    ) in out


def test_fix_uv_may_fork_names_the_script_for_a_script_lock(capsys, fake_pypi, gap_wheels, tmp_path):
    (tmp_path / "tool.py.lock").write_text(
        pyfork_uv_lock().replace(
            '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
            'dependencies = [{ name = "pyfork" }]\n\n',
            '[manifest]\nrequirements = [{ name = "pyfork" }]\n\n',
        ),
        encoding="utf-8",
    )
    _, out, _ = run(capsys, str(tmp_path / "tool.py.lock"), "-p", "3.13,3.14", "-t", "linux-x86_64", "--fix")
    assert f"try uv lock --script {tmp_path / 'tool.py'} --upgrade-package pyfork and run wheelgap again" in out


def test_fix_moves_a_pre_release_pin_to_a_final_release(capsys, fake_pypi, gap_wheels, monkeypatch):
    # grpcio 1.58.0rc1 got 1.59.0rc1 although 1.59.0 has the wheel (#825).
    arm = "cp314-cp314t-win_arm64"
    monkeypatch.setitem(
        FAKE_INDEX,
        "prepin",
        {
            "1.0rc1": ["prepin-1.0rc1.tar.gz"],
            "1.1rc1": [f"prepin-1.1rc1-{arm}.whl"],
            "1.1": [f"prepin-1.1-{arm}.whl"],
            "1.2rc1": ["prepin-1.2rc1-cp314-cp314t-win_amd64.whl"],
        },
    )
    _, out, _ = run(capsys, "prepin==1.0rc1", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert "  prepin 1.0rc1 -> 1.1: win-arm64\nUse prepin==1.1 instead.\n" in out
    _, data, _ = gap_wheels_json(capsys, "prepin==1.0rc1", "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["packages"][0]["fix"]["win-arm64"]["prerelease"] is False
    # No final release has the win-amd64 wheel: the pre-release is named, and said to be one.
    _, out, _ = run(capsys, "prepin==1.0rc1", "-p", "3.14t", "-t", "win-amd64", "--fix")
    assert "  prepin 1.0rc1 -> 1.2rc1 (a pre-release): win-amd64\n" in out
    _, out, _ = run(capsys, "prepin==1.0rc1", "-p", "3.14t", "-t", "win-amd64", "--fix", "--markdown")
    assert "- `prepin 1.0rc1 -> 1.2rc1` (a pre-release): win-amd64\n" in out
    _, data, _ = gap_wheels_json(capsys, "prepin==1.0rc1", "-p", "3.14t", "-t", "win-amd64", "--fix")
    assert data["packages"][0]["fix"]["win-amd64"] | {"how": None} == {
        "kind": "upgrade",
        "version": "1.2rc1",
        "prerelease": True,
        "exclude_newer": None,
        "how": None,
        "constraint": None,
        "gap_wheel": None,
    }


@pytest.mark.parametrize(
    "requires,said",
    [
        # #823: numpy<2 for Python < 3.13 and numpy>=1.26 for 3.13: only the second applies to a 3.13 target.
        (
            [("<1.3", "python_full_version < '3.14'"), (">=1.0", "python_full_version >= '3.14'")],
            " (within your pyproject.toml constraint fixme>=1.0)",
        ),
        ([("<1.3", "python_full_version >= '3.14'")], " (needs pyproject.toml's fixme<1.3 relaxed)"),
        ([("<1.3", "sys_platform == 'win32'"), (">=1.0", None)], " (needs pyproject.toml's fixme<1.3 relaxed)"),
        ([("<1.3", "extra == 'old'")], " (needs pyproject.toml's fixme<1.3 relaxed)"),  # uv locks every extra
        ([("<1.3", "sys_platform == 'linux'")], ""),  # no requirement applies on Windows: nothing to go by
        ([("<1.3", "platform_release == '6.1'")], ""),  # a marker the target can't decide: no answer
        ([("<1.3", "platform_release == '6.1'"), (">=1.0", None)], ""),
    ],
)
def test_fix_uv_lock_constraint_applies_only_where_its_marker_holds(
    capsys, fake_pypi, gap_wheels, tmp_path, requires, said
):
    dist = ", ".join(
        '{ name = "fixme"' + f', specifier = "{spec}"' + (f', marker = "{marker}"' if marker else "") + " }"
        for spec, marker in requires
    )
    lock = fixme_uv_lock(">=1.0").replace(
        'requires-dist = [{ name = "fixme", specifier = ">=1.0" }]', f"requires-dist = [{dist}]"
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert f"  fixme 1.0 -> 1.3: win-arm64{said}\n" in out
    _, data, _ = gap_wheels_json(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    constraint = data["packages"][0]["fix"]["win-arm64"]["constraint"]
    assert (constraint is None) == (not said)
    if constraint is not None:
        assert constraint["allows"] == ("within" in said)


def test_fix_uv_may_fork_when_the_blocking_requirement_doesnt_apply(capsys, fake_pypi, gap_wheels, tmp_path):
    # pyfork<2 only below Python 3.14: it doesn't hold 2.0 back on 3.14, so uv may still fork (#823).
    lock = pyfork_uv_lock().replace(
        'dependencies = [{ name = "pyfork" }]\n',
        'dependencies = [{ name = "pyfork" }]\n\n[package.metadata]\n'
        'requires-dist = [{ name = "pyfork", specifier = "<2", marker = "python_full_version < \'3.14\'" }]\n',
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13,3.14", "-t", "linux-x86_64", "--fix")
    assert "Maybe, with uv:\n  pyfork 1.0: " in out and "leaves out" not in out


CUTLIB = {
    "1.0": [{"filename": "cutlib-1.0.tar.gz", "upload_time_iso_8601": "2024-01-01T00:00:00Z"}],
    "1.1": [{"filename": "cutlib-1.1.tar.gz", "upload_time_iso_8601": "2024-07-01T00:00:00Z"}],
    "2.0": [
        {
            "filename": "cutlib-2.0-cp313-cp313-manylinux_2_17_x86_64.whl",
            "upload_time_iso_8601": "2024-08-18T10:00:00Z",
        },
        {"filename": "cutlib-2.0.tar.gz", "upload_time_iso_8601": "2024-08-18T12:00:00Z"},
    ],
}


def cutlib_uv_lock(options):
    return (
        f'version = 1\nrequires-python = ">=3.13"\n\n{options}\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "cutlib" }]\n\n'
        '[[package]]\nname = "cutlib"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/cutlib-1.0.tar.gz" }\n'
    )


@pytest.mark.parametrize(
    "options,setting",
    [
        ('[options]\nexclude-newer = "2024-08-01T00:00:00Z"\n', "exclude-newer"),
        (
            '[options]\nexclude-newer = "2030-01-01T00:00:00Z"\n\n'
            '[options.exclude-newer-package]\nCutLib = "2024-08-01T00:00:00Z"\n',
            "exclude-newer-package CutLib",
        ),
    ],
)
def test_fix_says_a_release_past_exclude_newer_needs_the_cutoff_moved(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch, options, setting
):
    # #822: 2.0 has the wheel but was uploaded after the lock's exclude-newer, so uv lock can't pick it as it stands.
    monkeypatch.setitem(FAKE_INDEX, "cutlib", CUTLIB)
    (tmp_path / "uv.lock").write_text(cutlib_uv_lock(options), encoding="utf-8")
    args = [str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--fix"]
    _, out, _ = run(capsys, *args)
    assert (
        f"  cutlib 1.0 -> 2.0: linux-x86_64 (needs uv's {setting} moved past 2024-08-18T12:00:00Z)\n"
        f"First move uv's {setting} (2024-08-01T00:00:00Z) past 2024-08-18T12:00:00Z: cutlib==2.0 was uploaded after "
        "its cutoff, so uv can't pick it before that.\n"
        f"In {tmp_path}, run uv lock --upgrade-package cutlib, then uv sync"
    ) in out
    _, data, _ = gap_wheels_json(capsys, *args)
    cell = data["packages"][0]["fix"]["linux-x86_64"]
    assert cell["version"] == "2.0" and cell["how"].startswith(f"First move uv's {setting} ")
    assert cell["exclude_newer"] == {
        "setting": setting,
        "cutoff": "2024-08-01T00:00:00Z",
        "span": None,
        "move_past": "2024-08-18T12:00:00Z",
    }


@pytest.mark.parametrize(
    "options",
    [
        "",
        '[options]\nexclude-newer = "2024-09-01T00:00:00Z"\n',
        '[options]\nexclude-newer = "2024-08-01T00:00:00Z"\n\n[options.exclude-newer-package]\ncutlib = false\n',
    ],
)
def test_fix_upgrade_within_exclude_newer_needs_no_move(capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch, options):
    monkeypatch.setitem(FAKE_INDEX, "cutlib", CUTLIB)
    (tmp_path / "uv.lock").write_text(cutlib_uv_lock(options), encoding="utf-8")
    args = [str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--fix"]
    _, out, _ = run(capsys, *args)
    assert "  cutlib 1.0 -> 2.0: linux-x86_64\n" in out and "First move" not in out
    _, data, _ = gap_wheels_json(capsys, *args)
    assert data["packages"][0]["fix"]["linux-x86_64"]["exclude_newer"] is None


@pytest.mark.parametrize(
    "options,setting",
    [
        ('[options]\nexclude-newer = "last week-ish"\n', "the exclude-newer"),
        ("[options.exclude-newer-package]\nCutLib = { since = 3 }\n", "the exclude-newer-package for CutLib"),
        ('[options]\nexclude-newer-span = "P3Q"\n', "the exclude-newer"),
    ],
)
def test_fix_looks_nothing_up_when_the_locks_exclude_newer_cant_be_read(
    capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch, options, setting
):
    monkeypatch.setitem(FAKE_INDEX, "cutlib", CUTLIB)
    (tmp_path / "uv.lock").write_text(cutlib_uv_lock(options), encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--fix")
    assert (
        f"Newer releases of cutlib weren't looked up: wheelgap can't read {setting} that uv recorded in uv.lock.\n"
        in out
    )
    assert not [url for url in fake_pypi if url.endswith("/cutlib/json")]


# numpy as PyPI has it, for the real locks in tests/fixtures/uvspan and uvspanpackage (uv 0.13.1, numpy pinned at
# 1.26.4, which has no cp313 wheel): 2.1.0 is the first release with one, uploaded after an 850-day cutoff.
SPAN_NUMPY = {
    "1.26.4": [{"filename": "numpy-1.26.4.tar.gz", "upload_time_iso_8601": "2024-02-06T00:26:44Z"}],
    "2.0.1": [{"filename": "numpy-2.0.1.tar.gz", "upload_time_iso_8601": "2024-07-21T13:58:00Z"}],
    "2.1.0": [
        {
            "filename": "numpy-2.1.0-cp313-cp313-manylinux_2_17_x86_64.whl",
            "upload_time_iso_8601": "2024-08-18T21:00:00Z",
        },
        {"filename": "numpy-2.1.0.tar.gz", "upload_time_iso_8601": "2024-08-18T22:13:47Z"},
    ],
}


@pytest.mark.parametrize(
    "folder,setting",
    [
        # [options] exclude-newer-span = "P850D", from exclude-newer = "850 days"
        ("uvspan", "exclude-newer"),
        # [options.exclude-newer-package] numpy = { timestamp = "0001-01-01T00:00:00Z", span = "P850D" }
        ("uvspanpackage", "exclude-newer-package numpy"),
    ],
)
def test_fix_reads_the_durations_uv_records_in_a_lock(
    capsys, fake_pypi, gap_wheels, fixtures, monkeypatch, folder, setting
):
    # A duration counts back from the time uv locks: the output names it, never the time it gives this run (#822).
    from datetime import UTC, datetime

    from wheelgap import cutoff

    monkeypatch.setitem(FAKE_INDEX, "numpy", SPAN_NUMPY)
    monkeypatch.setattr(cutoff, "current_time", lambda: datetime(2026, 11, 28, tzinfo=UTC))  # 850 days: 2024-07-31
    args = [str(fixtures / folder / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--fix"]
    _, out, _ = run(capsys, *args)
    assert (
        "  numpy 1.26.4 -> 2.1.0: linux-x86_64 (within your pyproject.toml constraint numpy>=1.26; "
        f"needs uv's {setting} span (P850D) shortened, or set to a time past 2024-08-18T22:13:47Z)\n"
        f"First shorten uv's {setting} span (P850D), or set it to a time past 2024-08-18T22:13:47Z: "
        "numpy==2.1.0 was uploaded after its cutoff, so uv can't pick it before that.\n"
    ) in out
    assert "2024-07-31" not in out
    _, data, _ = gap_wheels_json(capsys, *args)
    cell = data["packages"][0]["fix"]["linux-x86_64"]
    assert cell["exclude_newer"] == {
        "setting": setting,
        "cutoff": None,
        "span": "P850D",
        "move_past": "2024-08-18T22:13:47Z",
    }
    assert "2024-07-31" not in cell["how"]
    # A shorter duration reaches 2.1.0: within the cutoff, nothing to move.
    monkeypatch.setattr(cutoff, "current_time", lambda: datetime(2026, 12, 20, tzinfo=UTC))
    _, out, _ = run(capsys, *args)
    assert "  numpy 1.26.4 -> 2.1.0: linux-x86_64 (within your pyproject.toml constraint numpy>=1.26)\n" in out


def test_fix_on_a_pyproject_applies_its_exclude_newer(capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch):
    monkeypatch.setitem(FAKE_INDEX, "cutlib", CUTLIB)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\ndependencies = ["cutlib>=1.0"]\n\n'
        '[tool.uv]\nexclude-newer = "2024-08-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    _, out, _ = run(capsys, str(tmp_path / "pyproject.toml"), "-p", "3.13", "-t", "linux-x86_64", "--fix")
    assert (
        "  cutlib 1.1 -> 2.0: linux-x86_64 (within your pyproject.toml constraint cutlib>=1.0; needs uv's "
        "exclude-newer moved past 2024-08-18T12:00:00Z)\n"
        "First move uv's exclude-newer (2024-08-01T00:00:00Z) past 2024-08-18T12:00:00Z:"
    ) in out
    assert "change the requirements" not in out  # its range already allows 2.0: only the cutoff holds it back


@pytest.mark.parametrize("order", [1, -1])
def test_fix_constraint_note_is_per_target(capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch, order):
    # permark<2 on Windows, permark>=1 elsewhere: one note for both targets was right for one of them only (#823).
    monkeypatch.setitem(
        FAKE_INDEX,
        "permark",
        {
            "1.0": ["permark-1.0.tar.gz"],
            "2.0": ["permark-2.0-cp314-cp314t-win_arm64.whl", "permark-2.0-cp314-cp314t-musllinux_1_2_x86_64.whl"],
        },
    )
    dist = [
        '{ name = "permark", specifier = "<2", marker = "sys_platform == \'win32\'" }',
        '{ name = "permark", specifier = ">=1", marker = "sys_platform != \'win32\'" }',
    ][::order]
    lock = fixme_uv_lock(">=1.0").replace("fixme", "permark")
    lock = lock.replace(
        'requires-dist = [{ name = "permark", specifier = ">=1.0" }]', f"requires-dist = [{', '.join(dist)}]"
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    targets = ",".join(["musl-x86_64", "win-arm64"][::order])
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.14t", "-t", targets, "--fix")
    assert "  permark 1.0 -> 2.0: musl-x86_64 (within your pyproject.toml constraint permark>=1)\n" in out
    assert "  permark 1.0 -> 2.0: win-arm64 (needs pyproject.toml's permark<2 relaxed)\n" in out


def test_fix_says_first_move_once_for_every_cutoff_setting(capsys, fake_pypi, gap_wheels, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from wheelgap import cutoff

    monkeypatch.setitem(FAKE_INDEX, "cutlib", CUTLIB)
    monkeypatch.setitem(
        FAKE_INDEX,
        "cutlib2",
        {v: [{**f, "filename": f["filename"].replace("cutlib", "cutlib2")} for f in fs] for v, fs in CUTLIB.items()},
    )
    monkeypatch.setattr(cutoff, "current_time", lambda: datetime(2024, 8, 10, tzinfo=UTC))
    lock = cutlib_uv_lock(
        '[options]\nexclude-newer = "2024-08-01T00:00:00Z"\n\n'
        '[options.exclude-newer-package]\ncutlib2 = { timestamp = "0001-01-01T00:00:00Z", span = "P3D" }\n'
    )
    lock = lock.replace(
        'dependencies = [{ name = "cutlib" }]', 'dependencies = [{ name = "cutlib" }, { name = "cutlib2" }]'
    )
    lock += lock[lock.index('[[package]]\nname = "cutlib"') :].replace("cutlib", "cutlib2")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    _, out, _ = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64", "--fix")
    assert out.count("First ") == 1
    assert (
        "First move uv's exclude-newer (2024-08-01T00:00:00Z) past 2024-08-18T12:00:00Z, and shorten uv's "
        "exclude-newer-package cutlib2 span (P3D), or set it to a time past 2024-08-18T12:00:00Z: cutlib==2.0, "
        "cutlib2==2.0 were uploaded after their cutoff, so uv can't pick them before that.\n"
    ) in out
    assert "2024-08-07" not in out  # the time P3D gives this run


@pytest.mark.parametrize(
    "line,what", [("x", "requirements"), ("x>=1", "requirements"), ("x==1", "pins"), ("x==1.*", "requirements")]
)
def test_fix_deps_says_pins_only_for_a_line_that_pins(line, what):
    # --deps resolves every line to one release; a bare name or a range is still not a pin to change.
    target = build_targets(["3.14t"], ["win-arm64"])[0]
    source = Source("requirements.txt", kind="requirements", specs=[line], deps_paths={target: {}})
    cell = fix.Cell(Row(Package("x", "1"), {target: Status.SOURCE}), target, source, fix.UPGRADE, "2")
    assert fix.how(source, [cell]) == [f"In requirements.txt, change the {what} to x==2."]
