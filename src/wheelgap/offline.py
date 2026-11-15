"""Shared validation for checks that must not contact an index."""

from __future__ import annotations

from .pypi import may_look_up
from .sources import Source, SourceError


def undo_offline(how: str) -> str:
    """How to turn offline mode off: drop the flag, or override the config key for one run."""
    return "drop --offline" if how == "--offline" else "pass --online for this run"


def require_offline(
    sources: list[Source], how: str = "--offline", *, undo: str | None = None, simple_index: str | None = None
) -> None:
    """--offline: raise SourceError if an input has packages that can only be answered by looking them up."""
    problems = []
    for s in sources:
        # A private-index package with no files is skipped by fill_all, not looked up, so it doesn't count. A
        # pixi.lock records only the file it picked per platform (partial_files), which can't answer other targets.
        need = [
            p.name
            for p in s.packages
            if not p.skip
            and not p.error
            and (p.files is None or p.partial_files)
            and may_look_up(p.registry, simple_index)
        ]
        if need:
            names = ", ".join(need[:3]) + (", …" if len(need) > 3 else "")
            why = " (it records only the file pixi picked for each platform)" * any(p.partial_files for p in s.packages)
            problems.append(
                f"{s.label}{why} needs the index for {len(need)} package{'s' if len(need) != 1 else ''} ({names})"
            )
    if problems:
        raise SourceError(
            f"{how}: {'; '.join(problems)}; use a lockfile that lists each package's files, "
            f"or {undo or undo_offline(how)}"
        )
