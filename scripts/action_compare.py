"""The GitHub Action's compare-to helper (#201): the job summary, the PR comment, and safe log lines.

    python3 scripts/action_compare.py report --diff-json diff.json --exit-code 1 --lockfile uv.lock \\
        --base 1a2b3c4 --base-branch --head 9f8e7d6 --markdown report.md --payload comment.json
    python3 scripts/action_compare.py pick --comments comments.json --login 'github-actions[bot]' --lockfile uv.lock
    python3 scripts/action_compare.py log diff.err

The Action runs this with the runner's own Python, not wheelgap's, so it uses only the standard library and
doesn't import wheelgap.

- `report` writes the Markdown report (for $GITHUB_STEP_SUMMARY) and, with --payload, the JSON body for GitHub's
  comment API: the same report under a hidden marker that names the lockfile (--comment-markdown writes that body as
  Markdown too). When `wheelgap diff --fix` gave a `fix` for a cell, the table has a Fix column (an upgrade, an
  unofficial gap wheel, or none and why) and a "How to fix" list after it with the `how` of each upgrade. Its exit
  code is the step's result: 0 only when `wheelgap diff` exited 0 with a valid report listing nothing, 1 when it
  exited 1 with a valid report listing something, and 2 for anything else (no report, an error, or an exit code that
  disagrees with the report), so the step never passes on a guess.
  Targets `wheelgap diff` didn't compare (its `not_compared`: a Python a lockfile's requires-python leaves out) are
  listed after the counts with the reason, and never count as worse. A Python only the new lockfile leaves out is one
  the change dropped: its paragraph leads with "This change drops Python X.Y support", and the log gets a
  `::warning::` line naming only that version (checked to be one, never other text from the report).
- `pick` prints the id of this lockfile's comment from `gh api --paginate` output: the first one by `--login`
  (the token's own identity) whose body starts with the marker. Anyone else's comment is never edited.
- `log` copies wheelgap's stderr to stdout with anything the Actions runner would run as a workflow command
  defused: a line whose first non-blank text is `::`, and `##[` anywhere.

Package names, versions and wheelgap's messages come from the pull request. They reach the log only through `log`,
and the report escapes them: every character but ASCII letters, digits, spaces and `-` becomes an HTML character
reference, inside `<code>` in a raw HTML block. Markdown doesn't parse an HTML block, so nothing can become a table
cell, a link or emphasis, and GitHub doesn't turn text inside `<code>` into @mentions or issue links.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from urllib.parse import quote

# As wheelgap's own text output shows each status (check.CELL; a test keeps the two in step).
CELLS = {
    "wheel": "✓",
    "source": "● source",
    "missing": "✗ missing",
    "unknown": "? unknown",
    "n/a": "–",
    "bundled": "✓ bundled",
}
MAX_ESCAPED = 400  # characters of one escaped name, version or target
MAX_MESSAGE = 2_000  # characters of an escaped error message
MAX_COMMENT = 65_536  # GitHub refuses longer comments
MAX_UNCHECKED = 20  # names listed as unchecked
MARKER = "<!-- wheelgap-compare lockfile={} -->"
# fix.REASON_TEXT and fix.RESOLVE_NOTE, as --fix words them (a test keeps them in step).
FIX_REASONS = {
    "no_newer_release": "no newer release has a wheel",
    "loses_wheel": "every newer release with a wheel drops one it has elsewhere",
    "uv_may_fork": "every newer release with a wheel drops one it has on another Python",
    "no_binary": "--no-binary, so pip uses no wheel for it",
}
RESOLVE_NOTE = "wheelgap doesn't resolve dependencies: the rest of them must allow these releases."
GAP_WHEEL_NOTE = (
    "Gap wheels are unofficial: built by wheelgap from the PyPI sdist, not by the package's maintainers "
    '(<a href="https://wheelgap.github.io/wheelgap/recipes/#installing-from-the-gap-wheel-index">how to use them</a>).'
)
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
PYTHON = re.compile(r"[0-9]{1,2}\.[0-9]{1,3}t?")  # a target Python as wheelgap labels it: 3.13, 3.14t


def esc(text: object, limit: int = MAX_ESCAPED) -> str:
    """`text` as inert HTML of at most `limit` characters (then `…`): letters, digits, spaces and `-` as they
    are, everything else as `&#N;`. Control and format characters (line breaks, bidi overrides) are shown as
    `\\uXXXX` instead, so they can't reorder or split what's around them."""
    out: list[str] = []
    size = 0
    for ch in str(text):
        if ch.isascii() and (ch.isalnum() or ch in " -"):
            piece = ch
        elif unicodedata.category(ch).startswith("C"):
            piece = "".join(f"&#{ord(c)};" for c in f"\\u{ord(ch):04x}")
        else:
            piece = f"&#{ord(ch)};"
        if size + len(piece) > limit:
            out.append("…")
            break
        out.append(piece)
        size += len(piece)
    return "".join(out)


def code(text: object, limit: int = MAX_ESCAPED) -> str:
    return f"<code>{esc(text, limit)}</code>"


def versions(vs: list) -> str:
    return ", ".join(code(v) for v in vs) if vs else "?"


def cell(side: dict, prefix: str = "") -> str:
    status = side.get(f"{prefix}status")
    if status == "skipped":
        return f"skipped ({code(side.get(f'{prefix}skip_reason', '?'))})"
    if isinstance(status, str) and status in CELLS:
        return CELLS[status]
    return code(status)


def name_cell(pkg: dict, target: str) -> str:
    """The package's name, with the direct dependency that brings it in on this target, as `wheelgap diff` words it."""
    name = code(pkg.get("name"))
    direct = pkg.get("direct")
    via = direct.get(target) if isinstance(direct, dict) else None
    if isinstance(via, list) and via and all(isinstance(v, str) for v in via):
        name += f" (via {code(via[0])}{f' +{len(via) - 1}' if len(via) > 1 else ''})"
    return name


def fix_cell(fix: object) -> str:
    """One cell's way out, from `wheelgap diff --fix --json`'s `fix` (fix.to_json), in --fix's words."""
    if not isinstance(fix, dict):
        return ""  # an unknown or skipped cell: --fix has no answer for it
    kind = fix.get("kind")
    if kind == "upgrade":
        text = f"upgrade to {code(fix.get('version'))}"
        c = fix.get("constraint")
        if isinstance(c, dict) and c.get("allows") is False and isinstance(c.get("requirements"), list):
            reqs = ", ".join(code(r) for r in c["requirements"])
            if c.get("via") == "constraint":
                text += f" (needs {reqs} in the -c constraints of {code(c.get('file'))} relaxed)"
            else:
                text += f" (needs {code(c.get('file'))}'s {reqs} relaxed)"
        if isinstance(fix.get("gap_wheel"), dict):
            text += "; if you can't, an unofficial gap wheel"
        return text
    if kind == "gap_wheel":
        return f"an unofficial gap wheel: {code(fix.get('wheel'))}"
    if kind == "none":
        reason = fix.get("reason")
        if reason == "not_looked_up":
            return "newer releases weren't looked up (the log says why)"
        text = f"no fix yet: {esc(FIX_REASONS[reason]) if reason in FIX_REASONS else code(reason)}"
        if fix.get("abi3_only") is True:
            text += "; only abi3 wheels, which free-threaded Python can't use"
        return text
    return code(kind)


def has_fixes(data: dict) -> bool:
    """Whether `wheelgap diff --fix` gave a way out for any cell, so the table gets its Fix column."""
    return any(
        isinstance(side.get("fix"), dict) for pkg in data["worse"] + data["new"] for side in pkg["targets"].values()
    )


def how_lines(data: dict) -> list[str]:
    """After the table: what to run or change for each upgrade (--fix's `how`, once each), and the notes --fix adds."""
    hows: list[str] = []
    gap_wheels = False
    for pkg in data["worse"] + data["new"]:
        for side in pkg["targets"].values():
            fix = side.get("fix")
            if not isinstance(fix, dict):
                continue
            how = fix.get("how")
            if fix.get("kind") == "upgrade" and isinstance(how, str) and how not in hows:
                hows.append(how)
            gap_wheels = gap_wheels or fix.get("kind") == "gap_wheel" or isinstance(fix.get("gap_wheel"), dict)
    items = [f"<li>{code(how, MAX_MESSAGE)}</li>" for how in hows]
    if hows:
        items.append(f"<li>{esc(RESOLVE_NOTE)}</li>")
    if gap_wheels:
        items.append(f"<li>{GAP_WHEEL_NOTE}</li>")
    return ["", "<p>How to fix:</p>", "<ul>", *items, "</ul>"] if items else []


def rows(data: dict, *, suggestions: bool = False, fixes: bool = False) -> list[str]:
    """One table row per package and target that got worse, then the new packages' gaps; with `fixes`, each row
    ends with its Fix cell."""
    out = []
    span = 4 if fixes else 3

    def fixed(side: dict) -> str:
        return f"<td>{fix_cell(side.get('fix'))}</td>" if fixes else ""

    for pkg in data["worse"]:
        old, new = versions(pkg.get("old_versions", [])), versions(pkg.get("new_versions", []))
        version = new if old == new else f"{old} → {new}"
        for target, side in pkg["targets"].items():
            out.append(
                f"<tr><td>{name_cell(pkg, target)}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>{cell(side, 'old_')} → {cell(side, 'new_')}</td>{fixed(side)}</tr>"
            )
        lines = pkg.get("suggestions") if suggestions else None
        if isinstance(lines, list):
            hint = "; ".join(line for line in lines if isinstance(line, str))
            if hint:
                out.append(f'<tr><td></td><td colspan="{span}">{code(hint, MAX_MESSAGE)}</td></tr>')
    for pkg in data["new"]:
        version = f"(new) {versions(pkg.get('versions', []))}"
        for target, side in pkg["targets"].items():
            out.append(
                f"<tr><td>{name_cell(pkg, target)}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>(new) → {cell(side)}</td>{fixed(side)}</tr>"
            )
    return out


def counts(n_worse: int, n_new: int, compared: bool = True, partial: bool = False) -> str:
    """As `wheelgap diff` words its last line: `compared` False when every target was left out, `partial` when some
    were."""
    if not compared:
        return "Nothing was compared."
    if not n_worse and not n_new:
        return "Nothing got worse on the compared targets." if partial else "Nothing got worse."
    parts = []
    if n_worse:
        parts.append(f"{n_worse} package{'s' * (n_worse != 1)} got worse")
    if n_new:
        parts.append(f"{n_new} new package{'s' * (n_new != 1)} {'has' if n_new == 1 else 'have'} gaps")
    return ", ".join(parts) + "."


def _valid(data: object) -> bool:
    """A `wheelgap diff --json` report: `worse` and `new` lists of packages with per-target dicts."""
    if not isinstance(data, dict) or not isinstance(data.get("worse"), list) or not isinstance(data.get("new"), list):
        return False
    for pkg in data["worse"] + data["new"]:
        if not isinstance(pkg, dict) or not isinstance(pkg.get("targets"), dict) or not pkg["targets"]:
            return False
        if not all(isinstance(side, dict) for side in pkg["targets"].values()):
            return False
        if not all(isinstance(pkg.get(k, []), list) for k in ("versions", "old_versions", "new_versions")):
            return False
    if not isinstance(data.get("unchecked", []), list) or not isinstance(data.get("not_compared", []), list):
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("target"), str)
        and isinstance(entry.get("reason"), str)
        and isinstance(entry.get("python", ""), str)
        and isinstance(entry.get("sides", []), list)
        for entry in data.get("not_compared", [])
    )


def dropped(entry: dict) -> bool:
    """A target only the new lockfile leaves out: the change dropped its Python."""
    return entry.get("sides") == ["new"]


def dropped_pythons(data: dict) -> list[str]:
    """The Pythons the change dropped, once each, from the `python` of each dropped target."""
    return list(
        dict.fromkeys(str(entry.get("python", "?")) for entry in data.get("not_compared", []) if dropped(entry))
    )


def compared(data: dict) -> bool:
    """False only when the report says it left out every target it was given (`not_compared`)."""
    targets = data.get("targets")
    labels = [t.get("label") for t in targets if isinstance(t, dict)] if isinstance(targets, list) else []
    left_out = [entry.get("target") for entry in data.get("not_compared", [])]
    return not labels or any(label not in left_out for label in labels)


def not_compared_lines(data: dict) -> list[str]:
    """The targets `wheelgap diff` left out, grouped by its reason: a note, not a failure. The Pythons the change
    dropped come first, each said as the change's doing."""

    def names(targets: list[str]) -> str:
        shown = ", ".join(code(t) for t in targets[:MAX_UNCHECKED])
        return shown + (f" and {len(targets) - MAX_UNCHECKED} more" if len(targets) > MAX_UNCHECKED else "")

    drops: dict[tuple[str, str], list[str]] = {}
    other: dict[str, list[str]] = {}
    for entry in data.get("not_compared", []):
        if dropped(entry):
            drops.setdefault((str(entry.get("python", "?")), entry["reason"]), []).append(entry["target"])
        else:
            other.setdefault(entry["reason"], []).append(entry["target"])
    out = [
        f"<p><b>This change drops Python {esc(python)} support.</b> {code(reason, MAX_MESSAGE)}, so installing the "
        f"new lock there now fails. Not compared: {names(targets)}; the drop doesn't count as worse.</p>"
        for (python, reason), targets in drops.items()
    ]
    for reason, targets in other.items():
        out.append(
            f"<p>Not compared: {names(targets)}. {code(reason, MAX_MESSAGE)}: a lock isn't made for a Python outside "
            "its requires-python, so nothing there counts as worse.</p>"
        )
    return out


def load(path: str, exit_code: int) -> tuple[dict | None, str]:
    """The diff's report when it and the exit code agree, else None and why not (escaped)."""
    diff = "<code>wheelgap diff</code>"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, f"{diff} exited {int(exit_code)} without a report; the log says why."
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return None, f"{diff} couldn't compare them: {code(data['error'].get('message', '?'), MAX_MESSAGE)}"
    if not _valid(data):
        return None, f"{diff} exited {int(exit_code)} and printed something other than its report."
    listed = bool(data["worse"] or data["new"])
    if exit_code not in (0, 1) or listed != (exit_code == 1):
        return None, f"{diff} exited {int(exit_code)}, which doesn't match its report, so neither is trusted."
    return data, ""


def report(
    data: dict | None,
    problem: str,
    lockfile: str,
    base: str,
    base_branch: bool = False,
    head: str = "",
    budget: int = MAX_COMMENT,
    *,
    suggestions: bool = False,
    comment: bool = False,
) -> str:
    """The Markdown report, at most `budget` characters. `comment`: the pull request comment's wording, which only
    replaces an earlier comment when nothing got worse, so it says the gaps are resolved."""
    at = f" at {code(head)}" if head else ""
    if base_branch:
        with_ = f"the base branch ({code(base)})" if base else "the base branch"
    else:
        with_ = code(base)
    compared_with = f"Compared {code(lockfile)}{at} with {with_}."
    header = ["### wheelgap diff", ""]
    if data is None:
        return "\n".join([*header, f"<p>{compared_with} The check couldn't run: {problem}</p>", ""])
    listed = bool(data["worse"] or data["new"])
    resolved = "<b>Resolved.</b> " if comment and not listed else ""
    said = counts(len(data["worse"]), len(data["new"]), compared(data), bool(data.get("not_compared")))
    header.append(f"<p>{resolved}{compared_with} {said}</p>")
    if skipped := not_compared_lines(data):
        header += ["", *skipped]
    footer = [""]
    unchecked = data.get("unchecked", [])
    if unchecked:
        names = ", ".join(code(n) for n in unchecked[:MAX_UNCHECKED])
        if len(unchecked) > MAX_UNCHECKED:
            names += f" and {len(unchecked) - MAX_UNCHECKED} more"
        footer = ["", f"<p>Couldn't check at the same version on either side: {names}</p>", ""]
    fixes = has_fixes(data)
    table = rows(data, fixes=fixes)
    if not table:
        return "\n".join(header + footer)
    fix_th = "<th>Fix</th>" if fixes else ""
    head_rows = ["", "<table>", f"<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th>{fix_th}</tr>"]
    more_row = f'<tr><td colspan="{5 if fixes else 4}">and {{}} more rows</td></tr>'
    # Room for the joins, the closing tag and the "more rows" line, whose count has at most 7 digits.
    size = sum(len(line) + 1 for line in header + head_rows + footer) + len("</table>") + len(more_row) + 10
    shown = []
    for row in table:
        if size + len(row) + 1 > budget:
            break
        shown.append(row)
        size += len(row) + 1
    after: list[str] = []
    if len(shown) < len(table):
        shown.append(more_row.format(len(table) - len(shown)))
    else:
        # Every comparison row fit: then how to fix them, then the optional release hints, each only if it fits.
        how = how_lines(data) if fixes else []
        if size + sum(len(line) + 1 for line in how) <= budget:
            after = how
            size += sum(len(line) + 1 for line in how)
        if suggestions:
            with_hints = rows(data, suggestions=True, fixes=fixes)
            extra = sum(len(row) + 1 for row in with_hints) - sum(len(row) + 1 for row in table)
            if size + extra <= budget:
                shown = with_hints
    return "\n".join(header + head_rows + shown + ["</table>"] + after + footer)


def marker(lockfile: str) -> str:
    """The hidden marker that finds this lockfile's comment again: the path percent-encoded, `-` included, so
    it can't close the HTML comment."""
    return MARKER.format(quote(lockfile, safe="/._").replace("-", "%2D"))


def comments(text: str) -> list:
    """The comments in `gh api --paginate` output: one JSON array per page, one after the other."""
    found: list = []
    decoder = json.JSONDecoder()
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i == len(text):
            return found
        page, i = decoder.raw_decode(text, i)
        if not isinstance(page, list):
            raise ValueError("not a list of comments")
        found += page


def pick(listing: list, login: str, lockfile: str) -> int | None:
    """The id of the first comment by `login` whose body starts with this lockfile's marker."""
    start = marker(lockfile)
    for c in listing:
        if not isinstance(c, dict) or not isinstance(c.get("user"), dict) or not isinstance(c.get("id"), int):
            continue
        author = c["user"].get("login")
        body = c.get("body")
        if isinstance(author, str) and author.lower() == login.lower() and isinstance(body, str):
            if body.startswith(start):
                return c["id"]
    return None


_LEADING = re.compile(r"^(?:\s|\ufeff|\x1b\[[0-9;]*m)*")


def defuse(line: str) -> str:
    """`line` with nothing the Actions runner reads as a workflow command: it acts on a line whose first
    non-whitespace text is `::` and on `##[` anywhere (actions/runner's ActionCommand.TryParseV2 and TryParse).
    Colour codes and a byte order mark are skipped too, to be safe."""
    line = line.replace("##[", "#\\#[")
    start = _LEADING.match(line).end()  # type: ignore[union-attr]
    if line.startswith("::", start):
        line = line[: start + 1] + "\\" + line[start + 1 :]
    return line


def safe_log(text: str) -> str:
    # The runner splits lines at LF, CR and CRLF.
    return "\n".join(defuse(line) for line in re.split(r"\r\n|\r|\n", text))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("report", help="write the report; exit 0, 1 or 2 as the step should")
    r.add_argument("--diff-json", help="what `wheelgap diff --json` printed")
    r.add_argument("--exit-code", type=int, default=2, help="wheelgap diff's exit code")
    r.add_argument("--problem", help="diff didn't run: report this reason instead")
    r.add_argument("--lockfile", required=True)
    r.add_argument("--base", default="", help="the git ref or commit the lockfile was compared with")
    r.add_argument("--base-branch", action="store_true", help="--base is the pull request's base commit")
    r.add_argument("--head", default="", help="the commit checked, shown in the report")
    r.add_argument("--markdown", required=True, help="write the report here")
    r.add_argument("--payload", help="also write the comment API's JSON body here")
    r.add_argument("--comment-markdown", help="also write the comment's body here, as Markdown")
    k = sub.add_parser("pick", help="print the id of this lockfile's comment, if there is one")
    k.add_argument("--comments", required=True, help="what `gh api --paginate` printed")
    k.add_argument("--login", required=True, help="the token's identity: only its comments are edited")
    k.add_argument("--lockfile", required=True)
    g = sub.add_parser("log", help="print a file with workflow commands defused")
    g.add_argument("path")
    args = p.parse_args(argv)
    try:
        return run(args)
    except Exception as e:  # never a traceback in the log: it could quote the pull request's text
        print(f"wheelgap: the compare-to helper failed ({type(e).__name__})", file=sys.stderr)
        return 3


def run(args: argparse.Namespace) -> int:
    if args.command == "log":
        # UTF-8 whatever the runner's code page (cp1252 on Windows), so no message is lost to an encoding error.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        with open(args.path, encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        if text:
            print(safe_log(text.removesuffix("\n").removesuffix("\r")))
        return 0
    if args.command == "pick":
        with open(args.comments, encoding="utf-8") as f:
            found = pick(comments(f.read()), args.login, args.lockfile)
        if found is not None:
            print(found)
        return 0

    head = args.head if SHA.match(args.head) else ""
    if args.problem is not None or args.diff_json is None:
        data, problem = None, esc(args.problem or "no report", MAX_MESSAGE)
    else:
        data, problem = load(args.diff_json, args.exit_code)
    budget = MAX_COMMENT - len(marker(args.lockfile)) - 1
    text = report(data, problem, args.lockfile, args.base, args.base_branch, head[:7], budget)
    with open(args.markdown, "w", encoding="utf-8") as f:
        f.write(text)
    if args.payload or args.comment_markdown:
        body = report(
            data, problem, args.lockfile, args.base, args.base_branch, head[:7], budget, suggestions=True, comment=True
        )
        body = f"{marker(args.lockfile)}\n{body}"
        if args.payload:
            with open(args.payload, "w", encoding="utf-8") as f:
                json.dump({"body": body}, f)
        if args.comment_markdown:
            with open(args.comment_markdown, "w", encoding="utf-8") as f:
                f.write(body)
    # Counts only: names and versions in the log could be read as workflow commands.
    if data is None:
        print("wheelgap: couldn't compare the lockfiles; the job summary and the log above say why.")
        return 2
    left_out = len(data.get("not_compared", []))
    skipped = f" {left_out} target{'s' * (left_out != 1)} not compared." if left_out else ""
    said = counts(len(data["worse"]), len(data["new"]), compared(data), left_out > 0)
    print(f"wheelgap: {said}{skipped} The job summary has the details.")
    for python in dropped_pythons(data):
        # A workflow command on purpose, so it can't be missed: fixed text and a version checked to be one, never
        # the reason (it quotes the pull request's lockfile).
        what = f"Python {python}" if PYTHON.fullmatch(python) else "a target Python"
        print(
            f"::warning::wheelgap: This change drops {what} support: the new lockfile's requires-python leaves it "
            "out. The job summary has the details."
        )
    return 1 if data["worse"] or data["new"] else 0


if __name__ == "__main__":
    sys.exit(main())
