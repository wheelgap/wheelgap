import json
import re
from pathlib import Path

import pytest

from wheelgap import pypi, sources
from wheelgap.check import Status, check
from wheelgap.sources import (
    Source,
    SourceError,
    UvSelection,
    _BlockYaml,
    find_lockfile,
    load_inputs,
    parse_pixi_lock,
    parse_poetry_lock,
    parse_requirements,
    pythons_outside_lock,
    select_pyproject_extras,
    select_uv_dependencies,
)
from wheelgap.targets import build_targets


def by_name(source):
    return {p.name: p for p in source.packages}


def test_uv_lock(fixtures):
    (src,) = load_inputs([str(fixtures / "uvproj")])
    pkgs = by_name(src)
    assert src.label == "uv.lock"
    assert pkgs["demo"].skip == "editable"
    assert pkgs["gitdep"].skip == "git"
    assert pkgs["fastlib"].files == [
        "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "fastlib-2.0.0-cp313-cp313-macosx_11_0_arm64.whl",
        "fastlib-2.0.0-cp313-cp313-win_amd64.whl",
        "fastlib-2.0.0.tar.gz",
    ]
    assert pkgs["puresdist"].files == ["puresdist-1.0.tar.gz"]
    assert src.roots == [0]
    assert ("winonly", None, "sys_platform == 'win32'", None) in src.edges[0]
    assert ("devtool", None, None, None) in src.edges[0]


def test_uv_lock_dependency_group_names(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[manifest]\nrequirements = [{ name = "app" }]\n\n'
        '[manifest.dependency-groups]\ndev = [{ name = "pytest" }]\ndocs = [{ name = "sphinx" }]\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert [name for name, *_ in src.root_edges] == ["pytest", "sphinx", "app"]
    assert src.root_groups == {0: "dev", 1: "docs"}  # the plain requirement has no group


@pytest.mark.parametrize(
    "edge, message",
    [
        ('{ name = "b", marker = 5 }', "package a has a dependency 'b' with a `marker` that isn't a string"),
        ('{ name = "b", version = ["1.0"] }', "package a has a dependency 'b' with a `version` that isn't a string"),
        ('{ name = { x = "b" } }', "package a has a dependency with a `name` that isn't a string"),
        ('{ name = "b", extra = 3 }', "package a has a dependency 'b' with an `extra` that isn't a list of strings"),
        # uv writes a list here; a string would be read as its characters, and the extra's packages left out
        ('{ name = "b", extra = "s3" }', "package a has a dependency 'b' with an `extra` that isn't a list of strings"),
        ('"b"', "package a has a dependency that isn't a table"),
        ('{ marker = "x" }', "package a has a dependency without a `name`"),
        ('{ name = "b", source = "pypi" }', "package a has a dependency 'b' with a `source` that isn't a table"),
    ],
)
def test_uv_lock_dependency_edge_of_the_wrong_type_raises(tmp_path, edge, message):
    # Used to reach packaging as a TypeError, far from the file. A list of extras is a format uv writes.
    (tmp_path / "uv.lock").write_text(
        f'version = 1\n\n[[package]]\nname = "a"\nversion = "1.0"\ndependencies = [{edge}]\n', encoding="utf-8"
    )
    with pytest.raises(SourceError, match=re.escape(message)):
        load_inputs([str(tmp_path)])


@pytest.mark.parametrize(
    "tables, message",
    [
        (
            '[package.optional-dependencies]\ns3 = [{ name = "b", marker = 5 }]\n',
            "package a has a dependency 'b' with a `marker` that isn't a string",
        ),
        (
            '[package.dev-dependencies]\ndev = [{ name = "b", version = 1 }]\n',
            "package a has a dependency 'b' with a `version` that isn't a string",
        ),
        (
            '\n[manifest]\nrequirements = [{ name = "b", marker = 5 }]\n',
            "the manifest has a dependency 'b' with a `marker` that isn't a string",
        ),
        (
            "\n[manifest.dependency-groups]\ndev = [{ name = 5 }]\n",
            "the manifest has a dependency with a `name` that isn't a string",
        ),
        # the manifest writes `extras` where a package's edge writes `extra`
        (
            '\n[manifest]\nrequirements = [{ name = "b", extras = "s3" }]\n',
            "the manifest has a dependency 'b' with an `extras` that isn't a list of strings",
        ),
        # the lists and tables that hold the edges
        ("dependencies = 5\n", "package a has a group of dependencies that isn't a list"),
        ("optional-dependencies = 5\n", "package a has a `optional-dependencies` that isn't a table of groups"),
        ("\n[manifest]\nrequirements = 5\n", "the manifest has a `requirements` that isn't a list"),
        ("\n[manifest.dependency-groups]\ndev = 5\n", "the manifest has a group of dependencies that isn't a list"),
        (
            "\n[manifest]\ndependency-groups = []\n",
            "the manifest has a `dependency-groups` that isn't a table of groups",
        ),
    ],
)
def test_uv_lock_edge_of_the_wrong_type_in_a_group_or_the_manifest_raises(tmp_path, tables, message):
    (tmp_path / "uv.lock").write_text(
        f'version = 1\n\n[[package]]\nname = "a"\nversion = "1.0"\n{tables}', encoding="utf-8"
    )
    with pytest.raises(SourceError, match=re.escape(message)):
        load_inputs([str(tmp_path)])


def test_uv_lock_manifest_that_is_not_a_table_raises(tmp_path):
    (tmp_path / "uv.lock").write_text("version = 1\nmanifest = 5\n", encoding="utf-8")
    with pytest.raises(SourceError, match=re.escape("uv.lock: `manifest` isn't a table")):
        load_inputs([str(tmp_path)])


def test_uv_lock_dependency_extra_may_be_a_list(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[manifest]\nrequirements = [{ name = "a" }]\n\n[[package]]\nname = "a"\nversion = "1.0"\n'
        'dependencies = [{ name = "b", extra = ["s3", "gcs"] }]\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert src.edges[0] == [("b", None, None, None)]


def test_uv_lock_dependency_source_is_preserved(tmp_path):
    cpu = "https://download.pytorch.org/whl/cpu"
    (tmp_path / "uv.lock").write_text(
        f"""
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "torch", version = "2.5.1", source = {{ registry = "{cpu}" }}, marker = "sys_platform == 'linux'" }},
]
""",
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert src.edges[0] == [("torch", "2.5.1", "sys_platform == 'linux'", {"registry": cpu})]


def test_poetry_lock(fixtures):
    (src,) = load_inputs([str(fixtures / "poetryproj" / "poetry.lock")])
    pkgs = by_name(src)
    assert pkgs["fastlib"].files == ["fastlib-2.0.0-cp313-cp313-musllinux_1_2_aarch64.whl", "fastlib-2.0.0.tar.gz"]
    assert pkgs["backport"].markers == ['python_version < "3.13"']
    assert pkgs["localpkg"].skip == "directory"


@pytest.mark.parametrize("input_name", ["pdmproj", "pdmproj/pdm.lock"])
def test_pdm_lock(fixtures, input_name):
    (src,) = load_inputs([str(fixtures / input_name)])
    pkgs = by_name(src)
    assert src.label == "pdm.lock"
    assert pkgs["fastlib"].files == ["fastlib-2.0.0-cp313-cp313-musllinux_1_2_aarch64.whl", "fastlib-2.0.0.tar.gz"]
    assert pkgs["backport"].markers == ["python_version < '3.13'"]
    assert pkgs["localpkg"].skip == "path"
    assert all(p.origin == "pdm.lock" for p in src.packages)


def test_pdm_lock_legacy_metadata_files(tmp_path, fake_pypi):
    path = tmp_path / "pdm.lock"
    lock = (
        '[metadata]\nlock_version = "3.0"\n\n'
        '[[package]]\nname = "oldpkg"\nversion = "1.0"\n\n'
        '[[package]]\nname = "newpkg"\nversion = "2.0"\n\n'
        '[[package]]\nname = "jinja2"\nversion = "3.1.2"\n\n'
        "[metadata.files]\n"
        '"oldpkg 1.0" = [\n'
        '    {file = "oldpkg-1.0-py3-none-any.whl", hash = "sha256:00"},\n'
        "]\n"
        '"Jinja2 3.1.2" = [\n'
        '    {url = "https://files.pythonhosted.org/packages/Jinja2-3.1.2-py3-none-any.whl", hash = "sha256:00"},\n'
        "]\n"
    )
    path.write_text(lock, encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert pkgs["oldpkg"].files == ["oldpkg-1.0-py3-none-any.whl"]
    assert pkgs["newpkg"].files is None  # not in metadata.files either: fall back to PyPI
    assert pkgs["jinja2"].files == ["Jinja2-3.1.2-py3-none-any.whl"]  # normalized-name fallback
    from wheelgap.pypi import fill_all

    fill_all(src.packages)
    # Only the package missing from [metadata.files] is looked up on the index.
    assert fake_pypi and all("/newpkg/" in url for url in fake_pypi)


def test_pylock(fixtures):
    (src,) = load_inputs([str(fixtures / "pylockproj")])
    pkgs = by_name(src)
    assert pkgs["fastlib"].files == ["fastlib-2.0.0-cp313-abi3-manylinux_2_28_aarch64.whl"]
    assert pkgs["macpkg"].files == ["macpkg-1.0.tar.gz"]
    assert pkgs["macpkg"].markers == ["sys_platform == 'darwin'"]


def test_requirements(fixtures):
    src = parse_requirements(fixtures / "reqproj" / "requirements.txt")
    pkgs = by_name(src)
    assert pkgs["fastlib"].version == "2.0.0"
    assert pkgs["fastlib"].files is None
    assert pkgs["unpinned"].version is None
    assert pkgs["winonly"].markers == ['sys_platform == "win32"']
    assert pkgs["purewheel"].version == "3.1"  # pulled in via -r
    assert pkgs["./localpkg"].skip == "editable"
    assert len(src.packages) == 5


@pytest.mark.parametrize(
    "option",
    ["-c constraints.txt", "-cconstraints.txt", "--constraint constraints.txt", "--constraint=constraints.txt"],
)
def test_requirements_constraints_pin_without_adding_packages(tmp_path, option):
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n" + option + "\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("ranged==1.9\nonly-constraint==2\n", encoding="utf-8")
    packages = parse_requirements(path).packages
    assert [(p.name, p.version, p.spec) for p in packages] == [("ranged", "1.9", None)]


def test_requirements_nested_constraints_combine_ranges_and_keep_pins(tmp_path):
    directory = tmp_path / "sub"
    directory.mkdir()
    path = tmp_path / "requirements.txt"
    path.write_text("-r sub/more.txt\n-c sub/constraints.txt\n", encoding="utf-8")
    (directory / "more.txt").write_text("ranged>=1.5\npinned==3\n", encoding="utf-8")
    (directory / "constraints.txt").write_text("-c nested.txt\npinned==3\n", encoding="utf-8")
    (directory / "nested.txt").write_text("ranged<2 \\\n    --hash=sha256:00  # note\n", encoding="utf-8")
    ranged, pinned = parse_requirements(path).packages
    assert ranged.spec == "<2,>=1.5"
    assert ranged.version is None
    assert pinned.version == "3"
    assert pinned.error is None


@pytest.mark.parametrize(
    "req,constraint,expected_version,expected_spec,expected_error",
    [
        (
            "idna==3.6",
            "idna==3.7",
            "3.6",
            None,
            "requirement idna==3.6 conflicts with the constraint idna==3.7 (pip refuses this)",
        ),
        (
            "idna==3.6",
            "idna>=3.7",
            "3.6",
            None,
            "requirement idna==3.6 conflicts with the constraint idna>=3.7 (pip refuses this)",
        ),
        (
            "idna>=3.8",
            "idna==3.7",
            None,
            ">=3.8",
            "requirement idna>=3.8 conflicts with the constraint idna==3.7 (pip refuses this)",
        ),
        (
            "idna==1.9",
            "idna==1.9.0",
            "1.9",
            None,
            None,
        ),
        (
            "idna==3.6",
            "idna>=3.0",
            "3.6",
            None,
            None,
        ),
        # pip 26.3 checked: ==1.0 matches 1.0+local (PEP 440), so pip installs 1.0+local whichever side names it.
        ("demo==1.0+local", "demo==1.0", "1.0+local", None, None),
        ("demo==1.0", "demo==1.0+local", "1.0+local", None, None),
        (
            "demo==1.0+local",
            "demo==1.0+cpu",
            "1.0+local",
            None,
            "requirement demo==1.0+local conflicts with the constraint demo==1.0+cpu (pip refuses this)",
        ),
        (
            "demo==1.0+cpu",
            "demo!=1.0",
            "1.0+cpu",
            None,
            "requirement demo==1.0+cpu conflicts with the constraint demo!=1.0 (pip refuses this)",
        ),
        ("demo==1.0+cpu", "demo==1.0\ndemo==1.0+cpu", "1.0+cpu", None, None),
        ("demo", "demo==1.0\ndemo==1.0+cpu", "1.0+cpu", None, None),
        # A pinned pre-release counts, as pip takes it; <2.0 leaves out 2.0's pre-releases.
        ("demo==2.0b1", "demo>=1.0", "2.0b1", None, None),
        ("demo>=1.0", "demo==2.0b1", "2.0b1", None, None),
        (
            "demo==2.0b1",
            "demo<2.0",
            "2.0b1",
            None,
            "requirement demo==2.0b1 conflicts with the constraint demo<2.0 (pip refuses this)",
        ),
        (
            "demo==1!1.0",
            "demo==1.0",
            "1!1.0",
            None,
            "requirement demo==1!1.0 conflicts with the constraint demo==1.0 (pip refuses this)",
        ),
        ("demo==3.6", "demo", "3.6", None, None),
        # An exact constraint must meet the other constraints too.
        (
            "demo>=1",
            "demo==2.0\ndemo<1.5",
            None,
            ">=1",
            "the constraint demo==2.0 conflicts with the constraint demo<1.5 (pip refuses this)",
        ),
        # A marked constraint that the pin meets changes nothing on any target; one it doesn't depends on the target.
        ("idna==3.6", 'idna==3.6; sys_platform == "win32"', "3.6", None, None),
        (
            "idna==3.6",
            'idna==3.7; sys_platform == "win32"',
            "3.6",
            None,
            "constraint has an environment marker; target-dependent constraints aren't supported",
        ),
    ],
)
def test_requirements_constraint_conflict_and_agreement(
    tmp_path, req, constraint, expected_version, expected_spec, expected_error
):
    path = tmp_path / "requirements.txt"
    path.write_text(f"{req}\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text(f"{constraint}\n", encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.version == expected_version
    assert pkg.spec == expected_spec
    assert pkg.error == expected_error


def test_requirements_conflict_from_nested_constraints_file(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("idna==3.6\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("-c nested.txt\n", encoding="utf-8")
    (tmp_path / "nested.txt").write_text("idna==3.7\n", encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.version == "3.6"
    assert pkg.error == "requirement idna==3.6 conflicts with the constraint idna==3.7 (pip refuses this)"


def test_requirements_requirement_inside_constraints_file(tmp_path):
    # pip reads a -r inside a constraints file as requirements, not more constraints (#602):
    # the included file's packages are checked, while the constraints file's own lines stay constraints.
    path = tmp_path / "a.txt"
    path.write_text("-c cons.txt\nrequests==2.32.5\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r extra.txt\nurllib3<3\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    src = parse_requirements(path)
    assert [(p.name, p.version, p.origin) for p in src.packages] == [
        ("psycopg2", "2.9.11", "extra.txt"),
        ("requests", "2.32.5", "a.txt"),
    ]
    assert src.constraint_specs == {"urllib3": {"<3"}}


def test_requirements_requirement_inside_constraints_file_keeps_compiler_signs(tmp_path):
    # A -r inside a constraints file is read as requirements, with compiler signs (#602): a pip-compile
    # file reached that way still marks the input compiled, as a direct -r does.
    path = tmp_path / "a.txt"
    path.write_text("-c cons.txt\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r reqs.txt\n", encoding="utf-8")
    (tmp_path / "reqs.txt").write_text("lib==2.0 \\\n    --hash=sha256:aa\n    # via app\n", encoding="utf-8")
    src = parse_requirements(path)
    assert src.compiled
    assert [(p.name, p.version) for p in src.packages] == [("lib", "2.0")]


def test_requirements_hashes_in_a_constraints_file_do_not_mark_the_input_compiled(tmp_path):
    # Only requirement lines mark the input compiled: a constraints file with --hash lines and "# via" comments
    # (pip-compile output used with -c) leaves it as it was, so --deps still resolves the requirements (#602).
    path = tmp_path / "a.txt"
    path.write_text("lib==2.0\n-c cons.txt\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text(
        "lib==2.0 \\\n    --hash=sha256:aa\n    # via app\n-c nested.txt\n", encoding="utf-8"
    )
    (tmp_path / "nested.txt").write_text("other==1.0 --hash=sha256:bb\n", encoding="utf-8")
    src = parse_requirements(path)
    assert not src.compiled
    assert [(p.name, p.version) for p in src.packages] == [("lib", "2.0")]


def test_requirements_nested_includes_keep_their_kind(tmp_path):
    # pip: a -r file is requirements and a -c file is constraints, whatever file the line is in (#602).
    # top -c c1 -> -r r1 -> -c c2, and top -r r2 -> -c c3 -> -r r3, with includes in subfolders
    # resolved against the including file's folder.
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    path = tmp_path / "top.txt"
    path.write_text("-c sub/c1.txt\n-r sub/r2.txt\ntopreq\n", encoding="utf-8")
    (tmp_path / "sub" / "c1.txt").write_text("-r deep/r1.txt\nfromc1<2\n", encoding="utf-8")
    (tmp_path / "sub" / "deep" / "r1.txt").write_text("-c c2.txt\nfromr1==1.0\n", encoding="utf-8")
    (tmp_path / "sub" / "deep" / "c2.txt").write_text("fromc2<3\n", encoding="utf-8")
    (tmp_path / "sub" / "r2.txt").write_text("-c deep/c3.txt\nfromr2==2.0\n", encoding="utf-8")
    (tmp_path / "sub" / "deep" / "c3.txt").write_text("-r r3.txt\nfromc3<4\n", encoding="utf-8")
    (tmp_path / "sub" / "deep" / "r3.txt").write_text("fromr3==3.0\n", encoding="utf-8")
    src = parse_requirements(path)
    assert [(p.name, p.version, p.origin) for p in src.packages] == [
        ("fromr1", "1.0", "r1.txt"),
        ("fromr3", "3.0", "r3.txt"),
        ("fromr2", "2.0", "r2.txt"),
        ("topreq", None, "top.txt"),
    ]
    assert src.constraint_specs == {"fromc1": {"<2"}, "fromc2": {"<3"}, "fromc3": {"<4"}}


def test_requirements_include_cycle_through_a_constraints_file_is_an_error(tmp_path):
    # a.txt -c cons.txt -r a.txt: the chain is one chain for -r and -c together, so pip refuses the loop (#603).
    path = tmp_path / "a.txt"
    path.write_text("-c cons.txt\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-r a.txt\nranged<2\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "cons.txt: -r a.txt includes a file that is already being read (a.txt -> cons.txt -> a.txt); pip refuses this"
    )


def test_requirements_two_file_include_cycle_is_an_error(tmp_path):
    # The issue's example: c1.txt -r c2.txt -r c1.txt. pip refuses it, and so does wheelgap (#603).
    path = tmp_path / "c1.txt"
    path.write_text("-r c2.txt\nrequests==2.32.5\n", encoding="utf-8")
    (tmp_path / "c2.txt").write_text("-r c1.txt\nidna==3.11\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "c2.txt: -r c1.txt includes a file that is already being read (c1.txt -> c2.txt -> c1.txt); pip refuses this"
    )


@pytest.mark.parametrize("line", ["-r a.txt", "-c a.txt"])
def test_requirements_file_including_itself_is_an_error(tmp_path, line):
    # A file that includes itself, with -r or -c, is a cycle pip refuses (#603).
    path = tmp_path / "a.txt"
    path.write_text(line + "\nrequests==2.32.5\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        f"a.txt: {line} includes a file that is already being read (a.txt -> a.txt); pip refuses this"
    )


def test_requirements_three_file_include_cycle_is_an_error(tmp_path):
    # The chain is named in full, from the file the run started with (#603).
    path = tmp_path / "a.txt"
    path.write_text("-r b.txt\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("-r c.txt\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("-r a.txt\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "c.txt: -r a.txt includes a file that is already being read "
        "(a.txt -> b.txt -> c.txt -> a.txt); pip refuses this"
    )


def test_requirements_cycle_names_are_relative_to_the_starting_folder(tmp_path):
    # Two files with the same name in different folders stay distinguishable in the chain (#603).
    path = tmp_path / "a.txt"
    path.write_text("-r sub/a.txt\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("-r ../a.txt\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "sub/a.txt: -r ../a.txt includes a file that is already being read "
        "(a.txt -> sub/a.txt -> a.txt); pip refuses this"
    )


def test_requirements_cycle_names_a_file_above_the_starting_folder_by_its_relative_path(tmp_path):
    # A cycle that leaves the starting folder: the file above it is ../a.txt, not a second "a.txt" (#603).
    (tmp_path / "reqs").mkdir()
    path = tmp_path / "reqs" / "a.txt"
    path.write_text("-r ../a.txt\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("-r reqs/a.txt\n", encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "../a.txt: -r reqs/a.txt includes a file that is already being read "
        "(a.txt -> ../a.txt -> a.txt); pip refuses this"
    )


def test_requirements_include_diamond_lists_packages_once(tmp_path):
    # A file included twice from two different files is fine: it is read once, as pip reads it (#603).
    path = tmp_path / "top.txt"
    path.write_text("-r left.txt\n-r right.txt\n", encoding="utf-8")
    (tmp_path / "left.txt").write_text("-r base.txt\nleft==1.0\n", encoding="utf-8")
    (tmp_path / "right.txt").write_text("-r base.txt\nright==2.0\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("base==3.0\n", encoding="utf-8")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [
        ("base", "3.0"),
        ("left", "1.0"),
        ("right", "2.0"),
    ]


def _try_symlink(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks aren't available here")


def test_requirements_symlinked_file_include_reads_from_the_links_folder(tmp_path):
    # The issue's layout: link/reqs.txt points into real/, and both folders have a base.txt. pip
    # resolves -r base.txt against the folder of the file as named, so it reads link/base.txt (#604).
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    (tmp_path / "real" / "reqs.txt").write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / "real" / "base.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    (tmp_path / "link" / "base.txt").write_text("idna==3.11\n", encoding="utf-8")
    _try_symlink(Path("../real/reqs.txt"), tmp_path / "link" / "reqs.txt")
    assert [(p.name, p.version) for p in parse_requirements(tmp_path / "link" / "reqs.txt").packages] == [
        ("idna", "3.11")
    ]


def test_requirements_symlinked_folder_include_reads_from_the_links_folder(tmp_path):
    # The requirements file sits in a symlinked folder, and its include climbs out of it: pip joins
    # -r ../base.txt onto the folder as named (a/link), so it reads a/base.txt, not base.txt (#604).
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "reqs.txt").write_text("-r ../base.txt\n", encoding="utf-8")
    (tmp_path / "a" / "base.txt").write_text("idna==3.11\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    _try_symlink(Path("../b"), tmp_path / "a" / "link")
    assert [(p.name, p.version) for p in parse_requirements(tmp_path / "a" / "link" / "reqs.txt").packages] == [
        ("idna", "3.11")
    ]


def test_requirements_symlinked_constraints_file_include_reads_from_the_links_folder(tmp_path):
    # A -c inside a symlinked constraints file resolves against the link's folder too: the constraint
    # comes from link/base.txt, not real/base.txt (#604).
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    (tmp_path / "top.txt").write_text("idna\n-c link/cons.txt\n", encoding="utf-8")
    (tmp_path / "real" / "cons.txt").write_text("-c base.txt\n", encoding="utf-8")
    (tmp_path / "real" / "base.txt").write_text("idna==2.0\n", encoding="utf-8")
    (tmp_path / "link" / "base.txt").write_text("idna==3.11\n", encoding="utf-8")
    _try_symlink(Path("../real/cons.txt"), tmp_path / "link" / "cons.txt")
    (pkg,) = parse_requirements(tmp_path / "top.txt").packages
    assert (pkg.name, pkg.version) == ("idna", "3.11")


def test_requirements_symlinked_file_missing_include_names_the_path_pip_would_open(tmp_path):
    # The link's own folder has no base.txt, so pip fails opening link/base.txt: the error names that
    # path, not the one the link points into (#604).
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    (tmp_path / "real" / "reqs.txt").write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / "real" / "base.txt").write_text("psycopg2==2.9.11\n", encoding="utf-8")
    _try_symlink(Path("../real/reqs.txt"), tmp_path / "link" / "reqs.txt")
    with pytest.raises(SourceError, match=r"^reqs\.txt:1: -r base\.txt: file not found$") as caught:
        parse_requirements(tmp_path / "link" / "reqs.txt")
    # The filename, not str(error): that shows the path's repr, with doubled backslashes on Windows.
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert Path(caught.value.__cause__.filename) == tmp_path / "link" / "base.txt"


def test_requirements_file_reached_by_two_names_lists_packages_once(tmp_path):
    # The same file included once by its own name and once through a link is still read once: the loop
    # and "already read" identity is the resolved path, so its packages are listed once (#604).
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    path = tmp_path / "top.txt"
    path.write_text("-r real/base.txt\n-r link/base.txt\n", encoding="utf-8")
    (tmp_path / "real" / "base.txt").write_text("idna==3.11\n", encoding="utf-8")
    _try_symlink(Path("../real/base.txt"), tmp_path / "link" / "base.txt")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [("idna", "3.11")]


def test_requirements_shared_file_linked_into_two_folders_reads_each_folders_include(tmp_path):
    # One shared file linked into two service folders, both included from one top file. The shared
    # file's own line is listed once, but its -r base.txt is another file under each name, and pip
    # reads both (checked with pip 26.3's parser): neither folder's packages may be dropped (#604).
    for folder in ("shared", "a", "b"):
        (tmp_path / folder).mkdir()
    path = tmp_path / "top.txt"
    path.write_text("-r a/reqs.txt\n-r b/reqs.txt\n", encoding="utf-8")
    (tmp_path / "shared" / "reqs.txt").write_text("requests==2.32.5\n-r base.txt\n", encoding="utf-8")
    (tmp_path / "a" / "base.txt").write_text("idna==3.11\n", encoding="utf-8")
    (tmp_path / "b" / "base.txt").write_text("six==1.17.0\n", encoding="utf-8")
    _try_symlink(Path("../shared/reqs.txt"), tmp_path / "a" / "reqs.txt")
    _try_symlink(Path("../shared/reqs.txt"), tmp_path / "b" / "reqs.txt")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [
        ("requests", "2.32.5"),
        ("idna", "3.11"),
        ("six", "1.17.0"),
    ]


def test_requirements_shared_file_on_the_chain_under_two_names_is_not_a_cycle(tmp_path):
    # a/reqs.txt and b/reqs.txt are links to one shared file, and a's include leads to b's. The shared
    # file is then being read under two names at once, but nothing includes itself: pip compares the
    # names (os.path.abspath), reads on, and installs idna (checked with pip 26.3's parser) (#604).
    for folder in ("shared", "a", "b"):
        (tmp_path / folder).mkdir()
    (tmp_path / "shared" / "reqs.txt").write_text("-r next.txt\n", encoding="utf-8")
    (tmp_path / "a" / "next.txt").write_text("-r ../b/reqs.txt\n", encoding="utf-8")
    (tmp_path / "b" / "next.txt").write_text("idna==3.11\n", encoding="utf-8")
    _try_symlink(Path("../shared/reqs.txt"), tmp_path / "a" / "reqs.txt")
    _try_symlink(Path("../shared/reqs.txt"), tmp_path / "b" / "reqs.txt")
    assert [(p.name, p.version) for p in parse_requirements(tmp_path / "a" / "reqs.txt").packages] == [("idna", "3.11")]


def test_requirements_include_cycle_through_a_symlink_is_an_error(tmp_path):
    # A loop through a link is refused, and the chain names the files as they were written (#604).
    (tmp_path / "real").mkdir()
    (tmp_path / "link").mkdir()
    path = tmp_path / "a.txt"
    path.write_text("-r link/b.txt\n", encoding="utf-8")
    (tmp_path / "real" / "b.txt").write_text("-r ../a.txt\n", encoding="utf-8")
    _try_symlink(Path("../real/b.txt"), tmp_path / "link" / "b.txt")
    with pytest.raises(SourceError) as caught:
        parse_requirements(path)
    assert str(caught.value) == (
        "link/b.txt: -r ../a.txt includes a file that is already being read "
        "(a.txt -> link/b.txt -> a.txt); pip refuses this"
    )


def test_requirements_constraint_inside_constraints_file_stays_a_constraint(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("ranged\n-c cons.txt\n", encoding="utf-8")
    (tmp_path / "cons.txt").write_text("-c nested.txt\n", encoding="utf-8")
    (tmp_path / "nested.txt").write_text("ranged==1.9\n", encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.version == "1.9"


def test_requirements_requirement_inside_requirement_file_is_unchanged(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("-r extra.txt\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [("purewheel", "3.1")]


def test_requirements_file_read_as_constraint_and_requirement_gives_both(tmp_path):
    # The seen/stack bookkeeping is per kind of file, so a file read once as constraints and once
    # as requirements gives both (#602).
    path = tmp_path / "a.txt"
    path.write_text("-c shared.txt\n-r shared.txt\n", encoding="utf-8")
    (tmp_path / "shared.txt").write_text("ranged==1.9\npurewheel==3.1\n", encoding="utf-8")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [
        ("ranged", "1.9"),
        ("purewheel", "3.1"),
    ]


def test_requirements_marker_constraint_is_an_error(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text('ranged<2; sys_platform == "win32"\n', encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.error is not None and "marker" in pkg.error


def test_requirements_url_and_env_var_constraints_are_skipped(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text(
        "ranged\n-c https://example.com/upper.txt\n--constraint=${PIP_CONSTRAINT}\n-c c-${PY}.txt\n", encoding="utf-8"
    )
    assert [(p.name, p.version, p.spec, p.error) for p in parse_requirements(path).packages] == [
        ("ranged", None, None, None)
    ]


@pytest.mark.parametrize(
    "line,target",
    [
        ("--pre -r base.txt", "base.txt"),
        ("--pre --requirement=base.txt", "base.txt"),
        ("--pre --requirement base.txt", "base.txt"),
        ("--pre -rbase.txt", "base.txt"),
        ('-r "dir with space/base.txt"', "dir with space/base.txt"),
        ("-r 'dir with space/base.txt'", "dir with space/base.txt"),
    ],
)
def test_requirements_include_after_other_options(tmp_path, line, target):
    # pip shlex-splits a line of options and reads every option on it, not only the first
    (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / target).write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text(f"{line}\nfastlib==2.0.0\n", encoding="utf-8")
    packages = parse_requirements(path).packages
    assert [(p.name, p.version, p.origin) for p in packages] == [
        ("purewheel", "3.1", "base.txt"),
        ("fastlib", "2.0.0", "requirements.txt"),
    ]


@pytest.mark.parametrize("line", ["--pre -c pins.txt", "--pre --constraint=pins.txt", "--pre -cpins.txt"])
def test_requirements_constraint_after_other_options(tmp_path, line):
    (tmp_path / "pins.txt").write_text("ranged==1.9\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text(f"ranged\n{line}\n", encoding="utf-8")
    assert [(p.name, p.version) for p in parse_requirements(path).packages] == [("ranged", "1.9")]


@pytest.mark.parametrize(
    "line,names",
    [
        # optparse collects every -r and -c, and pip follows only the first -r, or the first -c if there's no -r
        ("-r a.txt -r b.txt", ["purewheel"]),
        ("-c pins.txt -r a.txt", ["purewheel"]),
    ],
)
def test_requirements_line_with_several_includes_reads_what_pip_reads(tmp_path, line, names):
    (tmp_path / "a.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    (tmp_path / "pins.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text(f"{line}\n", encoding="utf-8")
    assert [p.name for p in parse_requirements(path).packages] == names


def test_requirements_line_with_two_constraints_reads_only_the_first(tmp_path):
    (tmp_path / "pins.txt").write_text("ranged>=1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("ranged==1.9\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n-c pins.txt -c b.txt\n", encoding="utf-8")
    (ranged,) = parse_requirements(path).packages
    assert (ranged.version, ranged.spec) == (None, ">=1")


def test_requirements_editable_line_follows_no_include(tmp_path):
    # pip reads `-e PATH -r f` as an editable requirement, and an -r on a requirement line isn't followed
    (tmp_path / "a.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text("-e ./localpkg -r a.txt\n", encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.skip == "editable"


def test_requirements_format_option_before_include_still_applies(tmp_path):
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text("--no-binary :all: -r base.txt\n", encoding="utf-8")
    (purewheel,) = parse_requirements(path).packages
    assert (purewheel.no_binary, purewheel.only_binary) == (True, False)


@pytest.mark.parametrize(
    "line,names",
    [
        # optparse takes the token after an option with a value as that value, even one starting with -
        ("--no-binary -r base.txt", ["fastlib"]),
        ("--extra -r base.txt", ["fastlib"]),  # an abbreviation of --extra-index-url
        ("-C -r base.txt", ["fastlib"]),  # -C is short for --config-settings
        ("-i -cpins.txt -r base.txt", ["purewheel", "fastlib"]),
        ("--pre -r base.txt", ["purewheel", "fastlib"]),  # --pre is an option of its own, not short for --prefer-binary
    ],
)
def test_requirements_option_values_are_not_read_as_options(tmp_path, line, names):
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text(f"{line}\nfastlib==2.0.0\n", encoding="utf-8")
    assert [p.name for p in parse_requirements(path).packages] == names


def test_requirements_format_option_after_include_still_applies(tmp_path):
    # optparse applies every option on the line before pip reads the include
    (tmp_path / "base.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    path = tmp_path / "requirements.txt"
    path.write_text("-r base.txt --no-binary :all:\n", encoding="utf-8")
    (purewheel,) = parse_requirements(path).packages
    assert (purewheel.no_binary, purewheel.only_binary) == (True, False)


@pytest.mark.parametrize(
    "line,message",
    [
        ("-r 'unclosed", "pip can't split this line"),
        ("--pre -r", "-r needs a file name"),
        ("--constraint=", "--constraint needs a file name"),
        # pip's answer would depend on an option wheelgap doesn't read, so it's refused rather than guessed
        ("-r a.txt --req b.txt", "on a line with an abbreviated -r or -c"),
        ("-c a.txt --req b.txt", "on a line with an abbreviated -r or -c"),
        ("-r a.txt -e ./localpkg", "on a line with an editable"),
        # the token after an abbreviated include is its value, so the -C here doesn't hide the -r that pip follows
        ("--cons -C -r a.txt", "on a line with an abbreviated -r or -c"),
    ],
)
def test_requirements_unreadable_option_line_is_an_error(tmp_path, line, message):
    path = tmp_path / "requirements.txt"
    path.write_text(f"{line}\nfastlib==2.0.0\n", encoding="utf-8")
    with pytest.raises(SourceError, match="requirements.txt: ") as raised:
        parse_requirements(path)
    assert message in str(raised.value)


@pytest.mark.parametrize(
    "lines,version,error",
    [
        ("ranged==1.9\nranged==1.9.0\n", "1.9", None),  # the same version, written two ways
        ("ranged==1.9\nranged==2.0\n", None, "conflicting exact versions"),
        ("ranged @ https://example.com/ranged-1.9.tar.gz\n", None, "isn't an index version"),
    ],
)
def test_requirements_constraint_pins_and_unusable_constraints(tmp_path, lines, version, error):
    path = tmp_path / "requirements.txt"
    path.write_text("ranged\n-c constraints.txt\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text(lines, encoding="utf-8")
    (pkg,) = parse_requirements(path).packages
    assert pkg.version == version
    assert (pkg.error is None) if error is None else (error in pkg.error)


@pytest.mark.parametrize("input_name", ["pipenvproj", "pipenvproj/Pipfile.lock"])
def test_pipfile_lock(fixtures, input_name):
    (src,) = load_inputs([str(fixtures / input_name)])
    pkgs = by_name(src)
    assert src.label == "Pipfile.lock"
    assert len(src.packages) == 7
    assert pkgs["fastlib"].version == "2.0.0"
    assert pkgs["fastlib"].markers is None
    assert pkgs["winonly"].version == "311"
    assert pkgs["winonly"].markers == ["sys_platform == 'win32'"]
    assert pkgs["purewheel"].version == "3.1"
    assert pkgs["purewheel"].markers == ["python_version >= '3.11'"]
    assert pkgs["gitdep"].skip == "git"
    assert pkgs["localpkg"].skip == "path"
    assert pkgs["archivepkg"].skip == "file"
    assert pkgs["editablepkg"].skip == "editable"
    assert all(p.origin == "Pipfile.lock" for p in src.packages)
    assert all(p.files is None for p in src.packages)
    assert all(pkgs[name].skip is None for name in ("fastlib", "winonly", "purewheel"))
    assert all(pkgs[name].version is None for name in ("gitdep", "localpkg", "archivepkg", "editablepkg"))


@pytest.mark.parametrize("section", ["default", "develop"])
def test_pipfile_lock_with_one_section(tmp_path, section):
    path = tmp_path / "Pipfile.lock"
    path.write_text(json.dumps({section: {"purewheel": {"version": "==3.1"}}}), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    assert [(p.name, p.version) for p in src.packages] == [("purewheel", "3.1")]


def test_pipfile_lock_package_in_both_sections(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "default": {
            "certifi": {"version": "==2025.1.31"},
            "colorama": {"version": "==0.4.6", "markers": "os_name == 'nt'"},
        },
        "develop": {
            "Certifi": {"version": "==2025.1.31"},
            "colorama": {"version": "==0.4.6", "markers": "sys_platform == 'win32'"},
            "pytest": {"version": "==8.3.5"},
        },
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    assert [(p.name, p.version) for p in src.packages] == [
        ("certifi", "2025.1.31"),
        ("colorama", "0.4.6"),
        ("pytest", "8.3.5"),
    ]
    assert by_name(src)["colorama"].markers == ["os_name == 'nt'", "sys_platform == 'win32'"]


def test_empty_pipfile_lock(tmp_path):
    path = tmp_path / "Pipfile.lock"
    path.write_text("{}", encoding="utf-8")
    (src,) = load_inputs([str(tmp_path)])
    assert src.label == "Pipfile.lock"
    assert src.packages == []


def test_adhoc_specs():
    (src,) = load_inputs(["numpy==2.3.4", "psycopg2"])
    assert [(p.name, p.version) for p in src.packages] == [("numpy", "2.3.4"), ("psycopg2", None)]


def test_wildcard_pin_is_not_a_pin():
    (src,) = load_inputs(["numpy==2.*"])
    assert src.packages[0].version is None


def test_missing_file_errors():
    with pytest.raises(SourceError, match="no such file"):
        load_inputs(["nope/uv.lock"])


def test_no_lockfile_in_dir(fixtures):
    with pytest.raises(SourceError, match="no lockfile found"):
        find_lockfile(fixtures / "empty")


def test_bad_requirement():
    with pytest.raises(SourceError):
        load_inputs(["not a valid ! spec"])


def test_requirements_lock(tmp_path):
    path = tmp_path / "requirements.lock"
    path.write_text("fastlib==2.0.0\nwinonly==3.11 ; sys_platform == 'win32'\n", encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert src.label == "requirements.lock"
    assert pkgs["fastlib"].version == "2.0.0"
    assert pkgs["winonly"].markers == ['sys_platform == "win32"']


def test_requirements_dev_lock(tmp_path):
    path = tmp_path / "requirements-dev.lock"
    path.write_text("pytest==8.3.5\n", encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert src.label == "requirements-dev.lock"
    assert pkgs["pytest"].version == "8.3.5"


def test_requirements_uvicorn_lock(tmp_path):
    path = tmp_path / "requirements-uvicorn.lock"
    path.write_text("uvicorn==0.34.0\n", encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert src.label == "requirements-uvicorn.lock"
    assert pkgs["uvicorn"].version == "0.34.0"


def test_find_lockfile_requirements_lock(tmp_path):
    (tmp_path / "requirements.lock").write_text("numpy==2.0.0\n", encoding="utf-8")
    found = find_lockfile(tmp_path)
    assert found.name == "requirements.lock"


def test_pipfile_lock_custom_categories(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "docs": {
            "sphinx": {"version": "==7.2.6"},
            "furo": {"version": "==2024.1.29"},
        },
        "_meta": {"sources": []},
        "default": {
            "requests": {"version": "==2.31.0"},
            "sphinx": {"version": "==7.2.6"},
        },
        "develop": {
            "pytest": {"version": "==8.0.0"},
        },
        "tests": {
            "pytest-cov": {"version": "==4.1.0"},
        },
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    assert [(p.name, p.version) for p in src.packages] == [
        ("requests", "2.31.0"),
        ("sphinx", "7.2.6"),
        ("pytest", "8.0.0"),
        ("furo", "2024.1.29"),
        ("pytest-cov", "4.1.0"),
    ]
    assert len(src.packages) == 5


def _two_source_pipfile_lock(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "_meta": {
            "sources": [
                {"name": "pypi", "url": "https://pypi.org/simple", "verify_ssl": True},
                {"name": "legacy", "url": "https://pypi.python.org/simple", "verify_ssl": True},
                {"name": "internal", "url": "https://pkgs.example.com/simple", "verify_ssl": True},
                {"name": "broken", "url": "https://[bad/simple", "verify_ssl": True},
            ]
        },
        "default": {
            "acme-utils": {"version": "==2.1.0", "index": "internal"},
            "requests": {"version": "==2.31.0", "index": "pypi"},
            "six": {"version": "==1.16.0", "index": "legacy"},
            "fastlib": {"version": "==2.0.0"},
            "ghost": {"version": "==1.0.0", "index": "not-in-meta"},
            "oddball": {"version": "==1.0.0", "index": "broken"},
        },
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


def test_pipfile_lock_private_index_is_skipped(tmp_path, fake_pypi):
    (src,) = load_inputs([str(_two_source_pipfile_lock(tmp_path))])
    # Parsing records the index; fill_all decides whether it may be looked up (see test_may_look_up).
    assert [(p.name, p.skip, p.registry) for p in src.packages] == [
        ("acme-utils", None, "https://pkgs.example.com/simple"),
        ("requests", None, None),
        ("six", None, None),  # pypi.python.org is PyPI too
        ("fastlib", None, None),
        ("ghost", "private index", None),  # an index the lock doesn't describe: can't tell it's PyPI
        ("oddball", None, "https://[bad/simple"),
    ]
    pypi.fill_all(src.packages)
    assert [(p.name, p.skip) for p in src.packages] == [
        ("acme-utils", "private index"),
        ("requests", None),
        ("six", None),
        ("fastlib", None),
        ("ghost", "private index"),
        ("oddball", "private index"),  # a URL urlsplit rejects: skipped, not a crash
    ]
    assert not [url for url in fake_pypi if "acme" in url or "ghost" in url or "oddball" in url], fake_pypi


def test_pipfile_lock_private_index_in_one_category_skips_the_package(tmp_path, fake_pypi):
    # Pipenv writes "index" on direct dependencies: the same package can be direct in develop, transitive in default.
    path = tmp_path / "Pipfile.lock"
    lock = {
        "_meta": {"sources": [{"name": "internal", "url": "https://pkgs.example.com/simple"}]},
        "default": {"acme-utils": {"version": "==2.1.0"}},
        "develop": {"acme-utils": {"version": "==2.1.0", "index": "internal"}},
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    assert [(p.name, p.registry) for p in src.packages] == [("acme-utils", "https://pkgs.example.com/simple")]
    pypi.fill_all(src.packages)
    assert [(p.name, p.skip) for p in src.packages] == [("acme-utils", "private index")]
    assert fake_pypi == []


def test_pipfile_lock_package_pinned_to_two_indexes_is_skipped(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "_meta": {
            "sources": [
                {"name": "mirror", "url": "https://mirror.example.com/simple"},
                {"name": "internal", "url": "https://pkgs.example.com/simple"},
            ]
        },
        "default": {"acme-utils": {"version": "==2.1.0", "index": "mirror"}},
        "develop": {"acme-utils": {"version": "==2.1.0", "index": "internal"}},
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    assert [(p.name, p.skip) for p in src.packages] == [("acme-utils", "private index")]


def test_pipfile_lock_malformed_package_entry_raises(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {"default": {"requests": "==2.31.0"}}
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(SourceError, match="isn't a package entry"):
        load_inputs([str(path)])


@pytest.mark.parametrize("section", ["default", "develop", "docs"])
@pytest.mark.parametrize("value", [None, [], "x"])
def test_pipfile_lock_malformed_section_raises(tmp_path, section, value):
    path = tmp_path / "Pipfile.lock"
    path.write_text(json.dumps({"_meta": {}, section: value}), encoding="utf-8")
    with pytest.raises(SourceError, match=f"{section} isn't a package section"):
        load_inputs([str(path)])


def test_pdm_lock_extras_variants_count_once(tmp_path):
    # PDM writes one entry per extras variant (seen in PDM's own lockfile for coverage[toml]).
    (tmp_path / "pdm.lock").write_text(
        '[[package]]\nname = "coverage"\nversion = "7.13.5"\nmarker = "sys_platform == \'linux\'"\n'
        'files = [{file = "coverage-7.13.5.tar.gz", hash = "sha256:00"}]\n\n'
        '[[package]]\nname = "coverage"\nversion = "7.13.5"\nextras = ["toml"]\n'
        'files = [{file = "coverage-7.13.5.tar.gz", hash = "sha256:00"}]\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert [(p.name, p.version) for p in src.packages] == [("coverage", "7.13.5")]
    assert src.packages[0].markers is None  # the unconditional variant wins


def test_requirements_bare_vcs_urls_are_skipped(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text(
        "git+ssh://git@github.com/org/repo.git#egg=foo\n"  # the "@" in git@ used to abort the whole run
        "hg+https://hg.example.com/bar#egg=bar&subdirectory=src\n"
        "https://example.com/pkgs/baz-1.0.tar.gz\n"
        "named @ git+https://github.com/org/named.git\n"
        "requests==2.32.3\n",
        encoding="utf-8",
    )
    (src,) = load_inputs([str(path)])
    got = {p.name: (p.skip, p.version) for p in src.packages}
    assert got["foo"] == ("url", None) and got["bar"] == ("url", None)
    assert got["https://example.com/pkgs/baz-1.0.tar.gz"] == ("url", None)
    assert got["named"] == ("url", None)
    assert got["requests"] == (None, "2.32.3")


def test_uv_path_dependency_is_not_a_root(tmp_path):
    # winhelper is a path dependency only installed on Windows; its own dependency must follow suit.
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "winhelper", marker = "sys_platform == \'win32\'" }]\n\n'
        '[[package]]\nname = "winhelper"\nversion = "1"\nsource = { directory = "libs/winhelper" }\n'
        'dependencies = [{ name = "pywin32" }]\n\n'
        '[[package]]\nname = "pywin32"\nversion = "311"\nsource = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://files.pythonhosted.org/p/pywin32-311-cp313-cp313-win_amd64.whl" }]\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    rows = {r.package.name: r for r in check([src], [lin, win])}
    assert rows["pywin32"].results == {lin: Status.NOT_NEEDED, win: Status.WHEEL}


@pytest.mark.parametrize("markers", ["[\"sys_platform == 'win32'\"]", "[]", "1", "true", "{ main = 1 }"])
def test_poetry_rejects_wrong_typed_markers(tmp_path, markers):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "colorama"\nversion = "0.4.6"\ngroups = ["main", "dev"]\n'
        f'markers = {markers}\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"poetry\.lock.*colorama.*markers"):
        load_inputs([str(tmp_path)])


def test_poetry_dict_markers_with_an_unconditional_group(tmp_path):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "colorama"\nversion = "0.4.6"\ngroups = ["main", "dev"]\n'
        'markers = { main = "sys_platform == \\"win32\\"" }\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert src.packages[0].markers is None  # dev always installs it, so it's needed everywhere


def test_poetry_1x_lock_walks_dependency_markers(tmp_path):
    # The project's own dependencies (#815: without them a Poetry 1.x lock is refused where that could hide a gap).
    (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\napp-lib = "*"\n', encoding="utf-8")
    # Before lock-version 2.1, markers only sit on dependency edges.
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-lib"\nversion = "1.0"\n\n[package.dependencies]\n'
        'pywin32 = {version = ">=300", markers = "sys_platform == \\"win32\\""}\nrequests = "*"\n\n'
        '[[package]]\nname = "pywin32"\nversion = "306"\nfiles = []\n\n'
        '[[package]]\nname = "requests"\nversion = "2.32.3"\nfiles = []\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    rows = {r.package.name: r for r in check([src], [lin, win])}
    assert rows["pywin32"].results[lin] is Status.NOT_NEEDED  # used to be a false "missing" on Linux
    assert rows["pywin32"].results[win] is not Status.NOT_NEEDED
    assert rows["requests"].results[lin] is not Status.NOT_NEEDED


def test_poetry_package_without_files_is_looked_up(tmp_path):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "x"\nversion = "1"\n\n[metadata]\nlock-version = "2.1"\n'
    )
    (src,) = load_inputs([str(tmp_path)])
    assert src.packages[0].files is None  # not [] (which would mean "can't install")


def test_requirements_parsing_edge_cases(tmp_path):
    (tmp_path / "more.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "-rmore.txt\n"  # no space: used to be silently dropped
        "fastlib==2.0.0 \\\n    --hash sha256:00\n"  # --hash with a space used to abort the run
        "nowheels==0.9\t# a comment after a tab used to abort the run\n"
        "requests==2.32.3\t# see https://docs.python-requests.org\n"  # used to be skipped as a URL
        "named@https://example.com/named-1.0.tar.gz\n"
        "git+https://example.com/repo.git#subdirectory=src&egg=deep ; python_version >= '3.10'\n",
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path / "requirements.txt")])
    got = {p.name: (p.version, p.skip) for p in src.packages}
    assert got["purewheel"] == ("3.1", None)
    assert got["fastlib"] == ("2.0.0", None)
    assert got["nowheels"] == ("0.9", None)
    assert got["requests"] == ("2.32.3", None)
    assert got["named"] == (None, "url")
    assert got["deep"] == (None, "url")


def test_poetry_1x_dependency_cycle_is_still_walked(tmp_path):
    # sphinx and sphinxcontrib-applehelp depend on each other, so neither is "depended on by nothing".
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "requests"\nversion = "2.32.3"\nfiles = []\n\n'
        '[[package]]\nname = "sphinx"\nversion = "7.2.6"\nfiles = []\n\n[package.dependencies]\n'
        '"sphinxcontrib-applehelp" = "*"\ndocutils = ">=0.18"\n\n'
        '[[package]]\nname = "sphinxcontrib-applehelp"\nversion = "1.0.8"\nfiles = []\n\n'
        '[package.dependencies]\nSphinx = ">=5"\n\n'
        '[[package]]\nname = "docutils"\nversion = "0.20.1"\nfiles = []\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = {r.package.name: r.results[lin] for r in check([src], [lin])}
    assert all(s is not Status.NOT_NEEDED for s in rows.values()), rows  # docutils used to be "n/a"


def test_poetry_1x_edges_pick_the_version_in_range(tmp_path):
    # The project's own dependencies (#815: without them a Poetry 1.x lock is refused where that could hide a gap).
    (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\napp-lib = "*"\n', encoding="utf-8")
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "app-lib"\nversion = "1.0"\nfiles = []\n\n[package.dependencies]\n'
        'numpy = [{version = "1.24.4", markers = "python_version < \\"3.9\\""}, '
        '{version = ">=1.26", markers = "python_version >= \\"3.9\\""}]\n\n'
        '[[package]]\nname = "numpy"\nversion = "1.24.4"\nfiles = []\n\n'
        '[[package]]\nname = "numpy"\nversion = "1.26.4"\nfiles = []\n\n'
        '[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    (t,) = build_targets(["3.12"], ["linux-x86_64"])
    got = {(r.package.name, r.package.version): r.results[t] for r in check([src], [t])}
    assert got[("numpy", "1.24.4")] is Status.NOT_NEEDED  # only for Python < 3.9
    assert got[("numpy", "1.26.4")] is not Status.NOT_NEEDED


def test_pipfile_lock_vcs_skipped(tmp_path):
    path = tmp_path / "Pipfile.lock"
    lock = {
        "default": {
            "hgpkg": {"hg": "https://hg.example.com/repo"},
            "svnpkg": {"svn": "svn://svn.example.com/repo"},
            "bzrpkg": {"bzr": "lp:mybranch"},
        }
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert pkgs["hgpkg"].skip == "hg"
    assert pkgs["svnpkg"].skip == "svn"
    assert pkgs["bzrpkg"].skip == "bzr"


def test_pdm_lock_vcs_skipped(tmp_path):
    path = tmp_path / "pdm.lock"
    lock = (
        'version = "4.5"\n\n'
        '[[package]]\nname = "hgpkg"\nversion = "0.1"\nhg = "https://hg.example.com/repo"\n\n'
        '[[package]]\nname = "svnpkg"\nversion = "0.1"\nsvn = "svn://svn.example.com/repo"\n\n'
        '[[package]]\nname = "bzrpkg"\nversion = "0.1"\nbzr = "lp:mybranch"\n'
    )
    path.write_text(lock, encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert pkgs["hgpkg"].skip == "hg"
    assert pkgs["svnpkg"].skip == "svn"
    assert pkgs["bzrpkg"].skip == "bzr"


def test_poetry_lock_vcs_skipped(tmp_path):
    path = tmp_path / "poetry.lock"
    lock = (
        '[[package]]\nname = "hgpkg"\nversion = "0.1"\n[package.source]\ntype = "hg"\n\n'
        '[[package]]\nname = "svnpkg"\nversion = "0.1"\n[package.source]\ntype = "svn"\n\n'
        '[[package]]\nname = "bzrpkg"\nversion = "0.1"\n[package.source]\ntype = "bzr"\n'
    )
    path.write_text(lock, encoding="utf-8")
    (src,) = load_inputs([str(path)])
    pkgs = by_name(src)
    assert pkgs["hgpkg"].skip == "hg"
    assert pkgs["svnpkg"].skip == "svn"
    assert pkgs["bzrpkg"].skip == "bzr"


def test_poetry_1x_roots_are_cycles_nothing_else_reaches(tmp_path):
    # The project's own dependencies (#815: without them a Poetry 1.x lock is refused where that could hide a gap).
    (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\nz1 = "*"\n', encoding="utf-8")
    # a1<->a2 and z1<->z2 are cycles; z1 -> a1 only on Windows. Only the z cycle is a starting point,
    # so a1/a2 must not be needed on Linux (they used to be made roots too, by index order).
    pkg = '[[package]]\nname = "{n}"\nversion = "1"\nfiles = []\n\n[package.dependencies]\n{deps}\n\n'
    lock = (
        pkg.format(n="a1", deps='a2 = "*"')
        + pkg.format(n="a2", deps='a1 = "*"')
        + pkg.format(n="z1", deps='z2 = "*"\na1 = {version = "*", markers = "sys_platform == \\"win32\\""}')
        + pkg.format(n="z2", deps='z1 = "*"')
        + '[metadata]\nlock-version = "1.1"\n'
    )
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    (src,) = load_inputs([str(tmp_path)])
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    rows = {r.package.name: r.results for r in check([src], [lin, win])}
    assert rows["a1"][lin] is Status.NOT_NEEDED and rows["a2"][lin] is Status.NOT_NEEDED
    assert rows["a1"][win] is not Status.NOT_NEEDED and rows["z1"][lin] is not Status.NOT_NEEDED


def test_poetry_1x_direct_dependency_from_pyproject_is_a_root(tmp_path):
    # colorama is a direct dependency, and click also needs it on Windows only.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.10"\nclick = "*"\ncolorama = "*"\n', encoding="utf-8"
    )
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "click"\nversion = "8.1.7"\nfiles = []\n\n[package.dependencies]\n'
        'colorama = {version = "*", markers = "platform_system == \\"Windows\\""}\n\n'
        '[[package]]\nname = "colorama"\nversion = "0.4.6"\nfiles = []\n\n[metadata]\nlock-version = "1.1"\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = {r.package.name: r.results[lin] for r in check([src], [lin])}
    assert rows["colorama"] is not Status.NOT_NEEDED


@pytest.mark.parametrize(
    "pyproject",
    [
        'project = "x"\n',
        '[tool.poetry.group]\ndev = "x"\n',
        "[project]\ndependencies = [1]\n",
        b"\xff\xfe".decode("latin-1"),
    ],
)
def test_odd_pyproject_never_breaks_a_poetry_run(tmp_path, pyproject):
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="latin-1")
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "x"\nversion = "1"\nfiles = []\n\n[metadata]\nlock-version = "1.1"\n', encoding="utf-8"
    )
    (src,) = load_inputs([str(tmp_path)])
    assert [p.name for p in src.packages] == ["x"]


@pytest.mark.parametrize(
    ("requires", "pythons", "outside"),
    [
        ("==3.13.*", ["3.13", "3.14t"], ["3.14t"]),
        (">=3.11.4", ["3.11", "3.12"], []),  # a patch-level floor still serves 3.11
        (">=3.11, <3.15", ["3.10", "3.14", "3.15"], ["3.10", "3.15"]),
        (None, ["3.14"], []),
        ("not a specifier", ["3.14"], []),
        ("==3.12.4", ["3.12", "3.13"], ["3.13"]),  # a pinned patch still serves its minor
        (">=3.11,<3.11.5", ["3.11"], []),
        ("==3.14.0rc2", ["3.14"], []),
        (">=3.10, !=3.12.*", ["3.12"], []),  # uv only uses the bounds, so 3.12's wheels stay
    ],
)
def test_pythons_outside_lock(requires, pythons, outside):
    source = Source("uv.lock", requires_python=requires)
    assert pythons_outside_lock(source, build_targets(pythons, ["linux-x86_64"])) == outside


def test_uv_lock_records_requires_python(tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\nrequires-python = "==3.13.*"\n', encoding="utf-8")
    (src,) = load_inputs([str(tmp_path)])
    assert src.requires_python == "==3.13.*"


def test_pylock_records_requires_python_and_index(tmp_path):
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\nrequires-python = "==3.13.*"\n\n[[packages]]\nname = "fastlib"\nversion = "2.0.0"\n'
        'index = "https://pypi.org/simple"\n'
        'wheels = [{ name = "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]\n',
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path / "pylock.toml")])
    assert src.requires_python == "==3.13.*" and src.packages[0].registry == "https://pypi.org/simple"


def test_uv_lock_file_url_that_cannot_be_parsed_is_an_input_error(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "b"\nversion = "1.0"\n'
        'wheels = [{ url = "https://[files.pythonhosted.org/b-1.0-py3-none-any.whl" }]\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"uv\.lock.*package b.*can't be parsed"):
        load_inputs([str(tmp_path)])


def test_uv_lock_file_url_with_a_space_in_the_host_is_an_input_error(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "b"\nversion = "1.0"\n'
        'wheels = [{ url = "https://files pythonhosted.org/b-1.0-py3-none-any.whl" }]\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"uv\.lock.*package b.*can't be parsed"):
        load_inputs([str(tmp_path)])


def test_pylock_sdist_url_that_cannot_be_parsed_is_an_input_error(tmp_path):
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\n\n[[packages]]\nname = "b"\nversion = "1.0"\n'
        'sdist = { url = "https://[files.pythonhosted.org/b-1.0.tar.gz" }\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"pylock\.toml.*package b.*can't be parsed"):
        load_inputs([str(tmp_path / "pylock.toml")])


def test_pylock_sdist_url_that_cannot_be_parsed_is_an_input_error_even_with_a_name(tmp_path):
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\n\n[[packages]]\nname = "b"\nversion = "1.0"\n'
        'sdist = { name = "b-1.0.tar.gz", url = "https://user:secret@[files.pythonhosted.org/b-1.0.tar.gz" }\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"pylock\.toml.*package b.*can't be parsed") as e:
        load_inputs([str(tmp_path / "pylock.toml")])
    assert "secret" not in str(e.value)  # the URL may carry credentials, so it isn't echoed back


def test_pdm_lock_file_url_that_cannot_be_parsed_is_an_input_error(tmp_path):
    (tmp_path / "pdm.lock").write_text(
        '[[package]]\nname = "b"\nversion = "1.0"\n'
        'files = [{ url = "https://[files.pythonhosted.org/b-1.0-py3-none-any.whl", hash = "sha256:00" }]\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError, match=r"pdm\.lock.*package b.*can't be parsed"):
        load_inputs([str(tmp_path / "pdm.lock")])


def test_uv_lock_file_urls_that_still_parse_keep_their_file_names(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "b"\nversion = "1.0"\n'
        "wheels = [\n"
        '    { url = "https://files.example/b-1.0-py3-none-any.whl;x=1" },\n'  # URL parameters aren't the name
        '    { url = "https://[::1]/b-1.0-cp313-cp313-win_amd64.whl" },\n'
        '    { url = "https://user:p w@files.example/b-1.0-cp313-cp313-win_arm64.whl" },\n'
        "]\n",
        encoding="utf-8",
    )
    (src,) = load_inputs([str(tmp_path)])
    assert src.packages[0].files == [
        "b-1.0-py3-none-any.whl",
        "b-1.0-cp313-cp313-win_amd64.whl",
        "b-1.0-cp313-cp313-win_arm64.whl",
    ]


def test_pipfile_lock_non_string_version_raises(tmp_path):
    path = tmp_path / "Pipfile.lock"
    path.write_text(json.dumps({"default": {"requests": {"version": 2}}}), encoding="utf-8")
    with pytest.raises(SourceError, match="default.requests has a version that isn't a string"):
        load_inputs([str(path)])


def test_poetry_lock_records_a_legacy_source_as_the_registry(tmp_path):
    path = tmp_path / "poetry.lock"
    path.write_text(
        '[[package]]\nname = "purewheel"\nversion = "3.1"\n'
        'files = [{file = "purewheel-3.1-cp313-cp313-win_amd64.whl", hash = "sha256:00"}]\n\n'
        '[package.source]\ntype = "legacy"\nurl = "https://pkgs.example.com/simple"\nreference = "private"\n\n'
        '[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    (pkg,) = parse_poetry_lock(path).packages
    assert pkg.registry == "https://pkgs.example.com/simple"


PYPROJECT = """\
[project]
name = "My_Lib"
dependencies = ["ranged>=1.5,<2", "fastlib==2.0.0", "winonly; sys_platform == 'win32'"]

[project.optional-dependencies]
Fast = ["purewheel"]
test = ["nowheels", "my-lib[fast]"]
"""


def test_pyproject_direct_dependencies(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT, encoding="utf-8")
    (source,) = load_inputs([str(path)])
    pkgs = by_name(source)
    assert set(pkgs) == {"ranged", "fastlib", "winonly"}
    assert pkgs["ranged"].spec == "<2,>=1.5" and pkgs["ranged"].version is None
    assert pkgs["fastlib"].version == "2.0.0"
    assert pkgs["winonly"].markers == ['sys_platform == "win32"']
    assert source.pyproject_extras == {"fast": ["purewheel"], "test": ["nowheels", "my-lib[fast]"]}


def test_pyproject_records_requires_python(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\ndependencies = []\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    (source,) = load_inputs([str(path)])
    assert source.requires_python == ">=3.11"


@pytest.mark.parametrize(
    "extras,all_extras,added",
    [
        (["fast"], False, {"purewheel"}),
        (["FAST"], False, {"purewheel"}),
        (["test"], False, {"nowheels", "purewheel"}),  # my-lib[fast] pulls in the fast extra
        ([], True, {"nowheels", "purewheel"}),
        ([], False, set()),
    ],
)
def test_pyproject_extras(tmp_path, extras, all_extras, added):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT, encoding="utf-8")
    (source,) = load_inputs([str(path)])
    select_pyproject_extras(source, extras, all_extras)
    names = [p.name for p in source.packages]
    assert sorted(names) == sorted({"ranged", "fastlib", "winonly"} | added)  # each once, and never my-lib itself


def test_pyproject_unknown_extra(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT, encoding="utf-8")
    (source,) = load_inputs([str(path)])
    with pytest.raises(SourceError, match="unknown extras: docs"):
        select_pyproject_extras(source, ["docs"], False)


@pytest.mark.parametrize(
    "text,error",
    [
        ('[project]\nname = "x"\ndynamic = ["dependencies"]\n', "dependencies is dynamic"),
        ('[tool.poetry]\nname = "x"\n', "no \\[project\\] table"),
        ('[project]\nname = "x"\ndependencies = "numpy"\n', "must be a list of strings"),
        ('[project]\nname = "x"\ndependencies = ["numpy>=="]\n', "can't parse requirement"),
        ("[project\n", "invalid TOML"),
        (
            '[project]\nname = "x"\n\n[tool.poetry.dependencies]\npython = ">=3.10"\nnumpy = "^2"\n',
            "dependencies are in \\[tool.poetry.dependencies\\].*check poetry.lock",
        ),
        (
            '[project]\nname = "x"\ndependencies = []\n\n[tool.poetry.dependencies]\nnumpy = "^2"\n',
            "dependencies are in \\[tool.poetry.dependencies\\].*check poetry.lock",
        ),
        ('[project]\nname = "x"\n[project.optional-dependencies]\nTest = ["a"]\ntest = ["b"]\n', "PEP 685"),
        ('[project]\nname = "x"\ndependencies = ["x[nope]"]\n', "names unknown extras: nope"),
        ('[project]\nname = "x"\ndependencies = ["x[a]"]\ndynamic = ["optional-dependencies"]\n', "is dynamic"),
        ('[project]\nname = "x"\ndependencies = ["x[a]; os_name == \'nt\'"]\n', "refers to the project itself"),
    ],
)
def test_pyproject_input_errors(tmp_path, text, error):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SourceError, match=error):
        load_inputs([str(path)])


def test_pyproject_dynamic_optional_dependencies_with_extra(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\ndependencies = []\ndynamic = ["optional-dependencies"]\n', encoding="utf-8")
    (source,) = load_inputs([str(path)])
    select_pyproject_extras(source, [], False)  # no extra asked for: fine
    with pytest.raises(SourceError, match="optional-dependencies is dynamic"):
        select_pyproject_extras(source, ["test"], False)
    with pytest.raises(SourceError, match="optional-dependencies is dynamic"):
        select_pyproject_extras(source, [], True)  # --all-extras can't mean "none"


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nname = "x"\n\n[tool.poetry.dependencies]\npython = ">=3.10"\n',  # python isn't a dependency
        '[project]\nname = "x"\ndependencies = ["purewheel"]\n\n[tool.poetry.dependencies]\nnumpy = "^2"\n',
    ],
)
def test_pyproject_reads_project_dependencies_alongside_tool_poetry(tmp_path, text):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    (source,) = load_inputs([str(path)])
    assert [p.name for p in source.packages] == (["purewheel"] if "purewheel" in text else [])


def test_pyproject_extras_add_each_requirement_once(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\ndependencies = ["numpy>=2"]\n\n[project.optional-dependencies]\n'
        'plot = ["matplotlib"]\nall = ["matplotlib", "scipy", "numpy >= 2"]\n',
        encoding="utf-8",
    )
    (source,) = load_inputs([str(path)])
    select_pyproject_extras(source, [], True)
    assert sorted(p.name for p in source.packages) == ["matplotlib", "numpy", "scipy"]


def test_pyproject_self_reference_in_dependencies_adds_the_extra(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT.replace('"fastlib==2.0.0", ', '"fastlib==2.0.0", "my-lib[test]", '), encoding="utf-8")
    (source,) = load_inputs([str(path)])
    # my-lib[test] pulls in test, and test's my-lib[fast] pulls in fast; my-lib itself is never looked up
    assert sorted(p.name for p in source.packages) == ["fastlib", "nowheels", "purewheel", "ranged", "winonly"]
    select_pyproject_extras(source, ["test"], False)  # asking for it again adds nothing twice
    assert sorted(p.name for p in source.packages) == ["fastlib", "nowheels", "purewheel", "ranged", "winonly"]


def test_no_lockfile_suggests_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(SourceError, match="no lockfile found.*wheelgap .*pyproject.toml"):
        find_lockfile(tmp_path)


def test_no_lockfile_without_project_table_has_no_pyproject_hint(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
    with pytest.raises(SourceError, match="no lockfile found") as e:
        find_lockfile(tmp_path)
    assert "pyproject.toml`" not in str(e.value)


GROUPS_ONLY = '[dependency-groups]\ntest = ["pytest", "numpy"]\nlint = ["ruff"]\n'


def test_no_lockfile_groups_only_suggests_sorted_group_selection(tmp_path):
    (tmp_path / "pyproject.toml").write_text(GROUPS_ONLY, encoding="utf-8")
    with pytest.raises(SourceError) as e:
        find_lockfile(tmp_path)
    message = str(e.value)
    assert f"wheelgap {tmp_path / 'pyproject.toml'} --group NAME` (groups: lint, test)." in message
    assert message.endswith("Pass a file, or package names like `wheelgap numpy psycopg2`.")


@pytest.mark.parametrize(
    "contents",
    ["[dependency-groups]\n", "[project]\nname = 'x'\n[dependency-groups]\n", "[project]\nname = 'x'\n" + GROUPS_ONLY],
    ids=["no-groups", "project-and-no-groups", "project-and-groups"],
)
def test_no_lockfile_empty_groups_or_project_keeps_existing_hint_rules(tmp_path, contents):
    (tmp_path / "pyproject.toml").write_text(contents, encoding="utf-8")
    with pytest.raises(SourceError) as e:
        find_lockfile(tmp_path)
    message = str(e.value)
    assert "--group" not in message
    if "[project]" in contents:
        assert f"wheelgap {tmp_path / 'pyproject.toml'}`" in message
    else:
        assert "pyproject.toml`" not in message


@pytest.mark.parametrize(
    "contents,beside",
    [
        (GROUPS_ONLY, "setup.py"),
        (GROUPS_ONLY, "setup.cfg"),
        ('[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n' + GROUPS_ONLY, None),
        ('[tool.flit.metadata]\nmodule = "mypkg"\nrequires = ["numpy"]\n' + GROUPS_ONLY, None),
        ('[tool.pdm]\ndependencies = ["numpy"]\n' + GROUPS_ONLY, None),
        ('[tool.poetry.dependencies]\npython = "^3.10"\nnumpy = "*"\n' + GROUPS_ONLY, None),
        ('project = "invalid"\n' + GROUPS_ONLY, None),
    ],
    ids=["setup-py", "setup-cfg", "build-system", "flit-metadata", "pdm-dependencies", "poetry", "project-not-a-table"],
)
def test_no_lockfile_groups_only_in_a_buildable_project_has_no_group_hint(tmp_path, contents, beside):
    # `wheelgap pyproject.toml --group NAME` refuses these (the project's own dependencies are outside [project]),
    # so the hint mustn't send anyone there.
    (tmp_path / "pyproject.toml").write_text(contents, encoding="utf-8")
    if beside:
        (tmp_path / beside).write_text("", encoding="utf-8")
    with pytest.raises(SourceError, match="no lockfile found") as e:
        find_lockfile(tmp_path)
    assert "pyproject.toml`" not in str(e.value) and "--group" not in str(e.value)


def test_no_lockfile_group_hint_repeats_only_valid_group_names(tmp_path):
    # A quoted TOML key can hold anything: line breaks, escape codes, a workflow command, a sentence.
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\n"
        'Type_Check = []\n"lint.fast" = []\ndocs = []\n'
        '"a\\n::error::b" = []\n"\\u001b[2J" = []\n"run this instead" = []\n"-x" = []\n"caf\\u00e9" = []\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError) as e:
        find_lockfile(tmp_path)
    assert str(e.value).isprintable()
    assert "(groups: docs, lint-fast, type-check). Pass a file" in str(e.value)


def test_no_lockfile_groups_that_collide_once_normalized_have_no_hint(tmp_path):
    # `--group` refuses this file whichever group is asked for, so there is no command to suggest.
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\nType_Check = []\n"type.check" = []\ndocs = []\n', encoding="utf-8"
    )
    with pytest.raises(SourceError, match="no lockfile found") as e:
        find_lockfile(tmp_path)
    assert "pyproject.toml`" not in str(e.value) and "--group" not in str(e.value)


def test_no_lockfile_groups_with_no_valid_name_has_no_hint(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\n"a\\n::error::b" = ["numpy"]\n', encoding="utf-8")
    with pytest.raises(SourceError, match="no lockfile found") as e:
        find_lockfile(tmp_path)
    assert "pyproject.toml`" not in str(e.value) and "::error::" not in str(e.value)


def test_no_lockfile_unreadable_pyproject_has_no_hint(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b"[dependency-groups]\xff")
    with pytest.raises(SourceError) as e:
        find_lockfile(tmp_path)
    assert "pyproject.toml`" not in str(e.value)


def test_poetry_group_selection_keeps_markers_per_group(tmp_path):
    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "colorama"\nversion = "0.4.6"\ngroups = ["main", "dev"]\n'
        'markers = { main = "sys_platform == \\"win32\\"" }\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
        encoding="utf-8",
    )
    sources = load_inputs([str(tmp_path)])
    select_uv_dependencies(sources, UvSelection(no_dev=True))
    # Without dev, only main's marker applies.
    assert sources[0].packages[0].markers == ['sys_platform == "win32"']


def test_pixi_lock(fixtures):
    source = load_inputs([str(fixtures / "pixiproj")])[0]
    assert source.label == "pixi.lock" and source.kind == "pixi"
    pkgs = by_name(source)
    # Each platform records the one file pixi picked there; together they're the package's files, which are only
    # some of the release's, so the package is still looked up on its index.
    assert pkgs["fastlib"].files == ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]
    assert pkgs["fastlib"].partial_files and pkgs["fastlib"].registry is None
    assert pkgs["fastlib"].markers is None  # on every platform
    assert pkgs["internal"].registry == "https://example.com/simple"
    assert {name: p.skip for name, p in pkgs.items() if p.skip} == {
        "gitdep": "git",
        "localpkg": "conda",
        "openssl": "conda",
        "python": "conda",
    }
    assert pkgs["python"].version == "3.13.9"
    # winonly is locked on win-64 only: hidden on the lock's other platforms for its Python (3.13), nowhere else.
    linux, mac, win = build_targets(["3.13"], ["linux-x86_64", "macos-arm64", "win-amd64"])
    (linux14,) = build_targets(["3.14"], ["linux-x86_64"])
    rows = {r.package.name: r for r in check([source], [linux, mac, win, linux14])}
    assert rows["winonly"].results == {
        linux: Status.NOT_NEEDED,
        mac: Status.NOT_NEEDED,
        win: Status.WHEEL,
        linux14: Status.MISSING,
    }
    # A platform the lock doesn't cover still counts it: what pixi would install there isn't known. musl-x86_64
    # evaluates markers as linux-64 does (linux, x86_64), so it's hidden there too.
    musl, arm = build_targets(["3.13"], ["musl-x86_64", "linux-aarch64"])
    rows = {r.package.name: r for r in check([source], [musl, arm])}
    assert rows["winonly"].results == {musl: Status.NOT_NEEDED, arm: Status.MISSING}


def test_pixi_lock_environment(fixtures):
    source = load_inputs([str(fixtures / "pixiproj")], "test")[0]
    assert [(p.name, p.skip) for p in source.packages] == [("nowheels", None), ("python", "conda")]
    with pytest.raises(SourceError, match=r"pixi.lock: no environment 'docs' \(it has: default, test\)"):
        load_inputs([str(fixtures / "pixiproj")], "docs")
    with pytest.raises(SourceError, match="--environment applies to pixi.lock inputs only"):
        load_inputs([str(fixtures / "uvproj")], "test")


def test_find_lockfile_finds_pixi_lock(fixtures):
    assert find_lockfile(fixtures / "pixiproj").name == "pixi.lock"


PIXI_V6 = """\
version: 6
environments:
  default:
    channels:
    - url: https://conda.anaconda.org/conda-forge/
    indexes:
    - INDEX
    packages:
      linux-64:
      - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.8-h9e4cc4f_1_cpython.conda
      - pypi: https://files.pythonhosted.org/packages/aa/purewheel-3.1-py3-none-any.whl
      - pypi: https://mirror.example.com/files/fastlib-2.0.0.tar.gz
      - pypi: ./
packages:
- conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.8-h9e4cc4f_1_cpython.conda
  sha256: 0123
  md5: 4567
  depends:
  - libgcc >=13
  license: Python-2.0
- pypi: https://files.pythonhosted.org/packages/aa/purewheel-3.1-py3-none-any.whl
  name: purewheel
  version: '3.1'
  sha256: 89ab
- pypi: https://mirror.example.com/files/fastlib-2.0.0.tar.gz
  name: fastlib
  version: 2.0.0
  sha256: cdef
- pypi: ./
  name: myproject
  version: 0.1.0
  sha256: 0011
  requires_dist:
  - "fastlib ; python_version >= \\"3.10\\""
  requires_python: '>=3.10'
  editable: true
"""


@pytest.mark.parametrize(
    "index,fastlib",
    [
        # PyPI serves files only from files.pythonhosted.org, so a file elsewhere is a direct URL.
        ("https://pypi.org/simple", "url"),
        # A version 6 lock doesn't say which index a package came from: a name off PyPI's file host may be internal.
        ("https://mirror.example.com/simple", "private index"),
    ],
)
def test_pixi_lock_version_6(tmp_path, index, fastlib):
    lock = tmp_path / "pixi.lock"
    lock.write_text(PIXI_V6.replace("INDEX", index), encoding="utf-8")
    pkgs = by_name(load_inputs([str(lock)])[0])
    assert {name: p.skip for name, p in pkgs.items()} == {
        "purewheel": None,  # on PyPI's file host: a PyPI package whatever the indexes
        "fastlib": fastlib,
        "myproject": "editable",
        "python": "conda",
    }
    assert pkgs["purewheel"].files == ["purewheel-3.1-py3-none-any.whl"]


PIXI_V7 = (Path(__file__).parent / "fixtures" / "pixiproj" / "pixi.lock").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "old,new,message",
    [
        # Lock versions it doesn't know, older or newer: refused, never read as if they were 6 or 7.
        ("version: 7", "version: 5", "pixi.lock version '5' isn't supported"),
        ("version: 7", "version: 8", "pixi.lock version '8' isn't supported"),
        ("version: 7\n", "", "no top-level `version`"),
        # YAML outside the subset pixi writes
        ("- name: osx-arm64", "- name: &p osx-arm64", "unsupported YAML"),
        ("  license: Apache-2.0\n", "  license: |\n    Apache-2.0\n", "unsupported YAML"),
        ("  purls: []\n  run_exports:", "  purls: [pkg:pypi/x]\n  run_exports:", "unsupported YAML"),
        ("  name: winonly\n", "  name: winonly # a comment\n", "unsupported YAML"),
        ("  name: winonly\n", "  name: winonly\n    more\n", "unexpected indentation"),
        ("  name: winonly\n", "  name: 'winonly\n", "must end on its own line"),
        ("  name: winonly\n", '  name: "win\\qonly"\n', "unsupported escape"),
        ("  name: winonly\n", "  name: winonly\n  name: other\n", "duplicate key 'name'"),
        ("  name: winonly\n", "\tname: winonly\n", "unsupported character '\\t'"),
        ("  name: winonly\n", "  name: winonly\n  []: x\n", "unsupported key '[]'"),
        ("version: 7\n", "version: 7\n# generated\n", "comments and document markers"),
        ("version: 7\n", "version: 7\n---\n", "comments and document markers"),
        # The version the reader checked must be the one the YAML says ('7\'\'' is the string "7'").
        ("version: 7\n", "version: '7'''\n", "with that `version`"),
        (
            "- pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n  name: nowheels\n",
            "- pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n  name: nowheels\n"
            "  version: '0.9'\n"
            "- pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n  name: nowheels\n",
            "two PyPI packages at",
        ),
        # Structure it can't be sure of
        ("platforms:", "platform:", "unknown top-level key 'platform'"),
        (
            "  requires_python: '>=3.9'\n- pypi: https://files",
            "  editable: 'true'\n- pypi: https://files",
            "key 'editable'",
        ),
        (
            "    indexes:\n    - https://pypi.org/simple\n    packages:\n      linux-64:\n      - conda: https://conda.anaconda.org/conda-forge/linux-64/openssl",
            "    packages:\n      linux-64:\n      - conda: https://conda.anaconda.org/conda-forge/linux-64/openssl",
            "has PyPI packages but no `indexes`",
        ),
        ("    channels:\n", "    solver: uv\n    channels:\n", "unknown key 'solver'"),
        ("- name: win-64\n", "", "platform 'win-64', which `platforms` doesn't list"),
        (
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n",
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-1.0.tar.gz\n",
            "isn't in `packages`",
        ),
        (
            "- pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n  name: nowheels\n",
            "- pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n",
            "has no name",
        ),
        (
            "  name: nowheels\n  version: '0.9'\n",
            "  name: nowheels\n",
            "nowheels comes from an index but has no version",
        ),
        (
            "- pypi: git+https://github.com/example/gitdep.git?rev=v1#0123456789abcdef0123456789abcdef01234567\n  name",
            "- pypi: git+https://github.com/example/gitdep.git?rev=v1#0123456789abcdef0123456789abcdef01234567\n"
            "  conda: x\n  name",
            "isn't exactly one of",
        ),
        (
            "git+https://github.com/example/gitdep.git",
            "ftp://github.com/example/gitdep.git",
            "location wheelgap can't read",
        ),
        (
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n",
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n        extras: [x]\n",
            "unsupported YAML",
        ),
        (
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n",
            "      - pypi: https://files.pythonhosted.org/packages/33/44/nowheels-0.9.tar.gz\n        extras: x\n",
            "has other keys",
        ),
    ],
)
def test_pixi_lock_refuses_what_it_cannot_read_for_sure(tmp_path, old, new, message):
    assert old in PIXI_V7
    lock = tmp_path / "pixi.lock"
    lock.write_text(PIXI_V7.replace(old, new), encoding="utf-8")
    with pytest.raises(SourceError, match="^pixi.lock: ") as e:
        parse_pixi_lock(lock, "test" if "nowheels" in old else None)
    assert message in str(e.value)


def test_pixi_lock_reads_quoted_scalars(tmp_path):
    lock = tmp_path / "pixi.lock"
    text = PIXI_V7.replace("  name: winonly\n", '  name: "win\\x6fnly"\n').replace(
        "  name: purewheel\n", "  'name': 'purewheel'\n"
    )
    lock.write_text(text, encoding="utf-8")
    # (A name with '' in it, such as pure'wheel, isn't a valid package name; test_block_yaml_reads covers the escape.)
    assert {"winonly", "purewheel"} <= set(by_name(parse_pixi_lock(lock)))


@pytest.mark.parametrize(
    "text,message",
    [
        # Tabs and control characters anywhere, which YAML would read differently or libyaml refuses
        ("- true\t\n", "unsupported character '\\t'"),
        ("- a,b\t# c\n", "unsupported character '\\t'"),
        ("- a:\tb\n", "unsupported character '\\t'"),
        ("k: \t\n", "unsupported character '\\t'"),
        ("version: 311\t\n", "unsupported character '\\t'"),
        ("a: 1\x0bb: 2\n", "unsupported character '\\x0b'"),
        ("a: 1\x0cb: 2\n", "unsupported character '\\x0c'"),
        ("a: 1\x1cb: 2\n", "unsupported character '\\x1c'"),
        ("a: 1\x1db: 2\n", "unsupported character '\\x1d'"),
        ("a: 1\x1eb: 2\n", "unsupported character '\\x1e'"),
        ("a: 1\u2028b: 2\n", "unsupported character '\\u2028'"),
        ("a: 1\u2029b: 2\n", "unsupported character '\\u2029'"),
        ("k: a\ufffe\n", "unsupported character '\\ufffe'"),
        ("k: 'a\uffff'\n", "unsupported character '\\uffff'"),
        ("k: a\x01b\n", "unsupported character '\\x01'"),
        ("k: a\x7f\n", "unsupported character '\\x7f'"),
        ("k: a\x85\n", "unsupported character '\\x85'"),
        ("k: 'a\x9fb'\n", "unsupported character '\\x9f'"),
        ("\ufeffversion: 7\n", "unsupported character '\\ufeff'"),  # only parse_pixi_lock strips a leading one
        ('k: "\\ud800"\n', "isn't a Unicode character"),
        ('k: "\\U00110000"\n', "isn't a Unicode character"),
        ('k: "\\q"\n', "unsupported escape"),
        # A space before the colon, which YAML would leave out of the key
        ("a : b\n", "a space before the colon"),
        ("- 6 :\n", "a space before the colon"),
        # Sequence indicators and document markers where a value or a key should be
        ("k: -\n", "unsupported YAML: '-'"),
        ("--- a: b\n", "comments and document markers"),
        ("... a: b\n", "comments and document markers"),
        ("# a comment\n", "comments and document markers"),
        # Structure: lines left over, a first line that's indented, bare and nested items, junk after a quoted key
        ("- a\nb: c\n", "line 2: unexpected indentation"),
        ("  a: 1\nb: 2\n", "line 1: unexpected indentation"),
        ("k:\n-\n", "line 2: unexpected indentation"),
        ("k:\n- - a\n", "nested sequences"),
        ("'a'x: b\n", "expected `key: value`"),
        ("k:\n  a: 1\n    b: 2\n", "line 3: unexpected indentation"),
        ("k: v\n  more\n", "line 2: unexpected indentation"),
        # `k: a: b` is the key k with a value YAML refuses, not the key `k: a`
        ("k: a: b\n", "unsupported YAML: 'a: b'"),
        ("- 'a\n", "must end on its own line"),
        ("[]: x\n", "unsupported key '[]'"),
    ],
)
def test_block_yaml_refuses(text, message):
    with pytest.raises(sources._YamlError) as e:
        _BlockYaml(text).read()
    assert message in str(e.value)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("k: v   \nl: 'w'  \n", {"k": "v", "l": "w"}),  # trailing spaces aren't part of a value
        ("a: 1\r\nb: 2\r\n", {"a": "1", "b": "2"}),
        ("a: 1\rb: 2\r", {"a": "1", "b": "2"}),
        (
            "a:\n- x\n- y: 1\n  z: '2'\nb: {}\nc: []\nd:\n",
            {"a": ["x", {"y": "1", "z": "2"}], "b": {}, "c": [], "d": None},
        ),
        ("a:\n  -x: 1\n", {"a": {"-x": "1"}}),  # "-x" is a key, not a sequence item
        ("a:\n  b:\n  - 1\n  c: 2\n", {"a": {"b": ["1"], "c": "2"}}),
        ("- a\n-   b: 1\n    c: 2\n", ["a", {"b": "1", "c": "2"}]),
        ("url: https://x.org/a#b\n", {"url": "https://x.org/a#b"}),
        ('k: "a\\tb\\u00e9\\x41\\U0001F600"\n', {"k": "a\tb\u00e9A\U0001f600"}),
        ("'a: b': 'it''s'\n", {"a: b": "it's"}),
        ('k: "\\U0010FFFF"\n', {"k": "\U0010ffff"}),  # the last code point is still a character
    ],
)
def test_block_yaml_reads(text, expected):
    assert _BlockYaml(text).read() == expected


def test_pixi_lock_with_a_byte_order_mark(fixtures, tmp_path):
    lock = tmp_path / "pixi.lock"
    lock.write_bytes(b"\xef\xbb\xbf" + (fixtures / "pixiproj" / "pixi.lock").read_bytes())
    assert "fastlib" in by_name(parse_pixi_lock(lock))


def test_pixi_lock_empty_environment_name_is_an_error(fixtures):
    with pytest.raises(SourceError, match="the environment name is empty"):
        parse_pixi_lock(fixtures / "pixiproj" / "pixi.lock", "")
    with pytest.raises(SourceError, match="--environment needs a name"):
        load_inputs([str(fixtures / "pixiproj")], "")


def python(subdir: str, version: str = "3.13.9") -> str:
    return f"conda: https://conda.anaconda.org/conda-forge/{subdir}/python-{version}-h0123456_0_cp313.conda"


def pypi_entry(location: str, name: str, version: str | None = None, *extra: str) -> str:
    return "\n".join(
        [f"- pypi: {location}", f"  name: {name}", *([f"  version: '{version}'"] if version else []), *extra]
    )


def write_pixi(tmp_path, platforms, entries, *, subdirs=None, indexes=("https://pypi.org/simple",)):
    """A small version 7 pixi.lock: `platforms` maps each platform to its selectors, `entries` are PyPI entries."""
    subdirs = subdirs or {}
    lines = ["version: 7", "platforms:"]
    for name in platforms:
        lines += [f"- name: {name}"] + ([f"  subdir: {subdirs[name]}"] if name in subdirs else [])
    lines += ["environments:", "  default:", "    channels:", "    - url: https://conda.anaconda.org/conda-forge/"]
    lines += ["    indexes:", *(f"    - {i}" for i in indexes), "    packages:"]
    for name, selectors in platforms.items():
        lines += [f"      {name}:", *(f"      - {s}" for s in selectors)]
    lines.append("packages:")
    lines += [f"- {c}" for c in sorted({s for ss in platforms.values() for s in ss if s.startswith("conda: ")})]
    lines += entries
    path = tmp_path / "pixi.lock"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


FILES = "https://files.pythonhosted.org/packages/aa"
PURE = (
    f"pypi: {FILES}/purewheel-3.1-py3-none-any.whl",
    pypi_entry(f"{FILES}/purewheel-3.1-py3-none-any.whl", "purewheel", "3.1"),
)
FAST = (f"pypi: {FILES}/fastlib-2.0.0.tar.gz", pypi_entry(f"{FILES}/fastlib-2.0.0.tar.gz", "fastlib", "2.0.0"))


def results(lock, pythons, platforms, name):
    """The statuses of package `name` (by its locked files, no lookup) on those targets."""
    rows = [r for r in check([parse_pixi_lock(lock)], build_targets(pythons, platforms)) if r.package.name == name]
    return [{t.platform: s.value for t, s in r.results.items()} for r in rows]


def test_pixi_platforms_sharing_a_subdir_are_one_group(tmp_path):
    # linux-64-cuda (subdir linux-64) installs fastlib, linux-64 doesn't: pip on linux x86_64 can be either, so
    # fastlib still counts there (cuda-python's cuda_pathfinder locks linux-64-cuda-12 and -13 like this).
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), PURE[0]], "linux-64-cuda": [python("linux-64"), PURE[0], FAST[0]]},
        [PURE[1], FAST[1]],
        subdirs={"linux-64-cuda": "linux-64"},
    )
    assert results(lock, ["3.13"], ["linux-x86_64"], "fastlib") == [{"linux-x86_64": "source"}]


def test_pixi_two_versions_on_platforms_sharing_a_subdir(tmp_path):
    old = f"{FILES}/forked-0.9.tar.gz"
    new = f"{FILES}/forked-1.0.tar.gz"
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), f"pypi: {old}"], "glibc-2-17": [python("linux-64"), f"pypi: {new}"]},
        [pypi_entry(old, "forked", "0.9"), pypi_entry(new, "forked", "1.0")],
        subdirs={"glibc-2-17": "linux-64"},
    )
    # Two packages (the merge key has the version), and neither is hidden on linux x86_64.
    assert results(lock, ["3.13"], ["linux-x86_64"], "forked") == [{"linux-x86_64": "source"}] * 2


def test_pixi_platform_markers(tmp_path):
    # purewheel is locked on osx-arm64 only: n/a on each other locked platform's markers.
    platforms = {s: [python(s)] for s in ("linux-64", "linux-aarch64", "win-64", "osx-64")}
    platforms["osx-arm64"] = [python("osx-arm64"), PURE[0]]
    lock = write_pixi(tmp_path, platforms, [PURE[1]])
    assert results(
        lock, ["3.13"], ["linux-x86_64", "linux-aarch64", "win-amd64", "macos-x86_64", "macos-arm64"], "purewheel"
    ) == [
        {
            "linux-x86_64": "n/a",
            "linux-aarch64": "n/a",
            "win-amd64": "n/a",
            "macos-x86_64": "n/a",
            "macos-arm64": "wheel",
        }
    ]
    # win-arm64 isn't locked, so it counts there
    assert results(lock, ["3.13"], ["win-arm64"], "purewheel") == [{"win-arm64": "wheel"}]


def test_pixi_platform_named_by_its_subdir(tmp_path):
    lock = write_pixi(
        tmp_path,
        {"gpu": [python("linux-64")], "osx-arm64": [python("osx-arm64"), PURE[0]]},
        [PURE[1]],
        subdirs={"gpu": "linux-64"},
    )
    assert results(lock, ["3.13"], ["linux-x86_64"], "purewheel") == [{"linux-x86_64": "n/a"}]


OPENSSL = "conda: https://conda.anaconda.org/conda-forge/win-64/openssl-3.6.0-h725018a_0.conda"


def test_pixi_platform_without_python_hides_nothing(tmp_path):
    # No conda python on win-64: which Python pixi locked for isn't known, so purewheel isn't hidden there.
    lock = write_pixi(tmp_path, {"linux-64": [python("linux-64"), PURE[0]], "win-64": [OPENSSL]}, [PURE[1]])
    assert results(lock, ["3.13"], ["win-amd64"], "purewheel") == [{"win-amd64": "wheel"}]


def test_pixi_python_is_the_python_package_only(tmp_path):
    dateutil = "conda: https://conda.anaconda.org/conda-forge/noarch/python-dateutil-2.9.0-pyhd8ed1ab_1.conda"
    abi = "conda: https://conda.anaconda.org/conda-forge/noarch/python_abi-3.14-8_cp314.conda"
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), dateutil, abi], "osx-arm64": [python("osx-arm64"), PURE[0]]},
        [PURE[1]],
    )
    assert results(lock, ["3.13"], ["linux-x86_64"], "purewheel") == [{"linux-x86_64": "n/a"}]
    assert results(lock, ["3.14"], ["linux-x86_64"], "purewheel") == [{"linux-x86_64": "wheel"}]


def test_pixi_one_release_from_two_indexes_is_skipped(tmp_path):
    private = "https://private.example.com/files/fastlib-2.0.0.tar.gz"
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), FAST[0]], "osx-arm64": [python("osx-arm64"), f"pypi: {private}"]},
        [FAST[1], pypi_entry(private, "fastlib", "2.0.0", "  index: https://private.example.com/simple")],
    )
    (pkg,) = [p for p in parse_pixi_lock(lock).packages if p.name == "fastlib"]
    assert (pkg.skip, pkg.files) == ("private index", None)  # neither index may be asked for the other's package


@pytest.mark.parametrize(
    "host,indexes,skip",
    [
        ("https://files.pythonhosted.org/packages/aa", ["https://private.example.com/simple"], None),
        # Only PyPI's own file host counts as PyPI
        ("https://files.pythonhosted.org.example.com/aa", ["https://private.example.com/simple"], "private index"),
        ("https://mirror-files.pythonhosted.org/aa", ["https://private.example.com/simple"], "private index"),
        # With PyPI the only index, a file elsewhere is a direct URL; with another index too, it may be internal
        ("https://example.com/aa", ["https://pypi.org/simple"], "url"),
        ("https://example.com/aa", ["https://pypi.org/simple", "https://private.example.com/simple"], "private index"),
        ("https://example.com/aa", ["https://private.example.com/simple", "https://pypi.org/simple/"], "private index"),
    ],
)
def test_pixi_where_a_package_came_from(tmp_path, host, indexes, skip):
    location = f"{host}/fastlib-2.0.0.tar.gz"
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), f"pypi: {location}"]},
        [pypi_entry(location, "fastlib", "2.0.0")],
        indexes=indexes,
    )
    (pkg,) = [p for p in parse_pixi_lock(lock).packages if p.name == "fastlib"]
    assert pkg.skip == skip


def test_pixi_git_revisions_stay_apart(tmp_path):
    a = "git+https://github.com/example/gitdep.git#aaaa"
    b = "git+https://github.com/example/gitdep.git#bbbb"
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64"), f"pypi: {a}"], "osx-arm64": [python("osx-arm64"), f"pypi: {b}"]},
        [pypi_entry(a, "gitdep"), pypi_entry(b, "gitdep")],
    )
    assert [(p.name, p.skip) for p in parse_pixi_lock(lock).packages if p.name == "gitdep"] == [("gitdep", "git")] * 2


def test_pixi_conda_packages_are_skipped_once_by_name(tmp_path):
    cffi = "conda: https://conda.anaconda.org/conda-forge/{}/cffi-{}-py313h0_0.conda"
    lock = write_pixi(
        tmp_path,
        {
            "linux-64": [python("linux-64"), cffi.format("linux-64", "1.17.1")],
            "osx-arm64": [python("osx-arm64"), cffi.format("osx-arm64", "2.0.0")],
        },
        [],
    )
    assert [(p.name, p.skip) for p in parse_pixi_lock(lock).packages] == [("cffi", "conda"), ("python", "conda")]


def test_find_lockfile_order_with_pixi_lock(fixtures, tmp_path):
    (tmp_path / "pixi.lock").write_text(
        (fixtures / "pixiproj" / "pixi.lock").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    assert find_lockfile(tmp_path).name == "requirements.txt"  # pixi.lock comes last
    assert find_lockfile(tmp_path, pixi=True).name == "pixi.lock"
    (source,) = load_inputs([str(tmp_path)])
    assert (source.kind, source.passed_over) == ("requirements", tmp_path / "pixi.lock")
    (source,) = load_inputs([str(tmp_path)], "test")
    assert (source.kind, source.passed_over) == ("pixi", None)


def test_pixi_platform_with_unknown_python_vouches_for_its_packages(tmp_path):
    # linux-64-cuda builds its Python from source, so its version isn't known: it may be 3.12 like linux-64, and it
    # installs psycopg2, so psycopg2 isn't hidden on linux x86_64 for 3.12.
    psycopg2 = f"{FILES}/psycopg2-2.9.10.tar.gz"
    lock = write_pixi(
        tmp_path,
        {
            "linux-64": [python("linux-64", "3.12.3")],
            "linux-64-cuda": ["conda_source: python[abcd1234] @ ./python", f"pypi: {psycopg2}"],
            "win-64": [python("win-64", "3.12.3")],
        },
        ["- conda_source: python[abcd1234] @ ./python", pypi_entry(psycopg2, "psycopg2", "2.9.10")],
        subdirs={"linux-64-cuda": "linux-64"},
    )
    assert results(lock, ["3.12"], ["linux-x86_64", "win-amd64"], "psycopg2") == [
        {"linux-x86_64": "source", "win-amd64": "n/a"}
    ]


def test_pixi_platforms_sharing_a_subdir_with_different_pythons(tmp_path):
    lock = write_pixi(
        tmp_path,
        {"linux-64": [python("linux-64", "3.12.3"), PURE[0]], "linux-64-py313": [python("linux-64")]},
        [PURE[1]],
        subdirs={"linux-64-py313": "linux-64"},
    )
    # Two groups: purewheel is installed for 3.12 and not for 3.13 on linux x86_64.
    rows = check([parse_pixi_lock(lock)], build_targets(["3.12", "3.13"], ["linux-x86_64"]))
    assert [(t.python, s.value) for r in rows for t, s in r.results.items() if r.package.name == "purewheel"] == [
        ("3.12", "wheel"),
        ("3.13", "n/a"),
    ]


def test_pixi_lock_that_isnt_utf8(tmp_path):
    lock = tmp_path / "pixi.lock"
    lock.write_bytes(b"version: 7\nplatforms:\n- name: linux-\xff64\n")
    with pytest.raises(SourceError, match=r"^pixi.lock: not valid UTF-8 \(byte 36\)"):
        parse_pixi_lock(lock)


@pytest.mark.parametrize(
    "lines,expected",
    [
        ("--no-binary :all:\nfastlib==2.0.0\npurewheel==3.1\n", {"fastlib": (True, False), "purewheel": (True, False)}),
        ("--no-binary=:all:\nfastlib==2.0.0\n", {"fastlib": (True, False)}),
        ("--only-binary :all:\nfastlib==2.0.0\n", {"fastlib": (False, True)}),
        ("--only-binary=:all:\nfastlib==2.0.0\n", {"fastlib": (False, True)}),
        (
            "--no-binary my_pkg,purewheel\nmy-pkg==1.0\npurewheel==3.1\nfastlib==2.0.0\n",
            {"my-pkg": (True, False), "purewheel": (True, False), "fastlib": (False, False)},
        ),
        (
            "--no-binary :all:\n--only-binary purewheel\n--no-binary :none:\nfastlib==2.0.0\npurewheel==3.1\n",
            {"fastlib": (False, False), "purewheel": (False, True)},
        ),
        (
            "--only-binary fastlib\n--no-binary fastlib\n--no-binary :none:\nfastlib==2.0.0\n",
            {"fastlib": (False, False)},
        ),
        (
            "--no-binary :all: --only-binary purewheel\nfastlib==2.0.0\npurewheel==3.1\n",
            {"fastlib": (True, False), "purewheel": (False, True)},
        ),
        (
            "--no-binary :none:,fastlib\nfastlib==2.0.0\npurewheel==3.1\n",
            {"fastlib": (True, False), "purewheel": (False, False)},
        ),
        (
            "--no-binary fastlib,:none:\nfastlib==2.0.0\n",
            {"fastlib": (False, False)},
        ),
        (
            "--no-binary :all:,:none:\nfastlib==2.0.0\n",
            {"fastlib": (False, False)},
        ),
        (
            (
                "--no-index --no-binary fastlib\n"
                "--index-url https://example.com/simple --only-binary purewheel\n"
                "fastlib==2.0.0\npurewheel==3.1\n"
            ),
            {"fastlib": (True, False), "purewheel": (False, True)},
        ),
        (
            '--no-binary "fastlib, :none:"\nfastlib==2.0.0\n',
            {"fastlib": (True, False)},
        ),
        (
            '--no-binary "fastlib, purewheel"\nfastlib==2.0.0\npurewheel==3.1\n',
            {"fastlib": (True, False), "purewheel": (False, False)},
        ),
        (
            '--only-binary="fastlib,purewheel"\nfastlib==2.0.0\npurewheel==3.1\n',
            {"fastlib": (False, True), "purewheel": (False, True)},
        ),
        (
            "--no-binary 'fastlib,:none:'\nfastlib==2.0.0\n",
            {"fastlib": (False, False)},
        ),
        (
            '--no-binary "fastlib,\\\npurewheel"\npurewheel==3.1\n',
            {"purewheel": (True, False)},
        ),
        (
            '--only-binary "fastlib,\\\nnowheels"\nnowheels==0.9\n',
            {"nowheels": (False, True)},
        ),
        (
            "--no-binary fastlib,\\\npurewheel\npurewheel==3.1\n",
            {"purewheel": (True, False)},
        ),
        (
            "--only-binary fastlib,\\\nnowheels\nnowheels==0.9\n",
            {"nowheels": (False, True)},
        ),
        (
            '--no-binary "fastlib, \\\npurewheel"\nfastlib==2.0.0\npurewheel==3.1\n',
            {"fastlib": (True, False), "purewheel": (False, False)},
        ),
        (
            "# comment ending with \\\npurewheel==3.1\n",
            {"purewheel": (False, False)},
        ),
        (
            '--no-binary "fastlib,\\\\\npurewheel"\npurewheel==3.1\n',
            {"purewheel": (True, False)},
        ),
        (
            '--only-binary "fastlib,\\\\\nnowheels"\nnowheels==0.9\n',
            {"nowheels": (False, True)},
        ),
        (
            '--no-binary "fastlib,\\\\\r\npurewheel"\r\npurewheel==3.1\r\n',
            {"purewheel": (True, False)},
        ),
        (
            '--only-binary "fastlib,\\\\\r\nnowheels"\r\nnowheels==0.9\r\n',
            {"nowheels": (False, True)},
        ),
    ],
)
def test_requirements_binary_format_control(tmp_path, lines, expected):
    req = tmp_path / "requirements.txt"
    req.write_bytes(lines.encode())  # as written: write_text would turn \r\n into \r\r\n on Windows
    pkgs = by_name(parse_requirements(req))
    assert {name: (pkgs[name].no_binary, pkgs[name].only_binary) for name in expected} == expected


@pytest.mark.parametrize(
    "lines, expected",
    [
        # Each row is what pip 26.3 does (pip install --dry-run -r FILE): the option counts for the whole install,
        # like the same option on a line of its own, wherever the requirement that carries it is.
        ("fastlib>=2 --no-binary fastlib\n", {"fastlib": (True, False)}),
        (
            "fastlib>=2 --no-binary fastlib,purewheel\npurewheel==3.1\n",
            {"fastlib": (True, False), "purewheel": (True, False)},
        ),
        ("purewheel==3.1\nfastlib==2.0.0 --no-binary :all:\n", {"fastlib": (True, False), "purewheel": (True, False)}),
        (
            "fastlib==2.0.0 --no-binary purewheel\npurewheel==3.1\n",
            {"fastlib": (False, False), "purewheel": (True, False)},
        ),
        ("fastlib==2.0.0 --only-binary=fastlib\n", {"fastlib": (False, True)}),
        ("fastlib==2.0.0 --no-binary=fastlib --only-binary=fastlib\n", {"fastlib": (False, True)}),
        ("--only-binary :all:\nfastlib==2.0.0 --no-binary fastlib\n", {"fastlib": (True, False)}),
        ("fastlib==2.0.0 ; python_version >= '3.8' --no-binary fastlib\n", {"fastlib": (True, False)}),
        ("fastlib==2.0.0 --hash=sha256:aa --no-binary fastlib --hash sha256:bb\n", {"fastlib": (True, False)}),
        ("fastlib==2.0.0 \\\n    --no-binary fastlib\n", {"fastlib": (True, False)}),
        ("fastlib==2.0.0 --config-settings a=b -C c=d -Ce=f --config-setting=g=h\n", {"fastlib": (False, False)}),
    ],
)
def test_requirements_format_options_after_a_requirement(tmp_path, lines, expected):
    req = tmp_path / "requirements.txt"
    req.write_text(lines, encoding="utf-8")
    source = parse_requirements(req)
    pkgs = by_name(source)
    assert {name: (pkgs[name].no_binary, pkgs[name].only_binary) for name in expected} == expected
    assert all(not p.skip and not p.error for p in source.packages)
    assert all(" -" not in spec for spec in source.specs)  # --deps resolves the requirement, not its options


def test_requirements_options_after_a_requirement_keep_its_pin_and_hash_mode(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastlib==2.0.0 --no-binary fastlib --hash=sha256:aa\npurewheel>=3 --only-binary purewheel\n")
    source = parse_requirements(req)
    assert [(p.name, p.version, p.spec) for p in source.packages] == [
        ("fastlib", "2.0.0", None),
        ("purewheel", None, ">=3"),
    ]
    assert source.compiled and source.specs == ["fastlib==2.0.0", "purewheel>=3"]
    assert source.packages[0].hashes == frozenset({"aa"})
    assert source.packages[1].hashes == frozenset()


def test_requirements_hashes_preserved_for_both_spellings_and_continuation(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "pkg_eq==1.0 --hash=sha256:1111\n"
        "pkg_space==2.0 --hash sha256:2222\n"
        "pkg_continued==3.0 \\\n"
        "    --hash=sha256:3333 \\\n"
        "    --hash sha256:4444\n"
    )
    source = parse_requirements(req)
    assert [(p.name, p.version, p.hashes) for p in source.packages] == [
        ("pkg_eq", "1.0", frozenset({"1111"})),
        ("pkg_space", "2.0", frozenset({"2222"})),
        ("pkg_continued", "3.0", frozenset({"3333", "4444"})),
    ]


def test_requirements_hashes_keep_sha256_only_lowercased(tmp_path):
    # pip lowercases the hashes it's given, and only a sha256 can match a file on PyPI, which gives no other digest
    # (pip 26.3 takes the sdist for `--hash=sha256:<sdist> --hash=sha512:<wheel>`). A line with only sha384/sha512
    # hashes is read as without --hash: pip checks its pick after the download, which wheelgap can't do.
    req = tmp_path / "requirements.txt"
    req.write_text(
        f"upper==1.0 --hash=sha256:{'AB' * 32}\n"
        f"other==1.0 --hash=sha512:{'cd' * 64} --hash=sha384:{'cd' * 48}\n"
        f"mixed==1.0 --hash=sha512:{'ef' * 64} --hash=sha256:{'12' * 32}\n"
    )
    assert [p.hashes for p in parse_requirements(req).packages] == [
        frozenset({"ab" * 32}),
        frozenset(),
        frozenset({"12" * 32}),
    ]


@pytest.mark.parametrize(
    "line, ignored",
    [
        # Each row is what pip 26.3 does (pip install --dry-run -r FILE): it parses the option and drops it, so the
        # requirement is installed from the same index as without it, at the same version.
        ("fastlib==2.0.0 -f https://example.invalid/wheels.html", ["-f"]),
        ("fastlib==2.0.0 -fhttps://example.invalid/wheels.html", ["-f"]),
        ("fastlib==2.0.0 --find-links ./wheels", ["--find-links"]),
        ("fastlib==2.0.0 --find-links=./wheels", ["--find-links"]),
        ("fastlib==2.0.0 --find ./wheels", ["--find-links"]),  # optparse takes an unambiguous prefix
        ("fastlib==2.0.0 \\\n    -f ./wheels", ["-f"]),
        ("fastlib==2.0.0 -i https://example.invalid/simple", ["-i"]),
        ("fastlib==2.0.0 --index-url=https://example.invalid/simple", ["--index-url"]),
        ("fastlib==2.0.0 --pypi-url https://example.invalid/simple", ["--pypi-url"]),
        ("fastlib==2.0.0 --extra-index-url https://example.invalid/simple", ["--extra-index-url"]),
        ("fastlib==2.0.0 --no-index", ["--no-index"]),
        ("fastlib==2.0.0 --pre", ["--pre"]),
        ("fastlib==2.0.0 --prefer-binary", ["--prefer-binary"]),
        ("fastlib==2.0.0 -r more.txt", ["-r"]),  # never read, so a missing file is no error either
        ("fastlib==2.0.0 --requirement=more.txt", ["--requirement"]),
        ("fastlib==2.0.0 -c cons.txt", ["-c"]),
        ("fastlib==2.0.0 --trusted-host example.invalid", ["--trusted-host"]),
        ("fastlib==2.0.0 --require-hashes", ["--require-hashes"]),
        ("fastlib==2.0.0 --no-require-hashes", ["--no-require-hashes"]),
        ("fastlib==2.0.0 -f -r", ["-f"]),  # -r is -f's value: optparse takes the next word, whatever it is
        ("fastlib==2.0.0 --hash=sha256:aa stray", ["words that aren't options"]),
        ("fastlib==2.0.0 - -- -f x", ["words that aren't options"]),
        ("fastlib==2.0.0 -f x --pre -f y --no-binary :none:", ["-f", "--pre"]),
        # pip turns the feature on for the run, and no feature changes which file it picks: no note.
        ("fastlib==2.0.0 --use-feature fast-deps", []),
    ],
)
def test_requirements_options_pip_ignores_after_a_requirement(tmp_path, line, ignored):
    req = tmp_path / "requirements.txt"
    req.write_text(f"purewheel==3.1\n{line}\n", encoding="utf-8")
    source = parse_requirements(req)
    assert [(p.name, p.version, p.skip, p.no_binary, p.only_binary) for p in source.packages] == [
        ("purewheel", "3.1", None, False, False),
        ("fastlib", "2.0.0", None, False, False),
    ]
    assert source.specs == ["purewheel==3.1", "fastlib==2.0.0"]
    assert source.ignored_options == [
        f"requirements.txt:2: pip ignores {option} after a requirement" for option in ignored
    ]


def test_requirements_format_option_is_read_only_where_pip_reads_it(tmp_path):
    # pip 26.3: `-f --only-binary` makes "--only-binary" -f's value and sets "nowheels" aside, so only fastlib is
    # binary-only; a -C value that looks like an option is a config setting.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "fastlib==2.0.0 --only-binary fastlib -f --only-binary nowheels\n"
        "purewheel==3.1 -C --no-binary=purewheel\nnowheels==0.9\n",
        encoding="utf-8",
    )
    source = parse_requirements(req)
    assert [(p.name, p.no_binary, p.only_binary) for p in source.packages] == [
        ("fastlib", False, True),
        ("purewheel", False, False),
        ("nowheels", False, False),
    ]


def test_requirements_constraints_after_a_requirement_are_not_read(tmp_path):
    # pip 26.3 installs the newest release for `fastlib -c cons.txt` and never opens cons.txt.
    (tmp_path / "cons.txt").write_text("fastlib==1.0\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("fastlib -c cons.txt\npurewheel -r more.txt\n", encoding="utf-8")
    source = parse_requirements(req)
    assert [(p.name, p.version, p.spec) for p in source.packages] == [
        ("fastlib", None, None),
        ("purewheel", None, None),
    ]
    assert not source.constraint_specs
    assert source.ignored_options == [
        "requirements.txt:1: pip ignores -c after a requirement",
        "requirements.txt:2: pip ignores -r after a requirement",
    ]


def test_requirements_ignored_option_note_is_once_per_file_and_option(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "base.txt").write_text("purewheel==3.1 -f ./w\nnowheels==0.9 -f ./w --pre\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(
        "fastlib==2.0.0 -f ./w\n-r sub/base.txt\nfastlib==2.0.0 --find-links ./w\n-r sub/base.txt\n", encoding="utf-8"
    )
    assert parse_requirements(req).ignored_options == [
        "requirements.txt:1: pip ignores -f after a requirement",
        "sub/base.txt:1: pip ignores -f after a requirement",
        "sub/base.txt:2: pip ignores --pre after a requirement",
    ]


def test_conda_env_pip_list_ignored_option_note(tmp_path):
    env = tmp_path / "environment.yml"
    env.write_text("dependencies:\n  - pip:\n    - purewheel==3.1 -f ./wheels\n", encoding="utf-8")
    source = sources.load_file(env)
    assert [(p.name, p.version) for p in source.packages] == [("purewheel", "3.1")]
    assert source.ignored_options == ["environment.yml: pip ignores -f after a requirement"]


def _release_control(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    source = parse_requirements(tmp_path / "requirements.txt")
    return source, {p.key: p.prereleases for p in source.packages}


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # pip 26.3 applies --pre to the whole install, wherever its line is: before or after the requirement, in an
        # included -r file or a -c file (#634). It never changes a package with no release control.
        ({"requirements.txt": "fastlib>=2\n"}, {"fastlib": None}),
        ({"requirements.txt": "--pre\nfastlib>=2\n"}, {"fastlib": True}),
        ({"requirements.txt": "fastlib>=2\n--pre\n"}, {"fastlib": True}),
        ({"requirements.txt": "--trusted-host x --pre\nfastlib>=2\n"}, {"fastlib": True}),  # after another option
        ({"requirements.txt": "-r base.txt\nfastlib>=2\n", "base.txt": "--pre\n"}, {"fastlib": True}),
        ({"requirements.txt": "-c c.txt\nfastlib>=2\n", "c.txt": "--pre\n"}, {"fastlib": True}),
        # On a line with -r or -c, pip reads the file and never applies --pre (handle_option_line isn't called for
        # it): `--pre -r inc.txt` installs demo 1.0, not 2.0rc1.
        ({"requirements.txt": "--pre -r base.txt\n", "base.txt": "fastlib>=2\n"}, {"fastlib": None}),
        ({"requirements.txt": "-r base.txt --pre\n", "base.txt": "fastlib>=2\n"}, {"fastlib": None}),
        # After a requirement pip ignores --pre too (and wheelgap notes that).
        ({"requirements.txt": "fastlib>=2 --pre\n"}, {"fastlib": None}),
        # --all-releases and --only-final count on any line pip parses, a -r line and a requirement's too: their
        # parser callbacks set them. A package named wins over :all:, in either set.
        ({"requirements.txt": "--all-releases fastlib -r base.txt\n", "base.txt": "fastlib>=2\n"}, {"fastlib": True}),
        ({"requirements.txt": "fastlib>=2 --all-releases fastlib\nranged\n"}, {"fastlib": True, "ranged": None}),
        ({"requirements.txt": "--all :all:\nfastlib>=2\n"}, {"fastlib": True}),  # an abbreviation
        ({"requirements.txt": "--only-final :all:\nfastlib>=2\n"}, {"fastlib": False}),
        ({"requirements.txt": "--only-final fastlib\n--pre\nfastlib>=2\nranged\n"}, {"fastlib": False, "ranged": True}),
        ({"requirements.txt": "--pre\n--only-final fastlib\nfastlib\n"}, {"fastlib": False}),
        ({"requirements.txt": "--all-releases :all:,fastlib\n--only-final fastlib\nfastlib\n"}, {"fastlib": False}),
        (
            {"requirements.txt": "--only-final :all:,:none:,ranged\nranged\nfastlib\n"},
            {"ranged": False, "fastlib": None},
        ),
        ({"requirements.txt": "--only-final :all:\n--all-releases :all:\nfastlib\n"}, {"fastlib": True}),
    ],
)
def test_requirements_release_control_as_pip_reads_it(tmp_path, files, expected):
    # Each case's answer is what pip 26.3 installs for the same lines (pip install --dry-run against local wheels).
    _, prereleases = _release_control(tmp_path, files)
    assert prereleases == expected


@pytest.mark.parametrize(
    ("files", "where"),
    [
        ({"requirements.txt": "--prefer-binary\nfastlib>=2\n"}, "requirements.txt:1"),
        ({"requirements.txt": "fastlib>=2\n--pref\n"}, "requirements.txt:2"),
        ({"requirements.txt": "-r base.txt\n", "base.txt": "fastlib>=2\n--prefer-binary\n"}, "base.txt:2"),
        # pip ignores it on a -r line and after a requirement.
        ({"requirements.txt": "--prefer-binary -r base.txt\n", "base.txt": "fastlib>=2\n"}, None),
        ({"requirements.txt": "fastlib>=2 --prefer-binary\n"}, None),
    ],
)
def test_requirements_prefer_binary_is_recorded(tmp_path, files, where):
    source, _ = _release_control(tmp_path, files)
    assert source.format_control.prefer_binary == where


@pytest.mark.parametrize(
    ("line", "message"),
    [
        # pip 26.3 refuses both lines: "--all-releases option requires 1 argument", and from ReleaseControl,
        # "--all-releases / --only-final option requires 1 argument."
        ("--only-final", "requirements.txt:1: pip refuses this line: --only-final option requires 1 argument"),
        ("--only-final --pre", "requirements.txt:1: pip refuses this line: --all-releases / --only-final option"),
    ],
)
def test_requirements_release_option_line_pip_refuses(tmp_path, line, message):
    with pytest.raises(SourceError, match=re.escape(message)):
        _release_control(tmp_path, {"requirements.txt": f"{line}\nfastlib>=2\n"})


def test_requirements_only_final_refuses_a_pinned_pre_release(tmp_path):
    # pip 26.3 with `--only-final :all:` and `demo==2.0rc1`: "Could not find a final version that satisfies".
    source, _ = _release_control(tmp_path, {"requirements.txt": "--only-final :all:\nprelib==2.0rc1\nfastlib==2.0.0\n"})
    errors = {p.key: p.error for p in source.packages}
    assert errors["prelib"] == (
        "pip installs only final releases of prelib here (--only-final), so it finds no 2.0rc1 (pip refuses this)"
    )
    assert errors["fastlib"] is None


@pytest.mark.parametrize(
    ("files", "blocker"),
    [
        ({"requirements.txt": "-r base.txt\n", "base.txt": "--pre\nfastlib>=2\n"}, "base.txt:1 sets --pre"),
        ({"requirements.txt": "fastlib>=2 --only-final fastlib\n"}, "requirements.txt:1 sets --only-final"),
        ({"requirements.txt": "--prefer-binary\nfastlib>=2\n"}, "requirements.txt:1 sets --prefer-binary"),
    ],
)
def test_requirements_release_options_block_deps(tmp_path, files, blocker):
    source, _ = _release_control(tmp_path, files)
    assert source.deps_blockers == [f"{blocker}, which changes the releases pip picks for dependencies"]


@pytest.mark.parametrize(
    "line, message",
    [
        # pip 26.3 refuses these ("Invalid requirement: ..." and optparse's own words).
        ("fastlib>=2 --bogus-option x", "requirements.txt:2: pip refuses this line: no such option: --bogus-option"),
        ("fastlib>=2 --global-option=x", "requirements.txt:2: pip refuses this line: no such option: --global-option"),
        ("fastlib>=2 --src x", "requirements.txt:2: pip refuses this line: no such option: --src"),
        ("fastlib>=2 -x", "requirements.txt:2: pip refuses this line: no such option: -x"),
        (
            "fastlib>=2 --no x",
            "requirements.txt:2: pip refuses this line: ambiguous option: --no (--no-binary, --no-index, "
            "--no-require-hashes?)",
        ),
        (
            "fastlib>=2 --pr",
            "requirements.txt:2: pip refuses this line: ambiguous option: --pr (--pre, --prefer-binary?)",
        ),
        ("fastlib>=2 --no-binary", "requirements.txt:2: pip refuses this line: --no-binary option requires 1 argument"),
        ("fastlib>=2 --hash", "requirements.txt:2: pip refuses this line: --hash option requires 1 argument"),
        ("fastlib>=2 -f", "requirements.txt:2: pip refuses this line: -f option requires 1 argument"),
        ("fastlib>=2 --trusted-host", "requirements.txt:2: pip refuses this line: --trusted-host option requires 1"),
        ("fastlib>=2 --pre=yes", "requirements.txt:2: pip refuses this line: --pre option does not take a value"),
        (
            "fastlib>=2 --use-feature nope",
            "requirements.txt:2: pip refuses this line: option --use-feature: invalid choice: 'nope'",
        ),
        ("fastlib>=2 --no-binary 'fastlib", "requirements.txt:2: pip can't split this line (an unclosed quote?)"),
        # pip's parser callbacks refuse these values (pip 26.3 ran).
        ("fastlib>=2 --no-binary --pre", "requirements.txt:2: pip refuses this line: --no-binary / --only-binary"),
        ("fastlib>=2 --hash=abc", "requirements.txt:2: pip refuses this line: Arguments to --hash must be a hash"),
        ("fastlib>=2 --hash=md5:abc", "requirements.txt:2: pip refuses this line: Allowed hash algorithms for --hash"),
        ("fastlib>=2 -C foo", "requirements.txt:2: pip refuses this line: Arguments to -C must be of the form KEY=VAL"),
        # pip makes the requirement itself editable, and refuses a name: "fastlib>=2 is not a valid editable
        # requirement".
        ("fastlib>=2 -e .", "requirements.txt:2: pip reads -e after a requirement as making that requirement"),
        (
            "fastlib>=2 --editable=.",
            "requirements.txt:2: pip reads --editable after a requirement as making that requirement",
        ),
        # pip's ReleaseControl callback refuses a value that starts with "-" (pip 26.3 ran).
        ("fastlib>=2 --only-final --pre", "requirements.txt:2: pip refuses this line: --all-releases / --only-final"),
        ("fastlib>=2 --all-releases", "requirements.txt:2: pip refuses this line: --all-releases option requires 1"),
    ],
)
def test_requirements_other_options_after_a_requirement_are_refused(tmp_path, line, message):
    req = tmp_path / "requirements.txt"
    req.write_text(f"purewheel==3.1\n{line}\n", encoding="utf-8")
    with pytest.raises(SourceError, match=re.escape(message)):
        parse_requirements(req)


@pytest.mark.parametrize(
    "top, message",
    [
        ("purewheel==3.1\n-r missing.txt\n", "requirements.txt:2: -r missing.txt: file not found"),
        ("--requirement=missing.txt\n", "requirements.txt:1: --requirement=missing.txt: file not found"),
        ("# pins\n\n-c missing.txt\npurewheel\n", "requirements.txt:3: -c missing.txt: file not found"),
        (
            "purewheel==3.1 \\\n  --hash=sha256:aa\n-r sub/missing.txt\n",
            "requirements.txt:3: -r sub/missing.txt: file not found",
        ),
        ("-r sub/next.txt\n", "sub/next.txt:2: -r gone.txt: file not found"),  # the file that has the line
        ("-c sub/cons.txt\n", "sub/cons.txt:1: -r gone.txt: file not found"),
        ("-r sub\n", "requirements.txt:1: -r sub: "),  # a folder: the system's own words (pip: "Is a directory")
    ],
)
def test_requirements_include_that_cannot_be_read_names_the_file_and_line(tmp_path, top, message):
    # pip stops too ("Could not open requirements file"); before, this was a bare "[Errno 2] No such file ...".
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "next.txt").write_text("purewheel==3.1\n-r gone.txt\n", encoding="utf-8")
    (tmp_path / "sub" / "cons.txt").write_text("-r gone.txt\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(top, encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        parse_requirements(req)
    assert str(caught.value).startswith(message) and "Errno" not in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    if "file not found" in message:
        assert str(caught.value) == message


def test_requirements_binary_format_control_with_includes(tmp_path):
    (tmp_path / "base.txt").write_text("--no-binary :all:\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / "override.txt").write_text("--only-binary purewheel\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("-r base.txt\nfastlib==2.0.0\n-r override.txt\n", encoding="utf-8")
    pkgs = by_name(parse_requirements(req))
    assert (pkgs["fastlib"].no_binary, pkgs["fastlib"].only_binary) == (True, False)
    assert (pkgs["purewheel"].no_binary, pkgs["purewheel"].only_binary) == (False, True)


def test_requirements_binary_format_control_reapplied_on_repeated_includes(tmp_path):
    (tmp_path / "options.txt").write_text("--no-binary :all:\npurewheel==3.1\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("--only-binary nowheels\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(
        "-r options.txt\n--no-binary :none:\n-r options.txt\n"
        "-c constraints.txt\n--only-binary :none:\n-c constraints.txt\n"
        "fastlib==2.0.0\nnowheels==0.9\n",
        encoding="utf-8",
    )
    pkgs = parse_requirements(req).packages
    assert [p.name for p in pkgs] == ["purewheel", "fastlib", "nowheels"]
    by = {p.name: (p.no_binary, p.only_binary) for p in pkgs}
    assert by == {
        "purewheel": (True, False),
        "fastlib": (True, False),
        "nowheels": (False, True),
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("--no-b :all:", (True, False)),
        ("--no-bin=:all:", (True, False)),
        ("--only-b :all:", (False, True)),
        ("--o=:all:", (False, True)),
        ("--no- :all:", (False, False)),  # --no-index too: pip refuses it
        ("--only-final :all:", (False, False)),  # not --only-binary (a bare --only-final is refused, as pip does)
    ],
)
def test_requirements_binary_options_abbreviated(tmp_path, line, expected):
    # optparse, which reads pip's requirements files, takes an unambiguous prefix of a long option
    req = tmp_path / "requirements.txt"
    req.write_text(f"{line}\nsix==1.16.0\n", encoding="utf-8")
    (six,) = parse_requirements(req).packages
    assert (six.no_binary, six.only_binary) == expected


_UV_BUILD_POLICY_LOCK = """\
version = 1
[manifest]
requirements = [{ name = "fastlib" }, { name = "puresdist" }]
[[package]]
name = "fastlib"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" }]
sdist = { url = "https://files.example/fastlib-2.0.0.tar.gz" }
[[package]]
name = "puresdist"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/puresdist-1.0.tar.gz" }
"""


@pytest.mark.parametrize(
    "setting, expected",
    [
        ("no-build = true", {"fastlib": (False, True), "puresdist": (False, True)}),
        ("no-binary = true", {"fastlib": (True, False), "puresdist": (True, False)}),
        ('no-build-package = ["PureSDIST"]', {"fastlib": (False, False), "puresdist": (False, True)}),
        ('no-binary-package = ["FASTLIB"]', {"fastlib": (True, False), "puresdist": (False, False)}),
        (
            'no-build-package = ["fastlib"]\nno-binary-package = ["fastlib"]',
            {"fastlib": (True, True), "puresdist": (False, False)},
        ),
    ],
)
def test_uv_lock_build_policy_settings(tmp_path, setting, expected):
    (tmp_path / "uv.lock").write_text(_UV_BUILD_POLICY_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting + "\n", encoding="utf-8")
    (source,) = load_inputs([str(tmp_path)])
    packages = by_name(source)
    assert {name: (p.no_binary, p.only_binary) for name, p in packages.items()} == expected


def test_uv_toml_build_policy_takes_precedence_over_pyproject(tmp_path):
    (tmp_path / "uv.lock").write_text(_UV_BUILD_POLICY_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\nno-binary = true\n", encoding="utf-8")
    (tmp_path / "uv.toml").write_text('no-build-package = ["puresdist"]\n', encoding="utf-8")
    (source,) = load_inputs([str(tmp_path)])
    packages = by_name(source)
    assert packages["fastlib"].no_binary is False
    assert packages["puresdist"].only_binary is True


@pytest.mark.parametrize(
    "setting, key",
    [
        ("no-build = 1", "no-build"),
        ('no-binary = "yes"', "no-binary"),
        ('no-build-package = ["fastlib", 1]', "no-build-package"),
        ("no-binary-package = true", "no-binary-package"),
    ],
)
def test_uv_build_policy_rejects_wrong_types(tmp_path, setting, key):
    (tmp_path / "uv.lock").write_text(_UV_BUILD_POLICY_LOCK, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n" + setting + "\n", encoding="utf-8")
    with pytest.raises(SourceError, match=key):
        load_inputs([str(tmp_path)])


def test_uv_build_policy_rejects_a_tool_key_that_isnt_a_table(tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n[[package]]\nname = "a"\nversion = "1.0"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('tool = "uv"\n', encoding="utf-8")
    with pytest.raises(SourceError, match=r"\[tool\] must be a table"):
        load_inputs([str(tmp_path)])


def test_requirements_option_after_a_requirement_counts_each_time_the_file_is_read(tmp_path):
    # pip 26.3 reads b.txt twice, so its --no-binary comes after the --only-binary line: pip takes the sdist.
    (tmp_path / "b.txt").write_text("fastlib==2.0.0 --no-binary fastlib\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-r b.txt\n--only-binary fastlib\n-r b.txt\n", encoding="utf-8")
    pkgs = by_name(parse_requirements(tmp_path / "requirements.txt"))
    assert (pkgs["fastlib"].no_binary, pkgs["fastlib"].only_binary) == (True, False)


def test_requirements_tab_before_an_option_is_one_requirement_as_in_pip(tmp_path):
    # pip splits at single spaces: "fastlib==2.0.0<TAB>--no-binary" is one word, and an invalid requirement.
    req = tmp_path / "requirements.txt"
    req.write_text("fastlib==2.0.0\t--no-binary fastlib\n", encoding="utf-8")
    with pytest.raises(SourceError, match="can't parse requirement"):
        parse_requirements(req)
