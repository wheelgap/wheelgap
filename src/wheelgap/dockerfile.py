"""Turn the final stage of a Dockerfile into a wheelgap target, following named stages back to an image."""

from __future__ import annotations

import fnmatch
import json
import math
import os
import posixpath
import re
import shlex
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NoReturn

from packaging.requirements import InvalidRequirement, Requirement

from . import dockerignore
from .sources import (
    _COMMENT_RE,
    SourceError,
    _abbreviated_include,
    _join_continued_lines,
    _line_include,
    parse_requirements,
    requirement_to_package,
)
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


class UnknownImageError(DockerfileError):
    """The Dockerfile is one Docker reads, but its final image isn't one wheelgap knows the Python and platform of:
    not an official python or Astral uv image, a tag that names no Python, or a FROM using a build argument wheelgap
    can't expand. `-p` and `-t` on the command line can stand in for it; an error Docker itself would raise can't be
    stood in for, and stays a plain DockerfileError. `line` is the FROM instruction's line, where it is known."""

    line: int | None = None


@dataclass(frozen=True)
class ImageTarget:
    python: str
    platform: str
    glibc: str | None = None
    musl: str | None = None
    # The global ARG values the final image's FROM used, as (name, value): a default, or a --build-arg value for
    # the names in given_args.
    build_args: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    given_args: frozenset[str] = field(default=frozenset(), compare=False)
    # --build-arg names no global ARG declares: Docker doesn't use them in FROM.
    undeclared_args: tuple[str, ...] = field(default=(), compare=False)


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
# docker/dockerfile pinned by digest: the digest, not the tag, picks the image, so its version (and heredoc rules) is
# unknown, but it is still docker/dockerfile.
_DOCKERFILE_DIGEST_RE = re.compile(
    r"(?:docker\.io/)?docker/dockerfile(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}", re.ASCII
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


# The names BuildKit gives a value before reading the Dockerfile, from the platforms it builds on and for (and the
# target stage). A global ARG's default replaces that value, and only `--build-arg` replaces the default: defaultArgs in
# frontend/dockerfile/dockerfile2llb/platform.go, then buildMetaArgs in convert.go (AddOrReplace), at moby/buildkit
# 38b0e2ab2e9bc5fd3f622759ac92ad4b649f3bf4. So a default is used as for any other ARG, and with none the value is
# the build's own.
_AUTOMATIC_ARGS = frozenset(
    {
        "BUILDPLATFORM", "BUILDOS", "BUILDOSVERSION", "BUILDARCH", "BUILDVARIANT",
        "TARGETPLATFORM", "TARGETOS", "TARGETOSVERSION", "TARGETARCH", "TARGETVARIANT", "TARGETSTAGE",
    }
)  # fmt: skip
# Docker reads instruction keywords in any case, so `arg` is an ARG too.
_GLOBAL_ARG_RE = re.compile(r"^(?i:ARG)[\t\v\f\r ]+([A-Za-z_][A-Za-z0-9_]*)(?:=([^ \t\v\f\r]*))?$", re.ASCII)
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", re.ASCII)


@dataclass
class _GlobalArgs:
    """The global ARGs before the first FROM: each name's default, or why it can't be used, and the instructions
    wheelgap can't split into a name and a value. Only a FROM that uses one makes a problem an error: an ARG
    nothing in FROM refers to doesn't change the image."""

    defaults: dict[str, str]
    problems: dict[str, str]
    unreadable: list[str]
    declared: set[str] = field(default_factory=set)


def _global_arg_defaults(
    instructions: list[tuple[str, bool]], build_args: Mapping[str, str] | None = None
) -> _GlobalArgs:
    """The `ARG NAME=default` values Docker expands in FROM, from the instructions before the first FROM.

    A `--build-arg NAME=VALUE` in build_args replaces the default of a declared ARG, or gives a value to one with
    none, and is held to the same rules as a default. A global ARG left without a value is an error. An empty
    default (`ARG PY=`) sets the name to the empty string, which Docker accepts but which leaves the image as
    `python:-slim`, so it's one too. A default using quotes, escapes or `$` (another ARG, a shell modifier)
    is one as well: Docker would expand it, and guessing it could pick the wrong image. An ARG that declares more than
    one variable (`ARG A=1 B=2`), that is split over a line continuation, or whose value has a non-ASCII character
    (Docker splits an ARG on any Unicode space, so `ARG A=1<U+00A0>B=2` declares two), is Docker's too, but wheelgap
    would read the whole thing as one value, so a FROM using any build argument is an error while one is there.
    """
    build_args = build_args or {}
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
        args.declared.add(name)
        # Docker splits an ARG on any Unicode space, so a non-ASCII declaration may declare more than one name
        # (`ARG A=1<U+00A0>B=2`): it stays unreadable whatever --build-arg gives A.
        if not instruction.isascii():
            args.unreadable.append(f"{instruction}: non-ASCII characters in an ARG aren't supported")
            continue
        given = name in build_args
        what = f"--build-arg {name}" if given else f"ARG {name}"
        if given:
            value = build_args[name]
            if not value.isascii():
                args.problems[name] = f"{what}={value}: non-ASCII characters in a value aren't supported"
                continue
        # A later declaration of the same name replaces the earlier one, as in Docker.
        args.defaults.pop(name, None)
        args.problems.pop(name, None)
        if value is None and name in _AUTOMATIC_ARGS:
            args.problems[name] = f"ARG {name} has no default: {name} is set by the build, pass -p/-t instead"
        elif value is None:
            args.problems[name] = f"ARG {name} has no default and no --build-arg {name}=VALUE, pass -p/-t instead"
        elif value == "":
            empty = f"{what} is empty" if given else f"ARG {name} has an empty default"
            args.problems[name] = f"{empty}, pass -p/-t instead"
        elif any(c in value for c in "\"'\\$`"):
            args.problems[name] = (
                f"{what}={value}: {'values' if given else 'defaults'} with quotes, escapes or $ aren't supported, "
                "pass -p/-t instead"
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
            raise UnknownImageError(globals_.unreadable[0])
        if name in globals_.problems:
            raise UnknownImageError(globals_.problems[name])
        if name not in globals_.defaults:
            raise UnknownImageError(unsupported)
        used.append(name)
        return globals_.defaults[name]

    unsupported = f"{args}: build arguments are not supported, pass -p/-t instead"
    expanded = _VAR_REF_RE.sub(expand, args)
    if "$" in expanded:  # ${NAME:-default}, $BUILDPLATFORM and anything else wheelgap can't know
        raise UnknownImageError(globals_.unreadable[0] if globals_.unreadable else unsupported)
    return expanded, used


def _from_lines(text: str) -> list[str]:
    """The arguments of every FROM instruction in order, with line continuations joined as Docker joins them."""
    return [args for _, args in _from_instructions(text)]


def _from_instructions(text: str) -> list[tuple[int, str]]:
    """(line number, arguments) of every FROM instruction, as written: build arguments aren't expanded."""
    return _parse_froms(text)[0]


def _parse_froms(
    text: str, build_args: Mapping[str, str] | None = None, instructions: list[tuple[int, str]] | None = None
) -> tuple[list[tuple[int, str]], _GlobalArgs]:
    """(line number, arguments) of every FROM instruction in order, the line being where the instruction starts.

    If `instructions` is given, every instruction is added to it as (line number, instruction), continuations joined.

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
        if instructions is not None:
            instructions.append((line_number, instruction))
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
    return found, _global_arg_defaults(args_before_from, build_args)


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


def _split_from_as_written(args: str) -> str | None:
    """The stage name of a FROM whose build arguments aren't expanded, or None if it has none. A FROM that
    _split_from rejects only for its unexpanded `$` text has no name wheelgap can use, and is None too."""
    try:
        return _split_from(args)[2]
    except DockerfileError:
        return None


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
    image, platform, number, _, _ = _final_from(text)
    return image, platform, number


def _final_from(
    text: str, build_args: Mapping[str, str] | None = None
) -> tuple[str, str | None, int, dict[str, str], _GlobalArgs]:
    """final_from, plus the global ARG defaults that FROM used. Build arguments are expanded in every FROM, as
    Docker does, but one wheelgap can't expand is an error only in the FROM that names the final image: an
    earlier stage built `--platform=$BUILDPLATFORM` doesn't change the image the last stage runs on."""
    stages: dict[str, tuple[str, str | None, int, dict[str, str]]] = {}
    lines, global_args = _parse_froms(text, build_args)
    if not lines:
        raise DockerfileError("no FROM line found")
    errors: dict[int, UnknownImageError] = {}

    def split(number: int, args: str) -> tuple[str, str | None, str | None, dict[str, str]]:
        try:
            args, names = _expand_build_args(args, global_args)
        except UnknownImageError as e:
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
        error = UnknownImageError(
            f"FROM {image}: not an earlier stage ({known}) or an official python image, pass -p/-t instead"
        )
        error.line = number
        raise error
    if number in errors:
        errors[number].line = number
        raise errors[number]
    return image, platform, number, used, global_args


def parse_image(image: str, platform: str | None = None) -> ImageTarget:
    if "$" in image or (platform and "$" in platform):
        raise UnknownImageError(f"{image}: build arguments are not supported, pass -p/-t instead")
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
        raise UnknownImageError(
            f"{image}: only the official python images and Python-tagged Astral uv images are supported, "
            "pass -p/-t instead"
        )
    if name == UV_IMAGE:
        prefix = _UV_TAG_PREFIX_RE.match(tag)
        tag = tag[prefix.end() :] if prefix else ""
    m = _TAG_RE.match(tag)
    if not m:
        if name == UV_IMAGE:
            raise UnknownImageError(f"{image}: the tag must specify a Python version like python3.13-alpine")
        raise UnknownImageError(f"{image}: the tag must start with a Python version like 3.13")
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
            raise UnknownImageError(f"{image}: unknown image variant '{'-'.join(unknown)}'")
        release = next((part for part in variant if part in DEBIAN_GLIBC), None)
        if release is None and name in IMAGE_NAMES and minor_tag:
            release = UNTAGGED_DEBIAN.get(python)
        elif release is None and name in IMAGE_NAMES and rc:
            if python in RC_DEBIAN:
                release, rc_release = RC_DEBIAN[python], True
            elif (int(m.group(1)), int(m.group(2))) < (3, 10):
                raise UnknownImageError(
                    f"{image} is on a Debian release older than bullseye: pass -t and --glibc instead"
                )
        glibc = DEBIAN_GLIBC.get(release) if release else None

    arch = "x86_64"
    if platform:
        if platform.lower() not in ARCHES:
            raise UnknownImageError(f"--platform={platform}: only {', '.join(ARCHES)} are supported")
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


def read_dockerfile(path: Path, build_args: Mapping[str, str] | None = None) -> ImageTarget:
    """The image of the Dockerfile's final stage. build_args are `--build-arg NAME=VALUE` values, read as Docker
    reads them: one replaces the default of the global ARG it names (or gives it a value), and one no global ARG
    declares is left out of FROM (ImageTarget.undeclared_args)."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        byte = f"0x{e.object[e.start]:02x}"
        raise DockerfileError(f"{path}: not UTF-8 (byte {byte} at offset {e.start}), pass -p/-t instead") from None
    number = None
    try:
        image, platform, number, used, global_args = _final_from(text, build_args)
        target = parse_image(image, platform)
    except UnknownImageError as e:
        error = UnknownImageError(f"{path}: {e}")
        error.line = e.line if e.line is not None else number
        raise error from None
    except DockerfileError as e:
        raise DockerfileError(f"{path}: {e}") from None
    given = frozenset(name for name in used if name in (build_args or {}))
    undeclared = tuple(name for name in build_args or {} if name not in global_args.declared)
    return replace(target, build_args=tuple(used.items()), given_args=given, undeclared_args=undeclared)


# --dockerfile-inputs: the requirements files the final stage installs, read from a strict subset of instructions
# (see dockerfile_requirements). Anything else that may change what pip installs is an error naming its line.
_PLAIN_ARGS_RE = re.compile(r"[A-Za-z0-9_./:=, \t-]*", re.ASCII)  # no quotes, $, escapes, globs, # or shell operators
_PIP_RE = re.compile(r"pip(?:3(?:\.\d+)?)?", re.ASCII)
_PYTHON_RE = re.compile(r"python(?:3(?:\.\d+)?)?", re.ASCII)
_UV_IMAGE_RE = re.compile(r"ghcr\.io/astral-sh/uv(?::[\w][\w.-]*)?(?:@sha256:[0-9a-f]{64})?", re.ASCII)
_UV_CONFIG_NAMES = ("uv.toml", "pyproject.toml")
_COPY_FLAG_RE = re.compile(r"--(?:chown=[\w.:-]+|chmod=[0-7]{3,4}|link)", re.ASCII)
_PLAIN = "no quotes, variables, escapes, globs, comments or shell operators"
_PIP_CONFIG = ("/etc/pip.conf", "/etc/xdg/pip/", "/root/.config/pip/", "/root/.pip/")
_PIP_CONFIG_NAMES = ("pip.conf", "pip.ini")


@dataclass(frozen=True)
class _Copy:
    line: int
    sources: tuple[tuple[str, Path, bool], ...]  # (path in the build context, path on disk, is a folder)
    dest: str  # absolute and normalised
    into: bool  # the sources land inside dest: a / at its end, several sources, a folder, or dest a known folder
    error: str | None = None  # why wheelgap can't tell what this instruction puts in the image
    uv_binaries: tuple[str, ...] = ()


def _add_dir(dirs: set[str], folder: str) -> None:
    while folder not in dirs:
        dirs.add(folder)
        folder = posixpath.dirname(folder)


def _read_copy(
    number: int,
    args: str,
    workdir: str | None,
    dirs: set[str],
    context: Path,
    context_label: str = "the Dockerfile's folder",
    ignore_rules: tuple[dockerignore.Rule, ...] = (),
) -> _Copy:
    def unread(error: str) -> _Copy:
        return _Copy(number, (), "", False, error)

    if args.startswith("--from=") and len(args.split()) >= 2:
        source, *names, dest = args.split()
        if (
            _UV_IMAGE_RE.fullmatch(source.removeprefix("--from="))
            and names in (["/uv"], ["/uv", "/uvx"])
            and dest.endswith("/")
            and _PLAIN_ARGS_RE.fullmatch(dest)
            and (dest.startswith("/") or workdir is not None)
        ):
            target = posixpath.normpath(posixpath.join(workdir or "/", dest))
            _add_dir(dirs, target)
            return _Copy(number, (), target, True, uv_binaries=tuple(name[1:] for name in names))
        return unread(f"COPY {source} isn't read by --dockerfile-inputs except when copying /uv and optional /uvx")
    if args.lstrip().startswith("[") or not _PLAIN_ARGS_RE.fullmatch(args):
        return unread(f"only a plain `COPY SRC... DEST` is read by --dockerfile-inputs ({_PLAIN}, no JSON form)")
    words = args.split()
    while words and words[0].startswith("--"):
        if not _COPY_FLAG_RE.fullmatch(flag := words.pop(0)):
            return unread(f"COPY {flag} isn't read by --dockerfile-inputs (only --chown, --chmod and --link are)")
    if len(words) < 2:
        return unread("COPY needs a source and a destination")
    *names, dest = words
    sources = []
    root = context.resolve()
    for name in names:
        rel = posixpath.normpath(name)
        if name.startswith("/") or rel == ".." or rel.startswith("../"):
            return unread(f"COPY {name}: only paths inside the build context ({context_label}) are read")
        host = context / rel
        if not host.exists():
            return unread(f"COPY {name}: not found in the build context ({context_label})")
        if not host.resolve().is_relative_to(root):
            return unread(f"COPY {name}: a symlink out of the build context isn't read by --dockerfile-inputs")
        # A link is matched against the ignore rules by its own name, and what it points to by its: the rules may
        # leave the target out of the build context, so the link dangles in the image.
        if ignore_rules and (link := _symlink_below(host, context)) is not None:
            return unread(
                f"COPY {name}: {link.as_posix()} is a symlink, which --dockerfile-inputs doesn't read "
                "when an ignore file has rules"
            )
        is_dir = host.is_dir()
        if not is_dir and (rule := dockerignore.ignored(rel, ignore_rules)):
            return unread(f"COPY {name}: excluded from the build context by {rule}")
        sources.append((rel, host, is_dir))
    if dest.startswith("/"):
        target = posixpath.normpath(dest)
    elif workdir is None:
        return unread(f"COPY to {dest}: a relative destination needs a WORKDIR with an absolute path before it")
    else:
        target = posixpath.normpath(posixpath.join(workdir, dest))
    into = dest.endswith("/") or len(sources) > 1 or any(is_dir for _, _, is_dir in sources) or target in dirs
    if into:
        _add_dir(dirs, target)
    return _Copy(number, tuple(sources), target, into)


def _copied_file(copy: _Copy, target: str, context: Path, ignore_rules: tuple[dockerignore.Rule, ...]) -> Path | None:
    """The file in the build context `copy` puts at `target` in the image, or None if it puts none there."""
    if any(target == posixpath.join(copy.dest, binary) for binary in copy.uv_binaries):
        raise DockerfileError(f"line {copy.line}: {target} is a uv binary from another image, not a requirements file")
    root = context.resolve()
    for rel, host, is_dir in reversed(copy.sources):  # a later source overwrites an earlier one
        if not is_dir:
            if copy.into and target == posixpath.join(copy.dest, posixpath.basename(rel)):
                return host
            if not copy.into and target == copy.dest:
                return host
            if not copy.into and target == posixpath.join(copy.dest, posixpath.basename(rel)):
                raise DockerfileError(
                    f"line {copy.line}: can't tell whether {copy.dest} is a folder in the image: "
                    f"end the destination with / (or set WORKDIR {copy.dest} first)"
                )
        elif target.startswith(copy.dest.rstrip("/") + "/"):
            found = host / posixpath.relpath(target, copy.dest)
            if os.path.lexists(found) and (found.is_symlink() or not found.resolve().is_relative_to(root)):
                raise DockerfileError(
                    f"line {copy.line}: {found.relative_to(context)} is a symlink, "
                    "which --dockerfile-inputs doesn't read"
                )
            if found.is_file():
                # A folder COPY keeps a linked folder as a link, and the rules may leave out what it points to.
                if ignore_rules and (link := _symlink_below(found, context)) is not None:
                    raise DockerfileError(
                        f"line {copy.line}: {link.as_posix()} is a symlink, which --dockerfile-inputs doesn't read "
                        "when an ignore file has rules"
                    )
                relative = found.relative_to(context).as_posix()
                if rule := dockerignore.ignored(relative, ignore_rules):
                    raise DockerfileError(
                        f"line {copy.line}: COPY {rel}: {relative} is excluded from the build context by {rule}"
                    )
                return found
    return None


def _copies_config(copy: _Copy, names: tuple[str, ...]) -> bool:
    """Whether COPY can put an installer configuration file anywhere in the image."""
    for rel, host, is_dir in copy.sources:
        if is_dir:
            for _, dirnames, filenames in os.walk(host):
                if any(name.lower() in names for name in (*dirnames, *filenames)):
                    return True
        # Not knowing whether dest is a folder in the image, either name may be the one the file gets.
        elif any(posixpath.basename(name).lower() in names for name in (rel, copy.dest)):
            return True
    return names == _PIP_CONFIG_NAMES and (copy.dest + "/").startswith(_PIP_CONFIG)


def _requirement_includes(host: Path, number: int) -> list[tuple[bool, str]]:
    """The files a requirements or constraints file includes with -r or -c, as (whether it's a -c, the path as
    written), found on the same lines and with the same reader (`_line_include`) as `_read_requirements` in
    sources.py, which reads the file afterwards.
    An include wheelgap can't follow from the image back to the build context is a DockerfileError: a URL, a
    variable, an absolute path (pip reads it in the image; the reader would read that path on this machine), an
    abbreviated option, or one on a line with an editable."""
    includes = []
    for raw in _join_continued_lines(host.read_text(encoding="utf-8-sig")):
        line = _COMMENT_RE.split(raw, 1)[0].strip()
        if not line.startswith("-"):
            continue
        try:
            tokens = shlex.split(line)  # as pip reads a line of options
        except ValueError:
            raise DockerfileError(f"line {number}: {host.name}: pip can't split this line: {line}") from None
        try:
            include = _line_include(tokens, host.name)
        except SourceError as e:  # names the file itself
            raise DockerfileError(f"line {number}: {e}") from None
        if any(_abbreviated_include(token) for token in tokens) or (
            include is None and any(token.startswith(("-r", "-c", "--requirement", "--constraint")) for token in tokens)
        ):
            raise DockerfileError(f"line {number}: {host.name} has an unreadable -r or -c: {line}")
        if include is None:
            continue
        name = include[1]
        if name.startswith("/"):
            raise DockerfileError(
                f"line {number}: {host.name} includes {name} by an absolute path, which --dockerfile-inputs "
                "doesn't read: write it relative to the file"
            )
        if any(text in name for text in (":", "${", "\\")):
            raise DockerfileError(f"line {number}: {host.name} has a non-local include: {name}")
        parts = name.split("/")
        if any(part == ".." and parts[i - 1] != ".." for i, part in enumerate(parts) if i):
            # pip opens `sub/../base.txt` through `sub`, which the image may not have.
            raise DockerfileError(f"line {number}: {host.name} has an include with .. after a folder name: {name}")
        if any(token.startswith(("-r=", "-c=")) for token in tokens):
            raise DockerfileError(f"line {number}: {host.name} has an unreadable -r or -c: {line}")
        includes.append((include[0], name))
    return includes


def _find_copied_file(
    copies: list[_Copy], target: str, context: Path, number: int, ignore_rules: tuple[dockerignore.Rule, ...]
) -> Path:
    """The file in the build context that the COPY instructions in `copies` leave at `target` in the image: the last
    one that puts a file there wins, as in the build. A file the ignore rules leave out of the build context is a
    DockerfileError (`_copied_file`), so nothing the image doesn't have is returned."""
    for copy in reversed(copies):
        if copy.error:
            raise DockerfileError(f"line {copy.line}: {copy.error}")
        if (host := _copied_file(copy, target, context, ignore_rules)) is not None:
            return host
    raise DockerfileError(f"line {number}: {target} isn't copied from the build context by a COPY before this line")


def _symlink_below(host: Path, context: Path) -> Path | None:
    """The outermost part of `host` below the build context that is a symlink, relative to the context. The context
    folder itself may be one: Docker and wheelgap both read through it."""
    relative = host.relative_to(context)
    for part in reversed([relative, *relative.parents][:-1]):
        if (context / part).is_symlink():
            return part
    return None


def _check_includes(
    host: Path,
    target: str,
    copies: list[_Copy],
    context: Path,
    number: int,
    ignore_rules: tuple[dockerignore.Rule, ...],
    *,
    uv: bool = False,
    constraints_file: bool = False,
) -> tuple[Path, ...]:
    """The files that `host` (at `target` in the image) includes with -r or -c, at every depth, each checked to be the
    file the image has where pip looks for it. pip resolves an include beside the including file in the image;
    `_read_requirements` resolves it beside the file in the build context. So each include is followed in the image
    (`_find_copied_file`) and must come back to that same file in the context: one that isn't copied, or where a
    COPY puts another file, or that the ignore rules leave out of the build context, is a DockerfileError, and nothing
    the image doesn't have is read.

    For `uv` (a `uv pip install` line; `constraints_file` says `host` is its `-c FILE`), a -r inside a constraints
    file is a DockerfileError too: pip installs what that file names, and the requirements reader follows pip, but uv
    only constrains with it (uv 0.13.1: `-c c.txt` where c.txt has `-r more.txt` installs nothing from more.txt)."""
    found: dict[Path, None] = {}
    active: set[Path] = set()
    checked: set[tuple[Path, str, bool]] = set()

    def no_symlink(file: Path) -> None:
        # A symlinked folder in a copied folder stays a link in the image, where it may point somewhere else: it isn't
        # followed. A symlinked file isn't either: stricter than pip needs now that the reader takes a link's
        # includes from the link's folder (#618), but nothing is read through a link that way.
        if (link := _symlink_below(file, context)) is not None:
            raise DockerfileError(
                f"line {number}: {link} is a symlink, which --dockerfile-inputs doesn't read "
                "for a file with a -r or -c, or a file one names"
            )

    def visit(current: Path, image: str, uv_constraints: bool) -> None:
        if (current, image, uv_constraints) in checked:
            return
        active.add(current)
        includes = _requirement_includes(current, number)
        if includes:
            no_symlink(current)
        for constraint, include in includes:
            if uv_constraints and not constraint:
                raise DockerfileError(
                    f"line {number}: {current.name} is read as constraints and has -r {include}: pip installs what "
                    "that file names and uv only constrains with it, which --dockerfile-inputs doesn't read for uv"
                )
            image_path = posixpath.normpath(posixpath.join(posixpath.dirname(image), include))
            copied = _find_copied_file(copies, image_path, context, number, ignore_rules)
            no_symlink(copied)
            expected = Path(os.path.abspath(current.parent / include))
            if Path(os.path.abspath(copied)) != expected or (current.parent / include).resolve() != copied.resolve():
                raise DockerfileError(f"line {number}: {current.name} includes {include}, but COPY puts {copied} there")
            if copied in active:
                raise DockerfileError(f"line {number}: requirements include cycle through {include}")
            found[copied] = None
            visit(copied, image_path, uv and (uv_constraints or constraint))
        active.remove(current)
        checked.add((current, image, uv_constraints))

    visit(host, target, uv and constraints_file)
    return tuple(found)


# `pip install` flags that don't change which files pip picks: read and ignored. Spelled out in full only, since pip
# also takes any unambiguous prefix of a long option and wheelgap doesn't guess at those.
_PIP_HARMLESS_FLAGS = (
    "--no-cache-dir",
    "--upgrade",
    "-U",
    "--disable-pip-version-check",
    "--no-input",
    "-q",
    "--quiet",
)
_PIP_ROOT_USER_ACTIONS = ("warn", "ignore")  # pip's choices for --root-user-action; it exits on any other value
_PIP_FLAGS_READ = (
    "-r FILE, -c FILE, --no-cache-dir, --upgrade (-U), --disable-pip-version-check, --root-user-action, --no-input, "
    "--quiet (-q) and, with pinned requirements, --prefer-binary"
)


# `uv pip install` flags that are read. uv has its own flags: of pip's harmless ones above it takes --upgrade (-U) and
# --quiet (-q) with the same meaning, it has no --no-input, --root-user-action or --prefer-binary, and it takes
# --disable-pip-version-check only to warn that it does nothing (`uv pip install --help`, uv 0.13.1).
_UV_HARMLESS_FLAGS = ("--no-cache", "--no-cache-dir", "--upgrade", "-U", "-q", "--quiet")
_UV_FLAGS_READ = "--system, -r FILE, -c FILE, --no-cache, --no-cache-dir, --upgrade (-U) and --quiet (-q)"
_UV_REQUIREMENTS_SUFFIXES = (".txt", ".in")


def _refuse_uv_option_lines(file: Path, number: int) -> None:
    """A file `uv pip install` reads may only hold requirements and plain `-r FILE` / `-c FILE` lines. The reader
    follows pip for every other option line, and uv differs: with --extra-index-url uv takes the first index that
    has the package where pip takes the best file of them all, and it keeps --no-binary and --only-binary as two
    sets with no `:none:` (uv 0.13.9: `--no-binary :all:` then `--no-binary :none:` builds from source; pip takes
    the wheel). The include itself must be spelled as both read it: uv has no `-rFILE`, pip's optparse reads
    `-r=FILE` as a file named `=FILE`, and uv doesn't take quotes or escapes out of a line as pip's shlex does.
    A requirement with its own options (`six==1.16.0 --hash=...`) isn't an option line."""
    for raw in _join_continued_lines(file.read_text(encoding="utf-8-sig")):
        line = _COMMENT_RE.split(raw, 1)[0].strip()
        if not line.startswith("-"):
            continue
        try:
            tokens = shlex.split(line)  # as _requirement_includes, which has already split every such line
        except ValueError:
            tokens = []
        option = re.sub(r"[^A-Za-z-].*", "", line.split()[0])  # the option's name: no value, so never a URL
        if tokens == line.split() and (
            (len(tokens) == 2 and tokens[0] in ("-r", "-c", "--requirement", "--constraint"))
            or (len(tokens) == 1 and "=" in tokens[0] and option in ("--requirement", "--constraint"))
        ):
            continue
        if not option.startswith("--"):
            option = option[:2]  # `-ihttps://user:secret@host/simple`: never echo the URL
        raise DockerfileError(
            f"line {number}: {file.name} has a {option} line, and uv reads a requirements file's options by its own "
            "rules: --dockerfile-inputs only reads requirements and -r FILE / -c FILE lines in a file uv installs"
        )


# --dockerfile-inputs, RUN lines. A shell-form RUN is one string handed to `/bin/sh -c` as written (dispatchRun and
# withShell in dockerfile2llb/convert.go: the shell, then the args joined; no Dockerfile-level substitution), with the
# stage's ENV values and the ARGs that have a value in its environment (dispatchArg: `d.state.AddEnv` only when
# `arg.Value != nil`).
# https://github.com/moby/buildkit/blob/dd75b4955087a5a2d52188d6ce25b509b323280e/frontend/dockerfile/dockerfile2llb/convert.go
# wheelgap splits that string into simple commands with the POSIX shell's token rules
# (https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html, 2.2 Quoting, 2.3 Token Recognition,
# 2.6 Word Expansions, 2.9 Shell Commands) for the subset below, and stops on what it doesn't model.
class _ShellError(Exception):
    """A RUN the tokenizer doesn't read: a heredoc, an unterminated quote or substitution."""


_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9_./:=,@%+-]+", re.ASCII)  # expands to itself, unquoted: one field, no glob
_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=", re.ASCII)
_SHELL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_OPERATORS = ("&&", "||", ";;", "|&", ";", "|", "&", "(", ")", "\n")
_REDIRECTS = ("<<<", "<<-", "&>>", "<<", ">>", ">&", "<&", ">|", "<>", "&>", ">", "<")


@dataclass
class _Word:
    text: str  # as the shell would pass it, where `exact`; else as written, for messages and for name checks
    exact: bool = True  # False: an expansion wheelgap can't resolve, a glob, a tilde or a brace list is in it
    brackets: bool = False  # not exact only for an unquoted [...], as in `pip install uvicorn[standard]`
    variables: tuple[str, ...] = ()  # the variables whose known value is in `text`
    assigns: str | None = None  # NAME, for a word that starts with an unquoted NAME=


@dataclass
class _Command:
    words: list[_Word] = field(default_factory=list)
    assigns: list[tuple[str, _Word]] = field(default_factory=list)  # NAME=value words before the command name
    redirects: list[tuple[str, _Word]] = field(default_factory=list)
    before: str = ""  # the operator before it: "", "&&", "||", ";", "|", "&", "(", ")"
    after: str = ""
    nested: bool = False  # inside a command substitution, or the string of `sh -c`
    subshell: bool = False  # inside ( )


class _Shell:
    """Splits a shell command line into simple commands. `env` holds the variables whose value is known; one set by
    an assignment or `export` on the line is followed, and any other way a variable can change (`read`, `for`,
    `unset`, an assignment whose value isn't known) forgets it."""

    def __init__(self, text: str, env: Mapping[str, str]) -> None:
        self.text = text
        self.pos = 0
        self.env = dict(env)
        self.commands: list[_Command] = []
        self.unknown: set[str] = set()  # variables the line changed to a value wheelgap doesn't know
        self.forgotten = False  # an eval or a sourced file may have set any variable
        self.parens = False  # a function definition is on the line
        self.depth = 0  # of ( ) subshells
        self.used: list[str] = []  # the variables expanded in the word being read

    def parse(self, nested: bool = False) -> list[_Command]:
        text = self.text
        command = _Command(nested=nested)
        before = ""
        while True:
            while self.pos < len(text) and text[self.pos] in " \t":
                self.pos += 1
            if text.startswith("\\\n", self.pos):
                self.pos += 2
                continue
            if self.pos >= len(text):
                break
            ch = text[self.pos]
            if ch == "#":  # a comment: only where a word would start
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end
                continue
            if operator := next((op for op in _OPERATORS if text.startswith(op, self.pos)), None):
                if operator == "&" and text.startswith(("&>", "&>>"), self.pos):
                    operator = None
            if operator:
                self.pos += len(operator)
                paren = operator if operator in "()" else ""
                operator = {"\n": ";", ";;": ";", "|&": "|", "(": ";", ")": ";"}.get(operator, operator)
                if command.words or command.assigns or command.redirects:
                    if paren == "(":
                        self.parens = True  # `name()`: a function definition, whose body runs when it is called
                    self._finish(command, before, operator)
                    command = _Command(nested=nested)
                    before = operator
                elif operator != ";":
                    before = operator
                self.depth = max(self.depth + {"(": 1, ")": -1}.get(paren, 0), 0)
                continue
            if redirect := self._redirect():
                command.redirects.append(redirect)
                continue
            start = self.pos
            word = self._word()
            if not command.words and (m := _ASSIGNMENT_RE.match(text, start)) and m.end() <= self.pos:
                value = _Word(word.text[len(m.group(0)) :], word.exact)
                command.assigns.append((m.group(1), value))
            else:
                if m := _ASSIGNMENT_RE.match(text, start):
                    word.assigns = m.group(1)
                command.words.append(word)
        if command.words or command.assigns or command.redirects:
            self._finish(command, before, "")
        return self.commands

    def _finish(self, command: _Command, before: str, after: str) -> None:
        command.before, command.after = before, after
        command.subshell = self.depth > 0
        self.commands.append(command)
        # Follow what the line itself sets, so `VERSION=1.2 && pip install x==$VERSION` reads as the shell runs it.
        name = command.words[0].text if command.words and command.words[0].exact else None
        if not command.words:
            for variable, value in command.assigns:
                self._set(variable, value.text if value.exact else None)
        elif name in ("export", "readonly", "local", "declare", "typeset"):
            for word in command.words[1:]:
                if word.assigns:
                    self._set(word.assigns, word.text.partition("=")[2] if word.exact else None)
        elif name in ("read", "unset", "for", "getopts", "eval", "source", ".", "set", "shift"):
            for word in command.words[1:]:
                self._set(word.text, None)
            if name in ("eval", "source", "."):
                self.env.clear()  # it may set anything
                self.forgotten = True

    def _set(self, name: str, value: str | None) -> None:
        if value is None:
            self.env.pop(name, None)
            self.unknown.add(name)
        else:
            self.env[name] = value
            self.unknown.discard(name)

    def _redirect(self) -> tuple[str, _Word] | None:
        text = self.text
        m = re.compile(r"\d*").match(text, self.pos)
        at = m.end() if m else self.pos
        operator = next((op for op in _REDIRECTS if text.startswith(op, at)), None)
        if operator is None:
            return None
        if operator in ("<<", "<<-"):
            raise _ShellError("a heredoc")
        self.pos = at + len(operator)
        while self.pos < len(text) and text[self.pos] in " \t":
            self.pos += 1
        return operator, self._word()

    def _word(self) -> _Word:
        text = self.text
        parts: list[str] = []
        exact = True
        brackets = False
        start = self.pos
        self.used = []
        while self.pos < len(text):
            ch = text[self.pos]
            if ch in " \t\n;|&()<>":
                break
            if ch == "\\":
                if text.startswith("\\\n", self.pos):
                    self.pos += 2
                    continue
                parts.append(text[self.pos + 1 : self.pos + 2])
                self.pos += 2
            elif ch == "'":
                end = text.find("'", self.pos + 1)
                if end < 0:
                    raise _ShellError("an unterminated ' quote")
                parts.append(text[self.pos + 1 : end])
                self.pos = end + 1
            elif ch == '"':
                self.pos += 1
                while True:
                    if self.pos >= len(text):
                        raise _ShellError('an unterminated " quote')
                    ch = text[self.pos]
                    if ch == '"':
                        self.pos += 1
                        break
                    if ch == "\\" and text[self.pos + 1 : self.pos + 2] in ('"', "$", "\\", "`", "\n"):
                        if text[self.pos + 1] != "\n":
                            parts.append(text[self.pos + 1])
                        self.pos += 2
                    elif ch in "$`":
                        value, known = self._expansion(quoted=True)
                        parts.append(value)
                        exact = exact and known
                    else:
                        parts.append(ch)
                        self.pos += 1
            elif ch in "$`":
                value, known = self._expansion(quoted=False)
                parts.append(value)
                exact = exact and known
            else:
                # A glob, a tilde or (in bash) a brace list is expanded by the shell: the word isn't what is written.
                if ch == "[" and re.compile(r"\[[^\s\]]+\]").match(text, self.pos):
                    brackets = True
                elif ch in "*?" or (ch == "~" and self.pos == start) or (ch == "{" and self._brace_list()):
                    exact = False
                parts.append(ch)
                self.pos += 1
        return _Word("".join(parts), exact and not brackets, exact and brackets, tuple(self.used))

    def _brace_list(self) -> bool:
        m = re.compile(r"\{[^\s{}]*(?:,|\.\.)[^\s{}]*\}").match(self.text, self.pos)
        return m is not None

    def _matching(self, opener: str, closer: str) -> str:
        """The text up to the `closer` that matches the opener just read, quotes and nesting counted."""
        text = self.text
        depth = 1
        start = self.pos
        while self.pos < len(text):
            ch = text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            if ch == "'":
                end = text.find("'", self.pos + 1)
                if end < 0:
                    break
                self.pos = end + 1
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return text[start : self.pos - 1]
            self.pos += 1
        raise _ShellError(f"an unterminated {opener}")

    def _expansion(self, quoted: bool) -> tuple[str, bool]:
        """A `$...` or backquote expansion at self.pos: its value and True if known, else its text and False."""
        text = self.text
        start = self.pos
        if text[self.pos] == "`":
            end = self.pos + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                raise _ShellError("an unterminated ` substitution")
            self._substitution(text[self.pos + 1 : end])
            self.pos = end + 1
            return text[start : self.pos], False
        self.pos += 1
        if text.startswith("((", self.pos):
            self.pos += 1
            self._matching("(", ")")
            return text[start : self.pos], False
        if text.startswith("(", self.pos):
            self.pos += 1
            self._substitution(self._matching("(", ")"))
            return text[start : self.pos], False
        if text.startswith("{", self.pos):
            self.pos += 1
            inner = self._matching("{", "}")
            m = _SHELL_NAME_RE.match(inner)
            name = m.group(0) if m else ""
            rest = inner[len(name) :]
            if name and name in self.env and not self.forgotten:
                value = self.env[name]
                # ${NAME}, and ${NAME:-word} / ${NAME-word} when NAME is set and not empty: NAME's value.
                if value and (not rest or rest.startswith((":-", "-"))) and (quoted or _SAFE_VALUE_RE.fullmatch(value)):
                    self.used.append(name)
                    return value, True
            return text[start : self.pos], False
        m = _SHELL_NAME_RE.match(text, self.pos)
        if m is None:
            if self.pos < len(text) and text[self.pos] in "@*#?-$!0123456789":
                self.pos += 1
                return text[start : self.pos], False
            return "$", True  # a lone $ is a $
        self.pos = m.end()
        name = m.group(0)
        if name in self.env and not self.forgotten:
            value = self.env[name]
            if quoted or _SAFE_VALUE_RE.fullmatch(value):
                self.used.append(name)
                return value, True
        return text[start : self.pos], False

    def _substitution(self, inner: str) -> None:
        used = self.used
        sub = _Shell(inner, self.env)
        self.commands.extend(sub.parse(nested=True))
        self.used = used


# What a command that isn't a pip or uv install is taken to be, by its name. None of these installs Python packages
# with pip or uv, so a RUN made of them is passed over, and reported by class (InputsReport.passed_over):
# system package and account commands,
_SYSTEM_COMMANDS = frozenset(
    "apt-get apt apt-cache apt-key apt-mark add-apt-repository dpkg dpkg-reconfigure debconf-set-selections apk yum "
    "dnf microdnf zypper rpm yum-config-manager install_packages useradd adduser groupadd addgroup usermod groupmod "
    "userdel deluser chpasswd locale-gen update-locale localedef update-ca-certificates update-alternatives ldconfig "
    "fc-cache gpg gpgconf setcap".split()
)
# shell builtins and file commands,
_SHELL_COMMANDS = frozenset(
    ": true false echo printf cd export set unset test [ [[ exit umask ulimit shopt wait sleep pwd readonly local "
    "return break continue read trap hash type which mkdir chmod chown chgrp rm rmdir cp mv ln touch cat ls sed awk "
    "grep egrep fgrep head tail tee sort uniq cut tr wc tar unzip gunzip gzip xz bzip2 zip install date basename "
    "dirname realpath readlink id whoami uname mktemp du df stat file diff patch dd truncate md5sum sha1sum sha256sum "
    "sha512sum nproc arch getent hostname yes seq lsb_release dpkg-query dpkg-architecture".split()
)
# commands that only read: the files they name can't change (their redirects still can),
_READ_COMMANDS = frozenset(
    "echo printf test [ [[ cat ls grep egrep fgrep head tail sort uniq cut wc basename dirname realpath readlink stat "
    "file diff md5sum sha1sum sha256sum sha512sum du df which type".split()
)
# commands that don't change what is in a file (rm isn't one: an install of a file it removed fails the build),
_UNCHANGING_COMMANDS = _READ_COMMANDS | frozenset("cd mkdir chmod chown chgrp find touch export".split())
# and other programs: downloads, version control, compilers and build tools, JavaScript tooling. They can run a
# project's own scripts (`npm run build`), which wheelgap doesn't look into: the note names each program passed over.
_PROGRAMS = frozenset(
    "curl wget git git-lfs npm npx yarn pnpm node corepack bun deno go cargo rustup rustc gcc g++ cc c++ clang cmake "
    "ninja java javac mvn gradle dotnet ruby gem bundle composer php openssl ssh-keygen ssh-keyscan jq envsubst strip "
    "ldd pkg-config autoreconf autoconf automake playwright".split()
)
# Tools that install Python packages in ways --dockerfile-inputs doesn't read: a line running one is an error.
_INSTALLERS = frozenset(
    "poetry pdm pipenv conda mamba micromamba pipx easy_install hatch pixi rye uvx pip-sync flit".split()
)
_SHELLS = frozenset("sh bash dash ash zsh ksh".split())
# A distribution's own build of a Python package (Alpine's py3-yaml, Debian's python3-numpy), as against the
# interpreter's own pieces.
_SYSTEM_PYTHON_RE = re.compile(r"(?:py3|python3?(?:\.\d+)?)-([a-z0-9][a-z0-9.+_-]*)", re.ASCII)
_SYSTEM_PYTHON_TOOLS = frozenset(
    "dev devel pip venv virtualenv setuptools wheel distutils minimal full tk dbg doc libs apt pkg-resources "
    "is-python3 software-properties".split()
)
# A `pip install` of only these, with no -r FILE, sets up pip itself: passed over, with a note that says they aren't
# checked and that the pip the later lines run isn't known. (Each has only ever shipped a py3-none-any wheel; any
# other tool a line installs, uv or poetry say, is a requirement like the rest.)
_INSTALLER_TOOLS = frozenset("pip setuptools wheel".split())
_BIN_DIRS = frozenset({"/bin", "/usr/bin", "/sbin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin"})
PASSED_OVER = {
    "system-packages": "system package and account commands",
    "shell": "shell and file commands",
    "programs": "other programs",
    "venv": "virtual environment set-up",
    "cache-mounts": "cache mounts",
}

# Environment variables that don't change which files pip picks, by pip's rule that PIP_<OPTION> sets the option
# --<option> (https://pip.pypa.io/en/stable/topics/configuration/#environment-variables): the cache, what is printed,
# prompts, network patience, and where pip may write. Every other PIP_ name is an error.
_PIP_HARMLESS_ENV = frozenset(
    "PIP_NO_CACHE_DIR PIP_CACHE_DIR PIP_DISABLE_PIP_VERSION_CHECK PIP_ROOT_USER_ACTION PIP_NO_INPUT PIP_PROGRESS_BAR "
    "PIP_NO_COLOR PIP_DEFAULT_TIMEOUT PIP_TIMEOUT PIP_RETRIES PIP_QUIET PIP_VERBOSE PIP_NO_WARN_SCRIPT_LOCATION "
    "PIP_NO_COMPILE PIP_COMPILE PIP_BREAK_SYSTEM_PACKAGES PIP_NO_PYTHON_VERSION_WARNING".split()
)
# The same for uv (https://docs.astral.sh/uv/reference/environment/): the cache, bytecode, how files are linked,
# what is printed, network patience, where uv keeps Pythons and tools, and UV_PROJECT_ENVIRONMENT, which only
# project commands (`uv sync`, not read) use. UV_SYSTEM_PYTHON is read as --system. UV_VERSION isn't a uv setting
# (not in that reference, nor a string in the uv 0.13.1 binary): Dockerfiles use the name for the uv they install.
_UV_HARMLESS_ENV = frozenset(
    "UV_NO_CACHE UV_CACHE_DIR UV_COMPILE_BYTECODE UV_LINK_MODE UV_NO_PROGRESS UV_HTTP_TIMEOUT UV_HTTP_RETRIES "
    "UV_CONCURRENT_DOWNLOADS UV_CONCURRENT_BUILDS UV_CONCURRENT_INSTALLS UV_PYTHON_DOWNLOADS UV_PYTHON_INSTALL_DIR "
    "UV_TOOL_BIN_DIR UV_TOOL_DIR UV_INSTALL_DIR UV_BREAK_SYSTEM_PACKAGES UV_PROJECT_ENVIRONMENT UV_SYSTEM_PYTHON "
    "UV_VERSION".split()
)
# Words that name a pip or uv configuration file: a command that isn't read and names one may be writing it.
_CONFIG_WORDS = ("pip.conf", "pip.ini", ".config/pip", ".pip/", "uv.toml", ".netrc", "pydistutils.cfg", "sitecustomize")
_SHELL_RE = re.compile(
    r'\[\s*"(?:/bin/|/usr/bin/)?(?:sh|bash|dash|ash)"\s*,(?:\s*"(?:-[euxo]+|pipefail)"\s*,)*\s*"-[euxo]*c"\s*\]'
)


def _installer_setting(name: str) -> bool:
    """Whether an environment variable is a pip or uv setting that may change what is installed. pip reads a name
    that starts with PIP_ (in capitals) and lowercases the rest; uv's names are all capitals."""
    return name.startswith(("PIP_", "UV_")) and name.upper() not in _PIP_HARMLESS_ENV | _UV_HARMLESS_ENV


@dataclass
class InputsReport:
    """What --dockerfile-inputs didn't read, for the caller to say: the answer is the packages of the install lines
    wheelgap can read, and this is the rest."""

    # RUN lines passed over because nothing on them installs Python packages with pip or uv, by class (the keys of
    # PASSED_OVER, and "installer-tools").
    passed_over: dict[str, list[int]] = field(default_factory=dict)
    programs: list[str] = field(default_factory=list)  # the programs of the "programs" class, in order
    tools: list[str] = field(default_factory=list)  # what the "installer-tools" lines install
    # (line, what it runs) for lines that may install Python packages wheelgap can't see: a script, make, a program
    # it doesn't know. The answer doesn't cover them, and the caller must say so where it can't be missed.
    warnings: list[tuple[int, str]] = field(default_factory=list)
    notes: list[tuple[int, str, str]] = field(default_factory=list)  # (line, kind, message)
    # (line, the requirements a `pip install NAME...` on it names, whether uv installs them), in order.
    named: list[tuple[int, list[str], bool]] = field(default_factory=list)
    # (line, name) for each Python package a system package command installs (apk add py3-yaml, apt-get install
    # python3-numpy): pip keeps one that satisfies a requirement, where wheelgap checks a fresh install.
    system_python: list[tuple[int, str]] = field(default_factory=list)

    def passed(self, kind: str, number: int) -> None:
        lines = self.passed_over.setdefault(kind, [])
        if number not in lines:
            lines.append(number)


@dataclass
class _Install:
    """One `pip install` or `uv pip install` command of a RUN."""

    uv: bool
    files: list[str]
    constraints: list[str]
    prefer_binary: bool
    names: list[str]  # requirements written on the command line: `pip install NAME==VERSION`
    cwd: str | None  # the folder a relative FILE is in, or None with cwd_error
    cwd_error: str | None
    seen: int = 0  # how many of the reader's mentions come before it


def _requirement_name(text: str) -> str | None:
    """The normalised project name of a requirement written on a command line, or None if it isn't a plain one."""
    m = re.fullmatch(
        r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9._,-]*\])?\s*((?:[<>=!~]=?|===)[A-Za-z0-9.*+!,<>=~ -]+)?", text
    )
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


class _RunReader:
    """Reads the RUN instructions of the stages --dockerfile-inputs follows. Each command of a RUN is one of: a pip
    or uv install that is read (`installs`), a command that can't install Python packages (passed over, by class),
    a command wheelgap can't see into (a warning), or one that installs in a way that isn't read (a DockerfileError).
    """

    def __init__(self, report: InputsReport) -> None:
        self.report = report
        self.venvs: set[str] = set()  # folders `python -m venv` made: their bin/pip is the image's Python's
        # (line, word, folder) for each word of a command that wasn't read and may name a file it writes.
        self.mentions: list[tuple[int, str, str | None]] = []  # with the folder the command ran in, if known
        self.config_error: str | None = None  # a command that may have written a pip or uv config file

    def read(
        self,
        number: int,
        instruction: str,
        args: str,
        env: Mapping[str, str],
        workdir: str | None,
        workdir_error: str | None,
        uv_system: bool,
        arg_values: Mapping[str, bool] | None = None,
    ) -> list[_Install]:
        """The installs of one RUN, in order. `arg_values` names the variables in `env` that are ARGs, with whether
        --build-arg gave the value: an install that uses one gets a note, since another build may set another."""

        def stop(message: str) -> NoReturn:
            raise DockerfileError(f"line {number}: {message}")

        rest = _args_after_flags(args, "\\").strip(_GO_SPACES)
        if not args.endswith(rest) or any(c in args[: len(args) - len(rest)] for c in "\"'\\"):
            stop("RUN flags with quotes or escapes aren't read by --dockerfile-inputs")
        mounts = set()
        for flag in args[: len(args) - len(rest)].split():
            if flag.startswith("--mount="):
                # type= defaults to bind (instructions/commands_runmount.go, parseMount).
                fields = dict(part.partition("=")[::2] for part in flag.removeprefix("--mount=").split(","))
                mounts.add(fields.get("type", "bind"))
                if "$" in fields.get("type", ""):
                    stop("RUN --mount with a variable for its type isn't read by --dockerfile-inputs")
            elif not flag.startswith(("--network=", "--security=")):
                stop(f"RUN {flag.split('=')[0]} isn't a RUN flag --dockerfile-inputs reads")
        if mounts & {"cache", "tmpfs"}:
            self.report.passed("cache-mounts", number)
        # A bind mount can put a requirements file in front of the RUN with no COPY, and a secret or an ssh agent
        # can carry an index's credentials or a pip.conf: an install under one isn't read.
        special = sorted(mounts - {"cache", "tmpfs"})
        parser: _Shell | None = None
        if _is_exec_form(instruction, rest):
            commands = [_Command(words=[_Word(item) for item in json.loads(rest)])]
        elif _heredocs(instruction):
            self.report.warnings.append((number, "a heredoc script"))
            return []
        else:
            parser = _Shell(rest, env)
            try:
                commands = parser.parse()
            except _ShellError as e:
                stop(f"this RUN has {e}, which --dockerfile-inputs doesn't read")
        # `sh -c 'COMMANDS'`: the string is a command line of its own, read like one in a command substitution.
        index = 0
        while index < len(commands):
            words = commands[index].words
            if (
                len(words) == 3
                and all(word.exact for word in words)
                and posixpath.basename(words[0].text) in _SHELLS
                and re.fullmatch(r"-[eux]*c", words[1].text)
            ):
                try:
                    inner = _Shell(words[2].text, parser.env if parser else {}).parse(nested=True)
                except _ShellError as e:
                    stop(f"this RUN has {e}, which --dockerfile-inputs doesn't read")
                commands[index : index + 1] = inner or [_Command(words=[_Word(":")])]
            else:
                index += 1
        installs: list[_Install] = []
        cwd, cwd_error = workdir, workdir_error
        depth = 0  # inside if, for, while, until or { }
        risky_setting: str | None = None
        names_installer = False  # a pip or uv command, read or not
        groups = self._and_or_lists(commands)
        for index, command in enumerate(commands):
            words = list(command.words)
            texts = [word.text for word in words]
            assigns = list(command.assigns)
            conditional = None
            # Reserved words (2.4): what follows `if`, `then`, `do`... is a command like any other, but it may not run.
            while words and words[0].exact:
                reserved = words[0].text
                if reserved in ("if", "while", "until", "{"):
                    depth += 1
                elif reserved in ("fi", "done", "}"):
                    depth = max(depth - 1, 0)
                elif reserved not in ("then", "elif", "else", "do", "!", "time"):
                    break
                words = words[1:]
            while words and words[0].assigns and words[0].exact is not None:  # `then NAME=value command`
                assigns.append((words[0].assigns, _Word(words[0].text.partition("=")[2], words[0].exact)))
                words = words[1:]
            first = words[0].text if words and words[0].exact else None
            if first in ("case", "function", "select", "coproc") or (parser is not None and parser.parens):
                # `case` patterns and function bodies aren't split into commands the way the shell runs them.
                what = first if first in ("case", "function", "select", "coproc") else "name()"
                self._opaque_structure(number, commands, str(what), stop)
                return []
            if first == "for":
                depth += 1
                continue
            if depth:
                conditional = "inside if, for, while or { }"
            elif command.nested:
                conditional = "inside a command substitution or `sh -c`"
            elif command.subshell:
                conditional = "inside ( )"
            elif groups[index]:
                conditional = groups[index]
            # Wrappers that run the rest as the command.
            while words and words[0].exact and words[0].text in ("sudo", "exec", "command", "nice", "nohup", "env"):
                wrapper = words[0].text
                words = words[1:]
                if wrapper == "command" and words and words[0].text in ("-v", "-V"):
                    words = [_Word("type")]
                while wrapper == "env" and words and words[0].assigns:
                    assigns.append((words[0].assigns, _Word(words[0].text.partition("=")[2], words[0].exact)))
                    words = words[1:]
                if wrapper in ("sudo", "env") and words and words[0].text.startswith("-"):
                    words = [_Word(f"{wrapper} {words[0].text}", exact=False)]
            if words and words[0].exact and words[0].text == "export":
                assigns += [(w.assigns, _Word(w.text.partition("=")[2], w.exact)) for w in words[1:] if w.assigns]
            for name, _ in assigns:
                if _installer_setting(name):
                    risky_setting = name
            kind, detail = self._classify(number, words, cwd, uv_system, stop) if words else ("shell", "")
            if kind in ("install", "installer-tools", "pip"):
                names_installer = True
            if isinstance(detail, _Install):
                if conditional:
                    stop(f"`{' '.join(texts)}` is {conditional}, so --dockerfile-inputs can't tell whether it runs")
                if special:
                    stop(
                        f"RUN --mount=type={special[0]} with an install isn't read by --dockerfile-inputs: it can "
                        "bring files or credentials the Dockerfile doesn't show"
                    )
                if any(operator.endswith("<") for operator, _ in command.redirects):
                    stop(f"`{' '.join(texts)}` reads its input from a redirect, which --dockerfile-inputs doesn't read")
                detail.cwd, detail.cwd_error, detail.seen = cwd, cwd_error, len(self.mentions)
                for name in dict.fromkeys(name for word in command.words for name in word.variables):
                    if name in (arg_values or {}) and name in env:
                        how = (
                            f"--build-arg {name}={env[name]}"
                            if (arg_values or {})[name]
                            else f"the default of ARG {name}={env[name]}; pass --build-arg if the build sets one"
                        )
                        self.report.notes.append((number, "arg-value", f"this install uses {how}"))
                installs.append(detail)
            elif kind == "warning":
                assert isinstance(detail, str)
                self.report.warnings.append((number, detail))
            elif kind == "installer-tools":
                self.report.passed(kind, number)
                self.report.tools += [name for name in str(detail).split() if name not in self.report.tools]
            elif kind != "pip":
                self.report.passed(kind, number)
                if kind == "programs" and detail not in self.report.programs:
                    self.report.programs.append(str(detail))
            if first == "cd" and kind == "shell":
                target = words[1] if len(words) == 2 else None
                if conditional or command.subshell or target is None or not target.exact or target.text == "-":
                    cwd, cwd_error = None, f"line {number}: the folder after `{' '.join(texts)}` isn't known"
                elif target.text.startswith("/"):
                    cwd, cwd_error = posixpath.normpath(target.text), None
                elif cwd is not None:
                    cwd = posixpath.normpath(posixpath.join(cwd, target.text))
            elif first in ("pushd", "popd"):
                cwd, cwd_error = None, f"line {number}: the folder after `{first}` isn't known"
            # What this command may write to. A command that only reads can't change the files it names.
            written = [target.text for operator, target in command.redirects if ">" in operator]
            if kind not in ("install", "pip", "system-packages") and first not in _UNCHANGING_COMMANDS:
                written += [word.text for word in words[1:]]
            self.mentions += [(number, text, cwd) for text in written]
            if kind not in ("install", "pip") and (word := next((t for t in written if _names_config(t)), None)):
                self.config_error = (
                    f"line {number}: `{' '.join(texts)[:60]}` names {word}, which may be a pip or uv config file"
                )
        if risky_setting and names_installer:
            stop(f"{risky_setting} is set on this RUN and may change what pip or uv installs")
        return installs

    def check_untouched(self, number: int, host: Path, target: str | None, seen: int) -> None:
        """A requirements file an install line reads (`host`, at `target` in the image if that is known) must be the
        file COPY put there: a command before the install that wasn't read, and that names the file, a glob that
        matches it or a folder it is in, may have changed it."""
        for line, word, cwd in self.mentions[:seen]:
            named = host.name in word
            if not named and target is not None and (word.startswith("/") or cwd is not None):
                path = posixpath.normpath(posixpath.join(cwd or "/", word))
                named = fnmatch.fnmatchcase(target, path) or target.startswith(path.rstrip("/") + "/")
            if named:
                raise DockerfileError(
                    f"line {number}: {host.name} may have been changed by line {line}, which names {word} and "
                    "isn't read by --dockerfile-inputs"
                )

    @staticmethod
    def _and_or_lists(commands: list[_Command]) -> list[str | None]:
        """For each command, why it may not run or may fail unnoticed: its and-or list (2.9.3) has a `||`, it is
        part of a pipeline, or it runs in the background."""
        reasons: list[str | None] = [None] * len(commands)
        start = 0
        for index, command in enumerate(commands):
            if index and command.before not in ("&&", "||", "|"):
                start = index
            reason = None
            if command.before == "||" or command.after == "||":
                reason = "in a list with ||"
            elif command.before == "|" or command.after == "|":
                reason = "part of a pipeline"
            elif command.after == "&":
                reason = "run in the background"
            if reason:
                for other in range(start, len(commands)):
                    if other > start and commands[other].before not in ("&&", "||", "|"):
                        break
                    reasons[other] = reasons[other] or reason
        # A `||` later in the list also covers the commands before it.
        for index in range(len(commands) - 2, -1, -1):
            if commands[index + 1].before in ("&&", "||", "|") and reasons[index + 1] and not reasons[index]:
                reasons[index] = reasons[index + 1]
        return reasons

    def _opaque_structure(self, number: int, commands: list[_Command], what: str, stop) -> None:  # type: ignore[no-untyped-def]
        """A RUN with a structure wheelgap doesn't split: an error if any word names an installer, else a warning."""
        for command in commands:
            for word in command.words:
                base = posixpath.basename(word.text)
                if _PIP_RE.fullmatch(base) or _PYTHON_RE.fullmatch(base) or base in _INSTALLERS or base == "uv":
                    stop(
                        f"this RUN names {base} inside `{what}`, which --dockerfile-inputs doesn't read: "
                        "it can't tell what is installed"
                    )
        self.report.warnings.append((number, f"a `{what}` command list"))

    def _classify(self, number, words, cwd, uv_system, stop):  # type: ignore[no-untyped-def]
        """(class, detail) for one simple command: ("install", _Install), ("warning", what it runs), or a class it is
        passed over as. A command that installs Python packages in a way that isn't read is an error (`stop`)."""
        first = words[0]
        shown = " ".join(word.text for word in words)[:70]
        if not first.exact:
            return "warning", f"`{shown}`, a command name wheelgap can't resolve"
        name = first.text
        base = posixpath.basename(name)
        if "/" in name and posixpath.dirname(name) not in _BIN_DIRS:
            folder = posixpath.dirname(posixpath.dirname(posixpath.normpath(posixpath.join(cwd or "/", name))))
            in_venv = posixpath.basename(posixpath.dirname(name)) == "bin" and folder in self.venvs
            if (
                base in _INSTALLERS
                or base == "uv"
                or ((_PIP_RE.fullmatch(base) or _PYTHON_RE.fullmatch(base)) and not in_venv)
            ):
                stop(
                    f"`{shown}`: --dockerfile-inputs doesn't know which Python {name} belongs to (only pip, "
                    "python -m pip and uv on the PATH, or in a virtual environment `python -m venv` made on an "
                    "earlier line, are read)"
                )
            if not in_venv:
                return "warning", f"`{shown}`, a script or program at a path"
        args = words[1:]
        if _PIP_RE.fullmatch(base):
            return self._pip(number, args, False, uv_system, shown, stop)
        if _PYTHON_RE.fullmatch(base):
            while args and args[0].text in ("-u", "-B", "-E", "-s", "-I"):
                args = args[1:]
            if not args or args[0].text in ("-V", "--version", "-VV"):
                return "shell", ""
            option = args[0].text if args[0].exact else None
            if option is not None and option.startswith("-m") and (len(option) > 2 or len(args) > 1):
                module = option[2:] or (args[1].text if args[1].exact else None)
                rest = args[1:] if len(option) > 2 else args[2:]
                if module == "pip":
                    return self._pip(number, rest, False, uv_system, shown, stop)
                if module == "uv":
                    return self._uv(number, rest, uv_system, shown, stop)
                if module in ("venv", "virtualenv"):
                    folders = [word for word in rest if not word.text.startswith("-")]
                    if len(folders) == 1 and folders[0].exact and (cwd is not None or folders[0].text.startswith("/")):
                        self.venvs.add(posixpath.normpath(posixpath.join(cwd or "/", folders[0].text)))
                    return "venv", ""
                if module == "ensurepip":
                    return "installer-tools", "pip"
                if module == "compileall":
                    return "shell", ""
                return "warning", f"`{shown}`, a Python module wheelgap doesn't look into"
            if option is None or option.startswith("-"):
                return "warning", f"`{shown}`, Python code wheelgap doesn't look into"
            if posixpath.basename(option) == "setup.py":
                stop(
                    f"`{shown}` installs or builds a project from its setup.py, which --dockerfile-inputs doesn't read"
                )
            if posixpath.basename(option) == "get-pip.py":
                return "installer-tools", "pip"
            return "warning", f"`{shown}`, a Python script wheelgap doesn't look into"
        if base == "uv":
            return self._uv(number, args, uv_system, shown, stop)
        if base in _INSTALLERS:
            if [word.text for word in args] in (["--version"], ["-V"], ["--help"]):
                return "shell", ""
            stop(f"`{shown}`: {base} installs Python packages in a way --dockerfile-inputs doesn't read")
        if base in (".", "source") and len(args) == 1 and args[0].exact and args[0].text.endswith("/bin/activate"):
            return "venv", ""
        if base in _SHELLS or base in (".", "source", "eval", "make", "xargs"):
            return "warning", f"`{shown}`, which runs commands wheelgap can't see"
        if base == "find" and any(word.text in ("-exec", "-execdir", "-ok", "-okdir") for word in args):
            return "warning", f"`{shown}`, which runs commands wheelgap can't see"
        if base in _SYSTEM_COMMANDS:
            for word in args:
                if (m := _SYSTEM_PYTHON_RE.fullmatch(word.text)) and m.group(1) not in _SYSTEM_PYTHON_TOOLS:
                    self.report.system_python.append((number, word.text))
            return "system-packages", ""
        if base in _SHELL_COMMANDS or base == "find":
            return "shell", ""
        if base in _PROGRAMS:
            return "programs", base
        return "warning", f"`{shown}`, a program wheelgap doesn't know"

    def _uv(self, number, args, uv_system, shown, stop):  # type: ignore[no-untyped-def]
        texts = [word.text for word in args]
        if texts[:1] == ["pip"]:
            return self._pip(number, args[1:], True, uv_system, shown, stop)
        if texts in (["--version"], ["-V"], ["self", "version"], ["cache", "clean"], ["cache", "prune"]):
            return "shell", ""
        stop(
            f"`{shown}` isn't read by --dockerfile-inputs: of uv's commands, only `uv pip install --system -r FILE` is"
        )

    def _pip(self, number, args, uv, uv_system, shown, stop):  # type: ignore[no-untyped-def]
        """`pip ARGS` or `uv pip ARGS`."""
        tool = "uv pip" if uv else "pip"
        while args and args[0].text in (_UV_HARMLESS_FLAGS if uv else _PIP_HARMLESS_FLAGS):
            args = args[1:]  # before the subcommand, as in `pip --no-cache-dir install`
        if not args:
            return "shell", ""
        subcommand = args[0].text if args[0].exact else None
        if subcommand in ("--version", "-V", "list", "freeze", "check", "show", "cache", "help", "debug", "tree"):
            return "pip", ""  # nothing is installed, and nothing is read
        if subcommand != "install":
            stop(f"`{shown}`: only `install` is read of {tool}'s commands, and this one may change what is installed")
        # An unquoted name[extra] is a glob to the shell, which leaves it as written unless a file of that very
        # shape (the name plus one letter of the extras) is in the folder: read as the requirement it spells.
        args = [
            _Word(word.text) if word.brackets and _requirement_name(word.text) is not None else word for word in args
        ]
        if unknown := next((word for word in args if not word.exact), None):
            stop(
                f"`{shown}`: {unknown.text} isn't a value --dockerfile-inputs can read (a variable with no value in "
                "the Dockerfile, a command substitution or a glob)"
            )
        rest = [word.text for word in args]
        # Only installer tools, and no file: the installer's own set-up.
        flags = _UV_HARMLESS_FLAGS + ("--system",) if uv else _PIP_HARMLESS_FLAGS + ("--prefer-binary",)
        named = [text for text in rest[1:] if text not in flags and not text.startswith("--root-user-action")]
        tools = [_requirement_name(text) for text in named]
        if named and all(tool_name in _INSTALLER_TOOLS for tool_name in tools):
            return "installer-tools", " ".join(dict.fromkeys(tools))
        files, constraints, prefer_binary, names = _pip_install_files(number, rest, uv, uv_system)
        return "install", _Install(uv, files, constraints, prefer_binary, names, None, None)


def _env_pairs(
    keyword: str, args: str, build_args: Mapping[str, str], global_defaults: Mapping[str, str]
) -> list[tuple[str, str | None, bool]] | None:
    """(name, value, unset) for each variable an ENV or ARG instruction sets: the value if it is a literal wheelgap
    can read, else None; `unset` for an ARG that gets no value at all. None if the instruction isn't one wheelgap
    splits with certainty (an escape, a quote in a name, `ENV --flag`).

    ENV follows parseNameVal in parser/line_parsers.go: `ENV NAME value...` when the first word has no `=`, else
    `NAME=value` words. ARG follows dispatchArg in dockerfile2llb/convert.go: a --build-arg value, else the default,
    else the global ARG's value when the name is declared before the first FROM.
    https://github.com/moby/buildkit/blob/dd75b4955087a5a2d52188d6ce25b509b323280e/frontend/dockerfile/parser/line_parsers.go
    """
    if "\\" in args or args.startswith("--") or not args:
        return None
    try:
        words = _ShellLexer(args, raw_quotes=False, raw_escapes=False).words()
        raw = _ShellLexer(args, raw_quotes=True, raw_escapes=False).words()
    except (_LexError, RecursionError):
        return None
    if not words or len(words) != len(raw):
        return None

    def literal(value: str, written: str) -> str | None:
        return None if "$" in written else value

    name_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
    pairs: list[tuple[str, str | None, bool]] = []
    if keyword == "ENV" and "=" not in words[0]:
        legacy = re.split(r"[ \t]+", args, maxsplit=1)  # reWhitespace.Split(rest, 2)
        if len(legacy) < 2 or not name_re.fullmatch(legacy[0]) or any(c in legacy[1] for c in "\"'"):
            return None
        return [(legacy[0], literal(legacy[1].strip(), legacy[1]), False)]
    for word, written in zip(words, raw, strict=True):
        name, equals, value = word.partition("=")
        if not name_re.fullmatch(name) or not written.startswith(name) or (keyword == "ENV" and not equals):
            return None
        if keyword == "ENV":
            pairs.append((name, literal(value, written), False))
        elif name in build_args:
            pairs.append((name, build_args[name], False))
        elif equals:
            pairs.append((name, literal(value, written), False))
        elif name in global_defaults:
            pairs.append((name, global_defaults[name], False))
        else:
            pairs.append((name, None, True))
    return pairs


def _names_config(text: str) -> bool:
    return any(word in text for word in _CONFIG_WORDS)


_ARCHIVE_SUFFIXES = (
    ".whl",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".tar",
    ".tar.bz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
)
_PIP_INDEX_FLAGS = ("-i", "-f", "--index-url", "--extra-index-url", "--find-links", "--no-index", "--trusted-host")
_UV_INDEX_FLAGS = (*_PIP_INDEX_FLAGS, "--index", "--default-index", "--index-strategy")


def _command_line_requirement(number: int, tool: str, text: str) -> str:
    """A word of `pip install` that isn't an option or an option's value, as the requirement it is, or a
    DockerfileError saying what else it is. pip reads a word as a path when it has a path separator or starts
    with a dot, and as a file when it ends like an archive (`_looks_like_path` and `_get_url_from_path` in pip
    26.2's req/constructors.py); a bare name is a requirement even when a folder of that name exists."""
    if text.startswith("-"):
        flags = _UV_FLAGS_READ if tool == "uv pip" else _PIP_FLAGS_READ
        for_uv = " for uv" if tool == "uv pip" else ""
        raise DockerfileError(f"line {number}: {tool} install {text}: --dockerfile-inputs only reads {flags}{for_uv}")
    local = "/" in text or "\\" in text or text.startswith(".") or text.lower().endswith(_ARCHIVE_SUFFIXES)
    if local and "://" not in text:
        raise DockerfileError(
            f"line {number}: {tool} install {text}: a project folder or a file is installed here, which "
            "--dockerfile-inputs doesn't read yet: only requirements files and requirements by name are"
        )
    try:
        requirement = Requirement(text)
    except InvalidRequirement:
        requirement = None
    if requirement is None or requirement.url or "://" in text:
        raise DockerfileError(
            f"line {number}: {tool} install: a URL or something that isn't a requirement is installed here, which "
            "--dockerfile-inputs doesn't read (the text is left out, since a URL may hold credentials)"
        )
    return text


def _pip_install_files(
    number: int, rest: list[str], uv: bool, uv_system: bool
) -> tuple[list[str], list[str], bool, list[str]]:
    """(the -r FILEs, the -c FILEs, whether --prefer-binary is given, the requirements named on the line) of the
    words of a `pip install ...` or `uv pip install ...` command, from `install` on, with flags that don't change
    what the installer picks. `uv_system` says UV_SYSTEM_PYTHON is set, which uv reads as --system."""
    tool = "uv pip" if uv else "pip"
    for word in rest[1:]:
        option = word.split("=", 1)[0]
        if option in (_UV_INDEX_FLAGS if uv else _PIP_INDEX_FLAGS) or (
            word[:2] in ("-i", "-f") and not word.startswith("--") and word not in ("-i", "-f")
        ):
            # Requirements files have the same gap (an index line in one is not followed), and it isn't copied here.
            raise DockerfileError(
                f"line {number}: {tool} install {option if option.startswith('--') else word[:2]}: this line "
                "installs from another index or a folder of files, where the files can differ from PyPI's, and "
                "--dockerfile-inputs only checks PyPI"
            )
        if option in ("-e", "--editable"):
            raise DockerfileError(
                f"line {number}: {tool} install {option}: an editable install of a project folder isn't read by "
                "--dockerfile-inputs yet: only requirements files and requirements by name are"
            )
    names: list[str] = []
    files: list[str] = []
    constraints: list[str] = []
    prefer_binary = False
    system = uv_system
    i = 1
    while i < len(rest):
        if uv:
            # uv's own flags, first: a pip flag is read on a uv line only where uv has it with the same meaning.
            # uv spells the two file options --requirements and --constraints, and takes pip's spellings too.
            has_file = i + 1 < len(rest) and not rest[i + 1].startswith("-")
            if rest[i] in ("-r", "--requirement", "--requirements") and has_file:
                files.append(rest[i + 1])
                i += 2
            elif rest[i] in ("-c", "--constraint", "--constraints") and has_file:
                constraints.append(rest[i + 1])
                i += 2
            elif rest[i] == "--system":
                system = True
                i += 1
            elif rest[i] in _UV_HARMLESS_FLAGS:
                i += 1
            else:
                names.append(_command_line_requirement(number, tool, rest[i]))
                i += 1
        elif rest[i] in ("-r", "--requirement") and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
            files.append(rest[i + 1])
            i += 2
        elif rest[i] in ("-c", "--constraint") and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
            constraints.append(rest[i + 1])
            i += 2
        elif rest[i] in _PIP_HARMLESS_FLAGS:
            i += 1
        elif rest[i].startswith("--root-user-action=") and rest[i].partition("=")[2] in _PIP_ROOT_USER_ACTIONS:
            i += 1
        elif rest[i] == "--root-user-action" and rest[i + 1 : i + 2] and rest[i + 1] in _PIP_ROOT_USER_ACTIONS:
            i += 2
        elif rest[i] == "--prefer-binary":
            prefer_binary = True
            i += 1
        else:
            names.append(_command_line_requirement(number, tool, rest[i]))
            i += 1
    if not files and not names:
        raise DockerfileError(f"line {number}: {tool} install names nothing to install")
    if names and constraints:
        raise DockerfileError(
            f"line {number}: {tool} install -c FILE with requirements on the command line isn't read by "
            "--dockerfile-inputs yet: put them in a requirements file"
        )
    if uv and not system:
        raise DockerfileError(
            f"line {number}: uv pip install needs --system (or ENV UV_SYSTEM_PYTHON=1) for --dockerfile-inputs"
        )
    # uv picks the format of `-r FILE` by its name: pylock.toml is a lock, a .py file is a script with inline
    # metadata, and pyproject.toml, setup.py, setup.cfg and extensionless files are read in other ways again. A .txt
    # or .in file is a requirements file to uv and to wheelgap alike. The same names are asked of a `-c FILE`.
    # https://github.com/astral-sh/uv/blob/0.13.1/crates/uv-requirements/src/sources.rs (from_local_requirements_file)
    if uv:
        for option, named in (("-r", files), ("-c", constraints)):
            if other := next((file for file in named if not file.endswith(_UV_REQUIREMENTS_SUFFIXES)), None):
                raise DockerfileError(
                    f"line {number}: uv pip install {option} {other}: uv reads a file by its name (a lock, a script, "
                    "a project), and --dockerfile-inputs only reads requirements files named *.txt or *.in for uv"
                )
    return files, constraints, prefer_binary, names


def dockerfile_requirements(
    path: Path,
    build_args: Mapping[str, str] | None = None,
    context: Path | None = None,
    *,
    includes: dict[tuple[int, Path], tuple[Path, ...]] | None = None,
    constraints: dict[Path, tuple[Path, ...]] | None = None,
    dependencies: bool = False,
    uv_installs: set[tuple[int, Path]] | None = None,
    report: InputsReport | None = None,
) -> list[tuple[int, Path]]:
    """(line, requirements file) for each `pip install -r FILE` the RUN lines of the Dockerfile's final stage run.

    The answer is the packages of the install lines wheelgap can read, not everything the image holds. FILE is
    followed back through the WORKDIR and COPY instructions before it to a file in the build context, taken to be the
    Dockerfile's folder (or `context` if given). Every command of every RUN is classified (_RunReader): an install
    that is read; a command that can't install Python packages with pip or uv, passed over and listed by class in
    `report`; a command wheelgap can't see into (a script, make, a program it doesn't know), passed over with a
    warning in `report` that the caller must show; or one that installs in a way that isn't read, or that may change
    what a read install fetches, which is a DockerfileError naming its line, never a guess. The same holds for ADD,
    COPY --from, a SHELL that isn't sh or bash, an ENV or ARG that sets a PIP_ or UV_ variable off the harmless
    list, a pip config file, a final stage built on an earlier stage, and a `# syntax=` frontend other than
    docker/dockerfile.

    `constraints`, if given, is filled with each requirements file's `-c` files, to pass to load_inputs;
    `dependencies` says the check will follow dependencies (--deps), which --prefer-binary then can't be read for.
    The Docker ignore file is read (dockerignore.context_rules): a file its rules leave out of the build
    context, whether FILE or one it includes, is a DockerfileError naming the rule, and is never read. A -r or -c
    inside a `-r FILE` or a `-c FILE` is followed only when a COPY puts that same file where pip looks for it in the
    image (_check_includes); `includes`, if given, is filled with the files each (line, FILE) includes, for the
    requirements files and the constraints files alike.

    `RUN uv pip install --system -r FILE` is read too (with -c FILE and the flags in _UV_FLAGS_READ, and the
    documented `COPY --from=ghcr.io/astral-sh/uv /uv /uvx DEST/`), through the same lookup and checks. `uv_installs`
    must be given for it, and collects the (line, file) pairs uv installs: the caller checks those by uv's rules, not
    pip's. Because wheelgap picks a release for a range as pip does, a uv line is read only when every requirement is
    pinned with == (as for --prefer-binary) and `dependencies` is false; a copied uv.toml or pyproject.toml, a file
    not named *.txt or *.in, and a -r inside a constraints file (which uv reads as constraints) are errors as well.
    """
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        byte = f"0x{e.object[e.start]:02x}"
        raise DockerfileError(f"{path}: not UTF-8 (byte {byte} at offset {e.start}), pass -p/-t instead") from None
    context_dir = context if context is not None else path.parent
    context_label = str(context) if context is not None else "the Dockerfile's folder"
    try:
        return _dockerfile_requirements(
            text,
            context_dir,
            path.name,
            build_args,
            dockerfile_dir=path.parent,
            context_label=context_label,
            includes=includes,
            constraints=constraints,
            dependencies=dependencies,
            uv_installs=uv_installs,
            report=report,
        )
    except DockerfileError as e:
        number, _, message = str(e).removeprefix("line ").partition(": ")
        if number.isdecimal():  # `path:line: message`, as editors and CI logs link it
            raise DockerfileError(f"{path}:{number}: {message}") from None
        raise DockerfileError(f"{path}: {e}") from None


def _dockerfile_requirements(
    text: str,
    context: Path,
    name: str,
    build_args: Mapping[str, str] | None,
    dockerfile_dir: Path | None = None,
    context_label: str = "the Dockerfile's folder",
    includes: dict[tuple[int, Path], tuple[Path, ...]] | None = None,
    constraints: dict[Path, tuple[Path, ...]] | None = None,
    dependencies: bool = False,
    uv_installs: set[tuple[int, Path]] | None = None,
    report: InputsReport | None = None,
) -> list[tuple[int, Path]]:
    df_dir = dockerfile_dir if dockerfile_dir is not None else context
    try:
        ignore_rules = dockerignore.context_rules(context, df_dir / name)
    except dockerignore.IgnoreError as e:
        raise DockerfileError(str(e)) from None
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if _escape_char(lines) != "\\":
        raise DockerfileError("--dockerfile-inputs only reads Dockerfiles with the default \\ escape")
    syntax = _syntax_directive(lines)
    # A digest-pinned docker/dockerfile reads FROM, WORKDIR, COPY and RUN as any version does; a heredoc under it is
    # refused by _parse_froms, as plain --dockerfile refuses it.
    if _heredoc_rules(syntax) is None and not (syntax and _DOCKERFILE_DIGEST_RE.fullmatch(syntax)):
        raise DockerfileError(
            f"# syntax={syntax}: --dockerfile-inputs only reads Dockerfiles for docker/dockerfile frontends"
        )
    instructions: list[tuple[int, str]] = []
    froms, global_args = _parse_froms(text, build_args, instructions=instructions)
    if report is None:
        report = InputsReport()
    if _heredoc_rules(syntax) is None:  # a digest-pinned docker/dockerfile: any version, so any `<<` may be a heredoc
        for number, instruction in instructions:
            if _heredoc_args(instruction, "\\") is not None:
                raise DockerfileError(
                    f"line {number}: # syntax={syntax} may read the heredocs in {instruction.strip()[:60]!r} "
                    "differently from docker/dockerfile 1.16 and later, which wheelgap follows"
                )
    if not froms:
        raise DockerfileError("no FROM line found")
    final_line = froms[-1][0]
    # The FROM that names the final image: an earlier one if the final stage builds on a stage (`FROM ${BASE}` too).
    try:
        base_line = _final_from(text, build_args)[2]
    except UnknownImageError as e:
        # The caller has -p/-t for an image wheelgap can't read (it raised this error itself otherwise). The stage's
        # instructions are read all the same, once it is certain the FROM names an image and not an earlier stage:
        # a build argument wheelgap can't expand may name either.
        base_line = e.line if e.line is not None else final_line
        image = _split_from(froms[-1][1])[0]
        stages = [stage for _, args in froms[:-1] if (stage := _split_from_as_written(args)) is not None]
        if base_line == final_line and "$" in image and stages:
            raise DockerfileError(
                f"line {final_line}: FROM {image} uses a build argument wheelgap can't expand, and it may name an "
                f"earlier stage ({', '.join(stages)}): pass its value with --build-arg"
            ) from None
    if base_line != final_line:
        raise DockerfileError(
            f"line {final_line}: the final stage builds on an earlier stage; --dockerfile-inputs only reads a final "
            "stage built FROM an image"
        )
    start = max(i for i, (number, _) in enumerate(instructions) if number == final_line)
    workdir: str | None = None
    workdir_error: str | None = None
    run_error: str | None = None  # an instruction that changes how a later RUN installs
    dirs = {"/"}
    copies: list[_Copy] = []
    found: dict[Path, int] = {}
    used_constraints: dict[Path, tuple[Path, ...]] = {}
    installed_by_uv: dict[Path, bool] = {}

    def copied_requirements(file: str, option: str, uv: bool, install: _Install) -> Path:
        """The file in the build context that `-r FILE` or `-c FILE` on the RUN at `number` reads in the image, for
        pip or (`uv`) for uv pip."""
        if file.startswith("/"):
            target = posixpath.normpath(file)
        elif install.cwd is None:
            raise DockerfileError(
                install.cwd_error
                or f"line {number}: {option} {file} is relative, but no WORKDIR with an absolute path comes before it"
            )
        else:
            target = posixpath.normpath(posixpath.join(install.cwd, file))
        host = _find_copied_file(copies, target, context, number, ignore_rules)
        # A constraints file's includes are verified as a requirements file's are: a -r inside it is read as
        # requirements (pip's rule), so those packages are installed, and a -c inside it constrains them all.
        nested = _check_includes(
            host, target, copies, context, number, ignore_rules, uv=uv, constraints_file=option == "-c"
        )
        if uv and (other := next((file for file in nested if file.suffix not in _UV_REQUIREMENTS_SUFFIXES), None)):
            raise DockerfileError(
                f"line {number}: {host.name} includes {other.name}, and --dockerfile-inputs only reads requirements "
                "files named *.txt or *.in for uv"
            )
        if uv:
            for read in (host, *nested):
                _refuse_uv_option_lines(read, number)
        for read in (host, *nested):
            reader.check_untouched(number, read, target if read == host else None, install.seen)
        if includes is not None:
            includes[number, host] = nested
        return host

    reader = _RunReader(report)
    env: dict[str, str] = {}  # the ENV and ARG values wheelgap knows, as the shell of a RUN sees them
    uv_system = False  # ENV UV_SYSTEM_PYTHON=1: uv reads it as --system
    arg_values: dict[str, bool] = {}  # the names in env that an ARG set, and whether --build-arg gave the value
    global_defaults = global_args.defaults if not global_args.unreadable else {}

    def read_install(install: _Install) -> None:
        if run_error:
            raise DockerfileError(run_error)
        if reader.config_error:
            raise DockerfileError(reader.config_error)
        uv = install.uv
        if uv and (config := next((copy for copy in copies if _copies_config(copy, _UV_CONFIG_NAMES)), None)):
            raise DockerfileError(
                f"line {config.line}: this COPY puts a uv.toml or pyproject.toml in the image, and uv reads "
                "settings from them: copy the requirements file alone and run uv pip install before this COPY"
            )
        if uv and dependencies:
            # A dependency is a range, resolved here by pip's rules (see the pins asked for below).
            raise DockerfileError(
                f"line {number}: uv pip install isn't read with --deps: wheelgap follows dependencies as pip "
                "resolves them, and uv can pick other versions"
            )
        constraint_paths = tuple(copied_requirements(file, "-c", uv, install) for file in install.constraints)
        if constraint_paths and constraints is None:  # never read the requirements without their constraints
            raise DockerfileError(f"line {number}: pip install -c FILE isn't read here")
        for file in install.files:
            host = copied_requirements(file, "-r", uv, install)
            # Read as load_inputs will read it: with the line's -c files, and through every include, so a
            # requirement in an included file (or in a -r inside a constraints file) counts, and a requirement
            # that only a constraint pins with == counts as pinned.
            unpinned = (
                [p for p in parse_requirements(host, constraint_paths).packages if not p.version]
                if (install.prefer_binary or uv) and not dependencies
                else []
            )
            if uv and unpinned:
                where = host.name
                if unpinned[0].origin != host.name:
                    where = f"{unpinned[0].origin} (read with {host.name})"
                # wheelgap picks a release for a range as pip does, and pip skips releases whose Requires-Python
                # upper bound leaves the image's Python out, which uv takes: only a pin gives both the same one.
                raise DockerfileError(
                    f"line {number}: uv pip install: {unpinned[0].name} in {where} isn't pinned with ==, and uv "
                    "and pip can pick different versions for a range: pin it (in the file, or in a constraints "
                    "file)"
                )
            if install.prefer_binary and (dependencies or unpinned):
                where = host.name
                if unpinned and unpinned[0].origin != host.name:
                    where = f"{unpinned[0].origin} (read with {host.name})"
                # With it pip takes an older release that has a wheel over a newer one that has none, and
                # wheelgap checks the newest release a requirement allows: only a pin gives both the same one.
                raise DockerfileError(
                    f"line {number}: pip install --prefer-binary makes pip pick an older release that has a "
                    f"wheel, so it's only read when every requirement in {where} is pinned with == "
                    "and --deps isn't used"
                )
            if host in found and used_constraints[host] != constraint_paths:
                raise DockerfileError(
                    f"line {number}: {host.name} is installed again with other -c files than on line "
                    f"{found[host]}, which --dockerfile-inputs doesn't read"
                )
            if host in found and installed_by_uv[host] != uv:
                raise DockerfileError(
                    f"line {number}: {host.name} is installed by pip on one line and by uv on another (line "
                    f"{found[host]}), which --dockerfile-inputs doesn't read"
                )
            if uv:
                if uv_installs is None:  # never check a file uv installs by pip's rules
                    raise DockerfileError(f"line {number}: uv pip install -r FILE isn't read here")
                uv_installs.add((number, host))
            installed_by_uv[host] = uv
            used_constraints[host] = constraint_paths
            found.setdefault(host, number)
        if install.names:
            loose = [
                name for name in install.names if requirement_to_package(name, "").version is None
            ]  # with a marker or extras, a pin is still a pin
            if uv and loose:
                raise DockerfileError(
                    f"line {number}: uv pip install: {loose[0]} isn't pinned with ==, and uv and pip can pick "
                    "different versions for a range: pin it"
                )
            if install.prefer_binary and (dependencies or loose):
                raise DockerfileError(
                    f"line {number}: pip install --prefer-binary makes pip pick an older release that has a "
                    "wheel, so it's only read when every requirement on the line is pinned with == "
                    "and --deps isn't used"
                )
            # One entry per RUN line and installer: `pip install a && pip install b` names a and b on the line.
            if report.named and report.named[-1][0] == number and report.named[-1][2] == uv:
                report.named[-1][1].extend(install.names)
            else:
                report.named.append((number, list(install.names), uv))
        # An unread COPY or ADD before the one that copies FILE may still put a pip config file in the image.
        if unread := next((copy for copy in copies if copy.error), None):
            raise DockerfileError(f"line {unread.line}: {unread.error}")

    for number, instruction in instructions[start + 1 :]:
        parts = re.split(r"[ \t\v\f\r]+", instruction, maxsplit=1)
        keyword, args = parts[0].upper(), (parts[1].strip() if len(parts) > 1 else "")
        if keyword == "WORKDIR":
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", args, re.ASCII):
                workdir, workdir_error = None, f"line {number}: WORKDIR {args}: only a literal path is read ({_PLAIN})"
            elif args.startswith("/"):
                workdir, workdir_error = posixpath.normpath(args), None
            elif workdir is not None:
                workdir = posixpath.normpath(posixpath.join(workdir, args))
            if workdir is not None:
                _add_dir(dirs, workdir)
        elif keyword == "COPY":
            copies.append(copy := _read_copy(number, args, workdir, dirs, context, context_label, ignore_rules))
            if _copies_config(copy, _PIP_CONFIG_NAMES):
                run_error = f"line {number}: a pip config file may change what pip installs"
        elif keyword == "ADD":
            copies.append(_Copy(number, (), "", False, "ADD isn't read by --dockerfile-inputs, use COPY"))
        elif keyword == "SHELL":
            # RUN lines are split by the POSIX shell's rules, which sh, dash, ash and bash share for what is read
            # (bash's own expansions, brace lists and process substitution, are never taken as plain words).
            if not _SHELL_RE.fullmatch(args):
                run_error = (
                    f"line {number}: SHELL {args[:60]} changes how RUN lines run: --dockerfile-inputs only reads "
                    "sh, dash, ash and bash run with -c (and -e, -u, -x, -o pipefail)"
                )
        elif keyword in ("ENV", "ARG"):
            pairs = _env_pairs(keyword, args, build_args or {}, global_defaults)
            if pairs is None:
                # Not split into names with certainty: any name may have changed, and a setting may be among them.
                env.clear()
                if re.search(r"(?<![A-Za-z0-9_])(?:PIP|UV)_", args):
                    run_error = f"line {number}: an {keyword} setting PIP_ or UV_ variables changes what pip installs"
                continue
            for name, value, unset in pairs:
                arg_values.pop(name, None)
                if keyword == "ARG" and value is not None:
                    arg_values[name] = name in (build_args or {})
                if unset:
                    # An ARG with no default and no --build-arg isn't in the RUN's environment (dispatchArg).
                    if _installer_setting(name):
                        report.notes.append(
                            (
                                number,
                                "arg-unset",
                                f"ARG {name} has no default, so the build is read as run without --build-arg "
                                f"{name}: a build that passes it may install other files",
                            )
                        )
                    continue
                if value is None:
                    env.pop(name, None)
                else:
                    env[name] = value
                if _installer_setting(name):
                    # The value is left out: an index URL may hold credentials.
                    run_error = f"line {number}: {keyword} {name} may change what pip or uv installs"
                elif name == "UV_SYSTEM_PYTHON":
                    uv_system = value is not None and value.lower() in ("1", "true")
        elif keyword == "RUN":
            for install in reader.read(number, instruction, args, env, workdir, workdir_error, uv_system, arg_values):
                read_install(install)
    if not found and not report.named:
        unseen = ""
        if report.warnings:
            where = ", ".join(str(line) for line, _ in report.warnings)
            unseen = f" (it has commands wheelgap can't see into, which may install some: line {where})"
        raise DockerfileError(
            f"the final stage (line {final_line}) has no `RUN pip install` that --dockerfile-inputs can read" + unseen
        )
    if constraints is not None:
        constraints.update(used_constraints)
    return [(number, host) for host, number in found.items()]
