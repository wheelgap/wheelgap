"""scripts/check_projects.py, offline: how it reads inputs and the referees' output (a fake uv and pip, as
tests/test_check_against_pip.py fakes pip), and how it sorts each cell into agree, disagree or not compared."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wheelgap import make_targets


def _script():
    path = Path(__file__).parents[1] / "scripts" / "check_projects.py"
    spec = importlib.util.spec_from_file_location("wheelgap_check_projects", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()

MAC = make_targets("3.13", "macos-arm64")[0]
LINUX = make_targets("3.13", "linux-x86_64")[0]
WIN = make_targets("3.13", "win-amd64")[0]
WIN_ARM = make_targets("3.13", "win-arm64")[0]
PYPI = "https://pypi.org/simple"
CPU = "https://pt.test/cpu"

# A lock like nanochat's: torch from PyPI by default, from the CPU index with the `cpu` extra (2.9.1 on macOS,
# 2.9.1+cpu elsewhere), with uv's conflict markers, and a Windows dependency on platform_machine.
LOCK = """\
version = 1
revision = 3
requires-python = ">=3.10"
conflicts = [[
    { package = "a", extra = "cpu" },
    { package = "a", extra = "gpu" },
]]

[[package]]
name = "a"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "torch", version = "2.9.1", source = { registry = "https://pypi.org/simple" }, marker = "extra != 'x'" },
    { name = "winthing", marker = "platform_machine == 'AMD64'" },
    { name = "gitpkg" },
    { name = "appnope", marker = "sys_platform == 'darwin' or (extra == 'extra-1-a-cpu' and extra == 'x')" },
]

[package.optional-dependencies]
cpu = [
    { name = "torch", version = "2.9.1", source = { registry = "https://pt.test/cpu" }, marker = "sys_platform == 'darwin'" },
    { name = "torch", version = "2.9.1+cpu", source = { registry = "https://pt.test/cpu" }, marker = "sys_platform != 'darwin'" },
]

[[package]]
name = "torch"
version = "2.9.1"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/t/torch-2.9.1-cp313-cp313-manylinux_2_28_x86_64.whl" },
    { url = "https://files.pythonhosted.org/t/torch-2.9.1-cp313-none-macosx_11_0_arm64.whl" },
]

[[package]]
name = "torch"
version = "2.9.1"
source = { registry = "https://pt.test/cpu" }
resolution-markers = ["sys_platform == 'darwin'"]
wheels = [{ url = "https://pt.test/cpu/torch-2.9.1-cp313-none-macosx_11_0_arm64.whl" }]

[[package]]
name = "torch"
version = "2.9.1+cpu"
source = { registry = "https://pt.test/cpu" }
wheels = [{ url = "https://pt.test/cpu/torch-2.9.1%2Bcpu-cp313-cp313-manylinux_2_28_x86_64.whl" }]

[[package]]
name = "winthing"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "below" }]
wheels = [{ url = "https://files.pythonhosted.org/w/winthing-1.0-py3-none-any.whl" }]

[[package]]
name = "below"
version = "2.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/b/below-2.0.tar.gz" }

[[package]]
name = "appnope"
version = "0.1.4"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/a/appnope-0.1.4-py3-none-any.whl" }]

[[package]]
name = "gitpkg"
version = "1.0"
source = { git = "https://github.com/example/gitpkg?rev=abc#abc" }

[[package]]
name = "gitpkg"
version = "0.9"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/g/gitpkg-0.9-py3-none-any.whl" }]
"""  # noqa: E501 (uv writes each dependency on one line)


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / "uv.lock"
    path.write_text(LOCK, encoding="utf-8")
    return script.read_lock(path)


# --- the manifest -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "pinned"),
    [
        ("numpy==2.3.4\nrequests==2.32.5 ; python_version >= '3.10'\n", True),
        ("# comment\n\nnumpy==2.3.4  # trailing\n", True),
        ("numpy==2.3.4 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n", True),
        ("--index-url https://example.test/simple\nnumpy==2.3.4\n", True),
        ("numpy>=2\n", False),
        ("numpy\n", False),
        ("numpy==2.3.4\n-e .\nflask\n", False),
        ("# nothing\n", False),
    ],
)
def test_fully_pinned(text, pinned):
    assert script.fully_pinned(text) is pinned


def test_make_manifest_takes_root_inputs(tmp_path):
    repos = tmp_path / "repos"
    (repos / "a__lock").mkdir(parents=True)
    (repos / "a__lock" / "uv.lock").write_text(LOCK, encoding="utf-8")
    (repos / "a__lock" / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    (repos / "b__reqs").mkdir()
    (repos / "b__reqs" / "requirements.txt").write_text("-r base.txt\nsix==1.17.0\n", encoding="utf-8")
    (repos / "b__reqs" / "base.txt").write_text("idna==3.10\n", encoding="utf-8")
    (repos / "c__loose").mkdir()
    (repos / "c__loose" / "requirements.txt").write_text("six\n", encoding="utf-8")
    (repos / "d__poetry").mkdir()
    (repos / "d__poetry" / "poetry.lock").write_text("", encoding="utf-8")
    corpus = [{"full_name": n, "sha": "0" * 40} for n in ("a/lock", "b/reqs", "c/loose", "d/poetry", "e/gone")]
    (tmp_path / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    inputs = script.make_manifest(tmp_path)["inputs"]
    assert [(e["id"], e["kind"], e["files"]) for e in inputs] == [
        ("a/lock", "uv.lock", ["uv.lock", "pyproject.toml"]),
        ("b/reqs", "requirements", ["requirements.txt", "base.txt"]),
        ("d/poetry", "poetry.lock", ["poetry.lock"]),
    ]
    assert inputs[0]["commit"] == "0" * 40


def test_stage_copies_only_the_inputs_files(tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "uv.lock").write_text(LOCK, encoding="utf-8")
    (src / "setup.py").write_text("raise SystemExit", encoding="utf-8")
    entry = {"id": "x/y", "kind": "uv.lock", "dir": str(src), "main": "uv.lock", "files": ["uv.lock"]}
    folder = script.stage(entry, tmp_path / "work")
    assert sorted(p.name for p in folder.iterdir()) == ["uv.lock"]
    assert folder.name == script.input_hash(entry)


def test_check_workdir_refuses_a_project_above(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="above the work folder"):
        script.check_workdir(tmp_path / "out" / "work")


# --- reading a uv.lock --------------------------------------------------------------------------------------


def test_read_lock_sources_duplicates_and_collisions(lock):
    assert lock.duplicates == {("torch", "2.9.1")}
    assert lock.collisions == {"gitpkg"}
    assert lock.skipped_names == {"gitpkg"}
    assert lock.source_of("torch", "2.9.1+cpu") == CPU
    assert lock.source_of("torch", "2.9.1") is None  # two indexes: the files decide
    assert lock.source_of("torch", "2.9.1", ["torch-2.9.1-cp313-none-macosx_11_0_arm64.whl"]) == CPU
    pypi_files = ["torch-2.9.1-cp313-cp313-manylinux_2_28_x86_64.whl", "torch-2.9.1-cp313-none-macosx_11_0_arm64.whl"]
    assert lock.source_of("Torch", "2.9.1", pypi_files) == PYPI


def test_emulated_follows_platform_machine_on_windows_x86_64_only(lock):
    # winthing's edge turns on platform_machine == 'AMD64', which uv's x86_64-pc-windows-msvc says is x86_64;
    # below is under it. appnope's conflict clause names extras, not an emulated marker.
    # winthing's parent is the project itself, which uv walks through without installing it.
    assert lock.emulated(WIN, installed=set()) == {"winthing", "below"}
    assert lock.emulated(WIN_ARM, installed=set()) == set()
    assert lock.emulated(LINUX, installed=set()) == set()


# ai-agent-book's shape: browser-use -> pyobjc only through `platform_system == 'darwin'` (never true: CPython says
# "Darwin") or a clause that needs both sides of a conflict, and pyobjc's framework edges on platform_release.
GATED_LOCK = """\
version = 1
revision = 3
requires-python = ">=3.10"
conflicts = [[{ package = "a", extra = "x" }, { package = "a", extra = "y" }]]

[[package]]
name = "a"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "gate", marker = "platform_system == 'darwin' or (extra == 'extra-1-a-x' and extra == 'extra-1-a-y')" }]

[[package]]
name = "gate"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "child", marker = "platform_release >= '20.0'" }]
wheels = [{ url = "https://files.pythonhosted.org/g/gate-1.0-py3-none-any.whl" }]

[[package]]
name = "child"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/c/child-1.0-py3-none-any.whl" }]
"""  # noqa: E501


def test_emulation_needs_an_installed_parent(tmp_path):
    path = tmp_path / "uv.lock"
    path.write_text(GATED_LOCK, encoding="utf-8")
    lock = script.read_lock(path)
    # uv installs neither gate nor child on Linux: child's platform_release edge can't explain that, so it counts.
    assert lock.emulated(LINUX, installed=set()) == set()
    assert lock.emulated(LINUX, installed={"gate"}) == {"child"}
    wheelgap = {
        "rows": {"gate==1.0 @ https://pypi.org/simple": "wheel", "child==1.0 @ https://pypi.org/simple": "wheel"}
    }
    cells = script.compare(
        entry=ENTRY, referee="uv sync", mode="all", target=LINUX, wheelgap=wheelgap, answer={"status": {}}, lock=lock
    )
    assert {c.package.split("==")[0]: c.verdict for c in cells} == {"child": "disagree", "gate": "disagree"}
    # When uv installs gate, child's answer turns on the emulated platform_release: not compared, even if it agrees.
    answer = {
        "status": {"gate==1.0 @ https://pypi.org/simple": "wheel", "child==1.0 @ https://pypi.org/simple": "wheel"}
    }
    cells = script.compare(
        entry=ENTRY, referee="uv sync", mode="all", target=LINUX, wheelgap=wheelgap, answer=answer, lock=lock
    )
    assert {c.package.split("==")[0]: c.verdict for c in cells} == {"child": "not compared", "gate": "agree"}


# cognee's shape: the project needs `shared` itself, and `forked` (whose fork markers turn on python_full_version)
# needs it too. `shared` installs whatever uv emulates; `below`, reached only through `forked`, may not.
SOLID_LOCK = """\
version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "a"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "shared" }, { name = "forked" }]

[[package]]
name = "forked"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["python_full_version >= '3.13.1'", "python_full_version < '3.13.1'"]
dependencies = [{ name = "shared" }, { name = "below" }]

[[package]]
name = "forked"
version = "0.9"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["python_full_version < '3.13.1'"]

[[package]]
name = "shared"
version = "1.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "below"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_emulation_leaves_out_what_the_project_always_installs(tmp_path):
    path = tmp_path / "uv.lock"
    path.write_text(SOLID_LOCK, encoding="utf-8")
    lock = script.read_lock(path)
    assert lock.solid == {"a", "shared"}
    assert lock.emulated(LINUX, installed={"forked", "shared", "below"}) == {"forked", "below"}


@pytest.mark.parametrize(
    ("marker", "target", "depends"),
    [
        ("platform_machine == 'AMD64'", WIN, True),
        ("platform_machine == 'AMD64'", LINUX, False),
        # On Windows sys_platform decides it whatever the machine is, for every choice of the extras.
        (
            "(platform_machine == 'x86_64' and sys_platform == 'linux') or (extra == 'extra-1-a' and extra == 'x')",
            WIN,
            False,
        ),
        ("platform_machine == 'x86_64' or extra == 'extra-1-a'", WIN, True),
        ("platform_release >= '6'", LINUX, True),
        # cognee's lock: uv's empty platform_version is never a macOS 13 kernel; a real one can be.
        ("'Darwin Kernel Version 22.' in platform_version and sys_platform == 'darwin'", MAC, True),
        ("'Darwin Kernel Version 22.' in platform_version and sys_platform == 'darwin'", LINUX, False),
        ("python_full_version >= '3.13.2'", LINUX, True),
        ("python_full_version >= '3.13'", LINUX, False),
        ("python_full_version == '3.13.*' and sys_platform == 'darwin'", MAC, False),
        ("sys_platform == 'win32'", WIN, False),
    ],
)
def test_depends_on_emulation(marker, target, depends):
    assert script.depends_on_emulation(marker, target, script.emulated_markers(target)) is depends


@pytest.mark.parametrize(
    ("conflicts", "expected"),
    [
        ([], [[]]),
        (
            [[{"package": "a", "extra": "cpu"}, {"package": "a", "extra": "gpu"}]],
            [
                ["--no-extra", "cpu", "--no-extra", "gpu"],
                ["--no-extra", "gpu"],
                ["--no-extra", "cpu"],
            ],
        ),
        (
            [[{"package": "a", "group": "x"}, {"package": "a", "extra": "y"}]],
            [["--no-extra", "y", "--no-group", "x"], ["--no-extra", "y"], ["--no-group", "x"]],
        ),
    ],
)
def test_conflict_selections(conflicts, expected):
    assert script.conflict_selections(conflicts) == expected


def test_conflict_selections_refuses_package_conflicts_and_too_many():
    assert "between packages" in script.conflict_selections([[{"package": "a"}, {"package": "b"}]])
    many = [[{"package": "a", "extra": f"e{i}{j}"} for j in range(3)] for i in range(4)]  # 4**4 = 256 ways
    assert "more than 64" in script.conflict_selections(many)


# --- reading uv's output ------------------------------------------------------------------------------------

NO_BUILD = (
    "error: Distribution `below==2.0 @ registry+https://pypi.org/simple` can't be installed because it is marked as "
    "`--no-build` but has no binary distribution"
)
NEITHER = (
    "error: Distribution `torch==2.9.1+cpu @ registry+https://pt.test/cpu` can't be installed "
    "because it doesn't have a source distribution or wheel for the current platform\n\nhint: You're on macOS"
)

NO_BINARY_TORCH = (
    "error: Distribution `torch==2.9.1 @ registry+https://pt.test/cpu` can't be installed because it "
    "is marked as `--no-binary` but has no source distribution"
)


def test_parse_installed_and_errors():
    out = "Would download 2 packages\nWould install 2 packages\n + Torch==2.9.1\n + below==2.0\n"
    assert script.parse_installed(out) == [("torch", "2.9.1"), ("below", "2.0")]
    assert script.parse_dist_error(NO_BUILD) == script.DistError("below", "2.0", PYPI, script.NO_BUILD)
    assert script.parse_dist_error(NEITHER) == script.DistError("torch", "2.9.1+cpu", CPU, script.NEITHER)
    assert script.parse_dist_error("error: No solution found") is None


@pytest.mark.parametrize(
    ("output", "why"),
    [
        (
            "error: The current Python platform is not compatible with the lockfile's supported environments: "
            "`sys_platform == 'darwin'`",
            "environments",
        ),
        (
            "error: The requested interpreter resolved to Python 3.15.0, which is incompatible with the project's "
            "Python requirement: `>=3.12, <3.14`",
            "requires-python",
        ),
        (
            "error: Required uv version `>=0.13.7` does not match the running version `0.13.1`. Update `uv` by running "
            "`uv self update`.",
            "uv-version",
        ),
        ("error: Extras `cpu` and `gpu` are incompatible with the declared conflicts", None),
    ],
)
def test_refusal(output, why):
    assert script.refusal(output) == why


class FakeUv:
    """Answers `uv sync` from a table: {frozenset of flags that must be present: output}, first match wins, and a
    default plan. Records each command."""

    def __init__(self, plans):
        self.plans = plans
        self.calls = []

    def __call__(self, cmd, cwd, env):
        self.calls.append(cmd)
        args = set(cmd)
        excluded = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--no-install-package"}
        for need, avoid, code, out in self.plans:
            if need <= args and not (avoid & excluded):
                return code, out
        raise AssertionError(f"unexpected command {cmd}")


def test_sync_selection_two_passes(lock, tmp_path):
    installed = " + appnope==0.1.4\n + torch==2.9.1\n + winthing==1.0\n"
    fake = FakeUv(
        [
            # Pass 1: below has no wheel; with it left out, the rest are wheels.
            ({"--no-build", "--no-binary-package"}, set(), 2, NO_BINARY_TORCH),
            ({"--no-build"}, {"below"}, 2, NO_BUILD),
            ({"--no-build"}, set(), 0, "Would install 3 packages\n" + installed),
            # Pass 2, without --no-build: below is planned from its sdist.
            (set(), set(), 0, "Would install 4 packages\n" + installed + " + below==2.0\n"),
        ]
    )
    selection = script.Selection("default", [])
    script.sync_selection(fake, tmp_path, {}, ["uv", "sync"], selection, lock)
    assert selection.error is None
    assert selection.status == {
        "appnope==0.1.4 @ https://pypi.org/simple": "wheel",
        "torch==2.9.1 @ https://pt.test/cpu": "wheel",  # named by the --no-binary-package run
        "winthing==1.0 @ https://pypi.org/simple": "wheel",
        "below==2.0 @ https://pypi.org/simple": "source",
    }
    # gitpkg is always left out; below only in pass 1.
    assert all(
        ["--no-install-package", "gitpkg"] == c[c.index("gitpkg") - 1 : c.index("gitpkg") + 1] for c in fake.calls
    )
    assert "--no-build" not in fake.calls[2] and "below" not in fake.calls[2]


def test_sync_selection_missing_and_refusal(lock, tmp_path):
    fake = FakeUv(
        [
            (
                {"--no-build"},
                {"torch"},
                2,
                NO_BUILD.replace("below==2.0 @ registry+https://pypi.org/simple", f"torch==2.9.1+cpu @ registry+{CPU}"),
            ),
            ({"--no-build"}, set(), 0, " + appnope==0.1.4\n"),
            (set(), {"torch"}, 2, NEITHER),
            (set(), set(), 0, " + appnope==0.1.4\n"),
        ]
    )
    selection = script.sync_selection(fake, tmp_path, {}, ["uv", "sync"], script.Selection("all", []), lock)
    assert selection.status == {
        "appnope==0.1.4 @ https://pypi.org/simple": "wheel",
        f"torch==2.9.1+cpu @ {CPU}": "missing",
    }

    refused = FakeUv(
        [
            (
                {"--no-build"},
                set(),
                2,
                "error: The current Python platform is not compatible with the lockfile's supported environments: `x`",
            )
        ]
    )
    selection = script.sync_selection(refused, tmp_path, {}, ["uv", "sync"], script.Selection("all", []), lock)
    assert selection.refused == "environments" and selection.status == {}


def test_sync_selection_refuses_a_build(lock, tmp_path):
    fake = FakeUv([({"--no-build"}, set(), 0, "Building below==2.0\n + below==2.0\n")])
    with pytest.raises(script.RefereeError, match="built a package"):
        script.sync_selection(fake, tmp_path, {}, ["uv", "sync"], script.Selection("all", []), lock)


def test_uv_sync_referee_runs_default_and_each_conflict_choice(lock, tmp_path, monkeypatch):
    fake = FakeUv([({"python"}, set(), 0, "/py/python3.13\n"), (set(), set(), 0, " + appnope==0.1.4\n")])
    answer = script.uv_sync_referee(fake, tmp_path, MAC, lock, tmp_path)
    assert [s["label"] for s in answer["selections"]] == [
        "default",
        "all --no-extra cpu --no-extra gpu",
        "all --no-extra gpu",
        "all --no-extra cpu",
    ]
    first = fake.calls[0]
    assert first[:2] == ["uv", "sync"] and "--frozen" in first and "--dry-run" in first and "--offline" in first
    assert first[first.index("--python-platform") + 1] == "aarch64-apple-darwin"
    assert {"--no-install-project", "--no-install-workspace", "--no-install-local"} <= set(first)
    assert first[first.index("--python-preference") + 1] == "system"  # a project's only-managed doesn't apply
    assert answer["selections"][1]["command"].startswith("MACOSX_DEPLOYMENT_TARGET=14.0 uv sync")


def test_uv_env_isolates_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_INDEX_URL", "https://example.test/simple")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere")
    env = script.uv_env(MAC, tmp_path)
    assert "UV_INDEX_URL" not in env and "VIRTUAL_ENV" not in env
    assert env["XDG_CONFIG_HOME"] == str(tmp_path) and env["UV_NO_WRAP"] == "1"
    assert env["MACOSX_DEPLOYMENT_TARGET"] == "14.0"


@pytest.mark.parametrize(
    ("requires_python", "pythons"),
    [
        (">=3.10", []),
        ("==3.11.*", ["3.11"]),
        (">=3.12.3, <3.13", ["3.12"]),
        (">=3.9, <3.13", ["3.12"]),
        ("==3.8.*", []),
        ("not a specifier", []),
    ],
)
def test_own_targets(requires_python, pythons):
    targets = script.default_targets()
    got = script.own_targets(requires_python, targets)
    assert sorted({f"{t.major}.{t.minor}" for t in got}) == pythons
    if pythons:
        assert [t.platform for t in got] == list(script.PLATFORMS)


# --- comparing ------------------------------------------------------------------------------------------------

ENTRY = {"id": "o/p", "kind": "uv.lock"}


def _verdicts(cells):
    return {(c.package, c.wheelgap, c.answer): (c.verdict, c.why) for c in cells}


def test_union_merges_the_conflict_choices():
    answer = {
        "selections": [
            {"label": "default", "status": {"a==1": "wheel"}, "error": None, "refused": None, "command": "d"},
            {
                "label": "all x",
                "status": {"a==1": "wheel", "b==1": "missing"},
                "error": None,
                "refused": None,
                "command": "x",
            },
            {"label": "all y", "status": {"c==1": "source"}, "error": None, "refused": None, "command": "y"},
        ]
    }
    merged, default_keys, default = script.union(answer)
    assert merged["status"] == {"a==1": "wheel", "b==1": "missing", "c==1": "source"}
    assert merged["commands"] == {"a==1": "x", "b==1": "x", "c==1": "y"}
    assert default_keys == {"a==1"} and default["status"] == {"a==1": "wheel"}


def test_compare_cells(lock):
    wheelgap = {
        "rows": {
            "appnope==0.1.4 @ https://pypi.org/simple": "wheel",  # uv never installs it on Windows
            "torch==2.9.1 @ https://pypi.org/simple": "wheel",
            "below==2.0 @ https://pypi.org/simple": "wheel",  # under winthing: emulated
            "gitpkg==0.9 @ https://pypi.org/simple": "wheel",  # a name with a git entry too
            "odd==1 @ https://pypi.org/simple": "unknown",
        },
        "ambiguous": [],
    }
    answer = {
        "status": {
            "torch==2.9.1 @ https://pypi.org/simple": "source",
            "torch==2.9.1+cpu @ https://pt.test/cpu": "wheel",
        }
    }
    got = _verdicts(
        script.compare(
            entry=ENTRY, referee="uv sync", mode="all", target=WIN, wheelgap=wheelgap, answer=answer, lock=lock
        )
    )
    assert got[("appnope==0.1.4 @ https://pypi.org/simple", "wheel", "absent")] == (
        "disagree",
        "wheelgap lists a package the referee doesn't install",
    )
    assert got[("torch==2.9.1 @ https://pypi.org/simple", "wheel", "source")] == (
        "disagree",
        "wheelgap says wheel, the referee source",
    )
    assert got[("torch==2.9.1+cpu @ https://pt.test/cpu", "absent", "wheel")][0] == "disagree"
    assert got[("below==2.0 @ https://pypi.org/simple", "wheel", "absent")][0] == "not compared"
    assert "AMD64" in got[("below==2.0 @ https://pypi.org/simple", "wheel", "absent")][1]
    assert "git" in got[("gitpkg==0.9 @ https://pypi.org/simple", "wheel", "absent")][1]
    assert got[("odd==1 @ https://pypi.org/simple", "unknown", "absent")] == ("not compared", "wheelgap says unknown")


@pytest.mark.parametrize(
    ("wheelgap", "answer", "verdict"),
    [
        ({"refused": "uv.lock: locked for ..."}, {"refused": "environments"}, "agree"),
        ({"rows": {}}, {"refused": "requires-python"}, "not compared"),
        ({"rows": {}}, {"refused": "environments"}, "disagree"),
        ({"refused": "x"}, {"status": {}}, "disagree"),
        ({"rows": {}}, {"error": "uv: boom"}, "not compared"),
        ({"rows": {}}, {"refused": "uv-version"}, "not compared"),
        ({"refused": "x"}, {"refused": "requires-python"}, "agree"),
    ],
)
def test_compare_target_level(wheelgap, answer, verdict, lock):
    cells = script.compare(
        entry=ENTRY, referee="uv sync", mode="all", target=MAC, wheelgap=wheelgap, answer=answer, lock=lock
    )
    assert [(c.package, c.verdict) for c in cells] == [("*", verdict)]


def test_compare_pip_source_needs_the_sdists_requires_python():
    # pip --only-binary skips an sdist before reading its Requires-Python, so "source" there may be a refusal.
    from wheelgap import Result

    result = Result("m", "3.1.0", requires_python_rejected={"m-3.1.0.tar.gz": "Requires-Python <3.15"})
    wheelgap = {"rows": {"m==3.1.0": "missing", "n==1": "missing"}, "results": {"m==3.1.0": result}}
    answer = {"status": {"m==3.1.0": "source", "n==1": "source"}}
    got = _verdicts(
        script.compare(entry=ENTRY, referee="pip", mode="all", target=LINUX, wheelgap=wheelgap, answer=answer)
    )
    assert got[("m==3.1.0", "missing", "source")][0] == "not compared"
    assert "Requires-Python <3.15" in got[("m==3.1.0", "missing", "source")][1]
    assert got[("n==1", "missing", "source")][0] == "disagree"
    # Nor does it agree: "source" is no evidence when the sdist's Requires-Python leaves the target out.
    wheelgap = {"rows": {"m==3.1.0": "source"}, "results": {"m==3.1.0": result}}
    got = _verdicts(
        script.compare(entry=ENTRY, referee="pip", mode="all", target=LINUX, wheelgap=wheelgap, answer=answer)
    )
    assert got[("m==3.1.0", "source", "source")][0] == "not compared"


def test_compare_uv_pip_compile_is_wheel_or_not():
    wheelgap = {"rows": {"a==1": "source", "b==1": "missing", "c==1": "wheel", "d==1": "wheel"}}
    answer = {"status": {"a==1": "no wheel", "b==1": "no wheel", "c==1": "no wheel"}}
    got = _verdicts(
        script.compare(
            entry=ENTRY, referee="uv pip compile", mode="all", target=LINUX, wheelgap=wheelgap, answer=answer
        )
    )
    assert got[("a==1", "source", "no wheel")][0] == "agree"
    assert got[("b==1", "missing", "no wheel")][0] == "agree"
    assert got[("c==1", "wheel", "no wheel")][0] == "disagree"
    assert got[("d==1", "wheel", "absent")][0] == "disagree"


def test_totals_and_exit_code(capsys):
    cells = [
        script.Cell("o/p", "uv.lock", "uv sync", "all", "3.13 linux-x86_64", "a==1", "wheel", "wheel", "agree"),
        script.Cell(
            "o/p", "uv.lock", "uv sync", "all", "3.13 linux-x86_64", "b==1", "wheel", "absent", "disagree", "x"
        ),
        script.Cell(
            "o/p", "uv.lock", "uv sync", "all", "3.13 linux-x86_64", "c==1", "unknown", "absent", "not compared", "u"
        ),
    ]
    assert script.totals(cells) == {
        "cells": 3,
        "compared": 2,
        "agree": 1,
        "disagree": 1,
        "not_compared": 1,
        "agreement": 0.5,
    }
    versions = {"uv": "uv 0.13.1", "pip": "pip 26.3", "wheelgap": "0", "wheelgap_commit": "abc"}
    assert script.report(cells, versions, 1.0) == 1
    out = capsys.readouterr().out
    assert "3 cells: 2 compared, 1 agree, 1 disagree (50.00% agree), 1 not compared" in out
    assert script.report(cells[:1], versions, 1.0) == 0


def test_report_keeps_target_level_cells_out_of_the_rate(capsys):
    cells = [
        script.Cell("o/p", "uv.lock", "uv sync", "all", "3.13 linux-x86_64", "a==1", "wheel", "wheel", "agree"),
        script.Cell(
            "o/p", "uv.lock", "uv sync", "all", "3.15 linux-x86_64", "*", "refused", "refused", "agree", "both"
        ),
        script.Cell(
            "o/q", "uv.lock", "uv sync", "all", "3.13 linux-x86_64", "*", "-", "refused", "not compared", "old uv"
        ),
    ]
    versions = {"uv": "uv", "pip": "pip", "wheelgap": "0", "wheelgap_commit": "abc", "checkout_head": "def"}
    assert script.report(cells, versions, 1.0) == 0
    out = capsys.readouterr().out
    assert "1 cells: 1 compared, 1 agree, 0 disagree (100.00% agree), 0 not compared" in out
    assert "0 not compared; 2 target-level cells outside the rate (1 not compared)." in out
    assert "Target-level cells (not in the rate): 2" in out
    assert "Input × targets with a package compared: 1 of 3 (66.7% excluded)" in out
    assert "Nothing compared: o/q" in out
    data = script.results_json(cells, versions, [LINUX], {"inputs": []}, 1.0)
    assert data["totals"]["uv.lock vs uv sync (all)"]["cells"] == 1
    assert data["target_level"]["uv.lock vs uv sync (all)"]["cells"] == 2
    assert data["coverage"]["uv.lock vs uv sync (all)"]["inputs_with_nothing_compared"] == ["o/q"]


def test_report_counts_target_level_cells_for_no_referee_inputs(capsys):
    """poetry.lock has no referee: every cell is target-level and not compared.

    The summary line must not say "0 cells" while the reason list below it shows
    all of those cells as not compared (#797).
    """
    why = "no referee: Poetry can't install for another platform, and it isn't run on untrusted projects here"
    cells = [
        script.Cell("o/p", "poetry.lock", "-", "all", label, "*", "-", "-", "not compared", why)
        for label in ("3.11 linux-x86_64", "3.12 linux-x86_64", "3.13 linux-x86_64")
    ]
    versions = {"uv": "uv", "pip": "pip", "wheelgap": "0", "wheelgap_commit": "abc", "checkout_head": "def"}
    assert script.report(cells, versions, 1.0) == 0
    out = capsys.readouterr().out
    assert (
        "0 cells: 0 compared, 0 agree, 0 disagree (n/a agree), 0 not compared; "
        "3 target-level cells outside the rate (3 not compared)." in out
    )
    assert "Not compared, by reason:" in out
    assert f"       3  {why}" in out


def test_check_repo_summary_counts_target_level_cells(monkeypatch):
    import io

    why = "no referee: Poetry can't install for another platform, and it isn't run on untrusted projects here"
    cells = [
        script.Cell("org/demo", "poetry.lock", "-", "all", "3.13 linux-x86_64", "*", "-", "-", "not compared", why)
    ]
    entry = {"id": "org/demo", "kind": "poetry.lock", "main": "poetry.lock"}
    monkeypatch.setattr(script, "resolve_repo_commit", lambda repo, ref, fetch: "b" * 40)
    monkeypatch.setattr(script, "fetch_repo_corpus", lambda repo, sha, corpus_dir, fetch: {"inputs": [entry]})
    monkeypatch.setattr(script, "check_input", lambda *args, **kwargs: cells)
    out = io.StringIO()
    versions = {"uv": "0.6.0", "pip": "25.0", "wheelgap": "0.14.0", "wheelgap_commit": "0" * 40}
    code = script.check_repo("org/demo", target_names=["3.13 linux-x86_64"], versions=versions, out=out)
    assert code == 0
    assert (
        "Summary: 0 compared, 0 agreed, 0 disagreed, 0 not compared; "
        "1 target-level cells outside the rate (1 not compared)." in out.getvalue()
    )


def test_cache_key_covers_what_changes_an_answer(tmp_path):
    glibc_217 = make_targets("3.13", "linux-x86_64", glibc="2.17")[0]
    command = ["uv", "sync"]
    one = script.cache_path(tmp_path, "d", "uv sync", LINUX, "uv 1", command)
    assert one == script.cache_path(tmp_path, "d", "uv sync", LINUX, "uv 1", command)
    assert one != script.cache_path(tmp_path, "d", "uv sync", glibc_217, "uv 1", command)  # same label, other libc
    assert one != script.cache_path(tmp_path, "d", "uv sync", LINUX, "uv 2", command)
    assert one != script.cache_path(tmp_path, "d", "uv sync", LINUX, "uv 1", [*command, "--x"])
    assert one.parent == tmp_path / "d"


def test_cacheable():
    assert script.cacheable({"status": {}})
    assert not script.cacheable({"error": "uv timed out"})
    refused = {"label": "all", "error": "error: The current Python platform ...", "refused": "environments"}
    assert script.cacheable({"selections": [refused]})
    assert not script.cacheable(
        {"selections": [{"label": "all", "error": "error: No interpreter found", "refused": None}]}
    )


def test_sync_selection_never_takes_a_timeout_for_an_answer(lock, tmp_path):
    def fake(cmd, cwd, env):
        return -1, "error: timed out after 300 s"

    with pytest.raises(script.RefereeError, match="timed out"):
        script.sync_selection(fake, tmp_path, {}, ["uv", "sync"], script.Selection("all", []), lock)


def test_check_input_stages_outside_the_output_folder(tmp_path, monkeypatch):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "uv.lock").write_text(LOCK, encoding="utf-8")
    entry = {"id": "o/p", "kind": "uv.lock", "dir": str(src), "main": "uv.lock", "files": ["uv.lock"]}
    seen = []

    def fake(cmd, cwd, env):
        seen.append(Path(cwd))
        return (0, "/py/python3.13\n") if cmd[1] == "python" else (0, " + appnope==0.1.4\n")

    out = tmp_path / "out"
    versions = {"uv": "uv 0.13.1", "pip": "pip", "wheelgap": "0", "wheelgap_commit": "abc"}
    cells = script.check_input(entry, [MAC], out, versions, fake, workers=1)
    assert cells and all(c.target == "3.13 macos-arm64" for c in cells)
    assert seen and all(out not in cwd.parents for cwd in seen)  # the copy is a temporary folder...
    assert not any(cwd.exists() for cwd in seen)  # ...removed afterwards
    assert sorted(p.name for p in out.iterdir()) == ["cache"]


# --- requirements files -----------------------------------------------------------------------------------------


def test_flatten_reads_includes_and_markers(tmp_path):
    (tmp_path / "base.txt").write_text("idna==3.10 \\\n  --hash=sha256:aa\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "--extra-index-url https://example.test/simple\n-r base.txt\nsix==1.17.0 ; sys_platform == 'win32'\n",
        encoding="utf-8",
    )
    flat = script.flatten(tmp_path / "requirements.txt")
    assert flat.refuse is None
    assert flat.options == ["--extra-index-url https://example.test/simple"]
    assert [(p.name, p.version, p.marker, p.line) for p in flat.pins] == [
        ("idna", "3.10", "", "idna==3.10"),
        ("six", "1.17.0", 'sys_platform == "win32"', "six==1.17.0 ; sys_platform == 'win32'"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "--no-binary :all:",
        "--only-binary :none:",
        "--only-binary=:none:",
        "--find-links x",
        "-f x",
        "--config-settings a=b",
        "-c constraints.txt",
        "-e .",
        "six>=1",
        "-r missing.txt",
        "idna==3.10 --no-binary idna",
        # pip reads every option on a line, so an index option can't carry another one with it.
        "-i https://pypi.org/simple --only-binary :none:",
        "--index-url=https://pypi.org/simple --no-binary=:all:",
        "--pre --no-binary :all:",
        "--prefer-binary --find-links x",
        "--trusted-host x --no-binary idna",
        "--extra-index-url https://x.test/simple \\\n  --only-binary :none:",
        "-i --no-binary=:all:",
        "--extra-index-url https://x.test/${GITHUB_TOKEN}/simple",  # pip and uv expand it from os.environ
    ],
)
def test_flatten_refuses(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(f"{line}\nidna==3.10\n", encoding="utf-8")
    assert script.flatten(tmp_path / "requirements.txt").refuse


@pytest.mark.parametrize(
    "line", ["-i https://pypi.org/simple", "--index-url=https://pypi.org/simple", "--trusted-host x.test", "--pre"]
)
def test_flatten_passes_index_options(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(f"{line}\nidna==3.10\n", encoding="utf-8")
    flat = script.flatten(tmp_path / "requirements.txt")
    assert flat.refuse is None and flat.options == [line]


def test_pip_sdist_seen():
    log = (
        "  Skipping link: No sources permitted for docopt: https://files.pythonhosted.org/x/docopt-0.6.1.tar.gz "
        "(from https://pypi.org/simple/docopt/)\n"
        "  Skipping link: No sources permitted for docopt: https://files.pythonhosted.org/x/docopt-0.6.2.tar.gz "
        "(from https://pypi.org/simple/docopt/)\n"
    )
    assert script.pip_sdist_seen(log, "docopt", "0.6.2")
    assert not script.pip_sdist_seen(log, "docopt", "0.7.0")


class FakePip:
    """Fails each named requirement in turn, as pip stops at the first one, then succeeds with wheels."""

    def __init__(self, fails, sdists=()):
        self.fails = list(fails)
        self.sdists = set(sdists)
        self.calls = []

    def __call__(self, cmd, cwd, env):
        self.calls.append(cmd)
        reqs = Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8").split()
        report = Path(cmd[cmd.index("--report") + 1])
        for name in self.fails:
            if any(r.startswith(name + "==") for r in reqs):
                log = f"Fetched page https://pypi.org/simple/{name}/ as application/vnd.pypi.simple.v1+json\n"
                if name in self.sdists:
                    log += f"  Skipping link: No sources permitted for {name}: https://f/x/{name}-1.0.tar.gz (from x)\n"
                return 1, log + f"ERROR: No matching distribution found for {name}==1.0\n"
        install = [
            {
                "metadata": {"name": r.split("==")[0], "version": r.split("==")[1]},
                "download_info": {"url": f"https://f/{r.split('==')[0]}-1.0-py3-none-any.whl"},
            }
            for r in reqs
            if "==" in r
        ]
        report.write_text(json.dumps({"install": install}), encoding="utf-8")
        return 0, ""


def test_pip_referee(tmp_path):
    flat = script.Flat(
        [script.Pin(n, "1.0", "", f"{n}==1.0") for n in ("a", "b", "c")]
        + [script.Pin("w", "1.0", "sys_platform == 'win32'", "w==1.0")],
        [],
    )
    fake = FakePip(["b", "c"], sdists=["b"])
    answer = script.pip_referee(fake, tmp_path, LINUX, flat, tmp_path / "cache")
    assert answer["status"] == {"a==1.0": "wheel", "b==1.0": "source", "c==1.0": "missing"}
    assert answer["not_compared"] == {"w==1.0": "pip evaluates markers for the machine it runs on"}
    assert len(fake.calls) == 3
    first = fake.calls[0]
    assert "--only-binary=:all:" in first and "--no-deps" in first and "--isolated" in first
    assert first[first.index("--abi") + 1] == "cp313"


def test_pip_referee_stops_at_a_build(tmp_path):
    def fake(cmd, cwd, env):
        return 0, "Preparing metadata (pyproject.toml): started\n"

    flat = script.Flat([script.Pin("a", "1.0", "", "a==1.0")], [])
    assert "started a build" in script.pip_referee(fake, tmp_path, LINUX, flat, tmp_path)["error"]


def test_pip_referee_needs_evidence(tmp_path):
    def fake(cmd, cwd, env):
        return 1, "ERROR: No matching distribution found for a==1.0\n"  # never fetched the page

    flat = script.Flat([script.Pin("a", "1.0", "", "a==1.0")], [])
    assert "didn't fetch" in script.pip_referee(fake, tmp_path, LINUX, flat, tmp_path)["error"]


def test_uv_pip_referee(tmp_path):
    calls = []

    def fake(cmd, cwd, env):
        calls.append(cmd)
        text = Path(cmd[3]).read_text(encoding="utf-8")
        if "b==1.0" in text:
            return 1, (
                "error: No solution found when resolving dependencies:\n  ╰─▶ Because b==1.0 has no wheels with a "
                "matching platform tag (e.g., `manylinux_2_36_x86_64`) and you require b==1.0, we can conclude that "
                "your requirements are unsatisfiable.\n"
            )
        return 0, "a==1.0\n"  # w's marker is false on Linux

    flat = script.Flat(
        [
            script.Pin("a", "1.0", "", "a==1.0"),
            script.Pin("b", "1.0", "", "b==1.0"),
            script.Pin("w", "1.0", "sys_platform == 'win32'", "w==1.0"),
        ],
        [],
    )
    answer = script.uv_pip_referee(fake, tmp_path, LINUX, flat, tmp_path)
    assert answer["status"] == {"a==1.0": "wheel", "b==1.0": "no wheel"}
    assert calls[0][calls[0].index("--python-platform") + 1] == "x86_64-manylinux_2_36"


def test_uv_pip_referee_names_a_pin_with_a_marker(tmp_path):
    def fake(cmd, cwd, env):
        if "w==1.0" in Path(cmd[3]).read_text(encoding="utf-8"):
            return 1, (
                "error: No solution found when resolving dependencies\n"
                "  cause: Because w{sys_platform == 'win32'}==1.0 has no wheels with a matching Python ABI tag "
                "(e.g., `cp313`) and you require "
                "w{sys_platform == 'win32'}==1.0, we can conclude that your requirements are unsatisfiable.\n"
            )
        return 0, "a==1.0\n"

    flat = script.Flat(
        [script.Pin("a", "1.0", "", "a==1.0"), script.Pin("w", "1.0", "sys_platform == 'win32'", "w==1.0")], []
    )
    assert script.uv_pip_referee(fake, tmp_path, WIN, flat, tmp_path)["status"] == {
        "a==1.0": "wheel",
        "w==1.0": "no wheel",
    }


# --- wheelgap's side ----------------------------------------------------------------------------------------------


def test_wheelgap_answer_keys_rows_by_lock_entry(tmp_path, lock):
    path = tmp_path / "uv.lock"
    path.write_text(LOCK, encoding="utf-8")
    ours = script.wheelgap_answer(path, MAC, lock, "all")
    rows = ours["rows"]
    # The two torch 2.9.1 rows are told apart by their files; n/a rows (torch+cpu on macOS) aren't listed.
    assert rows["torch==2.9.1 @ https://pypi.org/simple"] == "wheel"
    assert rows["torch==2.9.1 @ https://pt.test/cpu"] == "wheel"
    assert "torch==2.9.1+cpu @ https://pt.test/cpu" not in rows
    assert rows["appnope==0.1.4 @ https://pypi.org/simple"] == "wheel"
    assert ours["ambiguous"] == []


def test_flatten_refuses_an_include_outside_the_folder(tmp_path):
    (tmp_path / "outside.txt").write_text("idna==3.10\n", encoding="utf-8")
    (tmp_path / "input").mkdir()
    for line in ("-r ../outside.txt", f"-r {tmp_path / 'outside.txt'}"):
        (tmp_path / "input" / "requirements.txt").write_text(f"{line}\nsix==1.16.0\n", encoding="utf-8")
        assert "outside" in script.flatten(tmp_path / "input" / "requirements.txt").refuse
    (tmp_path / "input" / "base.txt").write_text("idna==3.10\n", encoding="utf-8")
    (tmp_path / "input" / "requirements.txt").write_text("-r base.txt\nsix==1.16.0\n", encoding="utf-8")
    assert not script.flatten(tmp_path / "input" / "requirements.txt").refuse


def test_fetch_repo_corpus_matches_manifest_and_lands_files(tmp_path):
    sha = "a" * 40
    pyproject = b'[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.13"\n'
    files = {
        "https://api.github.com/repos/org/demo": json.dumps({"default_branch": "main"}).encode(),
        "https://api.github.com/repos/org/demo/commits/main": json.dumps({"sha": sha}).encode(),
        f"https://raw.githubusercontent.com/org/demo/{sha}/uv.lock": LOCK.encode("utf-8"),
        f"https://raw.githubusercontent.com/org/demo/{sha}/pyproject.toml": pyproject,
        f"https://raw.githubusercontent.com/org/demo/{sha}/requirements.txt": b"-r reqs/base.txt\nsix==1.16.0\n",
        f"https://raw.githubusercontent.com/org/demo/{sha}/reqs/base.txt": b"idna==3.10\n",
    }

    def fake_fetch(url: str) -> bytes:
        if url not in files:
            raise FileNotFoundError(url)
        return files[url]

    resolved = script.resolve_repo_commit("org/demo", fetch=fake_fetch)
    assert resolved == sha
    with pytest.raises(ValueError, match="OWNER/NAME"):
        script.resolve_repo_commit("../bad/repo", fetch=fake_fetch)
    corpus_dir = tmp_path / "corpus"
    got_manifest = script.fetch_repo_corpus("org/demo", resolved, corpus_dir, fetch=fake_fetch)
    folder = corpus_dir / "repos" / "org__demo"
    assert (folder / "uv.lock").is_file()
    assert (folder / "pyproject.toml").is_file()
    assert (folder / "requirements.txt").is_file()
    assert (folder / "reqs" / "base.txt").is_file()
    expected = script.make_manifest(corpus_dir)
    assert got_manifest == expected
    assert [e["kind"] for e in got_manifest["inputs"]] == ["uv.lock", "requirements"]
    assert got_manifest["inputs"][0]["commit"] == sha


def test_check_repo_prints_bug_report_on_disagreement():
    import io

    sha = "b" * 40
    pyproject = (
        b'[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.13"\ndependencies = ["winthing"]\n'
    )
    uv_lock = b"""\
version = 1
revision = 3
requires-python = ">=3.13"

[[package]]
name = "demo"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "winthing" }]

[[package]]
name = "winthing"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/w/winthing-1.0-py3-none-any.whl" }]
"""
    files = {
        f"https://api.github.com/repos/org/demo/commits/{sha}": json.dumps({"sha": sha}).encode(),
        f"https://raw.githubusercontent.com/org/demo/{sha}/uv.lock": uv_lock,
        f"https://raw.githubusercontent.com/org/demo/{sha}/pyproject.toml": pyproject,
    }

    def fake_fetch(url: str) -> bytes:
        if url not in files:
            raise FileNotFoundError(url)
        return files[url]

    # Fake uv claims winthing==1.0 has no wheel under --no-build (source), disagreeing with wheelgap (wheel).
    def fake_run(cmd, cwd, env):
        if cmd[:3] == ["uv", "python", "find"]:
            return 0, "/usr/bin/python3.13\n"
        if "--no-build" in cmd and "--no-install-package" not in cmd:
            return (
                1,
                "error: Distribution `winthing==1.0 @ registry+https://pypi.org/simple` "
                "can't be installed because it is marked as `--no-build` but has no binary distribution\n",
            )
        return 0, " + winthing==1.0\n"

    out = io.StringIO()
    code = script.check_repo(
        "org/demo",
        ref=sha,
        target_names=["3.13 linux-x86_64"],
        fetch=fake_fetch,
        run=fake_run,
        versions={"uv": "0.6.0", "pip": "25.0", "wheelgap": "0.14.0", "wheelgap_commit": "0" * 40},
        out=out,
    )
    text = out.getvalue()
    assert code == 1
    assert f"org/demo @ {sha}" in text
    assert "disagreed" in text
    assert f"Commit: {sha}" in text
    assert "Package: winthing==1.0" in text
    assert "wheelgap: wheel" in text
    assert "uv sync: source" in text
    assert "wheelgap command: uvx wheelgap uv.lock" in text
    assert "referee command: " in text and "uv sync" in text
