"""scripts/action_compare.py: the GitHub Action's compare-to report, PR comment and log lines (#201)."""

import html
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wheelgap.check import CELL
from wheelgap.diff import main as diff_main

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "actioncompare"
ARGS = ["-p", "3.13", "-t", "linux-x86_64,musl-aarch64"]


def _load():
    spec = importlib.util.spec_from_file_location("wheelgap_action_compare", ROOT / "scripts" / "action_compare.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load()
INERT = re.compile(r"^[A-Za-z0-9 \-&#;…]*$")


def fixture_diff(fake_pypi, capsys, old="base", new="head"):
    code = diff_main([str(FIXTURES / old / "uv.lock"), str(FIXTURES / new / "uv.lock"), *ARGS, "--json"])
    assert fake_pypi == []  # the Action's test workflow runs these fixtures; nothing is looked up
    return code, json.loads(capsys.readouterr().out)


def worse_entry(name="armlib", new_status="source", **side):
    return {
        "name": name,
        "old_versions": ["1.0"],
        "new_versions": ["2.0"],
        "targets": {"linux-x86_64": {"old_status": "wheel", "new_status": new_status, **side}},
    }


def new_entry(name="nativeext"):
    return {"name": name, "versions": ["0.3"], "targets": {"linux-x86_64": {"status": "source"}}}


def diff_report(worse=(), new=(), unchecked=()):
    return {"version": "0.9.0", "targets": [], "worse": list(worse), "new": list(new), "unchecked": list(unchecked)}


def test_cells_match_wheelgaps_text_output():
    assert ac.CELLS == {status.value: shown for status, (shown, _) in CELL.items()}


def test_fixtures_report_the_dropped_platform_and_the_new_gap(fake_pypi, capsys):
    code, data = fixture_diff(fake_pypi, capsys)
    assert code == 1
    text = ac.report(data, "", "uv.lock", "1a2b3c4", base_branch=True, head="9f8e7d6")
    assert text.splitlines() == [
        "### wheelgap diff",
        "",
        "<p>Compared <code>uv&#46;lock</code> at <code>9f8e7d6</code> with the base branch (<code>1a2b3c4</code>)."
        " 1 package got worse, 1 new package has gaps.</p>",
        "",
        "<table>",
        "<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th></tr>",
        "<tr><td><code>armlib</code></td><td><code>1&#46;4&#46;0</code> → <code>1&#46;5&#46;0</code></td>"
        "<td><code>musl-aarch64</code></td><td>✓ → ● source</td></tr>",
        "<tr><td><code>nativeext</code></td><td>(new) <code>0&#46;3</code></td>"
        "<td><code>linux-x86&#95;64</code></td><td>(new) → ● source</td></tr>",
        "<tr><td><code>nativeext</code></td><td>(new) <code>0&#46;3</code></td>"
        "<td><code>musl-aarch64</code></td><td>(new) → ● source</td></tr>",
        "</table>",
    ]


def test_nothing_worse(fake_pypi, capsys):
    code, data = fixture_diff(fake_pypi, capsys, old="head", new="head")
    assert code == 0
    text = ac.report(data, "", "uv.lock", "origin/main")
    assert "<p>Compared <code>uv&#46;lock</code> with <code>origin&#47;main</code>. Nothing got worse.</p>" in text
    assert "<table>" not in text


def test_new_gaps_alone_are_not_nothing_worse(tmp_path, capsys):
    (tmp_path / "diff.json").write_text(json.dumps(diff_report(new=[new_entry()])), encoding="utf-8")
    md = tmp_path / "report.md"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "1", "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--markdown", str(md)]) == 1
    text = md.read_text(encoding="utf-8")
    assert "1 new package has gaps." in text and "Nothing got worse" not in text
    assert "<td>(new) → ● source</td>" in text
    assert "Nothing got worse" not in capsys.readouterr().out


def test_skip_reason_is_escaped_in_code():
    row = ac.rows(diff_report(worse=[worse_entry(new_status="skipped", new_skip_reason="git@evil|x")]))[0]
    assert "<td>✓ → skipped (<code>git&#64;evil&#124;x</code>)</td>" in row


@pytest.mark.parametrize(
    "text",
    [
        "@octocat",
        "a|b",
        "<script>alert(1)</script>",
        "1.0\n::error::pwn ##[error]x",
        "www.example.com",
        "_emph_ *b* `code` [l](http://x) #12 :smile:",
        "-->",
        "1.0\u202egnp.exe",
    ],
)
def test_untrusted_text_is_inert(text):
    escaped = ac.esc(text)
    assert INERT.match(escaped)
    shown = html.unescape(escaped)
    # Exactly the text, except that control and format characters are shown as \uXXXX.
    assert shown == text.replace("\n", "\\u000a").replace("\u202e", "\\u202e")


@pytest.mark.parametrize("text", ["x" * 1000, "%" * 1000, "\n" * 1000])
def test_escaped_text_is_capped_after_escaping(text):
    escaped = ac.esc(text)
    assert len(escaped) <= ac.MAX_ESCAPED + 1 and escaped.endswith("…")
    assert html.unescape(escaped[:-1]) in text.replace("\n", "\\u000a")


def test_a_hostile_package_stays_in_its_cell():
    data = diff_report(
        worse=[
            {
                "name": "evil|</td></table>\n\n# hi",
                "old_versions": ["1.0"],
                "new_versions": ["2.0\n\n@octocat"],
                "targets": {"linux-x86_64": {"old_status": "wheel", "new_status": "<b>source"}},
            }
        ],
        unchecked=["@someone"],
    )
    text = ac.report(data, "", "uv.lock", "main")
    table = text[text.index("<table>") : text.index("</table>")]
    assert "" not in table.splitlines()  # a blank line would end the HTML block
    assert table.count("<td>") == 4 and table.count("<tr>") == 2
    assert "@" not in text and "<b>" not in text and "|" not in text and "\n#" not in text


def test_unchecked_names_are_capped():
    data = diff_report(unchecked=[f"pkg{i}" for i in range(25)])
    text = ac.report(data, "", "uv.lock", "main")
    assert "<code>pkg19</code> and 5 more</p>" in text and "pkg20" not in text


def test_the_body_stays_within_its_budget():
    long = "%" * 1000  # escapes to 5 times its length, then is capped
    data = diff_report(
        worse=[worse_entry(name=f"{i}{long}") for i in range(400)],
        new=[new_entry(name=f"{i}{long}") for i in range(400)],
        unchecked=[f"{i}{long}" for i in range(100)],
    )
    for budget in (ac.MAX_COMMENT - 100, 20_000):
        text = ac.report(data, "", long, long, head="9f8e7d6", budget=budget)
        assert len(text) <= budget
        assert re.search(r'<tr><td colspan="4">and \d+ more rows</td></tr>\n</table>', text)
        assert " and 80 more</p>" in text


def test_the_comment_payload_fits_github(tmp_path, capsys):
    long = "%" * 1000
    data = diff_report(worse=[worse_entry(name=f"{i}{long}") for i in range(400)], unchecked=[long] * 30)
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "1", "--lockfile", "a-" * 200]
    argv += ["--base", "main", "--markdown", str(tmp_path / "r.md"), "--payload", str(tmp_path / "c.json")]
    assert ac.main(argv) == 1
    assert len(json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["body"]) <= ac.MAX_COMMENT


@pytest.mark.parametrize(
    "content, code, says",
    [
        # diff's own error report
        ('{"error": {"message": "can\'t read x:uv.lock from git: <bad>", "type": "GitInputError"}}', 2, "couldn"),
        ("", 2, "exited 2 without a report"),
        ("", 0, "exited 0 without a report"),  # e.g. -h in args
        ("", 1, "exited 1 without a report"),  # e.g. pipx itself failed
        ("usage: wheelgap diff ...", 0, "exited 0 without a report"),
        ('{"version": "0.9.0", "packages": []}', 1, "something other than its report"),  # not a diff report
        ('{"worse": [{"name": "x"}], "new": []}', 1, "something other than its report"),  # no targets
        ('{"worse": [{"name": "x", "targets": {}}], "new": []}', 1, "something other than its report"),
        (json.dumps(diff_report()), 1, "doesn't match its report"),  # exit 1, but nothing listed
        (json.dumps(diff_report(worse=[worse_entry()])), 0, "doesn't match its report"),
        (json.dumps(diff_report(new=[new_entry()])), 0, "doesn't match its report"),
        (json.dumps(diff_report()), 2, "doesn't match its report"),  # a valid report, but exit 2
        (json.dumps(diff_report(worse=[worse_entry()])), 2, "doesn't match its report"),
        (json.dumps(diff_report()), 3, "doesn't match its report"),
    ],
)
def test_errors_and_disagreements_never_pass(tmp_path, capsys, content, code, says):
    path = tmp_path / "diff.json"
    path.write_text(content, encoding="utf-8")
    data, problem = ac.load(str(path), code)
    assert data is None and says in html.unescape(problem)
    assert "<bad>" not in problem
    md = tmp_path / "report.md"
    argv = ["report", "--diff-json", str(path), "--exit-code", str(code), "--lockfile", "uv.lock", "--base", "main"]
    assert ac.main([*argv, "--markdown", str(md)]) == 2
    text = md.read_text(encoding="utf-8")
    assert "The check couldn't run" in text and "Nothing got worse" not in text


@pytest.mark.parametrize(
    "data, code, result",
    [(diff_report(), 0, 0), (diff_report(worse=[worse_entry()]), 1, 1), (diff_report(new=[new_entry()]), 1, 1)],
)
def test_agreeing_reports_give_the_diffs_result(tmp_path, capsys, data, code, result):
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", str(code), "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--markdown", str(tmp_path / "r.md")]) == result


def test_marker_names_the_lockfile_and_cant_close_the_comment():
    assert ac.marker("uv.lock") == "<!-- wheelgap-compare lockfile=uv.lock -->"
    assert ac.marker("a--b/uv.lock") == "<!-- wheelgap-compare lockfile=a%2D%2Db/uv.lock -->"
    assert ac.marker('x -->y"\\') == "<!-- wheelgap-compare lockfile=x%20%2D%2D%3Ey%22%5C -->"


def test_main_writes_the_report_and_comment_and_logs_only_counts(fake_pypi, capsys, tmp_path):
    code, data = fixture_diff(fake_pypi, capsys)
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    md, payload = tmp_path / "report.md", tmp_path / "comment.json"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", str(code), "--lockfile", "uv.lock"]
    argv += ["--base", "1a2b3c4", "--base-branch", "--head", "9f8e7d6" * 5 + "abcdef12345"[:5]]
    argv += ["--markdown", str(md), "--payload", str(payload)]
    assert ac.main(argv) == 1
    log = capsys.readouterr().out
    assert log == "wheelgap: 1 package got worse, 1 new package has gaps. The job summary has the details.\n"
    report = md.read_text(encoding="utf-8")
    assert "<code>armlib</code>" in report and " at <code>9f8e7d6</code> " in report
    body = json.loads(payload.read_text(encoding="utf-8"))["body"]
    assert body == f"{ac.marker('uv.lock')}\n{report}"


def test_head_must_be_a_commit(tmp_path, capsys):
    (tmp_path / "diff.json").write_text(json.dumps(diff_report()), encoding="utf-8")
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "0", "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--head", "<b>not a sha", "--markdown", str(tmp_path / "r.md")]) == 0
    assert " at " not in (tmp_path / "r.md").read_text(encoding="utf-8")


# The comment to edit: only the token's own, and only with the marker at the start.
MINE = "github-actions[bot]"


def comment(id_, login, body):
    return {"id": id_, "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}, "body": body}


def test_pick_only_edits_the_tokens_own_marker_comment():
    mark = ac.marker("uv.lock")
    listing = [
        comment(1, "mallory", f"{mark}\nplanted by the pull request's author"),
        comment(2, "codecov[bot]", f"{mark}\nanother bot"),
        comment(3, MINE, f"quoting it: {mark}"),
        comment(4, MINE, f"{ac.marker('other/uv.lock')}\nanother lockfile"),
        comment(5, MINE, f"{mark}\nours"),
        comment(6, MINE, f"{mark}\na later duplicate"),
    ]
    assert ac.pick(listing, MINE, "uv.lock") == 5
    assert ac.pick(listing[:5], "GitHub-Actions[bot]", "uv.lock") == 5  # logins aren't case-sensitive
    assert ac.pick(listing[:4], MINE, "uv.lock") is None
    assert ac.pick(listing, "mallory", "uv.lock") == 1  # a personal token edits its owner's comment


def test_pick_reads_paginated_output(tmp_path, capsys):
    mark = ac.marker("uv.lock")
    pages = json.dumps([comment(1, "x", "hi")]) + "\n" + json.dumps([comment(7, MINE, f"{mark}\n")]) + "\n"
    (tmp_path / "c.json").write_text(pages, encoding="utf-8")
    assert ac.main(["pick", "--comments", str(tmp_path / "c.json"), "--login", MINE, "--lockfile", "uv.lock"]) == 0
    assert capsys.readouterr().out == "7\n"


@pytest.mark.parametrize("listing", ["", "[]", "[]\n[]\n"])
def test_pick_finds_nothing(tmp_path, capsys, listing):
    (tmp_path / "c.json").write_text(listing, encoding="utf-8")
    assert ac.main(["pick", "--comments", str(tmp_path / "c.json"), "--login", MINE, "--lockfile", "uv.lock"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("listing", ["{", '{"message": "Not Found"}', "<html>"])
def test_a_broken_listing_is_an_error_not_an_empty_one(tmp_path, capsys, listing):
    (tmp_path / "c.json").write_text(listing, encoding="utf-8")
    assert ac.main(["pick", "--comments", str(tmp_path / "c.json"), "--login", MINE, "--lockfile", "uv.lock"]) == 3
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("wheelgap: the compare-to helper failed (")


@pytest.mark.parametrize(
    "line, shown",
    [
        ("::error::pwned", ":\\:error::pwned"),
        ("   ::add-mask::x", "   :\\:add-mask::x"),
        ("\t::stop-commands::tok", "\t:\\:stop-commands::tok"),
        ("\x1b[31m::warning::x", "\x1b[31m:\\:warning::x"),
        ("note ##[error]x", "note #\\#[error]x"),
        ("###[x", "##\\#[x"),
        ("wheelgap: note: couldn't check x", "wheelgap: note: couldn't check x"),
        ("a :: b", "a :: b"),
        ("\ufeff::error::x", "\ufeff:\\:error::x"),
        ("\ufeff \x1b[0m ::error::x", "\ufeff \x1b[0m :\\:error::x"),
    ],
)
def test_defuse(line, shown):
    assert ac.defuse(line) == shown


def test_log_defuses_every_line_the_runner_would_see(tmp_path, capsys):
    (tmp_path / "err").write_bytes(b"ok\n::error::a\r::warning::b\r\n  ::add-mask::c\nx ##[group]d\n")
    assert ac.main(["log", str(tmp_path / "err")]) == 0
    out = capsys.readouterr().out
    lines = re.split(r"\r\n|\r|\n", out.removesuffix("\n"))
    assert lines == ["ok", ":\\:error::a", ":\\:warning::b", "  :\\:add-mask::c", "x #\\#[group]d"]
    assert not any(line.lstrip().startswith("::") or "##[" in line for line in lines)


def test_log_writes_utf8_on_a_cp1252_runner(tmp_path):
    (tmp_path / "err").write_text("wheelgap: note: ✓ → ● ::x\n::error::y\n", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "action_compare.py"), "log", str(tmp_path / "err")],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert done.returncode == 0
    assert done.stdout.decode("utf-8").splitlines() == ["wheelgap: note: ✓ → ● ::x", ":\\:error::y"]


def test_a_problem_report_when_diff_never_ran(tmp_path, capsys):
    md = tmp_path / "r.md"
    argv = ["report", "--lockfile", "uv.lock", "--base-branch", "--problem", "couldn't fetch the base commit"]
    assert ac.main([*argv, "--markdown", str(md)]) == 2
    text = html.unescape(md.read_text(encoding="utf-8"))
    assert "with the base branch. The check couldn't run: couldn't fetch the base commit</p>" in text
