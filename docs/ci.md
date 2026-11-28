# CI

Run wheelgap in CI so a dependency bump that drops a platform's wheels fails the pull request, not the release build.
Always pass `-p` and `-t` (or set them in [`[tool.wheelgap]`](configuration.md)): otherwise the CI runner's own
Python is checked.

## GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.14.0
  with:
    args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
```

The job fails when a dependency would build from source on a target you ship, for example when a dependency bump
drops ARM wheels. The gap table also goes to the run's summary page.

| input | default | meaning |
| --- | --- | --- |
| `args` | `""` | the arguments passed to wheelgap, as on the [command line](cli.md) |
| `summary` | `"true"` | also write the gap table to the job summary page (skipped when `args` include `--json`); `"false"` turns it off |
| `compare-to` | `""` | `base`, or a git ref such as `origin/main`, to fail only when the pull request makes the lockfile worse; see [below](#compare-to) |
| `comment` | `"false"` | with `compare-to`: also post one pull request comment when something got worse, and edit it on later runs |
| `fix` | `"true"` | with `compare-to` (**after 0.14.0**): say how to fix each gap that got worse, as [`--fix`](cli.md#fix) does; `"false"` looks nothing up; see [below](#pr-gate) |
| `github-token` | `${{ github.token }}` | the token for the pull request comment |
| `comment-author` | `""` | the login the token comments as, such as `my-app[bot]`, for a GitHub App's token |
| `dist` | `""` | a folder like `dist` with the release about to be uploaded, run as `--dist` instead of a lockfile check; see [below](#before-a-release) |

The Action also has a `report` output: with `compare-to`, the path of the Markdown report that went to the job
summary. **After 0.14.0**, a `comment` output is the path of the pull request comment's Markdown body, written whether
or not `comment: true` posts it.

The step's result is wheelgap's exit code: [`--fail-on missing`](cli.md#fail-on), [`--ignore`](cli.md#ignore) and
[`--exit-zero`](cli.md#exit-zero) in `args` work as they do on the command line.

### Only fail when a pull request makes things worse {#compare-to}

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
      - uses: wheelgap/wheelgap@v0.14.0
        with:
          args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64,musl-aarch64
          compare-to: base
          comment: true
```

- `args` must then start with the lockfile's path, followed only by what `wheelgap diff` takes: `-p`, `-t`,
  `--glibc`, `--musl`, `--macos`, `--distro`, `--ios` and `--android-api`; anything else exits 2. The step exits 1 when something
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
  `musl-aarch64`, `✓ → ● source`) and each new package's gaps. **After 0.14.0**, each row also says how to fix it
  ([below](#pr-gate)). The action's `report` output is the path of the same
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
- **Release suggestions.** With `fix: false` (**after 0.14.0**; before it, always), the comment also includes the [release suggestions](output.md#suggest) for packages that got worse:
  the nearest newer version that restores the wheels, or the last older version that had them. Lookups are limited to
  packages known to come from PyPI. If a lookup fails, the comment still reports the gap and the check's result is unchanged.
- **Permissions.** Commenting needs `pull-requests: write` for the token (`github-token`, by default the workflow's
  `GITHUB_TOKEN`). Under `pull_request`, pull requests from forks get a read-only token (unless a private repository's
  settings send write tokens to them), and Dependabot's pull requests get one by default too, unless the workflow's
  `permissions:` asks for more as above. When the comment can't be posted, the step warns (naming the missing permission
  on HTTP 403; **after 0.14.0**, a pull request from a fork gets a notice instead, since that's expected) and the job
  summary still has the report; the result is the same either way. So for Dependabot and fork
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
- uses: wheelgap/wheelgap@v0.14.0
  with:
    args: diff origin/${{ github.base_ref }}:uv.lock uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
    summary: "false"
```

### A low-noise pull request gate {#pr-gate}

**After 0.14.0.** With `compare-to: base` and `comment: true`, the Action is a pull request gate that stays quiet
until a pull request makes something worse, and then says how to fix it. That's mostly a dependency bump from
Renovate or Dependabot that drops a wheel the old version had, or a new package that would build from source:

```yaml
on: pull_request
permissions:
  contents: read
  pull-requests: write  # for the comment; without it, the job summary still has everything
jobs:
  wheels:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      # The fix input needs the release after 0.14.0: until then, pin a commit of main by its full SHA.
      - uses: wheelgap/wheelgap@v0.14.0
        with:
          args: uv.lock -p 3.13 -p 3.15 -t linux-x86_64,linux-aarch64,win-arm64
          compare-to: base
          comment: true
```

On each run of a pull request:

- **It fails only on what got worse.** The rule is [`wheelgap diff`'s](diff.md#what-counts-as-worse): a target whose
  status got worse (`✓ → ● source`), or a new package with a gap. Gaps the base branch already has don't fail it and
  aren't listed. A package that couldn't be checked (`unknown`) on both sides counts as worse when its version changed,
  since the new version was never checked. **After 0.14.0**, a target whose Python the lockfile's `requires-python`
  leaves out on either side isn't compared: the job summary lists it under "Not compared" with the reason, and it
  never fails the step. When only the pull request's lockfile leaves it out, the pull request dropped that Python:
  the summary leads with "This change drops Python 3.13 support" and the log gets a warning.
- **It says how to fix each one.** `fix: true` (the default) runs `wheelgap diff --fix`, which works out each listed
  gap's way out exactly as [`wheelgap uv.lock --fix`](cli.md#fix) would for the pull request's lockfile: an upgrade to
  the nearest newer official release with the wheel first (with what to run, and whether your own requirement allows
  it), else an unofficial wheel from wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/), else none and
  why. It looks only the listed packages up on PyPI, judging names as `--fix` does for the pull request's lockfile
  ([which names leave the run](how-it-works.md#pypi-origin)), and reads the gap wheel list in one request that names
  no package. A lookup that fails says so in the log and leaves the result alone; `fix: false` looks nothing up.
  The comment's older release hints ([above](#compare-to)) are left out when `fix` is on, since the Fix column says
  the same.
- **One comment, edited in place.** The first run that finds something worse posts one comment; every later run edits
  that comment, never posting another. When a later push fixes it, the comment is edited to say
  **Resolved.** When nothing got worse and there's no comment yet, nothing is posted.

For a pull request that adds `pyyaml==6.0.3` to a `uv.lock`, checked on Python 3.15 (from
`tests/fixtures/actioncompare` in this repository, with made-up commits), the comment reads:

> **wheelgap diff**
>
> Compared `action-compare-test/uv.lock` at `9f8e7d6` with the base branch (`1a2b3c4`). 1 new package has gaps.
>
> | Package | Version | Target | Status | Fix |
> | --- | --- | --- | --- | --- |
> | `pyyaml` | (new) `6.0.3` | `linux-x86_64` | (new) → ● source | upgrade to `6.0.4` (needs `pyproject.toml`'s `pyyaml==6.0.3` relaxed); if you can't, an unofficial gap wheel |
> | `pyyaml` | (new) `6.0.3` | `win-arm64` | (new) → ● source | upgrade to `6.0.4` (needs `pyproject.toml`'s `pyyaml==6.0.3` relaxed) |
>
> How to fix:
>
> - `In action-compare-test, run uv lock --upgrade-package pyyaml, then uv sync: uv moves each to the newest release your constraints allow, so check that it's at least pyyaml==6.0.4, then run wheelgap again.`
> - wheelgap doesn't resolve dependencies: the rest of them must allow these releases.
> - Gap wheels are unofficial: built by wheelgap from the PyPI sdist, not by the package's maintainers ([how to use them](recipes.md#installing-from-the-gap-wheel-index)).

The job summary has the same report, so a run that can't comment still shows it.

**Package names are attacker-controlled.** A pull request can put any text in its lockfile, so everything the comment
quotes from it (names, versions, paths, the fix's commands) is escaped into `<code>` and can't add links, @mentions,
images or Markdown; the [Action compare-to](https://github.com/wheelgap/wheelgap/blob/main/.github/workflows/action-compare.yml)
workflow runs the gate on a fixture pull request and checks the comment body.

**Permissions.** The comment needs `pull-requests: write`; `contents: read` is enough for everything else. The Action
never runs the pull request's code: it reads the two lockfiles with wheelgap and git.

**Pull requests from forks.** Under `pull_request`, GitHub gives a fork's run a read-only token, so it can't comment.
The step then writes a notice (not a warning) and the job summary has the report and the fixes; the result is the same.
Use that, and don't move the gate to `pull_request_target`, or to a `workflow_run` job that comments on the fork's
behalf: those run with a write token, a fork's `pull_request` run can change the workflow and so anything it uploads
for that job to post, and the pull request number it would comment on would come from that upload too. So the Action
refuses `comment: true` on `pull_request_target` and `workflow_run`, and the summary is the safe place for a fork's
report. Dependabot's pull requests get a read-only token too unless the workflow's `permissions:` asks for
`pull-requests: write`, as above; Renovate's come from a branch of the repository, so they can comment.

The `comment` output is the path of the comment's Markdown body, written on every run, if you want to post it some
other way.

### Annotations {#annotations}

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

With `--deps` on a requirements file, a direct requirement's gap is on its own line. A transitive gap is on the line of
the requirement that pulls it in when exactly one does on every target where it has a gap, and the message names it:
`cryptography 50.0.2 builds from source on win-arm64 (via mcp).` When more than one requirement pulls it in, or a line
isn't certain, the annotation names the file without a line and the message names them all. See
[GitHub annotations](cli.md#github-annotations) for the full rules.

With [`--fix`](cli.md#fix) in `args`, the annotation of a package that a newer official release
fixes names that release first, and the annotation of a package that a gap wheel fills gets one more sentence, naming
the targets it fills (as the fallback, "If you can't upgrade", when there's an upgrade too); see [below](#fix).

With `--dockerfile-inputs`, each annotation ends with `(installed by Dockerfile:4)`. When the package
is in a file included with `-r` (at any depth), it also names the file on the `RUN` line it comes through, relative to
the build context: `(installed by Dockerfile:4, through requirements.txt)`, or `through constraints.txt` for a `-r`
inside the line's `-c` file.

### Fixing a gap with a gap wheel {#fix}

Put [`--fix`](cli.md#fix) in `args` to see which gaps a wheel on wheelgap's
[gap wheel index](https://wheelgap.github.io/wheels/) fills:

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.14.0
  with:
    args: uv.lock -p 3.14 -t win-arm64 --fix
```

The gap wheel section, with how to install from the index, goes in the log and the job summary, and the fix goes in
the annotation on the package's line: `cryptography 46.0.3 builds from source on win-arm64. A gap wheel on wheelgap's
gap wheel index fills win-arm64 (unofficial, built by wheelgap from the PyPI sdist); see the gap wheel section of the
log for how to use it.` For a requirements file, the annotation gives the `pip install --extra-index-url` command
instead. Gap wheels are unofficial: built by wheelgap from the PyPI sdist, not by the package's maintainers. The
check's result doesn't change: the annotation's level and the step's exit code are the same with and without `--fix`.

The list of gap wheels is read from wheelgap.github.io in one request that names no package. With `--index-url` or a
`WHEELGAP_INDEX_URL` mirror, `--fix` is off and reads nothing. See
[the recipes](recipes.md#installing-from-the-gap-wheel-index) for how to install from the index.

### Watching a platform

To watch a platform before you support it, put `--advisory` in `args`:
`args: uv.lock -t linux-x86_64,win-arm64 --advisory win-arm64`. Its gaps show on the summary page, marked as
advisory, but don't fail the job. See [`--advisory`](cli.md#advisory).

### Your own workflow steps

Outside the Action, run wheelgap with `pipx run` (installed on GitHub's hosted runners) or `uvx`, and write the
Markdown report to the summary page:

```yaml
- run: |
    set -o pipefail
    pipx run --spec wheelgap==0.14.0 wheelgap uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --markdown | tee -a "$GITHUB_STEP_SUMMARY"
```

## pre-commit

```yaml
repos:
  - repo: https://github.com/wheelgap/wheelgap
    rev: v0.14.0
    hooks:
      - id: wheelgap
```

The hook runs when `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, `pixi.lock`, a
`requirements*.txt` or `requirements*.lock` file changes, and when `pyproject.toml` (its `[tool.wheelgap]` and
`[tool.uv]` settings change the answer), `uv.toml` or `.python-version` does. Put your targets in `[tool.wheelgap]` in
`pyproject.toml` so the hook checks the right platforms. Pass extra options with `args:`, for example
`args: [--fail-on, missing]`.

## Code scanning {#code-scanning}

`--sarif PATH` writes a [SARIF](output.md#sarif) report that
[upload-sarif](https://docs.github.com/en/code-security/code-scanning/integrating-with-code-scanning/uploading-a-sarif-file-to-github)
turns into code scanning alerts: listed in the Security tab, and shown on the lockfile line in pull requests that add
a gap.

```yaml
jobs:
  wheelgap:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uvx wheelgap@latest uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --sarif wheelgap.sarif
      - uses: github/codeql-action/upload-sarif@v4
        if: always()
        with:
          sarif_file: wheelgap.sarif
          category: wheelgap
```

`if: always()` uploads the report when wheelgap fails the job, which is when it has alerts. Code scanning on a private
repository needs GitHub Advanced Security (GitHub Code Security).

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
    - uvx wheelgap@0.14.0 uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --junit wheelgap-junit.xml
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

For findings on the lockfile's lines in GitLab merge requests, publish a Code Quality report instead (or alongside
JUnit):

```yaml
wheelgap:
  image: ghcr.io/astral-sh/uv:python3.13-bookworm-slim
  script:
    - uvx wheelgap@latest uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --gitlab-code-quality gl-code-quality-report.json
  artifacts:
    when: always
    reports:
      codequality: gl-code-quality-report.json
```

GitLab shows the findings in the merge request widget on every tier, inline in the merge request's changes view on
Ultimate, and in the pipeline's Code Quality tab on Premium and above. Run the job on the target branch too: GitLab
compares its report with the merge request's to identify new and resolved findings. See
[GitLab Code Quality](output.md#gitlab-code-quality) for severities and location limits.

## Before a release {#before-a-release}

The other place a dropped platform gets caught is right before upload. After `python -m build` or `uv build`,
[`--dist`](cli.md#dist) checks the `dist/` folder itself against the last release on PyPI and fails
when a target it had a wheel for got no wheel this time — the regression cryptography shipped when 46.0.4 lost the
Windows on ARM wheels that every release since 46.0.0 had:

```yaml
- name: Build
  run: uv build
- uses: wheelgap/wheelgap@v0.14.0
  with:
    dist: dist
    args: -p 3.13 -t win-arm64,linux-aarch64,musl-aarch64
- name: Publish
  run: uv publish  # or twine upload dist/*
```

`dist` runs `--dist` on the folder (no lockfile in `args`), writes the table to the job summary and fails the job
on a drop, so the upload step never runs. `uvx wheelgap@latest --dist dist -p 3.13 -t ...` is the same check
outside GitHub Actions.

A project tag like `cp313` still checks every `-t` platform, so pass the targets you ship to, not just the
interpreter you built with. Missing-wheel targets land as `source` (the sdist builds) or `missing` (no sdist); the
dropped-target lines are what makes a release pipeline fail.

Moving to a Rust extension? See [Rust extensions](rust-extensions.md) for the wheel matrix to build before this check.

## Other CI systems

- **Jenkins, Azure Pipelines, CircleCI:** publish the `--junit PATH` file with their JUnit test report step.
- **Any CI:** the [exit code](output.md#exit-codes) fails the job; `--json` gives a machine-readable report and
  `--markdown` one you can post as a comment.
- **Monorepos and multi-arch images:** see the [recipes](recipes.md).


For using the gap wheel index with pip, CI, Docker, or pip configuration, see [the recipes](recipes.md#installing-from-the-gap-wheel-index).
