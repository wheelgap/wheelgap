import json
import os
import re

import pytest

from wheelgap import dockerignore, pypi
from wheelgap.cli import main
from wheelgap.dockerfile import (
    DockerfileError,
    ImageTarget,
    InputsReport,
    UnknownImageError,
    _env_pairs,
    _heredocs,
    _Shell,
    _ShellError,
    _syntax_directive,
    dockerfile_requirements,
    last_from,
    parse_from,
    parse_image,
    read_dockerfile,
)
from wheelgap.sources import SourceError, load_inputs


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("python:3.13-alpine", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("--platform=linux/arm64 python:3.12-slim", ImageTarget("3.12", "linux-aarch64", glibc="2.41")),
        ('--platform="linux/arm64" python:3.12-slim', ImageTarget("3.12", "linux-aarch64", glibc="2.41")),
        ("--platform='linux/arm64' python:3.12-slim", ImageTarget("3.12", "linux-aarch64", glibc="2.41")),
        ('--platform="linux/arm/v7" python:3.13-alpine', ImageTarget("3.13", "musl-armv7l", musl="1.2")),
        ('--platform="" python:3.13', ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("python:3.14-slim-trixie", ImageTarget("3.14", "linux-x86_64", glibc="2.41")),
        ("python:3.13-bookworm", ImageTarget("3.13", "linux-x86_64", glibc="2.36")),
        ("python:3.11-slim-bullseye", ImageTarget("3.11", "linux-x86_64", glibc="2.31")),
        ("python:3.13.1-alpine3.22 AS runtime", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("docker.io/library/python:3.12", ImageTarget("3.12", "linux-x86_64", glibc="2.41")),
        ("index.docker.io/library/python:3.13-alpine", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("index.docker.io/python:3.13-alpine", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("--platform= python:3.13", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),  # empty value: the default
        ("-- python:3.13", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),  # BuildKit drops a bare --
        ("--platform=linux/arm64 -- python:3.13-alpine", ImageTarget("3.13", "musl-aarch64", musl="1.2")),
        ("python:3.13-slim@sha256:" + "0" * 64, ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("python:3.15.0a1-slim", ImageTarget("3.15", "linux-x86_64")),
        # No Debian release in the tag: the one Docker Hub publishes that Python on (see UNTAGGED_DEBIAN)
        ("python:3.13", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("python:3.13-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("python:3.8-slim", ImageTarget("3.8", "linux-x86_64", glibc="2.36")),
        ("python:3.6", ImageTarget("3.6", "linux-x86_64", glibc="2.31")),
        ("python:3.16-slim", ImageTarget("3.16", "linux-x86_64")),  # not published yet: falls back to --glibc
        # A patch tag may be older than its X.Y tag (3.12.7-slim is still bookworm), so it falls back to --glibc too
        ("python:3.12.7-slim", ImageTarget("3.12", "linux-x86_64")),
        ("python:3.13.9", ImageTarget("3.13", "linux-x86_64")),
        ("python:3.13-windowsservercore-ltsc2022", ImageTarget("3.13", "win-amd64")),
        # The floating X.Y-rc tags read their variant like X.Y tags. One with no Debian release in the tag stopped
        # being rebuilt at X.Y's release, on the Debian of the day (3.11-rc-slim is bullseye, not trixie), so it's
        # read from RC_DEBIAN; a still-built one (3.16-rc) keeps the --glibc default.
        ("python:3.16-rc", ImageTarget("3.16", "linux-x86_64")),
        ("python:3.16-rc-slim", ImageTarget("3.16", "linux-x86_64")),
        ("python:3.13-rc-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.36")),
        ("python:3.11-rc-slim", ImageTarget("3.11", "linux-x86_64", glibc="2.31")),
        ("python:3.10-rc", ImageTarget("3.10", "linux-x86_64", glibc="2.31")),
        ("python:3.15-rc", ImageTarget("3.15", "linux-x86_64", glibc="2.41")),
        ("--platform=linux/s390x python:3.11-rc-slim", ImageTarget("3.11", "linux-s390x", glibc="2.31")),
        ("python:3.16-rc-alpine", ImageTarget("3.16", "musl-x86_64", musl="1.2")),
        ("python:3.16-rc-slim-trixie", ImageTarget("3.16", "linux-x86_64", glibc="2.41")),
        ("--platform=linux/arm64 python:3.16-rc-slim-trixie", ImageTarget("3.16", "linux-aarch64", glibc="2.41")),
        ("python:3.15-rc-bookworm", ImageTarget("3.15", "linux-x86_64", glibc="2.36")),
        ("--platform=linux/ppc64le python:3.15-rc-bookworm", ImageTarget("3.15", "linux-ppc64le", glibc="2.36")),
        # Astral published rc tags for 3.14 (uv 0.8's Docker guide lists them); they always name their base
        ("ghcr.io/astral-sh/uv:python3.14-rc-alpine", ImageTarget("3.14", "musl-x86_64", musl="1.2")),
        ("ghcr.io/astral-sh/uv:python3.14-rc-bookworm-slim", ImageTarget("3.14", "linux-x86_64", glibc="2.36")),
        ("ghcr.io/astral-sh/uv:python3.13-bookworm-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.36")),
        ("ghcr.io/astral-sh/uv:python3.14-trixie-slim", ImageTarget("3.14", "linux-x86_64", glibc="2.41")),
        ("ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("ghcr.io/astral-sh/uv:0.13-python3.13-trixie-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("--platform=linux/arm/v7 python:3.13-alpine", ImageTarget("3.13", "musl-armv7l", musl="1.2")),
        ("--platform=linux/ppc64le python:3.13-bookworm", ImageTarget("3.13", "linux-ppc64le", glibc="2.36")),
        ("--platform=linux/riscv64 python:3.14-trixie", ImageTarget("3.14", "linux-riscv64", glibc="2.41")),
        ("--platform=linux/s390x python:3.13-alpine", ImageTarget("3.13", "musl-s390x", musl="1.2")),
        ("--platform=linux/riscv64 python:3.13", ImageTarget("3.13", "linux-riscv64", glibc="2.41")),
        ("--platform=linux/386 python:3.13-bookworm", ImageTarget("3.13", "linux-i686", glibc="2.36")),
        ("--platform=linux/386 python:3.11-slim-bullseye", ImageTarget("3.11", "linux-i686", glibc="2.31")),
        ("--platform=linux/386 python:3.13-alpine", ImageTarget("3.13", "musl-i686", musl="1.2")),
        ("--platform=linux/i386 python:3.13-bookworm", ImageTarget("3.13", "linux-i686", glibc="2.36")),
        # Tags Docker Hub stopped rebuilding before it dropped s390x and ppc64le for bookworm and bullseye
        ("--platform=linux/s390x python:3.9-bookworm", ImageTarget("3.9", "linux-s390x", glibc="2.36")),
        ("--platform=linux/s390x python:3.12.7-bookworm", ImageTarget("3.12", "linux-s390x", glibc="2.36")),
        ("--platform=linux/386 python:3.9-bookworm", ImageTarget("3.9", "linux-i686", glibc="2.36")),
        ("--platform=linux/386 python:3.12.7-bookworm", ImageTarget("3.12", "linux-i686", glibc="2.36")),
        ("--platform=linux/ppc64le python:3.11.0-slim-bullseye", ImageTarget("3.11", "linux-ppc64le", glibc="2.31")),
        ("--platform=linux/s390x python:3.6", ImageTarget("3.6", "linux-s390x", glibc="2.31")),
        (
            "--platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-alpine3.23",
            ImageTarget("3.12", "musl-aarch64", musl="1.2"),
        ),
    ],
)
def test_parse_from(line, expected):
    assert parse_from(line) == expected


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("ubuntu:24.04", "only the official python images"),
        ("Index.docker.io/library/python:3.13-alpine", "only the official python images"),
        ("python", "must start with a Python version"),
        ("python:latest", "must start with a Python version"),
        ("ghcr.io/astral-sh/uv:bookworm-slim", r"must specify a Python version like python3\.13-alpine"),
        ("ghcr.io/astral-sh/uv:3.13-alpine", r"must specify a Python version like python3\.13-alpine"),
        ("ghcr.io/astral-sh/uv:0.13.7-alpine", r"must specify a Python version like python3\.13-alpine"),
        ("python:${PY}-slim", "build arguments"),
        ("--platform=$BUILDPLATFORM python:3.13", "build arguments"),
        ("--platform=linux/mips64le python:3.13", "only linux/amd64"),
        ("--platform=linux/386 ghcr.io/astral-sh/uv:python3.13-bookworm-slim", "only published for linux/amd64"),
        ("--platform=linux/s390x ghcr.io/astral-sh/uv:python3.13-bookworm-slim", "only published for linux/amd64"),
        ("python:3.13-slim-buster", "unknown image variant 'buster'"),
        ("python:3.16-rc-nonsense", "unknown image variant 'nonsense'"),
        ("python:3.9-rc-slim", "older than bullseye: pass -t and --glibc instead"),
        ("--platform=linux/riscv64 python:3.13-rc-slim", r"isn't published for linux/riscv64 \(3\.13-trixie is\)$"),
        ("python:3.16-rc1", "unknown image variant 'rc1'"),
        # An X.Y-rc tag isn't read as frozen: 3.14-rc-bookworm has s390x but 3.15-rc-bookworm, built later, doesn't
        (
            "--platform=linux/s390x python:3.15-rc-bookworm",
            r"may not be published for linux/s390x: older -rc builds have it, newer ones don't \(3\.15-trixie is\)$",
        ),
        (
            "--platform=linux/ppc64le python:3.13-rc-slim-bullseye",
            r"may not be published for linux/ppc64le: .* \(3\.13-trixie is\)$",
        ),
        ("--platform=linux/riscv64 python:3.15-rc-bookworm", r"isn't published for linux/riscv64 \(3\.15-trixie is\)$"),
        ("--platform=linux/riscv64 python:3.13-bookworm", r"python:3\.13-bookworm isn't published for linux/riscv64"),
        (
            "--platform=linux/riscv64 python:3.13-slim-bookworm",
            r"isn't published for linux/riscv64 \(3\.13-trixie is\)",
        ),
        ("--platform=linux/s390x python:3.13-bookworm", "isn't published for linux/s390x"),
        ("--platform=linux/s390x python:3.13-slim-bookworm", "isn't published for linux/s390x"),
        ("--platform=linux/ppc64le python:3.13-bullseye", "isn't published for linux/ppc64le"),
        ("--platform=linux/s390x python:3.8-bullseye", r"isn't published for linux/s390x \(3\.8-bookworm is\)$"),
        ("--platform=linux/riscv64 python:3.9-bookworm", r"isn't published for linux/riscv64 \(3\.9-trixie is\)$"),
        ("--platform=linux/riscv64 python:3.8-bookworm", "isn't published for linux/riscv64$"),
        (
            "--platform=linux/riscv64 python:3.12.7-slim-bookworm",
            r"isn't published for linux/riscv64 \(3\.12-trixie is\)$",
        ),
    ],
)
def test_parse_from_errors(line, message):
    with pytest.raises(DockerfileError, match=message):
        parse_from(line)


def test_last_from_takes_the_final_stage():
    text = (
        "# syntax=docker/dockerfile:1\n"
        "FROM python:3.12-bookworm AS build\n"
        "RUN pip wheel . \\\n"
        "    -w /wheels\n"
        "from --platform=linux/arm64 \\\n"
        "    python:3.13-alpine\n"
        "COPY --from=build /wheels /wheels\n"
    )
    assert last_from(text) == "--platform=linux/arm64     python:3.13-alpine"  # joined as Docker joins
    assert parse_from(last_from(text)) == ImageTarget("3.13", "musl-aarch64", musl="1.2")


def test_read_dockerfile_legacy_docker_hub_in_final_stage(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("FROM python:3.12 AS base\nFROM index.docker.io/library/python:3.13\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")


def test_read_dockerfile_expands_a_global_arg_default(tmp_path, capsys):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG PYTHON_VERSION=3.12\nFROM python:${PYTHON_VERSION}-slim\n", encoding="utf-8")
    image = read_dockerfile(path)
    assert image == ImageTarget("3.12", "linux-x86_64", glibc="2.41")
    assert image.build_args == (("PYTHON_VERSION", "3.12"),)
    assert capsys.readouterr().err == ""  # the CLI notes it, once, naming the file


@pytest.mark.parametrize(
    ("image", "python", "patch"),
    [
        ("python:3.9.1-slim", "3.9", "3.9.1"),
        ("python:3.12.7", "3.12", "3.12.7"),
        ("python:3.12.7-alpine3.20", "3.12", "3.12.7"),
        ("python:3.15.0a1-slim", "3.15", "3.15.0a1"),
        ("python:3.9.1-windowsservercore", "3.9", "3.9.1"),
        ("ghcr.io/astral-sh/uv:0.13.7-python3.12.7-alpine", "3.12", "3.12.7"),
        ("python:3.9.1-slim@sha256:" + "0" * 64, "3.9", "3.9.1"),
        # No patch release in the tag: it follows X.Y, or is an X.Y pre-release.
        ("python:3.9", "3.9", None),
        ("python:3.13-slim", "3.13", None),
        ("python:3.15-rc", "3.15", None),
        ("python:3.15-rc-slim", "3.15", None),
        ("python:3.13rc1-slim", "3.13", None),
        ("ghcr.io/astral-sh/uv:python3.13-alpine", "3.13", None),
    ],
)
def test_parse_image_keeps_the_patch_release_a_tag_pins(image, python, patch):
    target = parse_image(image)
    assert target.python == python  # the check is still for X.Y: the patch is only kept for the note
    assert target.python_patch == patch
    assert target.image == image.split("@")[0]


@pytest.mark.parametrize("tag", ["3.15.0a1a1-slim", "3.15.0a1rc2"])
def test_parse_image_still_refuses_a_tag_with_two_pre_release_parts(tag):
    # Keeping the patch release must not widen the tags read: these were refused before and no image has them.
    with pytest.raises(UnknownImageError):
        parse_image(f"python:{tag}")


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_notes_a_patch_pinned_image(tmp_path, capsys, fake_pypi, quiet):
    path = tmp_path / "Dockerfile"
    path.write_text("FROM python:3.9.1-slim\n", encoding="utf-8")
    main(["purewheel", "--dockerfile", str(path), "--json", *(["--quiet"] if quiet else [])])
    captured = capsys.readouterr()
    note = (
        f"wheelgap: note: {path}: python:3.9.1-slim pins Python 3.9.1; wheelgap checks 3.9 at its newest patch "
        "release, so a package that needs a later 3.9 patch isn't flagged"
    )
    assert (note in captured.err) is not quiet
    targets = json.loads(captured.out)["targets"]
    assert [(t["python"], t["platform"]) for t in targets] == [("3.9", "linux-x86_64")]  # the check is unchanged


@pytest.mark.parametrize(
    ("image", "argv"),
    [
        ("python:3.9-slim", []),  # follows 3.9
        ("python:3.15-rc", []),
        ("python:3.9.1-slim", ["-p", "3.12"]),  # -p wins, so the image's Python isn't the one checked
    ],
)
def test_cli_has_no_patch_note_without_a_patch_pinned_image(tmp_path, capsys, fake_pypi, image, argv):
    path = tmp_path / "Dockerfile"
    path.write_text(f"FROM {image}\n", encoding="utf-8")
    main(["purewheel", "--dockerfile", str(path), "--json", *argv])
    assert "pins Python" not in capsys.readouterr().err


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_notes_the_global_arg_default_it_used(tmp_path, capsys, fake_pypi, quiet):
    path = tmp_path / "Dockerfile"
    path.write_text(
        "ARG NODE=22\nARG PYTHON_VERSION=3.12\nFROM node:${NODE} AS assets\nFROM python:${PYTHON_VERSION}-slim\n",
        encoding="utf-8",
    )
    main(["purewheel", "--dockerfile", str(path), "--json", *(["--quiet"] if quiet else [])])
    err = capsys.readouterr().err
    note = f"wheelgap: note: {path}: FROM uses the default of ARG PYTHON_VERSION=3.12"
    assert (note in err) is not quiet
    assert "NODE" not in err  # only the final image's FROM counts


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM --platform=$BUILDPLATFORM golang:1.22 AS build\nFROM python:3.13-slim\n",
        "ARG NODE\nFROM node:${NODE} AS assets\nFROM python:3.13-slim\n",
        "ARG A=1 B=2\nFROM node:${A} AS assets\nFROM python:3.13-slim\n",
    ],
)
def test_build_arguments_only_matter_in_the_final_images_from(tmp_path, dockerfile):
    # An earlier stage that isn't the final image's base can use any build argument, as before.
    path = tmp_path / "Dockerfile"
    path.write_text(dockerfile, encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")


def test_build_argument_problem_in_the_final_images_base_stage(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG PY\nFROM python:${PY}-slim AS base\nFROM base\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match="ARG PY has no default"):
        read_dockerfile(path)


def test_global_arg_naming_the_final_stage_is_noted(tmp_path):
    # The default picks the stage; a --build-arg could pick another, so it's noted.
    path = tmp_path / "Dockerfile"
    path.write_text(
        "ARG B=builder\nFROM python:3.12 AS builder\nFROM python:3.13 AS other\nFROM ${B}\n", encoding="utf-8"
    )
    image = read_dockerfile(path)
    assert image == ImageTarget("3.12", "linux-x86_64", glibc="2.41")
    assert image.build_args == (("B", "builder"),)


def test_base_stage_global_arg_default_is_used(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text(
        "ARG PY=3.12\nFROM python:${PY}-slim AS base\nFROM --platform=$BUILDPLATFORM base\n", encoding="utf-8"
    )
    image = read_dockerfile(path)
    assert image == ImageTarget("3.12", "linux-x86_64", glibc="2.41")
    assert image.build_args == (("PY", "3.12"),)


@pytest.mark.parametrize(
    ("dockerfile", "build_args", "python"),
    [
        ("ARG PY=3.12\nFROM python:${PY}-slim\n", {"PY": "3.13"}, "3.13"),  # replaces the default
        ("ARG PY\nFROM python:${PY}-slim\n", {"PY": "3.14"}, "3.14"),  # fills an ARG with none
        ("ARG PY=3.12\nFROM python:${PY}-slim\n", {}, "3.12"),
    ],
)
def test_build_arg_sets_the_global_arg(tmp_path, dockerfile, build_args, python):
    path = tmp_path / "Dockerfile"
    path.write_text(dockerfile, encoding="utf-8")
    image = read_dockerfile(path, build_args)
    assert image == ImageTarget(python, "linux-x86_64", glibc="2.41")
    assert image.build_args == (("PY", python),)
    assert image.given_args == frozenset(build_args)


def test_build_arg_no_global_arg_declares_is_ignored(tmp_path):
    # Docker uses a --build-arg in FROM only for an ARG declared before the first FROM.
    path = tmp_path / "Dockerfile"
    path.write_text("FROM python:3.13-slim\nARG PY=3.12\n", encoding="utf-8")
    image = read_dockerfile(path, {"PY": "3.11"})
    assert image == ImageTarget("3.13", "linux-x86_64", glibc="2.41")
    assert image.undeclared_args == ("PY",)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("3.1$X", r"--build-arg PY=3\.1\$X: values with quotes, escapes or \$ aren't supported"),
        ('"3.13"', '--build-arg PY="3.13": values with quotes'),
        ("", "--build-arg PY is empty"),
    ],
)
def test_build_arg_values_follow_the_rules_for_defaults(tmp_path, value, error):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG PY=3.12\nFROM python:${PY}-slim\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=error):
        read_dockerfile(path, {"PY": value})


def test_cli_build_arg_picks_the_image_and_says_where_values_came_from(tmp_path, capsys, fake_pypi):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG PY=3.12\nARG DISTRO=slim\nFROM python:${PY}-${DISTRO}\n", encoding="utf-8")
    main(["purewheel", "--dockerfile", str(path), "--build-arg", "PY=3.13", "--build-arg", "NOPE=1", "--json"])
    captured = capsys.readouterr()
    assert [(t["python"], t["platform"]) for t in json.loads(captured.out)["targets"]] == [("3.13", "linux-x86_64")]
    assert f"wheelgap: note: {path}: FROM uses --build-arg PY=3.13\n" in captured.err
    assert f"{path}: FROM uses the default of ARG DISTRO=slim; pass --build-arg if the build sets one" in captured.err
    assert "no ARG NOPE before the first FROM" in captured.err


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--build-arg", "PY=3.13"], "--build-arg only sets the ARGs of --dockerfile"),
        (["--dockerfile", "Dockerfile", "--build-arg", "PY"], "--build-arg PY: expected NAME=VALUE"),
        (["--dockerfile", "Dockerfile", "--build-arg", "1PY=3"], "--build-arg 1PY=3: expected NAME=VALUE"),
    ],
)
def test_cli_build_arg_usage_errors(capsys, argv, error):
    with pytest.raises(SystemExit) as exc:
        main(["purewheel", *argv])
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


AUTOMATIC = [
    "BUILDPLATFORM", "BUILDOS", "BUILDOSVERSION", "BUILDARCH", "BUILDVARIANT",
    "TARGETPLATFORM", "TARGETOS", "TARGETOSVERSION", "TARGETARCH", "TARGETVARIANT", "TARGETSTAGE",
]  # fmt: skip


@pytest.mark.parametrize("name", AUTOMATIC)
def test_automatic_platform_arg_with_a_default_uses_the_default(tmp_path, name):
    # BuildKit sets these from the platforms, then a global ARG's default replaces its value (#332).
    path = tmp_path / "Dockerfile"
    path.write_text(f"ARG {name}=3.12\nFROM python:${{{name}}}-slim\n", encoding="utf-8")
    image = read_dockerfile(path)
    assert image == ImageTarget("3.12", "linux-x86_64", glibc="2.41")
    assert image.build_args == ((name, "3.12"),)


def test_automatic_targetarch_default_picks_the_platform(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG TARGETARCH=arm64\nFROM --platform=linux/${TARGETARCH} python:3.13-slim\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.13", "linux-aarch64", glibc="2.41")


@pytest.mark.parametrize("name", AUTOMATIC)
def test_automatic_platform_arg_without_a_default_is_set_by_the_build(tmp_path, name):
    # With no default, the ARG keeps the value BuildKit set for the build, which wheelgap can't know.
    path = tmp_path / "Dockerfile"
    path.write_text(f"ARG {name}\nFROM --platform=${name} python:3.13\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=f": ARG {name} has no default: {name} is set by the build, pass -p/-t"):
        read_dockerfile(path)


def test_read_dockerfile_expands_a_global_arg_default_in_platform(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG ARCH=linux/arm64\nFROM --platform=${ARCH} python:3.13-alpine AS runtime\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.13", "musl-aarch64", musl="1.2")


@pytest.mark.parametrize(
    ("dockerfile", "message"),
    [
        ("ARG PYTHON_VERSION\nFROM python:${PYTHON_VERSION}-slim\n", "has no default"),
        # Docker accepts an empty default and sets PY to "", which names the image python:-slim.
        ("ARG PY=\nFROM python:${PY}-slim\n", "has an empty default"),
        # Docker splits this on the no-break space and declares two variables; wheelgap would read one value.
        ("ARG PY=3.12\u00a0PY=3.13\nFROM python:${PY}-slim\n", "non-ASCII characters in an ARG"),
        ('ARG PYTHON_VERSION="3.12"\nFROM python:${PYTHON_VERSION}-slim\n', "defaults with quotes"),
        ("ARG PYTHON_VERSION=3.1${SUFFIX}\nFROM python:${PYTHON_VERSION}-slim\n", "defaults with quotes"),
        ("ARG ARCH=linux/amd64\nFROM --platform=${ARCH:-linux/amd64} python:3.13\n", "build arguments"),
        ("FROM --platform=$BUILDPLATFORM python:3.13\n", "build arguments"),
        ("FROM python:${PY}-slim\nARG PY=3.13\n", "build arguments"),  # an ARG after FROM isn't used in FROM
        # Docker declares two variables here; wheelgap would read "1 B=2" as A's default, so it says so instead.
        ("ARG A=1 B=2\nFROM python:${A}-slim\n", "one NAME=value"),
        # The second ARG sets PY to 3.12 in Docker: wheelgap can't split it, so it mustn't use the first one's 3.11.
        ("ARG PY=3.11\nARG PY=3.12 OTHER=1\nFROM python:${PY}-slim\n", "one NAME=value"),
        ("ARG PYTHON_VERSION=3.12 \\\n    PYTHON_VERSION=3.13\nFROM python:${PYTHON_VERSION}-slim\n", "continuation"),
        # A heredoc body before the first FROM isn't a run of instructions: no ARG in it is a global one.
        ("RUN <<EOF\nARG PYTHON_VERSION=3.12\nEOF\nFROM python:${PYTHON_VERSION}-slim\n", "build arguments"),
    ],
)
def test_read_dockerfile_global_arg_errors(tmp_path, dockerfile, message):
    path = tmp_path / "Dockerfile"
    path.write_text(dockerfile, encoding="utf-8")
    with pytest.raises(DockerfileError, match=message):
        read_dockerfile(path)


@pytest.mark.parametrize(
    "args",
    [
        "ARG VERSION\n",
        "ARG X=\n",
        'ARG LABEL="my app"\n',
        "ARG A=1 B=2\n",
        "ARG BUILD_DATE=x \\\n    y\n",
    ],
)
def test_read_dockerfile_ignores_a_global_arg_no_from_uses(tmp_path, args):
    # An ARG wheelgap can't read only matters when FROM uses a build argument; this image is the same either way.
    path = tmp_path / "Dockerfile"
    path.write_text(f"{args}FROM python:3.13-slim\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")


def test_read_dockerfile_global_arg_keyword_in_any_case(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("arg PYTHON_VERSION=3.12\nfrom python:${PYTHON_VERSION}-slim\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.12", "linux-x86_64", glibc="2.41")


def test_read_dockerfile_global_arg_redeclared_takes_the_last(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("ARG PY\nARG PY=3.12\nFROM python:${PY}-slim\n", encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.12", "linux-x86_64", glibc="2.41")


def test_no_from(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("RUN echo hi\n")
    with pytest.raises(DockerfileError, match="no FROM line"):
        read_dockerfile(path)


@pytest.mark.parametrize("image", ["python:3.13-alpine", "ghcr.io/astral-sh/uv:python3.13-alpine"])
def test_cli_dockerfile(capsys, fixtures, tmp_path, image):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {image}\n")
    code = main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "--json"])
    captured = capsys.readouterr()
    assert code in (0, 1), captured.err
    data = json.loads(captured.out)
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "musl-x86_64")]


def test_cli_flags_beat_dockerfile(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", "win-amd64", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "win-amd64")]


@pytest.mark.parametrize(
    ("image", "status"),
    [
        ("python:3.13-slim", "wheel"),
        ("python:3.13-slim-bookworm", "source"),
        ("python:3.13-rc-slim", "source"),  # bookworm, 2.36
        ("python:3.13-rc-slim-trixie", "wheel"),
    ],
)
def test_cli_untagged_image_uses_its_debian_glibc(capsys, monkeypatch, tmp_path, image, status):
    files = [{"filename": "trixielib-1.0-cp313-cp313-manylinux_2_39_x86_64.whl"}, {"filename": "trixielib-1.0.tar.gz"}]
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: {"info": {"version": "1.0"}, "urls": files})
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {image}\n")
    main(["trixielib==1.0", "--dockerfile", str(dockerfile), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["packages"][0]["results"] == {"linux-x86_64": status}


def test_cli_bad_dockerfile_exit_2(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM node:22\n")
    assert main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile)]) == 2
    assert "only the official python images" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("line", "musl"),
    [
        ("python:3.9-alpine3.12", "1.1"),
        ("python:3.9-alpine3.10", "1.1"),
        ("python:3.13-alpine3.13", "1.2"),
        ("python:3.13-alpine", "1.2"),
        ("python:3.16-rc-alpine3.22", "1.2"),
        ("python:3.9-rc-alpine3.12", "1.1"),
    ],
)
def test_alpine_musl_version(line, musl):
    assert parse_from(line).musl == musl  # Alpine 3.12 and earlier ship musl 1.1


@pytest.mark.parametrize("platform", ["linux/arm64", "linux/arm64,linux/amd64"])
def test_cli_docker_platform_names_keep_the_images_libc(capsys, fixtures, tmp_path, platform):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", platform, "--json"])
    platforms = [t["platform"] for t in json.loads(capsys.readouterr().out)["targets"]]
    assert all(p.startswith("musl-") for p in platforms)  # used to be linux-aarch64 (glibc)


def test_cli_wheelgap_names_still_win_over_the_image(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", "linux-aarch64", "--json"])
    assert [t["platform"] for t in json.loads(capsys.readouterr().out)["targets"]] == ["linux-aarch64"]


def _dockerfile(tmp_path, text):
    path = tmp_path / "Dockerfile"
    path.write_text(text, encoding="utf-8")  # Docker reads UTF-8; Windows would otherwise write cp1252
    return path


def test_named_stage_is_followed(tmp_path):
    text = "FROM python:3.13-alpine AS base\nRUN apk add --no-cache libpq\n\nFROM base AS runtime\nCOPY . /app\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


def test_chain_of_stages_and_platforms(tmp_path):
    text = (
        "FROM --platform=linux/arm64 python:3.12-slim-bookworm AS base\n"
        "FROM base AS deps\n"
        "FROM python:3.11 AS unrelated\n"
        "FROM deps\n"
    )
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-aarch64", glibc="2.36")
    # As in Docker, a stage keeps the platform of the stage it builds on; --platform here changes nothing.
    text += "FROM --platform=linux/amd64 deps\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-aarch64", glibc="2.36")
    text = "FROM python:3.13-slim AS base\nFROM --platform=linux/arm64 base\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")


def test_stage_names_are_lowercased_like_docker(tmp_path):
    # Docker lowercases the AS name but looks a reference up as written (and image names are lowercase).
    text = "FROM python:3.13 as Base\nFROM base\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")
    text = "FROM python:3.13 AS build-env_1.2\nFROM build-env_1.2\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")
    with pytest.raises(DockerfileError, match="FROM BASE: not an earlier stage"):
        read_dockerfile(_dockerfile(tmp_path, "FROM python:3.13 as Base\nFROM BASE\n"))


def test_unknown_stage_is_a_clear_error(tmp_path):
    text = "FROM python:3.13-alpine AS base\nFROM bse\n"
    with pytest.raises(DockerfileError, match=r"FROM bse: not an earlier stage \(base\) or an official python image"):
        read_dockerfile(_dockerfile(tmp_path, text))


def test_scratch_after_stages_keeps_the_usual_error(tmp_path):
    text = "FROM python:3.13-alpine AS build\nRUN pip wheel . -w /w\nFROM scratch\nCOPY --from=build /w /w\n"
    with pytest.raises(DockerfileError, match="FROM scratch: the image starts empty"):
        read_dockerfile(_dockerfile(tmp_path, text))


_HEREDOC_LIKE_LINES = [
    "RUN echo $((1<<SHIFT))",
    'RUN python -c "print(1<<n)"',
    "CMD cat <<EOF",
    "ENV X=<<EOF",
    'LABEL note="a<<b"',
    'RUN ["sh", "-c", "cat <<EOF"]',
    'RUN --mount=type=cache,target=/x ["cat", "<<EOF"]',
    "RUN echo '<<EOF'",
    "RUN cat <<<word",
]


@pytest.mark.parametrize("line", _HEREDOC_LIKE_LINES)
def test_lines_with_heredoc_like_text_never_hide_the_final_from(tmp_path, line):
    # None of these is a heredoc to Docker (exec form, the wrong instruction, quoted, <<<), so no body is skipped.
    text = f"FROM python:3.13-alpine AS base\n{line}\nFROM python:3.12-slim\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64", glibc="2.41")


_SLIM = ImageTarget("3.12", "linux-x86_64", glibc="2.41")
_ALPINE = ImageTarget("3.13", "musl-x86_64", musl="1.2")
_TWO_STAGES = "FROM python:3.13-alpine AS base\nFROM python:3.12-slim\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The issue's example: SQL's `FROM base` in a heredoc body isn't a FROM instruction.
        (_TWO_STAGES + "RUN <<EOF psql\nSELECT *\nFROM base\nEOF\n", _SLIM),
        (_TWO_STAGES + "RUN <<'EOF'\nFROM base\nEOF\n", _SLIM),
        (_TWO_STAGES + "ONBUILD RUN <<EOF\nFROM base\nEOF\n", _SLIM),
        (_TWO_STAGES + "RUN python - <<EOF\nfrom app import main\nEOF\n", _SLIM),
        # <<- strips leading tabs (only tabs) before comparing; << doesn't.
        (_TWO_STAGES + "RUN <<-EOF\n\tFROM base\n\t\tEOF\n", _SLIM),
        (_TWO_STAGES + "RUN <<-EOF\n  EOF\nFROM base\nEOF\n", _SLIM),
        (_TWO_STAGES + "RUN <<EOF\n\tEOF\nFROM base\nEOF\n", _SLIM),
        # Every trailing \r is trimmed before comparing (then, for <<-, the leading tabs).
        (_TWO_STAGES.replace("\n", "\r\n") + "RUN <<EOF\r\nFROM base\r\nEOF\r\r\n", _SLIM),
        (_TWO_STAGES + "RUN <<-EOF\r\n\tFROM base\r\n\tEOF\r\n", _SLIM),
        # Several heredocs on one line: each body follows the last, in order.
        (_TWO_STAGES + "COPY <<A <<B /x\nB\nFROM base\nA\nFROM base\nB\n", _SLIM),
        # The body starts after the whole instruction, and is read as written: no continuations, no comments.
        (_TWO_STAGES + "RUN <<EOF \\\n    cat\nFROM base\nEOF\n", _SLIM),
        (_TWO_STAGES + "RUN <<EOF\necho \\\nEOF\nRUN echo \\\nFROM base\n", _SLIM),
        (_TWO_STAGES + "RUN <<#EOF\n# a comment\nFROM base\n#EOF\n", _SLIM),
        # `<< BITS))` in shell arithmetic is a heredoc to BuildKit, so the next `BITS))` line ends it.
        (_TWO_STAGES + "RUN echo $((1 << BITS))\nFROM base\nBITS))\n", _SLIM),
        # A heredoc in an earlier stage: its body can't redefine the stage.
        ("FROM python:3.13-alpine AS build\nRUN <<EOF\nFROM python:3.11 AS build\nEOF\nFROM build\n", _ALPINE),
        # Exec form and other instructions have no heredocs, so the next line is an instruction.
        (_TWO_STAGES + 'RUN ["sh", "-c", "cat <<EOF"]\nFROM base\n', _ALPINE),
        (_TWO_STAGES + "CMD cat <<EOF\nFROM base\n", _ALPINE),
        # Frontends that read heredocs as wheelgap does; and an old one is fine where no line has a `<<`.
        *[
            (f"{directive}\n{_TWO_STAGES}RUN <<EOF\nFROM base\nEOF\n", _SLIM)
            for directive in [
                "# syntax=docker/dockerfile:1",
                "# syntax=docker/dockerfile:1-labs",
                "# syntax=docker/dockerfile",
                "#syntax = docker/dockerfile:latest",
                "# check=skip=all\n# syntax=docker/dockerfile:1.28",
                "// syntax=docker.io/docker/dockerfile:1.29.0-labs",
                "# syntax=docker/dockerfile:1.16",
                "# syntax=docker/dockerfile:1.28.0-rc1",
                # 1.4 to 1.15 and 1.3-labs read an ordinary heredoc the same way.
                "# syntax=docker/dockerfile:1.7",
                "# syntax=docker/dockerfile:1.4.3",
                "# syntax=docker/dockerfile:1.15-labs",
                "# syntax=docker/dockerfile:1.3-labs",
                "# syntax=docker/dockerfile:1.15\n# escape=`",
                # Not a `# syntax=` directive, so the built-in frontend.
                "# hello\n# syntax=docker/dockerfile:1.2",  # after another comment
                " # syntax=docker/dockerfile:1.2",  # not at the start of the line
                # The frontend is the value up to its first space.
                "# syntax=docker/dockerfile:1 key=value",
            ]
        ],
        # From 1.16 a space after << is fine; from 1.9 parseJSON rejects `["a"] <<EOF`, so it's a heredoc.
        ("# syntax=docker/dockerfile:1.16\n" + _TWO_STAGES + "RUN cat << EOF\nFROM base\nEOF\n", _SLIM),
        ("# syntax=docker/dockerfile:1.17\n" + _TWO_STAGES + "RUN cat << EOF\nFROM base\nEOF\n", _SLIM),
        ("# syntax=docker/dockerfile:1.9\n" + _TWO_STAGES + 'RUN ["a"] <<EOF\nFROM base\nEOF\n', _SLIM),
        # No heredoc where an old frontend has none either: nothing to read differently.
        *[
            (f"# syntax={image}\n{_TWO_STAGES}{line}\nFROM base\n", _ALPINE)
            for image in ["docker/dockerfile:1.2", "docker/dockerfile:1.7", "example.com/frontend:1"]
            for line in ["RUN echo hi", "RUN echo $((1<<2))"]
        ],
    ],
)
def test_heredoc_bodies_are_not_instructions(tmp_path, text, expected):
    path = tmp_path / "Dockerfile"
    path.write_bytes(text.encode())  # keep the \r and \n exactly as written
    assert read_dockerfile(path) == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["# syntax=a"], "a"),
        (["#syntax = a  "], "a"),
        (["# Syntax=a b"], "a"),  # cut at the first space
        (["# check=x", "# escape=`", "# syntax=a"], "a"),
        (["#!/usr/bin/env x", "# syntax=a"], "a"),
        (["//syntax=a"], "a"),
        (["// check=x", "// syntax=a"], "a"),
        ([" # syntax=a"], None),  # a directive starts its line
        (["# hello", "# syntax=a"], None),
        (["# check=x", "# check=y", "# syntax=a"], None),  # parsing stops at a repeated directive
        (["//check=x", "//check=y", "//syntax=a"], None),
        (["# syntax="], None),
        (["", "# syntax=a"], None),
    ],
)
def test_syntax_directive_found_as_buildkit_does(lines, expected):
    assert _syntax_directive(lines) == expected


def test_a_line_too_deeply_nested_to_lex_is_an_error_not_a_traceback(tmp_path):
    # BuildKit (Go) reads this; Python's recursion limit stops wheelgap, so it says so instead of guessing.
    text = "FROM python:3.13\nRUN cat <<EOF " + "${a:-" * 5000 + "}" * 5000 + "\nEOF\n"
    with pytest.raises(DockerfileError, match="nested too deeply"):
        read_dockerfile(_dockerfile(tmp_path, text))


def _docs(*names):
    return [(name, False) for name in names]


# BuildKit's TestHeredocsFromLine (frontend/dockerfile/parser/parser_heredoc_test.go at cedaeca), less a repeated
# row. Its loop only checks the heredocs found, so its `RUN <<- EOF` row (expecting EOF) passes with none, and none
# is right: the lexer splits `<<-` from `EOF`.
_BUILDKIT_HEREDOCS_FROM_LINE = [
    ("RUN <<EOF", _docs("EOF")),
    ("RUN <<-EOF", [("EOF", True)]),
    ("RUN <<- EOF", []),
    ("RUN << -EOF", _docs("-EOF")),
    ("RUN <<'EOF'", _docs("EOF")),
    ("RUN 4<<EOF", _docs("EOF")),
    ("RUN <<EOF <<EOF2", _docs("EOF", "EOF2")),
    ("RUN '<<EOF'", []),
    ('RUN "<<EOF"', []),
]


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        *_BUILDKIT_HEREDOCS_FROM_LINE,
        ('RUN <<"EOF"', _docs("EOF")),
        (r"RUN <<\EOF", _docs("EOF")),
        ('RUN <<E"O"F', _docs("EOF")),
        ("RUN 2<<EOF", _docs("EOF")),
        ("COPY <<FILE1 <<FILE2 /dest", _docs("FILE1", "FILE2")),
        ("run <<EOF", _docs("EOF")),
        ("RUN << EOF", _docs("EOF")),
        ("RUN <<\tEOF", _docs("EOF")),
        ("RUN --mount=type=cache,target=/x <<EOF", _docs("EOF")),
        ("RUN --mount=type=secret,id=a -- <<EOF", _docs("EOF")),
        # From BuildKit's TestParseExtractsHeredoc.
        ("USER <<INVALID", []),
        ("RUN <<COMPLEX python3", _docs("COMPLEX")),
        ("COPY <<file.txt /dest", _docs("file.txt")),
        ("RUN <<eo'f'", _docs("eof")),
        (r"RUN <<eo\'f", _docs("eo'f")),
        (r"RUN <<'e'o\'f", _docs("eo'f")),
        ("RUN <<'one two'", _docs("one two")),
        ("RUN <<$EOF", _docs("$EOF")),
        ("RUN <<  EOF  > foo", _docs("EOF")),
        # `$(` and `$((` are ordinary text to BuildKit.
        ("RUN echo $((1 << BITS))", _docs("BITS))")),
        ("RUN x=$(cat <<EOF)", _docs("EOF)")),
        # ${x:-y} (any ${} with a modifier) empties the word it's in, as BuildKit's shared word buffer does; $$ is a
        # whole parameter, so the { after it is plain text.
        ("RUN cat <<EOF${x:-y}", []),
        ("RUN cat <<EOF$${x:-y}", _docs("EOF$${x:-y}")),
        ("RUN cat <<EOF${x}", _docs("EOF${x}")),
        ("RUN cat <<EOF$x", _docs("EOF$x")),
        ("RUN echo ${x:- <<EOF}", _docs("EOF}")),
        # Inside double quotes \ escapes only " $ and \, and is kept otherwise; a trailing \ is dropped.
        ('RUN <<"E\\OF"', _docs("E\\OF")),
        ('RUN <<"E\\"OF"', _docs('E"OF')),
        ("RUN <<EOF\\", _docs("EOF")),
        ("RUN <<EOF\\ ", _docs("EOF ")),
        # Exec form is only a JSON array of strings.
        ("RUN [ -f /x ] && cat <<EOF > /y", _docs("EOF")),
        ("RUN [NaN] <<EOF", _docs("EOF")),
        ('RUN --mount=type=cache,target=/x ["cat", "<<EOF"]', []),
        ('RUN -- ["<<EOF"]', []),
        ('RUN --x=` <<EOF ["a"]', _docs("EOF")),  # ` is plain text in a flag, unless it's the escape char
        ('COPY ["<<EOF", "/x"]', []),
        # BuildKit reads the --flags byte by byte: U+2003 isn't a space there, so the flag runs on to `["a"]`.
        ('RUN --x\u2003<<EOF ["a"]', []),
        # A lexer error (an unterminated quote, a bad ${}) is ignored, leaving no words.
        ('RUN cat <<EOF "unterminated', []),
        ("RUN cat <<'EOF", []),
        ("RUN cat <<EOF ${x", []),
        # Words split on Go's unicode.IsSpace (not \x1c-\x1f, unlike str.isspace); `<<` takes only tabs, \r and
        # spaces after it; the keyword splits off on ASCII whitespace only.
        ("RUN <<\x1cEOF", _docs("\x1cEOF")),
        ("RUN <<EOF\u00a0x", _docs("EOF")),
        ("RUN <<\vEOF", []),
        ("RUN\v<<EOF", _docs("EOF")),
        ("RUN\u00a0<<EOF", []),
        ("RUN <<\ufeffEOF", _docs("EOF")),  # Go's text/scanner skips a leading byte order mark
        # An empty delimiter is no heredoc.
        ('RUN <<""', []),
        ("RUN <<''", []),
        ("RUN <<''EOF", _docs("EOF")),
        ("RUN <<", []),
        # ONBUILD of RUN, COPY or ADD only, with keywords lowercased as Go does: U+0130 lowers to i, U+0131 doesn't.
        ("ONBUILD RUN <<EOF", _docs("EOF")),
        ("onbuild copy <<EOF /x", _docs("EOF")),
        ("ONBUILD CMD <<EOF", []),
        ("ONBUILD ONBUILD RUN <<EOF", []),
        ('ONBUILD RUN ["<<EOF"]', []),
        ("ONBUILD <<EOF", []),
        ("ONBU\u0130LD RUN <<EOF", _docs("EOF")),
        ("onbu\u0131ld run <<EOF", []),
        *[(line, []) for line in _HEREDOC_LIKE_LINES],
    ],
)
def test_heredocs(instruction, expected):
    assert _heredocs(instruction) == expected


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("RUN <<`EOF`", _docs("`EOF`")),
        (r"RUN <<\EOF", _docs("EOF")),
        ('RUN --x=` <<EOF ["a"]', []),  # the escaped space keeps `<<EOF` in the flag, so this is exec form
    ],
)
def test_heredocs_lex_with_backslash_under_a_backtick_escape(instruction, expected):
    # BuildKit's heredoc lexer always escapes with \; the `# escape=` char only matters to the --flags.
    assert _heredocs(instruction, "`") == expected


@pytest.mark.parametrize(
    ("instruction", "error"),
    [
        ("RUN echo ${x:- <<'E F'}", r"can't read the heredoc <<'E \(unexpected end of statement"),
        ('RUN ["cat <<EOF", 1]', "JSON array that isn't all strings"),
        ('ONBUILD RUN ["cat <<EOF", 1]', "JSON array that isn't all strings"),
    ],
)
def test_heredocs_raise_where_buildkit_rejects_the_line(instruction, error):
    with pytest.raises(DockerfileError, match=error):
        _heredocs(instruction)


def test_unknown_stage_lists_only_earlier_stages(tmp_path):
    text = "FROM python:3.13-alpine AS base\nFROM bse AS final\n"
    with pytest.raises(DockerfileError, match=r"not an earlier stage \(base\) or"):
        read_dockerfile(_dockerfile(tmp_path, text))
    # A stage built on a non-python image gets the usual image error, not a stage one.
    with pytest.raises(DockerfileError, match="alpine: only the official python images"):
        read_dockerfile(_dockerfile(tmp_path, "FROM alpine AS base\nFROM base\n"))


def test_python_lines_starting_with_from_are_not_errors(tmp_path):
    text = "FROM python:3.13 AS a\nRUN python - <<EOF\nfromtimestamp = 1\nEOF\nFROM python:3.12\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64", glibc="2.41")


def test_python_imports_in_heredocs_are_not_from_lines(tmp_path):
    text = (
        "FROM python:3.12-slim AS app\n"
        "COPY . /app\n"
        "FROM python:3.13-alpine\n"
        "COPY --from=app /app /app\n"
        "RUN python - <<EOF\n"
        "from app import main\n"
        "EOF\n"
    )
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


@pytest.mark.parametrize(
    "text",
    [
        # A blank line inside a continued instruction is skipped, so FROM base is part of the RUN.
        "FROM python:3.12-slim AS base\nFROM python:3.13-alpine\nRUN rm -rf /tmp/* \\\n\nFROM base\n",
        # The same with a comment line in between.
        "FROM python:3.12-slim AS base\nFROM python:3.13-alpine\nRUN rm -rf /tmp/* \\\n# note\nFROM base\n",
        # With `# escape=` + "`", a trailing backtick continues the line (and a backslash doesn't).
        "# escape=`\nFROM python:3.12-windowsservercore AS base\nFROM python:3.13-alpine\nRUN echo `\nFROM base\n",
        # A line of Go whitespace (U+3000 is unicode.IsSpace) is blank too, so FROM base is still part of the RUN.
        "FROM python:3.12-slim AS base\nFROM python:3.13-alpine\nRUN rm -rf /tmp/* \\\n\u3000\nFROM base\n",
    ],
)
def test_continuations_like_docker(tmp_path, text):
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


@pytest.mark.parametrize(
    "text",
    [
        # A comment ending in a backslash doesn't continue onto the next line.
        "FROM python:3.13-alpine AS app\n# path C:\\\nFROM python:3.12-slim\n",
        # A doubled backslash is an escaped backslash, not a continuation.
        "FROM python:3.13-alpine AS app\nRUN echo \\\\\nFROM python:3.12-slim\n",
        # Under `# escape=` + "`", a trailing backslash is just a character.
        "# escape=`\nFROM python:3.13-alpine AS app\nRUN dir C:\\\nFROM python:3.12-slim\n",
        # \x1c-\x1f aren't Go's unicode.IsSpace, so a \x1c-only line isn't blank to Docker: it ends the RUN.
        "FROM python:3.13-alpine AS app\nRUN echo \\\n\x1c\nFROM python:3.12-slim\n",
        # Nor is a \x1f before # a comment line: the RUN ends there too.
        "FROM python:3.13-alpine AS app\nRUN echo \\\n\x1f# note\nFROM python:3.12-slim\n",
    ],
)
def test_lines_that_do_not_continue(tmp_path, text):
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64", glibc="2.41")


def test_x1c_before_a_hash_is_not_a_comment(tmp_path):
    # Docker's isComment trims unicode.IsSpace only, so this line isn't a comment: it continues onto the FROM
    # below, and the final stage is the 3.13 one.
    text = "FROM python:3.13-alpine AS app\n\x1c# note \\\nFROM python:3.12-slim\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


@pytest.mark.parametrize(
    "text",
    [
        "FROM python:3.12-alpine AS build\nFROM --platform=linux/arm64 \\\n    python:3.11-slim \\\n# end\n",
        "FROM python:3.12-alpine AS build\nFROM --platform=linux/arm64 python:3.11-slim \\",
        "\ufeffFROM --platform=linux/arm64 python:3.11-slim\n",  # a UTF-8 BOM, which Docker strips
        "FROM --platform=linux/arm64 python:3.1\\\n1-slim\n",  # no space is added at the join
    ],
)
def test_final_line_edge_cases(tmp_path, text):
    path = tmp_path / "Dockerfile"
    path.write_text(text, encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.11", "linux-aarch64", glibc="2.41")


def test_escape_directive_rules(tmp_path):
    # An unknown directive-looking comment ends the directives, so the later escape= is ignored.
    text = "# version=2\n# escape=`\nFROM python:3.12-alpine\nRUN echo \\\nFROM python:3.11-slim\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "musl-x86_64", musl="1.2")
    with pytest.raises(DockerfileError, match="invalid escape directive"):
        read_dockerfile(_dockerfile(tmp_path, "# escape=x\nFROM python:3.12\n"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# escape=`\n# escape=\\\nFROM python:3.12\n", "only one escape parser directive"),
        ("# syntax=docker/dockerfile:1\n# syntax=docker/dockerfile:1\nFROM python:3.12\n", "only one syntax"),
        ("FROM python:3.12 AS a\nFROM\\\npython:3.11\n", "unknown instruction 'FROMpython:3.11'"),
        ("FROM python:3.12 AS a\nFROM\n", "FROM with no image"),
        ("FROM --platform linux/arm64 python:3.13\n", "--platform needs a value"),
        ("FROM --foo=bar python:3.13\n", "unknown flag '--foo=bar'"),
        ("FROM python:3.13-alpine AS\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM python:3.13 AS\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM python:3.13 builder\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM python:3.13 AS a b\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM --platform=linux/arm64 --platform=linux/amd64 python:3.13\n", "duplicate --platform flag"),
        ("FROM --platform= --platform=linux/arm64 python:3.13\n", "duplicate --platform flag"),
        ('FROM --platform="linux/arm64 python:3.13\n', "unterminated quote"),
        ("FROM --platform='linux/arm64 python:3.13\n", "unterminated quote"),
        ('FROM --platform="linux\\/arm64" python:3.13\n', "escapes in --platform aren't supported"),
        ("FROM python:3.13 AS\nFROM python:3.12\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM python:3.13 AS 1build\n", "invalid stage name '1build'"),
        ("FROM python:3.13 AS my@stage\n", "invalid stage name 'my@stage'"),
        ("FROM python:3.13 AS 1build\nFROM python:3.12\n", "invalid stage name '1build'"),  # any stage, as in Docker
        ("FROM --platform=linux/arm64\u00a0python:3.13\n", "no image"),
        ("FROM python:3.13 AS\u00a0stage\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM\u00a0python:3.13\n", "unknown instruction"),  # not a FROM to Docker at all
        ("FROM\u00a0--platform=linux/arm64 python:3.13\n", "unknown instruction"),
        ("FROM python:3.12\nFROM\u00a0python:3.13\n", "unknown instruction"),  # mustn't fall back to 3.12
        ("FROM python:3.12\x85FROM python:3.13\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM python:3.12\rFROM python:3.13", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM PYTHON:3.13\n", "repository name must be lowercase"),  # Docker rejects it, doesn't lowercase it
        ("FROM docker.io/library/PYTHON:3.13\n", "repository name must be lowercase"),
        ("FROM Docker.IO/library/python:3.12\n", "only the official python images"),  # host kept as written
        ("FROM python:3.13-SLIM\n", "unknown image variant 'SLIM'"),  # Docker Hub tags are case-sensitive
        ("FROM python:3.13-alpine3.1\u0662\n", "invalid tag"),
        ("FROM ghcr.io/astral-sh/uv:0.\u0665-python3.13-alpine\n", "invalid tag"),
        ("FROM python:3.1٣\n", "invalid tag"),  # an Arabic-Indic digit isn't allowed in a Docker tag
        # A heredoc the file ends in.
        ("FROM python:3.13\nRUN <<EOF\necho hi\n", "unterminated heredoc <<EOF"),
        ("FROM python:3.13\nRUN <<EOF\n\tEOF\n", "unterminated heredoc <<EOF"),  # only <<- strips tabs
        ("FROM python:3.13\nRUN <<EOF\nEOF \n", "unterminated heredoc <<EOF"),  # trailing spaces stay
        ("FROM python:3.13\nRUN <<A <<B\nA\n", "unterminated heredoc <<B"),
        ("FROM python:3.13\nRUN cat <<EOF \\", "unterminated heredoc <<EOF"),  # ends mid-continuation too
        ("FROM python:3.13\nRUN echo $((1 << BITS))\n", r"unterminated heredoc <<BITS\)\)"),
        # A heredoc under a `# syntax=` frontend that has none (1.3 and older, but 1.3-labs), or that wheelgap
        # can't place.
        *[
            (f"{directive}\n{_TWO_STAGES}RUN <<EOF\nFROM base\nEOF\n", "may read the heredocs in 'RUN <<EOF'")
            for directive in [
                "# syntax=docker/dockerfile:1.2",
                "# syntax=docker/dockerfile:1.2-labs",
                "# syntax=docker/dockerfile:1.3.1",
                "# syntax=docker/dockerfile:1.0-experimental",
                "# syntax=docker/dockerfile:1@sha256:" + "0" * 64,
                "# syntax=example.com/frontend:1",
                "#!/usr/bin/env buildctl\n# syntax=docker/dockerfile:1.2",
                "//syntax=docker/dockerfile:1.2",
            ]
        ],
        # 1.4 to 1.15 (and 1.3-labs) read some heredocs differently: a space after <<, ${...}, the --flags under
        # `# escape=` before 1.15, `[...] <<EOF` before 1.9.
        *[
            (f"# syntax=docker/dockerfile:{tag}\n{_TWO_STAGES}{line}\nFROM base\nEOF\n", "may read the heredocs")
            for tag, line in [
                ("1.7", "RUN cat << EOF"),
                ("1.15.0", "RUN cat <<\tEOF"),
                ("1.15", "RUN cat <<\rEOF"),  # \r splits words there, so `<<` is alone
                ("1.3-labs", "RUN echo $((1 << EOF))"),
                ("1.15-labs", "RUN cat <<EOF ${x:-y}"),
                ("1.4", "RUN cat <<EOF${x}"),
                ("1.14\n# escape=`", "RUN <<EOF"),
                ("1.8", 'RUN ["a"] <<EOF'),
            ]
        ],
        # Outside a heredoc, a Python import is a FROM with the wrong arguments.
        ("FROM python:3.13\nfrom app import main\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        # Docker trims only unicode.IsSpace, so \x1cFROM is an unknown instruction to it, as FROM\x1d is.
        ("FROM python:3.12-slim\n\x1cFROM python:3.13-alpine\n", "unknown instruction '\\\\x1cFROM'"),
        ("FROM python:3.12-slim\nFROM\x1d python:3.13-alpine\n", "unknown instruction 'FROM\\\\x1d'"),
        # With a space or tab after the \x1c, Docker's keyword is "\x1c" alone: still unknown, never a fallback.
        ("FROM python:3.12-slim\n\x1c FROM python:3.13-alpine\n", "unknown instruction '\\\\x1c'"),
        ("FROM python:3.12-slim\n\x1c\tFROM python:3.13-alpine\n", "unknown instruction '\\\\x1c'"),
        # The same when the file ends mid-continuation.
        ("FROM python:3.13-alpine\n\x1cFROM python:3.12-slim \\\n", "unknown instruction"),
        # A trailing \x1c is part of the image reference to Docker, which rejects the tag.
        ("FROM python:3.13-alpine\x1c\n", "invalid tag"),
    ],
)
def test_dockerfiles_docker_rejects(tmp_path, text, message):
    with pytest.raises(DockerfileError, match=message):
        read_dockerfile(_dockerfile(tmp_path, text))


@pytest.mark.parametrize("space", ["\v", "\f", "\r"])
def test_ascii_whitespace_inside_a_line_separates_words(tmp_path, space):
    # \v, \f and \r don't end a line for Docker (only \n does), but they do separate words, as spaces do.
    path = tmp_path / "Dockerfile"
    path.write_bytes(f"FROM python:3.13{space}AS build\nFROM build\n".encode())
    assert read_dockerfile(path) == ImageTarget("3.13", "linux-x86_64", glibc="2.41")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # \r\r\n (CRLF written in text mode on Windows): every trailing \r goes, so the continuation holds.
        (
            b"FROM python:3.13-alpine\nRUN echo hi \\\r\r\nFROM python:3.12-slim\n",
            ImageTarget("3.13", "musl-x86_64", musl="1.2"),
        ),
        (
            b"FROM python:3.12 AS a\r\nFROM --platform=linux/arm64 \\\r\n python:3.11-slim\r\n",
            ImageTarget("3.11", "linux-aarch64", glibc="2.41"),
        ),
    ],
)
def test_crlf_continuations(tmp_path, data, expected):
    path = tmp_path / "Dockerfile"
    path.write_bytes(data)
    assert read_dockerfile(path) == expected


def test_crlf_dockerfile_parses(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_bytes(b"FROM python:3.13-alpine\r\nRUN echo hello\r\n")
    assert read_dockerfile(path) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


def test_dockerfile_that_is_not_utf8_is_a_clear_error(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_bytes(b"FROM --platform=linux/arm64\xa0python:3.13\n")  # cp1252: Docker would read 0xa0 as a space
    with pytest.raises(DockerfileError, match="not UTF-8 \\(byte 0xa0 at offset 27\\), pass -p/-t instead"):
        read_dockerfile(path)


def test_a_build_arg_doesnt_make_a_non_ascii_arg_readable(tmp_path):
    # Docker splits ARG on any Unicode space: this line declares A and B, so a value for A can't settle B.
    path = tmp_path / "Dockerfile"
    path.write_text("ARG B=3.12\nARG A=1\u00a0B=3.13\nFROM python:${B}-slim\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match="non-ASCII characters in an ARG aren't supported"):
        read_dockerfile(path, {"A": "3"})


def _context(tmp_path, dockerfile, files=("requirements.txt",)):
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "Dockerfile"
    path.write_text(dockerfile, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("dockerfile", "files", "expected"),
    [
        # The python image's README: WORKDIR, then the file into it, then pip.
        (
            "FROM python:3.13\nWORKDIR /usr/src/app\nCOPY requirements.txt ./\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n",
            ["requirements.txt"],
            [(4, "requirements.txt")],
        ),
        # WORKDIR made /app, so the file lands inside it; an absolute -r; python -m pip; a continuation.
        (
            "FROM python:3.13-slim\nWORKDIR /app\nCOPY requirements.txt /app\nRUN python3 -m pip install \\\n"
            "    -r /app/requirements.txt\n",
            ["requirements.txt"],
            [(4, "requirements.txt")],
        ),
        # A file copied to a new name; a folder's contents; the later COPY of the same path wins.
        (
            "FROM python:3.13\nCOPY deps/base.txt /tmp/req.txt\nRUN pip install -r /tmp/req.txt\n",
            ["deps/base.txt"],
            [(3, "deps/base.txt")],
        ),
        (
            "FROM python:3.13\nWORKDIR /src\nCOPY . .\nRUN pip3 install -r reqs/prod.txt -r reqs/extra.txt\n",
            ["reqs/prod.txt", "reqs/extra.txt"],
            [(4, "reqs/prod.txt"), (4, "reqs/extra.txt")],
        ),
        (
            "FROM python:3.13\nWORKDIR /src\nCOPY a/requirements.txt ./\nCOPY --chown=app:app b/ ./\n"
            "RUN pip install -r requirements.txt\n",
            ["a/requirements.txt", "b/requirements.txt"],
            [(5, "b/requirements.txt")],
        ),
        # Earlier stages, other RUN lines and a COPY after the install don't matter.
        (
            "FROM node:22 AS assets\nRUN npm ci\nFROM python:3.13\nWORKDIR /app\nRUN apt-get update\n"
            "COPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY --from=assets /dist ./dist\n",
            ["requirements.txt"],
            [(7, "requirements.txt")],
        ),
    ],
)
def test_dockerfile_requirements_reads(tmp_path, dockerfile, files, expected):
    path = _context(tmp_path, dockerfile, files)
    assert dockerfile_requirements(path) == [(line, tmp_path / name) for line, name in expected]


@pytest.mark.parametrize(
    ("dockerfile", "message"),
    [
        ("FROM python:3.13\nRUN apt-get update\n", "the final stage \\(line 1\\) has no `RUN pip install` that"),
        ("FROM python:3.13\nRUN echo hi\n", "the final stage \\(line 1\\) has no `RUN pip install` that"),
        # A RUN that edits, replaces or removes the file between the COPY and the install.
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\nRUN echo uwsgi >> requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":5: requirements.txt may have been changed by line 4, which names requirements.txt",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN sed -i /pywin32/d requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":5: requirements.txt may have been changed by line 4, which names requirements.txt",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN apt-get update && rm requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":5: requirements.txt may have been changed by line 4, which names requirements.txt",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN python3 -mpip install uwsgi/\n",
            ":4: pip install uwsgi/: a project",
        ),
        (
            "FROM python:3.13\nARG PIP_INDEX_URL=https://user:secret@example.test/simple\nWORKDIR /app\nCOPY . .\n"
            "RUN pip install -r requirements.txt\n",
            ":2: ARG PIP_INDEX_URL may change what pip or uv installs$",
        ),
        (
            "ARG BASE\nFROM python:3.13-slim AS deps\nWORKDIR /app\nCOPY requirements.txt .\n"
            "RUN pip install -r requirements.txt\nFROM ${BASE}\nRUN pip install -r /app/requirements.txt\n",
            ":6: FROM \\$\\{BASE\\} uses a build argument wheelgap can't expand, and it may name an earlier stage "
            "\\(deps\\)",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt && pip download six\n",
            ":4: `pip download six`: only `install` is read of pip's commands",
        ),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install .\n", ":4: pip install \\.: a project folder"),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN uv pip install -r requirements.txt\n", ":4: .*needs --system"),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r $REQS\n", ":4: .*\\$REQS isn't a value"),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN --mount=type=secret,id=pipconf pip install "
            "-r requirements.txt\n",
            ":4: RUN --mount=type=secret with an install",
        ),
        (
            "FROM python:3.13\nRUN <<EOF\nmake all\nEOF\n",
            "has no `RUN pip install` that --dockerfile-inputs can read \\(it has commands wheelgap can't see "
            "into, which may install some: line 2\\)",
        ),
        # Another image may set a WORKDIR of its own.
        (
            "FROM ghcr.io/astral-sh/uv:python3.13-bookworm\nCOPY . .\nRUN pip install -r /requirements.txt\n",
            ":2: COPY to \\.: a relative",
        ),
        # Another image may set a WORKDIR of its own.
        (
            "FROM ghcr.io/astral-sh/uv:python3.13-bookworm\nRUN pip install -r requirements.txt\n",
            ":2: -r requirements.txt is relative",
        ),
        ("FROM python:3.13\nWORKDIR /app\nRUN pip install -r requirements.txt\n", "isn't copied from the build"),
        (
            "FROM python:3.13 AS build\nCOPY . /app\nONBUILD RUN pip install six\nFROM build\n"
            "RUN pip install -r /app/requirements.txt\n",
            ":3: ONBUILD in a stage the final stage builds on isn't read",
        ),
        (
            "FROM python:3.13 AS build\nFROM python:3.13\nWORKDIR /app\nCOPY --from=build /app/requirements.txt .\n"
            "RUN pip install -r requirements.txt\n",
            ":5: /app/requirements.txt may come from the COPY --from=build on line 4, and --dockerfile-inputs "
            "doesn't follow files copied from another stage or image",
        ),
        ("FROM python:3.13\nWORKDIR /app\nADD . .\nRUN pip install -r requirements.txt\n", ":3: ADD isn't read"),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY *.txt ./\nRUN pip install -r requirements.txt\n",
            ":3: only a plain `COPY SRC... DEST`",
        ),
        (
            "FROM python:3.13\nCOPY requirements.txt /tmp\nRUN pip install -r /tmp/requirements.txt\n",
            ":2: can't tell whether /tmp is a folder",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY missing.txt ./\nRUN pip install -r missing.txt\n",
            ":3: COPY missing.txt: not found",
        ),
        (
            "FROM python:3.13\nWORKDIR $HOME\nCOPY . .\nRUN pip install -r requirements.txt\n",
            ":2: WORKDIR \\$HOME: only a literal path",
        ),
        (
            "FROM python:3.13\nENV PIP_ONLY_BINARY=:all:\nWORKDIR /app\nCOPY . .\n"
            "RUN pip install -r requirements.txt\n",
            ":2: ENV PIP_ONLY_BINARY may change what pip or uv installs",
        ),
        (
            'FROM python:3.13\nSHELL ["/bin/bash", "-lc"]\nWORKDIR /app\nCOPY . .\n'
            "RUN pip install -r requirements.txt\n",
            ":2: SHELL .* changes how RUN lines run",
        ),
        ("# escape=`\nFROM python:3.13\nRUN pip install -r C:/r.txt\n", "default \\\\ escape"),
        # A frontend other than docker/dockerfile may read COPY and RUN differently.
        (
            "# syntax=example.com/frontend:1\nFROM python:3.13\nWORKDIR /app\nCOPY . .\n"
            "RUN pip install -r requirements.txt\n",
            "# syntax=example.com/frontend:1: --dockerfile-inputs only reads",
        ),
    ],
)
def test_dockerfile_requirements_refuses(tmp_path, dockerfile, message):
    path = _context(tmp_path, dockerfile)
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path)


def test_dockerfile_requirements_refuses_nested_includes(tmp_path):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n")
    # pip strips a byte order mark and reads abbreviated options, so these include base.txt too.
    # pip also reads one after other options on a line, and over a continuation.
    for text in (
        "-r base.txt\npurewheel==3.1\n",
        "\ufeff-r base.txt\n",
        "purewheel==3.1\n--req base.txt\n",
        "--pre -r base.txt\npurewheel==3.1\n",
        "--only-binary :all: --constraint=base.txt\n",
        "purewheel==3.1\n--pre \\\n-cbase.txt\n",
        # pip shlex-splits the options, so quoted or escaped ones count too.
        '--pre "-r" base.txt\n',
        "--pre '-rbase.txt'\n",
        "--pre \\-r base.txt\n",
        '--pre --"requirement"=base.txt\n',
    ):
        (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
        with pytest.raises(
            DockerfileError, match=":4: (?:/app/base.txt isn't copied|requirements.txt has an unreadable)"
        ):
            dockerfile_requirements(path)


@pytest.mark.parametrize(
    ("rules", "excluded_by"),
    [
        (".git\n__pycache__/\n", None),
        ("deps\n!deps/requirements.txt\n", None),
        ("deps/*\n!deps/requirements.txt\n", None),  # an ordinary exception below a folder is still one
        ("*.txt\n", None),  # a lone * does not cross a path separator
        ("**/requirement?.[t-v]xt\n", "**/requirement?.[t-v]xt"),
        ("**/requirements*.txt\n", "**/requirements*.txt"),
        ("/deps/\n", "/deps/"),
        ("deps\n!deps/requirements.txt\n**/requirements.txt\n", "**/requirements.txt"),
        ("**/requirements.txt\n!deps/requirements.txt\n", None),
        ("deps\n!deps\ndeps/requirements.txt\n", "deps/requirements.txt"),
        ("\ufeff# deps\r\n.\r\n", None),
        ("  # deps\n", None),
    ],
)
def test_dockerfile_requirements_dockerignore_matching(tmp_path, rules, excluded_by):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r deps/requirements.txt\n")
    requirements = tmp_path / "deps" / "requirements.txt"
    requirements.parent.mkdir()
    requirements.write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_bytes(rules.encode())
    if excluded_by:
        with pytest.raises(
            DockerfileError, match=r":3: COPY .: deps/requirements.txt is excluded .* \.dockerignore"
        ) as err:
            dockerfile_requirements(path)
        assert repr(excluded_by) in str(err.value)
    else:
        assert dockerfile_requirements(path) == [(4, requirements)]


def test_dockerfile_requirements_dockerignore_skips_ignored_pip_config(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "pip.conf").write_text(
        "[global]\nindex-url = https://example.invalid/simple\n", encoding="utf-8"
    )

    # Without .dockerignore, ci/pip.conf enters the image and refuses the project.
    with pytest.raises(
        DockerfileError,
        match=rf"^{re.escape(str(path))}:3: a pip config file may change what pip installs$",
    ):
        dockerfile_requirements(path)

    # With ci ignored, COPY . . never brings ci/pip.conf and the CLI checks requirements.txt.
    (tmp_path / ".dockerignore").write_text("ci\n", encoding="utf-8")
    assert dockerfile_requirements(path) == [(4, tmp_path / "requirements.txt")]
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--all"]) == 0
    out = capsys.readouterr().out
    assert "purewheel" in out
    assert "3.1" in out

    # Ignoring by glob also skips ci/pip.conf.
    (tmp_path / ".dockerignore").write_text("**/pip.conf\n", encoding="utf-8")
    assert dockerfile_requirements(path) == [(4, tmp_path / "requirements.txt")]

    # Re-including ci/pip.conf with a `!` exception refuses the project again.
    (tmp_path / ".dockerignore").write_text("ci\n!ci/pip.conf\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError,
        match=rf"^{re.escape(str(path))}:3: a pip config file may change what pip installs$",
    ):
        dockerfile_requirements(path)


@pytest.mark.parametrize("config_name", ["pyproject.toml", "uv.toml"])
def test_dockerfile_requirements_dockerignore_skips_ignored_uv_config(tmp_path, config_name):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN uv pip install --system -r requirements.txt\n",
        ["requirements.txt"],
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / config_name).write_text("", encoding="utf-8")

    with pytest.raises(
        DockerfileError,
        match=rf"^{re.escape(str(path))}:3: this COPY puts a uv\.toml or pyproject\.toml in the image",
    ):
        dockerfile_requirements(path, uv_installs=set())

    (tmp_path / ".dockerignore").write_text(f"{config_name}\n", encoding="utf-8")
    assert dockerfile_requirements(path, uv_installs=set()) == [(4, tmp_path / "requirements.txt")]


def test_dockerfile_requirements_dockerignore_rejects_wholly_ignored_copy_folder(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY deps/ /deps/\n"
        "COPY requirements.txt .\n"
        "RUN pip install -r requirements.txt\n",
        ["requirements.txt"],
    )
    (tmp_path / "deps").mkdir()
    (tmp_path / "deps" / "keep.txt").write_text("ok\n", encoding="utf-8")

    # Not ignored: read.
    assert dockerfile_requirements(path) == [(5, tmp_path / "requirements.txt")]

    # Wholly ignored: refused at the COPY line naming the rule.
    (tmp_path / ".dockerignore").write_text("deps\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError,
        match=(
            rf"^{re.escape(str(path))}:3: COPY deps/: "
            r"excluded from the build context by \.dockerignore:1 \('deps'\)$"
        ),
    ):
        dockerfile_requirements(path)

    # Ignored with an exception that re-includes a file under it: read.
    (tmp_path / ".dockerignore").write_text("deps\n!deps/keep.txt\n", encoding="utf-8")
    assert dockerfile_requirements(path) == [(5, tmp_path / "requirements.txt")]


@pytest.mark.parametrize(
    "pattern",
    [
        b"[unclosed",
        b"!",
        b"!/",
        b"[z-a]",
        b"\xff",
        # Moby's matcher cleans and trims a pattern a second time, so it reads these two as the exceptions
        # `!requirements.txt` and `!keep`, and trims the space that cleaning leaves at an end of the next two.
        b"./!requirements.txt",
        b"/!keep",
        b"./ requirements.txt",
        b"!requirements.txt /",
        # ...and cleans an exception with its ! on: this one becomes the rule `requirements.txt`, which excludes.
        b"!../../requirements.txt",
    ],
)
def test_dockerfile_requirements_refuses_unreadable_dockerignore(tmp_path, pattern):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n")
    (tmp_path / ".dockerignore").write_bytes(b"# comment\n" + pattern + b"\n")
    with pytest.raises(DockerfileError, match=r"\.dockerignore:2:"):
        dockerfile_requirements(path)


@pytest.mark.parametrize("pattern", ["a\\*b", "deps\\*.txt", "//deps", "c:deps", "!a\\*b"])
def test_dockerignore_refuses_patterns_a_windows_client_reads_differently(tmp_path, monkeypatch, pattern):
    ignore = tmp_path / ".dockerignore"
    ignore.write_text(pattern + "\n", encoding="utf-8")
    monkeypatch.setattr(dockerignore, "_WINDOWS_CLIENT", False)
    assert dockerignore.read_rules(ignore)
    monkeypatch.setattr(dockerignore, "_WINDOWS_CLIENT", True)
    with pytest.raises(dockerignore.IgnoreError, match=r"\.dockerignore:1: unsupported or invalid"):
        dockerignore.read_rules(ignore)


# Moby's own tests of the matcher BuildKit uses (TestMatches and matchTests), without the rows whose path ends in /:
# https://github.com/moby/patternmatcher/blob/5a6d8429a19bb6948a372ff19e86fe83599a04b7/patternmatcher_test.go
# Each is (lines of the ignore file, path, whether the path is left out). A path is checked as BuildKit's walk
# reaches it, through its parent folders, so `a*` leaves out ab/c.
_MOBY_MATCHES = [
    ("**", "file", True),
    ("**/", "file", True),
    ("**", "dir/file", True),
    ("**/", "dir/file", True),
    ("**/**", "dir/file", True),
    ("dir/**", "dir/file", True),
    ("dir/**", "dir/dir2/file", True),
    ("**/dir", "dir", True),
    ("**/dir", "dir/file", True),
    ("**/dir2/*", "dir/dir2/file", True),
    ("**/dir2/**", "dir/dir2/dir3/file", True),
    ("**file", "file", True),
    ("**file", "dir/file", True),
    ("**/file", "dir/file", True),
    ("**file", "dir/dir/file", True),
    ("**/file", "dir/dir/file", True),
    ("**/file*", "dir/dir/file", True),
    ("**/file*", "dir/dir/file.txt", True),
    ("**/file*txt", "dir/dir/file.txt", True),
    ("**/file*.txt", "dir/dir/file.txt", True),
    ("**/file*.txt*", "dir/dir/file.txt", True),
    ("**/**/*.txt", "dir/dir/file.txt", True),
    ("**/**/*.txt2", "dir/dir/file.txt", False),
    ("**/*.txt", "file.txt", True),
    ("**/**/*.txt", "file.txt", True),
    ("a**/*.txt", "a/file.txt", True),
    ("a**/*.txt", "a/dir/file.txt", True),
    ("a**/*.txt", "a/dir/dir/file.txt", True),
    ("a/*.txt", "a/dir/file.txt", False),
    ("a/*.txt", "a/file.txt", True),
    ("a/*.txt**", "a/file.txt", True),
    ("a[b-d]e", "ae", False),
    ("a[b-d]e", "ace", True),
    ("a[b-d]e", "aae", False),
    ("a[^b-d]e", "aze", True),
    (".*", ".foo", True),
    (".*", "foo", False),
    ("abc.def", "abcdef", False),
    ("abc.def", "abc.def", True),
    ("abc.def", "abcZdef", False),
    ("abc?def", "abcZdef", True),
    ("abc?def", "abcdef", False),
    ("**/foo/bar", "foo/bar", True),
    ("**/foo/bar", "dir/foo/bar", True),
    ("**/foo/bar", "dir/dir2/foo/bar", True),
    ("abc/**", "abc", False),
    ("abc/**", "abc/def", True),
    ("abc/**", "abc/def/ghi", True),
    ("**/.foo", ".foo", True),
    ("**/.foo", "bar.foo", False),
    ("a(b)c/def", "a(b)c/def", True),
    ("a(b)c/def", "a(b)c/xyz", False),
    ("a.|)$(}+{bc", "a.|)$(}+{bc", True),
    (
        "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl",
        "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl",
        True,
    ),
    ("dist/*.whl", "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl", True),
    ("**\n!util/docker/web", "util/docker/web/foo", False),
    ("**\n!util/docker/web\nutil/docker/web/foo", "util/docker/web/foo", True),
    (
        "**\n!dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl",
        "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl",
        False,
    ),
    ("**\n!dist/*.whl", "dist/proxy.py-2.4.0rc3.dev36+g08acad9-py3-none-any.whl", False),
    ("abc", "abc", True),
    ("*", "abc", True),
    ("*c", "abc", True),
    ("a*", "a", True),
    ("a*", "abc", True),
    ("a*", "ab/c", True),
    ("a*/b", "abc/b", True),
    ("a*/b", "a/c/b", False),
    ("a*b*c*d*e*/f", "axbxcxdxe/f", True),
    ("a*b*c*d*e*/f", "axbxcxdxexxx/f", True),
    ("a*b*c*d*e*/f", "axbxcxdxe/xxx/f", False),
    ("a*b*c*d*e*/f", "axbxcxdxexxx/fff", False),
    ("a*b?c*x", "abxbbxdbxebxczzx", True),
    ("a*b?c*x", "abxbbxdbxebxczzy", False),
    ("ab[c]", "abc", True),
    ("ab[b-d]", "abc", True),
    ("ab[e-g]", "abc", False),
    ("ab[^c]", "abc", False),
    ("ab[^b-d]", "abc", False),
    ("ab[^e-g]", "abc", True),
    ("a?b", "a\u263ab", True),
    ("a[^a]b", "a\u263ab", True),
    ("a???b", "a\u263ab", False),
    ("a[^a][^a][^a]b", "a\u263ab", False),
    ("[a-\u03b6]*", "\u03b1", True),
    ("*[a-\u03b6]", "A", False),
    ("a?b", "a/b", False),
    ("a*b", "a/b", False),
    ("*x", "xxx", True),
    # fsutil's walk (TestWalkerExclude, TestFSWalkNested, TestFilteredOpenInvert in filter_test.go): an exception
    # under an excluded folder is kept, and what a later rule excludes again under it is not.
    ("foo*\n!foo/bar2", "foo/bar2", False),
    ("foo*\n!foo/bar2", "foo2", True),
    ("foo\n!foo/bar", "foo/bar", False),
    ("foo\n!foo/bar\nfoo/bar/baz\n!foo/bar/baz/x", "foo/bar/baz/x", False),
    ("foo\n!foo/bar\nfoo/bar/baz\n!foo/bar/baz/x", "foo/bar/baz/y", True),
    ("**/bar\n!foo/bar/baz", "foo/bar/x", True),
]
# A backslash escapes the next character, except on a Windows client (see _WINDOWS_CLIENT).
_MOBY_ESCAPES = [
    ("a\\\\", "a\\", True),
    ("a\\*b", "a*b", True),
    ("a\\*b", "ab", False),
    ("[\\]a]", "]", True),
    ("[\\-]", "-", True),
    ("[x\\-]", "x", True),
    ("[x\\-]", "-", True),
    ("[x\\-]", "z", False),
    ("[\\-x]", "x", True),
    ("[\\-x]", "-", True),
    ("[\\-x]", "a", False),
]


def _ignored(tmp_path, lines, path):
    ignore = tmp_path / ".dockerignore"
    ignore.write_bytes((lines + "\n").encode())
    return dockerignore.ignored(path, dockerignore.read_rules(ignore)) is not None


@pytest.mark.parametrize(("lines", "path", "expected"), _MOBY_MATCHES)
def test_dockerignore_matches_as_moby_does(tmp_path, lines, path, expected):
    assert _ignored(tmp_path, lines, path) is expected


@pytest.mark.skipif(os.sep == "\\", reason="on Windows Docker reads a backslash as a path separator")
@pytest.mark.parametrize(("lines", "path", "expected"), _MOBY_ESCAPES)
def test_dockerignore_escapes_as_moby_does(tmp_path, lines, path, expected):
    assert _ignored(tmp_path, lines, path) is expected


# Patterns Moby's tests expect an error for: filepath.Match rejects them, or (the last) Go's regexp does.
@pytest.mark.parametrize(
    "pattern", ["[]a]", "[-]", "[x-]", "[-x]", "\\", "[a-b-c]", "[", "[^", "[^bc", "a[", "[Local-Only]/"]
)
def test_dockerignore_refuses_what_moby_rejects(tmp_path, pattern):
    ignore = tmp_path / ".dockerignore"
    ignore.write_text(pattern + "\n", encoding="utf-8")
    with pytest.raises(dockerignore.IgnoreError, match=r"\.dockerignore:1: unsupported or invalid"):
        dockerignore.read_rules(ignore)


@pytest.mark.parametrize(
    ("copies", "contents", "error"),
    [
        ("COPY requirements.txt ./", "-r base.txt", "base.txt isn't copied"),
        ("COPY requirements.txt ./\nCOPY base.txt /elsewhere/", "-r base.txt", "base.txt isn't copied"),
        ("COPY . ./\nCOPY other.txt /app/base.txt", "-r base.txt", "COPY puts .*other.txt there"),
        ("COPY . ./", "-r https://example.com/base.txt", "non-local include"),
        ("COPY . ./", "-c ${CONSTRAINTS}", "non-local include"),
        ("COPY . ./", "-e ./project -r base.txt", "unreadable -r or -c"),
        # pip would read /app/base.txt in the image; the requirements reader would read that path on this machine.
        ("COPY . ./", "-r /app/base.txt", "requirements.txt includes /app/base.txt by an absolute path"),
        # pip opens it through sub/, which the image doesn't have.
        ("COPY . ./", "-r sub/../base.txt", r"requirements.txt has an include with \.\. after a folder name"),
        ("COPY . ./", "-r requirements.txt", "requirements include cycle through requirements.txt"),
        ("COPY . ./", "-r", "requirements.txt: -r needs a file name"),
    ],
)
def test_dockerfile_requirements_refuses_unverifiable_includes(tmp_path, copies, contents, error):
    path = _context(tmp_path, f"FROM python:3.13\nWORKDIR /app\n{copies}\nRUN pip install -r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text(contents, encoding="utf-8")
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    run_line = 4 + copies.count("\n")
    with pytest.raises(DockerfileError, match=rf":{run_line}: .*{error}"):
        dockerfile_requirements(path)


def test_dockerfile_requirements_follows_includes_through_the_image(tmp_path):
    # pip reads an include beside the including file in the image: /app/requirements/base.txt, /app/constraints.txt.
    dockerfile = (
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements /app/requirements\n"
        "RUN pip install -r requirements/prod.txt\n"
    )
    path = _context(tmp_path, dockerfile, ["requirements/prod.txt", "requirements/base.txt", "constraints.txt"])
    prod = tmp_path / "requirements" / "prod.txt"
    prod.write_text("-r base.txt\n-c ../constraints.txt\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=":4: /app/constraints.txt isn't copied"):
        dockerfile_requirements(path)
    path.write_text(dockerfile.replace("RUN", "COPY constraints.txt /app/\nRUN"), encoding="utf-8")
    includes: dict = {}
    assert dockerfile_requirements(path, includes=includes) == [(5, prod)]
    assert includes == {(5, prod): (tmp_path / "requirements" / "base.txt", tmp_path / "constraints.txt")}


def test_dockerfile_requirements_includes_and_symlinks(tmp_path):
    context = tmp_path / "app"
    path = _context(
        context,
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt base.txt ./\nRUN pip install -r requirements.txt\n",
        ["requirements.txt", "base.txt", "real.txt"],
    )
    requirements, base, real = (context / name for name in ("requirements.txt", "base.txt", "real.txt"))
    requirements.write_text("-r base.txt\n", encoding="utf-8")
    try:
        (tmp_path / "link").symlink_to(context, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks aren't available here")
    # A build context reached through a symlink is read as before: only links below it count.
    linked = tmp_path / "link"
    includes: dict = {}
    assert dockerfile_requirements(linked / "Dockerfile", includes=includes) == [(4, linked / "requirements.txt")]
    assert includes == {(4, linked / "requirements.txt"): (linked / "base.txt",)}
    # Docker copies what a link named as a COPY source points to, so a linked file with no include is read, as before.
    requirements.unlink()
    requirements.symlink_to(real)
    assert dockerfile_requirements(path) == [(4, requirements)]
    # With an include it is refused: no file in an include chain is read through a link.
    real.write_text("-r base.txt\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=":4: requirements.txt is a symlink, which --dockerfile-inputs doesn't"):
        dockerfile_requirements(path)
    requirements.unlink()
    requirements.write_text("-r base.txt\n", encoding="utf-8")
    real.write_text("purewheel==3.1\n", encoding="utf-8")
    base.unlink()
    base.symlink_to(real)
    with pytest.raises(DockerfileError, match=":4: base.txt is a symlink, which --dockerfile-inputs doesn't read"):
        dockerfile_requirements(path)


def test_cli_dockerfile_inputs_follows_nested_requirements_and_constraints(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt base.txt prod.txt constraints.txt ./\n"
        "RUN pip install -r requirements.txt\n",
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r base.txt\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("-r prod.txt\n", encoding="utf-8")
    (tmp_path / "prod.txt").write_text("purewheel\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert main([str(requirements), "-p", "3.13", "-t", "linux-x86_64", "--json"]) == 0
    regular = json.loads(capsys.readouterr().out)
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["packages"] == regular["packages"]
    # A -c inside the requirements file is one of its includes, at any depth, not a -c of the RUN line.
    [dockerfile_input] = json.loads(result.out)["dockerfile_inputs"]
    assert dockerfile_input["constraints"] == []
    assert sorted(dockerfile_input["includes"]) == ["base.txt", "constraints.txt", "prod.txt"]
    assert f"{path}:4: RUN pip install -r reads " in result.err
    for name in ("requirements.txt", "base.txt", "prod.txt", "constraints.txt"):
        assert str(tmp_path / name) in result.err


def test_cli_dockerfile_inputs_has_constraints_and_includes_files(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n",
        ("requirements.txt", "constraints.txt", "reqs/base.txt"),
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r reqs/base.txt\npurewheel==3.1\n", encoding="utf-8")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    result = capsys.readouterr()
    dockerfile_input = json.loads(result.out)["dockerfile_inputs"][0]
    assert dockerfile_input["constraints"] == ["constraints.txt"]
    assert dockerfile_input["includes"] == ["reqs/base.txt"]


def test_cli_dockerfile_inputs_includes_leave_out_a_constraints_files_own_includes(tmp_path, capsys, fake_pypi):
    # A -r inside a -c file of the RUN line is read, and listed in the note, but isn't one of the install line's
    # includes: those are the files the requirements file itself reaches.
    path = _context(
        tmp_path,
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n",
        ("requirements.txt", "constraints.txt", "more.txt"),
    )
    (tmp_path / "constraints.txt").write_text("-r more.txt\n", encoding="utf-8")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    result = capsys.readouterr()
    [dockerfile_input] = json.loads(result.out)["dockerfile_inputs"]
    assert dockerfile_input["constraints"] == ["constraints.txt"]
    assert dockerfile_input["includes"] == []
    assert str(tmp_path / "more.txt") in result.err


@pytest.mark.parametrize(
    "flags",
    [
        "--upgrade",
        "-U",
        "--disable-pip-version-check",
        "--root-user-action=ignore",
        "--root-user-action=warn",
        "--root-user-action ignore",
        "--root-user-action warn",
        "--no-input",
        "-q",
        "--quiet",
        "--prefer-binary",
        "-U -q --no-input --no-cache-dir --root-user-action=ignore",
        "--no-compile",
        "--compile",
        "--progress-bar off",
        "--progress-bar=off",
        "--progress-bar raw",
        "--no-warn-script-location",
        "--no-color",
        "--break-system-packages",
        "--retries 5",
        "--retries=5",
        "--retries 0",
        "--timeout 60",
        "--timeout=60",
        "--timeout 7.5",
        "--default-timeout 100",
        "--default-timeout=100",
        "--no-deps",
    ],
)
def test_dockerfile_requirements_pip_install_flags(tmp_path, flags):
    # Before the file and after it: pip reads options anywhere on the line.
    for install in (f"{flags} -r requirements.txt", f"-r requirements.txt {flags}"):
        path = _context(tmp_path, f"FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install {install}\n")
        assert dockerfile_requirements(path) == [(4, tmp_path / "requirements.txt")]


@pytest.mark.parametrize(
    "setting",
    [
        "--index-url https://example.org",
        "--only-binary :all:",
        "--prefer-binary",  # the file below isn't pinned
        "--pre",
        "--user",
        "--target /opt/site",
        "--upgrade-strategy eager",
        "-e .",
        # pip reads combined short flags, glued values, = forms and unambiguous prefixes; wheelgap reads none of them.
        "-Uq",
        "-qq",
        "-Ur",
        "--upgr",
        "--requirement=requirements.txt",
        "--constraint=constraints.txt",
        "-cconstraints.txt",
        "-c",
        # pip exits on a value other than warn or ignore, and with no value the next word is the value.
        "--root-user-action=never",
        "--root-user-action never",
        "--root-user-action",
        # Flags that change resolution or target, or need their own reading
        "--require-hashes",
        "--no-binary :all:",
        "--prerelease allow",
        "--resolution lowest",
        "--prefix /opt",
        "--python /usr/bin/python",
        "--ignore-installed",
        "--force-reinstall",
        "--reinstall",
        "--no-build-isolation",
        "--platform manylinux_2_17_x86_64",
        "--python-version 3.12",
        # uv-only flags on a pip line
        "--compile-bytecode",
        "--no-progress",
        "--link-mode copy",
        "--strict",
        "--no-python-downloads",
        # Value-taking flags with missing or empty value
        "--progress-bar",
        "--progress-bar=",
        "--retries",
        "--retries=",
        "--timeout",
        "--timeout=",
        "--default-timeout",
        "--default-timeout=",
        # A value pip doesn't take: pip exits (cli/cmdoptions.py, pip 26.3), and a requirement isn't taken for one.
        "--progress-bar pretty",
        "--progress-bar=ascii",
        "--retries x",
        "--retries=-1",
        "--timeout six",
        "--default-timeout=never",
        "--root-user-action -U",
    ],
)
def test_dockerfile_requirements_refuses_install_flags_that_can_change_resolution(tmp_path, setting):
    path = _context(
        tmp_path,
        f"FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt {setting}\n",
        ["requirements.txt", "constraints.txt"],
    )
    (tmp_path / "requirements.txt").write_text("purewheel>=3\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=rf":4: pip install {re.escape(setting.split()[0])}[ :]"):
        dockerfile_requirements(path, constraints={})
    assert read_dockerfile(path).python == "3.13"


def test_dockerfile_requirements_refused_flag_message_lists_what_is_read(tmp_path):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install --pre -r requirements.txt\n")
    message = (
        ":4: pip install --pre: --dockerfile-inputs only reads -r FILE, -c FILE, --no-cache-dir, "
        r"--upgrade \(-U\), --disable-pip-version-check, --root-user-action, --no-input, --quiet \(-q\), "
        r"--no-compile, --compile, --progress-bar, --no-warn-script-location, --no-color, "
        r"--break-system-packages, --retries, --timeout, --default-timeout, --no-deps and, "
        "with pinned requirements, --prefer-binary"
    )
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path)


def test_dockerfile_requirements_prefer_binary_needs_pins_and_no_deps(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install --prefer-binary -r requirements.txt\n"
    )
    assert dockerfile_requirements(path) == [(4, tmp_path / "requirements.txt")]
    # --deps follows dependencies, which are ranges: pip may pick an older release with a wheel for those.
    with pytest.raises(DockerfileError, match=":4: pip install --prefer-binary .* and --deps isn't used"):
        dockerfile_requirements(path, dependencies=True)
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--deps"]) == 2
    assert f"{path}:4: pip install --prefer-binary makes pip pick an older release" in capsys.readouterr().err
    assert fake_pypi == []
    for text in ("purewheel==3.1\nnowheels>=0.9\n", "purewheel==3.*\n", "purewheel\n", "./vendored\n"):
        (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
        with pytest.raises(DockerfileError, match=":4: .*every requirement in requirements.txt is pinned with =="):
            dockerfile_requirements(path)


@pytest.mark.parametrize(
    ("dockerfile", "constraints_text", "error"),
    [
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt ./\n"
            "RUN pip install -r requirements.txt -c constraints.txt\n",
            "purewheel==3.1\n",
            ":4: /app/constraints.txt isn't copied from the build context",
        ),
        (
            "FROM ghcr.io/astral-sh/uv:python3.13-bookworm\nCOPY . /app/\n"
            "RUN pip install -r /app/requirements.txt --constraint constraints.txt\n",
            "purewheel==3.1\n",
            ":3: -c constraints.txt is relative, but no WORKDIR",
        ),
        # A -r or -c inside the constraints file is followed only when the image has that file, as one inside -r FILE.
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt constraints.txt ./\n"
            "RUN pip install -r requirements.txt -c constraints.txt\n",
            "-c more.txt\npurewheel==3.1\n",
            ":4: /app/more.txt isn't copied from the build context",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n",
            "--cons more.txt\npurewheel==3.1\n",
            ":4: constraints.txt has an unreadable -r or -c",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n"
            "RUN pip install -r requirements.txt\n",
            "purewheel==3.1\n",
            ":5: requirements.txt is installed again with other -c files than on line 4",
        ),
    ],
)
def test_dockerfile_install_constraint_file_refusals(tmp_path, dockerfile, constraints_text, error):
    path = _context(tmp_path, dockerfile)
    (tmp_path / "constraints.txt").write_text(constraints_text, encoding="utf-8")
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(path, constraints={})


def test_dockerfile_install_constraint_file_symlinks(tmp_path):
    # Refused and read exactly as a -r FILE is. Docker copies a link inside a copied folder as a link, so that isn't
    # read; it copies what a link named as a COPY source points to (BuildKit's COPY sets FollowSymlinks), so one that
    # stays inside the build context is read, and one that leaves it isn't.
    context = tmp_path / "app"
    folder = "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n"
    path = _context(context, folder)
    (context / "real.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    try:
        (context / "constraints.txt").symlink_to(context / "real.txt")
    except OSError:
        pytest.skip("symlinks aren't available here")
    with pytest.raises(DockerfileError, match=":3: constraints.txt is a symlink"):
        dockerfile_requirements(path, constraints={})
    path.write_text(folder.replace("COPY . .", "COPY requirements.txt constraints.txt ./"), encoding="utf-8")
    found: dict = {}
    assert dockerfile_requirements(path, constraints=found) == [(4, context / "requirements.txt")]
    assert found == {context / "requirements.txt": (context / "constraints.txt",)}
    (context / "constraints.txt").unlink()
    (context / "constraints.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(DockerfileError, match=":3: COPY constraints.txt: a symlink out of the build context"):
        dockerfile_requirements(path, constraints={})


def test_dockerfile_requirements_never_drops_install_constraints(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n"
        "RUN pip install -c constraints.txt -r requirements.txt -r dev.txt\n",
        ["requirements.txt", "constraints.txt", "dev.txt"],
    )
    # A caller that doesn't take the constraints files would check the requirements without them.
    with pytest.raises(DockerfileError, match=":4: pip install -c FILE isn't read here"):
        dockerfile_requirements(path)
    found: dict = {}
    assert dockerfile_requirements(path, constraints=found) == [
        (4, tmp_path / "requirements.txt"),
        (5, tmp_path / "dev.txt"),
    ]
    assert found == {
        tmp_path / "requirements.txt": (tmp_path / "constraints.txt",),
        tmp_path / "dev.txt": (tmp_path / "constraints.txt",),
    }


def test_load_inputs_applies_install_constraints_like_a_constraint_line(tmp_path):
    requirements, constraints, same = (tmp_path / name for name in ("requirements.txt", "constraints.txt", "same.txt"))
    requirements.write_text("purewheel\nnowheels>=0.5\n", encoding="utf-8")
    same.write_text("-c constraints.txt\npurewheel\nnowheels>=0.5\n", encoding="utf-8")
    # A -r inside a constraints file is requirements, as pip reads it: those packages are kept too.
    constraints.write_text("-r more.txt\npurewheel==3.1\nnowheels<1\n", encoding="utf-8")
    (tmp_path / "more.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    (source,) = load_inputs([str(requirements)], requirements_constraints={requirements: (constraints,)})
    (expected,) = load_inputs([str(same)])

    def rows(src):
        return [(p.name, p.version, p.spec) for p in src.packages]

    assert rows(source) == rows(expected)
    assert rows(source)[:2] == [("fastlib", "2.0.0", None), ("purewheel", "3.1", None)]
    # The source is still a requirements file with a path: --fix and --github-annotations go by both.
    assert (source.kind, source.path) == ("requirements", requirements)
    assert (source.constraint_specs, source.specs) == (expected.constraint_specs, expected.specs)
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    with pytest.raises(
        SourceError, match=r"uv.lock: constraints files \(pip install -c\) only apply to a requirements"
    ):
        load_inputs([str(lock)], requirements_constraints={lock: (constraints,)})


def test_cli_dockerfile_inputs_applies_install_constraints(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt constraints.txt ./\n"
        "RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt\n",
    )
    requirements = tmp_path / "requirements.txt"
    constraints = tmp_path / "constraints.txt"
    requirements.write_text("purewheel\n", encoding="utf-8")
    constraints.write_text("purewheel==3.1\n", encoding="utf-8")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    captured = capsys.readouterr()
    actual = json.loads(captured.out)["packages"]
    assert f"wheelgap: note: {path}:4: RUN pip install -r reads {requirements}, -c {constraints}\n" in captured.err
    requirements.write_text("-c constraints.txt\npurewheel\n", encoding="utf-8")
    assert main([str(requirements), "-p", "3.13", "-t", "linux-x86_64", "--json"]) == 0
    assert actual == json.loads(capsys.readouterr().out)["packages"]
    assert actual[0]["version"] == "3.1"


def test_dockerfile_install_constraint_file_includes_are_verified(tmp_path, capsys, fake_pypi):
    # A -r inside a constraints file is requirements (pip's rule): its packages are installed, so the file must be
    # the one the image has beside /app/constraints.txt, and its packages are checked.
    dockerfile = (
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt constraints.txt ./\n"
        "RUN pip install -r requirements.txt -c constraints.txt\n"
    )
    path = _context(tmp_path, dockerfile, ["requirements.txt", "constraints.txt", "more.txt"])
    requirements, constraints, more = (tmp_path / name for name in ("requirements.txt", "constraints.txt", "more.txt"))
    requirements.write_text("purewheel\n", encoding="utf-8")
    constraints.write_text("-r more.txt\npurewheel==3.1\n", encoding="utf-8")
    more.write_text("fastlib==2.0.0\n", encoding="utf-8")
    # more.txt is in the build context but not in the image: pip would stop the build, so nothing is read from disk.
    with pytest.raises(DockerfileError, match=":4: /app/more.txt isn't copied from the build context"):
        dockerfile_requirements(path, constraints={})
    path.write_text(dockerfile.replace("constraints.txt ./", "constraints.txt more.txt ./"), encoding="utf-8")
    includes: dict = {}
    found: dict = {}
    assert dockerfile_requirements(path, includes=includes, constraints=found) == [(4, requirements)]
    assert found == {requirements: (constraints,)}
    assert includes == {(4, requirements): (), (4, constraints): (more,)}
    (source,) = load_inputs([str(requirements)], requirements_constraints=found)
    assert [(p.name, p.version) for p in source.packages] == [("fastlib", "2.0.0"), ("purewheel", "3.1")]
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    captured = capsys.readouterr()
    assert sorted((p["name"], p["version"]) for p in json.loads(captured.out)["packages"]) == [
        ("fastlib", "2.0.0"),
        ("purewheel", "3.1"),
    ]
    note = (
        f"wheelgap: note: {path}:4: RUN pip install -r reads {requirements}, -c {constraints} (which includes {more})\n"
    )
    assert note in captured.err


def test_dockerfile_requirements_prefer_binary_needs_pins_in_included_files(tmp_path):
    dockerfile = (
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt base.txt constraints.txt more.txt ./\n"
        "RUN pip install --prefer-binary -r requirements.txt\n"
    )
    path = _context(tmp_path, dockerfile, ["requirements.txt", "base.txt", "constraints.txt", "more.txt"])
    requirements, base, constraints, more = (
        tmp_path / name for name in ("requirements.txt", "base.txt", "constraints.txt", "more.txt")
    )
    requirements.write_text("-r base.txt\npurewheel==3.1\n", encoding="utf-8")
    base.write_text("nowheels==0.9\n", encoding="utf-8")
    assert dockerfile_requirements(path) == [(4, requirements)]
    # requirements.txt's own lines are pinned; the file it includes isn't.
    base.write_text("nowheels>=0.9\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError, match=r":4: .*every requirement in base.txt \(read with requirements.txt\) is pinned with =="
    ):
        dockerfile_requirements(path)
    # A -r inside the line's constraints file is requirements too, so it needs pins as well.
    path.write_text(dockerfile.replace("-r requirements.txt", "-r requirements.txt -c constraints.txt"), "utf-8")
    base.write_text("nowheels==0.9\n", encoding="utf-8")
    constraints.write_text("-r more.txt\n", encoding="utf-8")
    more.write_text("fastlib>=2\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError, match=r":4: .*every requirement in more.txt \(read with requirements.txt\) is pinned with =="
    ):
        dockerfile_requirements(path, constraints={})
    # A requirement the constraints file pins with == installs that one release, with or without --prefer-binary.
    more.write_text("fastlib==2.0.0\n", encoding="utf-8")
    base.write_text("nowheels\n", encoding="utf-8")
    constraints.write_text("-r more.txt\nnowheels==0.9\n", encoding="utf-8")
    assert dockerfile_requirements(path, constraints={}) == [(4, requirements)]
    with pytest.raises(DockerfileError, match=":4: pip install --prefer-binary .* and --deps isn't used"):
        dockerfile_requirements(path, constraints={}, dependencies=True)
    constraints.write_text("-r more.txt\nnowheels>=0.9\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError, match=r":4: .*every requirement in base.txt \(read with requirements.txt\) is pinned with =="
    ):
        dockerfile_requirements(path, constraints={})


def test_dockerfile_requirements_install_constraints_and_in_file_constraints_share_a_chain(tmp_path):
    # constraints.txt is named on the RUN line and by prod.txt (from one folder down), and has a -c of its own.
    dockerfile = (
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements /app/requirements\nCOPY constraints.txt pins.txt /app/\n"
        "RUN pip install -r requirements/prod.txt -c constraints.txt\n"
    )
    path = _context(
        tmp_path,
        dockerfile,
        ["requirements/prod.txt", "requirements/base.txt", "constraints.txt", "pins.txt", "other.txt"],
    )
    prod, base = tmp_path / "requirements" / "prod.txt", tmp_path / "requirements" / "base.txt"
    constraints, pins = tmp_path / "constraints.txt", tmp_path / "pins.txt"
    prod.write_text("-c ../constraints.txt\n-r base.txt\npurewheel\n", encoding="utf-8")
    base.write_text("nowheels\n", encoding="utf-8")
    constraints.write_text("-c pins.txt\npurewheel==3.1\n", encoding="utf-8")
    pins.write_text("nowheels==0.9\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("nowheels==0.8\n", encoding="utf-8")
    includes: dict = {}
    found: dict = {}
    assert dockerfile_requirements(path, includes=includes, constraints=found) == [(5, prod)]
    assert found == {prod: (constraints,)}
    assert includes == {(5, constraints): (pins,), (5, prod): (constraints, pins, base)}
    (source,) = load_inputs([str(prod)], requirements_constraints=found)
    assert [(p.name, p.version) for p in source.packages] == [("nowheels", "0.9"), ("purewheel", "3.1")]
    # The image gets another file where constraints.txt's -c looks: pip would pin nowheels from that one.
    path.write_text(dockerfile.replace("RUN", "COPY other.txt /app/pins.txt\nRUN"), encoding="utf-8")
    with pytest.raises(DockerfileError, match=":6: constraints.txt includes pins.txt, but COPY puts .*other.txt there"):
        dockerfile_requirements(path, constraints={})


_COPY_ALL = "FROM python:3.13\nWORKDIR /app\nCOPY . .\n"


@pytest.mark.parametrize(
    ("dockerfile", "files", "rules", "error"),
    [
        # A file the installed file includes: the image has requirements.txt but not base.txt, so pip stops the build.
        (
            _COPY_ALL + "RUN pip install -r requirements.txt\n",
            {"requirements.txt": "-r base.txt\n"},
            "base.txt\n",
            r":3: COPY \.: base\.txt is excluded from the build context by \.dockerignore:1 \('base\.txt'\)",
        ),
        # The same two levels down, and with -c: no file in the chain is read once one is left out.
        (
            _COPY_ALL + "RUN pip install -r requirements.txt\n",
            {"requirements.txt": "-r more.txt\n", "more.txt": "-c base.txt\npurewheel\n"},
            "# local pins\n**/base.txt\n",
            r":3: COPY \.: base\.txt is excluded from the build context by \.dockerignore:2 \('\*\*/base\.txt'\)",
        ),
        # The -c file on the RUN line.
        (
            _COPY_ALL + "RUN pip install -r requirements.txt -c constraints.txt\n",
            {"constraints.txt": "purewheel==3.1\n"},
            "constraints.txt\n",
            r":3: COPY \.: constraints\.txt is excluded from the build context by \.dockerignore:1 ",
        ),
        # A -r inside the -c file: pip installs what it names, so it must be in the image too.
        (
            _COPY_ALL + "RUN pip install -r requirements.txt -c constraints.txt\n",
            {"constraints.txt": "-r base.txt\npurewheel==3.1\n"},
            "base.txt\n",
            r":3: COPY \.: base\.txt is excluded from the build context by \.dockerignore:1 \('base\.txt'\)",
        ),
        # --prefer-binary parses the files to look for pins, after the excluded one is refused.
        (
            _COPY_ALL + "RUN pip install --prefer-binary -r requirements.txt\n",
            {"requirements.txt": "-r base.txt\npurewheel==3.1\n", "base.txt": "nowheels==0.9\n"},
            "base.txt\n",
            r":3: COPY \.: base\.txt is excluded from the build context by \.dockerignore:1 \('base\.txt'\)",
        ),
        # A source COPY names that the rules leave out is "not found" to BuildKit, and the build fails on that line,
        # whether or not anything reads the file.
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt base.txt ./\nRUN pip install -r requirements.txt\n",
            {"requirements.txt": "-r base.txt\n"},
            "base.txt\n",
            r":3: COPY base\.txt: excluded from the build context by \.dockerignore:1 \('base\.txt'\)",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt base.txt ./\nRUN pip install -r requirements.txt\n",
            {},
            "base.txt\n",
            r":3: COPY base\.txt: excluded from the build context by \.dockerignore:1 \('base\.txt'\)",
        ),
    ],
)
def test_dockerfile_requirements_refuses_includes_the_ignore_rules_leave_out(tmp_path, dockerfile, files, rules, error):
    path = _context(tmp_path, dockerfile, ["requirements.txt", "base.txt"])
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    # Without the ignore file every one of these is read: the files are all in the folder.
    assert dockerfile_requirements(path, constraints={}, includes={}) == [(4, tmp_path / "requirements.txt")]
    (tmp_path / ".dockerignore").write_bytes(rules.encode())
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(path, constraints={}, includes={})


def test_dockerfile_requirements_follows_an_include_a_later_rule_puts_back(tmp_path):
    path = _context(tmp_path, _COPY_ALL + "RUN pip install -r requirements.txt\n", ["requirements.txt", "base.txt"])
    requirements, base = tmp_path / "requirements.txt", tmp_path / "base.txt"
    requirements.write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_bytes(b"*.txt\n!requirements.txt\n")
    with pytest.raises(DockerfileError, match=r":3: COPY \.: base\.txt is excluded .* \.dockerignore:1 \('\*\.txt'\)"):
        dockerfile_requirements(path)
    # The last rule that matches decides, as in the build: base.txt is in the image again.
    (tmp_path / ".dockerignore").write_bytes(b"*.txt\n!requirements.txt\n!base.txt\n")
    includes: dict = {}
    assert dockerfile_requirements(path, includes=includes) == [(4, requirements)]
    assert includes == {(4, requirements): (base,)}
    # ...and a rule after that one takes it out again.
    (tmp_path / ".dockerignore").write_bytes(b"*.txt\n!requirements.txt\n!base.txt\nbase.*\n")
    with pytest.raises(DockerfileError, match=r":3: COPY \.: base\.txt is excluded .* \.dockerignore:4 \('base\.\*'\)"):
        dockerfile_requirements(path)


def test_dockerfile_requirements_refuses_a_linked_folder_when_ignore_rules_exist(tmp_path):
    path = _context(tmp_path, _COPY_ALL + "RUN pip install -r link/req.txt\n", ["realdir/req.txt"])
    try:
        (tmp_path / "link").symlink_to("realdir", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks aren't available here")
    # The image gets realdir and the link to it, so pip reads link/req.txt.
    assert dockerfile_requirements(path) == [(4, tmp_path / "link" / "req.txt")]
    (tmp_path / ".dockerignore").write_bytes(b"# nothing is left out\n")
    assert dockerfile_requirements(path) == [(4, tmp_path / "link" / "req.txt")]
    # With realdir left out, /app/link points nowhere in the image and pip fails: realdir/req.txt isn't read.
    (tmp_path / ".dockerignore").write_bytes(b"realdir\n")
    with pytest.raises(
        DockerfileError, match=":3: link is a symlink, which --dockerfile-inputs doesn't read when an ignore file has"
    ):
        dockerfile_requirements(path)


def test_dockerfile_requirements_refuses_a_linked_file_when_ignore_rules_exist(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt ./\nRUN pip install -r requirements.txt\n",
        ["requirements.txt", "real.txt"],
    )
    requirements = tmp_path / "requirements.txt"
    requirements.unlink()
    try:
        requirements.symlink_to("real.txt")
    except OSError:
        pytest.skip("symlinks aren't available here")
    assert dockerfile_requirements(path) == [(4, requirements)]
    (tmp_path / ".dockerignore").write_bytes(b"# nothing is left out\n")
    assert dockerfile_requirements(path) == [(4, requirements)]
    # The link is matched by its own name, but what it points to isn't in the build context.
    (tmp_path / ".dockerignore").write_bytes(b"real.txt\n")
    with pytest.raises(
        DockerfileError,
        match=r":3: COPY requirements\.txt: requirements\.txt is a symlink, .* when an ignore file has rules",
    ):
        dockerfile_requirements(path)


def test_cli_dockerfile_inputs_reads_nothing_from_an_excluded_include(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, _COPY_ALL + "RUN pip install -r requirements.txt\n", ["requirements.txt", "base.txt"])
    (tmp_path / "requirements.txt").write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_bytes(b"base.txt\n")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 2
    captured = capsys.readouterr()
    assert f"{path}:3: COPY .: base.txt is excluded from the build context by .dockerignore:1 ('base.txt')" in (
        captured.err
    )
    assert fake_pypi == []  # nowheels, which the image never gets, isn't looked up


@pytest.mark.parametrize("dest", ["/bin/", "/usr/local/bin/"])
@pytest.mark.parametrize("binaries", ["/uv", "/uv /uvx"])
def test_cli_dockerfile_inputs_reads_uv_pip_system_install(tmp_path, capsys, fake_pypi, dest, binaries):
    path = _context(
        tmp_path,
        f"FROM python:3.13\nCOPY --from=ghcr.io/astral-sh/uv:0.13.9 {binaries} {dest}\n"
        "WORKDIR /app\nCOPY requirements.txt .\nRUN uv pip install --system --no-cache -r requirements.txt\n",
    )
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    result = capsys.readouterr()
    assert f"{path}:5: RUN uv pip install -r reads " in result.err
    assert main([str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64", "--json"]) == 0
    assert json.loads(result.out)["packages"] == json.loads(capsys.readouterr().out)["packages"]


@pytest.mark.parametrize(
    ("copy", "run", "message"),
    [
        (
            "COPY . ./",
            "uv pip install --system --require-hashes -r requirements.txt",
            ":4: uv pip install --require-hashes",
        ),
        (
            "COPY . ./",
            "uv pip install --system -r requirements.txt",
            ":3: this COPY puts a uv.toml or pyproject.toml in the image, and uv reads settings from them: "
            "copy the requirements file alone and run uv pip install before this COPY",
        ),
        ("COPY --from=ghcr.io/astral-sh/uv /other /bin/", "uv pip install --system -r requirements.txt", ":3: COPY"),
        ("COPY --from=other /uv /bin/", "uv pip install --system -r requirements.txt", ":3: COPY"),
        # uv reads these by name, as a lock and as a script with inline metadata, not as requirements files.
        ("COPY requirements.txt pylock.toml", "uv pip install --system -r pylock.toml", ":4: .*-r pylock.toml: uv"),
        ("COPY requirements.txt deps.py", "uv pip install --system -r deps.py", ":4: .*-r deps.py: uv reads"),
        ("COPY requirements.txt reqs", "uv pip install --system -r reqs", ":4: .*-r reqs: uv reads"),
        ("COPY requirements.txt ./", "uv pip install --system -r requirements.txt -c cons.py", ":4: .*-c cons.py: uv"),
        # Not this mode's commands: uv sync, uv add and uv pip sync install by other rules.
        ("COPY requirements.txt ./", "uv sync", ":4: `uv sync` isn't read by --dockerfile-inputs"),
        ("COPY requirements.txt ./", "uv add -r requirements.txt", ":4: `uv add -r requirements.txt` isn't read"),
        ("COPY requirements.txt ./", "uv pip sync --system requirements.txt", ":4: .*only `install` is read of uv pip"),
        ("COPY requirements.txt ./", "uv pip install --system --python 3.12 -r requirements.txt", ":4: .*--python"),
        ("COPY requirements.txt ./", "uv pip install --system --index-url=https://e.test -r requirements.txt", ":4:"),
    ],
)
def test_dockerfile_inputs_refuses_unsupported_uv_installs(tmp_path, copy, run, message):
    path = _context(tmp_path, f"FROM python:3.13\nWORKDIR /app\n{copy}\nRUN {run}\n")
    (tmp_path / "uv.toml").write_text("no-binary = true\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path)


def test_cli_dockerfile_inputs_checks_a_uv_install_as_uv_installs_it(tmp_path, capsys, fake_pypi, monkeypatch):
    """uv ignores a Requires-Python upper bound that makes pip refuse a pin, so the same file gets uv's answer under
    `RUN uv pip install` and pip's under `RUN pip install`."""
    from conftest import FAKE_INDEX

    filename = "purewheel-3.1-py3-none-any.whl"
    monkeypatch.setitem(FAKE_INDEX, "purewheel", {"3.1": [{"filename": filename, "requires_python": ">=3.8,<3.12"}]})
    dockerfile = "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\nRUN {} -r requirements.txt\n"
    path = _context(tmp_path, dockerfile.format("uv pip install --system"))
    argv = ["--dockerfile", str(path), "--dockerfile-inputs", "--json", "--no-cache"]
    assert main(argv) == 0
    result = capsys.readouterr()
    package = json.loads(result.out)["packages"][0]
    assert package["results"] == {"linux-x86_64": "wheel"}
    assert package["pip_refuses"] == {filename: "requires Python >=3.8,<3.12"}
    assert "note: 1 package is checked as uv installs it, though pip would refuse it" in result.err
    path.write_text(dockerfile.format("pip install"), encoding="utf-8")
    assert main(argv) == 1
    package = json.loads(capsys.readouterr().out)["packages"][0]
    assert package["results"] == {"linux-x86_64": "missing"}
    assert "pip_refuses" not in package


_UV_RUN = "FROM python:3.13\nWORKDIR /app\nCOPY {} ./\nRUN uv pip install --system {}\n"


@pytest.mark.parametrize(
    "flags",
    [
        "-U",
        "--upgrade",
        "-q",
        "--quiet",
        "--no-cache",
        "--no-cache-dir",
        "--compile-bytecode",
        "--compile",
        "--no-progress",
        "--link-mode copy",
        "--link-mode=copy",
        "--break-system-packages",
        "--strict",
        "--no-python-downloads",
        "--no-deps",
        "-U -q --no-cache",
        "-U --compile-bytecode --link-mode copy --no-deps",
    ],
)
def test_dockerfile_requirements_reads_the_flags_uv_has(tmp_path, flags):
    # uv spells the file option --requirements, and takes pip's --requirement too.
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", f"{flags} --requirements requirements.txt"))
    requirements = tmp_path / "requirements.txt"
    uv_installs: set = set()
    assert dockerfile_requirements(path, uv_installs=uv_installs) == [(4, requirements)]
    assert uv_installs == {(4, requirements)}
    # A caller that doesn't ask which files uv installs would check this one by pip's rules: it gets an error.
    with pytest.raises(DockerfileError, match=":4: uv pip install -r FILE isn't read here"):
        dockerfile_requirements(path)


@pytest.mark.parametrize(
    "flags",
    [
        "--no-input",
        "--disable-pip-version-check",
        "--root-user-action ignore",
        "--root-user-action=ignore",
        "--prefer-binary",
        "--no-compile",
        "--progress-bar off",
        "--progress-bar=off",
        "--no-warn-script-location",
        "--no-color",
        "--retries 5",
        "--retries=5",
        "--timeout 60",
        "--timeout=60",
        "--default-timeout 100",
        "--default-timeout=100",
    ],
)
def test_dockerfile_requirements_refuses_pip_only_flags_on_a_uv_line(tmp_path, flags):
    # uv has none of these (--disable-pip-version-check only as a warning that it does nothing).
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", f"{flags} -r requirements.txt"))
    message = (
        f":4: uv pip install {re.escape(flags.split()[0])}: --dockerfile-inputs only reads --system, -r FILE, "
        r"-c FILE, --no-cache, --no-cache-dir, --upgrade \(-U\), --quiet \(-q\), "
        r"--compile-bytecode \(--compile\), --no-progress, --link-mode, --break-system-packages, --strict, "
        r"--no-python-downloads and --no-deps for uv"
    )
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path, uv_installs=set())
    # The same flags on a pip line are read.
    path.write_text(path.read_text(encoding="utf-8").replace("uv pip install --system", "pip install"), "utf-8")
    assert dockerfile_requirements(path) == [(4, tmp_path / "requirements.txt")]


@pytest.mark.parametrize(
    "flags",
    [
        "--compile-bytecode",
        "--no-progress",
        "--link-mode copy",
        "--link-mode=copy",
        "--strict",
        "--no-python-downloads",
    ],
)
def test_dockerfile_requirements_refuses_uv_only_flags_on_a_pip_line(tmp_path, flags):
    # pip has none of these uv flags.
    path = _context(
        tmp_path, f"FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install {flags} -r requirements.txt\n"
    )
    message = (
        f":4: pip install {re.escape(flags.split()[0])}: --dockerfile-inputs only reads -r FILE, -c FILE, "
        r"--no-cache-dir, --upgrade \(-U\), --disable-pip-version-check, --root-user-action, --no-input, "
        r"--quiet \(-q\), --no-compile, --compile, --progress-bar, --no-warn-script-location, --no-color, "
        r"--break-system-packages, --retries, --timeout, --default-timeout, --no-deps and, "
        "with pinned requirements, --prefer-binary"
    )
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path)
    # The same flags on a uv line are read.
    path.write_text(_UV_RUN.format("requirements.txt", f"{flags} -r requirements.txt"), "utf-8")
    assert dockerfile_requirements(path, uv_installs=set()) == [(4, tmp_path / "requirements.txt")]


@pytest.mark.parametrize("flags", ["--link-mode bogus", "--link-mode=bogus", "--link-mode Copy", "--link-mode"])
def test_dockerfile_requirements_refuses_a_link_mode_uv_does_not_take(tmp_path, flags):
    # uv exits on a --link-mode other than clone, copy, hardlink or symlink (`uv pip install --help`, uv 0.13.1),
    # and the word after the flag is never taken for a requirement.
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", f"{flags} -r requirements.txt"))
    with pytest.raises(DockerfileError, match=rf":4: uv pip install {re.escape(flags.split()[0])}: "):
        dockerfile_requirements(path, uv_installs=set())


def test_dockerfile_requirements_no_deps_read_without_deps_refused_with_deps(tmp_path):
    # pip: read without --deps
    path_pip = _context(
        tmp_path / "pip", "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install --no-deps -r requirements.txt\n"
    )
    assert dockerfile_requirements(path_pip) == [(4, tmp_path / "pip" / "requirements.txt")]
    # pip: refused with --deps
    with pytest.raises(DockerfileError, match=r":4: pip install --no-deps installs no dependencies, .*: drop --deps"):
        dockerfile_requirements(path_pip, dependencies=True)

    # uv: read without --deps
    path_uv = _context(tmp_path / "uv", _UV_RUN.format("requirements.txt", "--no-deps -r requirements.txt"))
    assert dockerfile_requirements(path_uv, uv_installs=set()) == [(4, tmp_path / "uv" / "requirements.txt")]
    # uv: refused with --deps
    with pytest.raises(
        DockerfileError, match=r":4: uv pip install --no-deps installs no dependencies, .*: drop --deps"
    ):
        dockerfile_requirements(path_uv, dependencies=True, uv_installs=set())


def test_dockerfile_requirements_uv_needs_every_requirement_pinned(tmp_path):
    files = ["requirements.txt", "base.txt", "constraints.txt"]
    path = _context(tmp_path, _UV_RUN.format(" ".join(files), "-r requirements.txt"), files)
    requirements, base, constraints = (tmp_path / name for name in files)
    assert dockerfile_requirements(path, uv_installs=set()) == [(4, requirements)]
    # wheelgap picks a release for a range as pip does, and uv can pick another: a range is refused, not guessed.
    for text in ("purewheel\n", "purewheel>=3,<4\n"):
        requirements.write_text(text, encoding="utf-8")
        with pytest.raises(
            DockerfileError,
            match=":4: uv pip install: purewheel in requirements.txt isn't pinned with ==, and uv and pip can pick",
        ):
            dockerfile_requirements(path, uv_installs=set())
    # The same in a file it includes.
    requirements.write_text("-r base.txt\npurewheel==3.1\n", encoding="utf-8")
    base.write_text("nowheels>=0.9\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError,
        match=r":4: uv pip install: nowheels in base.txt \(read with requirements.txt\) isn't pinned with ==",
    ):
        dockerfile_requirements(path, uv_installs=set())
    # pip installs the same files without pins, and wheelgap reads them as before.
    uv_line = path.read_text(encoding="utf-8")
    path.write_text(uv_line.replace("uv pip install --system", "pip install"), encoding="utf-8")
    assert dockerfile_requirements(path) == [(4, requirements)]
    # A requirement that the line's constraints file pins with == is pinned for uv too (uv 0.13.1 installs
    # six==1.15.0 for `six` with `-c` naming a file that has six==1.15.0).
    path.write_text(uv_line.replace("-r requirements.txt", "-r requirements.txt -c constraints.txt"), "utf-8")
    base.write_text("nowheels\n", encoding="utf-8")
    constraints.write_text("nowheels==0.9\n", encoding="utf-8")
    found: dict = {}
    uv_installs: set = set()
    assert dockerfile_requirements(path, constraints=found, uv_installs=uv_installs) == [(4, requirements)]
    assert found == {requirements: (constraints,)}
    assert uv_installs == {(4, requirements)}


def test_cli_dockerfile_inputs_refuses_deps_for_a_uv_install(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", "-r requirements.txt"))
    with pytest.raises(DockerfileError, match=":4: uv pip install isn't read with --deps"):
        dockerfile_requirements(path, dependencies=True, uv_installs=set())
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--deps", "--json"]) == 2
    assert f"{path}:4: uv pip install isn't read with --deps" in capsys.readouterr().err
    assert fake_pypi == []


def test_dockerfile_requirements_uv_install_follows_the_ignore_rules(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN uv pip install --system -r requirements.txt\n",
        ["requirements.txt", "base.txt"],
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r base.txt\n", encoding="utf-8")
    includes: dict = {}
    assert dockerfile_requirements(path, includes=includes, uv_installs=set()) == [(4, requirements)]
    assert includes == {(4, requirements): (tmp_path / "base.txt",)}
    for name in ("base.txt", "requirements.txt"):
        (tmp_path / ".dockerignore").write_bytes(f"{name}\n".encode())
        with pytest.raises(
            DockerfileError,
            match=rf":3: COPY \.: {re.escape(name)} is excluded from the build context by \.dockerignore:1 ",
        ):
            dockerfile_requirements(path, uv_installs=set())


@pytest.mark.parametrize("name", ["pylock.toml", "deps.py", "reqs"])
def test_dockerfile_requirements_uv_includes_need_requirements_file_names(tmp_path, name):
    path = _context(
        tmp_path, _UV_RUN.format(f"requirements.txt {name}", "-r requirements.txt"), ["requirements.txt", name]
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {name}\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / name).write_text("nowheels==0.9\n", encoding="utf-8")
    message = (
        rf":4: requirements.txt includes {re.escape(name)}, and --dockerfile-inputs only reads requirements files "
        r"named \*\.txt or \*\.in for uv"
    )
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path, uv_installs=set())
    # pip reads any file name as a requirements file.
    path.write_text(path.read_text(encoding="utf-8").replace("uv pip install --system", "pip install"), "utf-8")
    includes: dict = {}
    assert dockerfile_requirements(path, includes=includes) == [(4, requirements)]
    assert includes == {(4, requirements): (tmp_path / name,)}


def test_dockerfile_requirements_uv_refuses_requirements_inside_constraints(tmp_path):
    # pip installs what a -r inside a constraints file names; uv only constrains with it (uv 0.13.1 and pip 26.2,
    # `-r r.txt -c c.txt` where c.txt has `-r more.txt`: pip installs more.txt's package, uv doesn't).
    files = ["requirements.txt", "constraints.txt", "more.txt"]
    path = _context(tmp_path, _UV_RUN.format(" ".join(files), "-r requirements.txt -c constraints.txt"), files)
    requirements, constraints, more = (tmp_path / name for name in files)
    constraints.write_text("-r more.txt\npurewheel==3.1\n", encoding="utf-8")
    more.write_text("nowheels==0.9\n", encoding="utf-8")
    error = ":4: constraints.txt is read as constraints and has -r more.txt: pip installs what that file names"
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(path, constraints={}, uv_installs=set())
    uv_line = path.read_text(encoding="utf-8")
    path.write_text(uv_line.replace("uv pip install --system", "pip install"), encoding="utf-8")
    assert dockerfile_requirements(path, constraints={}) == [(4, requirements)]
    # The same for a constraints file that the requirements file names itself.
    path.write_text(uv_line.replace(" -c constraints.txt", ""), encoding="utf-8")
    requirements.write_text("-c constraints.txt\npurewheel==3.1\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(path, uv_installs=set())
    # A -c inside a constraints file only constrains, for both: it is read.
    constraints.write_text("-c more.txt\npurewheel==3.1\n", encoding="utf-8")
    includes: dict = {}
    assert dockerfile_requirements(path, includes=includes, uv_installs=set()) == [(4, requirements)]
    assert includes == {(4, requirements): (constraints, more)}


_UV_FILES = {
    "requirements.txt": "-r base.txt\npurewheel==3.1\n",
    "base.txt": "nowheels==0.9\n",
    "constraints.txt": "purewheel==3.1\n",
}


@pytest.mark.parametrize("name", list(_UV_FILES))
def test_dockerfile_requirements_uv_refuses_option_lines_in_every_file_it_reads(tmp_path, name):
    # uv reads a requirements file's option lines by its own rules: with --extra-index-url it takes the first index
    # that has the package, where pip (and the requirements reader) takes the best file of them all.
    path = _context(
        tmp_path, _UV_RUN.format(" ".join(_UV_FILES), "-r requirements.txt -c constraints.txt"), list(_UV_FILES)
    )
    for file, text in _UV_FILES.items():
        (tmp_path / file).write_text(text, encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    assert dockerfile_requirements(path, constraints={}, uv_installs=set()) == [(4, requirements)]
    (tmp_path / name).write_text("--extra-index-url https://example.test/simple\n" + _UV_FILES[name], encoding="utf-8")
    message = (
        rf":4: {re.escape(name)} has a --extra-index-url line, and uv reads a requirements file's options by its "
        "own rules: --dockerfile-inputs only reads requirements and -r FILE / -c FILE lines in a file uv installs"
    )
    with pytest.raises(DockerfileError, match=message):
        dockerfile_requirements(path, constraints={}, uv_installs=set())
    # pip installs the same three files, and they are read as before.
    path.write_text(path.read_text(encoding="utf-8").replace("uv pip install --system", "pip install"), "utf-8")
    assert dockerfile_requirements(path, constraints={}) == [(4, requirements)]


@pytest.mark.parametrize(
    ("line", "option"),
    [
        # uv keeps --no-binary and --only-binary as two sets, and pip lets a later line undo an earlier one.
        ("--no-binary :all:", "--no-binary"),
        ("--only-binary purewheel", "--only-binary"),
        ("--pre -r base.txt", "--pre"),
        ("-rbase.txt", "-r"),  # uv: "Expected '=' or whitespace"
        ("-e .", "-e"),
    ],
)
def test_dockerfile_requirements_uv_refuses_other_option_lines(tmp_path, line, option):
    files = ["requirements.txt", "base.txt"]
    path = _context(tmp_path, _UV_RUN.format(" ".join(files), "-r requirements.txt"), files)
    (tmp_path / "requirements.txt").write_text(f"{line}\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    with pytest.raises(
        DockerfileError, match=rf":4: requirements.txt has a {re.escape(option)} line, and uv reads a requirements"
    ):
        dockerfile_requirements(path, uv_installs=set())


def test_dockerfile_requirements_uv_option_line_message_leaves_the_url_out(tmp_path):
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", "-r requirements.txt"))
    for line, option in (
        ("-ihttps://user:hunter2@private.example.test/simple", "-i"),
        ("--index-url=https://user:hunter2@private.example.test/simple", "--index-url"),
        ("--index-url https://user:hunter2@private.example.test/simple", "--index-url"),
    ):
        (tmp_path / "requirements.txt").write_text(f"{line}\npurewheel==3.1\n", encoding="utf-8")
        with pytest.raises(DockerfileError, match=f":4: requirements.txt has a {option} line, ") as err:
            dockerfile_requirements(path, uv_installs=set())
        assert "hunter2" not in str(err.value)
        assert "private.example.test" not in str(err.value)


def test_dockerfile_requirements_uv_reads_a_requirement_with_its_own_options(tmp_path):
    # A requirement that carries --hash isn't an option line, on its own line or continued as pip-compile writes it
    # (uv 0.13.1 installs it as pip does).
    path = _context(tmp_path, _UV_RUN.format("requirements.txt", "-r requirements.txt"))
    requirements = tmp_path / "requirements.txt"
    for text in (f"purewheel==3.1 --hash=sha256:{'0' * 64}\n", f"purewheel==3.1 \\\n    --hash=sha256:{'0' * 64}\n"):
        requirements.write_text(text, encoding="utf-8")
        assert dockerfile_requirements(path, uv_installs=set()) == [(4, requirements)]


def test_dockerfile_requirements_refuses_a_file_pip_and_uv_both_install(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt ./\nRUN pip install -r requirements.txt\n"
        "RUN uv pip install --system -r requirements.txt\n",
    )
    with pytest.raises(
        DockerfileError, match=r":5: requirements.txt is installed by pip on one line and by uv on another \(line 4\)"
    ):
        dockerfile_requirements(path, uv_installs=set())


def test_cli_dockerfile_inputs_checks_what_the_image_installs(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
    code = main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "musl-x86_64")]
    assert sorted(p["name"] for p in data["packages"]) == ["nowheels", "purewheel"]
    assert code == 1  # nowheels has no wheel
    note = f"wheelgap: note: {path}:4: RUN pip install -r reads {tmp_path / 'requirements.txt'}\n"
    assert note in captured.err


def test_plain_dockerfile_still_ignores_what_the_image_installs(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    assert main(["purewheel", "--dockerfile", str(path), "--json"]) == 0
    captured = capsys.readouterr()
    assert [p["name"] for p in json.loads(captured.out)["packages"]] == ["purewheel"]
    assert "RUN pip install" not in captured.err


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--dockerfile-inputs"], "--dockerfile-inputs reads what --dockerfile installs; pass --dockerfile too"),
        (["purewheel", "--dockerfile", "Dockerfile", "--dockerfile-inputs"], "can't be used with INPUT"),
        (
            ["--dockerfile-context", "."],
            "--dockerfile-context is only used with --dockerfile-inputs; pass --dockerfile-inputs too",
        ),
        (
            ["--dockerfile", "Dockerfile", "--dockerfile-inputs", "--dockerfile-context", "missing-context-folder-xyz"],
            "--dockerfile-context: not a directory",
        ),
        # An empty DIR (an unset variable) is refused, not read as "no context given" or as the current folder.
        (
            ["--dockerfile", "Dockerfile", "--dockerfile-inputs", "--dockerfile-context", ""],
            "--dockerfile-context: DIR is empty",
        ),
    ],
)
def test_cli_dockerfile_inputs_usage_errors(capsys, argv, error):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


def test_cli_dockerfile_inputs_context_reads_from_context_dir(tmp_path, capsys, fake_pypi):
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    df = docker_dir / "Dockerfile"
    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (docker_dir / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")

    code = main(["--dockerfile", str(df), "--dockerfile-inputs", "--dockerfile-context", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert [p["name"] for p in data["packages"]] == ["purewheel"]
    assert f"reads {tmp_path / 'requirements.txt'}" in captured.err
    assert f"reads {docker_dir / 'requirements.txt'}" not in captured.err


def test_cli_dockerfile_context_that_is_a_file_is_a_usage_error(tmp_path, capsys):
    path = _context(
        tmp_path,
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
    )
    file = tmp_path / "requirements.txt"
    with pytest.raises(SystemExit) as exc:
        main(["--dockerfile", str(path), "--dockerfile-inputs", "--dockerfile-context", str(file)])
    assert exc.value.code == 2
    assert f"--dockerfile-context: not a directory: {file}" in capsys.readouterr().err


def test_cli_dockerfile_inputs_context_refuses_paths_outside_the_context(tmp_path, capsys, fake_pypi):
    # The context is a folder below the Dockerfile's, so a file beside the Dockerfile is outside the build context.
    context = tmp_path / "app"
    context.mkdir()
    df = tmp_path / "Dockerfile"
    (tmp_path / "outside.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    argv = ["--dockerfile", str(df), "--dockerfile-inputs", "--dockerfile-context", str(context)]

    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY ../outside.txt .\nRUN pip install -r outside.txt\n",
        encoding="utf-8",
    )
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert f"{df}:3: COPY ../outside.txt: only paths inside the build context ({context}) are read" in err

    # A file that is only in the Dockerfile's folder isn't found: the folder named is the one that was searched.
    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY outside.txt .\nRUN pip install -r outside.txt\n",
        encoding="utf-8",
    )
    assert main(argv) == 2
    assert f"{df}:3: COPY outside.txt: not found in the build context ({context})" in capsys.readouterr().err
    assert fake_pypi == []


def test_cli_dockerfile_inputs_context_refuses_a_symlink_out_of_the_context(tmp_path, capsys, fake_pypi):
    # The link's target is in the Dockerfile's folder but not in the context, so the boundary checked is DIR's.
    context = tmp_path / "app"
    context.mkdir()
    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY link.txt .\nRUN pip install -r link.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "outside.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    try:
        (context / "link.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:
        pytest.skip("symlinks aren't available here")
    assert main(["--dockerfile", str(df), "--dockerfile-inputs", "--dockerfile-context", str(context)]) == 2
    err = capsys.readouterr().err
    assert f"{df}:3: COPY link.txt: a symlink out of the build context isn't read by --dockerfile-inputs" in err
    assert fake_pypi == []


def test_cli_dockerfile_inputs_uses_the_context_and_specific_ignore_file(tmp_path, capsys, fake_pypi):
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    df = docker_dir / "Dockerfile"
    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    argv = ["--dockerfile", str(df), "--dockerfile-inputs", "--dockerfile-context", str(tmp_path)]

    # Docker reads neither a .dockerignore beside the Dockerfile nor a Dockerfile.dockerignore in the context's root
    # when the two folders differ, so these leave nothing out of the build.
    (docker_dir / ".dockerignore").write_text("*.txt\n", encoding="utf-8")
    (tmp_path / "Dockerfile.dockerignore").write_text("*.txt\n", encoding="utf-8")
    assert main(argv) == 0
    capsys.readouterr()

    (tmp_path / ".dockerignore").write_text("*.txt\n", encoding="utf-8")
    assert main(argv) == 2
    assert (
        f"{df}:3: COPY requirements.txt: excluded from the build context by .dockerignore:1" in capsys.readouterr().err
    )

    # An empty Dockerfile-specific file: BuildKit's built-in frontend ignores nothing, a `# syntax=` frontend falls
    # back to .dockerignore, so wheelgap can't tell which rules the build uses.
    (docker_dir / "Dockerfile.dockerignore").write_text("", encoding="utf-8")
    assert main(argv) == 2
    assert f"{df}: Dockerfile.dockerignore is empty" in capsys.readouterr().err
    (docker_dir / "Dockerfile.dockerignore").write_text("# nothing is left out\n", encoding="utf-8")
    assert main(argv) == 0  # the Dockerfile-specific file takes precedence
    capsys.readouterr()
    (tmp_path / ".dockerignore").write_text("# no rules\n", encoding="utf-8")
    (docker_dir / "Dockerfile.dockerignore").write_text("", encoding="utf-8")
    assert main(argv) == 0  # empty again, but .dockerignore has no rules, so nothing is left out either way
    capsys.readouterr()
    (tmp_path / ".dockerignore").write_text("*.json\n", encoding="utf-8")

    (docker_dir / "Dockerfile.dockerignore").write_text("*.txt\n", encoding="utf-8")
    assert main(argv) == 2
    assert f"{df}:3: COPY requirements.txt: excluded from the build context by Dockerfile.dockerignore:1" in (
        capsys.readouterr().err
    )


def test_cli_dockerfile_inputs_context_reads_config_from_the_context(tmp_path, capsys, fake_pypi):
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    df = docker_dir / "Dockerfile"
    df.write_text(
        "FROM python:3.13-alpine\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    # The one beside the Dockerfile isn't read: its unknown key would be an input error (exit 2).
    (docker_dir / "pyproject.toml").write_text("[tool.wheelgap]\nno-such-key = true\n", encoding="utf-8")

    code = main(["--dockerfile", str(df), "--dockerfile-inputs", "--dockerfile-context", str(tmp_path)])
    assert code == 0
    assert capsys.readouterr().out == ""  # quiet = true, and purewheel has a wheel


def test_dockerfile_requirements_reads_system_package_commands(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13-slim\nRUN apt-get update && apt-get install -y --no-install-recommends gcc g++ "
        "&& rm -rf /var/lib/apt/lists/*\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
    )
    assert dockerfile_requirements(path) == [(5, tmp_path / "requirements.txt")]


_DIGEST = "@sha256:" + "0123456789abcdef" * 4


@pytest.mark.parametrize(
    "frontend",
    [
        f"docker/dockerfile:1{_DIGEST}",
        f"docker/dockerfile{_DIGEST}",
        f"docker.io/docker/dockerfile:1.7{_DIGEST}",
        f"docker/dockerfile:1.16{_DIGEST}",  # the digest, not the tag, picks the image
    ],
)
def test_dockerfile_requirements_reads_a_digest_pinned_docker_dockerfile_frontend(tmp_path, frontend):
    body = "FROM python:3.13-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
    path = _context(tmp_path, f"# syntax={frontend}\n{body}")
    assert dockerfile_requirements(path) == [(5, tmp_path / "requirements.txt")]
    # Its version is unknown, so a heredoc anywhere is still refused.
    path.write_text(f"# syntax={frontend}\n{body}RUN <<EOF\necho hi\nEOF\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match="may read the heredocs in 'RUN <<EOF'"):
        dockerfile_requirements(path)
    # Versions read `<<` differently, so it's refused even where 1.16 finds no heredoc (a here-string here),
    # before the final stage's RUN rules are checked.
    path.write_text(f"# syntax={frontend}\n{body}RUN cat <<<hi\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match="may read the heredocs"):
        dockerfile_requirements(path)


@pytest.mark.parametrize(
    "frontend",
    [
        "example.com/frontend:1",
        f"example.com/frontend:1{_DIGEST}",
        f"docker/dockerfile:1@sha256:{'0' * 63}",  # not a full digest
        f"docker/dockerfile:1@sha256:{'A' * 64}",  # digests are lower-case hex
    ],
)
def test_dockerfile_requirements_still_refuses_other_frontends(tmp_path, frontend):
    path = _context(
        tmp_path,
        f"# syntax={frontend}\nFROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n",
    )
    with pytest.raises(DockerfileError, match="only reads Dockerfiles for docker/dockerfile frontends"):
        dockerfile_requirements(path)


def test_dockerfile_requirements_refuses_a_pip_config_file(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nCOPY pip.conf /etc/pip.conf\nWORKDIR /app\nCOPY requirements.txt .\n"
        "RUN pip install -r requirements.txt\n",
        ["requirements.txt", "pip.conf"],
    )
    with pytest.raises(DockerfileError, match=":2: a pip config file may change what pip installs"):
        dockerfile_requirements(path)


@pytest.mark.parametrize(
    ("copy", "files"),
    [
        # A folder with a pip.conf in it, wherever it lands: /etc, the user's home...
        ("COPY etc/ /etc/", ["etc/pip.conf"]),
        ("COPY . /root/", [".config/pip/pip.conf"]),
        # ...and a file renamed to pip.conf, in sys.prefix or another user's home.
        ("COPY mirror.conf /usr/local/pip.conf", ["mirror.conf"]),
        ("COPY mirror.conf /home/app/.config/pip/pip.conf", ["mirror.conf"]),
        ("COPY mirror.conf /home/app/.pip/PIP.INI", ["mirror.conf"]),
    ],
)
def test_dockerfile_requirements_refuses_a_pip_config_file_by_its_name(tmp_path, copy, files):
    path = _context(
        tmp_path,
        f"FROM python:3.13\n{copy}\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        ["requirements.txt", *files],
    )
    with pytest.raises(DockerfileError, match=":2: a pip config file may change what pip installs"):
        dockerfile_requirements(path)


@pytest.mark.parametrize(
    "copy",
    [
        # Copies wheelgap can't read may still bring in a pip config, even before the one that copies FILE.
        "COPY --from=builder /etc/pip.conf /etc/pip.conf",
        "ADD https://example.com/pip.conf /etc/pip.conf",
        "ADD pip.conf /etc/pip.conf",
        "COPY *.conf /etc/",
    ],
)
def test_dockerfile_requirements_refuses_an_unread_copy_before_the_file(tmp_path, copy):
    path = _context(
        tmp_path,
        f"FROM python:3.13\n{copy}\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        ["requirements.txt", "pip.conf"],
    )
    with pytest.raises(DockerfileError, match=r"Dockerfile:2: "):
        dockerfile_requirements(path)


def test_dockerfile_requirements_refuses_a_symlink_that_overwrites_the_file(tmp_path):
    path = _context(
        tmp_path,
        "FROM python:3.13\nWORKDIR /app\nCOPY base.txt /app/requirements.txt\nCOPY . /app\n"
        "RUN pip install -r requirements.txt\n",
        ["base.txt", "reqs/prod.txt"],
    )
    try:
        (tmp_path / "requirements.txt").symlink_to(tmp_path / "reqs" / "prod.txt")
    except OSError:
        pytest.skip("symlinks aren't available here")
    with pytest.raises(DockerfileError, match=":4: requirements.txt is a symlink"):
        dockerfile_requirements(path)


def test_cli_dockerfile_inputs_refuses_before_any_lookup(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt && poetry install\n"
    )
    assert main(["--dockerfile", str(path), "--dockerfile-inputs"]) == 2
    assert f"{path}:4: `poetry install`: poetry installs Python packages in a way" in capsys.readouterr().err
    assert fake_pypi == []


_OTHER_IMAGES = [
    "FROM ubuntu:24.04\n",
    "FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04\n",
    "FROM node:22-slim AS web\nFROM ubuntu\n",  # a bare name with stages before it
    "FROM python:slim\n",  # no Python in the tag
    "ARG BASE\nFROM ${BASE}\n",  # a build argument with no default
    "ARG BASE=python:3.13\nFROM ${BASE:-debian}\n",  # a modifier wheelgap doesn't expand
    "FROM --platform=$BUILDPLATFORM python:3.13\n",
]


@pytest.mark.parametrize("head", _OTHER_IMAGES)
def test_cli_dockerfile_inputs_reads_any_image_when_p_and_t_are_given(tmp_path, capsys, fake_pypi, head):
    path = _context(tmp_path, f"{head}WORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
    argv = ["--dockerfile", str(path), "--dockerfile-inputs", "--json"]
    code = main([*argv, "-p", "3.13", "-t", "linux-aarch64", "--quiet"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert code == 1  # nowheels has no wheel
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "linux-aarch64")]
    assert sorted(p["name"] for p in data["packages"]) == ["nowheels", "purewheel"]
    # What isn't known is said even under --quiet, and in --json.
    line = head.count("\n")
    assert f"wheelgap: note: {path}:{line}: the final image isn't read (" in captured.err
    assert "which Python the image's pip installs for" in captured.err
    assert "pass --glibc or --musl" in captured.err
    [note] = data["dockerfile_notes"]
    assert (note["dockerfile"], note["line"], note["kind"]) == (str(path), line, "image-not-read")
    assert "the packages the image already has" in note["message"]
    # Without both -p and -t the image is still an error, and no package is looked up.
    for flags in ([], ["-p", "3.13"], ["-t", "linux-aarch64"]):
        fake_pypi.clear()
        assert main([*argv, *flags]) == 2
        captured = capsys.readouterr()
        assert f"wheelgap: error: {path}: " in captured.err
        assert "isn't read" not in captured.err
        assert fake_pypi == []


def test_cli_dockerfile_reads_any_image_when_p_and_t_are_given(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM ubuntu:24.04\nRUN anything at all\n")
    assert main(["purewheel", "--dockerfile", str(path), "-p", "3.13", "-t", "linux-x86_64", "--glibc", "2.39"]) == 0
    captured = capsys.readouterr()
    assert f"{path}:1: the final image isn't read (ubuntu:24.04: only the official python images" in captured.err
    assert "pass --glibc or --musl" not in captured.err  # it was passed
    assert main(["purewheel", "--dockerfile", str(path)]) == 2
    assert "ubuntu:24.04: only the official python images" in capsys.readouterr().err


def test_cli_dockerfile_p_and_t_leave_a_read_image_without_a_note(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM python:3.12-alpine\n")
    main(["purewheel", "--dockerfile", str(path), "-p", "3.13", "-t", "linux-x86_64", "--json"])
    captured = capsys.readouterr()
    assert "dockerfile_notes" not in json.loads(captured.out)
    assert "isn't read" not in captured.err


def test_cli_dockerfile_inputs_names_an_unread_image_before_an_unread_run(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM ubuntu:24.04\nWORKDIR /app\nCOPY . .\nRUN poetry install\n")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs"]) == 2
    error = capsys.readouterr().err
    assert "ubuntu:24.04: only the official python images" in error
    assert f"{path}:4" not in error
    # With -p/-t the image is passed, and the RUN is the error.
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "-p", "3.13", "-t", "linux-x86_64"]) == 2
    assert f"{path}:4: " in capsys.readouterr().err


@pytest.mark.parametrize(
    ("dockerfile", "error"),
    [
        # A build argument wheelgap can't expand may name the earlier stage, whose instructions would then count.
        ("FROM python:3.13 AS base\nARG BASE\nFROM ${BASE}\n", ":3: FROM ${BASE} uses a build argument"),
        ("FROM python:3.13 AS base\nFROM $BASE\n", "may name an earlier stage (base)"),
        ("FROM ubuntu AS base\nARG BASE\nFROM base AS mid\nFROM $BASE\n", ":4: FROM $BASE uses a build argument"),
        # What Docker itself rejects stays an error.
        ("FROM Ubuntu:24.04\n", "repository name must be lowercase"),
        ("FROM ubuntu:24.04 AS\n", "expected 'IMAGE' or 'IMAGE AS NAME'"),
        ("FROM --flag ubuntu\n", "unknown flag"),
        # An empty image has no Python but one a COPY brings.
        ("FROM scratch\n", ":1: FROM scratch: the image starts empty"),
        ("FROM python:3.13 AS base\nFROM scratch\nCOPY --from=base /usr/local /usr/local\n", "FROM scratch"),
    ],
)
def test_cli_dockerfile_inputs_p_and_t_dont_stand_in_for_these(tmp_path, capsys, fake_pypi, dockerfile, error):
    path = _context(
        tmp_path, f"{dockerfile}WORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "-p", "3.13", "-t", "linux-x86_64"]) == 2
    assert error in capsys.readouterr().err
    assert fake_pypi == []


def test_unknown_image_error_is_a_dockerfile_error_with_the_from_line(tmp_path):
    path = _context(tmp_path, "# a comment\nFROM debian:trixie\n")
    with pytest.raises(DockerfileError) as excinfo:
        read_dockerfile(path)
    assert isinstance(excinfo.value, UnknownImageError)
    assert excinfo.value.line == 2


def _commands(text, env=None):
    """(operator before, words, whether every word is exact) for each simple command of a shell line."""
    return [
        (c.before, [w.text for w in c.words], all(w.exact for w in c.words)) for c in _Shell(text, env or {}).parse()
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pip install -r requirements.txt", [("", ["pip", "install", "-r", "requirements.txt"], True)]),
        # 2.9.3: and-or lists, and ; & | between them.
        (
            "apt-get update && apt-get install -y gcc; pip check || true | cat & echo",
            [
                ("", ["apt-get", "update"], True),
                ("&&", ["apt-get", "install", "-y", "gcc"], True),
                (";", ["pip", "check"], True),
                ("||", ["true"], True),
                ("|", ["cat"], True),
                ("&", ["echo"], True),
            ],
        ),
        # 2.2 Quoting: a backslash, single quotes, double quotes (where \ only escapes $ ` " \ and a newline).
        ("echo 'a b' \"c d\" e\\ f", [("", ["echo", "a b", "c d", "e f"], True)]),
        ("echo \"a\\\"b\\$c\\d\" 'it'\\''s'", [("", ["echo", 'a"b$c\\d', "it's"], True)]),
        ("pip install 'six>=1.0' \"numpy<3\" a\\>b", [("", ["pip", "install", "six>=1.0", "numpy<3", "a>b"], True)]),
        ("echo ''", [("", ["echo", ""], True)]),
        # 2.3: an operator ends a word wherever it is; # starts a comment only where a word would start.
        ("a&&b;c|d", [("", ["a"], True), ("&&", ["b"], True), (";", ["c"], True), ("|", ["d"], True)]),
        ("echo a#b # the rest", [("", ["echo", "a#b"], True)]),
        ("echo a \\\n  b", [("", ["echo", "a", "b"], True)]),
        ("echo a\nb", [("", ["echo", "a"], True), (";", ["b"], True)]),
        # 2.6.2: a variable with a known value is its value; any other expansion leaves the word inexact.
        ("pip install six==$V", [("", ["pip", "install", "six==1.16.0"], True)]),
        ('pip install six==${V} "six==${V}" six==${V:-9}', [("", ["pip", "install", *["six==1.16.0"] * 3], True)]),
        ("pip install six==$OTHER", [("", ["pip", "install", "six==$OTHER"], False)]),
        ("pip install six==${OTHER:-1}", [("", ["pip", "install", "six==${OTHER:-1}"], False)]),
        (
            "pip install ${V:+x} ${V#1} $1 $@ $?",
            [("", ["pip", "install", "${V:+x}", "${V#1}", "$1", "$@", "$?"], False)],
        ),
        ("echo '$V' \\$V $", [("", ["echo", "$V", "$V", "$"], True)]),
        # An unquoted value with a space or a glob character is split or expanded by the shell: not taken as one word.
        ("pip install $SPACED", [("", ["pip", "install", "$SPACED"], False)]),
        ('pip install "$SPACED"', [("", ["pip", "install", "a b"], True)]),
        # Assignments on the line are followed; one whose value isn't known forgets the variable.
        ("V=2.0 && pip install six==$V", [("", [], True), ("&&", ["pip", "install", "six==2.0"], True)]),
        (
            "export V=3.0; pip install six==$V",
            [("", ["export", "V=3.0"], True), (";", ["pip", "install", "six==3.0"], True)],
        ),
        (
            "V=$(cat v) && pip install six==$V",
            [("", ["cat", "v"], True), ("", [], True), ("&&", ["pip", "install", "six==$V"], False)],
        ),
        ("read V; pip install six==$V", [("", ["read", "V"], True), (";", ["pip", "install", "six==$V"], False)]),
        (". ./env; pip install six==$V", [("", [".", "./env"], True), (";", ["pip", "install", "six==$V"], False)]),
        ("V=1 pip install six", [("", ["pip", "install", "six"], True)]),
        # 2.6.3: a command substitution's commands are commands too, and its word isn't known.
        (
            "pip install $(cat reqs) `echo six`",
            [
                ("", ["cat", "reqs"], True),
                ("", ["echo", "six"], True),
                ("", ["pip", "install", "$(cat reqs)", "`echo six`"], False),
            ],
        ),
        (
            "echo $((1 + 2)) \"$(echo ')')\"",
            [("", ["echo", ")"], True), ("", ["echo", "$((1 + 2))", "$(echo ')')"], False)],
        ),
        # 2.13 globs, a leading tilde and bash's brace lists are expanded by the shell.
        ("rm -rf /tmp/* ~/.cache a? [ab]c", [("", ["rm", "-rf", "/tmp/*", "~/.cache", "a?", "[ab]c"], False)]),
        ("pip install {six,numpy} six{,}", [("", ["pip", "install", "{six,numpy}", "six{,}"], False)]),
        (
            "[ -f x ] && echo '*' \"?\" a~b {} x[1",
            [("", ["[", "-f", "x", "]"], True), ("&&", ["echo", "*", "?", "a~b", "{}", "x[1"], True)],
        ),
        # 2.7: redirections aren't words of the command.
        ("pip install -r r.txt > /dev/null 2>&1", [("", ["pip", "install", "-r", "r.txt"], True)]),
        ("echo six>>r.txt 2>log <in", [("", ["echo", "six"], True)]),
    ],
)
def test_shell_commands(text, expected):
    assert _commands(text, {"V": "1.16.0", "SPACED": "a b"}) == expected


def test_shell_redirects_assignments_and_subshells():
    [command] = _Shell("A=1 B='x y' pip install six >> log 2>&1 < in", {}).parse()
    assert [(name, value.text) for name, value in command.assigns] == [("A", "1"), ("B", "x y")]
    assert [(operator, target.text) for operator, target in command.redirects] == [
        (">>", "log"),
        (">&", "1"),
        ("<", "in"),
    ]
    shell = _Shell("(cd /src && make) && pip check", {})
    assert [(c.words[0].text, c.subshell) for c in shell.parse()] == [("cd", True), ("make", True), ("pip", False)]
    assert not shell.parens
    function = _Shell("setup() { pip install six; }; setup", {})
    function.parse()
    assert function.parens


@pytest.mark.parametrize("text", ["echo 'a", 'echo "a', "echo $(a", "echo `a", "cat <<EOF", "echo ${a"])
def test_shell_refuses(text):
    with pytest.raises(_ShellError):
        _Shell(text, {}).parse()


def _inputs(
    tmp_path, body, files=("requirements.txt",), head="FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n"
):
    path = _context(tmp_path, head + body, files)
    report = InputsReport()
    found = dockerfile_requirements(path, report=report, uv_installs=set(), constraints={})
    return [line for line, _ in found], report


_INSTALL = "RUN pip install -r requirements.txt\n"


@pytest.mark.parametrize(
    ("body", "classes"),
    [
        # #652: lines after, before and around the install that install nothing.
        (_INSTALL + "COPY . .\nRUN chmod +x entrypoint.sh && mkdir -p /data\n", {"shell": [6]}),
        (
            "RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*\n" + _INSTALL,
            {"system-packages": [4], "shell": [4]},
        ),
        (
            "RUN DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git\n" + _INSTALL,
            {"system-packages": [4]},
        ),
        (
            "RUN export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get clean; apt-get autoremove -y\n"
            + _INSTALL,
            {"shell": [4], "system-packages": [4]},
        ),
        ("RUN apk add --no-cache build-base && adduser -D app\n" + _INSTALL, {"system-packages": [4]}),
        (
            "RUN curl -fsSL https://example.test/x.tgz | tar xz -C /opt && npm ci && npm run build\n" + _INSTALL,
            {"programs": [4], "shell": [4]},
        ),
        (
            "RUN git clone https://example.test/r.git /src && cd /src && cmake . && echo done > /tmp/log\n" + _INSTALL,
            {"programs": [4], "shell": [4]},
        ),
        (
            'RUN if [ "$TARGETARCH" = arm64 ]; then apt-get install -y libatlas3-base; fi\n' + _INSTALL,
            {"shell": [4], "system-packages": [4]},
        ),
        ("RUN for d in a b; do mkdir -p /data/$d; done\n" + _INSTALL, {"shell": [4]}),
        ("RUN (cd /opt && wget https://example.test/f) || true\n" + _INSTALL, {"shell": [4], "programs": [4]}),
        ("RUN sh -c 'echo hi && mkdir /x'\n" + _INSTALL, {"shell": [4]}),
        ('RUN ["mkdir", "-p", "/data"]\n' + _INSTALL, {"shell": [4]}),
        ("RUN --network=none --security=sandbox ls\n" + _INSTALL, {"shell": [4]}),
        ("RUN python -m venv /opt/venv && . /opt/venv/bin/activate\n" + _INSTALL, {"venv": [4]}),
        ("RUN python --version; pip --version; pip list; uv --version; pip check\n" + _INSTALL, {"shell": [4]}),
        # The install inside a chain, with its other spellings, and under a cache mount.
        (
            "RUN apt-get update && pip install -r requirements.txt && rm -rf /root/.cache/pip\n",
            {"system-packages": [4], "shell": [4]},
        ),
        ("RUN --mount=type=cache,target=/root/.cache/pip pip3 install -r requirements.txt\n", {"cache-mounts": [4]}),
        (
            "ENV CACHE=/root/.cache/pip\n"
            "RUN --mount=type=cache,target=$CACHE,sharing=locked python3 -m pip install -r requirements.txt\n",
            {"cache-mounts": [5]},
        ),
        ("RUN set -eux; python -m pip install --no-cache-dir -r requirements.txt; pip check\n", {"shell": [4]}),
        ("RUN pip --no-cache-dir install -r requirements.txt > /dev/null 2>&1\n", {}),
        ("RUN pip -q --no-color install -r requirements.txt\n", {}),
        ("RUN uv pip --no-progress install --system -r requirements.txt\n", {}),
        ("RUN /usr/local/bin/pip install -r /app/requirements.txt\n", {}),
        ("RUN cd / && pip install -r app/requirements.txt\n", {"shell": [4]}),
        ("RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt\n", {"venv": [4]}),
        ("RUN python -m venv .venv\nRUN .venv/bin/python -m pip install -r requirements.txt\n", {"venv": [4]}),
        ('SHELL ["/bin/bash", "-o", "pipefail", "-c"]\n' + _INSTALL, {}),
        ('SHELL ["bash", "-euxo", "pipefail", "-c"]\n' + _INSTALL, {}),
        # Installer set-up is passed over, and named.
        ("RUN pip install --no-cache-dir --upgrade pip setuptools wheel\n" + _INSTALL, {"installer-tools": [4]}),
        (
            "RUN python -m pip install -U 'pip>=24' wheel && python -m ensurepip\n" + _INSTALL,
            {"installer-tools": [4]},
        ),
        # A flag's value isn't a requirement name.
        ("RUN pip install --progress-bar off --retries 3 -U pip\n" + _INSTALL, {"installer-tools": [4]}),
        (
            "RUN pip install --root-user-action ignore --timeout 60 -U pip setuptools\n" + _INSTALL,
            {"installer-tools": [4]},
        ),
        ("RUN uv pip install --system --link-mode copy -U pip\n" + _INSTALL, {"installer-tools": [4]}),
    ],
)
def test_dockerfile_requirements_passes_over_runs_that_install_nothing(tmp_path, body, classes):
    lines, report = _inputs(tmp_path, body)
    assert len(lines) == 1
    assert {kind: sorted(numbers) for kind, numbers in report.passed_over.items()} == {k: v for k, v in classes.items()}
    assert report.warnings == []


def test_dockerfile_requirements_names_the_programs_and_tools_it_passed_over(tmp_path):
    body = (
        "RUN curl -O https://example.test/a && npm ci && curl -O https://example.test/b\nRUN pip install -U pip wheel\n"
    )
    _, report = _inputs(tmp_path, body + _INSTALL)
    assert report.programs == ["curl", "npm"]
    assert report.tools == ["pip", "wheel"]


@pytest.mark.parametrize(
    ("run", "what"),
    [
        ("./install.sh", "`./install.sh`, a script or program at a path"),
        ("sh scripts/setup.sh", "`sh scripts/setup.sh`, which runs commands wheelgap can't see"),
        ('bash -c "$SETUP"', "which runs commands wheelgap can't see"),
        ("curl -LsSf https://example.test/install.sh | sh", "`sh`, which runs commands wheelgap can't see"),
        ("make install", "`make install`, which runs commands wheelgap can't see"),
        ("python manage.py collectstatic --noinput", "`python manage.py collectstatic --noinput`, a Python script"),
        ("python -c 'import nltk; nltk.download(\"punkt\")'", "Python code wheelgap doesn't look into"),
        ("python -m spacy download en_core_web_sm", "`python -m spacy download en_core_web_sm`, a Python module"),
        ("mytool --setup", "`mytool --setup`, a program wheelgap doesn't know"),
        ("$TOOL build", "a command name wheelgap can't resolve"),
        ("find . -name '*.sh' -exec sh {} +", "which runs commands wheelgap can't see"),
        ("echo a | xargs something", "`xargs something`, which runs commands wheelgap can't see"),
        ('eval "$CMD"', "which runs commands wheelgap can't see"),
        ("case $TARGETARCH in arm64) echo a ;; esac", "a `case` command list"),
        ("setup() { echo hi; }; setup", "a `name()` command list"),
        ("sudo -u app ./run", "a command name wheelgap can't resolve"),
        ("<<EOF\necho hi\nEOF", "a heredoc script"),
    ],
)
def test_dockerfile_requirements_warns_about_runs_it_cant_see_into(tmp_path, run, what):
    lines, report = _inputs(tmp_path, f"{_INSTALL}RUN {run}\n")
    assert lines == [4]  # the answer is still given
    [(line, text)] = report.warnings
    assert line == 5
    assert what in text


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # Lines that install Python packages in a way that isn't read are never passed over.
        (
            _INSTALL + "RUN poetry install --no-root\n",
            ":5: `poetry install --no-root`: poetry installs Python packages",
        ),
        (_INSTALL + "RUN conda install -y numpy\n", ":5: `conda install -y numpy`: conda installs"),
        (_INSTALL + "RUN pipx install black && pdm sync\n", ":5: `pipx install black`: pipx installs"),
        (_INSTALL + "RUN python setup.py install\n", ":5: `python setup.py install` installs or builds a project"),
        (_INSTALL + "RUN uv sync --locked\n", ":5: `uv sync --locked` isn't read"),
        (_INSTALL + "RUN uv run pytest\n", ":5: `uv run pytest` isn't read"),
        (_INSTALL + "RUN pip uninstall -y six\n", ":5: `pip uninstall -y six`: only `install` is read of pip's"),
        (_INSTALL + "RUN pip config set global.index-url https://example.test\n", ":5: `pip config set"),
        (
            _INSTALL + "RUN /opt/conda/bin/pip install -r requirements.txt\n",
            ":5: .*which Python /opt/conda/bin/pip belongs to",
        ),
        (_INSTALL + "RUN /venv/bin/python -m pip install -r requirements.txt\n", ":5: .*which Python /venv/bin/python"),
        (_INSTALL + "RUN apt-get update && pip install --pre numpy\n", ":5: pip install --pre: --dockerfile-inputs"),
        (_INSTALL + "RUN pip install .\n", ":5: pip install \\."),
        # An install that may not run, or may fail without failing the build.
        (
            "RUN pip install -r requirements.txt || true\n",
            ":4: `pip install -r requirements.txt` is in a list with \\|\\|",
        ),
        ("RUN test -f x || echo no && pip install -r requirements.txt\n", ":4: .* is in a list with \\|\\|"),
        (
            "RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi\n",
            ":4: .* is inside if, for, while or { }",
        ),
        ("RUN for f in a b; do pip install -r requirements.txt; done\n", ":4: .* is inside if"),
        ("RUN { pip install -r requirements.txt; }\n", ":4: .* is inside if"),
        ("RUN (pip install -r requirements.txt)\n", ":4: .* is inside \\( \\)"),
        ("RUN yes | pip install -r requirements.txt\n", ":4: .* is part of a pipeline"),
        ("RUN pip install -r requirements.txt &\n", ":4: .* is run in the background"),
        ("RUN echo $(pip install -r requirements.txt)\n", ":4: .* is inside a command substitution"),
        ("RUN sh -c 'pip install -r requirements.txt'\n", ":4: .* is inside a command substitution or `sh -c`"),
        ("RUN pip install -r requirements.txt < /dev/null\n", ":4: .* reads its input from a redirect"),
        ("RUN case $A in x) pip install -r requirements.txt ;; esac\n", ":4: this RUN names pip inside `case`"),
        ("RUN f() { pip install -r requirements.txt; }; f\n", ":4: this RUN names pip inside `name\\(\\)`"),
        # What the install reads must be known.
        ("RUN pip install -r $REQS\n", ":4: .*\\$REQS isn't a value"),
        ("RUN pip install -r *.txt\n", ":4: .*\\*.txt isn't a value"),
        ("RUN pip install -r $(ls *.txt)\n", ":4: .* isn't a value"),
        ("RUN cd $SRC && pip install -r requirements.txt\n", ":4: the folder after `cd \\$SRC` isn't known"),
        ("RUN (cd /) && pip install -r app/requirements.txt\n", ":4: the folder after `cd /` isn't known"),
        ("RUN $PIP install -r requirements.txt\nRUN pip install -r missing.txt\n", ":4: this line runs `\\$PIP"),
        ("RUN pip install -r 'requirements.txt\n", ":4: this RUN has an unterminated ' quote"),
        # Settings and mounts on the RUN itself.
        (
            "RUN PIP_INDEX_URL=https://example.test/simple pip install -r requirements.txt\n",
            ":4: PIP_INDEX_URL is set on this RUN",
        ),
        ("RUN export UV_PRERELEASE=allow && pip install -r requirements.txt\n", ":4: UV_PRERELEASE is set on this RUN"),
        ("RUN env PIP_PRE=1 pip install -r requirements.txt\n", ":4: PIP_PRE is set on this RUN"),
        (
            "RUN --mount=type=bind,source=requirements.txt,target=/r.txt pip install -r /r.txt\n",
            ":4: RUN --mount=type=bind with an install",
        ),
        (
            "RUN --mount=source=.,target=/src pip install -r requirements.txt\n",
            ":4: RUN --mount=type=bind with an install",
        ),
        ("RUN --mount=type=ssh pip install -r requirements.txt\n", ":4: RUN --mount=type=ssh with an install"),
        ("RUN --mount=type=$KIND pip install -r requirements.txt\n", ":4: RUN --mount with a variable for its type"),
        (
            "RUN --mount=type=cache,target='/a b' pip install -r requirements.txt\n",
            ":4: RUN flags with quotes or escapes",
        ),
        ("RUN --device=gpu pip install -r requirements.txt\n", ":4: RUN --device isn't a RUN flag"),
        # A command that isn't read, before the install, that may have changed the file or written pip's config.
        (
            "RUN sed -i s/six/seven/ requirements.txt\n" + _INSTALL,
            ":5: requirements.txt may have been changed by line 4",
        ),
        (
            "RUN curl -o /app/requirements.txt https://example.test/r\n" + _INSTALL,
            ":5: requirements.txt may have been changed by line 4",
        ),
        (
            "RUN cp /other/*.txt /app/\n" + _INSTALL,
            ":5: requirements.txt may have been changed by line 4, which names /app/",
        ),
        (
            "RUN git clone https://example.test/r.git .\n" + _INSTALL,
            ":5: requirements.txt may have been changed by line 4, which names \\.",
        ),
        (
            "RUN echo six > req*.txt\n" + _INSTALL,
            ":5: requirements.txt may have been changed by line 4, which names req\\*.txt",
        ),
        (
            "RUN ./gen.sh requirements.txt && pip install -r requirements.txt\n",
            ":4: this line runs `./gen.sh requirements.txt`, a script .* before the install on the same line",
        ),
        (
            "RUN printf '[global]\\nindex-url = https://example.test\\n' > /etc/pip.conf\n" + _INSTALL,
            ":4: .* names /etc/pip.conf, which may be a pip or uv config file",
        ),
        (
            "RUN mkdir -p ~/.config/pip && cp /mnt/conf ~/.config/pip/pip.conf\n" + _INSTALL,
            ":4: .* names ~/.config/pip/pip.conf",
        ),
        ("RUN wget -O /root/.netrc https://example.test/n\n" + _INSTALL, ":4: .* names /root/.netrc"),
    ],
)
def test_dockerfile_requirements_stops_on_runs_that_may_change_the_answer(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        _inputs(tmp_path, body)


@pytest.mark.parametrize(
    "body",
    [
        # After the install, or naming something else, or only reading: the file is what COPY put there.
        _INSTALL + "RUN rm requirements.txt && sed -i s/a/b/ requirements.txt\n",
        "RUN cat requirements.txt && ls -l /app && grep six requirements.txt && chmod 644 requirements.txt\n"
        + _INSTALL,
        "RUN rm -rf /tmp/* /var/lib/apt/lists/* && find / -name '*.pyc' -delete\n" + _INSTALL,
        "RUN sed -i s/a/b/ other.txt && cp /x/*.cfg /etc/\n" + _INSTALL,
    ],
)
def test_dockerfile_requirements_reads_a_file_no_unread_command_may_have_changed(tmp_path, body):
    lines, _ = _inputs(tmp_path, body)
    assert len(lines) == 1


_SETTINGS = (
    "PIP_NO_CACHE_DIR PIP_CACHE_DIR PIP_DISABLE_PIP_VERSION_CHECK PIP_ROOT_USER_ACTION PIP_NO_INPUT PIP_PROGRESS_BAR "
    "PIP_NO_COLOR PIP_DEFAULT_TIMEOUT PIP_TIMEOUT PIP_RETRIES PIP_QUIET PIP_VERBOSE PIP_NO_WARN_SCRIPT_LOCATION "
    "PIP_NO_COMPILE PIP_COMPILE PIP_BREAK_SYSTEM_PACKAGES PIP_NO_PYTHON_VERSION_WARNING UV_NO_CACHE UV_CACHE_DIR "
    "UV_COMPILE_BYTECODE UV_LINK_MODE UV_NO_PROGRESS UV_HTTP_TIMEOUT UV_HTTP_RETRIES UV_CONCURRENT_DOWNLOADS "
    "UV_CONCURRENT_BUILDS UV_CONCURRENT_INSTALLS UV_PYTHON_DOWNLOADS UV_PYTHON_INSTALL_DIR UV_TOOL_BIN_DIR UV_TOOL_DIR "
    "UV_INSTALL_DIR UV_BREAK_SYSTEM_PACKAGES UV_PROJECT_ENVIRONMENT UV_SYSTEM_PYTHON UV_SYSTEM_CERTS UV_VERSION "
    "PYTHONUNBUFFERED "
    "pip_index_url NOTE"
).split()


@pytest.mark.parametrize("name", _SETTINGS)
@pytest.mark.parametrize(
    "form", ["ENV {}=1", "ENV {} 1", "ARG {}=1", "ARG {}", "ENV A=1 {}=copy B=2", 'ENV {}="see PIP_INDEX_URL docs"']
)
def test_dockerfile_requirements_reads_settings_that_dont_change_what_is_installed(tmp_path, name, form):
    lines, report = _inputs(tmp_path, form.format(name) + "\n" + _INSTALL)
    assert lines == [5]
    assert report.notes == []


_RISKY = (
    "PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_NO_BINARY PIP_ONLY_BINARY PIP_CONSTRAINT PIP_PRE PIP_PREFER_BINARY "
    "PIP_REQUIRE_HASHES PIP_FIND_LINKS PIP_NO_INDEX PIP_TRUSTED_HOST PIP_CONFIG_FILE PIP_USER PIP_TARGET PIP_PYTHON "
    "PIP_NO_DEPS PIP_UPGRADE_STRATEGY PIP_USE_FEATURE PIP_VERSION PIP_SOMETHING_NEW PIP_index_url UV_INDEX "
    "UV_INDEX_URL UV_DEFAULT_INDEX UV_EXTRA_INDEX_URL UV_FIND_LINKS UV_INDEX_STRATEGY UV_NO_BINARY "
    "UV_NO_BINARY_PACKAGE UV_NO_BUILD UV_PRERELEASE UV_RESOLUTION UV_EXCLUDE_NEWER UV_CONSTRAINT UV_OVERRIDE "
    "UV_PYTHON UV_PYTHON_PREFERENCE UV_CONFIG_FILE UV_NO_CONFIG UV_TORCH_BACKEND UV_SOMETHING_NEW"
).split()


@pytest.mark.parametrize("name", _RISKY)
@pytest.mark.parametrize("form", ["ENV {}=1", "ENV {} 1", "ARG {}=1", "ENV A=1 {}=$A", "ENV {}="])
def test_dockerfile_requirements_stops_on_settings_that_may_change_what_is_installed(tmp_path, name, form):
    with pytest.raises(DockerfileError, match=f":4: (ENV|ARG) {name} may change what pip or uv installs$"):
        _inputs(tmp_path, form.format(name) + "\n" + _INSTALL)
    # After the last install it changes nothing that was read.
    lines, _ = _inputs(tmp_path, _INSTALL + form.format(name) + "\n")
    assert lines == [4]


def test_dockerfile_requirements_settings_edge_cases(tmp_path):
    # An ARG with no default isn't in the RUN's environment: read as built without --build-arg, with a note.
    lines, report = _inputs(tmp_path, "ARG PIP_INDEX_URL\nARG UV_INDEX_URL PIP_EXTRA_INDEX_URL\n" + _INSTALL)
    assert lines == [6]
    assert [(line, kind) for line, kind, _ in report.notes] == [(4, "arg-unset"), (5, "arg-unset"), (5, "arg-unset")]
    assert "read as run without --build-arg PIP_INDEX_URL" in report.notes[0][2]
    # A --build-arg gives it a value, and so does a global ARG's default.
    path = tmp_path / "Dockerfile"
    with pytest.raises(DockerfileError, match=":4: ARG PIP_INDEX_URL may change"):
        dockerfile_requirements(path, {"PIP_INDEX_URL": "https://example.test/simple"})
    path.write_text(
        "ARG PIP_PRE=1\nFROM python:3.13\nARG PIP_PRE\nWORKDIR /app\nCOPY . .\n" + _INSTALL, encoding="utf-8"
    )
    with pytest.raises(DockerfileError, match=":3: ARG PIP_PRE may change"):
        dockerfile_requirements(path)
    # An ENV or ARG wheelgap can't split into names is refused as before when PIP_ or UV_ is anywhere in it.
    for line in ('ENV A="x\\"" PIP_PRE=1', "ENV --unset PIP_PRE", "ARG PIP_PRE=a\\ b"):
        path.write_text(f"FROM python:3.13\n{line}\nWORKDIR /app\nCOPY . .\n" + _INSTALL, encoding="utf-8")
        with pytest.raises(DockerfileError, match=":2: an (ENV|ARG) setting PIP_ or UV_ variables"):
            dockerfile_requirements(path)


def test_env_pairs_follow_buildkit():
    assert _env_pairs("ENV", "A=1 B='x y' C=\"z\"", {}, {}) == [
        ("A", "1", False),
        ("B", "x y", False),
        ("C", "z", False),
    ]
    assert _env_pairs("ENV", "PATH /opt/venv/bin:$PATH", {}, {}) == [("PATH", None, False)]
    assert _env_pairs("ENV", "NAME some  value", {}, {}) == [("NAME", "some  value", False)]
    assert _env_pairs("ENV", "A=1 B", {}, {}) is None  # Docker: can't find = in "B"
    assert _env_pairs("ENV", "ONLY", {}, {}) is None  # Docker: ENV must have two arguments
    assert _env_pairs("ARG", "A B=2 C", {"A": "x", "B": "y"}, {"C": "z"}) == [
        ("A", "x", False),
        ("B", "y", False),
        ("C", "z", False),
    ]
    assert _env_pairs("ARG", "A B=$A", {}, {}) == [("A", None, True), ("B", None, False)]


@pytest.mark.parametrize(
    ("env", "run", "error"),
    [
        ("ENV UV_SYSTEM_PYTHON=1", "uv pip install -r requirements.txt", None),
        ("ENV UV_SYSTEM_PYTHON=true", "uv pip install --no-cache -r requirements.txt", None),
        ("ENV UV_SYSTEM_PYTHON=0", "uv pip install -r requirements.txt", ":6: uv pip install needs --system"),
        ("ENV UV_SYSTEM_PYTHON=$X", "uv pip install -r requirements.txt", ":6: uv pip install needs --system"),
        (
            "ENV NOTE=1",
            "uv pip install -r requirements.txt",
            ":6: uv pip install needs --system \\(or ENV UV_SYSTEM_PYTHON=1\\)",
        ),
    ],
)
def test_dockerfile_requirements_reads_uv_system_python(tmp_path, env, run, error):
    body = f"COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /uvx /bin/\n{env}\nRUN {run}\n"
    if error is None:
        assert _inputs(tmp_path, body)[0] == [6]
    else:
        with pytest.raises(DockerfileError, match=error):
            _inputs(tmp_path, body)


def test_dockerfile_requirements_expands_env_and_arg_in_a_run(tmp_path):
    body = 'ENV REQS=requirements.txt DIR=/app\nARG NAME=requirements\nRUN pip install -r $DIR/${NAME}.txt -c "$REQS"\n'
    path = _context(tmp_path, "FROM python:3.13\nCOPY requirements.txt /app/\nWORKDIR /app\n" + body)
    constraints = {}
    assert dockerfile_requirements(path, constraints=constraints) == [(6, tmp_path / "requirements.txt")]
    assert constraints == {tmp_path / "requirements.txt": (tmp_path / "requirements.txt",)}
    # --build-arg replaces the ARG's default, as in the build.
    with pytest.raises(DockerfileError, match=":6: /app/other.txt isn't copied"):
        dockerfile_requirements(path, {"NAME": "other"}, constraints={})


def test_cli_dockerfile_inputs_says_what_it_passed_over_and_what_it_cant_see(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13\nRUN apt-get update && apt-get install -y gcc\nRUN pip install -U pip\nWORKDIR /app\n"
        "COPY requirements.txt .\nRUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt\n"
        "ARG PIP_INDEX_URL\nRUN curl -O https://example.test/x && ./install.sh\nRUN make assets\n",
    )
    argv = ["--dockerfile", str(path), "--dockerfile-inputs"]
    assert main(argv) == 0
    captured = capsys.readouterr()
    for note in (
        f"note: {path}: not read, since they install no Python packages with pip or uv: system package and account "
        "commands, line 2\n",
        f"note: {path}: not read, since they install no Python packages with pip or uv: other programs (curl), "
        "line 8\n",
        f"note: {path}: not read, since they install no Python packages with pip or uv: cache mounts, line 6\n",
        f"note: {path}: line 3 install or upgrade installer tools (pip): these and what they depend on aren't "
        "checked, and the pip version the later lines run isn't known",
        f"note: {path}:7: ARG PIP_INDEX_URL has no default, so the build is read as run without --build-arg",
        f"warning: {path}:8: this line runs `./install.sh`, a script or program at a path. If it installs Python "
        "packages, or another Python, that is not in this answer\n",
        f"warning: {path}:9: this line runs `make assets`, which runs commands wheelgap can't see.",
    ):
        assert note in captured.err
    # The report itself ends with it, so a green tick can't be read as "everything this image installs".
    unseen = (
        f"{path} lines 8, 9 run commands wheelgap can't see into: any Python packages they install are not in "
        "this answer."
    )
    assert captured.out.rstrip().endswith(f"! {unseen}")
    # --quiet drops the notes and keeps the warnings, on stderr and in the report.
    assert main([*argv, "--quiet"]) == 0
    captured = capsys.readouterr()
    assert "note:" not in captured.err
    assert captured.err.count("wheelgap: warning: ") == 2
    assert captured.out.strip() == f"! {unseen}"
    assert main([*argv, "--markdown"]) == 0
    assert capsys.readouterr().out.rstrip().endswith(f"> **Warning:** {unseen}")
    assert main([*argv, "--json", "--quiet"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(w["line"], w["kind"]) for w in data["dockerfile_warnings"]] == [(8, "may-install"), (9, "may-install")]
    assert "`make assets`" in data["dockerfile_warnings"][1]["message"]
    assert [(n["kind"], n.get("lines"), n["line"]) for n in data["dockerfile_notes"]] == [
        ("passed-over:system-packages", [2], None),
        ("passed-over:programs", [8], None),
        ("passed-over:cache-mounts", [6], None),
        ("passed-over:installer-tools", [3], None),
        ("arg-unset", None, 7),
    ]


def test_cli_dockerfile_inputs_json_has_no_warnings_when_every_run_is_read(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL)
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["dockerfile_warnings"] == []
    assert "dockerfile_notes" not in data
    assert "warning" not in captured.err
    # Without --dockerfile-inputs nothing is read from the RUN lines, so there is no key.
    assert main(["purewheel", "--dockerfile", str(path), "--json"]) == 0
    assert "dockerfile_warnings" not in json.loads(capsys.readouterr().out)


def _named(tmp_path, body, build_args=None, head="FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n"):
    path = _context(tmp_path, head + body)
    report = InputsReport()
    files = dockerfile_requirements(path, build_args, report=report, uv_installs=set(), constraints={})
    return [line for line, _ in files], report


@pytest.mark.parametrize(
    ("run", "names"),
    [
        ("pip install six", ["six"]),
        (
            "pip3 install --no-cache-dir \"six>=1.0\" 'numpy<3' purewheel==3.1",
            ["six>=1.0", "numpy<3", "purewheel==3.1"],
        ),
        ("apt-get update && python3 -m pip install -U six==1.16.0 && rm -rf /root/.cache", ["six==1.16.0"]),
        (
            "pip install \"six ; python_version < '3.9'\" six[extra]~=1.16",
            ["six ; python_version < '3.9'", "six[extra]~=1.16"],
        ),
        # Unquoted extras: a glob to the shell, which stays as written unless a file of that shape exists.
        ("pip install uvicorn[standard] fastapi[all]==0.115.0", ["uvicorn[standard]", "fastapi[all]==0.115.0"]),
        # Only pip, setuptools and wheel are passed over; any other tool is a package like the rest.
        ("pip install uv", ["uv"]),
        ("pip install --upgrade pip poetry==1.8.3", ["pip", "poetry==1.8.3"]),
        # A bare name is a requirement to pip even when a folder of that name exists.
        ("pip install app", ["app"]),
    ],
)
def test_dockerfile_requirements_reads_requirements_named_on_the_install_line(tmp_path, run, names):
    files, report = _named(tmp_path, f"RUN {run}\n")
    assert files == []
    assert report.named == [(4, names, False)]
    assert report.warnings == []


def test_dockerfile_requirements_reads_names_beside_files_and_under_uv(tmp_path):
    body = "RUN pip install -r requirements.txt six\nRUN pip install purewheel\n"
    files, report = _named(tmp_path, body + "RUN uv pip install --system --no-cache six==1.16.0 'purewheel==3.1'\n")
    assert files == [4]
    assert report.named == [
        (4, ["six"], False),
        (5, ["purewheel"], False),
        (6, ["six==1.16.0", "purewheel==3.1"], True),
    ]


def test_dockerfile_requirements_gathers_the_names_of_one_run_line(tmp_path):
    body = (
        "RUN pip install six && pip install purewheel==3.1 && uv pip install --system fastlib==2.0.0 && pip install six"
    )
    _, report = _named(tmp_path, f"COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /bin/\n{body}\n")
    assert report.named == [(5, ["six", "purewheel==3.1"], False), (5, ["fastlib==2.0.0"], True), (5, ["six"], False)]


def test_dockerfile_requirements_reads_a_name_from_an_arg_default_and_says_so(tmp_path):
    body = "ARG TORCH=2.1.0\nENV NAME=torch\nRUN pip install ${NAME}==$TORCH six==${SIX:-1}\n"
    with pytest.raises(DockerfileError, match=":6: .*six==\\$\\{SIX:-1\\} isn't a value"):
        _named(tmp_path, body)
    body = body.replace(" six==${SIX:-1}", "")
    _, report = _named(tmp_path, body)
    assert report.named == [(6, ["torch==2.1.0"], False)]
    note = "this install uses the default of ARG TORCH=2.1.0; pass --build-arg if the build sets one"
    assert report.notes == [(6, "arg-value", note)]
    _, report = _named(tmp_path, body, {"TORCH": "2.4.1"})
    assert report.named == [(6, ["torch==2.4.1"], False)]
    assert report.notes == [(6, "arg-value", "this install uses --build-arg TORCH=2.4.1")]
    # An ARG with no default and no --build-arg has no value to read.
    with pytest.raises(DockerfileError, match=":6: .*torch==\\$TORCH isn't a value --dockerfile-inputs can read"):
        _named(tmp_path, body.replace("ARG TORCH=2.1.0", "ARG TORCH"))


_ANOTHER_INDEX = (
    "this line installs from another index or a folder of files, where the files can differ from PyPI's, and "
    "--dockerfile-inputs only checks PyPI$"
)


@pytest.mark.parametrize(
    ("run", "error"),
    [
        # The project itself, a path, a file: named precisely, for a later version to read.
        ("pip install .", ":4: pip install \\.: a project folder or a file is installed here"),
        ("pip install ./pkg", ":4: pip install \\./pkg: a project folder or a file"),
        ("pip install '.[mcp]'", ":4: pip install \\.\\[mcp\\]: a project folder or a file"),
        ("pip install /src[qdrant,local]", ":4: pip install /src\\[qdrant,local\\]: a project folder or a file"),
        ("pip install dist/app-1.0-py3-none-any.whl", ":4: pip install dist/app-1.0-py3-none-any.whl: a project"),
        ("pip install app-1.0-py3-none-any.whl", ":4: pip install app-1.0-py3-none-any.whl: a project folder or"),
        ("pip install app-1.0.tar.gz six", ":4: pip install app-1.0.tar.gz: a project folder or a file"),
        ("pip install -e .", ":4: pip install -e: an editable install of a project folder"),
        ("pip install --editable=. six", ":4: pip install --editable: an editable install"),
        ("uv pip install --system -e .", ":4: uv pip install -e: an editable install"),
        # A URL: never echoed.
        (
            "pip install git+https://user:secret@example.test/r.git",
            ":4: pip install: a URL or something that isn't a requirement is installed here, which "
            "--dockerfile-inputs doesn't read \\(the text is left out",
        ),
        ("pip install https://example.test/app-1.0-py3-none-any.whl", ":4: pip install: a URL or something"),
        ("pip install 'six @ https://example.test/six.whl'", ":4: pip install: a URL or something"),
        ("pip install six==", ":4: pip install: a URL or something that isn't a requirement"),
        # Another index or a folder of files: requirements files ignore such a line (#632), and that isn't copied.
        ("pip install -i https://example.test/simple six", ":4: pip install -i: this line installs from another"),
        ("pip install --index-url=https://user:secret@example.test/simple six", ":4: pip install --index-url: "),
        ("pip install six --extra-index-url https://download.pytorch.org/whl/cpu", ":4: pip install --extra-index-url"),
        ("pip install -f /wheels six", ":4: pip install -f: this line installs from another index or a folder"),
        ("pip install --find-links=/wheels --no-index six", ":4: pip install --find-links: this line"),
        ("pip install --no-index six", ":4: pip install --no-index: this line"),
        ("pip install --trusted-host example.test six", ":4: pip install --trusted-host: this line"),
        ("pip install -ihttps://user:secret@example.test/simple six", ":4: pip install -i: " + _ANOTHER_INDEX),
        ("pip install -r requirements.txt --extra-index-url https://example.test/simple", ":4: .*--extra-index-url"),
        ("uv pip install --system --index https://example.test/simple six==1.16.0", ":4: uv pip install --index: "),
        ("uv pip install --system --default-index=https://example.test/simple six==1.16.0", ":4: .*--default-index"),
        # Values that aren't known, and flags that change what is picked.
        ("pip install six==$VERSION", ":4: .*six==\\$VERSION isn't a value --dockerfile-inputs can read"),
        ("pip install $PACKAGES", ":4: .*\\$PACKAGES isn't a value"),
        ("pip install $(cat names)", ":4: .* isn't a value"),
        ("pip install six*", ":4: .*six\\* isn't a value"),
        ("pip install {six,numpy}", ":4: .*\\{six,numpy\\} isn't a value"),
        ("pip install --pre six", ":4: pip install --pre: --dockerfile-inputs only reads"),
        ("pip install --require-hashes six", ":4: pip install --require-hashes: --dockerfile-inputs only reads"),
        ("pip install --only-binary=:all: six", ":4: pip install --only-binary=:all:: --dockerfile-inputs only"),
        ("pip install -c requirements.txt six", ":4: pip install -c FILE with requirements on the command line"),
        ("pip install --prefer-binary six", ":4: pip install --prefer-binary makes .* on the line is pinned"),
        ("pip install --no-cache-dir", ":4: pip install names nothing to install"),
        # A flag's value pip doesn't take isn't read, and the word after it isn't the flag's value (pip exits).
        ("pip install --timeout six", ":4: pip install --timeout: --dockerfile-inputs only reads"),
        ("pip install --progress-bar six", ":4: pip install --progress-bar: --dockerfile-inputs only reads"),
        ("uv pip install --system --link-mode six==1.16.0", ":4: uv pip install --link-mode: .* for uv"),
        # Only general options come before the subcommand: pip and uv exit on an install option there.
        ("pip --compile install six", ":4: `pip --compile install six`: only `install` is read of pip's"),
        ("pip -U install six", ":4: `pip -U install six`: only `install` is read of pip's"),
        ("uv pip --strict install --system six==1.16.0", ":4: .*only `install` is read of uv pip"),
        ("pip install six || true", ":4: `pip install six` is in a list with \\|\\|"),
        # uv picks its own version for a range, so under uv a name must be pinned.
        ("uv pip install --system six", ":4: uv pip install: six isn't pinned with ==, and uv and pip can pick"),
        ("uv pip install --system 'six>=1.16'", ":4: uv pip install: six>=1.16 isn't pinned"),
        ("uv pip install six==1.16.0", ":4: uv pip install needs --system"),
        ("uv pip install --system --prerelease=allow six==1.16.0", ":4: uv pip install --prerelease=allow: .* for uv"),
    ],
)
def test_dockerfile_requirements_stops_on_install_lines_it_cant_read_as_names(tmp_path, run, error):
    with pytest.raises(DockerfileError, match=error) as excinfo:
        _named(tmp_path, f"RUN {run}\n")
    assert "secret" not in str(excinfo.value)


def test_cli_dockerfile_inputs_checks_the_names_an_install_line_names(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM python:3.13\nRUN pip install --upgrade pip\nRUN pip install --no-cache-dir nowheels==0.9 purewheel\n"
        "WORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
        "COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /bin/\nRUN uv pip install --system fastlib==2.0.0\n",
    )
    (tmp_path / "requirements.txt").write_text("unpinned\n", encoding="utf-8")
    argv = ["--dockerfile", str(path), "--dockerfile-inputs"]
    assert main([*argv, "--json"]) == 1  # nowheels has no wheel
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert sorted((p["name"], p["version"]) for p in data["packages"]) == [
        ("fastlib", "2.0.0"),
        ("nowheels", "0.9"),
        ("purewheel", "3.1"),
        ("unpinned", "4.2"),
    ]
    assert data["sources"] == ["requirements.txt", f"{path}:3", f"{path}:8"]
    assert data["dockerfile_inputs"] == [
        {"dockerfile": str(path), "line": 6, "file": "requirements.txt", "constraints": [], "includes": []}
    ]
    assert data["dockerfile_packages"] == [
        {"dockerfile": str(path), "line": 3, "installer": "pip", "requirements": ["nowheels==0.9", "purewheel"]},
        {"dockerfile": str(path), "line": 8, "installer": "uv pip", "requirements": ["fastlib==2.0.0"]},
    ]
    assert data["dockerfile_warnings"] == []
    # The same note requirements files get for what isn't pinned, naming the line.
    assert (
        f"wheelgap: note: {path}:3: RUN pip install names 1 unpinned package (purewheel), so the newest matching "
        "release was checked; pin them to check what you install\n"
    ) in captured.err
    assert "requirements.txt has 1 unpinned package (unpinned)" in captured.err
    assert main(argv) == 1
    assert f"requirements.txt ({path}:6) + {path}:3 + {path}:8 · 4 packages" in capsys.readouterr().out


def test_cli_dockerfile_inputs_reads_a_dockerfile_that_only_names_packages(tmp_path, capsys, fake_pypi):
    # With no requirements file among the inputs, nothing else in the folder is read: not the lockfile beside it.
    path = _context(tmp_path, "FROM python:3.13-alpine\nRUN pip install purewheel==3.1\n", files=())
    (tmp_path / "uv.lock").write_text("not a lock\n", encoding="utf-8")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(p["name"], p["version"]) for p in data["packages"]] == [("purewheel", "3.1")]
    assert data["sources"] == [f"{path}:2"]
    assert data["dockerfile_inputs"] == []
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "musl-x86_64")]


_BASE = "FROM python:3.13-slim AS base\nWORKDIR /app\nCOPY requirements.txt .\n"


@pytest.mark.parametrize(
    ("dockerfile", "files", "named"),
    [
        # #601: the final stage is built FROM an earlier stage, whose WORKDIR, files and installs it has.
        (_BASE + "RUN apt-get update\nFROM base\n" + _INSTALL, [6], []),
        (_BASE + _INSTALL + "FROM base\nCOPY . .\n", [4], []),
        (_BASE + "RUN pip install six\nFROM base AS runtime\nRUN pip install purewheel==3.1\n", [], [4, 6]),
        # A chain of three, and stages that aren't on it: their lines aren't read at all.
        (
            "FROM node:22 AS web\nRUN npm ci && poetry install\n"
            + _BASE
            + "FROM base AS deps\n"
            + _INSTALL
            + "FROM python:3.13 AS other\nRUN pip install .\nFROM deps\nRUN pip install six\n",
            [7],
            [11],
        ),
        # The stage is found by its name; a later stage of the same name is the one a later FROM means.
        ("FROM python:3.13 AS base\nRUN pip install .\n" + _BASE + "FROM base\n" + _INSTALL, [7], []),
        ("ARG BASE=base\n" + _BASE + "FROM ${BASE}\n" + _INSTALL, [6], []),
        # ENV carries over to the stage built FROM it.
        (_BASE + "ENV REQS=requirements.txt UV_SYSTEM_PYTHON=1\nFROM base\nRUN pip install -r $REQS\n", [6], []),
    ],
)
def test_dockerfile_requirements_reads_the_stages_the_final_stage_builds_on(tmp_path, dockerfile, files, named):
    path = _context(tmp_path, dockerfile)
    report = InputsReport()
    found = dockerfile_requirements(path, report=report)
    assert [line for line, _ in found] == files
    assert [line for line, _, _ in report.named] == named
    assert report.warnings == report.copied == []


@pytest.mark.parametrize(
    ("dockerfile", "error"),
    [
        # What a parent stage does counts in the final stage: its settings, its config files, its unread lines.
        (_BASE + "ENV PIP_INDEX_URL=https://example.test/simple\nFROM base\n" + _INSTALL, ":4: ENV PIP_INDEX_URL may"),
        (_BASE + "ARG PIP_PRE=1\nFROM base\n" + _INSTALL, ":4: ARG PIP_PRE may change"),
        (_BASE + "RUN poetry install\nFROM base\n" + _INSTALL, ":4: `poetry install`: poetry installs"),
        (_BASE + "RUN sed -i s/a/b/ requirements.txt\nFROM base\n" + _INSTALL, ":6: requirements.txt may have been"),
        (_BASE + "RUN echo x > /etc/pip.conf\nFROM base\n" + _INSTALL, ":4: .* names /etc/pip.conf"),
        (_BASE + 'SHELL ["pwsh", "-c"]\nFROM base\n' + _INSTALL, ":4: SHELL .* changes how RUN lines run"),
        (_BASE + "ADD x.tgz /app/\nFROM base\n" + _INSTALL, ":4: ADD isn't read"),
        # An ARG of a parent stage: newer BuildKit carries it over and older ones don't, so its value isn't used.
        (_BASE + "ARG V=1.16.0\nFROM base\nRUN pip install six==$V\n", ":6: .*six==\\$V isn't a value"),
        # The final stage starts in the image's "/", not in the WORKDIR of a stage it isn't built on.
        ("FROM python:3.13\nWORKDIR /app\nFROM python:3.13\n" + _INSTALL, ":4: /requirements.txt isn't copied"),
        (_BASE + "FROM base\nRUN echo hi\n", "the final stage \\(line 4\\), like the 1 it builds on, has no `RUN pip"),
    ],
)
def test_dockerfile_requirements_stops_on_what_a_parent_stage_does(tmp_path, dockerfile, error):
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(_context(tmp_path, dockerfile), report=InputsReport())


@pytest.mark.parametrize(
    ("copy", "warns"),
    [
        # Only a single path that doesn't look like Python, and is a file or lands in a bin folder, is a note.
        ("COPY --from=web /app/dist/main.js /srv/static/", False),
        ("COPY --from=web /etc/ssl/certs/ca.pem /etc/ssl/certs/", False),
        ("COPY --link --from=docker.io/library/busybox:1.36 /bin/busybox /usr/local/bin/busybox", False),
        ("COPY --from=ghcr.io/example/tool:1 /tool /usr/local/bin/", False),
        # A folder may hold anything, so every other copy is a warning.
        ("COPY --from=web /app/dist /srv/static", True),
        ("COPY --from=web --chown=app:app /src/build ./static/", True),
        ("COPY --from=web /a.js /b.js /srv/static/", True),
        ("COPY --from=builder /usr/local/bin/python3.13 /usr/local/bin/", True),
        # Paths that may hold installed Python packages: the answer says it doesn't cover them.
        ("COPY --from=builder /opt/venv /opt/venv", True),
        ("COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages", True),
        ("COPY --from=builder /usr/local /usr/local", True),
        ("COPY --from=builder /install /usr/local", True),
        ("COPY --from=builder /build/wheels /wheels", True),
        ("COPY --from=builder /root/.local /opt/user", True),
        ("COPY --from=builder /src/app .", True),
        ("COPY --from=0 /app /app", True),
    ],
)
def test_dockerfile_requirements_says_what_copy_from_brings_instead_of_following_it(tmp_path, copy, warns):
    head = "FROM python:3.13 AS builder\nRUN pip install .\nFROM node:22 AS web\nFROM python:3.13\nWORKDIR /code\n"
    path = _context(tmp_path, f"{head}COPY requirements.txt .\n{_INSTALL}{copy}\n")
    report = InputsReport()
    assert [line for line, _ in dockerfile_requirements(path, report=report)] == [7]
    assert [line for line, _ in report.copied] == ([8] if warns else [])
    assert report.passed_over.get("copy-from", []) == ([] if warns else [8])


@pytest.mark.parametrize(
    ("copy", "run", "error"),
    [
        # A requirements file that may have come from another stage isn't read from the build context.
        (
            "COPY --from=builder /src/ /code/",
            _INSTALL,
            ":8: /code/requirements.txt may come from the COPY --from=builder on line 7",
        ),
        (
            "COPY --from=builder /src/requirements.txt requirements.txt",
            _INSTALL,
            ":8: /code/requirements.txt may come from",
        ),
        (
            "COPY --from=builder /src/reqs /code/requirements.txt/",
            "RUN pip install -r requirements.txt/base.txt\n",
            ":8: .* may come from",
        ),
        # A folder from elsewhere may carry a pip config file, or uv's.
        ("COPY --from=builder /root /root", _INSTALL, ":7: a pip config file may change what pip installs"),
        ("COPY --from=builder / /", _INSTALL, ":7: a pip config file"),
        ("COPY --from=builder /x/pip.conf /srv/conf", _INSTALL, ":7: a pip config file"),
        ("COPY --from=builder /cfg /home/app/.config/", _INSTALL, ":7: a pip config file"),
        (
            "COPY --from=builder /src/static /srv/static",
            "RUN uv pip install --system -r requirements.txt\n",
            ":7: COPY --from=builder may bring a uv.toml or pyproject.toml",
        ),
        # Forms that aren't split with certainty stay unread, and stop the installs after them.
        ("COPY --from=builder /src/*.whl /wheels/", _INSTALL, ":7: only a plain `COPY SRC... DEST`"),
        ("COPY --from=$STAGE /a /b", _INSTALL, ":7: only a plain `COPY SRC... DEST`"),
        ("COPY --from=builder --parents /a /b", _INSTALL, ":7: COPY --parents isn't read .*only --from, --chown"),
        ("COPY --from=builder --from=web /a /b", _INSTALL, ":7: COPY --from=web isn't read"),
    ],
)
def test_dockerfile_requirements_stops_on_a_copy_from_it_cant_rule_out(tmp_path, copy, run, error):
    head = "FROM python:3.13 AS builder\nFROM node:22 AS web\nFROM python:3.13\nWORKDIR /code\n"
    path = _context(
        tmp_path, f"{head}COPY requirements.txt .\nCOPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /bin/\n{copy}\n{run}"
    )
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(path, report=InputsReport(), uv_installs=set())


def test_dockerfile_requirements_reads_a_file_copied_over_what_copy_from_brought(tmp_path):
    body = "FROM python:3.13 AS builder\nFROM python:3.13\nCOPY --from=builder /src/static /code/\nWORKDIR /code\n"
    path = _context(tmp_path, body + "COPY requirements.txt .\n" + _INSTALL)
    assert dockerfile_requirements(path) == [(6, tmp_path / "requirements.txt")]


def test_cli_dockerfile_inputs_reads_a_multi_stage_dockerfile(tmp_path, capsys, fake_pypi):
    path = _context(
        tmp_path,
        "FROM ubuntu:24.04 AS base\nRUN apt-get update && apt-get install -y python3-pip\nWORKDIR /app\n"
        "FROM base AS builder\nRUN pip install nowheels==0.9\n"
        "FROM base\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
        "COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages\n"
        "COPY --from=builder /app/static ./static\nRUN ./start.sh --check\n",
    )
    argv = ["--dockerfile", str(path), "--dockerfile-inputs", "-p", "3.12", "-t", "linux-x86_64"]
    assert main([*argv, "--json"]) == 0  # the builder stage isn't one the final stage is built FROM
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert [p["name"] for p in data["packages"]] == ["purewheel"]
    assert data["dockerfile_inputs"] == [
        {"dockerfile": str(path), "line": 8, "file": "requirements.txt", "constraints": [], "includes": []}
    ]
    warnings = [(w["line"], w["kind"]) for w in data["dockerfile_warnings"]]
    assert warnings == [(9, "may-bring"), (10, "may-bring"), (11, "may-install")]
    assert "COPY --from=builder /usr/local/lib/python3.12/dist-packages" in data["dockerfile_warnings"][0]["message"]
    kinds = [(n["kind"], n.get("lines")) for n in data["dockerfile_notes"]]
    assert kinds == [("image-not-read", None), ("passed-over:system-packages", [2])]
    assert f"wheelgap: warning: {path}:9: COPY --from=builder " in captured.err
    assert "copies files from another stage or image, which wheelgap doesn't follow" in captured.err
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(
        f"! {path} lines 9, 10, 11 run commands wheelgap can't see into, or copy files from another stage or image: "
        "any Python packages they bring are not in this answer."
    )


def test_cli_dockerfile_inputs_says_when_the_system_package_manager_installs_python_packages(
    tmp_path, capsys, fake_pypi
):
    path = _context(
        tmp_path,
        "FROM python:3.13-alpine\nRUN apk add --no-cache git py3-yaml py3-gevent py3-pip python3-dev\n"
        "RUN apt-get install -y python3 python3.12-venv python3-numpy python-is-python3 && apk add py3-yaml\n"
        "WORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL,
    )
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    captured = capsys.readouterr()
    [note] = [n for n in json.loads(captured.out)["dockerfile_notes"] if n["kind"] == "system-python-packages"]
    assert note["lines"] == [2, 3]
    assert (
        "lines 2, 3 install Python packages with the system package manager (py3-yaml, py3-gevent, python3-numpy)"
        in (note["message"])
    )
    assert "pip keeps one of those when it satisfies a requirement" in captured.err
    # None of these is one: the interpreter's own pieces, and the installer.
    path.write_text(
        "FROM python:3.13\nRUN apt-get install -y python3 python3-pip python3-dev python3.12-venv python3-setuptools\n"
        "WORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL,
        encoding="utf-8",
    )
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    assert "system-python-packages" not in capsys.readouterr().out


def test_cli_dockerfile_unread_image_note_names_what_the_image_may_change(tmp_path, capsys, fake_pypi):
    # python:3-onbuild runs its own `pip install -r requirements.txt` in this build, and an image may set PIP_ values.
    path = _context(
        tmp_path, "FROM python:3-onbuild\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n"
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    argv = ["--dockerfile", str(path), "--dockerfile-inputs", "-p", "3.13", "-t", "linux-x86_64", "--json"]
    assert main(argv) == 0
    [note] = json.loads(capsys.readouterr().out)["dockerfile_notes"]
    for unknown in ("which pip version it has", "its own pip settings (ENV PIP_..., a pip.conf, a constraints file)"):
        assert unknown in note["message"]
    assert "ONBUILD instructions it runs in this build" in note["message"]


@pytest.mark.parametrize(
    ("image", "targets", "noted"),
    [
        ("python:3-alpine", "linux-x86_64,musl-x86_64", True),
        ("node:22-alpine3.20", "linux-aarch64", True),
        ("example/musl-python", "linux-x86_64", True),
        ("python:3-alpine", "musl-x86_64", False),  # musl was asked for
        ("ubuntu:24.04", "linux-x86_64", False),
    ],
)
def test_cli_dockerfile_alpine_tag_with_glibc_targets_gets_a_note(tmp_path, capsys, fake_pypi, image, targets, noted):
    path = _context(tmp_path, f"FROM {image}\n")
    assert main(["purewheel", "--dockerfile", str(path), "-p", "3.13", "-t", targets, "--json", "--quiet"]) == 0
    captured = capsys.readouterr()
    kinds = [note["kind"] for note in json.loads(captured.out)["dockerfile_notes"]]
    assert kinds == (["image-not-read", "image-libc"] if noted else ["image-not-read"])
    assert ("looks like an Alpine (musl) image, and glibc targets are checked" in captured.err) == noted


def test_cli_dockerfile_unread_image_note_in_markdown_annotations_and_sarif(tmp_path, capsys, fake_pypi, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    path = _context(tmp_path, "FROM ubuntu:24.04\n")
    sarif = tmp_path / "out.sarif"
    argv = ["purewheel", "--dockerfile", str(path), "-p", "3.13", "-t", "linux-x86_64"]
    assert main([*argv, "--markdown", "--github-annotations", "--sarif", str(sarif)]) == 0
    captured = capsys.readouterr()
    assert "> **Note:** " in captured.out and "the final image isn't read (ubuntu:24.04" in captured.out
    assert "::notice file=Dockerfile,line=1,title=wheelgap::the final image isn't read (ubuntu:24.04" in captured.err
    [result] = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]
    assert (result["ruleId"], result["level"]) == ("wheelgap/dockerfile-note", "note")
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "Dockerfile"
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": 1}
    # Without a note, the SARIF rules stay as they were.
    assert main(["purewheel", "-p", "3.13", "-t", "linux-x86_64", "--sarif", str(sarif)]) == 0
    rules = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["tool"]["driver"]["rules"]
    assert [rule["id"] for rule in rules] == ["wheelgap/source", "wheelgap/missing"]


# Review of the stack (#657): a line wheelgap can't see into stops when an install it reads comes after it, and the
# shell shapes that make an install not run, or run another pip, stop too.
_BEFORE_INSTALL = [
    # Lines wheelgap can't see into, before the install: they may rewrite the file, write a pip.conf, set PIP_ values.
    ("RUN ./setup.sh\n" + _INSTALL, ":4: this line runs `./setup.sh`, a script .* before the install on line 5"),
    ("RUN make deps && pip install -r requirements.txt\n", ":4: this line runs `make deps`.* on the same line"),
    ("RUN python manage.py migrate\n" + _INSTALL, ":4: this line runs `python manage.py migrate`"),
    ("RUN python -c 'import os'\n" + _INSTALL, ":4: this line runs `python -c"),
    ("RUN frobnicate --all\n" + _INSTALL, ":4: this line runs `frobnicate --all`, a program wheelgap doesn't know"),
    ("RUN dpkg -i /tmp/x.deb\n" + _INSTALL, ":4: this line runs `dpkg -i /tmp/x.deb`, which installs a package file"),
    ("RUN apt-get install -y ./x.deb\n" + _INSTALL, ":4: .*which installs a package file"),
    ("RUN rpm -Uvh /tmp/x.rpm\n" + _INSTALL, ":4: .*which installs a package file"),
    ("RUN apk add --allow-untrusted /tmp/x.apk\n" + _INSTALL, ":4: .*which installs a package file"),
    ('ENV PATH "/opt/x/bin:$PATH"\n' + _INSTALL, ":5: .*ENV PATH on line 4 puts /opt/x/bin first"),
    ("RUN bash -c 'pip install six' _\n" + _INSTALL, ":4: this line runs `bash -c"),
    ("RUN bash -lc 'pip install six'\n" + _INSTALL, ":4: this line runs `bash -lc"),
    ("RUN su -c 'pip install six' root\n" + _INSTALL, ":4: this line runs `su -c"),
    ("RUN timeout 600 pip install six\n" + _INSTALL, ":4: this line runs `timeout 600"),
    ("RUN sudo -H pip install six\n" + _INSTALL, ":4: this line runs `sudo -H"),
    ("RUN awk 'BEGIN{system(\"pip install six\")}'\n" + _INSTALL, ":4: this line runs `awk"),
    # M1: heredocs. One that names an installer stops wherever it is; any other before an install stops too.
    ("RUN <<EOF\necho 'idna==3.10' > requirements.txt\nEOF\n" + _INSTALL, ":4: this line runs a heredoc script"),
    (
        "RUN cat > /etc/pip.conf <<EOF\n[global]\nindex-url = https://example.invalid/simple\nEOF\n" + _INSTALL,
        ":4: this line runs a heredoc script",
    ),
    ("RUN <<EOF\npip install idna==3.10\nEOF\n" + _INSTALL, ":4: this RUN's heredoc names pip"),
    (_INSTALL + "RUN <<EOF\npython3 -m pip install idna\nEOF\n", ":5: this RUN's heredoc names python3"),
    (_INSTALL + "RUN python3 <<EOF\nprint(1)\nEOF\n", ":5: this RUN's heredoc names python3"),
    # M2: under sh, `&>` is `&` then `>`: pip runs in the background and the RUN doesn't wait for it.
    ("RUN pip install -r requirements.txt &> /dev/null\n", ":4: .* is run in the background"),
    ("RUN pip install -r requirements.txt &>> /tmp/log\n", ":4: .* is run in the background"),
    ("RUN sh -c 'pip install -r requirements.txt &> /dev/null'\n", ":4: .*inside a command substitution or `sh -c`"),
    # M3: installs that may not run while the build goes on.
    ('ARG MODE=prod\nRUN [ "$MODE" = dev ] && pip install -r requirements.txt; echo done\n', ":5: .*after &&"),
    ("RUN false && pip install -r requirements.txt; true\n", ":4: .*after && in a list that isn't the RUN's last"),
    (
        "RUN apt-get update && pip install -r requirements.txt\nRUN echo\n"
        if False
        else "RUN apt-get update && pip install -r requirements.txt &\n",
        ":4: .*run in the background",
    ),
    ("RUN exit 0; pip install -r requirements.txt\n", ":4: .*after `exit` on the same RUN"),
    ("RUN if true; then exit 0; fi; pip install -r requirements.txt\n", ":4: .*after `exit` on the same RUN"),
    ("RUN ! pip install -r requirements.txt\n", ":4: .*negated with `!`"),
    # M4: settings made in ways other than `export NAME=value`.
    (
        'SHELL ["/bin/bash", "-c"]\nRUN declare -x PIP_INDEX_URL=https://example.invalid/simple '
        "&& pip install -r requirements.txt\n",
        ":5: PIP_INDEX_URL is set on this RUN",
    ),
    ("RUN typeset -x PIP_INDEX_URL=https://example.invalid/simple && pip install -r requirements.txt\n", ":4: PIP_"),
    ("RUN . ./env.sh && pip install -r requirements.txt\n", ":4: this line runs `. ./env.sh`"),
    ('RUN eval "export PIP_INDEX_URL=x" && pip install -r requirements.txt\n', ":4: this line runs `eval"),
    ("RUN export $(cat .env | xargs) && pip install -r requirements.txt\n", ":4: `export \\$\\(cat .env"),
    ("RUN export $OPTIONS && pip install -r requirements.txt\n", ":4: `export \\$OPTIONS` may set any variable"),
    # M5: archives, patches and git write files they don't name.
    (
        "RUN tar xzf /tmp/src.tgz && pip install -r requirements.txt\n",
        ":4: requirements.txt may have been changed by line 4, which names \\.",
    ),
    (
        "RUN tar -xzf /tmp/src.tgz -C /app\n" + _INSTALL,
        ":5: requirements.txt may have been changed by line 4, which names /app",
    ),
    ("RUN unzip -o /tmp/src.zip\n" + _INSTALL, ":5: .* changed by line 4, which names \\."),
    ("RUN patch -p1 < /tmp/fix.patch\n" + _INSTALL, ":5: .* changed by line 4, which names \\."),
    ("RUN cpio -i < /tmp/a.cpio && pip install -r requirements.txt\n", ":4: this line runs `cpio"),
    ("RUN git checkout v2\n" + _INSTALL, ":5: .* changed by line 4, which may change files anywhere"),
    ("RUN git -C /src pull\n" + _INSTALL, ":5: .* changed by line 4, which may change files anywhere"),
    # M6: another Python than the image's.
    ("RUN /usr/bin/python3 -m pip install -r requirements.txt\n", ":4: .*doesn't know which Python /usr/bin/python3"),
    ("RUN /usr/bin/pip3 install -r requirements.txt\n", ":4: .*doesn't know which Python /usr/bin/pip3"),
    ("ENV PATH=/root/.local/bin:$PATH\n" + _INSTALL, ":5: .*ENV PATH on line 4 puts /root/.local/bin first"),
    ("ENV PATH /opt/conda/bin:$PATH\n" + _INSTALL, ":5: .*puts /opt/conda/bin first"),
    ("RUN export PATH=/opt/x/bin:$PATH && pip install -r requirements.txt\n", ":4: .*line 4 puts /opt/x/bin first"),
    ("RUN PATH=/opt/x/bin:$PATH pip install -r requirements.txt\n", ":4: .*puts /opt/x/bin first"),
    ("RUN . /opt/venv/bin/activate && pip install -r requirements.txt\n", ":4: .*puts /opt/venv/bin first"),
    ("ENV PATH=/opt/x/bin:$PATH\nRUN uv pip install --system -r requirements.txt\n", ":5: .*puts /opt/x/bin first"),
    ("ARG PATH=/opt/x/bin\n" + _INSTALL, ":5: .*the ARG on line 4 sets PATH"),
    # Config folders written whole, and mounts over the working folder or a config folder.
    ("RUN cp -r /tmp/etc/. /etc/\n" + _INSTALL, ":4: `cp -r /tmp/etc/. /etc/` writes into /etc/"),
    ("RUN cp -r /tmp/cfg/. /root/.config/\n" + _INSTALL, ":4: .* writes into /root/.config/"),
    ("RUN mv /tmp/.config /root/\n" + _INSTALL, ":4: .* writes into /root/"),
    ("RUN mv /tmp/cfg /root/.config\n" + _INSTALL, ":4: .* writes into /root/.config"),
    ("RUN ln -s /run/secrets/pip /etc/xdg/pip\n" + _INSTALL, ":4: .* names /etc/xdg/pip, which may be a pip"),
    (
        "RUN --mount=type=cache,target=/app pip install -r requirements.txt\n",
        ":4: a cache or tmpfs mount over /app covers /app/requirements.txt",
    ),
    (
        "RUN --mount=type=tmpfs,target=/etc/xdg/pip pip install -r requirements.txt\n",
        ":4: a cache or tmpfs mount over /etc/xdg/pip may put a pip config",
    ),
    (
        "RUN --mount=type=cache,dst=/root/.config pip install -r requirements.txt\n",
        ":4: a cache or tmpfs mount over /root/.config",
    ),
    (
        "RUN --mount=type=cache,target=$NOWHERE pip install -r requirements.txt\n",
        ":4: wheelgap can't tell which folder",
    ),
    # A pip that isn't the newest picks files by other rules.
    (
        "RUN pip install pip==19.3.1\n" + _INSTALL,
        ":4: this line runs `pip install pip==19.3.1`, which installs a pip version",
    ),
    ("RUN pip install 'pip<24' setuptools\n" + _INSTALL, ":4: .*installs a pip version wheelgap doesn't answer for"),
    # A name an ENV and then an ARG set: BuildKit gives the ARG's value, the classic builder the ENV's.
    (
        "ENV REQ=requirements.txt\nARG REQ=other.txt\nRUN pip install -r $REQ\n",
        ":6: .*REQ is set by an ENV and then by the ARG on line 5",
    ),
    (
        # Read as unset: BuildKit's 1 would be --system, the classic builder's 0 isn't.
        "ENV UV_SYSTEM_PYTHON=0\nARG UV_SYSTEM_PYTHON=1\nRUN uv pip install -r requirements.txt\n",
        ":6: uv pip install needs --system",
    ),
]


@pytest.mark.parametrize(("body", "error"), _BEFORE_INSTALL)
def test_dockerfile_requirements_stops_on_review_cases(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        _inputs(tmp_path, body)


@pytest.mark.parametrize(
    "body",
    [
        # After the last install, the same lines are only warnings: they can't change what the read install fetched.
        _INSTALL + "RUN ./setup.sh\n",
        _INSTALL + "RUN <<EOF\necho hi > /tmp/x\nEOF\n",
        _INSTALL + "RUN pip install pip==19.3.1\n",
        _INSTALL + "RUN awk 'BEGIN{system(\"pip install six\")}'\n",
        _INSTALL + "RUN timeout 600 pip install six\n",
    ],
)
def test_dockerfile_requirements_warns_after_the_last_install(tmp_path, body):
    lines, report = _inputs(tmp_path, body)
    assert lines == [4]
    assert [line for line, _ in report.warnings] == [5]


@pytest.mark.parametrize(
    "body",
    [
        # bash reads &> as a redirect; ARG then ENV gives the ENV's value in every builder.
        'SHELL ["/bin/bash", "-c"]\nRUN pip install -r requirements.txt &> /dev/null\n',
        'SHELL ["/bin/bash", "-o", "pipefail", "-c"]\nRUN pip install -r requirements.txt &>> /tmp/log\n',
        "ARG REQ=other.txt\nENV REQ=requirements.txt\nRUN pip install -r $REQ\n",
        # The RUN's last and-or list fails the build if a command in it fails, so its install runs in every build.
        "RUN echo start; apt-get update && pip install -r requirements.txt\n",
        "RUN set -eux; apt-get update && apt-get install -y gcc && pip install -r requirements.txt\n",
        # An exit inside a subshell only leaves the subshell.
        "RUN (exit 0); pip install -r requirements.txt\n",
        # Archives and patches elsewhere, and copies of named files into /etc.
        "RUN tar xzf /tmp/src.tgz -C /opt && pip install -r requirements.txt\n",
        "RUN unzip /tmp/a.zip -d /opt/a\n" + _INSTALL,
        "RUN git clone https://example.test/x /opt/x\n" + _INSTALL,
        "RUN cp /tmp/hosts.conf /etc/\n" + _INSTALL,
        "RUN mv /tmp/cfg /root/\n" + _INSTALL,
        # The image's own Python, the image's venv, and a PATH that keeps the image's Python first.
        "RUN /usr/local/bin/pip install -r requirements.txt\n",
        "RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt\n",
        "RUN python -m venv /opt/venv\nENV PATH=/opt/venv/bin:$PATH\n" + _INSTALL,
        "RUN python -m venv /opt/venv && . /opt/venv/bin/activate && pip install -r requirements.txt\n",
        "ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:$PATH\n" + _INSTALL,
        "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n" + _INSTALL,
        'ENV PATH "$PATH:/root/go/bin"\n' + _INSTALL,  # the legacy form, quoted: the image's Python stays first
        "ENV PATH=${PATH}:/opt/tool/bin\n" + _INSTALL,
        "RUN export PATH=/opt/x/bin:$PATH\n" + _INSTALL,  # a RUN's own PATH ends with it
        # Pip set-up that ends at the newest pip, and cache mounts elsewhere.
        "RUN pip install -U 'pip>=24' setuptools==70.0\n" + _INSTALL,
        "RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt\n",
        "RUN --mount=type=cache,target=.cache pip install -r requirements.txt\n",  # /app/.cache
        "RUN --mount=type=tmpfs,target=/tmp pip install -r requirements.txt\n",
    ],
)
def test_dockerfile_requirements_reads_review_cases(tmp_path, body):
    lines, report = _inputs(tmp_path, body)
    assert lines and report.warnings == []


@pytest.mark.parametrize(
    ("run", "pythons", "error"),
    [
        (
            "python3.11 -m pip install -r requirements.txt",
            ("3.13",),
            "installs for Python 3.11, and this run checks 3.13",
        ),
        ("pip3.11 install -r requirements.txt", ("3.13",), "installs for Python 3.11"),
        (
            "python3.13 -m pip install -r requirements.txt",
            ("3.13", "3.14"),
            "installs for Python 3.13, and this run checks 3.13, 3.14",
        ),
        ("python3.13 -m pip install -r requirements.txt", None, "a Python that isn't known"),
        (
            "python3.11 -m venv /venv && /venv/bin/pip install -r requirements.txt",
            ("3.13",),
            "installs for Python 3.11",
        ),
        ("python3.13 -m pip install -r requirements.txt", ("3.13",), None),
        ("pip3.13 install -r requirements.txt", ("3.13",), None),
        ("python3.13 -m venv /venv && /venv/bin/python -m pip install -r requirements.txt", ("3.13",), None),
    ],
)
def test_dockerfile_requirements_versioned_pythons_must_be_the_one_checked(tmp_path, run, pythons, error):
    path = _context(tmp_path, f"FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\nRUN {run}\n")
    if error is None:
        assert len(dockerfile_requirements(path, pythons=pythons)) == 1
    else:
        with pytest.raises(DockerfileError, match=error):
            dockerfile_requirements(path, pythons=pythons)


def test_dockerfile_requirements_reads_bin_dir_pythons_of_an_image_it_cant_read(tmp_path):
    # With -p/-t for the image, /usr/bin/pip3 is as much the image's as `pip3`; an official image's is Debian's.
    for image, read in (("ubuntu:24.04", True), ("python:3.12", False)):
        path = _context(
            tmp_path,
            f"FROM {image}\nWORKDIR /app\nCOPY requirements.txt .\nRUN /usr/bin/pip3 install -r requirements.txt\n",
        )
        if read:
            assert len(dockerfile_requirements(path, pythons=("3.12",))) == 1
        else:
            with pytest.raises(DockerfileError, match="doesn't know which Python /usr/bin/pip3"):
                dockerfile_requirements(path, pythons=("3.12",))


def test_dockerfile_requirements_notes_only_system_python_packages_before_the_install(tmp_path):
    _, report = _inputs(tmp_path, "RUN apk add py3-yaml\n" + _INSTALL + "RUN apk add py3-flask\n")
    assert report.system_python == [(4, "py3-yaml")]
    _, report = _inputs(tmp_path, "RUN apk add py3-yaml && pip install -r requirements.txt && apk add py3-flask\n")
    assert report.system_python == [(4, "py3-yaml")]


def test_cli_dockerfile_inputs_warnings_cant_be_missed(tmp_path, capsys, fake_pypi, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    path = _context(
        tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL + "RUN ./post.sh\n"
    )
    argv = ["--dockerfile", str(path), "--dockerfile-inputs"]
    # Text: no green tick; the headline says what the answer covers.
    assert main(argv) == 0  # warnings don't change the exit code
    out = capsys.readouterr().out
    assert "✓" not in out
    assert "! The 1 package wheelgap read has a wheel for 1 target; line 5 wasn't seen into." in out
    # Markdown: the headline too.
    assert main([*argv, "--markdown"]) == 0
    out = capsys.readouterr().out
    assert "### wheelgap: the 1 package wheelgap read has a wheel for 1 target; line 5 wasn't seen into" in out
    assert "every package has a wheel" not in out
    # --json: summary.complete.
    assert main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["complete"] is False
    # Annotations, SARIF and JUnit.
    sarif, junit = tmp_path / "out.sarif", tmp_path / "out.xml"
    assert main([*argv, "--github-annotations", "--sarif", str(sarif), "--junit", str(junit)]) == 0
    assert "::warning file=Dockerfile,line=5,title=wheelgap::this line runs `./post.sh`" in capsys.readouterr().out
    [result] = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]
    assert (result["ruleId"], result["level"]) == ("wheelgap/dockerfile-unseen", "warning")
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": 5}
    xml = junit.read_text(encoding="utf-8")
    assert '<property name="wheelgap.dockerfile-warning" value="' in xml
    assert f"<system-out>wheelgap: warning: {path}:5: this line runs `./post.sh`" in xml
    # With nothing unseen: complete, and the green tick.
    path.write_text("FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL, encoding="utf-8")
    assert main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["complete"] is True
    assert main(argv) == 0
    assert "✓ The 1 package has a wheel" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("head", "files", "run", "error"),
    [
        # The shell expands an unquoted six[a] to a file named sixa if the folder has one, and pip installs that.
        (
            "WORKDIR /app\nCOPY . .\n",
            ["sixa"],
            "pip install six[a]",
            ":4: six\\[a\\] is a glob .* COPY on line 3 puts sixa",
        ),
        ("WORKDIR /app\nCOPY sixa .\n", ["sixa"], "pip install six[a]", "COPY on line 3 puts sixa in /app"),
        ("WORKDIR /app\nCOPY src/ ./\n", ["src/sixa"], "pip install six[a]", "puts sixa in /app"),
        ("WORKDIR /app/sixa\nWORKDIR /app\n", [], "pip install six[a]", "a WORKDIR puts sixa in /app"),
        ("WORKDIR /app\n", [], "mkdir sixa && pip install six[a]", "line 3 may put sixa in /app"),
        ("WORKDIR /app\n", [], "git clone https://example.test/x . && pip install six[a]", "may put files in /app"),
        # Where the folder's contents aren't known, it isn't read either.
        ("FROM ubuntu:24.04\n", [], "pip install six[a]", "the folder it runs in isn.t known"),
        ("", [], "pip install us[r]", ":2: us\\[r\\] is a glob .* and the image puts usr in /"),  # g30
        ("COPY sixa /\n", ["sixa"], "pip install six[a]", "the COPY on line 2 puts sixa in /"),
        ("", [], "mkdir sixa && pip install six[a]", "line 2 may put sixa in /"),  # g28
        ("WORKDIR /usr/src/app\n", [], "pip install six[a]", "/usr/src/app is a folder of the image"),
        ("WORKDIR /\n", [], "pip install li[b]", "the image puts lib in /"),
        # Paths with brackets are a project folder or an editable install, not a glob to wheelgap.
        ("WORKDIR /app\n", [], "pip install ./x[dev]", "pip install ./x\\[dev\\]: a project folder or a file"),
        ("WORKDIR /app\n", [], "pip install -e ./x[dev]", "pip install -e ./x\\[dev\\]: an editable install"),
        ("WORKDIR /app\n", [], "pip install --editable=./x[dev]", "an editable install of a project folder"),
        # ENV then ARG: BuildKit's RUN sees the ARG's value, the classic builder's the ENV's.
        ("ENV VER=1.15.0\nARG VER=1.16.0\n", [], "pip install six==$VER", "VER is set by an ENV and then by the ARG"),
    ],
)
def test_dockerfile_requirements_name_globs_and_paths(tmp_path, head, files, run, error):
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises(DockerfileError, match=error):
        _named(tmp_path, f"RUN {run}\n", head=f"FROM python:3.13\n{head}")


@pytest.mark.parametrize(
    ("head", "run", "names"),
    [
        # No file in the folder matches: the word is the requirement it spells.
        ("WORKDIR /app\nCOPY requirements.txt .\n", "pip install six[a]", ["six[a]"]),
        ("WORKDIR /app\nCOPY . .\n", "pip install uvicorn[standard]", ["uvicorn[standard]"]),
        ("WORKDIR /app\n", "pip install 'six[a]'", ["six[a]"]),  # quoted: never a glob
        ("WORKDIR /app\nRUN mkdir -p /data/sixa\n", "pip install six[a]", ["six[a]"]),
        ("WORKDIR /app\nRUN mkdir data\n", "pip install six[a]", ["six[a]"]),
        # No WORKDIR on the python image: `/`, whose names are known (Scrapegraph-ai's scrapegraphai[burr]).
        ("", "pip install scrapegraphai[burr]", ["scrapegraphai[burr]"]),
        # ARG then ENV, or an ARG with no value after an ENV: every builder gives the ENV's value.
        ("ARG VER=1.16.0\nENV VER=1.15.0\n", "pip install six==$VER", ["six==1.15.0"]),
        ("ENV VER=1.15.0\nARG VER\n", "pip install six==$VER", ["six==1.15.0"]),
    ],
)
def test_dockerfile_requirements_reads_names_no_file_can_match(tmp_path, head, run, names):
    _, report = _named(tmp_path, f"RUN {run}\n", head=f"FROM python:3.13\n{head}")
    assert report.named == [(head.count("\n") + 2, names, False)]


_M8_HEAD = "FROM python:3.13 AS build\nRUN pip install --target=/packages idna==3.10\nFROM python:3.13\nWORKDIR /app\n"


# Second review (#657): writes over the image's Python or pip, installers named inside other programs' arguments,
# and PATH values BuildKit expands.
_PI = "RUN python -m pip install -r requirements.txt\n"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # A copied folder the image's Python then uses, before an install: it may bring another pip.
        (
            "COPY --from=build /packages /packages\nENV PYTHONPATH=/packages\nCOPY requirements.txt .\n" + _INSTALL,
            ":6: this line uses files copied from another stage or image \\(ENV PYTHONPATH names /packages",
        ),
        (
            "COPY --from=build /opt/env /opt/env\nENV PATH=/opt/env/bin:$PATH\nCOPY requirements.txt .\n" + _INSTALL,
            ":8: .*pip may not be the image.s own, since ENV PATH on line 6 puts /opt/env/bin first",
        ),
        (
            "COPY --from=build /packages /usr/local/lib/python3.13/site-packages\nCOPY requirements.txt .\n" + _INSTALL,
            ":5: this line copies files over the image's Python or pip",
        ),
        (
            "COPY --from=build /root/.local /root/.local\nCOPY requirements.txt .\n" + _INSTALL,
            ":5: this line copies files over the image's Python or pip",
        ),
        ("COPY --from=build /usr/local /usr/local\nCOPY requirements.txt .\n" + _INSTALL, ":5: a pip config file"),
        # Where pip reads its config from, moved to a copied folder (or a copy into where it was moved).
        (
            "COPY --from=build /cfg /cfg\nENV HOME=/cfg\nCOPY requirements.txt .\n" + _INSTALL,
            ":6: ENV HOME makes pip read its config from a folder the COPY --from=build on line 5 brought",
        ),
        (
            "COPY --from=build /cfg /etc/cfg\nENV XDG_CONFIG_DIRS=/etc/cfg\nCOPY requirements.txt .\n" + _INSTALL,
            ":6: ENV XDG_CONFIG_DIRS makes pip read its config from a folder",
        ),
        (
            "ENV HOME=/srv/me\nCOPY --from=build /cfg /srv/me/.config\nCOPY requirements.txt .\n" + _INSTALL,
            ":6: a pip config file may change what pip installs",
        ),
        ("ENV HOME=$X\nCOPY requirements.txt .\n" + _INSTALL, ":5: ENV HOME moves where pip reads its config"),
        # An unquoted name[extra] in a folder a COPY --from wrote to.
        (
            "COPY --from=build /x /app/x\nRUN pip install six[a]\n",
            "the COPY --from=build on line 5 may put files in /app",
        ),
    ],
)
def test_dockerfile_requirements_stops_on_what_a_copy_from_may_change(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(_context(tmp_path, _M8_HEAD + body), report=InputsReport())


def test_dockerfile_requirements_warns_about_packages_copied_from_a_build_stage(tmp_path):
    # M8: the final image has idna from the build stage; the answer is six alone, so it must say so.
    body = "COPY requirements.txt .\n" + _INSTALL + "COPY --from=build /packages /packages\nENV PYTHONPATH=/packages\n"
    report = InputsReport()
    assert [line for line, _ in dockerfile_requirements(_context(tmp_path, _M8_HEAD + body), report=report)] == [6]
    assert [line for line, _ in report.copied] == [7, 8]
    assert "ENV PYTHONPATH names /packages, which the COPY --from=build on line 7 brought" in report.copied[1][1]
    assert report.passed_over.get("copy-from") is None
    # A copy that can't replace the image's Python, before the install, is a warning too, not a stop.
    body = "COPY --from=build /app/static ./static\nCOPY requirements.txt .\n" + _INSTALL
    report = InputsReport()
    assert [line for line, _ in dockerfile_requirements(_context(tmp_path, _M8_HEAD + body), report=report)] == [7]
    assert [line for line, _ in report.copied] == [5]


def test_cli_dockerfile_inputs_says_a_single_copied_file_is_not_followed(tmp_path, capsys, fake_pypi):
    body = "COPY --from=build /bin/tool /usr/local/bin/\nCOPY requirements.txt .\n" + _INSTALL
    path = _context(tmp_path, _M8_HEAD + body)
    assert main(["--dockerfile", str(path), "--dockerfile-inputs", "--json"]) == 0
    captured = capsys.readouterr()
    [note] = [n for n in json.loads(captured.out)["dockerfile_notes"] if n["kind"] == "passed-over:copy-from"]
    assert note["message"] == "not followed: files copied from another stage or image, line 5"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # A: a RUN that writes over the Python or pip a later install runs (case names from the review).
        (
            "RUN ln -sf /usr/bin/python3 /usr/local/bin/python\n" + _PI,
            ":4: this line writes /usr/local/bin/python",
        ),  # g15
        (
            "RUN update-alternatives --install /usr/local/bin/python python /usr/bin/python3 99\n" + _PI,  # g16
            ":4: this line writes /usr/local/bin/python, which may replace the Python or pip",
        ),
        (
            "RUN printf '#!/bin/sh\\nexec pip3 \"$@\"\\n' > /usr/local/bin/pip\n" + _INSTALL,
            ":4: .*writes /usr/local/bin/pip",
        ),
        ("RUN cp /usr/bin/pip3 /usr/local/bin/pip\n" + _INSTALL, ":4: .*writes /usr/local/bin/pip"),  # p19
        ("RUN echo x | tee /usr/local/bin/pip\n" + _INSTALL, ":4: .*writes /usr/local/bin/pip"),  # p20
        ("RUN cd /usr/local/bin && ln -sf /usr/bin/pip3 pip\n" + _INSTALL, ":4: this line writes pip"),  # p17
        (
            "RUN sed -i s/x/y/ /usr/local/lib/python3.13/site-packages/pip/_internal/models/target_python.py\n"
            + _INSTALL,
            ":4: .*writes /usr/local/lib/python3.13/site-packages/pip",
        ),  # p21
        ("RUN echo /opt/deps > /usr/local/lib/python3.13/site-packages/deps.pth\n" + _INSTALL, ":4: .*deps.pth"),  # p23
        (
            "RUN python -m venv /venv && ln -sf /usr/bin/python3 /venv/bin/python\n"
            "RUN /venv/bin/pip install -r requirements.txt\n",
            ":4: .*writes /venv/bin/python",
        ),  # p02
        # C: an installer named in a passed-over program's arguments.
        (
            "RUN node -e \"require('child_process').execSync('pip install idna==3.10')\"\n" + _INSTALL,
            ":4: .*names an installer in its arguments",
        ),  # r1
        ("RUN ruby -e 'system(\"pip install idna==3.10\")'\n" + _INSTALL, ":4: .*names an installer"),  # r2
        (
            "RUN echo x | sed 'e pip install idna==3.10'\n" + _INSTALL,
            ":4: .*`sed e pip install idna==3.10`, which names an installer",
        ),  # r3
        ("RUN git -c alias.x='!pip install idna==3.10' x\n" + _INSTALL, ":4: .*names an installer"),  # r5
        ("RUN npm exec -- python3 -m pip install idna\n" + _INSTALL, ":4: .*names an installer"),
        # bash's `hash -p` makes `pip` run another file.
        (
            'SHELL ["/bin/bash", "-c"]\nRUN hash -p /usr/bin/pip3 pip && pip install -r requirements.txt\n',
            ":5: .*hash -p",
        ),  # p15
    ],
)
def test_dockerfile_requirements_stops_on_second_review_cases(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        _inputs(tmp_path, body)


def test_dockerfile_requirements_stops_on_update_alternatives_over_the_python_checked(tmp_path):
    # s1: with -p/-t for ubuntu:22.04, python3 is the image's 3.10, until update-alternatives makes it 3.11.
    run = (
        "RUN apt-get update && apt-get install -y python3.11 python3-pip && "
        "update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1\n"
    )
    head = "FROM ubuntu:22.04\nWORKDIR /app\nCOPY requirements.txt .\n"
    path = _context(tmp_path, head + run + "RUN python3 -m pip install -r requirements.txt\n")
    with pytest.raises(DockerfileError, match=":4: this line writes /usr/bin/python3, which may replace"):
        dockerfile_requirements(path, pythons=("3.10",))
    path = _context(
        tmp_path,
        head + "RUN ln -sf /usr/bin/python3.11 /usr/bin/python3\nRUN python3 -m pip install -r requirements.txt\n",
    )
    with pytest.raises(DockerfileError, match=":4: this line writes /usr/bin/python3"):  # s2
        dockerfile_requirements(path, pythons=("3.10",))


@pytest.mark.parametrize(
    "body",
    [
        # Writes elsewhere, and copies from the Python's folders, are still passed over.
        "RUN ln -s /usr/local/bin/python3 /opt/python\n" + _INSTALL,
        "RUN cp /usr/local/bin/pip /tmp/pip-backup\n" + _INSTALL,
        "RUN echo x > /usr/local/bin/myscript && chmod +x /usr/local/bin/myscript\n" + _INSTALL,
        "RUN node -e \"console.log('hi')\" && git -c core.x=1 status\n" + _INSTALL,
        "RUN echo 'run pip install -r requirements.txt' > /tmp/README\n" + _INSTALL,
        # BuildKit expands an ENV value with the values before it: the usual venv recipe reads.
        'ENV VIRTUAL_ENV=/opt/venv\nRUN python -m venv $VIRTUAL_ENV\nENV PATH="$VIRTUAL_ENV/bin:$PATH"\n'
        + _INSTALL,  # p01
        "RUN python -m venv /venv\nENV PATH /venv/bin:$PATH\n" + _INSTALL,  # g19
        # Read-only probes after a PATH change don't install, whichever Python they run.
        "ENV PATH=/opt/tools/bin:$PATH\nRUN python --version && pip --version && pip list && python -m pip freeze\n",
    ],
)
def test_dockerfile_requirements_reads_second_review_cases(tmp_path, body):
    lines, report = _inputs(
        tmp_path, body if "pip install" in body else body + "ENV PATH=/usr/local/bin:$PATH\n" + _INSTALL
    )
    assert lines and report.warnings == []


def test_dockerfile_requirements_path_entry_with_an_unknown_value_is_shown_as_written(tmp_path):
    with pytest.raises(DockerfileError, match=r"puts \$VIRTUAL_ENV/bin, a value wheelgap doesn't know, first"):
        _inputs(tmp_path, 'ENV PATH="$VIRTUAL_ENV/bin:$PATH"\n' + _INSTALL)


def test_dockerfile_requirements_reads_the_root_folder_of_an_official_python_image(tmp_path):
    # No WORKDIR: BuildKit runs and copies in "/", since the python image sets no WorkingDir.
    path = _context(tmp_path, "FROM python:3.13\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n")
    assert [line for line, _ in dockerfile_requirements(path)] == [3]


def test_dockerfile_requirements_names_the_command_after_nice(tmp_path):
    with pytest.raises(DockerfileError, match=":4: this line runs `./x.sh`, a script"):  # g07
        _inputs(tmp_path, "RUN nice -n 10 ./x.sh\n" + _INSTALL)


def test_cli_dockerfile_inputs_unread_image_is_not_complete(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, "FROM ubuntu:24.04\nWORKDIR /app\nCOPY requirements.txt .\n" + _INSTALL)
    argv = ["--dockerfile", str(path), "--dockerfile-inputs", "-p", "3.12", "-t", "linux-x86_64"]
    assert main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["complete"] is False
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "✓" not in out
    assert "! The 1 package wheelgap read has a wheel for 1 target; the image wasn't read (-p and -t stand in" in out
    assert main([*argv, "--markdown"]) == 0
    assert (
        "### wheelgap: the 1 package wheelgap read has a wheel for 1 target; the image wasn't read"
        in capsys.readouterr().out
    )


_IMG = "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\n"
_SITE = "COPY --from=python:3.12 /usr/local/lib/python3.12/site-packages"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # B: a folder PYTHONPATH and the others name, set before the COPY --from that fills it (q1, g21 to g24).
        (
            f"ENV PYTHONPATH=/deps\n{_SITE} /deps\n" + _INSTALL,
            ":5: this line copies files into a folder ENV PYTHONPATH on line 4 names",
        ),
        (f"ENV PYTHONPATH=/deps:${{PYTHONPATH}}\n{_SITE} /deps\n" + _INSTALL, ":5: .*ENV PYTHONPATH on line 4 names"),
        (f"ENV PYTHONPATH /deps\n{_SITE} /deps\n" + _INSTALL, ":5: .*ENV PYTHONPATH on line 4 names"),
        ("ENV PYTHONUSERBASE=/deps\nCOPY --from=python:3.12 /usr/local /deps\n" + _INSTALL, ":5: .*ENV PYTHONUSERBASE"),
        (f"ENV PYTHONPATH=/app\n{_SITE} /app/lib\n" + _INSTALL, ":5: .*ENV PYTHONPATH on line 4 names"),
        ("ENV DEPS=/deps\nENV PYTHONPATH=$DEPS\n" + f"{_SITE} /deps\n" + _INSTALL, ":6: .*ENV PYTHONPATH on line 5"),
        # g27: `python -m pip` imports from the working folder first.
        (
            "COPY --from=tools:1 /pip /app/pip\nRUN python -m pip install -r requirements.txt\n",
            ":4: COPY --from=tools:1 may put files in /app, and `python -m pip`",
        ),
        (
            "COPY --from=tools:1 /stuff/ /app/\nRUN python3 -m pip install -r requirements.txt\n",
            ":4: .*`python -m pip`",
        ),
        ("COPY --from=tools:1 /x /app/json\nRUN python3 -m pip install -r requirements.txt\n", ":4: .*`python -m pip`"),
    ],
)
def test_dockerfile_requirements_stops_on_a_copy_into_a_folder_python_uses(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        dockerfile_requirements(_context(tmp_path, _IMG + body), report=InputsReport())


def test_dockerfile_requirements_reads_copies_that_cant_change_the_install(tmp_path):
    # g39: a lib folder's other entries aren't the image's Python; and `pip` (not `python -m pip`) doesn't import
    # from the working folder.
    for body in (
        "COPY --from=node:20 /usr/local/lib/node_modules /usr/local/lib/node_modules\n" + _INSTALL,
        "COPY --from=web:1 /dist /app/static\nRUN python -m pip install -r requirements.txt\n",
    ):
        report = InputsReport()
        assert len(dockerfile_requirements(_context(tmp_path, _IMG + body), report=report)) == 1
        assert [line for line, _ in report.copied] == [4]
    for dest in ("/usr/local/lib", "/usr/local/lib/python3.13", "/usr/lib/libpython3.13.so.1.0", "/usr/local"):
        with pytest.raises(DockerfileError, match="copies files over the image's Python or pip|a pip config file"):
            dockerfile_requirements(_context(tmp_path, _IMG + f"COPY --from=x:1 /a {dest}\n" + _INSTALL))


def test_dockerfile_requirements_stops_on_copied_packages_with_deps(tmp_path):
    # pip keeps a copied package that satisfies a requirement, and its own dependencies then decide the rest.
    path = _context(tmp_path, _IMG + "COPY --from=build:1 /packages /packages\n" + _INSTALL)
    assert len(dockerfile_requirements(path)) == 1
    with pytest.raises(DockerfileError, match=":4: COPY --from=build:1 may bring installed packages, which pip keeps"):
        dockerfile_requirements(path, dependencies=True)


def test_cli_dockerfile_inputs_names_one_line_in_the_singular(tmp_path, capsys, fake_pypi):
    path = _context(tmp_path, _IMG + _INSTALL + "COPY --from=web:1 /dist /app/static\n")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs"]) == 0
    assert f"{path} line 5 copies files from another stage or image: any Python" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # C2: an installer named in another program's arguments, with options or separate quoted words in between.
        (
            "RUN node -e \"require('child_process').execSync('pip --no-cache-dir install idna')\"\n" + _INSTALL,
            ":4: .*names an installer",
        ),
        ('RUN ruby -e \'system("pip", "install", "idna")\'\n' + _INSTALL, ":4: .*names an installer"),
        (
            "RUN node -e \"require('child_process').execFileSync('pip', ['install','idna'])\"\n" + _INSTALL,
            ":4: .*names an installer",
        ),
        ("RUN git -c alias.x='!pip -q install idna' x\n" + _INSTALL, ":4: .*names an installer"),
        # E: bash -c sources $BASH_ENV before every RUN.
        (
            'SHELL ["/bin/bash", "-c"]\nRUN echo "export PIP_INDEX_URL=https://example.invalid/simple" > /etc/x.sh\n'
            "ENV BASH_ENV=/etc/x.sh\n" + _INSTALL,
            ":7: bash runs the file ENV BASH_ENV on line 6",
        ),
        # B2: an ENV entry wheelgap can't expand may be any folder, so a later copy into it stops.
        (
            "ARG BASE\nENV PYTHONPATH=$BASE/deps\n"
            "COPY --from=python:3.13 /usr/local/lib/python3.13/site-packages /deps\n" + _INSTALL,
            ":6: this line copies files into a folder ENV PYTHONPATH on line 5 names",
        ),
    ],
)
def test_dockerfile_requirements_stops_on_third_review_cases(tmp_path, body, error):
    with pytest.raises(DockerfileError, match=error):
        _inputs(tmp_path, body)


def test_dockerfile_requirements_reads_only_plain_runs_on_a_windows_image(tmp_path):
    # D: on a Windows image RUN runs in cmd /S /C, where `set X=...&& pip install` gives pip another index.
    head = "FROM python:3.12-windowsservercore\nWORKDIR /app\nCOPY requirements.txt .\n"
    path = _context(tmp_path, head + "RUN set PIP_INDEX_URL=https://example.invalid/simple&& pip install six==1.16.0\n")
    with pytest.raises(DockerfileError, match=":4: on a Windows image this RUN runs in cmd"):
        dockerfile_requirements(path)
    path = _context(tmp_path, head + "RUN pip install -r requirements.txt\n")
    assert dockerfile_requirements(path)


@pytest.mark.parametrize("filename", ["requirements.pip", "deps.py", "pylock.a.b.toml", "deps"])
def test_cli_dockerfile_inputs_reads_pip_requirements_under_any_filename(tmp_path, capsys, fake_pypi, filename):
    (tmp_path / filename).write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"FROM python:3.13-slim\nWORKDIR /app\nCOPY {filename} .\nRUN pip install -r {filename}\n",
        encoding="utf-8",
    )
    assert main(["--dockerfile", str(dockerfile), "--dockerfile-inputs", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert sorted((p["name"], p["version"]) for p in data["packages"]) == [
        ("nowheels", "0.9"),
        ("purewheel", "3.1"),
    ]


@pytest.mark.parametrize("filename", ["pylock.toml", "pylock.dev.toml"])
def test_cli_dockerfile_inputs_reads_a_pip_lines_pylock_as_a_lock(tmp_path, capsys, fake_pypi, fixtures, filename):
    # pip 26.1 and later install `pip install -r pylock.toml` (or pylock.NAME.toml) as a PEP 751 lock.
    (tmp_path / filename).write_bytes((fixtures / "pylockproj" / "pylock.toml").read_bytes())
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"FROM python:3.13-slim\nWORKDIR /app\nCOPY {filename} .\nRUN pip install -r {filename}\n", encoding="utf-8"
    )
    main(["--dockerfile", str(dockerfile), "--dockerfile-inputs", "--json"])
    assert "fastlib" in {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}


def test_cli_dockerfile_inputs_uv_still_refuses_non_requirements_filename_and_cli_keeps_script_reader(
    tmp_path, capsys, fake_pypi
):
    (tmp_path / "deps.py").write_text("purewheel==3.1\nnowheels==0.9\n", encoding="utf-8")
    uv_dockerfile = tmp_path / "Dockerfile"
    uv_dockerfile.write_text(
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /bin/\n"
        "COPY deps.py .\nRUN uv pip install --system -r deps.py\n",
        encoding="utf-8",
    )
    assert main(["--dockerfile", str(uv_dockerfile), "--dockerfile-inputs"]) == 2
    uv_err = capsys.readouterr().err
    assert "uv pip install -r deps.py: uv reads a file by its name" in uv_err

    assert main([str(tmp_path / "deps.py")]) == 2
    cli_err = capsys.readouterr().err
    assert "no # /// script block" in cli_err


def test_github_annotations_with_dockerfile_inputs_names_top_file_when_included(
    tmp_path, capsys, fake_pypi, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))

    reqs_dir = tmp_path / "reqs"
    reqs_dir.mkdir()
    deep_dir = reqs_dir / "deep"
    deep_dir.mkdir()
    (reqs_dir / "base.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    (deep_dir / "leaf.txt").write_text("winonly\n", encoding="utf-8")
    (reqs_dir / "mid.txt").write_text("-r deep/leaf.txt\n", encoding="utf-8")
    (tmp_path / "from_constraints.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("-r from_constraints.txt\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-r reqs/base.txt\n-r reqs/mid.txt\npurewheel==3.1\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt -c constraints.txt\n",
        encoding="utf-8",
    )

    assert (
        main(["--dockerfile", "Dockerfile", "--dockerfile-inputs", "--github-annotations", "-t", "musl-aarch64"]) == 1
    )
    out = capsys.readouterr().out
    # 1. Direct include: names top file through requirements.txt
    assert (
        "::error file=reqs/base.txt,line=1,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on musl-aarch64 (installed by Dockerfile:4, through requirements.txt)."
    ) in out
    # 2. Two includes deep: names top file only (not reqs/mid.txt)
    assert (
        "::error file=reqs/deep/leaf.txt,line=1,title=winonly 311%3A can't install::"
        "winonly 311 can't install on musl-aarch64 (installed by Dockerfile:4, through requirements.txt)."
    ) in out
    assert "mid.txt" not in out
    # 3. Package from -r inside the -c file: names the -c file, which is what leads to it from the RUN line
    assert (
        "::error file=from_constraints.txt,line=1,title=fastlib 2.0.0%3A builds from source::"
        "fastlib 2.0.0 builds from source on musl-aarch64 (installed by Dockerfile:4, through constraints.txt)."
    ) in out

    # 4. Package directly in the top file: message unchanged (no ", through ...")
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("", encoding="utf-8")
    assert (
        main(["--dockerfile", "Dockerfile", "--dockerfile-inputs", "--github-annotations", "-t", "musl-aarch64"]) == 1
    )
    top_out = capsys.readouterr().out
    assert (
        "::error file=requirements.txt,line=1,title=nowheels 0.9%3A builds from source::"
        "nowheels 0.9 builds from source on musl-aarch64 (installed by Dockerfile:4)."
    ) in top_out
    assert ", through " not in top_out


def test_exact_case_path_matches_only_exact_component_names(tmp_path):
    from wheelgap.dockerfile import _exact_case_path

    (tmp_path / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    reqs_dir = tmp_path / "reqs"
    reqs_dir.mkdir()
    (reqs_dir / "base.txt").write_text("six==1.16.0\n", encoding="utf-8")

    assert _exact_case_path(tmp_path, "requirements.txt") == (tmp_path / "requirements.txt", None)
    assert _exact_case_path(tmp_path, "reqs/base.txt") == (reqs_dir / "base.txt", None)
    assert _exact_case_path(tmp_path, "Requirements.txt") == (None, "requirements.txt")
    assert _exact_case_path(tmp_path, "Reqs/base.txt") == (None, "reqs/base.txt")
    assert _exact_case_path(tmp_path, "missing.txt") == (None, None)


@pytest.mark.parametrize(
    ("dockerfile_body", "error_pattern", "case_hint"),
    [
        (
            "FROM python:3.13-slim\nWORKDIR /app\nCOPY Requirements.txt .\nRUN pip install -r Requirements.txt\n",
            "COPY Requirements.txt: not found in the build context",
            "it has requirements.txt, and a Linux image build tells the two apart",
        ),
        (
            "FROM python:3.13-slim\nWORKDIR /app\nCOPY Reqs/base.txt .\nRUN pip install -r base.txt\n",
            "COPY Reqs/base.txt: not found in the build context",
            "it has reqs/base.txt, and a Linux image build tells the two apart",
        ),
        (
            "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r Requirements.txt\n",
            "/app/Requirements.txt isn't copied from the build context",
            None,
        ),
    ],
)
def test_dockerfile_requirements_rejects_case_mismatched_copy_paths(
    tmp_path, dockerfile_body, error_pattern, case_hint
):
    (tmp_path / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    reqs_dir = tmp_path / "reqs"
    reqs_dir.mkdir()
    (reqs_dir / "base.txt").write_text("six==1.16.0\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(dockerfile_body, encoding="utf-8")

    with pytest.raises(DockerfileError, match=re.escape(error_pattern)) as raised:
        dockerfile_requirements(dockerfile)
    # Only a host that ignores case finds the file under the other name, and says which name the context has.
    if case_hint is not None and (tmp_path / "REQUIREMENTS.TXT").exists():
        assert case_hint in str(raised.value)


def test_dockerfile_requirements_rejects_case_mismatched_include_under_folder_copy(tmp_path):
    # An include reached through a folder COPY: on a host that ignores case, Base.txt used to be read as base.txt.
    (tmp_path / "requirements.txt").write_text("-r Base.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("six==1.16.0\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n", encoding="utf-8"
    )

    with pytest.raises(DockerfileError, match=re.escape("/app/Base.txt isn't copied from the build context")):
        dockerfile_requirements(dockerfile)


def test_dockerfile_requirements_reads_exact_case_copy_paths(tmp_path):
    (tmp_path / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    reqs_dir = tmp_path / "reqs"
    reqs_dir.mkdir()
    (reqs_dir / "base.txt").write_text("six==1.16.0\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.13-slim\nWORKDIR /app\nCOPY requirements.txt .\nCOPY reqs/base.txt .\n"
        "RUN pip install -r requirements.txt -r base.txt\n",
        encoding="utf-8",
    )

    inputs = dockerfile_requirements(dockerfile)
    assert [req_path.name for _, req_path in inputs] == ["requirements.txt", "base.txt"]
