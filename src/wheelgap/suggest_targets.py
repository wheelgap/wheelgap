"""`--suggest-targets`: the targets and Pythons a project already names in its CI workflows and Dockerfiles.

It only suggests. Workflows are read with a conservative line scan (the standard library has no YAML parser):
anything it can't read for sure is reported as unreadable, never guessed. Dockerfiles go through
`read_dockerfile`, so they're read exactly as `--dockerfile` reads them.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from .dockerfile import DockerfileError, HeredocSyntaxError, ImageTarget, final_from, parse_image, read_dockerfile
from .targets import DISTROS, PLATFORMS


@dataclass(frozen=True)
class Found:
    value: str  # a target such as linux-aarch64, or a Python such as 3.13
    path: str  # relative to the project folder, with forward slashes
    line: int
    source: str  # what was read there
    glibc: str | None = None  # the libc version the source names, if any
    musl: str | None = None
    macos: str | None = None  # the macOS version a runner label names


@dataclass(frozen=True)
class Unreadable:
    path: str
    line: int | None
    reason: str


# The settings that every source of a target family must agree on before the snippet sets them.
_SHARED = (("glibc", "linux-"), ("musl", "musl-"), ("macos", "macos-"))


@dataclass
class Suggestions:
    targets: list[Found] = field(default_factory=list)
    pythons: list[Found] = field(default_factory=list)
    unreadable: list[Unreadable] = field(default_factory=list)

    def config(self) -> dict[str, object]:
        """The [tool.wheelgap] keys to suggest: each target and Python once; glibc, musl and macos only when every
        source of those targets names the same version."""
        config: dict[str, object] = {}
        if self.pythons:
            config["python"] = sorted({f.value for f in self.pythons}, key=_python_order)
        if self.targets:
            seen = {f.value for f in self.targets}
            config["targets"] = [name for name in PLATFORMS if name in seen]
            for key, prefix in _SHARED:
                versions = {getattr(f, key) for f in self.targets if f.value.startswith(prefix)}
                if len(versions) == 1 and (version := versions.pop()) is not None:
                    config[key] = version
        return config

    def left_out(self) -> list[str]:
        """Why glibc, musl or macos isn't in the snippet although a source names one."""
        config = self.config()
        notes = []
        for key, prefix in _SHARED:
            sources = [f for f in self.targets if f.value.startswith(prefix)]
            if key in config or not any(getattr(f, key) for f in sources):
                continue
            groups: dict[str, list[str]] = {}
            for f in sources:
                groups.setdefault(getattr(f, key) or "not named", []).append(_where(f))
            says = "; ".join(
                f"{version} at {', '.join(places[:2])}{f' and {len(places) - 2} more' * (len(places) > 2)}"
                for version, places in groups.items()
            )
            notes.append(f"# {key} not set: the {prefix}* targets don't all name the same one ({says})")
        return notes


def _python_order(version: str) -> tuple[int, int, str]:
    major, minor = version.rstrip("t").split(".")
    return int(major), int(minor), version


# GitHub-hosted runner labels, as listed (2026-11-12) at
# https://docs.github.com/en/actions/reference/runners/github-hosted-runners
# and, for the larger macOS runners, https://docs.github.com/en/actions/reference/runners/larger-runners
# Labels are case-insensitive. Anything else (self-hosted, larger Linux/Windows runners, which take custom names)
# can't be read.
_RUNNERS = [
    (re.compile(r"ubuntu-\d+\.\d+-arm", re.ASCII), "linux-aarch64"),
    (re.compile(r"ubuntu-(?:latest|slim|\d+\.\d+)", re.ASCII), "linux-x86_64"),
    (re.compile(r"windows-11(?:-vs\d+)?-arm", re.ASCII), "win-arm64"),
    (re.compile(r"windows-(?:latest|\d{4}(?:-vs\d+)?)", re.ASCII), "win-amd64"),
    # macos-latest has been Apple silicon since 2024; -large runners are Intel, -xlarge (and xcode-*) Apple silicon.
    (re.compile(r"macos-(?:latest|\d+)-xlarge|macos-latest|xcode-\d+(?:-xlarge)?", re.ASCII), "macos-arm64"),
    (re.compile(r"macos-(?:latest|\d+)-large|macos-\d+-intel|macos-(?:1[0-3])", re.ASCII), "macos-x86_64"),
    (re.compile(r"macos-(?:1[4-9]|[2-9]\d)", re.ASCII), "macos-arm64"),
]
# The OS a pinned label runs (a -latest label moves, so it names none): Ubuntu's glibc, from the distro table, and
# the macOS version, which is the newest a macOS wheel may need there.
_UBUNTU_RE = re.compile(r"ubuntu-(\d+\.\d+)(?:-arm)?", re.ASCII)
_MACOS_RE = re.compile(r"macos-(\d+)(?:-intel|-large|-xlarge)?", re.ASCII)


def runner_target(label: str) -> str | None:
    """The target a GitHub-hosted runner label runs on, or None for a label wheelgap doesn't know."""
    for pattern, target in _RUNNERS:
        if pattern.fullmatch(label.lower()):
            return target
    return None


def _runner_os(label: str) -> tuple[str | None, str | None]:
    """(glibc, macOS version) of a pinned runner label, where wheelgap knows it."""
    lower = label.lower()
    if (m := _UBUNTU_RE.fullmatch(lower)) and (distro := DISTROS.get(f"ubuntu-{m.group(1)}")):
        return distro[1], None
    if m := _MACOS_RE.fullmatch(lower):
        return None, f"{m.group(1)}.0"
    return None, None


# actions/setup-python's `architecture` (x86, x64 or arm64; the runner's own when unset:
# https://github.com/actions/setup-python/blob/main/action.yml and src/setup-python.ts) where the result is certain:
# the runner's own architecture, 32-bit Python on x64 Windows, and Intel Python under Rosetta on Apple silicon.
_ARCHITECTURES = {
    ("linux-x86_64", "x64"): "linux-x86_64",
    ("linux-aarch64", "arm64"): "linux-aarch64",
    ("musl-x86_64", "x64"): "musl-x86_64",
    ("musl-aarch64", "arm64"): "musl-aarch64",
    ("win-amd64", "x64"): "win-amd64",
    ("win-amd64", "x86"): "win32",
    ("win-arm64", "arm64"): "win-arm64",
    ("macos-x86_64", "x64"): "macos-x86_64",
    ("macos-arm64", "arm64"): "macos-arm64",
    ("macos-arm64", "x64"): "macos-x86_64",
}
# core.getBooleanInput, which setup-python reads `freethreaded` with, takes these and nothing else.
_TRUE, _FALSE = ("true", "True", "TRUE"), ("false", "False", "FALSE")

# setup-python and setup-uv versions read for sure: 3.13, 3.13.1, 3.14t. Others (3.x, pypy3.10, >=3.11) aren't.
_PYTHON_RE = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)", re.ASCII)
_PYTHON_ACTIONS = ("actions/setup-python", "astral-sh/setup-uv")
_MATRIX_REF_RE = re.compile(r"\$\{\{[ \t]*matrix\.([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\}\}", re.ASCII)
_WS = " \t"  # YAML's whitespace: never NBSP or other Unicode spaces, which are ordinary characters to it
# Line breaks YAML 1.1 or 1.2 knows besides \n (\r\n is fine): the lines they make aren't the ones split on here.
_ODD_BREAK_RE = re.compile("\r(?!\n)|[\x85\u2028\u2029]")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", re.ASCII)
# Plain scalars YAML 1.1 or 1.2 reads as something other than the string (0x10 is 16, yes may be true): GitHub's
# reading of them isn't certain, so they're never compared or suggested.
_AMBIGUOUS_RE = re.compile(r"[-+]?0[xo][0-9a-fA-F_]+|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|(?i:yes|no|on|off|y|n)")

_CONTAINERFILES = ("Dockerfile", "Containerfile")
_TEMPLATE_SUFFIXES = (".template", ".tmpl", ".tpl", ".j2", ".jinja", ".jinja2", ".in")
# Folders whose Dockerfiles are test inputs, examples or build output rather than what the project ships.
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "site-packages",
    "tests",
    "test",
    "fixtures",
    "examples",
    "example",
    "docs",
}
_SKIP_DIRS |= {"target", "build", "dist"}
# Files named like a Dockerfile that are something else: its docs, or code about Dockerfiles (dockerfile.py).
_NOT_CONTAINERFILES = (".md", ".rst", ".txt", ".py", ".pyc", ".sh", ".json", ".yml", ".yaml", ".toml", ".dockerignore")


class _Unsure(Exception):
    def __init__(self, reason: str, line: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.line = line


class _Computed(_Unsure):
    """A matrix (or a part of one) that's computed: it can't be read, but GitHub still runs the job."""


class _NotRead(Exception):
    """A line outside the YAML this scanner reads: the whole file is refused."""

    def __init__(self, line: int, text: str):
        super().__init__(text)
        self.line = line
        self.text = text


class _Opaque:
    """A value the grammar accepts but never reads: refused wherever a value is read."""

    def __init__(self, what: str) -> None:
        self.what = what


_BLOCK = _Opaque("a multi-line value")  # `key: |` and the lines under it
_LINES = _Opaque("a value over several lines")  # a plain scalar continued on more-indented lines
_ALIAS = _Opaque("an alias")  # `key: *name`
_ESCAPED = _Opaque("a quoted value with escapes")  # `key: "a\tb"`
_QUOTED_LINES = _Opaque("a quoted value over several lines")


@dataclass
class _Node:
    """A line of a workflow: `key: value`, or a list item (`- value`, key None). `value` is what's on the line (None
    when the value is the indented lines below): a _Scalar, a flow list, or _BLOCK."""

    line: int
    indent: float
    key: str | None
    value: object = None
    raw: str = ""  # the value as written, for messages
    children: list[_Node] = field(default_factory=list)
    sealed: bool = False  # its children came from a flow mapping on its line: nothing may be indented under it

    def get(self, key: str) -> _Node | None:
        if self.value is _ALIAS:
            raise _Unsure(f"{self.key}: {self.raw} is an alias, which isn't read", self.line)
        # (A key is never in a mapping twice: the grammar refuses that.)
        return next((c for c in self.children if c.key == key), None)


@dataclass(frozen=True)
class _Scalar:
    text: str
    quoted: bool
    line: int

    def typed(self) -> tuple[str, object]:
        """The YAML 1.2 core schema type and value, so `3.10` and `"3.10"` differ as they do to GitHub."""
        if self.quoted:
            return "str", self.text
        if _AMBIGUOUS_RE.fullmatch(self.text):
            raise _Unsure(f"can't tell how GitHub reads the unquoted {self.text} (quote it)", self.line)
        lower = self.text.lower()
        if lower in ("", "~", "null"):
            return "null", None
        if lower in ("true", "false"):
            return "bool", lower == "true"
        if _NUMBER_RE.fullmatch(self.text):
            return "num", float(self.text)
        return "str", self.text

    def plain_text(self) -> str:
        """The text GitHub passes on: an unquoted 3.10 is the number 3.1, so it can't be read as written."""
        kind, value = self.typed()
        if kind == "num" and not self.text.isdigit() and repr(value) != self.text:
            raise _Unsure(f"unquoted {self.text} is the number {value:g} in YAML (quote it)", self.line)
        return self.text


# The YAML this scanner reads: every line of a workflow must be one of these, or the file isn't read at all. Rather
# than parse YAML (the standard library has no parser), it accepts only a small subset whose meaning is certain:
# - blank lines, and comments (a `#` at the start of a line or after a space or tab);
# - `key:` or `key: VALUE`, with a key of letters, digits, `_`, `.` and `-` or a whole quoted key without escapes;
# - `-`, `- VALUE` or `- key: VALUE` (a list item);
# - `---` first (after an optional `%YAML 1.2`).
# VALUE is one of these, then an optional comment:
# - a plain scalar on one line, that doesn't start with an indicator and holds no `: ` or ` #`;
# - a whole single- or double-quoted scalar on one line, without escapes (`\` or `''`);
# - a flow list `[a, 'b']` or mapping `{k: a, "l": 'b'}` on one line, of such scalars and flow collections, with no
#   `:` or `#` in plain items and a space after each key's `:`;
# - a block scalar header (`|`, `>-`, `|2`...), whose lines are skipped by their indentation;
# - an anchor alone (`&name`, for the block below it) or an alias alone (`*name`, never read);
# - a plain scalar continued on more-indented lines, or starting on the line after its key (never read: as in
#   YAML, nothing under a scalar can be a key);
# - a double-quoted scalar on one line with escapes (never read);
# - `[` alone, opening a flow list over several lines (also on the line after the key, as Prettier writes it): one item
#   per line, each but the last followed by `,`, then `]`.
# - `{` alone, followed by one `key: ITEM` scalar pair per line with commas, then `}`;
# - a quoted scalar over more-indented lines (never read), with a whole closing quote and optional comment.
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)
_QUOTED_RE = re.compile(r"'[^'\n]*'(?!')|\"[^\"\\\n]*\"")  # no escapes: no `\` in "...", no `''` in '...'
_BLOCK_HEADER_RE = re.compile(r"[|>](?:[1-9][-+]?|[-+][1-9]?)?", re.ASCII)
_ESCAPED_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')  # a double-quoted scalar with escapes, which are never decoded
_PROPERTY_RE = re.compile(r"([&*])[A-Za-z0-9_-]+", re.ASCII)  # an anchor or an alias, alone
# A plain item in a flow collection: no flow indicators, quotes, `:` or `#`, and no other indicator to start with.
_FLOW_PLAIN_RE = re.compile(r"(?:[^ \t,\[\]{}:#'\"&*!|>%@`?\\-]|-(?=[^ \t,\[\]{}:#'\"]))[^,\[\]{}:#'\"\\]*")


def _comment_only(rest: str) -> bool:
    """Nothing after a value but spaces and a comment (whose `#` follows a space or tab, as YAML requires)."""
    return not rest.strip(_WS) or (rest[0] in _WS and rest.lstrip(_WS).startswith("#"))


def _plain(text: str) -> str | None:
    """A plain scalar on one line, before any comment, or None when the text isn't one this scanner reads."""
    m = re.search(r"[ \t]#", text)
    value = (text[: m.start()] if m else text).rstrip(_WS)
    if not value or value[0] in ",[]{}#&*!|>'\"%@`?:" or value[0] == "-" and (len(value) == 1 or value[1] in _WS):
        return None
    # After the first character, quotes, brackets and the other indicators are ordinary characters of a plain
    # scalar in block context (as in `uses: actions/setup-python@v6` or `if: github.ref == 'refs/heads/main'`); only
    # `: ` and ` #` aren't.
    if re.search(r":(?:[ \t]|$)", value):
        return None
    return value


class _Flow:
    """A flow list or mapping on one line, as the grammar above allows it."""

    def __init__(self, text: str, line: int, pos: int = 0) -> None:
        self.text, self.line, self.pos = text, line, pos

    def fail(self) -> NoReturn:
        raise _NotRead(self.line, self.text)

    def peek(self) -> str:
        return self.text[self.pos : self.pos + 1]

    def spaces(self) -> None:
        while self.peek() and self.peek() in _WS:
            self.pos += 1

    def collection(self) -> object:
        return self.seq() if self.peek() == "[" else self.mapping() if self.peek() == "{" else self.fail()

    def items(self, close: str, item: Callable[[], None]) -> None:
        self.pos += 1
        self.spaces()
        while self.peek() != close:
            item()
            self.spaces()
            if self.peek() == ",":
                self.pos += 1
                self.spaces()
            elif self.peek() != close:
                self.fail()
        self.pos += 1

    def value(self) -> object:
        return self.collection() if self.peek() in ("[", "{") else self.scalar()

    def seq(self) -> list[object]:
        out: list[object] = []
        self.items("]", lambda: out.append(self.value()))
        return out

    def mapping(self) -> dict[str, object]:
        out: dict[str, object] = {}

        def pair() -> None:
            m = _QUOTED_RE.match(self.text, self.pos) or _KEY_RE.match(self.text, self.pos)
            if m and m.group()[0] in "'\"":
                _check_quoted(m.group()[1:-1], m.group()[0], self.line)
            key = m.group()[1:-1] if m and m.group()[0] in "'\"" else m.group() if m else ""
            # `key: value`, with a space after the colon (JSON's `"key":value` isn't read)
            if (
                not m
                or self.text[m.end() : m.end() + 1] != ":"
                or self.text[m.end() + 1 : m.end() + 2] not in (" ", "\t")
            ):
                self.fail()
            if key in out:
                self.fail()
            self.pos = m.end() + 1
            self.spaces()
            out[key] = self.value()

        self.items("}", pair)
        return out

    def scalar(self) -> _Scalar:
        if m := _QUOTED_RE.match(self.text, self.pos):
            _check_quoted(m.group()[1:-1], m.group()[0], self.line)
            self.pos = m.end()
            return _Scalar(m.group()[1:-1], True, self.line)
        if m := _FLOW_PLAIN_RE.match(self.text, self.pos):
            value = m.group().rstrip(_WS)
            self.pos = m.start() + len(value)
            return _Scalar(value, False, self.line)
        self.fail()


_OPEN = object()  # `[` alone: a flow list continues on the next lines
_OPEN_MAP = object()  # `{` alone: a flat flow mapping continues on the next lines
_OPEN_QUOTE = object()


# YAML 1.2's double-quoted escapes; `\` before a tab is one too.
_ESCAPE = re.compile(r'\\(?:[0abtnvfre "/\\N_LP\t]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})')


def _quoted_end(text: str, quote: str, line: int) -> int | None:
    """The end of a whole quoted value's line, or None when it continues. Its contents are never decoded."""
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == quote:
            if quote == "'" and text[pos : pos + 2] == "''":
                raise _NotRead(line, text)  # `''` is an escape, and quoted values with escapes are never read
            return pos + 1
        if quote == '"' and char == "\\":
            if pos + 1 == len(text):  # YAML's escaped line break
                return None
            escape = re.match(_ESCAPE, text[pos:])
            if escape is None or escape.group().startswith("\\U") and int(escape.group()[2:], 16) > 0x10FFFF:
                raise _NotRead(line, text)
            pos += len(escape.group())
            continue
        if ord(char) < 32 and char != "\t" or 0x7F <= ord(char) <= 0x9F or ord(char) in (0xFFFE, 0xFFFF):
            raise _NotRead(line, text)
        pos += 1
    return None


def _check_quoted(inner: str, quote: str, line: int) -> None:
    """The checks one-line and quoted-over-several-lines scalars share: a control character, a bad escape or `''`
    in a single-quoted value refuses the file. (The text has no closing quote, so _quoted_end only validates.)"""
    _quoted_end(inner, quote, line)


def _value(text: str, line: int) -> tuple[object, str]:
    """The VALUE after `key:` or `- ` (spaces already skipped), parsed, and as written."""
    if not text or text.startswith("#"):
        return None, ""
    if text[0] in "'\"":
        if (m := _QUOTED_RE.match(text)) and _comment_only(text[m.end() :]):
            _check_quoted(m.group()[1:-1], text[0], line)
            return _Scalar(m.group()[1:-1], True, line), m.group()
        if (m := _ESCAPED_RE.match(text)) and _comment_only(text[m.end() :]):
            _check_quoted(m.group()[1:-1], text[0], line)
            return _ESCAPED, m.group()
        if _quoted_end(text[1:], text[0], line) is None:
            return _OPEN_QUOTE, text
        raise _NotRead(line, text)
    if (m := _PROPERTY_RE.match(text)) and _comment_only(text[m.end() :]):
        # An anchor on the block below (read as it is) or an alias (never read).
        return (None if m.group(1) == "&" else _ALIAS), m.group()
    if text[0] == "[" and _comment_only(text[1:]):
        return _OPEN, "["
    if text[0] == "{" and _comment_only(text[1:]):
        return _OPEN_MAP, "{"
    if text[0] in "[{":
        flow = _Flow(text, line)
        value = flow.collection()
        if not _comment_only(text[flow.pos :]):
            raise _NotRead(line, text)
        return value, text[: flow.pos]
    if text[0] in "|>":
        if not (m := _BLOCK_HEADER_RE.match(text)) or not _comment_only(text[m.end() :]):
            raise _NotRead(line, text)
        return _BLOCK, m.group()
    if (plain := _plain(text)) is None:
        raise _NotRead(line, text)
    return _Scalar(plain, False, line), plain


def _split_key(body: str) -> tuple[str, str] | None:
    """`key: rest` -> (key, rest with leading spaces skipped), or None when the line doesn't start with a key."""
    if body[:1] in ("'", '"'):
        if not (m := _QUOTED_RE.match(body)):
            return None
        try:
            _check_quoted(m.group()[1:-1], m.group()[0], 0)
        except _NotRead:
            return None
        key, after = m.group()[1:-1], body[m.end() :]
    elif m := _KEY_RE.match(body):
        key, after = m.group(), body[m.end() :]
    else:
        return None
    if not after.startswith(":") or after[1:2] not in ("", " ", "\t"):
        return None
    return key, after[1:].lstrip(_WS)


def _flow_children(mapping: dict[str, object], line: int, indent: float) -> list[_Node]:
    """A flow mapping's entries as the nodes a block mapping would have."""
    children = []
    for key, value in mapping.items():
        at = value.line if isinstance(value, _Scalar) else line
        node = _Node(at, indent + 1, key, value, value.text if isinstance(value, _Scalar) else "...", sealed=True)
        if isinstance(value, dict):
            node.children, node.value = _flow_children(value, line, indent + 1), None
        children.append(node)
    return children


def _parse_tree(text: str) -> _Node:
    """The workflow as a tree of lines, by indentation, if every line is in the grammar above; else _NotRead."""
    if m := _ODD_BREAK_RE.search(text):
        raise _NotRead(text[: m.start()].count("\n") + 1, f"a line break other than \\n ({m.group()!r})")
    root = _Node(0, -1, None)
    stack = [root]
    lines = [raw.removesuffix("\r") for raw in text.split("\n")]
    block_column: int | None = None  # skipping a block scalar's lines indented more than this
    block_content: int | None = None  # and the indentation of its first line
    plain: tuple[_Node, int] | None = None  # a plain scalar's node and column, while its lines may continue
    started = directive = False
    anchors: set[str] = set()
    number = 0

    def content(i: int) -> bool:
        stripped = lines[i].strip(_WS)
        return bool(stripped) and not stripped.startswith("#")

    def attach(node: _Node) -> None:
        while stack[-1].indent >= node.indent:
            stack.pop()
        parent = stack[-1]
        if parent.value is not None or parent.sealed:  # a further line under a key or item with its value already
            raise _NotRead(node.line, lines[node.line - 1])
        if parent.children and (
            parent.children[0].indent != node.indent or (parent.children[0].key is None) != (node.key is None)
        ):
            raise _NotRead(node.line, lines[node.line - 1])  # indentation that doesn't line up, or keys and items
        if node.key is not None and any(c.key == node.key for c in parent.children):
            raise _NotRead(node.line, lines[node.line - 1])  # a key twice in one mapping: invalid YAML
        parent.children.append(node)
        stack.append(node)

    def flow_lines(node: _Node, start: int) -> int:
        """Read a flow list over several lines into node, from the line after `[`; return the next line's index."""
        base = len(lines[node.line - 1]) - len(lines[node.line - 1].lstrip(" "))
        items: list[object] = []
        i, closed = start, True  # closed: the last item had its comma (or there's none yet)
        while i < len(lines):
            line = lines[i]
            i += 1
            if not content(i - 1):
                continue
            indent, body = len(line) - len(line.lstrip(" ")), line.strip(_WS)
            if line[indent : indent + 1] == "\t":  # a tab where the indentation ends
                raise _NotRead(i, line)
            if body.startswith("]"):
                if indent < base or not _comment_only(body[1:]):
                    raise _NotRead(i, line)
                node.value, node.raw = items, "[...]"
                return i
            if indent <= base or not closed:
                raise _NotRead(i, line)
            if body.startswith("{") and _comment_only(body[1:]):
                item = _Node(i, indent, None)
                i, closed = mapping_lines(item, i, comma=True)
                items.append(item.value)
                continue
            flow = _Flow(body, i)
            items.append(flow.value())
            end = flow.pos  # the comment (if any) after the item needs the space before it
            flow.spaces()
            closed = flow.peek() == ","
            if not _comment_only(body[flow.pos + 1 :] if closed else body[end:]):
                raise _NotRead(i, line)
        raise _NotRead(node.line, lines[node.line - 1])  # never closed

    def mapping_lines(node: _Node, start: int, comma: bool = False) -> tuple[int, bool]:
        """A flat multi-line flow mapping; a comma after `}` belongs to an enclosing flow list only."""
        base = len(lines[node.line - 1]) - len(lines[node.line - 1].lstrip(" "))
        mapping: dict[str, object] = {}
        i, closed = start, True
        while i < len(lines):
            line = lines[i]
            i += 1
            if not content(i - 1):
                continue
            indent, body = len(line) - len(line.lstrip(" ")), line.strip(_WS)
            if line[indent : indent + 1] == "\t" or indent < base:
                raise _NotRead(i, line)
            if body.startswith("}"):
                rest = body[1:]
                end_comma = comma and rest.startswith(",")
                if not _comment_only(rest[1:] if end_comma else rest):
                    raise _NotRead(i, line)
                node.value, node.raw = mapping, "{...}"
                return i, end_comma
            pair = _split_key(body)
            if indent <= base or not closed or pair is None or pair[0] in mapping:
                raise _NotRead(i, line)
            flow = _Flow(pair[1], i)
            mapping[pair[0]] = flow.scalar()  # no nested collections, aliases or multi-line items
            end = flow.pos
            flow.spaces()
            closed = flow.peek() == ","
            if not _comment_only(pair[1][flow.pos + 1 :] if closed else pair[1][end:]):
                raise _NotRead(i, line)
        raise _NotRead(node.line, lines[node.line - 1])

    def quoted_lines(node: _Node, start: int, column: int) -> int:
        quote, i = node.raw[0], start
        while i < len(lines):
            line = lines[i]
            i += 1
            indent = len(line) - len(line.lstrip(" "))
            if line.strip(_WS) and (indent <= column or line[indent : indent + 1] == "\t"):
                raise _NotRead(node.line, lines[node.line - 1])
            end = _quoted_end(line[indent:], quote, i)
            if end is not None:
                if not _comment_only(line[indent + end :]):
                    raise _NotRead(i, line)
                node.value = _QUOTED_LINES
                return i
        raise _NotRead(node.line, lines[node.line - 1])

    while number < len(lines):
        line = lines[number]
        number += 1
        indent = len(line) - len(line.lstrip(" "))
        if block_column is not None:
            if not line.strip(_WS):
                continue
            if indent > block_column:
                # Its lines are indented as its first one; a line between that and the key is invalid YAML.
                if block_content is None:
                    block_content = indent
                elif indent < block_content:
                    raise _NotRead(number, line)
                continue
            block_column = None
        if plain is not None:
            # A plain scalar goes on over more-indented lines (a comment ends it). Nothing under a scalar can be a key,
            # so these lines are text whatever they look like; only `: ` would make the YAML invalid.
            if content(number - 1) and indent > plain[1]:
                if line[indent : indent + 1] == "\t" or re.search(r":(?:[ \t]|$)", line.split(" #")[0].rstrip(_WS)):
                    raise _NotRead(number, line)
                plain[0].value = _LINES
                if re.search(r"[ \t]#", line):
                    plain = None
                continue
            if line.strip(_WS):
                plain = None
        if not content(number - 1):
            continue
        body = line[indent:]
        if body[0] == "\t":
            raise _NotRead(number, line)
        if not started and indent == 0 and body.startswith("%"):
            if directive or not re.fullmatch(r"%YAML[ \t]+1\.2", body.split("#")[0].rstrip(_WS)):
                raise _NotRead(number, line)
            directive = True
            continue
        if not started and indent == 0 and body.startswith("---") and _comment_only(body[3:]):
            started = True
            continue
        if directive and not started:
            raise _NotRead(number, line)  # %YAML must be followed by ---
        started = True
        is_item = body == "-" or body.startswith(("- ", "-\t"))
        rest = body[1:].lstrip(_WS) if is_item else body
        try:
            pair = _split_key(rest)
            if pair is None and not is_item:
                raise _NotRead(number, line)
            if is_item:
                item = _Node(number, indent + 0.5, None)
                attach(item)
                if pair is None:
                    item.value, item.raw = _value(rest, number)
                    node, column = item, indent
                else:
                    column = indent + len(body) - len(rest)
                    node = _Node(number, column, pair[0])
                    attach(node)
                    node.value, node.raw = _value(pair[1], number)
            else:
                assert pair is not None
                node = _Node(number, indent, pair[0])
                attach(node)
                node.value, node.raw = _value(pair[1], number)
                column = indent
        except _NotRead as e:
            raise _NotRead(number, line) from e
        if node.raw.startswith("&"):
            if node.raw[1:] in anchors:
                raise _NotRead(number, line)  # an anchor defined twice
            anchors.add(node.raw[1:])
        elif node.value is _ALIAS and node.raw[1:] not in anchors:
            raise _NotRead(number, line)  # an alias with no anchor before it
        if node.value is _BLOCK:
            block_column, block_content = column, None
            if digit := re.search(r"[1-9]", node.raw):
                block_content = column + int(digit.group())
        elif node.value is _OPEN:
            node.value = None
            number = flow_lines(node, number)
        elif node.value is _OPEN_MAP:
            number, _ = mapping_lines(node, number)
        elif node.value is _OPEN_QUOTE:
            number = quoted_lines(node, number, column)
        elif isinstance(node.value, _Scalar) and not node.value.quoted and not re.search(r"[ \t]#", line):
            plain = node, column
        elif node.value is None and node.key is not None:
            following = next((i for i in range(number, len(lines)) if content(i)), None)
            if following is not None and len(lines[following]) - len(lines[following].lstrip(" ")) > column:
                below = lines[following].strip(_WS)
                if below.split(" #")[0].rstrip(_WS) == "[":  # Prettier's `key:` with `[` alone on the next line
                    number = flow_lines(node, following + 1)
                elif not below.startswith(("-", "#")) and _split_key(below) is None and _plain(below) is not None:
                    plain = node, column  # a plain scalar starting on the line after its key
                    node.raw = below
        if isinstance(node.value, dict):  # `key: {a: b}` is the mapping a: b, with nothing indented under it
            node.children, node.value = _flow_children(node.value, node.line, node.indent), None
            node.sealed = True
    return root


def _scalar(node: _Node) -> _Scalar:
    if isinstance(node.value, _Scalar) and not node.children:
        return node.value
    if isinstance(node.value, _Opaque):
        raise _Unsure(f"{node.key or 'a list item'}: {node.value.what} isn't read", node.line)
    if node.value is None and not node.children:
        raise _Unsure(f"{node.key or 'a list item'} has no value", node.line)
    raise _Unsure(f"{node.key or 'a list item'}: {node.raw or '...'} isn't a single value", node.line)


def _data(node: _Node) -> object:
    """A matrix value: a scalar, a list of scalars, a list of flat mappings or a mapping of those."""
    if isinstance(node.value, _Opaque):
        raise _Unsure(f"{node.key or 'a list item'}: {node.value.what} isn't read", node.line)
    if node.value is not None:
        return node.value
    if not node.children:
        raise _Unsure(f"{node.key or 'a list item'} has no value", node.line)
    if all(c.key is None for c in node.children):
        return [_data(c) for c in node.children]
    return {str(c.key): _data(c) for c in node.children}


def _same(a: _Scalar, b: _Scalar) -> bool:
    """Equal as GitHub compares matrix values; unsure where type or case would decide it."""
    strict = a.typed() == b.typed()
    if strict != (a.text.lower() == b.text.lower()):
        raise _Unsure(f"can't tell whether GitHub counts {a.text!r} and {b.text!r} as the same value", b.line)
    return strict


def _matrix(job: _Node, where: _Node) -> tuple[list[dict[str, _Scalar]], int]:
    """The job's matrix combinations, worked out as GitHub does, and the matrix's line."""
    strategy = job.get("strategy")
    node = strategy.get("matrix") if strategy is not None else None
    if node is None:
        raise _Unsure("the job has no strategy.matrix", where.line)
    if isinstance(node.value, _Scalar) and "${{" in node.value.text:
        raise _Computed(f"the matrix is computed ({node.raw})", node.line)
    data = _data(node)
    if not isinstance(data, dict):
        raise _Unsure("can't read the matrix", node.line)

    def scalars(key: str, value: object) -> list[_Scalar]:
        if isinstance(value, _Scalar) and "${{" in value.text:
            raise _Computed(f"matrix.{key} is computed ({value.text})", value.line)
        if value == []:
            raise _Unsure(f"matrix.{key} is an empty list", node.line)
        if not isinstance(value, list) or not all(isinstance(v, _Scalar) for v in value):
            raise _Unsure("the matrix has values that aren't a plain list", node.line)
        for v in value:
            if "${{" in v.text:
                raise _Computed(f"the matrix has a computed value ({v.text})", v.line)
        return value

    def entries(key: str) -> list[dict[str, _Scalar]]:
        value = data.get(key, [])
        if isinstance(value, _Scalar) and "${{" in value.text:
            raise _Computed(f"the matrix's {key} is computed ({value.text})", value.line)
        if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
            raise _Unsure(f"can't read the matrix's {key}", node.line)
        out = []
        for entry in value:
            for v in entry.values():
                if not isinstance(v, _Scalar):
                    raise _Unsure(f"the matrix's {key} has values that aren't plain", node.line)
                if "${{" in v.text:
                    raise _Computed(f"the matrix has a computed value ({v.text})", v.line)
            out.append(entry)
        return out

    dims = {key: scalars(key, value) for key, value in data.items() if key not in ("include", "exclude")}
    include, exclude = entries("include"), entries("exclude")
    keys = {*dims, *(k for e in include + exclude for k in e)}
    if len({k.lower() for k in keys}) != len(keys):
        raise _Unsure(f"the matrix has keys that differ only in case ({', '.join(sorted(keys))})", node.line)
    if any(k not in dims for e in exclude for k in e):
        raise _Unsure("the matrix excludes a key it doesn't have", node.line)
    # https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
    # Excludes first (an exclude matches every combination that has all its values); then each include is added to
    # every original combination whose original values it doesn't change (later includes may overwrite what earlier
    # ones added), or else becomes a combination of its own. Every comparison is made, so one that can't be told apart
    # is refused even where another value already decides the match.
    combos: list[dict[str, _Scalar]] = []
    if dims:
        for values in itertools.product(*dims.values()):
            combo = dict(zip(dims, values, strict=True))
            if not any(all([_same(combo[k], v) for k, v in e.items()]) for e in exclude):
                combos.append(combo)
    extra = []
    for entry in include:
        matching = [c for c in combos if all([_same(c[k], v) for k, v in entry.items() if k in dims])]
        for c in matching:
            c.update(entry)
        if not matching:
            extra.append(dict(entry))
    return combos + extra, node.line


def _matrix_runs(job: _Node) -> None:
    """Refuse a job whose matrix may have no combinations, since GitHub then runs nothing. A computed matrix is
    refused only where it's used; one with values that aren't plain still has a combination for sure when every
    dimension is a list with something in it and nothing is excluded."""
    try:
        every, line = _matrix(job, job)
    except _Computed:
        return
    except _Unsure:
        node = job.get("strategy").get("matrix")  # type: ignore[union-attr]
        try:
            data = _data(node)  # type: ignore[arg-type]
        except _Unsure:
            data = None
        if not isinstance(data, dict) or "exclude" in data:
            raise
        dims = [v for k, v in data.items() if k not in ("include", "exclude")]
        include = data.get("include")
        if not all(isinstance(v, list) and v for v in dims) or not (dims or isinstance(include, list) and include):
            raise
        return
    if not every:
        raise _Unsure("the matrix is empty", line)


def _matrix_values(job: _Node, key: str, where: _Node) -> list[_Scalar]:
    combos, line = _matrix(job, where)
    if not combos:
        raise _Unsure("the matrix is empty", line)
    if not all(key in c for c in combos):
        raise _Unsure(f"matrix.{key} isn't set in every matrix combination", where.line)
    values: dict[tuple[int, str, bool], _Scalar] = {}
    for c in combos:
        values.setdefault((c[key].line, c[key].text, c[key].quoted), c[key])
    return list(values.values())


def _values(job: _Node, node: _Node) -> list[tuple[_Scalar, str]]:
    """The values a `key: literal` or `key: ${{ matrix.KEY }}` takes, each with how it was found."""
    value = _scalar(node)
    if m := _MATRIX_REF_RE.fullmatch(value.text):
        return [
            (v, f"{node.key}: {v.text} (matrix.{m.group(1)}, line {node.line})")
            for v in _matrix_values(job, m.group(1), node)
        ]
    if "${{" in value.text:
        raise _Unsure(f"{node.key}: {value.text} is an expression", node.line)
    return [(value, f"{node.key}: {value.text}")]


def _literal(node: _Node) -> str:
    """A setup input that must be written out: an expression (matrix.* included) can't be read."""
    value = _scalar(node)
    if "${{" in value.text:
        raise _Unsure(f"{node.key}: {value.text} is an expression", node.line)
    return value.text


@dataclass(frozen=True)
class _Setup:
    """An actions/setup-python or astral-sh/setup-uv step, with its inputs by lowercase name (as Actions reads them)."""

    step: _Node
    action: str
    inputs: dict[str, _Node]

    @property
    def setup_python(self) -> bool:
        return self.action == "actions/setup-python"


# The inputs that pick the interpreter: a step that sets one under `if:` may or may not run.
_INTERPRETER_INPUTS = ("python-version", "architecture", "freethreaded", "python-arch")


def _setups(steps: _Node | None) -> list[_Setup]:
    """The job's setup steps. A step whose `uses`, `with` or inputs can't be read for sure refuses the whole job, since
    it might set the architecture or the Python."""
    if steps is None:
        return []
    if steps.value == []:
        return []
    if steps.value is not None:
        raise _Unsure(f"steps: {steps.raw} isn't read", steps.line)
    found = []
    for step in steps.children:
        if step.key is not None or step.value is not None:
            raise _Unsure(f"can't read the step {step.raw or step.key}", step.line)
        uses, with_, condition = step.get("uses"), step.get("with"), step.get("if")  # keys are case-sensitive
        if uses is None:
            continue
        try:
            text = _scalar(uses).text
        except _Unsure as e:
            raise _Unsure(f"can't read the uses: {uses.raw or '...'} of a step", e.line) from None
        action = text.split("@", 1)[0].lower()
        if "${{" in text:
            raise _Unsure(f"uses: {text} is an expression", uses.line)
        if action not in _PYTHON_ACTIONS:
            continue
        inputs: dict[str, _Node] = {}
        if with_ is not None:
            if with_.value is not None or any(c.key is None for c in with_.children):
                raise _Unsure(f"with: {with_.raw or '...'} isn't read", with_.line)
            for c in with_.children:
                name = str(c.key).lower()  # input names are case-insensitive (INPUT_<NAME>)
                if name in inputs:
                    raise _Unsure(f"with: sets {name} twice", c.line)
                inputs[name] = c
        if condition is not None and any(name in inputs for name in _INTERPRETER_INPUTS):
            raise _Unsure(f"the {action} step has if: {condition.raw or '...'}, so it may not run", condition.line)
        found.append(_Setup(step, action, inputs))
    return found


# An architecture a job's setup step asks for: None (the runner's own), the input's value, the matrix key it's
# taken from, or why it can't be read.
_Arch = str | None | tuple[str, int] | _Unsure


def _arch(setup: _Setup) -> _Arch:
    if not setup.setup_python:
        if (node := setup.inputs.get("python-arch")) is not None:
            return _Unsure(f"setup-uv's python-arch: {node.raw} isn't read", node.line)
        return None
    if (node := setup.inputs.get("architecture")) is None:
        return None
    try:
        value = _scalar(node)
    except _Unsure as e:
        return _Unsure(f"architecture: {node.raw} isn't read", e.line)
    if m := _MATRIX_REF_RE.fullmatch(value.text):
        return m.group(1), node.line
    if "${{" in value.text:
        return _Unsure(f"architecture: {value.text} isn't read", node.line)
    try:
        return value.plain_text() or None  # '' is setup-python's default: the runner's own
    except _Unsure as e:
        return e


def _image_sources(
    image: ImageTarget, written: str, platform: str | None = None, runner_cpu: bool = False, note: str | None = None
) -> tuple[str, str]:
    """The (target, Python) source notes for an image written as a Dockerfile FROM or a job's container or image.
    note, when given, says which platform the image was read for instead of the default notes."""
    notes = []
    if note:
        notes.append(note)
    elif runner_cpu and platform:
        # Actions pulls the image for this CPU; a Dockerfile's explicit --platform already appears in its source line.
        notes.append(f"read as {platform}, the runner's CPU")
    elif not platform and not image.platform.startswith("win"):
        notes.append("no --platform: read as linux/amd64")
    if image.glibc or image.musl:
        notes.append(f"glibc {image.glibc}" if image.glibc else f"musl {image.musl}")
    detail = written + (f" ({', '.join(notes)})" if notes else "")
    return detail, written


# The CPU of every GitHub-hosted runner wheelgap knows, from runner_target's table. A container job pulls its image
# without forcing a platform, so Actions runs the image's build for the runner's CPU (a job on ubuntu-24.04-arm runs
# python:3.13-alpine's aarch64 build). Windows containers can only run on a Windows host, so they need no platform.
_RUNNER_PLATFORM = {
    "linux-x86_64": "linux/amd64",
    "linux-aarch64": "linux/arm64",
}


def _container_image(job: _Node) -> tuple[str, _Scalar] | None:
    """The image a job runs in, or None when it has no `container:`.

    A job in a container runs in that image pulled for the runner's CPU, so its runner's OS and libc say nothing about
    its target. `container:` may be a literal image string or a mapping whose only key is a literal `image:`; anything
    else (an expression, a value that isn't a single scalar, or a mapping with `options:` that could change the
    platform, `services:` sidecars, or any other key) is refused rather than guessed.
    """
    container = job.get("container")
    if container is None:
        return None
    node = container
    if container.value is None and container.children:
        for child in container.children:
            if child.key != "image":
                raise _Unsure(f"container: {child.key} isn't read", child.line)
        image = container.get("image")
        assert image is not None  # the loop above left only an image: key
        node = image
    value = _scalar(node)
    if "${{" in value.text:
        raise _Unsure(f"container: {value.text} is an expression", node.line)
    return value.plain_text(), value


def _container_platform(job: _Node, image: ImageTarget, line: int) -> str:
    """The platform a container job's image is pulled for, from its runner's label.

    GitHub Actions only supports job containers on Linux runners. A runner wheelgap can't resolve (an expression, a
    matrix, an unknown label) or a non-Linux runner, as well as a Windows container image, stays under "Couldn't read"
    rather than being guessed.
    """
    runs_on = job.get("runs-on")
    if runs_on is None:
        raise _Unsure("no runs-on line, so GitHub wouldn't run the container job", job.line)
    if runs_on.children or isinstance(runs_on.value, list):
        raise _Unsure(f"runs-on {runs_on.raw or 'with a group or labels'} isn't a single label", runs_on.line)
    label = _scalar(runs_on)
    if m := _MATRIX_REF_RE.fullmatch(label.text):
        raise _Unsure(f"runs-on: matrix.{m.group(1)} isn't a single label", runs_on.line)
    if "${{" in label.text:
        raise _Unsure(f"runs-on: {label.text} is an expression", runs_on.line)
    try:
        text = label.plain_text()
    except _Unsure as e:
        raise _Unsure(f"runs-on: {e.reason}", e.line) from None
    target = runner_target(text)
    if target is None:
        raise _Unsure(f"{text} isn't a GitHub-hosted runner wheelgap knows", label.line)
    if not target.startswith("linux-"):
        raise _Unsure(f"job containers only run on Linux runners, not {text}", label.line)
    if text.lower() == "ubuntu-slim":
        raise _Unsure("ubuntu-slim runs in a container itself, so it can't run a job container", label.line)
    if image.platform.startswith("win"):
        raise _Unsure("container: Windows container images aren't supported on GitHub Actions Linux runners", line)
    return _RUNNER_PLATFORM[target]


def _container_libc(job: _Node) -> str | None:
    """ "musl" or "glibc" for a job's readable literal `container:` image, "unknown" for a `container:` whose image
    can't be read (_read_job reports why), or None for a job without one. setup-python has no musl builds, so it can't
    install a Python in a musl image, and in an unknown one it may not either."""
    if "container" not in {c.key for c in job.children}:
        return None
    try:
        found = _container_image(job)
        if found is None:
            return None
        return "musl" if parse_image(found[0]).musl else "glibc"
    except (_Unsure, DockerfileError):
        return "unknown"


def _picks_python(setup: _Setup) -> bool:
    """Whether a setup step picks the job's interpreter (as _job_archs counts it)."""
    return setup.setup_python or bool({"python-version", "python-arch"} & setup.inputs.keys())


def _refuse_services(job: _Node) -> None:
    """`services:` sits beside `container:` at the job level (or inside it in older, invalid layouts). GitHub's
    service containers aren't read, so refuse the job's target rather than guess from the image (#270)."""
    if (services := job.get("services")) is not None:
        raise _Unsure("services isn't read", services.line)
    container = job.get("container")
    if container is not None and (services := container.get("services")) is not None:
        raise _Unsure("container: services isn't read", services.line)


def _container_target(job: _Node, image: str, line: int) -> tuple[ImageTarget, str]:
    """A container job's image, read for the platform Actions pulls it on (the runner's CPU)."""
    try:
        parsed = parse_image(image)
    except DockerfileError as e:
        raise _Unsure(f"container: {str(e).removesuffix(', pass -p/-t instead')}", line) from None
    platform = _container_platform(job, parsed, line)
    try:
        return parse_image(image, platform), platform
    except DockerfileError as e:
        raise _Unsure(f"container: {str(e).removesuffix(', pass -p/-t instead')}", line) from None


def _job_archs(setups: list[_Setup]) -> list[str | None | tuple[str, int]]:
    # Each step that picks an interpreter adds its architecture; a setup-uv step without a Python picks none.
    pickers = [s for s in setups if _picks_python(s)]
    archs = list(dict.fromkeys(_arch(s) for s in pickers)) or [None]
    if unread := next((a for a in archs if isinstance(a, _Unsure)), None):
        raise _Unsure(f"{unread.reason}, so the job's target isn't read", unread.line)
    return [a for a in archs if not isinstance(a, _Unsure)]


def _apply_arch(
    combo: dict[str, _Scalar],
    arch: str | None | tuple[str, int],
    target: str,
    detail: str,
    where_label: str,
    line: int,
    path: str,
    *,
    glibc: str | None = None,
    musl: str | None = None,
    macos: str | None = None,
) -> Found | tuple[int | None, str]:
    if isinstance(arch, tuple):
        # A combination without the key (or with '') gives setup-python '', so the target's own.
        value = combo.get(arch[0])
        try:
            chosen = value.plain_text() if value is not None else None
        except _Unsure as e:
            return (e.line, e.reason)
        if chosen:
            detail += f", architecture: {chosen} (matrix)"
        chosen = chosen or None
    else:
        chosen = arch
        if chosen is not None:
            detail += f", architecture: {chosen}"
    mapped = target if chosen is None else _ARCHITECTURES.get((target, chosen))
    if mapped is None:
        return (line, f"setup-python's architecture: {chosen} on {where_label} isn't read")
    return Found(mapped, path, line, detail, glibc=glibc, musl=musl, macos=macos)


def _read_job(job: _Node, path: str, out: Suggestions) -> None:
    def unsure(e: _Unsure) -> None:
        out.unreadable.append(Unreadable(path, e.line, f"job {job.key}: {e.reason}"))

    try:
        if job.value is not None:
            raise _Unsure("can't read the job", job.line)
        setups = _setups(job.get("steps"))
        if (strategy := job.get("strategy")) is not None and strategy.get("matrix") is not None:
            # A matrix that can't be read might have no combinations, and then GitHub runs no job at all; a computed
            # one is refused only where it's used.
            _matrix_runs(job)
    except _Unsure as e:
        unsure(_Unsure(e.reason + " (neither its runner nor its Python is read)", e.line))
        return

    # setup-python has no musl builds, so in an Alpine image its step fails and the job never runs: neither its
    # Python nor the job's target (nor the image's Python) is suggested; the job is refused below.
    libc = _container_libc(job)
    musl_setup_python = libc == "musl" and any(s.setup_python for s in setups)
    if libc in ("musl", "unknown"):
        # In an image that can't be read, setup-python may fail too (the image may be musl), so its Python isn't
        # suggested either; the job itself is refused below for its unreadable container.
        setups = [s for s in setups if not s.setup_python]
    for setup in setups:
        try:
            _read_python(job, setup, path, out)
        except _Unsure as e:
            unsure(e)

    try:
        found: set[Found] = set()
        refused: set[tuple[int | None, str]] = set()
        if "container" in {c.key for c in job.children}:
            container = _container_image(job)
            assert container is not None  # the key is present, so _container_image reads it or refuses the job
            image, image_node = container
            _refuse_services(job)
            if musl_setup_python:
                raise _Unsure(
                    f"setup-python has no musl builds, so it can't install a Python in {image}", image_node.line
                )
            parsed, platform = _container_target(job, image, image_node.line)
            source = f"container: {image}"
            detail, python_source = _image_sources(parsed, source, platform, runner_cpu=True)
            if not any(_picks_python(s) for s in setups):
                # A step that picks the interpreter puts its own Python first on PATH, as on a runner.
                out.pythons.append(Found(parsed.python, path, image_node.line, python_source))
            archs = _job_archs(setups)
            combos: list[dict[str, _Scalar]] = [{}]
            if any(isinstance(a, tuple) for a in archs):
                combos, line = _matrix(job, job.get("container") or job)
                if not combos:
                    raise _Unsure("the matrix is empty", line)
            for combo in combos:
                for arch in archs:
                    result = _apply_arch(
                        combo,
                        arch,
                        parsed.platform,
                        detail,
                        f"{source} ({platform})",
                        image_node.line,
                        path,
                        glibc=parsed.glibc,
                        musl=parsed.musl,
                    )
                    if isinstance(result, Found):
                        found.add(result)
                    else:
                        refused.add(result)
        else:
            runs_on = job.get("runs-on")
            if runs_on is None:
                if job.get("uses") is None:  # a reusable workflow call has no runs-on of its own
                    raise _Unsure("no runs-on line", job.line)
                return
            if runs_on.children or isinstance(runs_on.value, list):
                raise _Unsure(f"runs-on {runs_on.raw or 'with a group or labels'} isn't a single label", runs_on.line)
            archs = _job_archs(setups)
            label_node = _scalar(runs_on)
            label_key = m.group(1) if (m := _MATRIX_REF_RE.fullmatch(label_node.text)) else None
            if label_key is None and "${{" in label_node.text:
                raise _Unsure(f"runs-on: {label_node.text} is an expression", runs_on.line)
            combos = [{}]
            if label_key is not None or any(isinstance(a, tuple) for a in archs):
                # Each matrix combination runs its own runner with its own architecture.
                combos, line = _matrix(job, runs_on)
                if not combos:
                    raise _Unsure("the matrix is empty", line)
            # Everything is worked out before anything is reported, so a refusal partway through the matrix leaves no
            # targets behind and the result doesn't depend on the matrix's order.
            for combo in combos:
                if label_key is None:
                    label_value, source = label_node, f"runs-on: {label_node.text}"
                elif label_key in combo:
                    label_value = combo[label_key]
                    source = f"runs-on: {label_value.text} (matrix.{label_key}, line {runs_on.line})"
                else:
                    raise _Unsure(f"matrix.{label_key} isn't set in every matrix combination", runs_on.line)
                try:
                    label = label_value.plain_text()
                except _Unsure as e:
                    refused.add((e.line, e.reason))
                    continue
                if (target := runner_target(label)) is None:
                    refused.add((label_value.line, f"{label} isn't a GitHub-hosted runner wheelgap knows"))
                    continue
                glibc, macos = _runner_os(label)
                for arch in archs:
                    result = _apply_arch(
                        combo,
                        arch,
                        target,
                        source,
                        label,
                        label_value.line,
                        path,
                        glibc=glibc,
                        macos=macos,
                    )
                    if isinstance(result, Found):
                        found.add(result)
                    else:
                        refused.add(result)
        order = list(PLATFORMS)
        for f in sorted(found, key=lambda f: (f.line, order.index(f.value), f.source)):
            if f not in out.targets:
                out.targets.append(f)
        for where, reason in sorted(refused, key=lambda r: (r[0] or 0, r[1])):
            unsure(_Unsure(reason, where))
    except _Unsure as e:
        unsure(e)


def _read_python(job: _Node, setup: _Setup, path: str, out: Suggestions) -> None:
    """The Python an actions/setup-python or astral-sh/setup-uv step installs."""
    version = setup.inputs.get("python-version")
    if version is None:
        if setup.setup_python:
            if (file := setup.inputs.get("python-version-file")) is not None:
                raise _Unsure(f"setup-python reads {_scalar(file).text}, which isn't read here", file.line)
            raise _Unsure(
                "setup-python with no python-version (it reads .python-version, or uses the runner's)", setup.step.line
            )
        return
    freethreaded = False
    if setup.setup_python and (node := setup.inputs.get("freethreaded")) is not None:
        text = _literal(node)
        if text not in _TRUE + _FALSE:
            raise _Unsure(f"freethreaded: {text} isn't true or false", node.line)
        freethreaded = text in _TRUE
    for value, source in _values(job, version):
        try:
            text = value.plain_text()
        except _Unsure as e:
            out.unreadable.append(Unreadable(path, e.line, f"job {job.key}: {e.reason}"))
            continue
        if not (m := _PYTHON_RE.fullmatch(text)):
            reason = f"job {job.key}: python-version {text} isn't a CPython version wheelgap can read"
            out.unreadable.append(Unreadable(path, value.line, reason))
            continue
        python = f"3.{m.group(1)}{m.group(2)}"
        if freethreaded and not python.endswith("t"):
            python, source = python + "t", f"{source}, freethreaded: true"
        if python.endswith("t") and int(m.group(1)) < 13:
            reason = f"job {job.key}: {source} asks for a free-threaded {text}, but free-threading starts at 3.13"
            out.unreadable.append(Unreadable(path, value.line, reason))
            continue
        out.pythons.append(Found(python, path, value.line, source))


def _not_read(e: _NotRead) -> str:
    """The reason for a file refused at a line outside the grammar, with that line cut to 60 characters."""
    shown = e.text.strip(_WS)
    shown = shown if len(shown) <= 60 else shown[:57] + "..."
    return f"uses YAML this scanner doesn't read: {shown}"


def read_workflow(path: Path, name: str, out: Suggestions) -> None:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        out.unreadable.append(Unreadable(name, None, "not UTF-8"))
        return
    except OSError as e:
        out.unreadable.append(Unreadable(name, None, f"couldn't read the file ({e.strerror or e})"))
        return
    read_workflow_text(text, name, out)


def read_workflow_text(text: str, name: str, out: Suggestions) -> None:
    try:
        root = _parse_tree(text)
        jobs = root.get("jobs")
        if jobs is None:
            raise _Unsure("no jobs")
        if jobs.value is not None or any(j.key is None for j in jobs.children):
            raise _Unsure("can't read the jobs", jobs.line)
    except _NotRead as e:
        out.unreadable.append(Unreadable(name, e.line, _not_read(e)))
        return
    except _Unsure as e:
        out.unreadable.append(Unreadable(name, e.line, e.reason))
        return
    for job in jobs.children:
        _read_job(job, name, out)


# GitLab.com hosted runner tags (2026-11-17):
# https://docs.gitlab.com/ci/runners/hosted_runners/linux/
# https://docs.gitlab.com/ci/runners/hosted_runners/windows/
# https://docs.gitlab.com/ci/runners/hosted_runners/macos/
_GITLAB_RUNNERS = {
    **{f"saas-linux-{size}-amd64": "linux-x86_64" for size in ("small", "medium", "large", "xlarge", "2xlarge")},
    **{f"saas-linux-{size}-arm64": "linux-aarch64" for size in ("small", "medium", "large")},
    "saas-windows-medium-amd64": "win-amd64",
    "saas-macos-medium-m1": "macos-arm64",
    "saas-macos-large-m2pro": "macos-arm64",
}
# The runner GitLab.com picks for a job without tags (on a self-managed instance it can be any runner).
_GITLAB_DEFAULT_RUNNER = "saas-linux-small-amd64"
# On the macOS runners `image:` names the macOS VM, not a container image: macos-15-xcode-16 is macOS 15, the newest
# a macOS wheel may need there (as _runner_os reads a pinned GitHub macOS runner).
_GITLAB_MACOS_IMAGE_RE = re.compile(r"macos-(\d+)-xcode-\d+", re.ASCII)
_GITLAB_GLOBALS = {
    "default",
    "image",
    "stages",
    "types",
    "variables",
    "workflow",
    "include",
    "cache",
    "services",
    "before_script",
    "after_script",
    "spec",
}
# Job keywords that can change a job's image or runner from elsewhere: only that job is refused.
_GITLAB_REFUSED = ("extends", "inherit", "trigger")


def _gitlab_anchors(node: _Node) -> None:
    """Refuse the whole file for YAML anchors and aliases, which can copy an image or tags into any job."""
    if node.raw.startswith("&") or node.value is _ALIAS:
        raise _Unsure("YAML anchors and aliases aren't read", node.line)
    for child in node.children:
        _gitlab_anchors(child)


def _gitlab_image(node: _Node) -> tuple[str, int]:
    """A literal `image:` (a string, or a mapping with only `name:`) and its line."""
    if node.value is None and node.children and all(child.key is not None for child in node.children):
        for child in node.children:
            if child.key != "name":
                raise _Unsure(f"image: {child.key} isn't read", child.line)
        image_name = node.get("name")
        assert image_name is not None  # the loop above left only a name: key
        node = image_name
    text = _scalar(node).plain_text()
    if "$" in text:
        raise _Unsure(f"image: {text} uses a variable, which isn't read", node.line)
    return text, node.line


def _read_gitlab_job(job: _Node, defaults: _Node, root: _Node, name: str, out: Suggestions) -> None:
    try:
        if job.value is not None or any(child.key is None for child in job.children):
            raise _Unsure("can't read the job", job.line)
        for keyword in _GITLAB_REFUSED:
            if (node := job.get(keyword)) is not None:
                raise _Unsure(f"{keyword} isn't read", node.line)
        if (parallel := job.get("parallel")) is not None and parallel.get("matrix") is not None:
            raise _Unsure("parallel: matrix isn't read", parallel.line)
        tags = job.get("tags") or defaults.get("tags")
        label: str | None = None
        target: str | None = None
        if tags is not None:
            values = _data(tags)
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], _Scalar):
                raise _Unsure("tags isn't one known hosted runner tag", tags.line)
            label = values[0].plain_text()
            target = _GITLAB_RUNNERS.get(label)
            if target is None:
                raise _Unsure(f"{label} isn't a GitLab-hosted runner tag wheelgap knows", tags.line)
        image = job.get("image") or defaults.get("image") or root.get("image")
        if image is None:
            if tags is None or target is None:
                raise _Unsure("no image or known hosted runner tag", job.line)
            out.targets.append(Found(target, name, tags.line, f"tags: {label}"))
            return
        if target == "win-amd64":
            raise _Unsure(f"image is set, but GitLab's hosted Windows runners ({label}) don't use image", image.line)
        image_text, line = _gitlab_image(image)
        if target == "macos-arm64":
            if not (m := _GITLAB_MACOS_IMAGE_RE.fullmatch(image_text)):
                raise _Unsure(
                    f"{label} runs a macOS VM, and image: {image_text} isn't one such as macos-15-xcode-16", line
                )
            source = f"image: {image_text} (tags: {label})"
            out.targets.append(Found(target, name, line, source, macos=f"{m.group(1)}.0"))
            return
        try:
            if parse_image(image_text).platform.startswith("win"):
                raise _Unsure(f"image: {image_text} is a Windows image, which GitLab's Linux runners can't run", line)
            platform = _RUNNER_PLATFORM[target] if target is not None else "linux/amd64"
            parsed = parse_image(image_text, platform)
        except DockerfileError as e:
            raise _Unsure(f"image: {str(e).removesuffix(', pass -p/-t instead')}", line) from None
        if target is not None:
            note = f"read as {platform}, the {label} runner's CPU"
        else:
            note = f"untagged: read as GitLab.com's default {_GITLAB_DEFAULT_RUNNER} runner"
        detail, python_source = _image_sources(parsed, f"image: {image_text}", platform, note=note)
        out.targets.append(Found(parsed.platform, name, line, detail, parsed.glibc, parsed.musl))
        out.pythons.append(Found(parsed.python, name, line, python_source))
    except _Unsure as e:
        out.unreadable.append(Unreadable(name, e.line, f"job {job.key}: {e.reason}"))


def read_gitlab(path: Path, out: Suggestions) -> None:
    name = path.name
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        out.unreadable.append(Unreadable(name, None, "not UTF-8"))
        return
    except OSError as e:
        out.unreadable.append(Unreadable(name, None, f"couldn't read the file ({e.strerror or e})"))
        return
    try:
        root = _parse_tree(text)
        if any(child.key is None for child in root.children):
            raise _Unsure("the pipeline isn't a mapping")
        _gitlab_anchors(root)
        if (include := root.get("include")) is not None:
            raise _Unsure("include isn't read", include.line)
        defaults = root.get("default") or _Node(0, 0, "default")
        if defaults.value is not None or any(child.key is None for child in defaults.children):
            raise _Unsure("default isn't a mapping", defaults.line)
    except _NotRead as e:
        out.unreadable.append(Unreadable(name, e.line, _not_read(e)))
        return
    except _Unsure as e:
        out.unreadable.append(Unreadable(name, e.line, e.reason))
        return
    for job in root.children:
        if job.key is not None and not job.key.startswith(".") and job.key not in _GITLAB_GLOBALS:
            _read_gitlab_job(job, defaults, root, name, out)


def _container_kind(name: str) -> str | None:
    """ "file" for a Dockerfile or Containerfile (Dockerfile, dockerfile.dev, Containerfile.prod, prod.Dockerfile),
    "template" for one that a tool fills in first (Dockerfile.j2), None for anything else."""
    lower = name.lower()
    if lower.endswith(_NOT_CONTAINERFILES):
        return None
    bases = ("dockerfile", "containerfile")
    if not (
        lower in bases
        or lower.startswith(tuple(f"{base}{sep}" for base in bases for sep in ".-_"))
        or lower.endswith(tuple(f".{base}" for base in bases))
    ):
        return None
    return "template" if lower.endswith(_TEMPLATE_SUFFIXES) else "file"


_OUTSIDE = "a symlink out of the project: not read"


def _outside(path: Path, root: Path) -> bool:
    """Whether path, its symlinks followed, leads out of the project folder (root, already resolved)."""
    try:
        return not path.resolve().is_relative_to(root)
    except (OSError, RuntimeError):  # a symlink loop: reading it fails, and says so
        return False


def dockerfiles(folder: Path, out: Suggestions) -> list[Path]:
    """Every Dockerfile and Containerfile in the project, outside hidden folders, virtual environments and the
    folders in _SKIP_DIRS. Templates, symlinks out of the project and folders that can't be opened are noted as
    unreadable."""
    root = folder.resolve()

    def failed(e: OSError) -> None:
        where = Path(e.filename).relative_to(folder).as_posix() if e.filename else "."
        out.unreadable.append(Unreadable(where, None, f"couldn't open the folder ({e.strerror or e})"))

    found: list[Path] = []
    for top, dirs, files in os.walk(folder, onerror=failed):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and d not in _SKIP_DIRS
            and not os.path.exists(os.path.join(top, d, "pyvenv.cfg"))  # False, not an error, when unreadable
        )
        for f in sorted(files):
            kind = _container_kind(f)
            if kind == "template":
                name = (Path(top) / f).relative_to(folder).as_posix()
                out.unreadable.append(Unreadable(name, None, "a template, filled in before it's built: not read"))
            elif kind == "file" and _outside(Path(top) / f, root):
                name = (Path(top) / f).relative_to(folder).as_posix()
                out.unreadable.append(Unreadable(name, None, _OUTSIDE))
            elif kind == "file":
                found.append(Path(top) / f)
    return found


def read_docker(path: Path, name: str, out: Suggestions) -> None:
    try:
        image = read_dockerfile(path)
        base, platform, line = final_from(path.read_bytes().decode("utf-8-sig"))
    except HeredocSyntaxError as e:  # its message starts with path:line:, so take the line and the bare reason
        out.unreadable.append(Unreadable(name, e.line, e.reason))
        return
    except DockerfileError as e:
        reason = str(e).removeprefix(f"{path}: ").removesuffix(", pass -p/-t instead")  # no -p/-t here
        out.unreadable.append(Unreadable(name, None, reason))
        return
    except OSError as e:
        out.unreadable.append(Unreadable(name, None, f"couldn't read the file ({e.strerror or e})"))
        return
    source = f"FROM {f'--platform={platform} ' if platform else ''}{base}"
    detail, python_source = _image_sources(image, source, platform)
    out.targets.append(Found(image.platform, name, line, detail, image.glibc, image.musl))
    out.pythons.append(Found(image.python, name, line, python_source))


def suggest(folder: Path) -> Suggestions:
    """Read the project's GitHub Actions, GitLab CI and Dockerfiles. Raises NotADirectoryError for a missing folder."""
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder}: not a folder")
    out = Suggestions()
    root = folder.resolve()
    workflows = folder / ".github" / "workflows"
    if workflows.is_dir() and _outside(workflows, root):
        out.unreadable.append(Unreadable(".github/workflows", None, _OUTSIDE))
    elif workflows.is_dir():
        try:
            paths = sorted(workflows.iterdir())
        except OSError as e:
            paths = []
            out.unreadable.append(
                Unreadable(".github/workflows", None, f"couldn't open the folder ({e.strerror or e})")
            )
        for path in paths:
            if path.suffix in (".yml", ".yaml") and path.is_file():
                name = path.relative_to(folder).as_posix()
                if _outside(path, root):
                    out.unreadable.append(Unreadable(name, None, _OUTSIDE))
                else:
                    read_workflow(path, name, out)
    gitlab = folder / ".gitlab-ci.yml"
    if gitlab.is_file() and _outside(gitlab, root):
        out.unreadable.append(Unreadable(".gitlab-ci.yml", None, _OUTSIDE))
    elif gitlab.is_file():
        read_gitlab(gitlab, out)
    for path in dockerfiles(folder, out):
        read_docker(path, path.relative_to(folder).as_posix(), out)
    out.unreadable = list(dict.fromkeys(out.unreadable))
    return out


def _where(item: Found | Unreadable) -> str:
    return f"{item.path}:{item.line}" if item.line is not None else item.path


def snippet(config: dict[str, object]) -> list[str]:
    # JSON strings and lists of them are TOML too (the values are plain ASCII names).
    return ["[tool.wheelgap]", *(f"{key} = {json.dumps(value)}" for key, value in config.items())]


def render_text(found: Suggestions) -> list[str]:
    lines: list[str] = []
    for title, items in (("Targets", found.targets), ("Pythons", found.pythons)):
        if not items:
            lines.append(f"{title}: none found")
            continue
        lines.append(f"{title}:")
        values = sorted({f.value for f in items}, key=list(PLATFORMS).index if title == "Targets" else _python_order)
        for value in values:
            lines.append(f"  {value}")
            seen = dict.fromkeys((_where(f), f.source) for f in items if f.value == value)
            width = max(len(where) for where, _ in seen)
            lines.extend(f"    {where:<{width}}  {source}" for where, source in seen)
    if found.unreadable:
        lines.append("Couldn't read (not guessed):")
        lines.extend(f"  {_where(u)}  {u.reason}" for u in found.unreadable)
    config = found.config()
    if not config.get("targets"):
        lines += ["", "No targets found, so there's nothing to suggest. Nothing was changed or checked."]
        return lines
    lines += [
        "",
        "Suggested settings for pyproject.toml (nothing was changed or checked). They combine every job, lint,",
        "release and bot jobs too, so keep only the platforms you ship to:",
        "",
    ]
    lines += snippet(config)
    lines += found.left_out()
    return lines


def to_json(found: Suggestions) -> dict[str, object]:
    return {
        "targets": [{"target": f.value, "file": f.path, "line": f.line, "from": f.source} for f in found.targets],
        "python": [{"python": f.value, "file": f.path, "line": f.line, "from": f.source} for f in found.pythons],
        "unreadable": [{"file": u.path, "line": u.line, "reason": u.reason} for u in found.unreadable],
        "config": found.config(),
    }
