"""Keep text from inputs from running as GitHub Actions workflow commands in a CI log.

The Actions runner reads every line a step prints, on stdout and stderr. A line whose first non-whitespace text is
`::` is a workflow command (`::error::...`, `::add-mask::...`, `::stop-commands::...`), and so is any line with
`##[` anywhere in it (the legacy form). Lines end at LF, CR or CRLF. wheelgap prints package names, versions,
paths and error messages from lockfiles, requirements files, Dockerfiles and index responses, and a pull request can
put anything in those. So the CLI writes through CommandGuard, which changes any line that would be read as a
command:

- `##[` becomes `\\u0023#[`;
- a line starting with `::` (after whitespace, a BOM and colour codes) gets its first `:` written as `\\u003a`.

Both are JSON escapes, so in `--json` output, where such text can only be inside a string, the decoded values are
unchanged; in text and Markdown the escape shows. Normal output never contains either, so it's never changed.

"Whitespace" is what .NET's TrimStart removes (NBSP, U+0085, U+3000, ...); regex \\s matches all of those. The
runner reads the log as UTF-8, so the guard writes UTF-8 to a pipe whatever PYTHONIOENCODING says: in latin-1,
`Â` + NBSP would be the bytes C2 A0, which the runner reads as NBSP alone, then `::`. A traceback, printed after
the guard is gone, would print exception text unguarded, so guarded_output prints it itself; if even that fails, it
writes a fixed message instead.

Problem matchers (`::add-matcher::`, set up by other steps) turn ordinary lines into annotations by design; they're
out of scope here.

See ActionCommand.TryParseV2 / TryParse and ActionCommandManager in https://github.com/actions/runner.
"""

from __future__ import annotations

import io
import re
import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

_BREAK = re.compile(r"(\r\n|\r|\n)")
# `::` at the start of a line, after whitespace (what .NET's TrimStart skips; \s covers it), a BOM (.NET drops one
# at the start of a pipe) or colour codes (the runner doesn't skip those, so this is only to be safe).
_LEADING_COMMAND = re.compile(r"\A((?:\s|\ufeff|\x1b\[[0-9;]*[A-Za-z])*)::")


def neutralize(line: str) -> str:
    """One line (no line breaks) changed so the runner won't read it as a workflow command. It never raises."""
    return _LEADING_COMMAND.sub(r"\1\\u003a:", line.replace("##[", "\\u0023#["), count=1)


# What the guard writes instead of an error it can't print safely, such as one raised while writing.
_FALLBACK = "wheelgap: error: an internal error occurred while writing output\n"


class CommandGuard(io.TextIOBase):
    """A text stream that writes each complete line to `raw` through neutralize().

    A line is held until its line break arrives, since a command can be split over several writes; finish()
    writes what's left at the end. To a stream that isn't a terminal, it writes UTF-8 to the binary buffer, as the
    runner reads it.
    """

    def __init__(self, raw: TextIO):
        self.raw = raw
        buffer = getattr(raw, "buffer", None)
        self._buffer = None if buffer is None or raw.isatty() else buffer
        if self._buffer is not None:
            raw.flush()  # anything already written to the text layer goes first
        self._pending: list[str] = []  # the held partial line, in pieces (joined once its line ends)

    def _emit(self, text: str) -> None:
        if self._buffer is None:
            self.raw.write(text)
        else:
            self._buffer.write(text.encode("utf-8", "backslashreplace"))
            if getattr(self.raw, "line_buffering", False):
                self._buffer.flush()

    def write(self, s: str) -> int:
        end = max(s.rfind("\n"), s.rfind("\r")) + 1
        if not end:
            self._pending.append(s)
            return len(s)
        # A CR that turns out to be half of a CRLF is fine: the empty "line" between them is left as it is.
        text = "".join(self._pending) + s[:end]
        self._pending = [s[end:]] if end < len(s) else []
        parts = _BREAK.split(text)
        self._emit("".join(part if i % 2 else neutralize(part) for i, part in enumerate(parts)))
        return len(s)

    def finish(self) -> None:
        if self._pending:
            pending, self._pending = "".join(self._pending), []
            self._emit(neutralize(pending))
        self.flush()

    def flush(self) -> None:  # a held partial line stays held: it may still turn into a command
        (self.raw if self._buffer is None else self._buffer).flush()

    def close(self) -> None:
        pass  # the raw stream isn't ours to close (IOBase's finalizer would also flush it, maybe after it's gone)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self.raw.isatty()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self.raw.encoding if self._buffer is None else "utf-8"

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return self.raw.errors if self._buffer is None else "backslashreplace"


def write_command(command: str, stream: TextIO) -> None:
    """Write a workflow command wheelgap means to send (such as an annotation it built and escaped itself) on its own
    line, past the guard. Everything else goes through it.

    The command must be one line, so it can only ever carry itself. A partial line the guard is holding is ended
    first (and neutralized as usual), so the command starts its own line.
    """
    if not command.startswith("::") or "\n" in command or "\r" in command:
        raise ValueError(f"not a single workflow command line: {command!r}")
    if isinstance(stream, CommandGuard):
        if stream._pending:
            stream.write("\n")
        stream._emit(command + "\n")
    else:
        stream.write(command + "\n")


@contextmanager
def guarded_output() -> Iterator[None]:
    """Route sys.stdout and sys.stderr through CommandGuard for the duration (once, if nested).

    An exception that escapes is printed here, through the guard, and becomes exit 2: left to the interpreter, its
    traceback (with whatever input text the message quotes) would be printed after the guard is gone. So is the
    message of sys.exit("...").
    """
    if isinstance(sys.stdout, CommandGuard) and isinstance(sys.stderr, CommandGuard):
        yield
        return
    out, err = sys.stdout, sys.stderr
    guards = CommandGuard(out), CommandGuard(err)
    sys.stdout, sys.stderr = guards
    failed = False
    try:
        yield
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            raise
        message = e.code
        failed = not _print_safely(lambda: print(message, file=sys.stderr))
        raise SystemExit(1) from None  # as the interpreter does for sys.exit("message")
    except Exception:  # not KeyboardInterrupt: its traceback is wheelgap's own code, no input text
        failed = not _print_safely(traceback.print_exc)
        raise SystemExit(2) from None
    finally:
        sys.stdout, sys.stderr = out, err
        for guard in guards:
            failed = not _print_safely(guard.finish) or failed
        if failed:
            err.write(_FALLBACK)  # a fixed message: the text that failed may be what mustn't be printed
            err.flush()


def _print_safely(action: Callable[[], object]) -> bool:
    """Run an action that prints through the guard; False if it raised (whatever it printed so far went through
    the guard, and the rest is dropped)."""
    try:
        action()
    except Exception:
        return False
    return True
