import json

import pytest

from wheelgap import pypi
from wheelgap.cli import main
from wheelgap.dockerfile import (
    DockerfileError,
    ImageTarget,
    _heredocs,
    _syntax_directive,
    dockerfile_requirements,
    last_from,
    parse_from,
    read_dockerfile,
)


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
    with pytest.raises(DockerfileError, match="scratch: only the official python images"):
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
        ("FROM python:3.13\nRUN apt-get update\n", "the final stage \\(line 1\\) has no `RUN pip install -r FILE`"),
        ("FROM python:3.13\nRUN echo hi\n", ":2: only `RUN pip install -r FILE` and system package commands"),
        # A RUN that edits, replaces or removes the file between the COPY and the install.
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY requirements.txt .\nRUN echo uwsgi >> requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":4: only `RUN pip install",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN sed -i /pywin32/d requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":4: only `RUN pip install",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN apt-get update && rm requirements.txt\n"
            "RUN pip install -r requirements.txt\n",
            ":4: only `RUN pip install",
        ),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN python3 -mpip install uwsgi\n", ":4: only `RUN pip install"),
        (
            "FROM python:3.13\nARG PIP_INDEX_URL\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\n",
            ":2: an ARG setting PIP_ or UV_",
        ),
        (
            "ARG BASE=deps\nFROM python:3.13-slim AS deps\nWORKDIR /app\nCOPY requirements.txt .\n"
            "RUN pip install -r requirements.txt\nFROM ${BASE}\nRUN pip install -r /app/requirements.txt\n",
            ":6: the final stage builds on an earlier stage",
        ),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt && pip check\n",
            ":4: only `RUN pip install -r FILE`",
        ),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install .\n", ":4: pip install \\.: --dockerfile-inputs"),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN uv pip install --system -r requirements.txt\n", ":4: only"),
        ("FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r $REQS\n", ":4: only `RUN"),
        (
            "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN --mount=type=cache,target=/root/.cache pip install "
            "-r requirements.txt\n",
            ":4: RUN flags such as --mount",
        ),
        ("FROM python:3.13\nRUN <<EOF\npip install numpy\nEOF\n", ":2: only `RUN pip install"),
        ("FROM python:3.13\nCOPY . .\nRUN pip install -r /requirements.txt\n", ":2: COPY to \\.: a relative"),
        ("FROM python:3.13\nRUN pip install -r requirements.txt\n", ":2: -r requirements.txt is relative"),
        ("FROM python:3.13\nWORKDIR /app\nRUN pip install -r requirements.txt\n", "isn't copied from the build"),
        (
            "FROM python:3.13 AS build\nCOPY . /app\nFROM build\nRUN pip install -r /app/requirements.txt\n",
            ":3: the final stage builds on an earlier stage",
        ),
        (
            "FROM python:3.13 AS build\nFROM python:3.13\nWORKDIR /app\nCOPY --from=build /app/requirements.txt .\n"
            "RUN pip install -r requirements.txt\n",
            ":4: COPY --from=build isn't read",
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
            ":2: an ENV setting PIP_ or UV_",
        ),
        (
            'FROM python:3.13\nSHELL ["/bin/bash", "-c"]\nWORKDIR /app\nCOPY . .\n'
            "RUN pip install -r requirements.txt\n",
            ":2: SHELL changes how RUN lines run",
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


def test_dockerfile_requirements_refuses_a_dockerignore_and_nested_includes(tmp_path):
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
        with pytest.raises(DockerfileError, match=":4: requirements.txt has a nested -r or -c"):
            dockerfile_requirements(path)
    (tmp_path / ".dockerignore").write_text("*.txt\n", encoding="utf-8")
    with pytest.raises(DockerfileError, match=r"\.dockerignore may leave files out of the build context"):
        dockerfile_requirements(path)


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


def test_cli_dockerfile_inputs_context_refuses_dockerignore(tmp_path, capsys, fake_pypi):
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
    assert f"{df}: .dockerignore may leave files out of the build context" in capsys.readouterr().err
    (tmp_path / ".dockerignore").unlink()

    (docker_dir / "Dockerfile.dockerignore").write_text("*.txt\n", encoding="utf-8")
    assert main(argv) == 2
    assert f"{df}: Dockerfile.dockerignore may leave files out of the build context" in capsys.readouterr().err


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
    path = _context(tmp_path, "FROM python:3.13\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt && true\n")
    assert main(["--dockerfile", str(path), "--dockerfile-inputs"]) == 2
    assert f"{path}:4: only `RUN pip install -r FILE`" in capsys.readouterr().err
    assert fake_pypi == []
