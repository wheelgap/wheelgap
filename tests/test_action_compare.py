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
from conftest import FAKE_INDEX

from wheelgap import fix, suggest
from wheelgap.check import CELL
from wheelgap.cli import main as cli_main
from wheelgap.diff import main as diff_main
from wheelgap.pypi import DEFAULT_INDEX

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


@pytest.mark.parametrize(
    ("release", "suggestion"),
    [
        ("1.6.0", "armlib 1.5.0 -> 1.6.0 has wheels for musl-aarch64"),
        ("1.4.0", "armlib 1.5.0: no newer release has a wheel for musl-aarch64; the last one that did is 1.4.0"),
        (None, None),  # 404 leaves the affected package's entry alone.
    ],
)
def test_comment_suggests_only_worsened_packages(monkeypatch, fake_pypi, capsys, release, suggestion):
    monkeypatch.setenv("_WHEELGAP_ACTION_SUGGEST", "1")
    if release:
        monkeypatch.setitem(
            FAKE_INDEX,
            "armlib",
            {
                release: [
                    f"armlib-{release}-cp313-cp313-musllinux_1_2_aarch64.whl",
                    f"armlib-{release}-cp313-cp313-manylinux_2_17_x86_64.whl",
                ],
            },
        )
        if release == "1.6.0":  # 1.5.1 restores musl but drops the glibc wheel.
            monkeypatch.setitem(FAKE_INDEX["armlib"], "1.5.1", ["armlib-1.5.1-cp313-cp313-musllinux_1_2_aarch64.whl"])
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--json"]) == 1
    assert fake_pypi == [f"{DEFAULT_INDEX}/armlib/json"]
    data = json.loads(capsys.readouterr().out)
    assert data["worse"][0].get("suggestions") == ([suggestion] if suggestion else None)
    assert data["new"][0].get("suggestions") is None


def test_comment_suggestions_keep_names_from_private_base_local(monkeypatch, fake_pypi, capsys, tmp_path):
    monkeypatch.setenv("_WHEELGAP_ACTION_SUGGEST", "1")
    old = tmp_path / "uv.lock"
    base = (FIXTURES / "base" / "uv.lock").read_text(encoding="utf-8")
    old.write_text(
        base.replace("https://pypi.org/simple", "https://private.example.invalid/simple", 1),
        encoding="utf-8",
    )
    assert diff_main([str(old), str(FIXTURES / "head" / "uv.lock"), *ARGS, "--json"]) == 1
    assert fake_pypi == []
    data = json.loads(capsys.readouterr().out)
    assert data["worse"][0]["name"] == "armlib"
    assert "suggestions" not in data["worse"][0]


def test_an_unexpected_lookup_failure_keeps_the_diff_result(monkeypatch, fake_pypi, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("lookup failed")

    monkeypatch.setenv("_WHEELGAP_ACTION_SUGGEST", "1")
    monkeypatch.setattr(suggest, "look_up_all", fail)
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["worse"][0]["name"] == "armlib"
    assert "suggestions" not in data["worse"][0]


def test_optional_suggestions_never_displace_comparison_rows():
    worse = worse_entry()
    worse["suggestions"] = ["a" * 900]
    data = diff_report(worse=[worse], new=[new_entry()])
    args = (data, "", "uv.lock", "main")
    assert ac.report(*args, budget=650, suggestions=True) == ac.report(*args, budget=650)
    assert "<code>nativeext</code>" in ac.report(*args, budget=650, suggestions=True)
    assert worse["suggestions"][0] in ac.report(*args, suggestions=True)


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


def test_rows_name_the_direct_dependency_per_target():
    worse = worse_entry()
    worse["direct"] = {"linux-x86_64": ["Parent_A", "parent-b"]}
    new = new_entry()
    new["direct"] = {"linux-x86_64": ["mcp"], "win-amd64": ["other"]}
    worse_row, new_row = ac.rows(diff_report(worse=[worse], new=[new]))
    assert worse_row.startswith("<tr><td><code>armlib</code> (via <code>Parent&#95;A</code> +1)</td>")
    assert new_row.startswith("<tr><td><code>nativeext</code> (via <code>mcp</code>)</td>")
    # A malformed value is ignored rather than printed.
    new["direct"] = {"linux-x86_64": [1]}
    assert ac.rows(diff_report(new=[new]))[0].startswith("<tr><td><code>nativeext</code></td>")


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


# The gate's fix (wheelgap diff --fix): what --fix says for the new lockfile, per listed cell.
GAP_WHEEL = "nativeext-0.3-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"


@pytest.fixture
def armlib_upgrade(monkeypatch):
    """armlib 1.6.0 has both wheels again; nativeext has no newer release on the index."""
    monkeypatch.setitem(
        FAKE_INDEX,
        "armlib",
        {
            "1.6.0": [
                "armlib-1.6.0-cp313-cp313-musllinux_1_2_aarch64.whl",
                "armlib-1.6.0-cp313-cp313-manylinux_2_17_x86_64.whl",
            ]
        },
    )
    monkeypatch.setitem(FAKE_INDEX, "nativeext", {"0.3": ["nativeext-0.3.tar.gz"]})


def test_fix_reasons_and_notes_match_fix():
    assert ac.FIX_REASONS == fix.REASON_TEXT
    assert ac.RESOLVE_NOTE == fix.RESOLVE_NOTE


def test_diff_fix_is_exactly_what_fix_says_for_the_new_lockfile(fake_pypi, gap_wheels, capsys, armlib_upgrade):
    gap_wheels.add("nativeext", "0.3", GAP_WHEEL)
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--fix", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert cli_main([new, *ARGS, "--fix", "--json"]) == 1
    main = {p["name"]: p.get("fix", {}) for p in json.loads(capsys.readouterr().out)["packages"]}
    armlib = data["worse"][0]["targets"]["musl-aarch64"]["fix"]
    assert armlib == main["armlib"]["musl-aarch64"]
    assert armlib["kind"] == "upgrade" and armlib["version"] == "1.6.0"
    assert "run uv lock --upgrade-package armlib, then uv sync" in armlib["how"]
    nativeext = data["new"][0]["targets"]
    assert {t: side["fix"] for t, side in nativeext.items()} == main["nativeext"]
    assert nativeext["linux-x86_64"]["fix"]["kind"] == "gap_wheel"
    assert nativeext["musl-aarch64"]["fix"] == {
        "kind": "none",
        "reason": "no_newer_release",
        "abi3_only": False,
        "build_packages": None,
    }
    assert data["fix_summary"] == {
        "gaps": 3,
        "upgrade": 1,
        "gap_wheel": 1,
        "none": 1,
        "not_looked_up": 0,
        "uv_may_fork": 0,
    }


def test_diff_fix_looks_up_only_the_listed_packages(fake_pypi, gap_wheels, capsys, armlib_upgrade, tmp_path):
    # nativeext's gaps are on both sides, so they're unchanged: no lookup, no fix, no fix_summary entry.
    base = (FIXTURES / "base" / "uv.lock").read_text(encoding="utf-8")
    head = (FIXTURES / "head" / "uv.lock").read_text(encoding="utf-8")
    nativeext = head[head.index('[[package]]\nname = "nativeext"') : head.index('[[package]]\nname = "purewheel"')]
    old = tmp_path / "old" / "uv.lock"
    old.parent.mkdir()
    base = base.replace('[[package]]\nname = "purewheel"', nativeext + '[[package]]\nname = "purewheel"')
    old.write_text(base.replace('{ name = "armlib" },', '{ name = "armlib" },\n    { name = "nativeext" },'))
    assert diff_main([str(old), str(FIXTURES / "head" / "uv.lock"), *ARGS, "--fix", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in data["worse"]] == ["armlib"] and data["new"] == []
    assert fake_pypi == [f"{DEFAULT_INDEX}/armlib/json"]
    assert data["fix_summary"]["gaps"] == 1


def test_diff_fix_nothing_worse_looks_nothing_up(fake_pypi, gap_wheels, capsys):
    head = str(FIXTURES / "head" / "uv.lock")
    assert diff_main([head, head, *ARGS, "--fix", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert fake_pypi == [] and gap_wheels.calls == 0
    assert data["fix_summary"]["gaps"] == 0


def test_diff_fix_text_is_the_fix_section(fake_pypi, capsys, armlib_upgrade):
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--fix"]) == 1
    out = capsys.readouterr().out
    assert "1 package got worse, 1 new package has gaps.\n\n--fix: 3 gaps (2 packages × their targets): 1 can be" in out
    assert "  armlib 1.5.0 -> 1.6.0: musl-aarch64\n" in out


def test_diff_fix_failure_keeps_the_result(monkeypatch, fake_pypi, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(fix, "plan", fail)
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--fix", "--json"]) == 1
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert "--fix couldn't work out the fixes (RuntimeError)" in err
    assert not ac.has_fixes(data) and data["summary"] == {"worse": 1, "new": 1}


def test_diff_fix_refuses_deps(capsys):
    assert diff_main(["a==1", "a==2", "--deps", "--fix", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "--fix works on two lockfiles, not with --deps"


def test_the_report_shows_each_fix_and_how(fake_pypi, gap_wheels, capsys, armlib_upgrade):
    gap_wheels.add("nativeext", "0.3", GAP_WHEEL)
    old, new = [str(FIXTURES / side / "uv.lock") for side in ("base", "head")]
    assert diff_main([old, new, *ARGS, "--fix", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    text = ac.report(data, "", "uv.lock", "1a2b3c4", base_branch=True, head="9f8e7d6")
    lines = text.splitlines()
    assert lines[5] == "<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th><th>Fix</th></tr>"
    assert lines[6].endswith("<td>✓ → ● source</td><td>upgrade to <code>1&#46;6&#46;0</code></td></tr>")
    assert lines[7].endswith(f"<td>(new) → ● source</td><td>an unofficial gap wheel: {ac.code(GAP_WHEEL)}</td></tr>")
    assert lines[8].endswith("<td>(new) → ● source</td><td>no fix yet: no newer release has a wheel</td></tr>")
    how = lines[lines.index("</table>") + 1 :]
    assert how[:3] == ["", "<p>How to fix:</p>", "<ul>"] and how[-1] == "</ul>"
    shown = html.unescape(text)
    assert f"<li><code>{armlib_how(data)}</code></li>" in shown and "run uv lock --upgrade-package armlib" in shown
    assert f"<li>{fix.RESOLVE_NOTE}</li>" in shown
    assert "Gap wheels are unofficial" in text


def armlib_how(data):
    return data["worse"][0]["targets"]["musl-aarch64"]["fix"]["how"]


@pytest.mark.parametrize(
    "entry, shown",
    [
        ({"kind": "upgrade", "version": "2.0", "how": "x", "constraint": None, "gap_wheel": None}, "upgrade to 2.0"),
        (
            {
                "kind": "upgrade",
                "version": "2.0",
                "constraint": {
                    "file": "pyproject.toml",
                    "requirements": ["a<2"],
                    "allows": False,
                    "via": "requirement",
                },
                "gap_wheel": {"wheel": "a.whl"},
            },
            "upgrade to 2.0 (needs pyproject.toml's a<2 relaxed); if you can't, an unofficial gap wheel",
        ),
        (
            {
                "kind": "upgrade",
                "version": "2.0",
                "constraint": {"file": "r.txt", "requirements": ["a==1"], "allows": False, "via": "constraint"},
            },
            "upgrade to 2.0 (needs a==1 in the -c constraints of r.txt relaxed)",
        ),
        ({"kind": "upgrade", "version": "2.0", "constraint": {"allows": True}}, "upgrade to 2.0"),
        ({"kind": "upgrade", "version": "2.0rc1", "prerelease": True}, "upgrade to 2.0rc1 (a pre-release)"),
        ({"kind": "none", "reason": "loses_wheel"}, "no fix yet: every newer release with a wheel drops one"),
        ({"kind": "none", "reason": "no_newer_release", "abi3_only": True}, "; only abi3 wheels, which free-threaded"),
        ({"kind": "none", "reason": "not_looked_up"}, "newer releases weren't looked up (the log says why)"),
        ({"kind": "none", "reason": "<b>new</b>"}, "no fix yet: <b>new</b>"),
        ({"kind": "@octocat"}, "@octocat"),
        (None, ""),
        ("upgrade", ""),
    ],
)
def test_fix_cell(entry, shown):
    cell = ac.fix_cell(entry)
    assert shown in html.unescape(re.sub(r"</?code>", "", cell))
    assert "<b>" not in cell and "@" not in cell  # anything from the report is escaped, in <code>


def test_hostile_fix_text_stays_inert():
    hostile = "@octocat [x](https://evil.example) </code></td></table>\n\n# hi |"
    fix_entry = {
        "kind": "upgrade",
        "version": hostile,
        "how": hostile,
        "constraint": {"file": hostile, "requirements": [hostile], "allows": False, "via": "requirement"},
        "gap_wheel": None,
    }
    worse = worse_entry()
    worse["targets"]["linux-x86_64"]["fix"] = fix_entry
    new = new_entry()
    new["targets"]["linux-x86_64"]["fix"] = {"kind": "gap_wheel", "wheel": hostile}
    text = ac.report(diff_report(worse=[worse], new=[new]), "", "uv.lock", "main")
    table = text[text.index("<table>") : text.index("</table>")]
    assert "" not in table.splitlines()  # a blank line would end the HTML block
    assert table.count("<td>") == 10 and table.count("<tr>") == 3
    assert "@" not in text and "|" not in text and "\n#" not in text and "](" not in text
    assert text.count("</code>") == text.count("<code>")
    for piece in re.findall(r"<code>(.*?)</code>", text):
        assert INERT.match(piece)


def test_how_to_fix_never_displaces_comparison_rows():
    worse = worse_entry()
    worse["targets"]["linux-x86_64"]["fix"] = {"kind": "upgrade", "version": "2.0", "how": "a" * 1500}
    data = diff_report(worse=[worse], new=[new_entry()])
    small = ac.report(data, "", "uv.lock", "main", budget=900)
    assert "<code>nativeext</code>" in small and "How to fix" not in small and len(small) <= 900
    assert "How to fix" in ac.report(data, "", "uv.lock", "main")


def test_the_comment_says_resolved_but_the_summary_does_not(tmp_path, capsys):
    (tmp_path / "diff.json").write_text(json.dumps(diff_report()), encoding="utf-8")
    md, body = tmp_path / "report.md", tmp_path / "comment.md"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "0", "--lockfile", "uv.lock"]
    argv += ["--base", "main", "--markdown", str(md), "--payload", str(tmp_path / "c.json")]
    assert ac.main([*argv, "--comment-markdown", str(body)]) == 0
    assert "Resolved" not in md.read_text(encoding="utf-8")
    comment = body.read_text(encoding="utf-8")
    assert comment.startswith(ac.marker("uv.lock") + "\n### wheelgap diff\n\n<p><b>Resolved.</b> Compared ")
    assert comment.endswith("Nothing got worse.</p>\n")
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["body"] == comment


def test_a_worse_comment_is_the_report(tmp_path, capsys):
    worse = worse_entry()
    worse["targets"]["linux-x86_64"]["fix"] = {"kind": "upgrade", "version": "2.0", "how": "Use armlib==2.0 instead."}
    (tmp_path / "diff.json").write_text(json.dumps(diff_report(worse=[worse])), encoding="utf-8")
    md, body = tmp_path / "report.md", tmp_path / "comment.md"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "1", "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--markdown", str(md), "--comment-markdown", str(body)]) == 1
    assert body.read_text(encoding="utf-8") == f"{ac.marker('uv.lock')}\n{md.read_text(encoding='utf-8')}"
    assert "Resolved" not in body.read_text(encoding="utf-8")


def test_the_workflows_pyyaml_fixtures(monkeypatch, fake_pypi, capsys):
    """What the Action compare-to workflow's fix step expects, offline: the pull request adds pyyaml 6.0.3, which builds
    from source on 3.15 (the lockfiles list every file, so that needs no lookup), and --fix names 6.0.4."""
    old, new = [str(FIXTURES / f"pyyaml-{side}" / "uv.lock") for side in ("base", "head")]
    args = ["-p", "3.15", "-t", "linux-x86_64,win-arm64", "--json"]
    assert diff_main([old, new, *args]) == 1
    assert fake_pypi == []
    data = json.loads(capsys.readouterr().out)
    assert data["worse"] == [] and [p["name"] for p in data["new"]] == ["pyyaml"]
    wheels = ["pyyaml-6.0.4-cp315-cp315-manylinux_2_28_x86_64.whl", "pyyaml-6.0.4-cp315-cp315-win_arm64.whl"]
    monkeypatch.setitem(FAKE_INDEX, "pyyaml", {"6.0.3": ["pyyaml-6.0.3.tar.gz"], "6.0.4": wheels})
    assert diff_main([old, new, *args, "--fix"]) == 1
    data = json.loads(capsys.readouterr().out)
    fixes = [side["fix"] for side in data["new"][0]["targets"].values()]
    assert [(f["kind"], f["version"]) for f in fixes] == [("upgrade", "6.0.4")] * 2
    text = ac.report(data, "", "action-compare-test/uv.lock", "1a2b3c4")
    assert "<td><code>win-arm64</code></td><td>(new) → ● source</td><td>upgrade to <code>6&#46;0&#46;4</code>" in text


def test_diff_fix_for_a_bumped_requirements_pin_from_git(fake_pypi, gap_wheels, capsys, monkeypatch, tmp_path):
    # The Dependabot case: the base copy of requirements.txt comes from git (which can't place a name on PyPI), the
    # pull request moves an existing pin. The fix is still what `wheelgap requirements.txt --fix` says.
    monkeypatch.setitem(
        FAKE_INDEX,
        "armlib",
        {
            "1.4.0": ["armlib-1.4.0-cp313-cp313-manylinux_2_17_x86_64.whl"],
            "1.5.0": ["armlib-1.5.0.tar.gz"],
            "1.6.0": ["armlib-1.6.0-cp313-cp313-manylinux_2_17_x86_64.whl"],
        },
    )
    monkeypatch.chdir(tmp_path)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q"], check=True)
    (tmp_path / "requirements.txt").write_text("armlib==1.4.0\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (tmp_path / "requirements.txt").write_text("armlib==1.5.0\n")
    args = ["-p", "3.13", "-t", "linux-x86_64", "--fix", "--json"]
    assert diff_main(["HEAD:./requirements.txt", "requirements.txt", *args]) == 1
    got = json.loads(capsys.readouterr().out)["worse"][0]["targets"]["linux-x86_64"]["fix"]
    assert cli_main(["requirements.txt", *args[:-2], "--fix", "--json"]) == 1
    main = json.loads(capsys.readouterr().out)["packages"][0]["fix"]["linux-x86_64"]
    assert got == main and got["kind"] == "upgrade" and got["version"] == "1.6.0"


def moved_lock(registry: str) -> str:
    """A uv.lock like jumpserver/jumpserver#17168's (#807): locked for Python 3.14 and newer, its package from
    `registry`."""
    return (
        'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n\n'
        '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { editable = "." }\n'
        'dependencies = [\n    { name = "fastlib" },\n]\n\n'
        f'[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/fastlib-2.0.0.tar.gz", hash = "sha256:00", size = 1 }\n'
        'wheels = [\n    { url = "https://files.example/fastlib-2.0.0-cp314-cp314-manylinux_2_17_x86_64.whl", '
        'hash = "sha256:01", size = 1 },\n]\n'
    )


def test_a_python_the_lock_leaves_out_is_a_note_not_a_failure(fake_pypi, capsys, tmp_path):
    """#807, through the Action's path: diff --json, then the report. The base lock is PyPI's and the head moved its
    package to a mirror; both are locked for 3.14+, so 3.13 isn't compared and the step passes with a note."""
    for side, registry in (("base", "https://pypi.org/simple"), ("head", "https://mirrors.example/pypi/simple")):
        (tmp_path / side).mkdir()
        (tmp_path / side / "uv.lock").write_text(moved_lock(registry), encoding="utf-8")
    argv = [str(tmp_path / "base" / "uv.lock"), str(tmp_path / "head" / "uv.lock"), "-p", "3.13", "-p", "3.14"]
    code = diff_main([*argv, "-t", "linux-x86_64", "--json"])
    assert code == 0
    (tmp_path / "diff.json").write_text(capsys.readouterr().out, encoding="utf-8")
    md, body = tmp_path / "report.md", tmp_path / "comment.md"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", str(code), "--lockfile", "uv.lock"]
    argv += ["--base", "main", "--markdown", str(md), "--comment-markdown", str(body)]
    assert ac.main(argv) == 0
    assert capsys.readouterr().out == (
        "wheelgap: Nothing got worse on the compared targets. 1 target not compared. The job summary has the details.\n"
    )
    for text in (md.read_text(encoding="utf-8"), body.read_text(encoding="utf-8")):
        assert "Nothing got worse on the compared targets.</p>" in text and "<table>" not in text
        assert "drops" not in text
        assert (
            "<p>Not compared: <code>3&#46;13 linux-x86&#95;64</code>. <code>both lockfiles are locked for Python "
            "&#62;&#61;3&#46;14&#44; which leaves out Python 3&#46;13</code>: a lock isn't made for a Python outside "
            "its requires-python, so nothing there counts as worse.</p>"
        ) in text


def test_every_target_left_out_says_nothing_was_compared(tmp_path, capsys):
    data = diff_report()
    data["targets"] = [{"label": "linux-x86_64", "python": "3.13", "platform": "linux-x86_64"}]
    data["not_compared"] = [{"target": "linux-x86_64", "sides": ["new"], "reason": "the new lockfile is locked"}]
    text = ac.report(data, "", "uv.lock", "main")
    assert "Nothing was compared.</p>" in text and "Nothing got worse" not in text
    data["not_compared"] = []
    assert "Nothing got worse.</p>" in ac.report(data, "", "uv.lock", "main")


def test_a_python_the_change_dropped_leads_the_report_and_warns_in_the_log(fake_pypi, capsys, tmp_path):
    """Only the head lock leaves 3.13 out: the pull request dropped it. Still exit 0 (nothing worse or new), but the
    summary and comment lead with it and the log gets a ::warning:: line."""
    for side, requires in (("base", ">=3.12"), ("head", ">=3.14")):
        (tmp_path / side).mkdir()
        lock = moved_lock("https://pypi.org/simple").replace('">=3.14"', f'"{requires}"')
        (tmp_path / side / "uv.lock").write_text(lock, encoding="utf-8")
    argv = [str(tmp_path / "base" / "uv.lock"), str(tmp_path / "head" / "uv.lock"), "-p", "3.13", "-p", "3.14"]
    code = diff_main([*argv, "-t", "linux-x86_64", "--json"])
    assert code == 0
    (tmp_path / "diff.json").write_text(capsys.readouterr().out, encoding="utf-8")
    md, body = tmp_path / "report.md", tmp_path / "comment.md"
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", str(code), "--lockfile", "uv.lock"]
    argv += ["--base", "main", "--markdown", str(md), "--comment-markdown", str(body)]
    assert ac.main(argv) == 0
    assert capsys.readouterr().out.splitlines() == [
        "wheelgap: Nothing got worse on the compared targets. 1 target not compared. The job summary has the details.",
        "::warning::wheelgap: This change drops Python 3.13 support: the new lockfile's requires-python leaves it "
        "out. The job summary has the details.",
    ]
    for text in (md.read_text(encoding="utf-8"), body.read_text(encoding="utf-8")):
        lines = text.splitlines()
        at = lines.index(next(line for line in lines if "Nothing got worse on the compared targets." in line))
        assert lines[at + 2] == (
            "<p><b>This change drops Python 3&#46;13 support.</b> <code>the new lockfile no longer supports Python "
            "3&#46;13 &#40;old&#58; &#62;&#61;3&#46;12&#44; new&#58; &#62;&#61;3&#46;14&#41;</code>, so installing the "
            "new lock there now fails. Not compared: <code>3&#46;13 linux-x86&#95;64</code>; the drop doesn't count "
            "as worse.</p>"
        )


def test_the_drop_warning_names_only_a_real_version(tmp_path, capsys):
    data = diff_report()
    hostile = "3.13\n::error::owned"
    data["not_compared"] = [{"target": "x", "sides": ["new"], "python": hostile, "reason": "r"}]
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "0", "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--markdown", str(tmp_path / "r.md")]) == 0
    log = capsys.readouterr().out.splitlines()
    assert log[-1] == (
        "::warning::wheelgap: This change drops a target Python support: the new lockfile's requires-python leaves it "
        "out. The job summary has the details."
    )
    assert not any(line.startswith("::error") for line in log)
    assert "This change drops Python 3&#46;13&#92;&#117;&#48;&#48;&#48;&#97;&#58;&#58;error" in (
        tmp_path / "r.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "not_compared",
    [
        "3.13",
        [["3.13"]],
        [{"target": ["x"], "reason": "r"}],
        [{"target": "3.13"}],
        [{"target": "x", "reason": "r", "python": 3.13}],
        [{"target": "x", "reason": "r", "sides": "new"}],
    ],
)
def test_a_malformed_not_compared_is_never_a_pass(tmp_path, capsys, not_compared):
    data = diff_report()
    data["not_compared"] = not_compared
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    argv = ["report", "--diff-json", str(tmp_path / "diff.json"), "--exit-code", "0", "--lockfile", "uv.lock"]
    assert ac.main([*argv, "--base", "main", "--markdown", str(tmp_path / "r.md")]) == 2
    assert "printed something other than its report" in (tmp_path / "r.md").read_text(encoding="utf-8")
