"""File the weekly bug hunt's issues and write its job summary (.github/workflows/bug-hunt.yml, #777).

    uv run --no-dev python scripts/bug_hunt_file.py RESULTS_DIR [--file]

RESULTS_DIR holds status.tsv, one `OWNER/NAME<TAB>exit code` line per repo the workflow checked, and the
OWNER__NAME.json file that `scripts/check_projects.py repo --issues-json` wrote for each. With --file, each
disagreement becomes an issue in $GITHUB_REPOSITORY, labelled `bug` and `bug-hunt`, unless an issue with the same
marker (repo, input, target and package name, no version) already exists, open or closed. The summary goes to
$GITHUB_STEP_SUMMARY (stdout without it).

It runs every week on its own, so it fails closed and stays quiet:
- the `bug-hunt` label is created (or updated) first, and if that fails nothing is filed;
- if the existing issues can't be listed, nothing is filed: a search error never means "no duplicates";
- at most MAX_ISSUES are filed per run, and the rest are listed in the summary;
- a repo that couldn't be checked (an exit code other than 0 or 1, or no JSON file) is an "error" row.
Each of those, and any issue that couldn't be filed, makes the exit code 1, so the run shows as failed.

Titles and bodies go to `gh` as arguments and a file, never through a shell. RESULTS_DIR comes from the job that
read the scanned repositories, so nothing in it is trusted: a repo must be in scripts/bug_hunt_repos.txt, each
disagreement's input and target must be ones check_projects.py writes, and the title, body and marker are built
here again from those fields and the plain-text report (the body only ever shows the report in a ```text fence);
an item whose marker isn't the one built here is refused. The summary shows that text in code spans, and stdout and
stderr go through wheelgap.logsafe. Dedup reads an existing issue's marker only from its body's last line, where
this script puts it, never from the report above it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wheelgap.logsafe import guarded_output

MAX_ISSUES = 5
LABEL = "bug-hunt"
LABEL_COLOR = "5319e7"
LABEL_DESCRIPTION = "Found by the weekly bug-hunt workflow"
# More bug-hunt issues than this and the listing may be cut short, so dedup can't be sure: refuse to file.
LIST_LIMIT = 1000
MARKER = re.compile(r"<!-- bug-hunt [^\n<>]* -->")
ISSUE_URL = re.compile(r"https://github\.com/[\w./-]+")
REPOS_FILE = Path(__file__).with_name("bug_hunt_repos.txt")
INPUTS = frozenset({"uv.lock", "poetry.lock", "requirements.txt"})
TARGET = re.compile(r"[0-9]+\.[0-9]+t? [a-z0-9_.-]+")
MAX_REPORT = 20_000
# As check_projects.plain: text from a scanned repo in a title or marker can't end the HTML comment, mention someone
# or add a line.
_PLAIN = re.compile(r"[^A-Za-z0-9 ._:/=+~!(),-]")


def plain(text: str) -> str:
    return _PLAIN.sub("_", text)


def marker_for(repo: str, input_name: str, target: str, package: str) -> str:
    """As check_projects.bug_hunt_marker: repo, input, target and the package's name (and index), no version."""
    base, _, source = package.partition(" @ ")
    name = base.partition("==")[0]
    what = f"{plain(name)} @ {plain(source)}" if source else plain(name)
    return f"<!-- bug-hunt {plain(repo)} {plain(input_name)} {plain(target)} {what} -->"


def text_fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def issue_body(report: str, marker: str) -> str:
    """As check_projects.build_issue_payload's body: the report only ever in a text fence, the marker last."""
    return (
        "Found by the weekly bug hunt (`.github/workflows/bug-hunt.yml`): wheelgap and a referee disagree on this "
        "package. The report below is `scripts/check_projects.py`'s output. Its package names, versions and "
        "index URLs come from the scanned repository: check the cell with the two commands before acting on it.\n\n"
        f"{text_fence(report)}\n\n{marker}\n"
    )


def read_repos(path: Path) -> set[str]:
    """The repos the bug hunt scans: one OWNER/NAME per line, # comments."""
    lines = (line.partition("#")[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line}


# gh's arguments (without "gh") -> (exit code, stdout, stderr)
Gh = Callable[[list[str]], tuple[int, str, str]]


def run_gh(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class RepoResult:
    repo: str
    code: int | None  # check_projects.py's exit code; None if the workflow recorded none
    data: dict[str, Any] | None  # its --issues-json file, if it wrote a readable one

    @property
    def status(self) -> str:
        if self.data is None or self.code not in (0, 1):
            return "error"
        return "disagree" if self.code == 1 else "ok"


@dataclass
class Outcome:
    filed: list[tuple[dict, str]] = field(default_factory=list)  # (item, issue URL)
    duplicates: list[dict] = field(default_factory=list)
    over_cap: list[dict] = field(default_factory=list)
    not_filed: list[dict] = field(default_factory=list)  # filing stopped before these
    unfiled: list[dict] = field(default_factory=list)  # found while filing is off
    problems: list[str] = field(default_factory=list)


def load(results: Path) -> list[RepoResult]:
    """One result per line of status.tsv, in its order (an unreadable status.tsv gives none: see main)."""
    out: list[RepoResult] = []
    for line in (results / "status.tsv").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        repo, _, code_text = line.partition("\t")
        try:
            code: int | None = int(code_text.strip())
        except ValueError:
            code = None
        data = None
        try:
            loaded = json.loads((results / f"{repo.replace('/', '__')}.json").read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            pass
        out.append(RepoResult(repo, code, data))
    return out


def rebuild(item: Any, repo: str) -> dict | None:
    """The issue for one disagreement of `repo`, built here from its checked fields; None if any is off."""
    if not isinstance(item, dict):
        return None
    fields = [item.get(k) for k in ("repo", "input", "target", "package", "report", "marker")]
    if not all(isinstance(value, str) for value in fields):
        return None
    item_repo, input_name, target, package, report, marker = (str(value) for value in fields)
    name, _, rest = package.partition("==")
    version = rest.partition(" @ ")[0]
    if (
        item_repo != repo
        or input_name not in INPUTS
        or not TARGET.fullmatch(target)
        or not name
        or not version
        or len(package) > 500
        or len(report) > MAX_REPORT
    ):
        return None
    built = marker_for(repo, input_name, target, package)
    if marker != built or not MARKER.fullmatch(built):
        return None
    return {
        "repo": repo,
        "input": input_name,
        "target": target,
        "package": package,
        "marker": built,
        "title": plain(f"bug-hunt: {name}=={version} on {target} ({repo})")[:200],
        "body": issue_body(report, built),
    }


def pending_items(results: Sequence[RepoResult], repos: set[str], problems: list[str]) -> list[dict]:
    """Every disagreement to file, once per marker, in the order of the repos."""
    seen: set[str] = set()
    items: list[dict] = []
    for result in results:
        if result.data is None:
            continue
        if result.repo not in repos:
            problems.append(f"{code(result.repo)} isn't in {REPOS_FILE.name}: its results weren't used")
            continue
        found = result.data.get("disagreements", [])
        found_items: list[Any] = found if isinstance(found, list) else [None]  # not a list: malformed
        for raw in found_items:
            item = rebuild(raw, result.repo)
            if item is None:
                problems.append(f"{code(result.repo)}: a malformed disagreement in its JSON file, not filed")
                continue
            if item["marker"] not in seen:
                seen.add(item["marker"])
                items.append(item)
    return items


def ensure_label(gh: Gh, repo: str, problems: list[str]) -> bool:
    code, _, err = gh(
        [
            "label",
            "create",
            LABEL,
            "--repo",
            repo,
            "--force",
            "--color",
            LABEL_COLOR,
            "--description",
            LABEL_DESCRIPTION,
        ]
    )
    if code != 0:
        print(f"gh label create failed ({code}):\n{err}", file=sys.stderr)
        problems.append(f"couldn't create the {LABEL} label, so nothing was filed")
        return False
    return True


def existing_markers(gh: Gh, repo: str, problems: list[str]) -> set[str] | None:
    """The markers of every bug-hunt issue, open or closed; None (and a problem) if they can't all be read."""
    code, out, err = gh(
        [
            *("issue", "list", "--repo", repo, "--state", "all", "--label", LABEL),
            *("--limit", str(LIST_LIMIT), "--json", "number,body"),
        ]
    )
    if code != 0:
        print(f"gh issue list failed ({code}):\n{err}", file=sys.stderr)
        problems.append("couldn't list the existing bug-hunt issues, so nothing was filed")
        return None
    try:
        issues = json.loads(out)
        if not isinstance(issues, list):
            raise ValueError("not a list")
        bodies = [str(issue.get("body") or "") for issue in issues]
    except (ValueError, AttributeError) as exc:
        problems.append(f"couldn't read gh issue list's output ({exc}), so nothing was filed")
        return None
    if len(issues) >= LIST_LIMIT:
        problems.append(f"{len(issues)} bug-hunt issues: the list may be cut short, so nothing was filed")
        return None
    # Only the last line, which this script writes: the fenced report above it is the scanned repo's text, and a
    # marker in it mustn't mark someone else's bug as filed.
    last = [next((line.strip() for line in reversed(body.splitlines()) if line.strip()), "") for body in bodies]
    return {line for line in last if MARKER.fullmatch(line)}


def file_issue(gh: Gh, repo: str, item: dict) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="bug-hunt-") as tmp:
        body = Path(tmp) / "body.md"
        body.write_text(item["body"], encoding="utf-8")
        code, out, err = gh(
            [
                *("issue", "create", "--repo", repo, "--title", item["title"], "--body-file", str(body)),
                *("--label", "bug", "--label", LABEL),
            ]
        )
    if code != 0:
        print(f"gh issue create failed ({code}):\n{err}", file=sys.stderr)
        return False, err.strip()
    return True, out.strip()


def file_all(gh: Gh, repo: str, items: list[dict], cap: int, problems: list[str]) -> Outcome:
    outcome = Outcome(problems=problems)
    if not items:
        return outcome
    if not ensure_label(gh, repo, problems):
        outcome.not_filed = items
        return outcome
    markers = existing_markers(gh, repo, problems)
    if markers is None:
        outcome.not_filed = items
        return outcome
    new: list[dict] = []
    for item in items:
        (outcome.duplicates if item["marker"] in markers else new).append(item)
    to_file, outcome.over_cap = new[:cap], new[cap:]
    for number, item in enumerate(to_file):
        ok, text = file_issue(gh, repo, item)
        if not ok:
            problems.append("gh issue create failed, so filing stopped")
            outcome.not_filed = to_file[number:]
            break
        outcome.filed.append((item, text))
    if outcome.over_cap:
        problems.append(f"{len(new)} new disagreements, more than the cap of {cap}: the rest weren't filed")
    return outcome


def code(text: Any) -> str:
    """Text from a scanned repo as a Markdown code span that can't end early, break a table row or add a line."""
    clean = re.sub(r"[`|\r\n]", "_", str(text))
    return f"`{clean}`" if clean else ""


def item_line(item: dict) -> str:
    return f"- {code(item.get('package'))} on {code(item.get('target'))} in {code(item.get('repo'))}"


def summary(results: Sequence[RepoResult], outcome: Outcome, filing: bool, cap: int) -> str:
    lines = [
        "### Weekly bug hunt",
        "",
        f"Filing issues: {'on' if filing else 'off'} (at most {cap} per run)",
        "",
        "| Repository | Status | Exit code | Compared | Agreed | Disagreed | Not compared |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        s = (r.data or {}).get("summary")
        s = s if isinstance(s, dict) else {}
        counts = [
            str(s[k]) if isinstance(s.get(k), int) else "" for k in ("compared", "agree", "disagree", "not_compared")
        ]
        exit_code = "none" if r.code is None else str(r.code)
        lines.append(f"| {code(r.repo)} | {r.status} | {exit_code} | " + " | ".join(counts) + " |")
    lines.append("")

    def section(title: str, entries: list[str]) -> None:
        if entries:
            lines.extend([f"#### {title}", "", *entries, ""])

    section(
        "Issues filed",
        [f"{item_line(item)}: {url if ISSUE_URL.fullmatch(url) else code(url)}" for item, url in outcome.filed],
    )
    section("Already filed (open or closed)", [item_line(item) for item in outcome.duplicates])
    section(f"Over the cap of {cap}, not filed", [item_line(item) for item in outcome.over_cap])
    section("Not filed (see the problems below)", [item_line(item) for item in outcome.not_filed])
    section("Disagreements found (filing is off)", [item_line(item) for item in outcome.unfiled])
    section("Problems", [f"- {problem}" for problem in outcome.problems])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, gh: Gh = run_gh, env: Mapping[str, str] = os.environ) -> int:
    with guarded_output():
        return _main(argv, gh, env)


def _main(argv: list[str] | None, gh: Gh, env: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(description="File the bug hunt's issues and write its job summary.")
    parser.add_argument("results", type=Path, help="the folder with status.tsv and the --issues-json files")
    parser.add_argument("--file", action="store_true", help="file issues (default: only write the summary)")
    parser.add_argument("--max-issues", type=int, default=MAX_ISSUES, help=f"per run (default {MAX_ISSUES})")
    parser.add_argument("--repos", type=Path, default=REPOS_FILE, help="the repos the bug hunt scans")
    args = parser.parse_args(argv)
    problems: list[str] = []
    try:
        repos = read_repos(args.repos)
    except OSError as exc:
        repos = set()
        problems.append(f"no list of repos: {exc.strerror}")
    try:
        results = load(args.results)
    except OSError as exc:
        results = []
        problems.append(f"no status.tsv: the check step recorded no repos ({exc.strerror})")
    errors = [r.repo for r in results if r.status == "error"]
    if errors:
        problems.append(f"{len(errors)} repos couldn't be checked: " + ", ".join(code(repo) for repo in errors))
    items = pending_items(results, repos, problems)
    repo = env.get("GITHUB_REPOSITORY", "")
    if args.file and items and not repo:
        problems.append("GITHUB_REPOSITORY isn't set, so nothing was filed")
        outcome = Outcome(not_filed=items, problems=problems)
    elif args.file:
        outcome = file_all(gh, repo, items, args.max_issues, problems)
    else:
        outcome = Outcome(unfiled=items, problems=problems)
    text = summary(results, outcome, args.file, args.max_issues)
    if path := env.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    for problem in problems:
        print(f"bug-hunt: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
