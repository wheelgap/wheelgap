import pytest

from wheelgap.check import (
    ConflictWorld,
    Status,
    check,
    lock_environments_label,
    lock_environments_note,
    marker_holds,
    pdm_target_value,
    status_for,
)
from wheelgap.pypi import fill_all
from wheelgap.sources import Package, Source, load_inputs
from wheelgap.targets import build_targets


def results(rows):
    return {r.package.name: [s.value for s in r.results.values()] for r in rows}


def test_status_for():
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1-cp313-cp313-manylinux_2_17_x86_64.whl"], lin) is Status.WHEEL
    assert status_for(["a-1-cp313-cp313-win_amd64.whl", "a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1-cp313-cp313-win_amd64.whl"], lin) is Status.MISSING
    assert status_for([], lin) is Status.MISSING
    assert status_for(None, lin) is Status.UNKNOWN
    assert status_for(["garbage.whl", "a-1.zip"], lin) is Status.SOURCE


def test_uv_lock_graph(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    targets = build_targets(["3.13"], ["linux-x86_64", "musl-aarch64", "win-amd64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "source", "wheel"]
    assert got["purewheel"] == ["wheel", "wheel", "wheel"]
    assert got["puresdist"] == ["source", "source", "source"]
    assert got["winonly"] == ["n/a", "n/a", "wheel"]  # only pulled in on Windows
    assert got["oldpy"] == ["n/a", "n/a", "n/a"]  # only on Python < 3.13
    assert got["devtool"] == ["wheel", "wheel", "wheel"]  # dev group is included
    assert got["orphan"] == ["n/a", "n/a", "n/a"]  # nothing depends on it
    assert got["nativeext"] == ["source", "source", "source"]
    assert "gitdep" not in got and "demo" not in got


def test_uv_lock_via(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    lin, musl, win = targets = build_targets(["3.13"], ["linux-x86_64", "musl-aarch64", "win-amd64"])
    rows = {r.package.name: r for r in check(sources, targets)}
    assert rows["nativeext"].via == {t: ["demo", "fastlib", "nativeext"] for t in targets}
    assert rows["winonly"].via == {win: ["demo", "winonly"]}  # n/a targets get no path
    assert rows["orphan"].via == {}


def graph(edges, root_edges=(), root_groups=None, roots=(0,)):
    """A uv.lock-style Source whose package names are the keys of `edges`, in order."""
    names = list(edges)
    packages = [Package(n, "1", files=[f"{n}-1.tar.gz"]) for n in names]
    for i in roots:
        packages[i].skip = "editable"
    deps = {names.index(n): [(d, None, m, None) for d, m in out] for n, out in edges.items()}
    return Source(
        "uv.lock", packages, edges=deps, roots=list(roots), root_edges=list(root_edges), root_groups=root_groups or {}
    )


def via(src, targets, name):
    return next(r for r in check([src], targets) if r.package.name == name).via


def test_via_is_a_shortest_path():
    # A depth-first walk pops `long` first and reaches `c` through long → x.
    src = graph(
        {
            "root": [("short", None), ("long", None)],
            "short": [("c", None)],
            "long": [("x", None)],
            "x": [("c", None)],
            "c": [],
        }
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "c") == {t: ["root", "short", "c"]}


def test_via_differs_per_target():
    win_only = "sys_platform == 'win32'"
    src = graph(
        {"root": [("w", win_only), ("l", None)], "w": [("c", None)], "l": [("x", None)], "x": [("c", None)], "c": []}
    )
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    assert via(src, [lin, win], "c") == {lin: ["root", "l", "x", "c"], win: ["root", "w", "c"]}


def test_via_from_dependency_group():
    src = graph(
        {"root": [], "tool": [("dep", None)], "dep": []},
        root_edges=[("tool", None, None, None)],
        root_groups={0: "dev"},
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "tool") == {t: ["(dev group)", "tool"]}
    assert via(src, [t], "dep") == {t: ["(dev group)", "tool", "dep"]}


def test_via_from_plain_requirement():
    src = graph({"req": []}, root_edges=[("req", None, None, None)], roots=())
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "req") == {t: ["req"]}


def test_uv_lock_graph_python_312(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    (t,) = build_targets(["3.12"], ["linux-x86_64"])
    got = results(check(sources, [t]))
    assert got["oldpy"] == ["source"]
    assert got["fastlib"] == ["source"]  # only cp313 wheels


def test_uv_version_disambiguation():
    src = Source(
        "uv.lock",
        packages=[
            Package("root", "0", skip="editable"),
            Package("np", "1.0", files=["np-1.0.tar.gz"]),
            Package("np", "2.0", files=["np-2.0-py3-none-any.whl"]),
        ],
        edges={
            0: [("np", "1.0", "python_version < '3.10'", None), ("np", "2.0", "python_version >= '3.10'", None)],
            1: [],
            2: [],
        },
        roots=[0],
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([src], [t])
    assert [(r.package.version, r.results[t].value) for r in rows] == [("1.0", "n/a"), ("2.0", "wheel")]


def test_uv_source_disambiguation(tmp_path):
    cpu = "https://download.pytorch.org/whl/cpu"
    pypi = "https://pypi.org/simple"
    cpu_whl = "torch-2.5.1-cp313-cp313-manylinux_2_17_x86_64.whl"
    mac_whl = "torch-2.5.1-cp313-none-macosx_11_0_arm64.whl"
    (tmp_path / "uv.lock").write_text(
        f"""
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "linux-helper" }},
    {{ name = "mac-helper" }},
]

[[package]]
name = "linux-helper"
version = "1.0"
source = {{ registry = "{pypi}" }}
wheels = [{{ url = "https://files.pythonhosted.org/packages/linux_helper-1.0-py3-none-any.whl", hash = "sha256:00" }}]
dependencies = [
    {{ name = "torch", version = "2.5.1", source = {{ registry = "{cpu}" }}, marker = "sys_platform == 'linux'" }},
]

[[package]]
name = "mac-helper"
version = "1.0"
source = {{ registry = "{pypi}" }}
wheels = [{{ url = "https://files.pythonhosted.org/packages/mac_helper-1.0-py3-none-any.whl", hash = "sha256:00" }}]
dependencies = [
    {{ name = "torch", version = "2.5.1", source = {{ registry = "{pypi}" }}, marker = "sys_platform == 'darwin'" }},
]

[[package]]
name = "torch"
version = "2.5.1"
source = {{ registry = "{cpu}" }}
wheels = [{{ url = "{cpu}/{cpu_whl}", hash = "sha256:00" }}]

[[package]]
name = "torch"
version = "2.5.1"
source = {{ registry = "{pypi}" }}
wheels = [{{ url = "https://files.pythonhosted.org/packages/{mac_whl}", hash = "sha256:00" }}]
""",
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    lin, mac = build_targets(["3.13"], ["linux-x86_64", "macos-arm64"])
    rows = [r for r in check([src], [lin, mac]) if r.package.name == "torch"]
    assert [(r.package.registry, r.results[lin].value, r.results[mac].value) for r in rows] == [
        (cpu, "wheel", "n/a"),
        (pypi, "n/a", "wheel"),
    ]
    assert rows[0].via == {lin: ["app", "linux-helper", "torch"]}
    assert rows[1].via == {mac: ["app", "mac-helper", "torch"]}


def test_markers_on_packages(fixtures):
    sources = load_inputs([str(fixtures / "pylockproj")])
    targets = build_targets(["3.13"], ["linux-aarch64", "macos-arm64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "missing"]
    assert got["macpkg"] == ["n/a", "source"]
    assert all(r.via == {} for r in check(sources, targets))  # no graph, no paths


def test_bad_marker_counts_as_needed():
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert marker_holds("this is not a marker", t)
    assert marker_holds(None, t)
    assert not marker_holds("sys_platform == 'win32'", t)


def test_requirements_against_fake_index(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "reqproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages)
    targets = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    got = results(check(sources, targets))
    assert got == {
        "fastlib": ["wheel", "source"],
        "unpinned": ["wheel", "wheel"],
        "winonly": ["n/a", "wheel"],
        "purewheel": ["wheel", "wheel"],
    }
    assert next(p for p in packages if p.name == "unpinned").version == "4.2"
    assert not any("localpkg" in c for c in fake_pypi)


def test_lookup_errors_are_unknown(fake_pypi):
    pkgs = [Package("doesnotexist", "1.0"), Package("fastlib", "9.9")]
    fill_all(pkgs)
    assert [p.error for p in pkgs] == ["not on index", "version 9.9 not on index"]
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([Source("x", pkgs)], [t])
    assert [r.results[t] for r in rows] == [Status.UNKNOWN, Status.UNKNOWN]


def test_online_refresh_overrides_lockfile(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "uvproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages, refresh=True)
    fastlib = next(p for p in packages if p.name == "fastlib")
    assert fastlib.files == ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]


def test_pyodide_sdist_only_is_missing():
    (py,) = build_targets(["3.13"], ["pyodide"])
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1.tar.gz"], py) is Status.MISSING  # no compiler in the browser
    assert status_for(["a-1-cp313-cp313-pyodide_2025_0_wasm32.whl", "a-1.tar.gz"], py) is Status.WHEEL


def test_via_prefers_the_projects_own_dependency_over_a_group():
    # numpy is a runtime dependency of the project and also listed in the dev group.
    src = graph(
        {"app": [("numpy", None)], "numpy": []}, root_edges=[("numpy", None, None, None)], root_groups={0: "dev"}
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "numpy") == {t: ["app", "numpy"]}


def test_via_prefers_a_plain_requirement_over_a_group():
    src = graph(
        {"a": [("c", None)], "b": [("c", None)], "c": []},
        root_edges=[("a", None, None, None), ("b", None, None, None)],
        root_groups={0: "dev"},
        roots=(),
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "c") == {t: ["b", "c"]}


def test_markers_wheelgap_cannot_know_count_as_needed():
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    # uv writes [tool.uv] conflicts as extra == '...' terms; installers know the OS release, we don't.
    for marker in ("extra == 'extra-7-proj-cu124'", "platform_release >= '20.0'", "'docs' in dependency_groups"):
        assert marker_holds(marker, t), marker
    assert not marker_holds("sys_platform == 'win32'", t)


def test_uv_conflict_items_decide_markers_given_which_are_on():
    lin, mac = build_targets(["3.13"], ["linux-x86_64", "macos-arm64"])
    unsloth, vllm = "extra-9-agentbook-unsloth", "extra-9-agentbook-vllm"
    known = frozenset({unsloth, vllm})
    # bojieli/ai-agent-book's browser-use -> pyobjc edge: lowercase 'darwin' never matches platform_system, and the
    # other clause needs both sides of the unsloth/vllm conflict, so no install of the lock has pyobjc.
    pyobjc = f"platform_system == 'darwin' or (extra == '{unsloth}' and extra == '{vllm}')"
    for on in (frozenset(), frozenset({unsloth}), frozenset({vllm})):
        world = ConflictWorld(on, known)
        assert not marker_holds(pyobjc, lin, extras=frozenset(), conflicts=world)
        assert not marker_holds(pyobjc, mac, extras=frozenset(), conflicts=world)
    assert marker_holds(pyobjc, mac, extras=frozenset())  # without the conflicts: unknown, so counted
    torch = f"sys_platform != 'darwin' and extra != '{unsloth}'"
    assert marker_holds(torch, lin, extras=frozenset(), conflicts=ConflictWorld(frozenset({vllm}), known))
    assert not marker_holds(torch, lin, extras=frozenset(), conflicts=ConflictWorld(frozenset({unsloth}), known))
    # A name that isn't one of the lock's conflict items stays unknown.
    assert marker_holds(
        "extra == 'extra-3-foo-x1'", mac, extras=frozenset(), conflicts=ConflictWorld(frozenset(), known)
    )


def test_platform_machine_is_unknown_on_32bit_x86():
    win32, i686, musl = build_targets(["3.13"], ["win32", "linux-i686", "musl-i686"])
    # 32-bit Python on 64-bit Windows reports AMD64, and a linux/386 container on an x86_64 host reports x86_64.
    assert marker_holds('platform_machine == "AMD64"', win32)
    assert marker_holds("platform_machine == 'x86_64'", i686) and marker_holds("platform_machine == 'x86_64'", musl)
    # SQLAlchemy 2.0's greenlet marker: must not hide greenlet, which has no wheels for these targets.
    greenlet = (
        'platform_machine == "aarch64" or (platform_machine == "ppc64le" or (platform_machine == "x86_64" or '
        '(platform_machine == "amd64" or (platform_machine == "AMD64" or (platform_machine == "win32" or '
        'platform_machine == "WIN32")))))'
    )
    assert marker_holds(greenlet, win32) and marker_holds(greenlet, i686)
    # The rest of the marker still decides.
    assert not marker_holds("sys_platform == 'linux' and platform_machine == 'x86_64'", win32)
    assert not marker_holds("sys_platform == 'win32' and platform_machine == 'AMD64'", i686)
    (amd64,) = build_targets(["3.13"], ["win-amd64"])
    assert not marker_holds("platform_machine == 'x86'", amd64)


def test_platform_machine_is_unknown_on_32bit_arm():
    glibc, musl = build_targets(["3.13"], ["linux-armv7l", "musl-armv7l"])
    # Raspberry Pi OS 32-bit on a 64-bit kernel, or a linux/arm/v7 container on an arm64 host, reports aarch64.
    assert marker_holds('platform_machine == "aarch64"', glibc) and marker_holds('platform_machine == "aarch64"', musl)
    assert marker_holds("platform_machine == 'armv7l'", glibc)
    # The rest of the marker still decides.
    assert not marker_holds("sys_platform == 'win32' and platform_machine == 'aarch64'", glibc)
    assert not marker_holds("sys_platform == 'win32' and platform_machine == 'aarch64'", musl)
    # 64-bit-only userlands keep their own value.
    ppc, s390x = build_targets(["3.13"], ["linux-ppc64le", "linux-s390x"])
    assert not marker_holds("platform_machine == 'aarch64'", ppc) and not marker_holds(
        "platform_machine == 'x86_64'", s390x
    )


def test_more_sdist_formats():
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["foo-1.0.tar"], t) is Status.SOURCE
    assert status_for(["foo-1.0.tar.lz"], t) is Status.SOURCE


def test_yanked_files_count_only_when_every_usable_file_is_yanked():
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    files = ["foo-2.0-cp313-cp313-manylinux_2_17_x86_64.whl", "foo-2.0-cp313-cp313-win_amd64.whl"]
    yanked = frozenset({files[0]})
    # Only the Linux wheel is yanked: pip on Linux has nothing else, so it installs the yanked wheel.
    assert status_for(files, lin, yanked=yanked) is Status.WHEEL
    assert status_for(files + ["foo-2.0.tar.gz"], lin, yanked=yanked) is Status.SOURCE  # a live sdist wins
    assert status_for(files, win, yanked=yanked) is Status.WHEEL


def test_unknowable_markers_still_respect_the_platform_part():
    lin, mac, win = build_targets(["3.13"], ["linux-x86_64", "macos-arm64", "win-amd64"])
    cpu = "sys_platform != 'darwin' and extra == 'extra-7-proj-cpu'"  # uv conflicts (torch cpu/cu124)
    assert marker_holds(cpu, lin) and not marker_holds(cpu, mac)
    cuda = "sys_platform == 'win32' and 'cuda' in extras"  # pylock.toml
    assert marker_holds(cuda, win) and not marker_holds(cuda, lin)
    pyobjc = "sys_platform == 'darwin' and platform_release >= '20.0'"
    assert marker_holds(pyobjc, mac) and not marker_holds(pyobjc, lin)


def test_three_valued_markers_from_the_review():
    lin, mac = build_targets(["3.13"], ["linux-x86_64", "macos-arm64"])
    for marker, on_linux, on_mac in [
        ("'cuda' in extras and 'cpu' not in extras", True, True),  # conflicting extras: unknown, so needed
        ("platform_release >= '20' and platform_release < '23'", True, True),
        ("sys_platform == 'linux' and 'microsoft' in platform_release", True, False),  # WSL check
        ("extra == 'a' and extra == 'b'", True, True),
        ("sys_platform == 'darwin' and extra == 'gpu' or python_version < '3.9'", False, True),
        ("(sys_platform == 'win32' or extra == 'x') and python_version >= '3.10'", True, True),
        ("sys_platform == 'win32' and python_version >= '3.10'", False, False),
    ]:
        assert marker_holds(marker, lin) is on_linux, (marker, "linux")
        assert marker_holds(marker, mac) is on_mac, (marker, "mac")


def test_marker_values_with_quotes_are_not_misread(monkeypatch):
    from packaging._parser import Value

    # packaging before 26.3 always re-serialised values in double quotes; wheelgap must not rely on
    # serialize() picking the right quotes, or this marker reads as "x" and "win32" and turns False.
    monkeypatch.setattr(Value, "serialize", lambda self: f'"{self.value}"')
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert marker_holds("""sys_platform != 'x" and sys_platform == "win32'""", lin)


@pytest.mark.parametrize("platform", ["musl-x86_64", "musl-aarch64", "musl-armv7l"])
def test_plain_linux_wheel_counts_on_musl(platform):
    # pip on Alpine lists linux_<arch> among its tags (packaging 26.3's tags._linux_platforms), and uv does too
    # (crates/uv-platform-tags/src/tags.rs, 0.13.9): PyTorch's torch-1.10.2+cpu-cp39-cp39-linux_x86_64.whl installs.
    (musl,) = build_targets(["3.9"], [platform])
    arch = platform.removeprefix("musl-")
    assert status_for([f"torch-1.10.2+cpu-cp39-cp39-linux_{arch}.whl"], musl) is Status.WHEEL
    assert status_for(["torch-1.10.2+cpu-cp39-cp39-linux_s390x.whl"], musl) is Status.MISSING  # another arch


def test_explain_ordering_on_musl():
    from wheelgap.check import explain, rejection

    (musl,) = build_targets(["3.13"], ["musl-armv7l"])
    assert rejection("grpcio-1.85.1-cp313-cp313-linux_aarch64.whl", musl) == "linux_aarch64 doesn't match musl-armv7l"
    files = ["x-1-cp313-abi3-macosx_11_0_universal2.whl", "x-1-cp312-cp312-linux_armv7l.whl"]
    # The same-arch Linux wheel comes first; a macOS universal2 wheel isn't "same arch" on Linux.
    assert [f for f, _ in explain(files, musl)][0] == "x-1-cp312-cp312-linux_armv7l.whl"


def test_mobile_explain_reasons_and_sdists():
    from wheelgap.check import rejection

    (ios,) = build_targets(["3.13"], ["ios"])
    (android,) = build_targets(["3.13"], ["android"])
    assert rejection("x-1-cp313-cp313-ios_15_0_arm64_iphoneos.whl", ios) == (
        "ios_15_0_arm64_iphoneos needs iOS 15.0, newer than 13.0 (--ios)"
    )
    assert rejection("x-1-cp313-cp313-android_26_arm64_v8a.whl", android) == (
        "android_26_arm64_v8a needs Android API level 26, newer than 24 (--android-api)"
    )
    assert rejection("x-1-cp313-cp313-ios_11_0_arm64_iphoneos.whl", ios) == (
        "ios_11_0_arm64_iphoneos is older than iOS 12.0, the oldest pip accepts"
    )
    assert rejection("x-1-cp313-cp313-ios_12_10_arm64_iphoneos.whl", ios) == (
        "ios_12_10_arm64_iphoneos isn't one of the iOS versions pip tries for 13.0 (x.0 to x.9 of each older release)"
    )
    assert rejection("x-1-cp313-cp313-android_15_arm64_v8a.whl", android) == (
        "android_15_arm64_v8a is older than API level 16, the oldest pip accepts"
    )
    # Mobile installs can't build sdists, so an sdist alone is "can't install", not "from source".
    assert status_for(["x-1.0.tar.gz"], ios) is Status.MISSING


def test_python_full_version_and_implementation_version_markers():
    from packaging._parser import parse_marker as _parse

    from wheelgap.check import _atom

    (lin313,) = build_targets(["3.13"], ["linux-x86_64"])
    (win313,) = build_targets(["3.13"], ["win-x86_64"])

    # Patch-dependent comparisons on the target minor version are unknown (never hide).
    assert _atom(_parse("python_full_version >= '3.13.2'")[0], lin313) is None
    assert _atom(_parse("python_full_version > '3.13.0'")[0], lin313) is None
    assert _atom(_parse("python_full_version == '3.13.1'")[0], lin313) is None
    assert _atom(_parse("python_full_version != '3.13.1'")[0], lin313) is None
    assert _atom(_parse("python_full_version in '3.13.1 3.13.2'")[0], lin313) is None
    assert _atom(_parse("implementation_version >= '3.13.2'")[0], lin313) is None
    assert _atom(_parse("python_full_version != '3.13.0'")[0], lin313) is None
    assert _atom(_parse("'3.13.2' <= python_full_version")[0], lin313) is None
    assert _atom(_parse("python_full_version < '3.13.1rc1'")[0], lin313) is None
    # A patch named however the value writes it: a wildcard, a v prefix, an epoch, another separator
    assert _atom(_parse("python_full_version == '3.13.1.*'")[0], lin313) is None
    assert _atom(_parse("python_full_version == 'v3.13.2'")[0], lin313) is None
    assert _atom(_parse("python_full_version == '0!3.13.2'")[0], lin313) is None
    assert _atom(_parse("python_full_version in '3.13.1;3.13.2'")[0], lin313) is None

    # Comparisons that agree across every X.Y.* patch release still decide True/False.
    assert _atom(_parse("python_full_version >= '3.13.0'")[0], lin313) is True
    assert _atom(_parse("python_full_version >= '3.13.0rc1'")[0], lin313) is True
    assert _atom(_parse("python_full_version == '3.14.1.*'")[0], lin313) is False
    assert _atom(_parse("python_full_version == '3.13.*'")[0], lin313) is True
    assert _atom(_parse("python_full_version < '3.14.0'")[0], lin313) is True
    assert _atom(_parse("python_full_version < '3.13.0'")[0], lin313) is False
    assert _atom(_parse("python_full_version >= '3.14.0'")[0], lin313) is False
    assert _atom(_parse("python_full_version == '3.14.1'")[0], lin313) is False
    assert _atom(_parse("implementation_version < '3.13.0'")[0], lin313) is False

    # Three-valued logic keeps unknown patch-gated packages reachable unless another term is False.
    assert marker_holds("python_full_version >= '3.13.2'", lin313)
    assert marker_holds("python_full_version >= '3.13.2' and sys_platform == 'win32'", win313)
    assert not marker_holds("python_full_version >= '3.13.2' and sys_platform == 'win32'", lin313)
    assert marker_holds("extra == 'api' and python_full_version >= '3.13.2'", lin313, extras=frozenset({"api"}))
    assert not marker_holds("extra == 'api' and python_full_version >= '3.13.2'", lin313, extras=frozenset({"other"}))


def test_status_for_and_explain_with_format_control():
    from wheelgap.check import explain, explain_lines

    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    (win,) = build_targets(["3.13"], ["win-amd64"])
    files = ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]

    assert status_for(files, lin, no_binary=True) is Status.SOURCE
    assert status_for([files[0]], lin, no_binary=True) is Status.MISSING
    assert status_for(files, win, only_binary=True) is Status.MISSING
    assert status_for(files, lin, only_binary=True) is Status.WHEEL

    # With a live wheel and a yanked sdist, --no-binary falls back to the yanked sdist.
    yanked_sdist = frozenset({"fastlib-2.0.0.tar.gz"})
    assert status_for(files, lin, yanked=yanked_sdist, no_binary=True) is Status.SOURCE

    assert explain(files, lin, no_binary=True) == [
        ("fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "ignored (--no-binary)")
    ]
    assert explain_lines(files, lin, no_binary=True) == [
        "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl  ignored (--no-binary)"
    ]
    lines_only_bin = explain_lines(files, win, only_binary=True)
    assert any("--only-binary forbids building from source" in line for line in lines_only_bin)


@pytest.mark.parametrize(
    "entry,python,platform,expected",
    [
        ({"requires_python": "==3.13.*", "platform": "manylinux_2_40_x86_64"}, "3.13", "linux-x86_64", True),
        ({"requires_python": "==3.13.*", "platform": "manylinux_2_40_x86_64"}, "3.14", "linux-x86_64", False),
        ({"requires_python": "==3.13.*", "platform": "manylinux_2_40_x86_64"}, "3.13", "linux-aarch64", False),
        ({"requires_python": "==3.13.*", "platform": "manylinux_2_40_x86_64"}, "3.13", "musl-x86_64", False),
        ({"requires_python": ">=3.10", "platform": "manylinux_2_17_x86_64"}, "3.13", "linux-x86_64", True),
        ({"requires_python": ">=3.10", "platform": "manylinux_2_17_i686"}, "3.13", "linux-i686", True),
        ({"requires_python": ">=3.10", "platform": "alpine"}, "3.13", "musl-x86_64", True),
        ({"requires_python": ">=3.10", "platform": "musllinux_1_2_aarch64"}, "3.13", "musl-aarch64", True),
        ({"requires_python": ">=3.10", "platform": "windows"}, "3.13", "win-amd64", True),
        ({"requires_python": ">=3.10", "platform": "windows"}, "3.13", "win32", False),
        ({"requires_python": ">=3.10", "platform": "windows_x86"}, "3.13", "win32", True),
        ({"requires_python": ">=3.10", "platform": "windows_arm64"}, "3.13", "win-arm64", True),
        ({"requires_python": ">=3.10", "platform": "macos"}, "3.13", "macos-arm64", True),
        ({"requires_python": ">=3.10", "platform": "macos_26_0_arm64"}, "3.13", "macos-arm64", True),  # HIGHER
        ({"requires_python": ">=3.10", "platform": "macos_x86_64"}, "3.13", "macos-arm64", False),
        ({"requires_python": ">=3.10", "platform": "freebsd_14_x86_64"}, "3.13", "linux-x86_64", False),
        ({"requires_python": ">=3.10", "platform": "linux"}, "3.13", "pyodide", False),
        ({"requires_python": ">=3.10", "platform": "emscripten_wasm32"}, "3.13", "pyodide", None),
        ({"requires_python": ">=3.10", "platform": "nonsense"}, "3.13", "linux-x86_64", None),
        ({"requires_python": ">=3.10", "platform": "linux_sparc"}, "3.13", "linux-x86_64", None),
        ({"requires_python": ">=3.10", "implementation": "cpython"}, "3.14", "linux-x86_64", True),
        ({"requires_python": ">=3.10", "implementation": "cpython"}, "3.14t", "linux-x86_64", False),
        (
            {"requires_python": ">=3.10", "implementation": "cpython", "gil_disabled": True},
            "3.14t",
            "macos-arm64",
            True,
        ),
        ({"requires_python": ">=3.10", "implementation": "pypy"}, "3.13", "linux-x86_64", False),
        ({"requires_python": ">=3.10", "implementation": "jython"}, "3.13", "linux-x86_64", None),
        ({"requires_python": ">=3.10", "gil_disabled": True}, "3.13", "linux-x86_64", True),  # needs implementation
        ({"requires_python": ">=3.10", "platform": "linux"}, "3.14t", "linux-x86_64", True),
        ({"requires_python": ">=3.13.2"}, "3.13", "linux-x86_64", None),  # 3.13.0 no, 3.13.5 yes
        ({"requires_python": "!=3.13.2"}, "3.13", "linux-x86_64", None),
        ({"requires_python": ">=3.13.2"}, "3.14", "linux-x86_64", True),
        ({"requires_python": "<3.13"}, "3.13", "linux-x86_64", False),
        ({"requires_python": ""}, "3.9", "linux-x86_64", True),
        ({"requires_python": "not a specifier"}, "3.13", "linux-x86_64", None),
        ({"platform": "linux"}, "3.13", "linux-x86_64", None),  # PDM can't read it without requires_python
        ({"requires_python": ">=3.10", "libc": "glibc"}, "3.13", "linux-x86_64", None),  # a key PDM 2.29 lacks
    ],
)
def test_pdm_target_value(entry, python, platform, expected):
    (target,) = build_targets([python], [platform])
    assert pdm_target_value(entry, target) is expected


def test_lock_environments_label_names_several_locks():
    uv = Source("uv.lock", supported_markers=["sys_platform == 'linux'"])
    pdm = Source("pdm.lock", pdm_targets=[{"requires_python": ">=3.10", "platform": "linux"}])
    assert lock_environments_label([uv]) == "uv.lock is locked for `[tool.uv] environments` only"
    assert lock_environments_label([uv, pdm, Source("pylock.toml")]) == (
        "uv.lock and pdm.lock are each locked for `[tool.uv] environments` or `pdm lock` targets only"
    )
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert lock_environments_note([uv, pdm], ["win-amd64"], [lin], "default targets").endswith(
        "which leave out win-amd64: checking the default targets they all cover (linux-x86_64)"
    )
