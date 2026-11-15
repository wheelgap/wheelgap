# CI

Run wheelgap in CI so a dependency bump that drops a platform's wheels fails the pull request, not the release build.
Always pass `-p` and `-t` (or set them in [`[tool.wheelgap]`](configuration.md)): otherwise the CI runner's own
Python is checked.

## GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
```

The job fails when a dependency would build from source on a target you ship, for example when a dependency bump
drops ARM wheels. The gap table also goes to the run's summary page.

| input | default | meaning |
| --- | --- | --- |
| `args` | `""` | the arguments passed to wheelgap, as on the [command line](cli.md) |
| `summary` | `"true"` | also write the gap table to the job summary page (skipped when `args` include `--json`); `"false"` turns it off |
| `compare-to` | `""` | after 0.9.0: `base`, or a git ref such as `origin/main`, to fail only when the pull request makes the lockfile worse; see [below](#compare-to) |
| `comment` | `"false"` | after 0.9.0, with `compare-to`: also post one pull request comment when something got worse, and edit it on later runs |
| `github-token` | `${{ github.token }}` | after 0.9.0: the token for the pull request comment |
| `comment-author` | `""` | after 0.9.0: the login the token comments as, such as `my-app[bot]`, for a GitHub App's token |

After 0.9.0, the Action also has a `report` output: with `compare-to`, the path of the Markdown report that went to the
job summary.

The step's result is wheelgap's exit code: [`--fail-on missing`](cli.md#fail-on), [`--ignore`](cli.md#ignore) and
[`--exit-zero`](cli.md#exit-zero) in `args` work as they do on the command line.

### Only fail when a pull request makes things worse {#compare-to}

!!! note "After 0.9.0"
    `compare-to`, `comment`, `github-token`, `comment-author` and the `report` output aren't in
    `wheelgap/wheelgap@v0.9.0`: until the next release, use `wheelgap/wheelgap@main`.

With `compare-to: base`, the Action runs `wheelgap diff` between the lockfile on the pull
request's base commit and the one in the checkout, so it fails only when a dependency bump (from Dependabot, Renovate or
a person) loses a wheel or adds a package with a gap, not on gaps you already have:

```yaml
on: pull_request
permissions:
  contents: read
  pull-requests: write  # only for comment: true
jobs:
  wheels:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: wheelgap/wheelgap@main  # a release after 0.9.0, once there is one
        with:
          args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64,musl-aarch64
          compare-to: base
          comment: true
```

- `args` must then start with the lockfile's path, followed only by what `wheelgap diff` takes: `-p` and `-t` (and,
  after 0.9.0, `--glibc`, `--musl`, `--macos` and `--distro`); anything else exits 2. The step exits 1 when something
  got worse and 0 when nothing did, but only when `wheelgap diff`'s exit code and its report agree; anything else, such
  as bad input or a run that printed no report, exits 2.
- The lockfile must exist on both sides: one the pull request adds exits 2. A requirements file that includes others
  with `-r` or `-c` exits 2 too, because only the lockfile itself (and the `pyproject.toml` beside it) is read from git.
- `compare-to: base` needs a `pull_request` event and the commit `actions/checkout` checks out for it by default: the
  merge of the pull request into its base branch. It compares with that merge commit's first parent, the base branch the
  pull request is merged onto, and fetches it if the checkout is shallow (the default; in a private repository with
  `persist-credentials: false`, check out with `fetch-depth: 0` instead). Any other event, or a checkout of anything but
  that merge commit (`GITHUB_SHA`), such as the pull request's head commit, exits 2; then give a git ref instead, such
  as `compare-to: origin/main` (fetch it first), or the merge base of the two.
- The report goes to the job summary: a table of each package and target that got worse (`armlib 1.4.0 → 1.5.0`,
  `musl-aarch64`, `✓ → ● source`) and each new package's gaps. The action's `report` output is the path of the same
  Markdown file. wheelgap's own messages go to the log with anything the runner would read as a [workflow
  command](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands) defused, since they can
  quote the lockfile.
- With `comment: true`, it also posts the table as a pull request comment when something got worse, and edits that one
  comment on every later run, whatever the result: to say nothing got worse, or that the check couldn't run and why
  (including when the checkout isn't the merge commit or the base commit can't be fetched). The comment names the commit
  it checked, so a run that finishes out of order shows. A hidden marker naming the lockfile finds the comment, so two
  lockfiles get two comments, and only a comment by the token's own identity is edited: `github-actions[bot]` for the
  default token, or a personal token's user. For a GitHub App's token, set `comment-author` to the app's login (such as
  `my-app[bot]`); without it, each run that finds something worse posts a new comment and leaves the older ones as they
  were. The token is only sent to the `https://` host in `GITHUB_SERVER_URL` (GitHub Enterprise Server included); if
  that isn't an `https://` URL, no comment is posted and the step warns. Package names and versions come from the pull
  request, so they're escaped and can't add links, @mentions or Markdown to the comment.
- **Permissions.** Commenting needs `pull-requests: write` for the token (`github-token`, by default the workflow's
  `GITHUB_TOKEN`). Under `pull_request`, pull requests from forks get a read-only token (unless a private repository's
  settings send write tokens to them), and Dependabot's pull requests get one by default too, unless the workflow's
  `permissions:` asks for more as above. When the comment can't be posted, the step warns (naming the missing permission
  on HTTP 403) and the job summary still has the report; the result is the same either way. So for Dependabot and fork
  pull requests, the summary-only setup (`comment: false`, the default, with `contents: read`) is enough.
- **Don't read the pull request's lockfile in a workflow that has a write token or secrets.** `pull_request_target` and
  `workflow_run` run the base branch's workflow with them, so a pull request's files there must be treated as hostile:
  checking out and running its code hands it the token, and even reading its lockfile can pull other files' contents
  into the output, through a `-r` include or a symlink. So `comment: true` is refused on those events, and `compare-to:
  base` needs `pull_request`.

Without `compare-to`, you can also put a [`diff`](diff.md) command in `args`, but then set `summary: "false"`: the
summary is a second run that puts `--markdown` and other flags before `args`, so with a `diff` command it would check a
PyPI package named `diff` instead. `compare-to` is the better option: it picks the base commit for you, writes a job
summary for the comparison, and can comment on the pull request.

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0  # so git can read the base branch's lockfile
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: diff origin/${{ github.base_ref }}:uv.lock uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
    summary: "false"
```

### Annotations {#annotations}

!!! note "After 0.9.0"
    The annotations need the Action from `main` or a release after 0.9.0: `wheelgap/wheelgap@v0.9.0` doesn't add
    them.

Each package with a gap is also marked on its own line of the lockfile in the pull request's diff, as a GitHub
annotation: an error when it fails the check, or a warning for a source build that `--fail-on missing` allows or a
gap only on `--advisory` targets. So a bump that drops a platform is flagged next to the line that bumped it.

The Action passes `--github-annotations` for this; in your own workflow steps, pass it yourself. It respects
`--ignore`, and `--exit-zero` still annotates. The annotations are printed before the usual output, or to stderr with
`--json` and `--markdown` so their stdout stays the same. GitHub shows at most 10 errors and 10 warnings per step, so
the tenth one says how many more packages have gaps. Paths are relative to `GITHUB_WORKSPACE` (or the current
directory outside Actions). The line is found in `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`,
`Pipfile.lock` and requirements files (and files they include with `-r`); when wheelgap can't be sure which line a
package is on (a name and version written twice, or a `pyproject.toml` input), the annotation names the file without
a line.

### Watching a platform

!!! note "After 0.9.0"
    `--advisory` is not in 0.9.0.

To watch a platform before you support it, put `--advisory` in `args`:
`args: uv.lock -t linux-x86_64,win-arm64 --advisory win-arm64`. Its gaps show on the summary page, marked as
advisory, but don't fail the job. See [`--advisory`](cli.md#advisory).

### Your own workflow steps

Outside the Action, run wheelgap with `pipx run` (installed on GitHub's hosted runners) or `uvx`, and write the
Markdown report to the summary page:

```yaml
- run: |
    set -o pipefail
    pipx run --spec wheelgap==0.9.0 wheelgap uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --markdown | tee -a "$GITHUB_STEP_SUMMARY"
```

## pre-commit

```yaml
repos:
  - repo: https://github.com/wheelgap/wheelgap
    rev: v0.9.0
    hooks:
      - id: wheelgap
```

The hook runs when `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, or a `requirements*.txt` or
`requirements*.lock` file changes, and after 0.9.0 also `pixi.lock`. Put your targets in `[tool.wheelgap]` in
`pyproject.toml` so the hook checks the right platforms. Pass extra options with `args:`, for example
`args: [--fail-on, missing]`.

## GitLab CI

GitLab shows a JUnit report in the merge request. `--junit PATH` writes one alongside the usual output, with one
testsuite per target and a failed test per gap:

```yaml
wheelgap:
  image: ghcr.io/astral-sh/uv:python3.13-bookworm-slim
  rules:
    - changes:
        - uv.lock
        - pyproject.toml
  script:
    - uvx wheelgap@0.9.0 uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --junit wheelgap-junit.xml
  artifacts:
    when: always
    reports:
      junit: wheelgap-junit.xml
```

`rules: changes` runs the job only when the lockfile or `pyproject.toml` (where `[tool.wheelgap]` lives) changed: in
a merge request pipeline, compared with the target branch, and in a branch pipeline, compared with the previous push.
GitLab treats every file as changed in pipelines with nothing to compare against, such as a new branch's first push,
a tag, a scheduled pipeline or one started by hand, so the job runs there too. A
[scheduled pipeline](https://docs.gitlab.com/ci/pipelines/schedules/) therefore also catches a release that dropped a
wheel without any change on your side (or drop `rules` to check on every pipeline). Without
[`workflow: rules`](https://docs.gitlab.com/ci/yaml/workflow/), a push to a branch with an open merge request can
start both a branch and a merge request pipeline, each running the job; see
[avoid duplicate pipelines](https://docs.gitlab.com/ci/jobs/job_rules/#avoid-duplicate-pipelines).

`when: always` keeps the report when the job fails, which is when you want it. See [JUnit XML](output.md#junit-xml)
for how cells map to test results.

## Other CI systems

- **Jenkins, Azure Pipelines, CircleCI:** publish the `--junit PATH` file with their JUnit test report step.
- **Any CI:** the [exit code](output.md#exit-codes) fails the job; `--json` gives a machine-readable report and
  `--markdown` one you can post as a comment.
- **Monorepos and multi-arch images:** see the [recipes](recipes.md).
