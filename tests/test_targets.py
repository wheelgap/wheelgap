import pytest
from packaging.utils import parse_wheel_filename

from wheelgap import targets as target_module
from wheelgap.targets import (
    DEFAULT_PLATFORMS,
    EVERY_NATIVE_PLATFORM,
    NATIVE_PLATFORMS,
    NEWEST_PYTHON,
    PLATFORMS,
    Target,
    TargetError,
    build_targets,
    manylinux_platforms,
    next_python_note,
    parse_python,
    resolve_platform,
)


def accepts(target: Target, filename: str) -> bool:
    return target.accepts(parse_wheel_filename(filename)[3])


@pytest.mark.parametrize(
    "names,libc,version",
    [
        (("ubuntu-20.04",), "glibc", "2.31"),
        (("ubuntu-22.04", " UBUNTU-22.04 "), "glibc", "2.35"),
        (("ubuntu-24.04",), "glibc", "2.39"),
        (("debian-11", "bullseye"), "glibc", "2.31"),
        (("debian-12", "bookworm"), "glibc", "2.36"),
        (("debian-13", "trixie"), "glibc", "2.41"),
        (("rhel-8", "rocky-8", "alma-8"), "glibc", "2.28"),
        (("rhel-9", "rocky-9", "alma-9"), "glibc", "2.34"),
        (("amazonlinux-2",), "glibc", "2.26"),
        (("amazonlinux-2023",), "glibc", "2.34"),
        (("alpine",), "musl", "1.2"),
    ],
)
def test_distro_versions_and_wheel_boundaries(names, libc, version):
    for name in names:
        assert target_module.resolve_distro(name) == (libc, version)
        platform, family = ("linux-x86_64", "manylinux") if libc == "glibc" else ("musl-x86_64", "musllinux")
        target = build_targets(["3.13"], [platform], **{libc: version})[0]
        major, minor = map(int, version.split("."))
        assert accepts(target, f"pkg-1.0-cp313-cp313-{family}_{major}_{minor}_x86_64.whl")
        assert not accepts(target, f"pkg-1.0-cp313-cp313-{family}_{major}_{minor + 1}_x86_64.whl")


@pytest.mark.parametrize("name", ["", "ubuntu-99.04", "alpine-3.12", "rocky-10"])
def test_unknown_distro_lists_supported_names(name):
    with pytest.raises(TargetError, match="unknown distro") as caught:
        target_module.resolve_distro(name)
    for known in ("ubuntu-22.04", "bookworm", "rocky-9", "alma-8", "alpine"):
        assert known in str(caught.value)


def t(platform: str, py: str = "3.13", **kw) -> Target:
    ma, mi, ft = parse_python(py)
    return Target(ma, mi, ft, platform, **kw)


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("3.13", (3, 13, False)),
        ("3.14t", (3, 14, True)),
        ("cp314t", (3, 14, True)),
        ("312", (3, 12, False)),
        ("next", (*NEWEST_PYTHON, False)),
        (" NEXT-T ", (*NEWEST_PYTHON, True)),
    ],
)
def test_parse_python(spec, expected):
    assert parse_python(spec) == expected


def test_newest_python_is_the_latest_release():
    # 3.15 came out in October 2026. Move NEWEST_PYTHON on when the next one does (see AGENTS.md).
    assert NEWEST_PYTHON == (3, 15)


@pytest.mark.parametrize("spec", ["nextt", "next-", "next-3.15", "latest", "next3"])
def test_parse_python_rejects_other_names(spec):
    with pytest.raises(TargetError, match="try 3.13, 3.14t or next"):
        parse_python(spec)


def test_next_in_build_targets():
    targets = build_targets(["3.14,next", "next-t", "3.15"], ["linux"])  # 3.15 and next are the same target
    assert [t.python for t in targets] == ["3.14", "3.15", "3.15t"]
    with pytest.raises(TargetError, match="no Pyodide build for Python 3.15"):
        build_targets(["next"], ["pyodide"])


@pytest.mark.parametrize(
    "pythons,expected",
    [
        (["3.14", "next"], "next is Python 3.15, the newest CPython this wheelgap version knows"),
        (
            ["next-t,3.14", " Next "],
            "next-t is Python 3.15t and next is Python 3.15, the newest CPython this wheelgap version knows",
        ),
        (["3.14", "3.15t"], None),
    ],
)
def test_next_python_note(pythons, expected):
    assert next_python_note(pythons) == expected


@pytest.mark.parametrize("spec", ["3.12t", "cp312t", "312t", "3.9t", " 3.12T ", "2.7t"])
def test_parse_python_rejects_free_threaded_before_3_13(spec):
    # No cp312t wheel can exist, so every package would look like a source build.
    with pytest.raises(TargetError, match=r"free-threading starts at Python 3\.13 \(not \d\.\d+t\)"):
        parse_python(spec)
    with pytest.raises(TargetError, match="free-threading starts at Python 3.13"):
        build_targets([f"3.13,{spec}"], ["linux"])


def test_parse_python_keeps_free_threaded_from_3_13():
    assert parse_python("3.13t") == (3, 13, True)
    assert parse_python("3.12") == (3, 12, False)


def test_parse_python_rejects_garbage():
    with pytest.raises(TargetError):
        parse_python("latest")


def test_parse_python_rejects_non_ascii_digits():
    # int("٣") is 3, so a Unicode \d match would quietly turn -p 3.1٣ into 3.13.
    with pytest.raises(TargetError):
        parse_python("3.1٣")
    with pytest.raises(TargetError, match="can't parse Python version"):
        build_targets(["3.1٣"], [])


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("alpine", "musl-x86_64"),
        ("linux-arm64", "linux-aarch64"),
        ("linux/amd64", "linux-x86_64"),
        ("linux/arm64", "linux-aarch64"),
        ("linux/arm64/v8", "linux-aarch64"),
        (" LINUX/ARM64/V8 ", "linux-aarch64"),
        ("windows", "win-amd64"),
        ("MACOS_INTEL", "macos-x86_64"),
    ],
)
def test_aliases(alias, expected):
    assert resolve_platform(alias) == expected


def test_unknown_platform():
    with pytest.raises(TargetError, match="unknown target"):
        resolve_platform("solaris")


@pytest.mark.parametrize(
    "name,suggestion",
    [
        ("linux-aarch46", "linux-aarch64"),
        ("Linux_AARCH46", "linux-aarch64"),
        ("window-amd64", "win-amd64"),
        ("linx-x86_64", "linux-x86_64"),
        ("musl-aarch46", "musl-aarch64"),
        ("pyodid", "pyodide"),
        ("linuxaarch64", "linux-aarch64"),
        ("linux-aarch-64", "linux-aarch64"),
    ],
)
def test_unknown_platform_suggests_a_close_name(name, suggestion):
    with pytest.raises(TargetError) as caught:
        resolve_platform(name)
    assert str(caught.value) == f"unknown target {name!r}; did you mean {suggestion!r}?"


# A real name for another OS or arch, a bare arch, or a Docker platform (glibc or musl depends on the image):
# suggesting a near neighbour here would point at the wrong platform.
@pytest.mark.parametrize(
    "name",
    [
        "macos-amd64",
        "musl-amd64",
        "osx-arm64",
        "darwin-arm64",
        "linux-riscv32",
        "linux-armv6",
        "linux-armv8",
        "linux-ppc64be",
        "linux-aarch32",
        "linux-armel",
        "win-arm64ec",
        "macos-arm64e",
        "x86_64",
        "aarch64",
        "linux/arm46",
    ],
)
def test_unknown_platform_never_suggests_another_platform(name):
    with pytest.raises(TargetError, match=r"\(known:") as caught:
        resolve_platform(name)
    assert "did you mean" not in str(caught.value)


def test_unknown_platform_without_a_close_name():
    with pytest.raises(TargetError, match=r"unknown target 'banana' \(known:") as caught:
        resolve_platform("banana")
    assert "did you mean" not in str(caught.value)


def test_manylinux_includes_legacy_aliases():
    plats = manylinux_platforms("x86_64", (2, 28))
    assert plats[0] == "manylinux_2_28_x86_64"
    assert "manylinux2014_x86_64" in plats and "manylinux1_x86_64" in plats
    assert "manylinux1_aarch64" not in manylinux_platforms("aarch64", (2, 28))
    assert "manylinux2014_aarch64" in manylinux_platforms("aarch64", (2, 28))


def test_manylinux_stops_at_2_17_outside_x86_and_i686():
    # pip (packaging._manylinux.platform_tags) goes down to glibc 2.5 only on x86_64/i686; every
    # other arch's first manylinux was 2.17, so pip takes no manylinux tag below that (issue #79).
    plats = manylinux_platforms("aarch64", (2, 28))
    assert "manylinux_2_17_aarch64" in plats and "manylinux2014_aarch64" in plats
    assert "manylinux_2_12_aarch64" not in plats
    for arch in ("aarch64", "armv7l", "ppc64le", "s390x", "riscv64"):
        floor_plats = manylinux_platforms(arch, (2, 28))
        assert f"manylinux_2_17_{arch}" in floor_plats and f"manylinux_2_12_{arch}" not in floor_plats
    assert "manylinux_2_5_x86_64" in manylinux_platforms("x86_64", (2, 28))
    assert "manylinux_2_12_x86_64" in manylinux_platforms("x86_64", (2, 28))
    assert "manylinux_2_5_i686" in manylinux_platforms("i686", (2, 28))
    # pip's 2.17 floor is a glibc-major-2 rule: it doesn't apply to a future glibc major 3
    assert "manylinux_3_12_aarch64" in manylinux_platforms("aarch64", (3, 20))


def test_manylinux_floor_boundary_is_exact_outside_x86():
    # exact lists at the floor: glibc 2.17 still has its own tag and the 2014 alias; 2.16 is below
    # every manylinux pip counts on aarch64, so only plain linux wheels stay
    assert t("linux-aarch64", glibc=(2, 17)).platform_tags() == [
        "manylinux_2_17_aarch64",
        "manylinux2014_aarch64",
        "linux_aarch64",
    ]
    assert t("linux-aarch64", glibc=(2, 16)).platform_tags() == ["linux_aarch64"]


@pytest.mark.parametrize(
    "platform,arch",
    [
        ("linux-aarch64", "aarch64"),
        ("linux-armv7l", "armv7l"),
        ("linux-ppc64le", "ppc64le"),
        ("linux-s390x", "s390x"),
        ("linux-riscv64", "riscv64"),
    ],
)
def test_manylinux_below_2_17_is_rejected_outside_x86_arches(platform, arch):
    # default target glibc (2.36): a manylinux_2_12 wheel is simply not in the tags pip would take
    assert not accepts(t(platform), f"x-1-cp313-cp313-manylinux_2_12_{arch}.whl")


def test_manylinux_2_12_is_still_accepted_on_x86_64():
    assert accepts(t("linux-x86_64"), "x-1-cp313-cp313-manylinux_2_12_x86_64.whl")


def test_manylinux_target_below_the_arch_floor_takes_only_plain_linux():
    # a glibc-2.8 aarch64: pip runs no manylinux wheel there, only plain linux_aarch64 (PEP 599)
    assert t("linux-aarch64", glibc=(2, 8)).platform_tags() == ["linux_aarch64"]


def test_glibc_ceiling():
    assert accepts(t("linux-x86_64", glibc=(2, 36)), "x-1-cp313-cp313-manylinux_2_34_x86_64.whl")
    assert not accepts(t("linux-x86_64", glibc=(2, 31)), "x-1-cp313-cp313-manylinux_2_34_x86_64.whl")
    assert accepts(t("linux-x86_64"), "x-1-cp313-cp313-manylinux2014_x86_64.whl")


def test_glibc_and_musl_dont_mix():
    assert not accepts(t("musl-x86_64"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64"), "x-1-cp313-cp313-musllinux_1_1_x86_64.whl")
    assert accepts(t("musl-x86_64"), "x-1-cp313-cp313-musllinux_1_1_x86_64.whl")


def test_arch_matters():
    assert not accepts(t("linux-aarch64"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert not accepts(t("win-arm64"), "x-1-cp313-cp313-win_amd64.whl")
    assert accepts(t("win-arm64"), "x-1-cp313-cp313-win_arm64.whl")


def test_macos_universal2_and_minimum_version():
    assert accepts(t("macos-arm64"), "x-1-cp313-cp313-macosx_10_13_universal2.whl")
    assert accepts(t("macos-x86_64"), "x-1-cp313-cp313-macosx_10_9_x86_64.whl")
    assert not accepts(t("macos-arm64", macos=(14, 0)), "x-1-cp313-cp313-macosx_15_0_arm64.whl")
    assert accepts(t("macos-arm64", macos=(15, 0)), "x-1-cp313-cp313-macosx_15_0_arm64.whl")


def test_python_version_matters():
    assert not accepts(t("linux-x86_64", "3.14"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert accepts(t("linux-x86_64", "3.14"), "x-1-cp310-abi3-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.11"), "x-1-cp312-abi3-manylinux_2_17_x86_64.whl")


def test_free_threading():
    ft = t("linux-x86_64", "3.14t")
    assert accepts(ft, "x-1-cp314-cp314t-manylinux_2_17_x86_64.whl")
    assert not accepts(ft, "x-1-cp314-cp314-manylinux_2_17_x86_64.whl")
    assert not accepts(ft, "x-1-cp310-abi3-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.14"), "x-1-cp314-cp314t-manylinux_2_17_x86_64.whl")


def test_pure_python_everywhere():
    for plat in PLATFORMS:
        assert accepts(t(plat, "3.14t"), "x-1-py3-none-any.whl"), plat
    assert not accepts(t("linux-x86_64"), "x-1-py2-none-any.whl")


def test_marker_env():
    env = t("win-arm64", "3.14").marker_env
    assert env["sys_platform"] == "win32" and env["platform_machine"] == "ARM64"
    assert env["python_version"] == "3.14"
    assert t("musl-aarch64").marker_env["platform_machine"] == "aarch64"


def test_build_targets_defaults_and_all():
    ts = build_targets(["3.13"], [])
    assert [x.platform for x in ts] == DEFAULT_PLATFORMS
    ts = build_targets(["3.13,3.14t"], ["all"])
    assert len(ts) == 2 * len(NATIVE_PLATFORMS)
    ts = build_targets(["3.13"], ["alpine,linux", "alpine"])
    assert [x.platform for x in ts] == ["musl-x86_64", "linux-x86_64"]


def test_build_targets_accepts_docker_platform_list():
    ts = build_targets(["3.13"], ["linux/amd64,linux/arm64,linux/arm64/v8"])
    assert [x.platform for x in ts] == ["linux-x86_64", "linux-aarch64"]


def test_build_targets_bad_glibc():
    with pytest.raises(TargetError):
        build_targets(["3.13"], [], glibc="new")


@pytest.mark.parametrize("pythons", [[], [""], [","], ["  ", " , "]])
def test_build_targets_empty_python(pythons):
    with pytest.raises(TargetError, match=r"no Python version given \(-p is empty\)"):
        build_targets(pythons, ["linux"])


def test_pyodide_tags_per_python():
    (t313,) = build_targets(["3.13"], ["pyodide"])
    assert accepts(t313, "x-1-cp313-cp313-pyodide_2025_0_wasm32.whl")
    assert accepts(t313, "x-1-cp313-cp313-pyemscripten_2025_0_wasm32.whl")  # PEP 783 spelling of the same ABI
    assert accepts(t313, "x-1-py3-none-any.whl")
    assert not accepts(t313, "x-1-cp313-cp313-pyemscripten_2026_0_wasm32.whl")  # ABIs don't mix
    assert not accepts(t313, "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    (t314,) = build_targets(["3.14"], ["wasm"])
    assert accepts(t314, "x-1-cp314-cp314-pyemscripten_2026_0_wasm32.whl")
    assert not accepts(t314, "x-1-cp313-cp313-pyodide_2025_0_wasm32.whl")
    (t312,) = build_targets(["3.12"], ["emscripten"])
    assert accepts(t312, "x-1-cp312-cp312-pyodide_2024_0_wasm32.whl")


def test_pyodide_marker_env():
    (t,) = build_targets(["3.13"], ["pyodide"])
    assert t.marker_env["sys_platform"] == "emscripten"
    assert t.marker_env["platform_machine"] == "wasm32"
    assert not t.builds_sdists


def test_pyodide_unknown_python_and_free_threading():
    with pytest.raises(TargetError, match="no Pyodide build for Python 3.10"):
        build_targets(["3.10"], ["pyodide"])
    with pytest.raises(TargetError, match="no Pyodide build for Python 3.14t"):
        build_targets(["3.14t"], ["pyodide"])


def test_all_does_not_include_pyodide():
    assert "pyodide" not in [t.platform for t in build_targets(["3.13"], ["all"])]
    assert len(build_targets(["3.13"], ["all"])) == 8


def test_python_37_uses_the_m_abi():
    # CPython 3.7 and older built pymalloc ("m") ABIs, so numpy's cp37-cp37m wheels install there.
    assert accepts(t("linux-x86_64", "3.7"), "numpy-1.21.6-cp37-cp37m-manylinux_2_12_x86_64.manylinux2010_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.8"), "x-1-cp38-cp38m-manylinux_2_17_x86_64.whl")


def test_abi3t_wheels_on_free_threaded_315():
    wheel = "x-1.0-cp315-abi3.abi3t-manylinux_2_28_x86_64.whl"
    assert accepts(t("linux-x86_64", "3.15t"), wheel)  # needs packaging >= 26.3 (PEP 803)
    assert not accepts(t("linux-x86_64", "3.14t"), wheel)
    assert not accepts(t("linux-x86_64", "3.15t"), "x-1.0-cp312-abi3-manylinux_2_28_x86_64.whl")


def test_version_floors_accept_a_major_only():
    (target,) = build_targets(["3.13"], ["macos"], macos="15")
    assert target.macos == (15, 0)


@pytest.mark.parametrize("arch", ["armv7l", "ppc64le", "s390x", "riscv64", "i686"])
def test_extra_linux_arches(arch):
    glibc, musl = t(f"linux-{arch}"), t(f"musl-{arch}")
    assert accepts(glibc, f"x-1-cp313-cp313-manylinux_2_28_{arch}.whl")
    assert not accepts(glibc, "x-1-cp313-cp313-manylinux_2_28_x86_64.whl")
    assert not accepts(glibc, f"x-1-cp313-cp313-musllinux_1_2_{arch}.whl")
    assert accepts(musl, f"x-1-cp313-cp313-musllinux_1_2_{arch}.whl")
    assert not accepts(musl, "x-1-cp313-cp313-musllinux_1_2_aarch64.whl")
    if arch in ("i686", "armv7l"):  # depends on the host: see test_platform_machine_is_unknown_on_32bit_x86 / _arm
        assert glibc.unknown_markers == musl.unknown_markers == {"platform_machine"}
    else:
        assert glibc.marker_env["platform_machine"] == arch
        assert not glibc.unknown_markers
    assert glibc.marker_env["sys_platform"] == "linux"


def test_legacy_manylinux2014_only_where_it_exists():
    assert accepts(t("linux-armv7l"), "x-1-cp313-cp313-manylinux2014_armv7l.whl")
    assert accepts(t("linux-ppc64le"), "x-1-cp313-cp313-manylinux2014_ppc64le.whl")
    # manylinux2014 never covered riscv64: those wheels are manylinux_2_* only.
    assert not any(tag.startswith("manylinux2014") for tag in t("linux-riscv64").platform_tags())


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("linux/arm/v7", "linux-armv7l"),
        ("linux-arm", "linux-armv7l"),
        ("linux-armhf", "linux-armv7l"),
        ("alpine-armv7", "musl-armv7l"),
        ("linux/ppc64le", "linux-ppc64le"),
        ("linux/s390x", "linux-s390x"),
        ("linux/riscv64", "linux-riscv64"),
        ("linux/386", "linux-i686"),
        ("linux-386", "linux-i686"),
        ("linux-x86", "linux-i686"),
        ("alpine-x86", "musl-i686"),
        ("linux/i386", "linux-i686"),
        ("linux-i386", "linux-i686"),
        ("manylinux-i686", "linux-i686"),
        ("musl-x86", "musl-i686"),
        ("musl-386", "musl-i686"),
        ("alpine-i686", "musl-i686"),
        ("musllinux-i686", "musl-i686"),
        ("windows-x86", "win32"),
        ("win-x86", "win32"),
        ("win32", "win32"),
    ],
)
def test_extra_arch_aliases(alias, expected):
    assert resolve_platform(alias) == expected


def test_all_keeps_the_common_eight_and_everything_adds_the_rest():
    assert len(NATIVE_PLATFORMS) == 8
    assert not any(p.endswith(("armv7l", "ppc64le", "s390x", "riscv64", "i686", "win32")) for p in NATIVE_PLATFORMS)
    ts = build_targets(["3.13"], ["everything"])
    assert [x.platform for x in ts] == EVERY_NATIVE_PLATFORM
    assert len(ts) == 19 and "pyodide" not in EVERY_NATIVE_PLATFORM
    assert {"win32", "linux-i686", "musl-i686"} <= set(EVERY_NATIVE_PLATFORM)


def test_win32_target():
    win32 = t("win32")
    assert accepts(win32, "numpy-2.1.3-cp313-cp313-win32.whl")
    assert not accepts(t("win-amd64"), "numpy-2.1.3-cp313-cp313-win32.whl")
    assert not accepts(win32, "numpy-2.1.3-cp313-cp313-win_amd64.whl")
    assert not accepts(win32, "numpy-2.1.3-cp313-cp313-manylinux_2_17_i686.whl")
    assert win32.unknown_markers == {"platform_machine"}
    assert win32.marker_env["sys_platform"] == "win32"


def test_linux_i686_takes_legacy_manylinux():
    assert accepts(t("linux-i686"), "x-1-cp313-cp313-manylinux1_i686.whl")
    assert accepts(t("linux-i686"), "x-1-cp313-cp313-manylinux_2_12_i686.whl")
    assert not accepts(t("linux-x86_64"), "x-1-cp313-cp313-manylinux_2_17_i686.whl")


def test_plain_linux_arch_wheels_count_on_glibc_only():
    # PyPI accepts linux_armv7l uploads for the Raspberry Pi (grpcio ships them), and pip installs them.
    assert accepts(t("linux-armv7l"), "grpcio-1.85.1-cp313-cp313-linux_armv7l.whl")
    assert not accepts(t("linux-aarch64"), "grpcio-1.85.1-cp313-cp313-linux_armv7l.whl")
    # They're glibc builds, so they don't count on Alpine (pip would take them, then fail to load them).
    assert not accepts(t("musl-armv7l"), "grpcio-1.85.1-cp313-cp313-linux_armv7l.whl")


def test_ios_targets():
    device, sim = t("ios-arm64"), t("ios-simulator-arm64")
    assert accepts(device, "pillow-12.0.0-cp313-cp313-ios_13_0_arm64_iphoneos.whl")
    assert not accepts(device, "pillow-12.0.0-cp313-cp313-ios_13_0_arm64_iphonesimulator.whl")
    assert accepts(sim, "pillow-12.0.0-cp313-cp313-ios_13_0_arm64_iphonesimulator.whl")
    assert accepts(device, "x-1-cp313-cp313-ios_12_0_arm64_iphoneos.whl")  # older minimums are fine
    assert not accepts(device, "x-1-cp313-cp313-ios_15_0_arm64_iphoneos.whl")  # above the default 13.0
    assert accepts(t("ios-arm64", ios=(15, 0)), "x-1-cp313-cp313-ios_15_0_arm64_iphoneos.whl")
    assert not accepts(device, "x-1-cp313-cp313-macosx_11_0_arm64.whl")
    assert accepts(device, "six-1.17.0-py2.py3-none-any.whl")
    assert device.marker_env["sys_platform"] == "ios" and device.marker_env["platform_system"] == "iOS"
    assert sim.marker_env["platform_machine"] == "arm64"
    assert device.marker_env["platform_machine"] == "arm64"  # what pip sees in Briefcase's cross venv


def test_android_targets():
    arm = t("android-arm64")
    assert accepts(arm, "x-1-cp313-cp313-android_21_arm64_v8a.whl")
    assert accepts(arm, "x-1-cp313-cp313-android_24_arm64_v8a.whl")
    assert not accepts(arm, "x-1-cp313-cp313-android_26_arm64_v8a.whl")  # above the default API level 24
    assert accepts(t("android-arm64", android_api=26), "x-1-cp313-cp313-android_26_arm64_v8a.whl")
    assert not accepts(arm, "x-1-cp313-cp313-android_21_x86_64.whl")
    assert not accepts(arm, "x-1-cp313-cp313-manylinux_2_17_aarch64.whl")
    assert arm.marker_env["sys_platform"] == "android" and arm.marker_env["platform_machine"] == "aarch64"


def test_mobile_is_wheel_only_and_named_explicitly():
    assert not t("ios-arm64").builds_sdists and not t("android-x86_64").builds_sdists
    assert [x.platform for x in build_targets(["3.13"], ["mobile"])] == [
        "ios-arm64",
        "ios-simulator-arm64",
        "ios-simulator-x86_64",
        "android-arm64",
        "android-x86_64",
    ]
    assert not any(p.startswith(("ios", "android")) for p in EVERY_NATIVE_PLATFORM)
    assert [x.platform for x in build_targets(["3.13"], ["ios,android,ios-simulator"])] == [
        "ios-arm64",
        "android-arm64",
        "ios-simulator-arm64",
    ]


def test_mobile_versions_and_errors():
    (x,) = build_targets(["3.14"], ["android"], android_api="28", ios="17.2")
    assert x.android_api == 28 and x.ios == (17, 2)
    with pytest.raises(TargetError, match="need Python 3.13"):
        build_targets(["3.12"], ["ios"])
    with pytest.raises(TargetError, match="can't parse Android API level"):
        build_targets(["3.13"], ["android"], android_api="Q")
    with pytest.raises(TargetError, match="too old"):
        build_targets(["3.13"], ["android"], android_api="15")
    with pytest.raises(TargetError, match="doesn't exist"):
        build_targets(["3.13"], ["android"], android_api="5000000")
    for flag in ("ios", "macos", "glibc"):
        with pytest.raises(TargetError, match="out of range"):
            build_targets(["3.13"], ["ios"], **{flag: "13.3000000"})
    with pytest.raises(TargetError, match=r"can't parse iOS version 'abc' \(expected e.g. 13.0\)"):
        build_targets(["3.13"], ["ios"], ios="abc")
    with pytest.raises(TargetError, match="iOS 11.0 is too old"):
        build_targets(["3.13"], ["ios"], ios="11.0")
