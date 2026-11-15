"""The GitHub Action's compare-to helper (#201): the job summary, the PR comment, and safe log lines.

    python3 scripts/action_compare.py report --diff-json diff.json --exit-code 1 --lockfile uv.lock \\
        --base 1a2b3c4 --base-branch --head 9f8e7d6 --markdown report.md --payload comment.json
    python3 scripts/action_compare.py pick --comments comments.json --login 'github-actions[bot]' --lockfile uv.lock
    python3 scripts/action_compare.py log diff.err

The Action runs this with the runner's own Python, not wheelgap's, so it uses only the standard library and
doesn't import wheelgap.

- `report` writes the Markdown report (for $GITHUB_STEP_SUMMARY) and, with --payload, the JSON body for GitHub's
  comment API: the same report under a hidden marker that names the lockfile. Its exit code is the step's result:
  0 only when `wheelgap diff` exited 0 with a valid report listing nothing, 1 when it exited 1 with a valid
  report listing something, and 2 for anything else (no report, an error, or an exit code that disagrees with the
  report), so the step never passes on a guess.
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
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


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


def rows(data: dict) -> list[str]:
    """One table row per package and target that got worse, then the new packages' gaps."""
    out = []
    for pkg in data["worse"]:
        old, new = versions(pkg.get("old_versions", [])), versions(pkg.get("new_versions", []))
        version = new if old == new else f"{old} → {new}"
        for target, side in pkg["targets"].items():
            out.append(
                f"<tr><td>{code(pkg.get('name'))}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>{cell(side, 'old_')} → {cell(side, 'new_')}</td></tr>"
            )
    for pkg in data["new"]:
        version = f"(new) {versions(pkg.get('versions', []))}"
        for target, side in pkg["targets"].items():
            out.append(
                f"<tr><td>{code(pkg.get('name'))}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>(new) → {cell(side)}</td></tr>"
            )
    return out


def counts(n_worse: int, n_new: int) -> str:
    """As `wheelgap diff` words its last line."""
    if not n_worse and not n_new:
        return "Nothing got worse."
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
    return isinstance(data.get("unchecked", []), list)


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
) -> str:
    """The Markdown report, at most `budget` characters."""
    at = f" at {code(head)}" if head else ""
    if base_branch:
        with_ = f"the base branch ({code(base)})" if base else "the base branch"
    else:
        with_ = code(base)
    compared = f"Compared {code(lockfile)}{at} with {with_}."
    header = ["### wheelgap diff", ""]
    if data is None:
        return "\n".join([*header, f"<p>{compared} The check couldn't run: {problem}</p>", ""])
    header.append(f"<p>{compared} {counts(len(data['worse']), len(data['new']))}</p>")
    footer = [""]
    unchecked = data.get("unchecked", [])
    if unchecked:
        names = ", ".join(code(n) for n in unchecked[:MAX_UNCHECKED])
        if len(unchecked) > MAX_UNCHECKED:
            names += f" and {len(unchecked) - MAX_UNCHECKED} more"
        footer = ["", f"<p>Couldn't check at the same version on either side: {names}</p>", ""]
    table = rows(data)
    if not table:
        return "\n".join(header + footer)
    head_rows = ["", "<table>", "<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th></tr>"]
    more_row = '<tr><td colspan="4">and {} more rows</td></tr>'
    # Room for the joins, the closing tag and the "more rows" line, whose count has at most 7 digits.
    size = sum(len(line) + 1 for line in header + head_rows + footer) + len("</table>") + len(more_row) + 10
    shown = []
    for row in table:
        if size + len(row) + 1 > budget:
            break
        shown.append(row)
        size += len(row) + 1
    if len(shown) < len(table):
        shown.append(more_row.format(len(table) - len(shown)))
    return "\n".join(header + head_rows + shown + ["</table>"] + footer)


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
    if args.payload:
        with open(args.payload, "w", encoding="utf-8") as f:
            json.dump({"body": f"{marker(args.lockfile)}\n{text}"}, f)
    # Counts only: names and versions in the log could be read as workflow commands.
    if data is None:
        print("wheelgap: couldn't compare the lockfiles; the job summary and the log above say why.")
        return 2
    print(f"wheelgap: {counts(len(data['worse']), len(data['new']))} The job summary has the details.")
    return 1 if data["worse"] or data["new"] else 0


if __name__ == "__main__":
    sys.exit(main())
