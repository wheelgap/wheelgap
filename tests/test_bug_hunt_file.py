"""scripts/bug_hunt_file.py with a fake `gh`: dedup (open and closed issues, exact markers), failing closed when the
label or the listing fails, the per-run cap, "error" rows for repos that couldn't be checked, and an untrusted
results folder (items are rebuilt from checked fields, markers read only from an issue's last line)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _script():
    path = Path(__file__).parents[1] / "scripts" / "bug_hunt_file.py"
    spec = importlib.util.spec_from_file_location("wheelgap_bug_hunt_file", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()


REPOS = ("o/a", "o/b", "o/ok", "o/crash")


def item(repo: str, package: str, target: str = "3.13 linux-x86_64") -> dict:
    """A disagreement as check_projects.py writes it (its title and body are ignored: the filer builds its own)."""
    marker = f"<!-- bug-hunt {repo} uv.lock {target} {package} @ https://pypi.org/simple -->"
    return {
        "marker": marker,
        "title": "ignored",
        "body": "ignored",
        "report": f"report for {package}",
        "repo": repo,
        "input": "uv.lock",
        "target": target,
        "package": f"{package}==1.0 @ https://pypi.org/simple",
    }


def title(it: dict) -> str:
    return f"bug-hunt: {it['package'].split(' @ ')[0]} on {it['target']} ({it['repo']})"


def results(tmp_path: Path, repos: dict[str, tuple[int, list[dict] | None]]) -> Path:
    """status.tsv plus one JSON file per repo (None: the check wrote none)."""
    folder = tmp_path / "results"
    folder.mkdir()
    lines = []
    for repo, (code, items) in repos.items():
        lines.append(f"{repo}\t{code}")
        if items is not None:
            summary = {"compared": 10, "agree": 10 - len(items), "disagree": len(items), "not_compared": 0}
            data = {"repo": repo, "commit": "c" * 40, "summary": summary, "disagreements": items}
            (folder / f"{repo.replace('/', '__')}.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "status.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


class FakeGh:
    """Answers `gh label create`, `gh issue list` (the given issues) and `gh issue create` (a new URL each)."""

    def __init__(self, issues=(), label=(0, ""), listing=None, create_fails_at=None):
        self.issues = list(issues)
        self.label = label
        self.listing = listing  # (code, stdout, stderr) instead of the issues
        self.create_fails_at = create_fails_at
        self.calls: list[list[str]] = []
        self.created: list[tuple[str, str, list[str]]] = []  # (title, body, labels)

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["label", "create"]:
            return self.label[0], "", self.label[1]
        if args[:2] == ["issue", "list"]:
            if self.listing is not None:
                return self.listing
            return 0, json.dumps([{"number": n, "body": b} for n, b in enumerate(self.issues, 1)]), ""
        assert args[:2] == ["issue", "create"]
        if self.create_fails_at is not None and len(self.created) == self.create_fails_at:
            return 1, "", "HTTP 502"
        title = args[args.index("--title") + 1]
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        self.created.append((title, body, labels))
        return 0, f"https://github.com/o/self/issues/{100 + len(self.created)}\n", ""

    def kinds(self):
        return [" ".join(args[:2]) for args in self.calls]


def run(folder: Path, gh: FakeGh, tmp_path: Path, *flags: str) -> tuple[int, str]:
    summary = tmp_path / "summary.md"
    env = {"GITHUB_REPOSITORY": "o/self", "GITHUB_STEP_SUMMARY": str(summary)}
    repos = tmp_path / "repos.txt"
    repos.write_text("# the list\n" + "\n".join(REPOS) + "\n", encoding="utf-8")
    code = script.main([str(folder), "--repos", str(repos), *flags], gh=gh, env=env)
    return code, summary.read_text(encoding="utf-8")


def test_files_new_disagreements_and_skips_open_and_closed_duplicates(tmp_path):
    a, b, c = item("o/a", "x"), item("o/a", "y"), item("o/b", "z")
    folder = results(tmp_path, {"o/a": (1, [a, b]), "o/b": (1, [c, dict(c)]), "o/ok": (0, [])})
    # x has an issue (closed, as not planned: the listing is --state all); y's only "match" is a longer marker, and
    # z's marker is only inside another issue's report (scanned text), not on its last line.
    gh = FakeGh(
        issues=[
            f"old report\n{a['marker']}\r\n",
            b["marker"].replace(" -->", "/extra -->"),
            f"```text\n{c['marker']}\n```\n\n<!-- bug-hunt o/q uv.lock 3.13 linux-x86_64 q -->\n",
        ]
    )
    code, text = run(folder, gh, tmp_path, "--file")
    assert code == 0
    assert gh.kinds() == ["label create", "issue list", "issue create", "issue create"]
    label, listing = gh.calls[0], gh.calls[1]
    assert label[:3] == ["label", "create", "bug-hunt"] and "--force" in label
    assert listing[listing.index("--state") + 1] == "all" and listing[listing.index("--label") + 1] == "bug-hunt"
    assert [(t, labels) for t, _, labels in gh.created] == [
        (title(b), ["bug", "bug-hunt"]),
        (title(c), ["bug", "bug-hunt"]),  # c twice in the JSON: filed once
    ]
    body = gh.created[0][1]
    assert body == script.issue_body("report for y", b["marker"])
    assert "\n```text\nreport for y\n```\n\n" in body and body.endswith(f"\n{b['marker']}\n")
    assert "#### Issues filed" in text and "https://github.com/o/self/issues/101" in text
    assert "#### Already filed (open or closed)\n\n- `x==1.0 @ https://pypi.org/simple`" in text
    assert "| `o/ok` | ok | 0 | 10 | 10 | 0 | 0 |" in text


@pytest.mark.parametrize(
    ("gh", "calls"),
    [
        (FakeGh(label=(1, "HTTP 403: Resource not accessible by integration")), ["label create"]),
        (FakeGh(listing=(1, "", "API rate limit exceeded")), ["label create", "issue list"]),
        (FakeGh(listing=(0, "not json", "")), ["label create", "issue list"]),
        (FakeGh(listing=(0, json.dumps([{"number": 1, "body": ""}] * 1000), "")), ["label create", "issue list"]),
    ],
)
def test_fails_closed(tmp_path, capsys, gh, calls):
    folder = results(tmp_path, {"o/a": (1, [item("o/a", "x")])})
    code, text = run(folder, gh, tmp_path, "--file")
    assert code == 1
    assert gh.kinds() == calls and gh.created == []  # nothing filed
    assert "#### Not filed (see the problems below)\n\n- `x==1.0" in text
    err = capsys.readouterr().err
    assert "nothing was filed" in err
    if gh.label[0]:
        assert "Resource not accessible by integration" in err  # gh's own error is shown


def test_caps_the_issues_per_run(tmp_path):
    items = [item("o/a", f"p{n}") for n in range(7)]
    folder = results(tmp_path, {"o/a": (1, items)})
    gh = FakeGh()
    code, text = run(folder, gh, tmp_path, "--file")
    assert code == 1  # so the run shows as failed and someone looks
    assert [t for t, _, _ in gh.created] == [title(i) for i in items[: script.MAX_ISSUES]] and script.MAX_ISSUES == 5
    assert "#### Over the cap of 5, not filed\n\n- `p5==1.0 @ https://pypi.org/simple`" in text
    assert "- `p6==1.0" in text and "more than the cap of 5" in text


def test_stops_filing_when_a_create_fails(tmp_path):
    items = [item("o/a", f"p{n}") for n in range(3)]
    gh = FakeGh(create_fails_at=1)
    code, text = run(results(tmp_path, {"o/a": (1, items)}), gh, tmp_path, "--file")
    assert code == 1 and len(gh.created) == 1
    assert gh.kinds().count("issue create") == 2
    assert "#### Not filed (see the problems below)\n\n- `p1==1.0" in text and "- `p2==1.0" in text


def test_failed_repos_are_error_rows(tmp_path):
    folder = results(tmp_path, {"o/a": (2, []), "o/crash": (1, None), "o/b": (1, [item("o/b", "x")])})
    gh = FakeGh()
    code, text = run(folder, gh, tmp_path, "--file")
    assert code == 1
    assert "| `o/a` | error | 2 | 10 | 10 | 0 | 0 |" in text
    assert "| `o/crash` | error | 1 |  |  |  |  |" in text
    assert "2 repos couldn't be checked: `o/a`, `o/crash`" in text
    assert len(gh.created) == 1  # the repos that were checked still get their issues


def test_no_status_file_is_an_error(tmp_path):
    (tmp_path / "results").mkdir()
    code, text = run(tmp_path / "results", FakeGh(), tmp_path, "--file")
    assert code == 1 and "no status.tsv" in text


def test_without_file_nothing_calls_gh(tmp_path):
    gh = FakeGh()
    code, text = run(results(tmp_path, {"o/a": (1, [item("o/a", "x")])}), gh, tmp_path)
    assert code == 0 and gh.calls == []
    assert "Filing issues: off" in text and "#### Disagreements found (filing is off)\n\n- `x==1.0" in text
    assert "| `o/a` | disagree | 1 | 10 | 9 | 1 | 0 |" in text


@pytest.mark.parametrize(
    "change",
    [
        {"marker": "<!-- bug-hunt o/a --> <img src=x> -->"},
        {"marker": "not a marker"},
        {"marker": "<!-- bug-hunt o/a uv.lock 3.13 linux-x86_64 other @ https://pypi.org/simple -->"},
        {"repo": "o/b"},  # not the repo whose file it's in
        {"input": "setup.py"},
        {"target": "3.13 linux-x86_64\n@someone"},
        {"package": "no-version"},
        {"report": 5},
        {"report": "x" * 20_001},
    ],
)
def test_malformed_items_are_not_filed(tmp_path, change):
    bad = {**item("o/a", "x"), **change}
    gh = FakeGh()
    code, _ = run(results(tmp_path, {"o/a": (1, [bad])}), gh, tmp_path, "--file")
    assert code == 1 and gh.created == []


def test_scanned_text_stays_in_code_spans(tmp_path, capsys):
    bad = item("o/a", "x")
    bad["package"] = "x==1 @ https://e.test/|`[a](b)\n::error::hi"
    bad["marker"] = script.marker_for("o/a", "uv.lock", bad["target"], bad["package"])
    assert bad["marker"] == "<!-- bug-hunt o/a uv.lock 3.13 linux-x86_64 x @ https://e.test/___a_(b)_::error::hi -->"
    code, text = run(results(tmp_path, {"o/a": (1, [bad])}), FakeGh(), tmp_path)
    assert code == 0
    assert "- `x==1 @ https://e.test/__[a](b)_::error::hi` on" in text


def test_results_for_a_repo_not_in_the_list_are_not_used(tmp_path):
    gh = FakeGh()
    code, text = run(results(tmp_path, {"o/evil": (1, [item("o/evil", "x")])}), gh, tmp_path, "--file")
    assert code == 1 and gh.calls == [] and "`o/evil` isn't in bug_hunt_repos.txt" in text
