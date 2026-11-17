"""--fix: which gaps a wheel on wheelgap's gap wheel index fills (wheelgap.gapwheels), from the fake list in
conftest.py (the autouse gap_wheels fixture): never the network."""

import json
import urllib.error

import pytest
from test_cli import _assert_matches_output_schema, run

from wheelgap import gapwheels, pypi
from wheelgap.check import Row, Status
from wheelgap.sources import Package, Source
from wheelgap.targets import build_targets

INDEX = "https://wheelgap.github.io/wheels/simple/"
DOWNLOAD = "https://github.com/wheelgap/wheels/releases/download/gap-wheels/"
FT_WHEEL = "nowheels-0.9-cp314-cp314t-win_arm64.whl"
NOWHEELS = ["nowheels==0.9", "-p", "3.14t", "-t", "win-arm64"]
NOTHING_FITS = "wheelgap: note: --fix: no wheel on the gap wheel index fills these gaps"
UNREADABLE = "wheelgap: note: couldn't read the gap wheel list, so --fix has no suggestions"


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
        "wheelgap's gap wheel index has wheels PyPI lacks for:\n"
        "  nowheels 0.9: win-arm64\n"
        "\n"
        "To install them with pip, add this line to your requirements file:\n"
        f"  --extra-index-url {INDEX}\n"
        "or pass it to pip install:\n"
        f"  pip install --extra-index-url {INDEX} ...\n"
        "If the requirements pin hashes, also add each wheel's hash to its line:\n"
        f"  nowheels==0.9 --hash=sha256:{'ab' * 32}\n"
        f"{gapwheels.PIP_CAVEAT}\n"
        "\n"
        "How these wheels are built and checked: https://wheelgap.github.io/wheels/\n"
    )
    assert out == out_plain + section  # after the normal output, which is unchanged
    assert "[tool.uv" not in out


def test_pip_way_for_a_requirements_file(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert code == 1 and "  nowheels 0.9: win-arm64\n" in out and f"  --extra-index-url {INDEX}\n" in out


def test_uv_way_for_a_uv_lock_with_direct_and_transitive_packages(capsys, fixtures, gap_wheels):
    # puresdist is a direct dependency of the uvproj project; nativeext comes in through fastlib.
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    gap_wheels.add("nativeext", "0.3", "nativeext-0.3-cp314-cp314t-win_arm64.whl")
    args = [str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64"]
    code_plain, out_plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--fix")
    assert code == code_plain == 1
    assert out.removeprefix(out_plain) == (
        "\n"
        "wheelgap's gap wheel index has wheels PyPI lacks for:\n"
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
    assert {k: v for k, v in data.items() if k != "gap_wheels"} == plain  # nothing else changes


def test_markdown_section(capsys, fixtures, gap_wheels):
    gap_wheels.add("puresdist", "1.0", "puresdist-1.0-cp314-cp314t-win_arm64.whl")
    _, out, _ = run(
        capsys, str(fixtures / "uvproj" / "uv.lock"), "-p", "3.14t", "-t", "win-arm64", "--fix", "--markdown"
    )
    assert "#### Gap wheels\n" in out and "- `puresdist 1.0: win-arm64`\n" in out
    assert '```toml\n[[tool.uv.index]]\nname = "wheelgap"\n' in out and 'puresdist = { index = "wheelgap" }\n```' in out
    _, out, _ = run(capsys, *NOWHEELS, "--markdown")
    assert "Gap wheels" not in out


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
    assert code == code_plain == 1 and out == out_plain
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
    assert sum(line.startswith("  x==1 --hash=") for line in lines) == 1


def test_no_binary_package_isnt_blamed_on_its_origin(capsys, fake_pypi, gap_wheels, tmp_path):
    gap_wheels.add("nowheels", "0.9", FT_WHEEL)
    req = tmp_path / "requirements.txt"
    req.write_text("--no-binary nowheels\nnowheels==0.9\n", encoding="utf-8")
    _, data, err = gap_wheels_json(capsys, str(req), "-p", "3.14t", "-t", "win-arm64", "--fix")
    assert data["gap_wheels"] == [] and "can't tell they come from PyPI" not in err


def test_lock_advice_carries_the_private_index_caveat():
    assert gapwheels.LOCK_CAVEAT in gapwheels.text_lines([_fill("poetry", "poetry.lock")], False)
