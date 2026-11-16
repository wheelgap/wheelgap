"""The GitHub Action's compare-to step (#201), run as GitHub runs it: the `run:` block from action.yml under
`bash -e`, in a git checkout of the fixture lockfiles, with stub `pipx`, `python3` and `gh` on PATH.

The `pipx` stub runs this wheelgap with the network patched out; `gh` only logs its calls and answers from
environment variables. Both, and `python3`, record it if the token ever reaches them through the environment.
"""

import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("git"),
    reason="the Action's step is bash, run on Linux and macOS runners",
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "actioncompare"
TOKEN = "s3cr3t-token-value"
ARGS = "uv.lock -p 3.13 -t linux-x86_64,musl-aarch64"


def step_script() -> str:
    """The compare step's `run: |` block from action.yml, dedented."""
    lines = (ROOT / "action.yml").read_text(encoding="utf-8").splitlines()
    start = lines.index("      id: compare")
    run = next(i for i in range(start, len(lines)) if lines[i] == "      run: |")
    body = []
    for line in lines[run + 1 :]:
        if line and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


STUBS = {
    "pipx": f"""#!/bin/bash
env | grep -q {TOKEN} && echo pipx >> "$LEAK_FILE"
[ -n "${{PIPX_FAIL:-}}" ] && {{ echo "pipx: failed to install" >&2; exit 1; }}
[ -n "${{PIPX_INJECT:-}}" ] && printf '::error::pwned\\nnote ##[warning]x\\r::add-mask::y\\n' >&2
printf '%s\\n' "$*" >> "${{PIPX_LOG:-/dev/null}}"
shift 4  # run --spec PATH wheelgap
exec "$PYTHON_REAL" -c '
import os
import sys
from wheelgap import pypi
def offline(url, *args, **kwargs):
    with open(os.environ["PYPI_LOG"], "a", encoding="utf-8") as log:
        print(url, file=log)
    if os.environ.get("STUB_RELEASES") and url == "https://pypi.org/pypi/armlib/json":
        return {{"releases": {{"1.6.0": [
            {{"filename": "armlib-1.6.0-cp313-cp313-musllinux_1_2_aarch64.whl"}},
            {{"filename": "armlib-1.6.0-cp313-cp313-manylinux_2_17_x86_64.whl"}},
        ]}}}}
    raise AssertionError("network")
pypi.fetch_json = offline
from wheelgap.cli import main
sys.exit(main(sys.argv[1:]))
' "$@"
""",
    "python3": f"""#!/bin/bash
env | grep -q {TOKEN} && echo python >> "$LEAK_FILE"
exec "$PYTHON_REAL" "$@"
""",
    "gh": r"""#!/bin/bash
printf '%s\n' "$*" >> "$GH_LOG"
printf '%s\n' "$GH_HOST" >> "$GH_LOG.host"
[ "$GH_TOKEN" = "TOKEN_HERE" ] || { echo "gh: no token (HTTP 401)" >&2; exit 4; }
case "$*" in
  "api user --jq .login")
    [ -n "${GH_ME:-}" ] && { echo "$GH_ME"; exit 0; }
    echo "gh: Resource not accessible by integration (HTTP 403)" >&2; exit 1 ;;
  *--paginate*)
    [ -n "${GH_LIST_FAIL:-}" ] && { echo "gh: Server Error (HTTP $GH_LIST_FAIL)" >&2; exit 1; }
    printf '%s\n' "${GH_COMMENTS:-[]}" ;;
  *"-X POST"* | *"-X PATCH"*)
    [ -n "${GH_WRITE_FAIL:-}" ] && { echo "gh: Forbidden (HTTP $GH_WRITE_FAIL)" >&2; exit 1; }
    cp "${@: -1}" "$GH_LOG.body"
    echo '{"body": "::error::the response must not reach the log"}' ;;
esac
""".replace("TOKEN_HERE", TOKEN),
}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def origin(tmp_path_factory):
    """A repository whose HEAD is a pull request's merge commit, as actions/checkout checks one out: the base
    fixture on main, the head fixture on a branch, merged with --no-ff."""
    repo = tmp_path_factory.mktemp("origin")
    git(repo, "init", "-q")
    shutil.copy(FIXTURES / "base" / "uv.lock", repo / "uv.lock")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "pr")
    shutil.copy(FIXTURES / "head" / "uv.lock", repo / "uv.lock")
    git(repo, "commit", "-qam", "bump armlib")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    return {"repo": repo, "base": base, "head": head, "merge": git(repo, "rev-parse", "HEAD")}


@pytest.fixture
def run(origin, tmp_path):
    """Run the step in a fresh shallow clone (so the base commit has to be fetched) and return what happened."""
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin['repo']}", str(work))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in STUBS.items():
        (stubs / name).write_text(text, encoding="utf-8")
        (stubs / name).chmod(0o755)
    (tmp_path / "step.sh").write_text(step_script(), encoding="utf-8")

    def go(**overrides):
        out = tmp_path / "out"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        env = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PYTHON_REAL": sys.executable,
            "LEAK_FILE": str(out / "leaks"),
            "GH_LOG": str(out / "gh.log"),
            "PYPI_LOG": str(out / "pypi.log"),
            "RUNNER_TEMP": str(out),
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_OUTPUT": str(out / "output"),
            "GITHUB_STEP_SUMMARY": str(out / "summary"),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_SHA": origin["merge"],
            "WHEELGAP_ARGS": ARGS,
            "WHEELGAP_DIST": "",
            "WHEELGAP_SUMMARY": "true",
            "WHEELGAP_COMPARE_TO": "base",
            "WHEELGAP_COMMENT": "false",
            "WHEELGAP_TOKEN": "",
            "WHEELGAP_EVENT": "pull_request",
            "WHEELGAP_HEAD_SHA": origin["head"],
            "WHEELGAP_PR": "7",
        }
        if overrides.get("WHEELGAP_COMMENT", "false").lower() == "true":
            env["WHEELGAP_TOKEN"] = TOKEN
        env.update(overrides)
        done = subprocess.run(
            ["bash", "-e", str(tmp_path / "step.sh")], cwd=work, env=env, capture_output=True, text=True
        )
        assert not (out / "leaks").exists(), "the token reached pipx or python"

        def read(name):
            path = out / name
            return path.read_text(encoding="utf-8") if path.exists() else ""

        return {
            "code": done.returncode,
            "log": done.stdout + done.stderr,
            "summary": read("summary"),
            "output": read("output"),
            "gh": read("gh.log").splitlines(),
            "pypi": read("pypi.log").splitlines(),
            "hosts": set(read("gh.log.host").splitlines()),
            "body": json.loads(read("gh.log.body"))["body"] if read("gh.log.body") else None,
            "work": work,
        }

    go.work = work  # type: ignore[attr-defined]
    return go


def writes(result):
    return [call.split(" --input ")[0] for call in result["gh"] if " -X " in call]


def mine(id_, body):
    return {"id": id_, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


MARK = "<!-- wheelgap-compare lockfile=uv.lock -->"


def test_a_worse_pull_request_fails_and_gets_one_comment(run, origin):
    r = run(WHEELGAP_COMMENT="True")  # any case
    assert r["code"] == 1, r["log"]
    assert "wheelgap: 1 package got worse, 1 new package has gaps." in r["log"]
    assert f"with the base branch (<code>{origin['base'][:7]}</code>)" in r["summary"]
    assert f" at <code>{origin['head'][:7]}</code> " in r["summary"]
    assert "<td><code>armlib</code></td>" in r["summary"]
    assert r["output"].startswith("report=")
    assert writes(r) == ["api -X POST repos/o/r/issues/7/comments"]
    assert r["body"].startswith(MARK + "\n") and r["body"].endswith(r["summary"])
    assert "::error::the response" not in r["log"]


@pytest.mark.parametrize("previous", [False, True])
def test_comment_suggests_a_newer_release_when_posting_or_updating(run, previous):
    listing = json.dumps([mine(9, MARK + "\nOld body")]) if previous else "[]"
    r = run(WHEELGAP_COMMENT="true", STUB_RELEASES="1", GH_COMMENTS=listing)
    assert r["code"] == 1, r["log"]
    assert r["pypi"] == ["https://pypi.org/pypi/armlib/json"]
    suggestion = "armlib 1.5.0 -> 1.6.0 has wheels for musl-aarch64"
    body = html.unescape(r["body"])
    assert body.count(suggestion) == 1
    assert body.index("<code>armlib</code>") < body.index(suggestion) < body.index("<code>nativeext</code>")
    assert suggestion not in html.unescape(r["summary"])
    assert writes(r) == [
        "api -X PATCH repos/o/r/issues/comments/9" if previous else "api -X POST repos/o/r/issues/7/comments"
    ]


def test_summary_only_does_not_request_suggestions(run):
    r = run(STUB_RELEASES="1", _WHEELGAP_ACTION_SUGGEST="1")
    assert r["code"] == 1, r["log"]
    assert r["pypi"] == []
    assert r["body"] is None


def test_the_shallow_checkout_fetches_the_base(run, origin):
    r = run()
    assert r["code"] == 1 and r["gh"] == []
    git(r["work"], "cat-file", "-e", f"{origin['base']}^{{commit}}")


def test_only_the_tokens_own_comment_is_edited(run):
    listing = [
        {"id": 1, "user": {"login": "mallory", "type": "User"}, "body": MARK + "\nplanted"},
        {"id": 2, "user": {"login": "codecov[bot]", "type": "Bot"}, "body": MARK + "\nanother bot"},
        mine(3, "quoting " + MARK),
        mine(4, MARK + "\nours"),
    ]
    r = run(WHEELGAP_COMMENT="true", GH_COMMENTS=json.dumps(listing))
    assert r["code"] == 1
    assert writes(r) == ["api -X PATCH repos/o/r/issues/comments/4"]
    assert "the response must not reach the log" not in r["log"]
    r = run(WHEELGAP_COMMENT="true", GH_COMMENTS=json.dumps(listing[:3]))
    assert writes(r) == ["api -X POST repos/o/r/issues/7/comments"]


def test_a_personal_tokens_own_comment_is_edited(run):
    listing = [mine(4, MARK), {"id": 5, "user": {"login": "maintainer", "type": "User"}, "body": MARK}]
    r = run(WHEELGAP_COMMENT="true", GH_ME="maintainer", GH_COMMENTS=json.dumps(listing))
    assert writes(r) == ["api -X PATCH repos/o/r/issues/comments/5"]


def test_nothing_worse_updates_an_old_comment_but_never_posts(run):
    r = run(WHEELGAP_COMPARE_TO="HEAD", WHEELGAP_COMMENT="true")
    assert r["code"] == 0 and writes(r) == []
    assert "Nothing got worse." in r["summary"]
    r = run(WHEELGAP_COMPARE_TO="HEAD", WHEELGAP_COMMENT="true", GH_COMMENTS=json.dumps([mine(9, MARK)]))
    assert r["code"] == 0 and writes(r) == ["api -X PATCH repos/o/r/issues/comments/9"]
    assert "Nothing got worse." in r["body"]


def test_an_error_updates_an_old_comment_but_never_posts(run):
    r = run(WHEELGAP_COMPARE_TO="nosuchref", WHEELGAP_COMMENT="true")
    assert r["code"] == 2 and writes(r) == []
    assert "The check couldn't run" in r["summary"]
    r = run(WHEELGAP_COMPARE_TO="nosuchref", WHEELGAP_COMMENT="true", GH_COMMENTS=json.dumps([mine(9, MARK)]))
    assert r["code"] == 2 and writes(r) == ["api -X PATCH repos/o/r/issues/comments/9"]
    assert "The check couldn't run" in r["body"] and "nosuchref" in r["body"]
    assert "Nothing got worse" not in r["body"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"PIPX_FAIL": "1"},  # pipx exits 1 without a report
        {"WHEELGAP_ARGS": f"{ARGS} --fail-on missing"},  # not a diff option: exit 2
        {"WHEELGAP_ARGS": f"{ARGS} -h"},  # exit 0, but no report
    ],
)
def test_no_report_is_exit_2(run, overrides):
    r = run(**overrides)
    assert r["code"] == 2
    assert "The check couldn't run" in r["summary"] and "Nothing got worse" not in r["summary"]


def test_wheelgaps_messages_are_defused_in_the_log(run):
    r = run(PIPX_INJECT="1")
    assert r["code"] == 1
    assert ":\\:error::pwned" in r["log"] and "note #\\#[warning]x" in r["log"] and ":\\:add-mask::y" in r["log"]
    lines = r["log"].replace("\r", "\n").splitlines()
    assert not [line for line in lines if line.lstrip().startswith("::") or "##[" in line]


def test_a_failed_listing_warns_and_posts_nothing(run):
    r = run(WHEELGAP_COMMENT="true", GH_LIST_FAIL="500")
    assert r["code"] == 1 and writes(r) == []
    assert "::warning::wheelgap: couldn't list the pull request's comments, so none was posted (HTTP 500)." in r["log"]


@pytest.mark.parametrize("status, says", [("403", "pull-requests: write"), ("422", "(HTTP 422). The job")])
def test_a_failed_post_warns_by_status(run, status, says):
    r = run(WHEELGAP_COMMENT="true", GH_WRITE_FAIL=status)
    assert r["code"] == 1
    warning = [line for line in r["log"].splitlines() if line.startswith("::warning::")]
    assert len(warning) == 1 and says in warning[0]
    assert ("pull-requests: write" in warning[0]) == (status == "403")


def test_a_ref_without_a_pull_request_only_writes_the_summary(run):
    r = run(WHEELGAP_EVENT="push", WHEELGAP_PR="", WHEELGAP_HEAD_SHA="", WHEELGAP_COMPARE_TO="HEAD~0")
    assert r["code"] == 0 and "Nothing got worse." in r["summary"]
    r = run(WHEELGAP_EVENT="push", WHEELGAP_PR="", WHEELGAP_COMPARE_TO="HEAD", WHEELGAP_COMMENT="true")
    assert r["code"] == 0 and r["gh"] == []
    assert "::notice::wheelgap: no pull request to comment on" in r["log"]


@pytest.mark.parametrize(
    "overrides, says",
    [
        ({"WHEELGAP_EVENT": "push"}, "compare-to: base needs a pull_request event"),
        ({"WHEELGAP_EVENT": "pull_request_target"}, "compare-to: base needs a pull_request event"),
        (
            {"WHEELGAP_EVENT": "pull_request_target", "WHEELGAP_COMPARE_TO": "HEAD", "WHEELGAP_COMMENT": "true"},
            "comment: true is refused on pull_request_target and workflow_run",
        ),
        (
            {"WHEELGAP_EVENT": "workflow_run", "WHEELGAP_COMPARE_TO": "HEAD", "WHEELGAP_COMMENT": "true"},
            "comment: true is refused on pull_request_target and workflow_run",
        ),
        ({"WHEELGAP_COMMENT": "yes"}, "comment must be true or false"),
        ({"WHEELGAP_HEAD_SHA": "abc; rm -rf /"}, "the pull_request event has no head commit"),
        ({"WHEELGAP_COMMENT_AUTHOR": "my app"}, "comment-author must be a GitHub login"),
        ({"WHEELGAP_ARGS": "-p 3.13 uv.lock"}, "args must start with the lockfile's path"),
        ({"WHEELGAP_ARGS": "diff uv.lock"}, "args must start with the lockfile's path"),
        ({"WHEELGAP_ARGS": "nope.lock -p 3.13"}, "and that file must exist"),
        ({"WHEELGAP_COMPARE_TO": "a:b"}, "compare-to must be base or a git ref"),
        ({"WHEELGAP_COMPARE_TO": "--output=x"}, "compare-to must be base or a git ref"),
    ],
)
def test_refusals(run, overrides, says):
    r = run(**overrides)
    assert r["code"] == 2
    assert r["log"].startswith("::error::wheelgap: ") and says in r["log"].splitlines()[0]
    assert r["summary"] == "" and r["gh"] == []


def test_a_head_checkout_is_refused_for_base(run, origin):
    git(run.work, "fetch", "-q", "--depth", "1", "origin", origin["head"])
    git(run.work, "checkout", "-q", "--detach", origin["head"])
    r = run()
    assert r["code"] == 2 and "needs actions/checkout's default merge commit" in r["log"]


def test_a_folder_named_diff_is_refused(run):
    (run.work / "diff").mkdir()
    r = run(WHEELGAP_COMPARE_TO="HEAD")
    assert r["code"] == 2 and "a file or folder named diff" in r["log"]


def test_a_file_named_like_the_git_copy_is_refused(run):
    (run.work / "HEAD:.").mkdir()
    shutil.copy(FIXTURES / "head" / "uv.lock", run.work / "HEAD:." / "uv.lock")
    r = run(WHEELGAP_COMPARE_TO="HEAD")
    assert r["code"] == 2 and "named like the git ref and lockfile" in r["log"]


@pytest.mark.parametrize(
    "overrides, says",
    [
        ({"WHEELGAP_HEAD_SHA": "0" * 40}, "needs actions/checkout's default merge commit"),
        ({"GITHUB_SHA": "0" * 40}, "needs actions/checkout's default merge commit"),
        ({"BREAK_ORIGIN": "1"}, "couldn't fetch the base commit"),
    ],
)
def test_late_refusals_also_update_an_old_comment(run, overrides, says):
    if overrides.pop("BREAK_ORIGIN", None):
        git(run.work, "remote", "set-url", "origin", "file:///nonexistent/wheelgap")
    r = run(**overrides)
    assert r["code"] == 2 and r["log"].startswith("::error::wheelgap: ") and says in r["log"].splitlines()[0]
    assert "The check couldn't run" in r["summary"] and says in html.unescape(r["summary"])
    assert r["output"].startswith("report=") and writes(r) == []
    r = run(WHEELGAP_COMMENT="true", GH_COMMENTS=json.dumps([mine(9, MARK)]), **overrides)
    assert r["code"] == 2 and writes(r) == ["api -X PATCH repos/o/r/issues/comments/9"]
    assert "The check couldn't run" in r["body"] and "Nothing got worse" not in r["body"]


def test_comment_author_picks_an_apps_comment(run):
    listing = [mine(4, MARK), {"id": 6, "user": {"login": "my-app[bot]", "type": "Bot"}, "body": MARK}]
    r = run(WHEELGAP_COMMENT="true", WHEELGAP_COMMENT_AUTHOR="my-app[bot]", GH_COMMENTS=json.dumps(listing))
    assert writes(r) == ["api -X PATCH repos/o/r/issues/comments/6"]
    assert "api user --jq .login" not in r["gh"]


def test_a_login_with_an_underscore_is_its_own(run):
    listing = [mine(4, MARK), {"id": 8, "user": {"login": "mona_octo", "type": "User"}, "body": MARK}]
    r = run(WHEELGAP_COMMENT="true", GH_ME="mona_octo", GH_COMMENTS=json.dumps(listing))
    assert writes(r) == ["api -X PATCH repos/o/r/issues/comments/8"]


def test_gh_talks_only_to_the_servers_https_host(run):
    r = run(WHEELGAP_COMMENT="true", GITHUB_SERVER_URL="https://ghe.example.com")
    assert writes(r) == ["api -X POST repos/o/r/issues/7/comments"] and r["hosts"] == {"ghe.example.com"}
    r = run(WHEELGAP_COMMENT="true")
    assert r["hosts"] == {"github.com"}


@pytest.mark.parametrize("url", ["http://ghe.example.com", "https://ghe.example.com/x?y", "ftp://github.com", ""])
def test_no_comment_without_an_https_server(run, url):
    r = run(WHEELGAP_COMMENT="true", GITHUB_SERVER_URL=url) if url else run(WHEELGAP_COMMENT="true")
    if not url:  # unset outside Actions: github.com
        assert r["hosts"] == {"github.com"}
        return
    assert r["code"] == 1 and r["gh"] == []
    assert "::warning::wheelgap: GITHUB_SERVER_URL isn't an https URL, so no comment was posted." in r["log"]


def test_a_lockfile_named_like_an_option_is_refused(run):
    shutil.copy(FIXTURES / "head" / "uv.lock", run.work / "-uv.lock")
    r = run(WHEELGAP_COMPARE_TO="HEAD", WHEELGAP_ARGS="-uv.lock -p 3.13")
    assert r["code"] == 2 and "args must start with the lockfile's path" in r["log"]


def test_args_are_not_globbed(run):
    r = run(WHEELGAP_COMPARE_TO="HEAD", WHEELGAP_ARGS="uv.loc? -p 3.13")
    assert r["code"] == 2 and "and that file must exist" in r["log"]


def test_summary_false_skips_the_summary_but_not_the_output(run):
    r = run(WHEELGAP_SUMMARY="false")
    assert r["code"] == 1 and r["summary"] == "" and r["output"].startswith("report=")


def run_step_script() -> str:
    """The first step's `run: |` block — the lockfile or --dist check — dedented."""
    lines = (ROOT / "action.yml").read_text(encoding="utf-8").splitlines()
    start = lines.index("    - name: Run wheelgap")
    run = next(i for i in range(start, len(lines)) if lines[i] == "      run: |")
    body = []
    for line in lines[run + 1 :]:
        if line and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


@pytest.fixture
def run_step(tmp_path):
    """Run the first step (the lockfile/--dist check, no git needed) in a work folder, with stubs on PATH."""
    work = tmp_path / "work"
    work.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in STUBS.items():
        (stubs / name).write_text(text, encoding="utf-8")
        (stubs / name).chmod(0o755)
    (tmp_path / "step.sh").write_text(run_step_script(), encoding="utf-8")

    def go(**overrides):
        out = tmp_path / "out"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        env = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PYTHON_REAL": sys.executable,
            "LEAK_FILE": str(out / "leaks"),
            "PYPI_LOG": str(out / "pypi.log"),
            "PIPX_LOG": str(out / "pipx.log"),
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_STEP_SUMMARY": str(out / "summary"),
            "WHEELGAP_ARGS": "-p 3.13 -t linux-x86_64,musl-aarch64",
            "WHEELGAP_DIST": "",
            "WHEELGAP_SUMMARY": "true",
        }
        env.update(overrides)
        done = subprocess.run(
            ["bash", "-e", str(tmp_path / "step.sh")], cwd=work, env=env, capture_output=True, text=True
        )
        assert not (out / "leaks").exists(), "the token reached pipx or python"

        def read(name):
            path = out / name
            return path.read_text(encoding="utf-8") if path.exists() else ""

        return {
            "code": done.returncode,
            "log": done.stdout + done.stderr,
            "summary": read("summary"),
            "pipx": read("pipx.log").splitlines(),
            "pypi": read("pypi.log").splitlines(),
            "work": work,
        }

    go.work = work  # type: ignore[attr-defined]
    return go


def build_dist(work, *names):
    for name in names:
        (work / "dist").mkdir(exist_ok=True)
        (work / "dist" / name).write_bytes(b"")


@pytest.fixture
def record_step(run_step):
    """Record exact argv and control both exit codes, without running wheelgap."""
    pipx = run_step.work.parent / "stubs" / "pipx"
    pipx.write_text(
        f"""#!/bin/bash
env | grep -q {TOKEN} && echo pipx >> "$LEAK_FILE"
exec "$PYTHON_REAL" -c '
import json
import os
import sys
with open(os.environ["PIPX_LOG"], "a", encoding="utf-8") as log:
    print(json.dumps(sys.argv[1:]), file=log)
if "--markdown" in sys.argv[5:]:
    print(os.environ.get("STEP_REPORT", "### wheelgap\\n\\nA report."))
    sys.exit(int(os.environ.get("STEP_SUMMARY_CODE", "0")))
sys.exit(int(os.environ.get("STEP_CHECK_CODE", "0")))
' "$@"
""",
        encoding="utf-8",
    )
    return run_step


@pytest.mark.parametrize("code", [0, 1, 2])
def test_lockfile_step_preserves_arguments_and_check_exit_code(record_step, code):
    r = record_step(WHEELGAP_ARGS="uv.lock -p 3.13 -t linux-aarch64", STEP_CHECK_CODE=str(code))
    prefix = ["run", "--spec", str(ROOT), "wheelgap"]
    args = ["uv.lock", "-p", "3.13", "-t", "linux-aarch64"]
    calls = [json.loads(line) for line in r["pipx"]]
    assert calls[0] == prefix + ["--github-annotations"] + args
    assert r["code"] == code
    if code <= 1:
        assert calls[1:] == [prefix + ["--markdown", "--no-quiet", "--exit-zero"] + args]
        assert r["summary"] == "### wheelgap\n\nA report.\n"
    else:
        assert len(calls) == 1 and r["summary"] == ""


def test_diff_step_keeps_the_subcommand_first_without_annotations(record_step):
    r = record_step(WHEELGAP_ARGS="diff base.lock head.lock", STEP_CHECK_CODE="1")
    prefix = ["run", "--spec", str(ROOT), "wheelgap"]
    args = ["diff", "base.lock", "head.lock"]
    assert [json.loads(line) for line in r["pipx"]] == [
        prefix + args,
        prefix + ["--markdown", "--no-quiet", "--exit-zero"] + args,
    ]
    assert r["code"] == 1 and r["summary"].startswith("### wheelgap\n")


def test_lockfile_step_splits_and_expands_globs_before_both_runs(record_step):
    for name in ["b.lock", "a.lock", "my lock.lock", "unrelated.txt"]:
        (record_step.work / name).write_bytes(b"")
    r = record_step(WHEELGAP_ARGS="*.lock\t-p  3.13\n-t linux-aarch64")
    args = ["a.lock", "b.lock", "my lock.lock", "-p", "3.13", "-t", "linux-aarch64"]
    prefix = ["run", "--spec", str(ROOT), "wheelgap"]
    assert [json.loads(line) for line in r["pipx"]] == [
        prefix + ["--github-annotations"] + args,
        prefix + ["--markdown", "--no-quiet", "--exit-zero"] + args,
    ]
    assert r["code"] == 0


@pytest.mark.parametrize("report", ["", "usage: wheelgap ...", "0.11.0", "linux-aarch64", "not a report\n### wheelgap"])
def test_lockfile_step_does_not_append_non_report_output(record_step, report):
    r = record_step(WHEELGAP_ARGS="uv.lock", STEP_REPORT=report)
    assert len(r["pipx"]) == 2
    assert r["code"] == 0 and r["summary"] == ""


def test_lockfile_step_ignores_a_failed_summary_run(record_step):
    r = record_step(WHEELGAP_ARGS="uv.lock", STEP_CHECK_CODE="1", STEP_SUMMARY_CODE="2")
    assert len(r["pipx"]) == 2
    assert r["code"] == 1 and r["summary"] == ""


def test_dist_fails_the_job_on_a_dropped_platform(run_step):
    build_dist(
        run_step.work,
        "armlib-2.0-cp313-cp313-manylinux_2_17_x86_64.whl",
        "armlib-2.0.tar.gz",
    )
    r = run_step(WHEELGAP_DIST="dist", STUB_RELEASES="1")
    assert r["code"] == 1, r["log"]
    assert "musl-aarch64: 1.6.0 had a wheel, this release has none" in r["log"]
    assert "### wheelgap" in r["summary"] and "had a wheel, this release has none" in r["summary"]
    assert r["pypi"] == ["https://pypi.org/pypi/armlib/json"] * 2  # check + summary run, nothing else
    # Both runs take --dist, and --dist has no input file to annotate.
    assert r["pipx"] and all("--dist dist" in call and "--github-annotations" not in call for call in r["pipx"])


def test_dist_passes_when_every_target_keeps_its_wheel(run_step):
    build_dist(
        run_step.work,
        "armlib-2.0-cp313-cp313-manylinux_2_17_x86_64.whl",
        "armlib-2.0-cp313-cp313-musllinux_1_2_aarch64.whl",
        "armlib-2.0.tar.gz",
    )
    r = run_step(WHEELGAP_DIST="dist", STUB_RELEASES="1")
    assert r["code"] == 0, r["log"]
    assert "had a wheel" not in r["log"] and "### wheelgap" in r["summary"]


def test_dist_when_the_previous_release_lookup_fails(run_step):
    build_dist(
        run_step.work,
        "armlib-2.0-cp313-cp313-manylinux_2_17_x86_64.whl",
        "armlib-2.0-cp313-cp313-musllinux_1_2_aarch64.whl",
    )
    r = run_step(WHEELGAP_DIST="dist")  # no STUB_RELEASES: the stub answers no index URL
    assert r["code"] == 0, r["log"]
    assert "dropped-target check didn't run" in r["log"]


@pytest.mark.parametrize(
    "dist, says",
    [
        ("-x", "dist must be a folder in the checkout"),
        ("/etc", "dist must be a folder in the checkout"),
        ("../out", "dist must be a folder in the checkout"),
        ("build/../../out", "dist must be a folder in the checkout"),
        ("..", "dist must be a folder in the checkout"),
        ("missing", "isn't a folder in the checkout"),
    ],
)
def test_dist_refusals(run_step, dist, says):
    r = run_step(WHEELGAP_DIST=dist)
    assert r["code"] == 2
    assert r["log"].startswith("::error::wheelgap: ") and says in r["log"].splitlines()[0]
    assert r["pipx"] == [] and r["summary"] == ""


def test_dist_rejects_a_lockfile_in_args(run_step):
    build_dist(run_step.work, "armlib-2.0.tar.gz")
    r = run_step(WHEELGAP_DIST="dist", WHEELGAP_ARGS="uv.lock -p 3.13 -t linux")
    assert r["code"] == 2 and "takes no other inputs" in r["log"]


def test_dist_and_compare_to_are_refused(run):
    r = run(WHEELGAP_DIST="dist")
    assert r["code"] == 2
    assert r["log"].startswith("::error::wheelgap: ") and "can't be combined with compare-to" in r["log"]
    assert r["summary"] == "" and r["gh"] == []
