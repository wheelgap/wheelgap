"""Turn the final stage of a Dockerfile into a wheelgap target, following named stages back to an image."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NoReturn

from .targets import ALPINE_MUSL, DEBIAN_GLIBC

# Debian release an official python X.Y tag that names none (python:3.13, python:3.13-slim) is built on, per Python.
# Checked on Docker Hub 2026-11-10: 3.9 to 3.15 point at trixie; 3.7 and 3.8 stopped on bookworm, 3.6 on bullseye.
UNTAGGED_DEBIAN = {"3.6": "bullseye", "3.7": "bookworm", "3.8": "bookworm"} | {f"3.{n}": "trixie" for n in range(9, 16)}
# A patch or pre-release tag (python:3.12.7-slim) stops being rebuilt once the next one ships, so it can still be on
# an older release than its X.Y tag (3.12.7-slim is bookworm): those keep the --glibc default.
_MINOR_TAG_RE = re.compile(r"^\d+\.\d+(?:-|$)", re.ASCII)
# The Debian release each X.Y-rc tag that stopped being rebuilt at X.Y's release is on (python:3.11-rc-slim is
# bullseye), from Docker Hub on 2026-11-14: each -rc-slim digest matches its -rc-slim-<release> one. Older -rc tags are
# on releases with no glibc here (3.9-rc is buster), and a still-built one (3.16-rc) keeps the --glibc default.
RC_DEBIAN = {"3.10": "bullseye", "3.11": "bullseye", "3.12": "bookworm", "3.13": "bookworm"} | {
    "3.14": "trixie",
    "3.15": "trixie",
}
_ALPINE_RE = re.compile(r"^alpine(\d+)\.(\d+)", re.ASCII)
# docker --platform -> wheelgap arch
ARCHES = {
    "linux/amd64": "x86_64",
    "linux/x86_64": "x86_64",
    "linux/arm64": "aarch64",
    "linux/arm64/v8": "aarch64",
    "linux/aarch64": "aarch64",
    "linux/arm/v7": "armv7l",
    "linux/arm": "armv7l",
    "linux/ppc64le": "ppc64le",
    "linux/s390x": "s390x",
    "linux/riscv64": "riscv64",
    "linux/386": "i686",
    "linux/i386": "i686",  # containerd normalises it to linux/386
}
WINDOWS_VARIANTS = ("windowsservercore", "nanoserver")
# splitDockerDomain maps index.docker.io to docker.io before adding the official library/ prefix.
# https://github.com/distribution/reference/blob/v0.6.0/normalize.go#L123-L170
IMAGE_NAMES = (
    "python",
    "library/python",
    "docker.io/library/python",
    "docker.io/python",
    "index.docker.io/library/python",
    "index.docker.io/python",
)
UV_IMAGE = "ghcr.io/astral-sh/uv"
UV_ARCHES = {"x86_64", "aarch64"}
# Arches the official python images are published for, by Debian release (trixie and alpine have them all).
# Checked on Docker Hub 2026-11-10 for the X.Y tags (python:3.13-bookworm, python:3.13-slim-bullseye), and
# 2026-11-11 for linux/386.
DEBIAN_ARCHES = {
    "bookworm": {"x86_64", "aarch64", "armv7l", "ppc64le", "i686"},
    "bullseye": {"x86_64", "aarch64", "armv7l", "i686"},
}
# Images that stopped being rebuilt before Docker Hub dropped ppc64le/s390x still have them: patch and pre-release
# tags (python:3.12.7-bookworm, python:3.11.0-bullseye) and these end-of-life X.Y tags. riscv64 was never built for
# either release.
OLD_DEBIAN_ARCHES = {"x86_64", "aarch64", "armv7l", "ppc64le", "s390x", "i686"}
OLD_DEBIAN_TAGS = {
    ("3.6", "bullseye"),
    ("3.7", "bullseye"),
    ("3.7", "bookworm"),
    ("3.8", "bookworm"),
    ("3.9", "bookworm"),
}

# X.Y, X.Y.Z or a pre-release (3.15.0a1, 3.13rc1), then the variant; or the official images' floating X.Y-rc tags
# (python:3.16-rc, 3.16-rc-slim-trixie), which follow the newest pre-release of X.Y. Astral's uv images have had
# them too (uv:python3.14-rc-alpine, python3.14-rc-bookworm-slim).
_TAG_RE = re.compile(r"^(\d+)\.(\d+)(?:(-rc)(?=-|$)|(?:\.\d+)?(?:(?:a|b|rc)\d+)?)(?:-(.+))?$", re.ASCII)
_UV_TAG_PREFIX_RE = re.compile(r"^(?:\d+\.\d+(?:\.\d+)?-)?python", re.ASCII)
_DOCKER_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")  # distribution/reference's tag grammar


class DockerfileError(ValueError):
    pass


@dataclass(frozen=True)
class ImageTarget:
    python: str
    platform: str
    glibc: str | None = None
    musl: str | None = None
    # The global ARG defaults the final image's FROM used, as (name, value): a --build-arg can change them.
    build_args: tuple[tuple[str, str], ...] = field(default=(), compare=False)


_DIRECTIVE_RE = re.compile(r"^#\s*([A-Za-z]+)\s*=\s*(.+?)\s*$")
_DIRECTIVES = {"syntax", "escape", "check"}  # the parser directives BuildKit knows
# DirectiveParser's directiveRegexp (parser/directives.go), with Go's \s: [\t\n\f\r ], no \v.
_SYNTAX_DIRECTIVE_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9]*)[\t\n\f\r ]*=[\t\n\f\r ]*(.+?)[\t\n\f\r ]*$")
# docker/dockerfile tags (the frontend a `# syntax=` names), by how they read heredocs, from parser.go,
# split_command.go, line_parsers.go and shell/lex.go at the dockerfile/1.N.0 tags of moby/buildkit:
# - 1.16 and later, and the floating 1, 1-labs, latest and labs channels: as _heredocs does (1.16 to 1.28 differ
#   only in Dump and how bodies are buffered; lex.go's 1.28 change to escapes in double quotes is undone when the
#   delimiter is lexed again).
# - 1.4 to 1.15, and 1.3-labs: the lexer doesn't take the spaces after `<<` into the word and the regexp has no
#   \s*, so `<< EOF` isn't a heredoc; ${...} is lexed differently (no shared word buffer in 1.4, modifiers added
#   later); the --flags ignore `# escape=` before 1.15; and before 1.9 parseJSON takes `["a"] <<EOF` as exec form.
# - 1.3 and older (heredocs are only in 1.3's -labs build, behind the dfheredoc build tag): no heredocs.
_DOCKERFILE_SYNTAX_RE = re.compile(
    r"(?:docker\.io/)?docker/dockerfile(?::(?:1|latest|labs|1-labs|1\.(\d+)(?:\.\d+)?(?:-rc\d+)?(-labs)?))?", re.ASCII
)

# Heredocs, as BuildKit reads them: see _heredocs.
# Go's unicode.IsSpace. Unlike str.isspace, it doesn't count \x1c-\x1f.
_GO_SPACES = (
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_GO_SPACE_BYTES = b"\t\n\v\f\r \x85\xa0"  # unicode.IsSpace on a single byte, as extractBuilderFlags calls it
# Go's \s is [\t\n\f\r ] (no \v), and its $ is the end of the text: used with fullmatch.
_HEREDOC_RE = re.compile(r"(\d*)<<(-?)[\t\n\f\r ]*([^<]*)", re.ASCII)


class _LexError(Exception):
    """BuildKit's shell lexer returned an error."""


class _ShellLexer:
    """BuildKit's shell.Lex (shell/lex.go) as the parser uses it: \\ as escape, SkipUnsetEnv, no variables set."""

    def __init__(self, text: str, raw_quotes: bool, raw_escapes: bool) -> None:
        self.text = text.removeprefix("\ufeff")  # Go's text/scanner skips a leading byte order mark
        self.pos = 0
        self.raw_quotes = raw_quotes
        self.raw_escapes = raw_escapes
        # The word being built, as a list of non-empty strings so building it stays linear: like Go's wordsBuffer,
        # one buffer shared by nested _stop_on calls.
        self.buf: list[str] = []

    def words(self) -> list[str]:
        """ProcessWords, raising _LexError where it returns an error."""
        return self._stop_on("", self.raw_escapes, False)[1]

    def _peek(self) -> str:
        return self.text[self.pos : self.pos + 1]  # "" at the end, Go's scanner.EOF

    def _next(self) -> str:
        ch = self._peek()
        self.pos += len(ch)
        return ch

    def _stop_on(self, stop: str, raw_escapes: bool, pattern: bool) -> tuple[str, list[str]]:
        """processStopOn: read up to `stop` (to the end if it's ""), returning the text and its words."""
        result = ""
        words: list[str] = []
        in_word = False
        self.buf = []  # this also empties the word of any _stop_on this one is nested in, as in Go

        def add(text: str, raw: bool) -> None:  # wordsStruct's addRawString (raw) and addString
            nonlocal in_word
            for ch in [text] if raw else text:
                if raw or ch not in _GO_SPACES:
                    if ch:
                        self.buf.append(ch)
                    in_word = True
                elif in_word and self.buf:
                    words.append("".join(self.buf))
                    self.buf = []
                    in_word = False

        saved, self.raw_escapes = self.raw_escapes, raw_escapes
        try:
            stopped = False
            while ch := self._peek():
                if ch == stop:
                    self._next()
                    stopped = True
                    break
                if ch == "$":
                    text = self._dollar()
                    result += text
                    add(text, raw=False)
                elif ch in "<'\"":
                    text = self._heredoc() if ch == "<" else self._quoted(ch, pattern)
                    result += text
                    add(text, raw=True)
                else:
                    self._next()
                    if ch == "\\":
                        if self.raw_escapes:
                            add(ch, raw=True)
                            result += ch
                        ch = self._next()
                        if not ch:
                            break
                        add(ch, raw=True)
                    else:
                        add(ch, raw=False)
                    result += ch
            if stop and not stopped:
                raise _LexError(f"unexpected end of statement while looking for matching {stop}")
        finally:
            self.raw_escapes = saved
        if self.buf:
            words.append("".join(self.buf))
            self.buf = []
        return result, words

    def _quoted(self, quote: str, pattern: bool) -> str:
        """processSingleQuote and processDoubleQuote."""
        self._next()
        result = quote if self.raw_quotes else ""
        while ch := self._peek():
            if ch == quote:
                self._next()
                return result + ch if self.raw_quotes else result
            if quote == '"' and ch == "$":
                result += self._dollar()
                continue
            self._next()
            if quote == "'":
                result += ch * 2 if pattern and ch == "\\" else ch
                continue
            if ch == "\\":
                after = self._peek()
                if not after:
                    continue  # a \ at the end is dropped (then the quote is unterminated)
                if after in '"$\\':
                    if self.raw_escapes:
                        result += ch
                    ch = self._next()
                elif self.raw_escapes and (not self.raw_quotes or pattern):
                    result += ch
            result += ch
        raise _LexError(f"unexpected end of statement while looking for matching {quote}")

    def _dollar(self) -> str:
        """processDollar. No variable is set, so the text comes back as written (but see ${x//a/b})."""
        self._next()
        if self._peek() != "{":
            return "$" + self._name()
        self._next()
        ch = self._peek()
        if not ch:
            raise _LexError("syntax error: missing '}'")
        if ch in "{}:":
            raise _LexError("syntax error: bad substitution")
        name = self._name()
        ch = self._next()
        if ch == "}":
            return "${" + name + "}"
        modifier = ch
        if ch == ":":
            ch = self._next()
            modifier += ch
        if modifier == ch and ch == "/":
            if self._peek() == "/":
                self._next()  # ${x//a/b} comes back as ${x/a/b}
            pattern = self._stop_on("/", True, True)[0]
            return "${" + name + "/" + pattern + "/" + self._stop_on("}", True, False)[0] + "}"
        if modifier != ch or (ch and ch in "+-?#%"):
            raw = bool(ch) and ch in "#%"
            if raw and modifier != ch:
                raise _LexError(f"unsupported modifier ({modifier}) in substitution")
            return "${" + name + modifier + self._stop_on("}", raw, raw)[0] + "}"
        raise _LexError(f"unsupported modifier ({modifier}) in substitution")

    def _name(self) -> str:
        """processName: a variable name, a run of digits or one special parameter."""
        name = ""
        while ch := self._peek():
            if not name and ch.isdecimal():
                while (ch := self._peek()) and ch.isdecimal():
                    name += self._next()
                return name
            if not name and ch in "@*#?-$!0":
                return self._next()
            if not (ch.isalpha() or ch.isdecimal() or ch == "_"):
                break
            name += self._next()
        return name

    def _heredoc(self) -> str:
        """processPossibleHeredoc: `<<` takes the tabs, carriage returns and spaces after it into its word."""
        self._next()
        if self._peek() != "<":
            return "<"
        self._next()
        result = "<<"
        while (ch := self._peek()) and ch in "\t\r ":
            result += self._next()
        return result


def _args_after_flags(args: str, escape: str) -> str:
    """What's left of an instruction's args after its --flags (parser/split_command.go, extractBuilderFlags).

    BuildKit reads the UTF-8 bytes, so the byte 0x85 or 0xA0 inside a character (as in U+00E0) counts as a space.
    """
    data = args.encode()
    word = b""  # the flag being read, unquoted: BuildKit stops reading flags at a bare `--`
    in_word = False
    quote: int | None = None
    pos = 0
    while pos < len(data):
        ch = data[pos]
        if not in_word:
            if ch not in _GO_SPACE_BYTES:
                if data[pos : pos + 2] != b"--":
                    return data[pos:].decode(errors="replace")
                in_word = True
                continue
        elif quote is None and ch in _GO_SPACE_BYTES:
            if word == b"--":
                return data[pos:].decode(errors="replace")
            in_word, word = False, b""
        elif quote is None and ch in b"'\"":
            quote = ch
        elif quote is not None and ch == quote:
            quote = None
        elif ch == ord(escape) and pos + 1 == len(data):
            quote = None  # a trailing escape is skipped
        else:
            if ch == ord(escape):
                pos += 1
            word += data[pos : pos + 1]
        pos += 1
    return ""


def _split_command(line: str, escape: str) -> tuple[str, str]:
    """The keyword, lowercased, and the args after any --flags (parser/split_command.go, splitCommand)."""
    parts = re.split(r"[\t\v\f\r ]+", line.strip(_GO_SPACES), maxsplit=1)
    # Go's strings.ToLower, for comparing with ASCII keywords: U+0130 and the Kelvin sign also lower to ASCII.
    keyword = "".join(c.lower() if c.isascii() else {"\u0130": "i", "\u212a": "k"}.get(c, c) for c in parts[0])
    if len(parts) == 1:
        return keyword, ""
    return keyword, _args_after_flags(parts[1], escape).strip(_GO_SPACES)


def _not_go_json(text: str) -> NoReturn:
    raise ValueError(f"Go's encoding/json rejects {text}")


def _go_json_number(text: str) -> float:
    if math.isinf(number := float(text)):
        raise ValueError(f"Go's encoding/json rejects {text}, which overflows a float64")
    return number


def _is_exec_form(line: str, args: str) -> bool:
    """Whether BuildKit reads args as a JSON array (parser/line_parsers.go, parseJSON), raising if it rejects them."""
    args = args.lstrip(_GO_SPACES)
    if not args.startswith("["):
        return False
    try:
        items = json.loads(args, parse_constant=_not_go_json, parse_int=_go_json_number, parse_float=_go_json_number)
    except RecursionError:
        # Arrays nested past Python's limit aren't strings. (Past Go's limit, 10000, they aren't JSON: raise anyway.)
        items = [[]]
    except ValueError:
        return False  # not JSON: shell form
    if not all(isinstance(item, str) for item in items):
        raise DockerfileError(f"{line}: Docker rejects a JSON array that isn't all strings")
    return True


def _heredoc_name(line: str, rest: str) -> str | None:
    """The delimiter for `<<rest`, or None if there's no heredoc (parser/parser.go, heredocFromMatch)."""
    if not rest:
        return None
    try:
        words = _ShellLexer(rest, raw_quotes=False, raw_escapes=False).words()
        if len(words) != 1:
            return None  # <<"" and <<'' (no word) aren't heredocs
        if len(_ShellLexer(rest, raw_quotes=True, raw_escapes=False).words()) != 1:
            raise _LexError("internal lexing of heredoc produced inconsistent results")
    except _LexError as e:
        raise DockerfileError(f"{line}: Docker can't read the heredoc <<{rest} ({e})") from None
    return words[0]


def _heredoc_args(instruction: str, escape: str) -> str | None:
    """The args of a shell-form RUN, COPY or ADD (or ONBUILD of those) with `<<` in it, else None.

    canContainHeredoc in parser.go, and the strings.Contains(line, "<<") before it.
    """
    keyword, args = _split_command(instruction, escape)
    if keyword == "onbuild" and args:
        keyword, args = _split_command(args, escape)
    if keyword in ("run", "copy", "add") and not _is_exec_form(instruction, args) and "<<" in instruction:
        return args
    return None


def _heredocs(instruction: str, escape: str = "\\") -> list[tuple[str, bool]]:
    """(delimiter, strip_tabs) for each heredoc BuildKit finds in one instruction, with continuations joined.

    Only shell-form RUN, COPY and ADD (and ONBUILD of those) have heredocs. Words are split, and delimiters
    unquoted, by BuildKit's shell lexer, which always uses \\ as its escape: `escape`, from the `# escape=`
    directive, is only used to find where the --flags end. Raises DockerfileError where BuildKit rejects the line.

    Follows BuildKit at commit cedaeca8850a2bb536bbd1db72419f3cf491bdb5: canContainHeredoc, heredocsFromLine and
    heredocFromMatch in parser.go, splitCommand and extractBuilderFlags in split_command.go, parseJSON in
    line_parsers.go, and the lexer in shell/lex.go:
    https://github.com/moby/buildkit/blob/cedaeca8850a2bb536bbd1db72419f3cf491bdb5/frontend/dockerfile/parser/parser.go
    https://github.com/moby/buildkit/blob/cedaeca8850a2bb536bbd1db72419f3cf491bdb5/frontend/dockerfile/shell/lex.go
    """
    if _heredoc_args(instruction, escape) is None:
        return []
    docs = []
    try:
        try:
            words = _ShellLexer(instruction, raw_quotes=True, raw_escapes=True).words()
        except _LexError:
            return []  # heredocsFromLine ignores the error, and has no words
        for word in words:
            if (m := _HEREDOC_RE.fullmatch(word)) and (name := _heredoc_name(instruction, m.group(3))) is not None:
                docs.append((name, m.group(2) == "-"))
    except RecursionError:
        # Go has no such limit (around 400 nested ${a:-...} here), so BuildKit reads the line: we can't say how.
        raise DockerfileError(
            f"{instruction[:80]}...: nested too deeply for wheelgap to find its heredocs, pass -p/-t instead"
        ) from None
    return docs


def _escape_char(lines: list[str]) -> str:
    """The `# escape=` parser directive (\\ by default, ` on Windows images)."""
    seen: set[str] = set()
    escape = "\\"
    for line in lines:
        m = _DIRECTIVE_RE.match(line.strip())
        if not m or m.group(1).lower() not in _DIRECTIVES:
            break  # directives come first; any other line (even another comment) ends them
        name = m.group(1).lower()
        if name in seen:
            raise DockerfileError(f"only one {name} parser directive can be used")
        seen.add(name)
        if name == "escape":
            if m.group(2) not in ("`", "\\"):
                raise DockerfileError(f"invalid escape directive {m.group(2)!r} (Docker accepts ` or \\)")
            escape = m.group(2)
    return escape


def _syntax_directive(lines: list[str]) -> str | None:
    """The frontend image a `# syntax=` (or `//syntax=`) directive names, found as BuildKit's DetectSyntax finds it.

    Follows parseDirective and DirectiveParser in BuildKit's parser/directives.go at commit
    cedaeca8850a2bb536bbd1db72419f3cf491bdb5: a first `#!` line is skipped, the directives must start their line,
    and the image is the value up to its first space. (A Dockerfile that's all JSON can't have a FROM line.)
    https://github.com/moby/buildkit/blob/cedaeca8850a2bb536bbd1db72419f3cf491bdb5/frontend/dockerfile/parser/directives.go
    """
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    for comment in ("#", "//"):
        seen: set[str] = set()
        for line in lines:
            if not line.startswith(comment):
                break
            m = _SYNTAX_DIRECTIVE_RE.match(line[len(comment) :].lstrip(_GO_SPACES))
            if not m or m.group(1).lower() not in _DIRECTIVES or m.group(1).lower() in seen:
                break
            seen.add(m.group(1).lower())
            if m.group(1).lower() == "syntax":
                return m.group(2).split(" ", 1)[0]
    return None


def _heredoc_rules(syntax: str | None) -> int | None:
    """The docker/dockerfile minor version whose heredoc rules `syntax` follows (see _DOCKERFILE_SYNTAX_RE).

    16 for no `# syntax=` (wheelgap assumes BuildKit's built-in frontend is 1.16 or later) and for 1.16 and later,
    4 to 15 for those (3 for 1.3-labs), 0 for no heredocs, and None for a frontend wheelgap can't place.
    """
    if syntax is None:
        return 16
    if not (m := _DOCKERFILE_SYNTAX_RE.fullmatch(syntax)):
        return None
    if m.group(1) is None:
        return 16
    minor = int(m.group(1))
    return min(minor, 16) if minor >= 4 or (minor == 3 and m.group(2)) else 0


# Docker reads instruction keywords in any case, so `arg` is an ARG too.
_GLOBAL_ARG_RE = re.compile(r"^(?i:ARG)[\t\v\f\r ]+([A-Za-z_][A-Za-z0-9_]*)(?:=([^ \t\v\f\r]+))?$", re.ASCII)
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", re.ASCII)


@dataclass
class _GlobalArgs:
    """The global ARGs before the first FROM: each name's default, or why it can't be used, and the instructions
    wheelgap can't split into a name and a value. Only a FROM that uses one makes a problem an error: an ARG
    nothing in FROM refers to doesn't change the image."""

    defaults: dict[str, str]
    problems: dict[str, str]
    unreadable: list[str]


def _global_arg_defaults(instructions: list[tuple[str, bool]]) -> _GlobalArgs:
    """The `ARG NAME=default` values Docker expands in FROM, from the instructions before the first FROM.

    A global ARG without a default can only come from `--build-arg`, which wheelgap doesn't read, so using it is an
    error. A default using quotes, escapes or `$` (another ARG, a shell modifier) is one too: Docker would expand it,
    and guessing it could pick the wrong image. An ARG that declares more than one variable (`ARG A=1 B=2`), or that
    is split over a line continuation, is Docker's too, but wheelgap would read the whole thing as one value, so a
    FROM using any build argument is an error while one is there.
    """
    args = _GlobalArgs({}, {}, [])
    for instruction, joined in instructions:
        if joined:
            args.unreadable.append(
                f"{instruction}: an ARG split over a line continuation isn't supported, pass -p/-t instead"
            )
            continue
        m = _GLOBAL_ARG_RE.match(instruction)
        if not m:
            args.unreadable.append(f"{instruction}: an ARG must declare one NAME=value, pass -p/-t instead")
            continue
        name, value = m.group(1), m.group(2)
        # A later declaration of the same name replaces the earlier one, as in Docker.
        args.defaults.pop(name, None)
        args.problems.pop(name, None)
        if value is None:
            args.problems[name] = f"ARG {name} has no default: a --build-arg value isn't read, pass -p/-t instead"
        elif any(c in value for c in "\"'\\$`"):
            args.problems[name] = (
                f"ARG {name}={value}: defaults with quotes, escapes or $ aren't supported, pass -p/-t instead"
            )
        else:
            args.defaults[name] = value
    return args


def _expand_build_args(args: str, globals_: _GlobalArgs) -> tuple[str, list[str]]:
    """Expand `$NAME` and `${NAME}` in a FROM's arguments from the global ARG defaults, returning the names used."""
    used: list[str] = []

    def expand(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if globals_.unreadable:  # it may declare this name too, after the default read here
            raise DockerfileError(globals_.unreadable[0])
        if name in globals_.problems:
            raise DockerfileError(globals_.problems[name])
        if name not in globals_.defaults:
            raise DockerfileError(unsupported)
        used.append(name)
        return globals_.defaults[name]

    unsupported = f"{args}: build arguments are not supported, pass -p/-t instead"
    expanded = _VAR_REF_RE.sub(expand, args)
    if "$" in expanded:  # ${NAME:-default}, $BUILDPLATFORM and anything else wheelgap can't know
        raise DockerfileError(globals_.unreadable[0] if globals_.unreadable else unsupported)
    return expanded, used


def _from_lines(text: str) -> list[str]:
    """The arguments of every FROM instruction in order, with line continuations joined as Docker joins them."""
    return [args for _, args in _from_instructions(text)]


def _from_instructions(text: str) -> list[tuple[int, str]]:
    """(line number, arguments) of every FROM instruction, as written: build arguments aren't expanded."""
    return _parse_froms(text)[0]


def _parse_froms(text: str) -> tuple[list[tuple[int, str]], _GlobalArgs]:
    """(line number, arguments) of every FROM instruction in order, the line being where the instruction starts.

    Heredoc bodies (`RUN <<EOF` ... `EOF`) are skipped, so a line in one is never read as an instruction.
    Follows Parse in BuildKit's parser.go at commit cedaeca8850a2bb536bbd1db72419f3cf491bdb5: each body starts
    after the whole instruction (continuations joined), one heredoc after another, and is read line by line as
    written, with no continuations or comments. A line ends a body when, with every trailing \r trimmed (and, for
    `<<-`, its leading tabs), it's exactly the delimiter. A body the file ends in is an error.
    https://github.com/moby/buildkit/blob/cedaeca8850a2bb536bbd1db72419f3cf491bdb5/frontend/dockerfile/parser/parser.go

    That's docker/dockerfile 1.16 and later. With no `# syntax=` line, wheelgap assumes Docker's built-in frontend
    is too: a known limit, since the classic builder and older built-in frontends read heredocs differently or not
    at all (to them, `FROM base` after `RUN : << '#'` is an instruction). A `# syntax=` frontend that reads a line's
    heredocs differently from 1.16 is an error (see _heredoc_rules).
    """
    lines = [raw.rstrip("\r") for raw in text.split("\n")]  # Docker's trimNewline drops every trailing \r
    escape = _escape_char(lines)
    syntax = _syntax_directive(lines)
    rules = _heredoc_rules(syntax)
    esc = re.escape(escape)
    continued = re.compile(rf"(?<!{esc}){esc}[ \t]*$")  # one escape char at the end, not two
    found: list[tuple[int, str]] = []
    args_before_from: list[tuple[str, bool]] = []  # (instruction, continued) of the ARGs before the first FROM
    parts: list[str] = []  # an instruction continued onto the next line
    start = 0  # the line the instruction being read starts on
    heredocs: deque[tuple[str, bool]] = deque()  # (delimiter, strip_tabs) of the bodies still to skip, in order

    def finish(instruction: str, joined: bool, line_number: int) -> None:
        # Docker splits the keyword off on ASCII whitespace only, so `FROM<U+00A0>python` isn't a FROM to it.
        words = re.split(r"[ \t\v\f\r]+", instruction, maxsplit=1)
        if words[0].upper() != "FROM" and re.match(r"(?i)FROM\s", words[0]):
            raise DockerfileError(
                f"unknown instruction {words[0]!r} (Docker splits instructions on ASCII whitespace only)"
            )
        # Python's strip() also drops \x1c-\x1f, so this catches "\x1cFROM x" and "\x1c FROM x" alike.
        if words[0].upper() != "FROM" and re.match(r"(?i)FROM(\s|$)", instruction.strip()):
            raise DockerfileError(
                f"unknown instruction {words[0]!r} (Docker doesn't count \\x1c-\\x1f as whitespace around FROM)"
            )
        if joined and words and words[0].upper().startswith("FROM") and words[0].upper() != "FROM":
            raise DockerfileError(f"unknown instruction {words[0]!r} (a FROM joined to the next line?)")
        if words and words[0].upper() == "FROM":
            if len(words) == 1:
                raise DockerfileError("FROM with no image")
            found.append((line_number, words[1]))
        elif not found and words and words[0].upper() == "ARG":
            # The ARGs before the first FROM, as Docker joined them: only those are expanded in FROM. A line in a
            # heredoc body never gets here, so `RUN <<EOF` with an ARG in its body isn't read as one.
            args_before_from.append((instruction, joined))

    def heredocs_in(instruction: str) -> deque[tuple[str, bool]]:
        docs = deque(_heredocs(instruction, escape))
        if rules == 16 or (args := _heredoc_args(instruction, escape)) is None:
            return docs
        if rules is None or rules == 0:
            differs = bool(docs)
        else:
            differs = (
                "${" in instruction
                or re.search(r"<<[\t\r ]", instruction) is not None
                or (rules < 15 and escape != "\\")
                or (rules < 9 and args.startswith("["))
            )
        if differs:
            raise DockerfileError(
                f"# syntax={syntax} may read the heredocs in {instruction.strip()[:60]!r} differently from "
                "docker/dockerfile 1.16 and later, which wheelgap follows, pass -p/-t instead"
            )
        return docs

    for number, line in enumerate(lines, 1):
        if heredocs:  # a body line: never an instruction, a comment or a continuation
            name, strip_tabs = heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == name:
                heredocs.popleft()
            continue
        # parser.go (linked above): isComment and isEmptyContinuationLine trim leading unicode.IsSpace (not \x1c-\x1f).
        stripped = line.lstrip(_GO_SPACES)
        if parts and (not stripped or stripped.startswith("#")):
            continue  # Docker skips blank and comment lines inside a continued instruction
        if not parts and stripped.startswith("#"):
            continue  # a comment never continues, even if it ends in the escape char
        if m := continued.search(line):
            if not parts:
                start = number
            parts.append(line[: m.start()])
            continue
        instruction = "".join([*parts, line])  # Docker joins continued lines with nothing in between
        # splitCommand: strings.TrimSpace, https://github.com/moby/buildkit/blob/cedaeca/frontend/dockerfile/parser/split_command.go
        finish(instruction.strip(_GO_SPACES), joined=bool(parts), line_number=start if parts else number)
        heredocs = heredocs_in(instruction)
        parts = []
    if parts:  # the file ended mid-continuation: Docker still reads what's there
        finish("".join(parts).strip(_GO_SPACES), joined=len(parts) > 1, line_number=start)
        heredocs = heredocs_in("".join(parts))
    if heredocs:
        raise DockerfileError(f"unterminated heredoc <<{heredocs[0][0]} (BuildKit rejects it)")
    return found, _global_arg_defaults(args_before_from)


def last_from(text: str) -> str:
    """The arguments of the last FROM instruction, with line continuations joined."""
    found = _from_lines(text)
    if not found:
        raise DockerfileError("no FROM line found")
    return found[-1]


def _unquote(value: str, args: str) -> str:
    """Strip single or double quotes as BuildKit does to flag values (parser/split_command.go, extractBuilderFlags).

    Backslash escapes (and `$` expansion) aren't modelled, so a value using them is an error, never a guess.
    """
    if "\\" in value or "`" in value:
        raise DockerfileError(f"FROM {args}: escapes in --platform aren't supported, pass -p/-t instead")
    out = []
    quote: str | None = None
    for ch in value:
        if quote is None:
            if ch in ('"', "'"):
                quote = ch
            else:
                out.append(ch)
        elif ch == quote:
            quote = None
        else:
            out.append(ch)
    if quote is not None:
        raise DockerfileError(f"FROM {args}: unterminated quote in --platform={value}")
    return "".join(out)


def _split_from(args: str) -> tuple[str, str | None, str | None]:
    """`[--platform=P] image [AS name]` -> (image, platform, stage name), rejecting what Docker rejects."""
    stripped = args.strip(" \t\v\f\r")
    words = re.split(r"[ \t\v\f\r]+", stripped) if stripped else []
    platform = None
    seen_platform = False
    while words and words[0].startswith("--"):
        flag = words.pop(0)
        if flag == "--":  # BuildKit drops it and stops reading flags
            break
        if flag == "--platform":
            raise DockerfileError(f"FROM {args}: --platform needs a value: use --platform=VALUE")
        if not flag.startswith("--platform="):
            raise DockerfileError(f"FROM {args}: unknown flag {flag!r}")
        if seen_platform:
            raise DockerfileError(f"FROM {args}: duplicate --platform flag")
        seen_platform = True
        # Quotes are stripped as Docker does; an empty value means the default platform, as in Docker.
        platform = _unquote(flag.split("=", 1)[1], args) or None
    if not words:
        raise DockerfileError(f"FROM {args}: no image")
    if len(words) == 1:
        return words[0], platform, None
    if len(words) == 3 and words[1].upper() == "AS":
        stage = words[2].lower()
        if not re.fullmatch(r"[a-z][a-z0-9-_.]*", stage):  # BuildKit's parseBuildStageName
            raise DockerfileError(f"FROM {args}: invalid stage name {words[2]!r}")
        return words[0], platform, stage
    raise DockerfileError(f"FROM {args}: expected 'IMAGE' or 'IMAGE AS NAME'")


def parse_from(args: str) -> ImageTarget:
    """Parse `[--platform=P] image[:tag][@digest] [AS name]`."""
    image, platform, _ = _split_from(args)
    return parse_image(image, platform)


def final_image(text: str) -> tuple[str, str | None]:
    """The image the last stage is built on, and its platform.

    `FROM base AS runtime` builds on an earlier stage called `base`; that is followed back (through
    any chain of stages) to a real image. Like Docker, a stage keeps the platform of the stage it
    builds on: a `--platform` on `FROM base` doesn't change what `base` was built for.
    """
    image, platform, _ = final_from(text)
    return image, platform


def final_from(text: str) -> tuple[str, str | None, int]:
    """final_image, plus the line of the FROM instruction that names that image."""
    image, platform, number, _ = _final_from(text)
    return image, platform, number


def _final_from(text: str) -> tuple[str, str | None, int, dict[str, str]]:
    """final_from, plus the global ARG defaults that FROM used. Build arguments are expanded in every FROM, as
    Docker does, but one wheelgap can't expand is an error only in the FROM that names the final image: an
    earlier stage built `--platform=$BUILDPLATFORM` doesn't change the image the last stage runs on."""
    stages: dict[str, tuple[str, str | None, int, dict[str, str]]] = {}
    lines, global_args = _parse_froms(text)
    if not lines:
        raise DockerfileError("no FROM line found")
    errors: dict[int, DockerfileError] = {}

    def split(number: int, args: str) -> tuple[str, str | None, str | None, dict[str, str]]:
        try:
            args, names = _expand_build_args(args, global_args)
        except DockerfileError as e:
            errors[number] = e  # the arguments stay as written; parse_image rejects their `$` anyway
            names = []
        image, platform, stage = _split_from(args)
        return image, platform, stage, {name: global_args.defaults[name] for name in names}

    for number, args in lines[:-1]:
        image, platform, stage, used = split(number, args)
        if (base := stages.get(image)) is not None:  # AS names are lowercased, references aren't
            image, platform, number, base_used = base
            used = base_used | used  # an ARG naming the stage changes the image too
        if stage:
            stages[stage] = (image, platform, number, used)
    number, args = lines[-1]
    image, platform, _, used = split(number, args)
    if (base := stages.get(image)) is not None:
        image, platform, number, base_used = base
        used = base_used | used  # an ARG naming the stage changes the image too
    elif stages and not any(c in image for c in ":/@$") and image.lower() not in (*IMAGE_NAMES, "scratch"):
        known = ", ".join(stages)
        raise DockerfileError(
            f"FROM {image}: not an earlier stage ({known}) or an official python image, pass -p/-t instead"
        )
    if number in errors:
        raise errors[number]
    return image, platform, number, used


def parse_image(image: str, platform: str | None = None) -> ImageTarget:
    if "$" in image or (platform and "$" in platform):
        raise DockerfileError(f"{image}: build arguments are not supported, pass -p/-t instead")
    image = image.split("@", 1)[0]
    name, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    # Docker requires a lowercase repository path. A registry host (the first part, if it has a dot or colon,
    # is localhost, or has capitals: splitDockerDomain) is kept exactly as written, so `Docker.IO` isn't Docker Hub.
    first, slash, path = name.partition("/")
    if not (slash and (any(c in first for c in ".:") or first == "localhost" or first != first.lower())):
        path = name
    if path != path.lower():
        raise DockerfileError(f"{image}: repository name must be lowercase, as Docker requires")
    if tag and not _DOCKER_TAG_RE.fullmatch(tag):
        raise DockerfileError(f"{image}: invalid tag {tag!r} (Docker allows only ASCII letters, digits, '_', '.', '-')")
    if name not in IMAGE_NAMES and name != UV_IMAGE:
        raise DockerfileError(
            f"{image}: only the official python images and Python-tagged Astral uv images are supported, "
            "pass -p/-t instead"
        )
    if name == UV_IMAGE:
        prefix = _UV_TAG_PREFIX_RE.match(tag)
        tag = tag[prefix.end() :] if prefix else ""
    m = _TAG_RE.match(tag)
    if not m:
        if name == UV_IMAGE:
            raise DockerfileError(f"{image}: the tag must specify a Python version like python3.13-alpine")
        raise DockerfileError(f"{image}: the tag must start with a Python version like 3.13")
    python = f"{m.group(1)}.{m.group(2)}"
    # An X.Y-rc tag stops being rebuilt once X.Y is released, so like a patch tag it can be on an older Debian than
    # its X.Y tag (see _MINOR_TAG_RE).
    rc = bool(m.group(3))
    minor_tag = bool(_MINOR_TAG_RE.match(tag)) and not rc
    rc_release = False  # the Debian release came from RC_DEBIAN, whose images kept every arch they were built for
    variant = m.group(4).split("-") if m.group(4) else []  # Docker Hub tags are case-sensitive: -SLIM doesn't exist

    release = None
    if alpine := next((part for part in variant if part.startswith("alpine")), None):
        family, glibc, musl = "musl", None, ALPINE_MUSL
        if (m := _ALPINE_RE.match(alpine)) and (int(m.group(1)), int(m.group(2))) <= (3, 12):
            musl = "1.1"
    elif any(part in WINDOWS_VARIANTS for part in variant):
        if platform:
            raise DockerfileError(f"{image}: --platform is not supported for Windows images")
        return ImageTarget(python, "win-amd64")
    else:
        family, musl = "linux", None
        unknown = [part for part in variant if part not in DEBIAN_GLIBC and part != "slim"]
        if unknown:
            raise DockerfileError(f"{image}: unknown image variant '{'-'.join(unknown)}'")
        release = next((part for part in variant if part in DEBIAN_GLIBC), None)
        if release is None and name in IMAGE_NAMES and minor_tag:
            release = UNTAGGED_DEBIAN.get(python)
        elif release is None and name in IMAGE_NAMES and rc:
            if python in RC_DEBIAN:
                release, rc_release = RC_DEBIAN[python], True
            elif (int(m.group(1)), int(m.group(2))) < (3, 10):
                raise DockerfileError(
                    f"{image} is on a Debian release older than bullseye: pass -t and --glibc instead"
                )
        glibc = DEBIAN_GLIBC.get(release) if release else None

    arch = "x86_64"
    if platform:
        if platform.lower() not in ARCHES:
            raise DockerfileError(f"--platform={platform}: only {', '.join(ARCHES)} are supported")
        arch = ARCHES[platform.lower()]
        if name == UV_IMAGE and arch not in UV_ARCHES:
            raise DockerfileError(f"{image}: Astral's uv images are only published for linux/amd64 and linux/arm64")
        if name in IMAGE_NAMES and release in DEBIAN_ARCHES:
            # An X.Y-rc tag may be live or long stale, so it gets only the arches both kinds are published for.
            frozen = not _MINOR_TAG_RE.match(tag) or (python, release) in OLD_DEBIAN_TAGS or rc_release
            if arch not in (OLD_DEBIAN_ARCHES if frozen else DEBIAN_ARCHES[release]):
                if UNTAGGED_DEBIAN.get(python) == "trixie":
                    hint = f" ({python}-trixie is)"  # the X.Y tag: a patch tag has no trixie twin
                elif (python, "bookworm") in OLD_DEBIAN_TAGS and arch in OLD_DEBIAN_ARCHES:
                    hint = f" ({python}-bookworm is)"
                else:
                    hint = ""
                if rc and arch in OLD_DEBIAN_ARCHES:  # 3.14-rc-bookworm has s390x, 3.15-rc-bookworm doesn't
                    raise DockerfileError(
                        f"{image} may not be published for {platform}: older -rc builds have it, newer ones don't{hint}"
                    )
                raise DockerfileError(f"{image} isn't published for {platform}{hint}")
    return ImageTarget(python, f"{family}-{arch}", glibc, musl)


def read_dockerfile(path: Path) -> ImageTarget:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        byte = f"0x{e.object[e.start]:02x}"
        raise DockerfileError(f"{path}: not UTF-8 (byte {byte} at offset {e.start}), pass -p/-t instead") from None
    try:
        image, platform, _, used = _final_from(text)
        target = parse_image(image, platform)
    except DockerfileError as e:
        raise DockerfileError(f"{path}: {e}") from None
    return replace(target, build_args=tuple(used.items())) if used else target
