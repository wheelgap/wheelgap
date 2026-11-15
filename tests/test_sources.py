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
    (directory / "constraints.txt").write_text("-c nested.txt\npinned==1\n", encoding="utf-8")
    (directory / "nested.txt").write_text(
        "ranged<2 \\\n    --hash=sha256:00  # note\n-c constraints.txt\n", encoding="utf-8"
    )
    ranged, pinned = parse_requirements(path).packages
    assert ranged.spec == "<2,>=1.5"
    assert ranged.version is None
    assert pinned.version == "3"


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
        ("--only-final", (False, False)),
    ],
)
def test_requirements_binary_options_abbreviated(tmp_path, line, expected):
    # optparse, which reads pip's requirements files, takes an unambiguous prefix of a long option
    req = tmp_path / "requirements.txt"
    req.write_text(f"{line}\nsix==1.16.0\n", encoding="utf-8")
    (six,) = parse_requirements(req).packages
    assert (six.no_binary, six.only_binary) == expected
