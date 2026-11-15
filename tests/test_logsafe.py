"""Text from inputs must never run as a GitHub Actions workflow command in a CI log."""

import io
import json
import os
import random
import re
import subprocess
import sys

import pytest

import wheelgap.logsafe
from wheelgap import cli
from wheelgap.cli import main
from wheelgap.diff import main as diff_main
from wheelgap.logsafe import CommandGuard, guarded_output, neutralize, write_command

LEADING_COMMAND = re.compile(r"(?:\s|\ufeff|\x1b\[[0-9;]*[A-Za-z])*::")


def commands(text):
    """Lines the Actions runner would read as workflow commands: `::` first (after whitespace or a BOM, which .NET
    skips; colour codes too, to be safe) or `##[` anywhere. Lines end at LF, CR or CRLF, as the runner reads them."""
    return [line for line in re.split(r"\r\n|\r|\n", text) if LEADING_COMMAND.match(line) or "##[" in line]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


TOML_LOCKS = {
    "uv.lock": 'version = 1\n\n[[package]]\nname = {name}\nversion = "1.0"\n'
    'source = {{ registry = "https://pypi.org/simple" }}\n',
    "poetry.lock": '[[package]]\nname = {name}\nversion = "1.0"\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
    "pdm.lock": '[metadata]\ngroups = ["default"]\n\n[[package]]\nname = {name}\nversion = "1.0"\nfiles = []\n',
    "pylock.toml": 'lock-version = "1.0"\ncreated-by = "test"\n\n[[packages]]\nname = {name}\nversion = "1.0"\n',
}


@pytest.mark.parametrize(
    "name",
    [
        "::warning file=a,line=1::pwned",
        "x\n::error::pwn",
        "a ##[error]b",
        "x_",
        "",
        "\u017fix",  # long s and the Kelvin sign match [a-z] case-insensitively unless the pattern is ASCII-only
        "\u212aelvin",
    ],
)
@pytest.mark.parametrize("lockfile", [*TOML_LOCKS, "Pipfile.lock"])
def test_invalid_package_names_are_input_errors(capsys, fake_pypi, tmp_path, lockfile, name):
    # pip can't install a name that isn't PEP 508, so it's bad input (exit 2), not a package with a gap.
    if lockfile == "Pipfile.lock":
        text = json.dumps({"default": {name: {"version": "==1.0"}}})
    else:
        text = TOML_LOCKS[lockfile].format(name=json.dumps(name))  # a JSON string is a TOML basic string
    (tmp_path / lockfile).write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / lockfile), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    message = f"{lockfile}: a package has the name {name!r}, which isn't a valid package name (PEP 508)"
    assert neutralize(message) in err
    assert not commands(out + err)
    assert fake_pypi == []


def test_missing_package_name_is_an_input_error(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nversion = "1.0"\n', encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "uv.lock: a package has no name" in err


VERSION = "1.0\n::error::pwn \r::add-mask::x ##[error]legacy"
UV_LOCK = (
    'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
    'source = {{ editable = "." }}\ndependencies = [{{ name = "b" }}]\n\n'
    '[[package]]\nname = "b"\nversion = {version}\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    'sdist = {{ url = "https://files.example/b-1.0.tar.gz", hash = "sha256:00" }}\n'
)


@pytest.mark.parametrize(
    "flags", [[], ["--explain"], ["--all"], ["-q"], ["--markdown", "--explain"], ["--json", "--explain"]]
)
@pytest.mark.parametrize("color", ["always", "never"])
def test_lockfile_text_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, flags, color):
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version=json.dumps(VERSION)), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--color", color, *flags)
    assert code == 1
    if "-q" not in flags:  # which shows no versions
        assert "pwn" in out  # still shown, just not as a command
    assert not commands(out + err)
    if "--json" in flags:  # the escapes are JSON's own, so the value is unchanged
        assert json.loads(out)["packages"][0]["version"] == VERSION


@pytest.mark.parametrize("entry", [main, diff_main])
def test_diff_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, entry):
    (tmp_path / "old").mkdir()
    wheel = 'wheels = [{ url = "https://files.example/b-1.0-py3-none-any.whl", hash = "sha256:00" }]\n'
    (tmp_path / "old" / "uv.lock").write_text(UV_LOCK.format(version='"1.0"') + wheel, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version=json.dumps(VERSION)), encoding="utf-8")
    argv = [str(tmp_path / "old"), str(tmp_path), "-p", "3.13", "-t", "linux-x86_64"]
    code = entry(["diff", *argv] if entry is main else argv)  # wheelgap diff, and python -m wheelgap.diff
    out, err = capsys.readouterr()
    assert code == 1
    assert "pwn" in out + err
    assert not commands(out + err)


@pytest.mark.parametrize(
    "line,where",
    [
        ("./##[error]path\nnowheels==0.9", "out"),  # a path requirement: shown in the skipped list
        ("-e ./x\n::warning::y", "err"),  # the second line isn't a requirement pip can parse
        ("nowheels[##[error]x]", "err"),  # quoted in the parse error
    ],
)
def test_requirements_text_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, line, where):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    _, out, err = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64")
    assert "u0023" in (out if where == "out" else err) or "u003a" in (out if where == "out" else err)
    assert not commands(out + err)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("::error::x", "\\u003a:error::x"),
        ("  ::error::x", "  \\u003a:error::x"),
        ("\x1b[1m::error::x\x1b[0m", "\x1b[1m\\u003a:error::x\x1b[0m"),
        ("a ##[error]b ##[warning]c", "a \\u0023#[error]b \\u0023#[warning]c"),
        ('  "version": "##[x"', '  "version": "\\u0023#[x"'),  # still valid JSON, with the same value
        # What .NET's TrimStart skips (tab, VT, FF, NEL, NBSP, em space, line separator, ideographic space), and a BOM.
        *[(f"{ws}::error::x", f"{ws}\\u003a:error::x") for ws in "\t\v\f\x85\xa0\u2003\u2028\u3000\ufeff"],
        # Normal output is never changed.
        ("numpy  2.3.4  ✓  ● source", "numpy  2.3.4  ✓  ● source"),
        ("  via: a → b", "  via: a → b"),
        ("a: b::c # [x]", "a: b::c # [x]"),
        ("", ""),
    ],
)
def test_neutralize(line, expected):
    assert neutralize(line) == expected
    assert not commands(neutralize(line))


def test_command_guard_holds_partial_lines():
    raw = io.StringIO()
    guard = CommandGuard(raw)
    guard.write(":")
    guard.flush()  # a partial line stays held: the rest may make it a command
    assert raw.getvalue() == ""
    guard.write(":error::x\r")  # written at once: a CRLF split here only leaves an empty "line" between CR and LF
    guard.write("\n#")
    guard.write("#[warning]y\rok\n::tail")
    assert raw.getvalue() == "\\u003a:error::x\r\n\\u0023#[warning]y\rok\n"
    guard.finish()
    assert raw.getvalue().endswith("ok\n\\u003a:tail")
    assert not commands(raw.getvalue())


def test_guarded_output_restores_streams_and_nests(capsys):
    out, err = sys.stdout, sys.stderr
    with guarded_output():
        guard, err_guard = sys.stdout, sys.stderr
        assert isinstance(guard, CommandGuard) and guard.isatty() == out.isatty()
        assert isinstance(err_guard, CommandGuard) and err_guard.raw is err
        with guarded_output():
            assert (sys.stdout, sys.stderr) == (guard, err_guard)  # not wrapped twice
        print("::error::x")
        print("partial", end="")
    assert (sys.stdout, sys.stderr) == (out, err)
    assert capsys.readouterr().out == "\\u003a:error::x\npartial"


def test_write_command_passes_the_guard(capsys):
    with guarded_output():
        print("::error::from a lockfile")
        write_command("::error file=uv.lock,line=3::from wheelgap", sys.stdout)
    assert capsys.readouterr().out == "\\u003a:error::from a lockfile\n::error file=uv.lock,line=3::from wheelgap\n"


def test_write_command_ends_a_held_line_first(capsys):
    with guarded_output():
        print("::partial", end="")
        write_command("::warning::x", sys.stdout)
    assert capsys.readouterr().out == "\\u003a:partial\n::warning::x\n"


@pytest.mark.parametrize("command", ["::error::a\n::error::b", "::error::a\rb", "x ::error::a", ""])
def test_write_command_takes_one_command_line(command):
    with pytest.raises(ValueError):
        write_command(command, io.StringIO())


ANNOTATED_LOCK = UV_LOCK + (
    '\n[[package]]\nname = "c"\nversion = "1.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    'wheels = [{{ url = "https://files.example/c-1.0-py3-none-manylinux_2_17_x86_64.whl", hash = "sha256:00" }}]\n'
)


@pytest.mark.parametrize("flags", [[], ["--explain"], ["--json"], ["--markdown"]])
def test_github_annotations_pass_the_guard(capsys, fake_pypi, tmp_path, monkeypatch, flags):
    # wheelgap's own ::error / ::warning lines come out as commands; the lockfile's text never does.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    lock = ANNOTATED_LOCK.format(version=json.dumps(VERSION)).replace(
        '{ name = "b" }', '{ name = "b" }, { name = "c" }'
    )
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    argv = [str(tmp_path), "-p", "3.13", "-t", "linux-x86_64,win-arm64", "--advisory", "win-arm64"]
    code, out, err = run(capsys, *argv, "--github-annotations", *flags)
    assert code == 1
    escaped = "1.0%0A::error::pwn %0D::add-mask::x ##[error]legacy"  # by workflow_command, once
    assert commands(out + err) == [
        f"::error file=uv.lock,line=11,title=b {escaped.replace(':', '%3A')}%3A builds from source::"
        f"b {escaped} builds from source on linux-x86_64, win-arm64 (advisory).",
        "::warning file=uv.lock,line=17,title=c 1.0%3A can't install::"
        "c 1.0 can't install on win-arm64 (advisory). Gaps on advisory targets don't fail the run.",
    ]
    assert "pwn" in (out if "--json" in flags else out.split("\n", 2)[2])  # the injected text, shown but escaped
    if "--json" in flags:
        assert json.loads(out)["packages"][0]["version"] == VERSION


@pytest.mark.parametrize("where", ["config", "cli"])
def test_advisory_values_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, where):
    # A PR can edit [tool.wheelgap] advisory = [...]; the error quoting it must not be a command either.
    value = "x\n::error::pwn ##[error]y"
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version='"1.0"'), encoding="utf-8")
    config = f"advisory = [{json.dumps(value)}]\n" if where == "config" else ""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "0.1.0"\n\n[tool.wheelgap]\n{config}', encoding="utf-8"
    )
    flags = ["--advisory", value] if where == "cli" else []
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", *flags)
    assert code == 2
    assert "unknown target" in err
    assert not commands(out + err)


def test_command_guard_many_small_writes():
    raw = io.StringIO()
    guard = CommandGuard(raw)
    for _ in range(200_000):  # quadratic string building would take minutes
        guard.write(":")
    guard.write("\n")
    assert raw.getvalue() == "\\u003a" + ":" * 199_999 + "\n"


@pytest.mark.parametrize("message", ["bad\n::error::x", "bad ##[error]x"])
def test_tracebacks_go_through_the_guard(capsys, monkeypatch, message):
    # Left to the interpreter, a traceback is printed after the guard has gone, and quotes the message.
    def broken():
        raise ValueError(message)

    monkeypatch.setattr(cli, "make_parser", broken)
    with pytest.raises(SystemExit) as caught:
        main(["x"])
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert "Traceback" in err and "ValueError: bad" in err
    assert not commands(out + err)


def test_exit_messages_go_through_the_guard(capsys, monkeypatch):
    def leaving():
        sys.exit("bad\n::error::x")

    monkeypatch.setattr(cli, "make_parser", leaving)
    with pytest.raises(SystemExit) as caught:
        main(["x"])
    assert caught.value.code == 1
    assert capsys.readouterr().err == "bad\n\\u003a:error::x\n"


@pytest.mark.parametrize(
    "lockfile,text",
    [
        ("uv.lock", 'version = 1\n[[package]]\nname = "a"\nversion = 1\n'),
        ("poetry.lock", '[[package]]\nname = "a"\nversion = 1\nfiles = []\n[metadata]\nlock-version = "2.1"\n'),
        ("pdm.lock", '[metadata]\ngroups = ["default"]\n[[package]]\nname = "a"\nversion = 1\nfiles = []\n'),
        ("pdm.lock", '[[package]]\nname = "a"\nversion = "1"\nmarker = ["x"]\nfiles = []\n'),
        ("pylock.toml", 'lock-version = "1.0"\n[[packages]]\nname = "a"\nversion = 1\n'),
        ("pylock.toml", 'lock-version = "1.0"\n[[packages]]\nname = "a"\nversion = "1"\nmarker = 3\n'),
    ],
)
def test_fields_of_the_wrong_type_are_input_errors(capsys, fake_pypi, tmp_path, lockfile, text):
    (tmp_path / lockfile).write_text(text, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / lockfile), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert f"wheelgap: error: {lockfile}: package a has a " in err and "that isn't a string" in err


@pytest.mark.parametrize("encoding,lead", [("latin-1", "\xc2\xa0"), ("cp1252", "\xc2\u2026")])
def test_output_is_utf8_whatever_pythonioencoding_says(tmp_path, encoding, lead):
    # In latin-1, "Â" + NBSP is C2 A0: UTF-8 for NBSP, which the runner skips like a space, and then `::` starts the
    # line. In cp1252, "Â…" is C2 85: UTF-8 for NEL, another such space.
    version = f"1.0\n{lead}::error::pwn"
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version=json.dumps(version)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "NO_COLOR")} | {"PYTHONIOENCODING": encoding}
    argv = [str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--offline", "--no-cache", "--explain"]
    proc = subprocess.run([sys.executable, "-m", "wheelgap", *argv], capture_output=True, env=env, check=False)
    out, err = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 1, err
    assert version in out  # UTF-8, so the version comes back whole
    assert not commands(out + err)


def test_guarded_output_guards_stderr_even_when_stdout_already_is(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdout", CommandGuard(sys.stdout))
    with guarded_output():
        print("::error::x", file=sys.stderr)
    assert capsys.readouterr().err == "\\u003a:error::x\n"


# A colon, a colour code, a colon: the visible text starts with `::`, but there's no `::` in the string. neutralize
# used to look for one there and raise, and the traceback quoting the line then printed after the guard was gone.
SPLIT_COLONS = ":\x1b[0m:##[add-mask]hunter2"


def wheelgap_subprocess(*argv):
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "NO_COLOR")}
    proc = subprocess.run([sys.executable, *argv], capture_output=True, env=env, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("entry", ["wheelgap", "wheelgap.diff"])
@pytest.mark.parametrize("input_name", ["requirements.txt", "pyproject.toml"])
def test_colons_split_by_colour_codes(tmp_path, entry, input_name):
    if input_name == "requirements.txt":
        text = SPLIT_COLONS + "\n"
    else:
        dependency = json.dumps(":\x1b[0m:##[error]pwned")  # a JSON string is a TOML basic string (\u001b)
        text = f'[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = [{dependency}]\n'
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / input_name).write_text(text, encoding="utf-8")
    (tmp_path / "old.txt").write_text("", encoding="utf-8")
    new = str(tmp_path / "new" / input_name)
    if entry == "wheelgap":
        argv = [new, "-p", "3.13", "-t", "linux-x86_64", "--offline", "--no-cache", "--github-annotations"]
    else:
        argv = [str(tmp_path / "old.txt"), new, "-p", "3.13", "-t", "linux-x86_64"]
    code, out, err = wheelgap_subprocess("-m", entry, *argv)
    assert code == 2, err
    assert "Traceback" not in err and "wheelgap: error: " in err
    assert not commands(out + err)


ALPHABET = [
    ":",
    "::",
    "#",
    "##[",
    "[",
    "]",
    "\x1b",
    "\x1b[",
    "\x1b[0m",
    "\x1b[1;31m",
    " ",
    "\t",
    "\xa0",
    "\x85",
    "\u3000",
]
ALPHABET += ["\ufeff", "\r", "\n", "\r\n", "a", "0", ";", "m", "%"]


def test_neutralize_never_raises_or_leaves_a_command():
    rng = random.Random(247)
    for _ in range(5000):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))
        for line in re.split(r"\r\n|\r|\n", text):
            result = neutralize(line)
            assert isinstance(result, str)
            assert not commands(result), repr(line)
        raw = io.StringIO()  # the same text through the guard, in random pieces
        guard = CommandGuard(raw)
        cuts = sorted(rng.sample(range(len(text) + 1), min(3, len(text) + 1)))
        for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True):
            guard.write(text[start:end])
        guard.finish()
        assert not commands(raw.getvalue()), repr(text)


def test_an_error_in_the_guard_prints_a_fixed_message(capsys, fake_pypi, tmp_path, monkeypatch):
    # If the guard itself fails, the error quoting the text mustn't be printed: not by the guard, which just failed,
    # and not by the interpreter after the guard is gone.
    def broken(line):
        raise RuntimeError(f"can't neutralize {line}")

    monkeypatch.setattr(wheelgap.logsafe, "neutralize", broken)
    (tmp_path / "requirements.txt").write_text(SPLIT_COLONS + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        main([str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64"])
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert (out, err) == ("", "wheelgap: error: an internal error occurred while writing output\n")


@pytest.mark.parametrize("name", ["::warning file=a,line=1::pwned", "a ##[error]b", "x_", "\u212aelvin", "pure'wheel"])
def test_invalid_pixi_pypi_names_are_input_errors(capsys, fake_pypi, fixtures, tmp_path, name):
    lock = (fixtures / "pixiproj" / "pixi.lock").read_text(encoding="utf-8")
    assert lock.count("  name: winonly\n") == 1
    quoted = "'" + name.replace("'", "''") + "'"
    (tmp_path / "pixi.lock").write_text(lock.replace("  name: winonly\n", f"  name: {quoted}\n"), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "pixi.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    message = f"pixi.lock: a package has the name {name!r}, which isn't a valid package name (PEP 508)"
    assert neutralize(message) in err
    assert not commands(out + err)
