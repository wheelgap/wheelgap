"""origin.known_pypi: whether a package is positively PyPI's, read from each kind of input."""

import pytest
from test_build import nowheels_sdist
from test_cli import run
from test_conda_forge import (
    API,
    PDM_LOCK,
    PIPFILE_CORP,
    PIPFILE_PYPI,
    POETRY_LOCK,
    PYPROJECT,
    conda_calls,
    pylock,
    uv_lock,
)
from test_notes import note
from test_sources import PIXI_V6

from wheelgap import notes, origin, pypi
from wheelgap.sources import Package, Source, load_inputs

EMPTY: dict[str, str] = {}
UV_DEFAULT_INDEX = "[[tool.uv.index]]\nname = 'corp'\nurl = 'https://pypi.corp.example/simple'\ndefault = true\n"


def package(tmp_path, files, name="nowheels"):
    """The input's package `name`, after the lookups a run would make (fake_pypi), and its Source."""
    first = next(iter(files))
    if "==" in first:  # a requirement on the command line
        sources = load_inputs([first])
    else:
        for filename, text in files.items():
            (tmp_path / filename).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                (tmp_path / filename).write_bytes(text)
            else:
                (tmp_path / filename).write_text(text, encoding="utf-8")
        sources = load_inputs([str(tmp_path / first)])
    packages = [p for s in sources for p in s.packages]
    pypi.fill_all(packages)
    return next((p, s) for s in sources for p in s.packages if p.key == name)


@pytest.mark.parametrize(
    "files,known",
    [
        ({"nowheels==0.9": ""}, True),  # a name on the command line, found on pypi.org
        ({"requirements.txt": "nowheels==0.9\n"}, True),
        ({"requirements.txt": "--index-url https://pypi.corp.example/simple\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "nowheels==0.9\n", "uv.toml": "index-url = 'https://x.example/simple'\n"}, False),
        ({"pyproject.toml": PYPROJECT}, True),
        ({"pyproject.toml": PYPROJECT + "\n[[tool.uv.index]]\nurl = 'https://pypi.corp.example/simple'\n"}, False),
        ({"pyproject.toml": PYPROJECT, "uv.toml": "[pip]\nindex-url = 'https://x.example/simple'\n"}, False),
        ({"Pipfile.lock": PIPFILE_PYPI}, True),
        ({"Pipfile.lock": PIPFILE_CORP}, False),
        ({"uv.lock": uv_lock("https://pypi.org/simple")}, True),
        ({"uv.lock": uv_lock("https://pkgs.example.com/simple")}, False),
        ({"pylock.toml": pylock("https://pypi.org/simple")}, True),
        ({"pylock.toml": pylock(None)}, False),
        ({"pdm.lock": PDM_LOCK}, False),
        ({"poetry.lock": POETRY_LOCK}, False),
        # A pyproject.toml, uv.toml or pdm.toml next to the input, or in a folder above it up to the repository root.
        (
            {
                "requirements.txt": "nowheels==0.9\n",
                "pyproject.toml": UV_DEFAULT_INDEX,
            },
            False,
        ),
        (
            {
                "requirements.txt": "nowheels==0.9\n",
                "pyproject.toml": "[tool.uv]\nindex-url = 'https://x.example/simple'\n",
            },
            False,
        ),
        (
            {
                "requirements.txt": "nowheels==0.9\n",
                "pyproject.toml": "[tool.uv.pip]\nextra-index-url = ['https://x.example/s']\n",
            },
            False,
        ),
        ({"requirements.txt": "nowheels==0.9\n", "pyproject.toml": "[tool.uv]\nno-index = true\n"}, False),
        (
            {
                "requirements.txt": "nowheels==0.9\n",
                "pyproject.toml": "[tool.uv]\ndefault-index = 'https://x.example/s'\n",
            },
            False,
        ),
        ({"requirements.txt": "nowheels==0.9\n", "pyproject.toml": "[tool.ruff]\nline-length = 120\n"}, True),
        ({"requirements.txt": "nowheels==0.9\n", "pyproject.toml": "not toml ["}, False),
        ({"requirements.txt": "nowheels==0.9\n", "pdm.toml": "[pypi]\nurl = 'https://x.example/simple'\n"}, False),
        (
            {
                "sub/requirements.txt": "nowheels==0.9\n",
                "pyproject.toml": UV_DEFAULT_INDEX,
            },
            False,
        ),  # a parent folder
        ({"sub/requirements.txt": "nowheels==0.9\n", "uv.toml": "index-url = 'https://x.example/s'\n"}, False),
        (
            {
                "repo/requirements.txt": "nowheels==0.9\n",
                "repo/.git/HEAD": "",
                "pyproject.toml": UV_DEFAULT_INDEX,
            },
            True,
        ),
        (
            {
                "Pipfile.lock": PIPFILE_PYPI,
                "pyproject.toml": UV_DEFAULT_INDEX,
            },
            False,
        ),
        ({"Pipfile.lock": PIPFILE_PYPI, "uv.toml": "index-url = 'https://x.example/s'\n"}, False),
        ({"pyproject.toml": PYPROJECT + "\n[tool.pixi.pypi-options]\nindex-url = 'https://x.example/simple'\n"}, False),
        (
            {"pyproject.toml": PYPROJECT + "\n[tool.pixi.pypi-options]\nextra-index-urls = ['https://x.example/s']\n"},
            False,
        ),
        ({"pyproject.toml": PYPROJECT + "\n[tool.pixi.pypi-options]\nfind-links = [{path = './wheels'}]\n"}, False),
        ({"pyproject.toml": PYPROJECT, "pdm.toml": "[pypi.corp]\nurl = 'https://x.example/simple'\n"}, False),
        # Requirements files: a byte order mark, includes that aren't read, abbreviated include options.
        ({"requirements.txt": b"\xef\xbb\xbf--index-url https://x.example/simple\nnowheels==0.9\n"}, False),
        ({"requirements.txt": b"\xef\xbb\xbfnowheels==0.9\n"}, True),
        ({"requirements.txt": "-c https://x.example/constraints.txt\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "-c ${CONSTRAINTS}\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "--requirem more.txt\nnowheels==0.9\n", "more.txt": ""}, False),
        ({"requirements.txt": "--cons=more.txt\nnowheels==0.9\n", "more.txt": ""}, False),
        ({"requirements.txt": "--require-hashes\nnowheels==0.9 --hash=sha256:00\n"}, True),
        ({"requirements.txt": "-c cons.txt\nnowheels==0.9\n", "cons.txt": "nowheels<1\n"}, True),
    ],
)
def test_known_pypi_by_input(fake_pypi, tmp_path, files, known):
    pkg, source = package(tmp_path, files)
    assert origin.known_pypi(pkg, source, env=EMPTY) is known


def test_pixi_lock_entries_are_not_known(fake_pypi, tmp_path):
    # Looked up on pypi.org (pixi.lock keeps one file per platform), but a pixi.lock isn't Source.pypi_only.
    pkg, source = package(tmp_path, {"pixi.lock": PIXI_V6.replace("INDEX", "https://pypi.org/simple")}, "purewheel")
    assert pkg.found_on_pypi and not origin.known_pypi(pkg, source, env=EMPTY)


@pytest.mark.parametrize("variable", [*origin.INDEX_ENV, "UV_INDEX_CORP_USERNAME", "PIP_index_url", "PIP_Find_Links"])
def test_an_index_in_the_environment(fake_pypi, tmp_path, variable):
    env = {variable: "https://pypi.corp.example/simple"}
    pkg, source = package(tmp_path, {"requirements.txt": "nowheels==0.9\n"})
    assert origin.known_pypi(pkg, source, env=EMPTY)
    assert not origin.known_pypi(pkg, source, env=env)  # found on pypi.org, but pip or uv may use another index
    assert origin.known_pypi(pkg, source, env={variable: ""})  # an empty variable sets nothing
    pkg, source = package(tmp_path, {"uv.lock": uv_lock("https://pypi.org/simple")})
    assert origin.known_pypi(pkg, source, env=env)  # the lock says it came from pypi.org


@pytest.mark.parametrize("variable", ["PIP_PROGRESS_BAR", "UV_CACHE_DIR", "pip_index_url", "UV_INDEXES"])
def test_other_variables_dont_count(variable):
    assert origin.index_env({variable: "x"}) is None


@pytest.mark.parametrize(
    "registry,known",
    [
        ("https://pypi.org/simple", True),
        ("https://pypi.org/simple/", True),
        ("", False),  # says nothing about where it came from
        ("https://pypi.org/simple-corp", False),
        ("https://pypi.org/simple/corp", False),
        ("https://pypi.python.org/simple", False),
        (None, False),  # no registry and not found on pypi.org this run
    ],
)
def test_registry_must_be_exactly_pypi(registry, known):
    assert origin.known_pypi(Package("nowheels", "0.9", registry=registry), Source("uv.lock"), env=EMPTY) is known


def test_found_on_pypi_is_set_by_pypi_org_lookups_only(fake_pypi, monkeypatch):
    assert Package("nowheels", "0.9").found_on_pypi is False
    pkg = Package("nowheels", "0.9")
    pypi.fill(pkg)
    assert pkg.found_on_pypi is True
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    pkg = Package("nowheels", "0.9")
    pypi.fill(pkg)
    assert pkg.files and pkg.found_on_pypi is False  # found, but on the mirror


@pytest.mark.parametrize(
    "mirror,known", [("https://mirror.example.com/pypi", False), ("https://pypi.org/pypi/", True), ("", True)]
)
def test_wheelgap_index_url_mirror(mirror, known):
    pkg = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    assert origin.known_pypi(pkg, Source("uv.lock"), env={"WHEELGAP_INDEX_URL": mirror}) is known
    assert origin.other_index(env={"WHEELGAP_INDEX_URL": mirror}) == (None if known else "WHEELGAP_INDEX_URL")


def test_index_url_and_skipped_packages():
    pkg = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    assert origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY)
    assert not origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY, simple_index="https://x.example/simple")
    assert origin.other_index("https://x.example/simple", EMPTY) == "--index-url"
    pkg.skip = "private index"
    assert not origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY)


@pytest.mark.parametrize(
    "found_on_pypi,pypi_only,known", [(True, True, True), (True, False, False), (False, True, False)]
)
def test_found_on_pypi_and_pypi_only_both_needed(found_on_pypi, pypi_only, known):
    pkg = Package("nowheels", "0.9", found_on_pypi=found_on_pypi)
    assert origin.known_pypi(pkg, Source("requirements.txt", pypi_only=pypi_only), env=EMPTY) is known


def test_the_default_environment_is_os_environ(monkeypatch):
    pkg = Package("nowheels", "0.9", found_on_pypi=True)
    source = Source("command line", pypi_only=True)
    assert origin.known_pypi(pkg, source)
    monkeypatch.setenv("UV_INDEX", "corp=https://pypi.corp.example/simple")
    assert not origin.known_pypi(pkg, source)


# #262: a name is private for the whole run when any input places it somewhere other than PyPI.


def two_inputs(tmp_path):
    """a/uv.lock places nowheels on a private index; b/requirements.txt pins the same name, which pypi.org has too."""
    lock, reqs = tmp_path / "a" / "uv.lock", tmp_path / "b" / "requirements.txt"
    for path, text in ((lock, uv_lock("https://pkgs.acme.example/simple")), (reqs, "nowheels==0.9\npurewheel==3.1\n")):
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
    return str(lock), str(reqs)


def test_private_names_are_private_in_every_input(fake_pypi, tmp_path):
    sources = load_inputs(list(two_inputs(tmp_path)))
    pypi.fill_all([p for s in sources for p in s.packages])
    reqs = next(s for s in sources if s.requirements)
    nowheels, purewheel = (next(p for p in reqs.packages if p.key == name) for name in ("nowheels", "purewheel"))
    assert origin.known_pypi(nowheels, reqs, env=EMPTY)  # going by its own input alone
    private = origin.private_names(sources, env=EMPTY)
    assert "nowheels" in private and "purewheel" not in private
    assert not origin.known_pypi(nowheels, reqs, env=EMPTY, private=private)
    assert origin.known_pypi(purewheel, reqs, env=EMPTY, private=private)
    assert "nowheels" not in origin.private_names([reqs], env=EMPTY)  # the requirements file alone


def test_private_names_judges_a_range_by_its_resolutions():
    # The lookups mark each Python's resolution as found on pypi.org, not the range they were resolved from.
    resolved = Package("nowheels", "0.9", found_on_pypi=True)
    ranged = Package("nowheels", None, spec=">=0.1", resolutions={"3.13": resolved, "3.14": resolved})
    source = Source("command line", [ranged], pypi_only=True)
    assert origin.private_names([source], env=EMPTY) == frozenset()
    # A Python with no matching release is an error, not another index: the name is still pypi.org's.
    ranged.resolutions["3.9"] = Package("nowheels", None, spec=">=0.1", error="no release matches >=0.1 for Python 3.9")
    assert origin.private_names([source], env=EMPTY) == frozenset()
    resolved.found_on_pypi = False
    assert origin.private_names([source], env=EMPTY) == {"nowheels"}


def test_private_names_include_what_a_group_selection_left_out():
    # poetry.lock --no-dev drops the package but the lock still places it somewhere wheelgap no longer knows.
    lock = Source("poetry.lock", [], unselected_keys={"nowheels"})
    pkg = Package("nowheels", "0.9", found_on_pypi=True)
    reqs = Source("requirements.txt", [pkg], pypi_only=True)
    private = origin.private_names([lock, reqs], env=EMPTY)
    assert private == {"nowheels"} and not origin.known_pypi(pkg, reqs, env=EMPTY, private=private)


def test_private_names_include_a_name_a_lock_cant_place(fake_pypi, tmp_path):
    # pdm.lock records no index, so a requirements file's nowheels beside it is kept in too.
    (tmp_path / "pdm.lock").write_text(PDM_LOCK, encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    sources = load_inputs([str(tmp_path / "pdm.lock"), str(tmp_path / "b" / "requirements.txt")])
    pypi.fill_all([p for s in sources for p in s.packages])
    assert origin.private_names(sources, env=EMPTY) == {"nowheels"}


def test_private_names_cover_another_entry_of_the_same_input():
    # One uv.lock that forks a name between pypi.org and another index: the pypi.org entry is kept in too.
    on_pypi = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    elsewhere = Package("nowheels", "0.9", registry="https://pkgs.acme.example/simple")
    source = Source("uv.lock", [on_pypi, elsewhere])
    assert origin.known_pypi(on_pypi, source, env=EMPTY)  # going by that entry alone
    private = origin.private_names([source], env=EMPTY)
    assert private == {"nowheels"}
    assert not origin.known_pypi(on_pypi, source, env=EMPTY, private=private)


def test_private_names_with_another_index_is_every_name():
    pkg = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    sources = [Source("uv.lock", [pkg])]
    assert origin.private_names(sources, env=EMPTY) == frozenset()
    assert origin.private_names(sources, env=EMPTY, simple_index="https://x.example/simple") == {"nowheels"}


EXPLAIN = ("-p", "3.13", "--explain", "--no-cache")


def test_conda_forge_keeps_a_name_another_input_places_on_a_private_index(capsys, fake_pypi, tmp_path):
    lock, reqs = two_inputs(tmp_path)
    _, out, err = run(capsys, reqs, "-t", "linux-aarch64", *EXPLAIN, "--conda-forge")
    assert conda_calls(fake_pypi) == [API + "nowheels"] and "didn't look up" not in err  # alone, it's PyPI's
    fake_pypi.clear()
    _, out, err = run(capsys, lock, reqs, "-t", "linux-aarch64", *EXPLAIN, "--conda-forge")
    assert conda_calls(fake_pypi) == [] and "conda-forge has" not in out
    assert "didn't look up 1 package whose index isn't known to be PyPI (nowheels)" in err


def test_conda_forge_note_counts_two_spellings_of_a_name_once(capsys, fake_pypi, tmp_path):
    lock, reqs = two_inputs(tmp_path)
    (tmp_path / "b" / "requirements.txt").write_text("NoWheels==0.9\n", encoding="utf-8")
    _, _, err = run(capsys, lock, reqs, "-t", "linux-aarch64", *EXPLAIN, "--conda-forge")
    assert conda_calls(fake_pypi) == [] and "didn't look up 1 package whose index isn't known to be PyPI" in err


def test_notes_keep_a_name_another_input_places_on_a_private_index(capsys, fake_pypi, tracker, tmp_path):
    lock, reqs = two_inputs(tmp_path)
    tracker.add("nowheels", note())
    run(capsys, reqs, "-t", "linux-x86_64", *EXPLAIN, "--notes")
    assert tracker.calls == [f"{notes.TRACKER}/nowheels.json"]  # alone, it's PyPI's
    tracker.calls.clear()
    _, out, _ = run(capsys, lock, reqs, "-t", "linux-x86_64", *EXPLAIN, "--notes")
    assert tracker.calls == [] and "note from the wheelgap tracker" not in out
    _, _, err = run(capsys, lock, reqs, "-t", "linux-x86_64", *EXPLAIN)
    assert "add --notes" not in err  # and no suggestion to send it


def test_build_info_keeps_a_name_another_input_places_on_a_private_index(capsys, fake_pypi, fake_files, tmp_path):
    lock, reqs = two_inputs(tmp_path)
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, reqs, "-t", "linux-x86_64", *EXPLAIN, "--build-info")
    assert "build needs: at least a Rust toolchain" in out and fake_files.calls  # alone, it's PyPI's
    fake_files.calls.clear()
    fake_pypi.clear()
    _, out, _ = run(capsys, lock, reqs, "-t", "linux-x86_64", *EXPLAIN, "--build-info")
    assert "build needs" not in out and fake_files.calls == []
    assert fake_pypi.count("https://pypi.org/pypi/nowheels/0.9/json") == 1  # the check itself; none for the sdist
