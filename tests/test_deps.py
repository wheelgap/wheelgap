"""--deps (#353): package names on the command line, with what they install resolved per target from PyPI's
metadata. A fake JSON API serves each project page and each release page with its Requires-Dist."""

import json
import re
import urllib.error
from collections import Counter

import pytest
from packaging.requirements import Requirement

import wheelgap
from wheelgap import cache, deps, pypi
from wheelgap.cli import main
from wheelgap.deps import DepsError, Resolver

ANY = "py3-none-any.whl"
LINUX = "cp314-cp314-manylinux_2_17_x86_64.whl"
WIN_AMD = "cp314-cp314-win_amd64.whl"
WIN_ARM = "cp314-cp314-win_arm64.whl"


def release(requires=(), files=(ANY,), requires_python=None, yanked=False, sdist=True, json_requires="same"):
    """`requires` is what the wheels' metadata files declare; `json_requires`, what PyPI's JSON page says (by
    default the same), which comes from whichever file was uploaded first."""
    requires = list(requires) if requires is not None else None
    return {"requires": requires, "files": list(files), "requires_python": requires_python, "yanked": yanked,
            "sdist": sdist, "json_requires": requires if json_requires == "same" else json_requires}  # fmt: skip


# name -> version -> release. "app" needs lib[crypto]; lib's crypto extra needs crypt, which has no Windows ARM
# wheel; on Windows lib also needs winonly.
INDEX = {
    "app": {"1.0": release(["lib[crypto]>=1"])},
    "parenta": {"1.0": release(["crypt>=1"])},
    "parentb": {"1.0": release(["crypt>=1"])},
    "plain": {"1.0": release(["lib>=1"])},
    "lib": {
        "1.0": release(),
        "2.0": release(["crypt>=1; extra == 'crypto'", "winonly; sys_platform == 'win32'"]),
        "3.0": release(requires_python=">=3.15"),  # newest, but not for 3.14
    },
    "crypt": {"1.0": release(files=(LINUX, WIN_AMD))},
    "winonly": {"1.0": release(files=(WIN_ARM, WIN_AMD))},
    # a and b disagree about c: the newest c that suits both is 1.5.
    "a": {"1.0": release(["c>=1"])},
    "b": {"1.0": release(["c<2"])},
    "c": {"1.0": release(), "1.5": release(), "2.0": release()},
    "x": {"1.0": release(["c>=2"])},
    "y": {"1.0": release(["c<2"])},
    "yank": {"1.0": release(), "2.0": release(yanked=True)},
    "nometa": {"1.0": release(requires=None, files=())},  # sdist only and no Requires-Dist: can't tell
    "sdistonly": {"1.0": release(["c"], files=())},  # no wheel: the JSON page's Requires-Dist is all there is
    # PyPI's JSON page lists nothing (an sdist was uploaded first), but the wheel declares c (methodtools 0.4.7).
    "jsonliar": {"1.0": release(["c"], json_requires=None)},
    # pc's newest final is 2.0; pa's constraint lets its 2.1b1 prerelease in, whichever way round they come.
    "pa": {"1.0": release(["pc>=2.0b1"])},
    "pb": {"1.0": release(["pc"])},
    "pc": {"1.0": release(), "2.0": release(), "2.1b1": release()},
    # noarm 2.0 has only a Linux wheel and no sdist, so pip on Windows on ARM installs 1.0; nowhere never installs.
    "noarm": {"1.0": release(files=(WIN_ARM,)), "2.0": release(files=(LINUX,), sdist=False)},
    "nowhere": {"1.0": release(files=(LINUX,), sdist=False), "2.0": release(files=(LINUX,), sdist=False)},
    # Its newest release is an sdist only; the one before has a Windows ARM wheel, which --only-binary makes pip pick.
    "sdnew": {"1.0": release(files=(WIN_ARM,)), "2.0": release(files=())},
    "egg": {"1.0": release(files=(WIN_ARM,)), "2.0": release(files=("py3.14.egg",), sdist=False)},
    "yq": {"1.0": release(["c"], yanked=True, json_requires=[])},  # yanked: its wheel still says what it needs
    # No Windows ARM or pure wheel, so a build from source there: two platforms' wheels must agree.
    "agree": {"1.0": release(["c"], files=(LINUX, WIN_AMD))},
    "differ": {"1.0": release(["c"], files=(LINUX, WIN_AMD), json_requires=None)},
    # Two Linux wheels agree, but the Windows one is the one a Windows source build is like.
    "sameos": {"1.0": release(["c"], files=(LINUX, "cp314-cp314-manylinux_2_17_aarch64.whl", WIN_AMD))},
    # dropped's newest release has no Windows ARM wheel; the one before had one (#403).
    "tool": {"1.0": release(["dropped"])},
    "dropped": {"1.0": release(files=(WIN_ARM, LINUX)), "2.0": release(files=(LINUX,))},
}
DIFFERING = {
    "differ-1.0-" + WIN_AMD: ["c", "pywin"],
    "sameos-1.0-" + WIN_AMD: ["c", "pywin"],
}  # its win_amd64 wheel declares another dependency


@pytest.fixture
def index(monkeypatch):
    calls = []

    def entries(name, version, r):
        filenames = [f"{name}-{version}-{tag}" for tag in r["files"]] + [f"{name}-{version}.tar.gz"] * r["sdist"]
        return [{"filename": f, "url": f"https://files.example/{f}", "requires_python": r["requires_python"],
                 "yanked": r["yanked"]} for f in filenames]  # fmt: skip

    def fetch_text(url, max_bytes, timeout=20.0, attempts=4):
        calls.append(url)
        filename = url.removeprefix("https://files.example/").removesuffix(".metadata")
        name, version = filename.split("-")[:2]
        requires = DIFFERING.get(filename, INDEX[name][version]["requires"])
        return "Metadata-Version: 2.1\nName: " + name + "\n" + "".join(f"Requires-Dist: {d}\n" for d in requires)

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        calls.append(url)
        parts = url.removeprefix(pypi.index_url() + "/").removesuffix("/json").split("/")
        name = parts[0]
        if name not in INDEX:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        versions = INDEX[name]
        if len(parts) == 1:
            latest = next(reversed(versions))
            return {"info": {"name": name, "version": latest},
                    "releases": {v: entries(name, v, r) for v, r in versions.items()},
                    "urls": entries(name, latest, versions[latest])}  # fmt: skip
        r = versions.get(parts[1])
        if r is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return {"info": {"name": name, "version": parts[1], "requires_dist": r["json_requires"]},
                "urls": entries(name, parts[1], r)}  # fmt: skip

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    monkeypatch.setattr(pypi, "fetch_text", fetch_text)
    return calls


WIN, LIN = wheelgap.make_targets(["3.14"], ["win-arm64", "linux-x86_64"])


def resolve(*specs, target=WIN):
    tree = Resolver().resolve([Requirement(s) for s in specs], target)
    return tree.chosen, tree.errors, tree.paths


def test_extras_and_markers_are_followed_per_target(index):
    chosen, errors, paths = resolve("app==1.0")
    assert (chosen, errors) == ({"app": "1.0", "lib": "2.0", "crypt": "1.0", "winonly": "1.0"}, {})
    assert paths["crypt"] == ["app", "lib", "crypt"]
    assert resolve("app==1.0", target=LIN)[0] == {"app": "1.0", "lib": "2.0", "crypt": "1.0"}  # no winonly
    assert resolve("plain")[0] == {"plain": "1.0", "lib": "2.0", "winonly": "1.0"}  # lib without [crypto]: no crypt


def test_requires_python_and_yanked_releases_are_skipped(index):
    assert resolve("lib")[0]["lib"] == "2.0"  # 3.0 needs Python 3.15
    assert resolve("lib", target=wheelgap.make_targets(["3.15"], ["win-arm64"])[0])[0]["lib"] == "3.0"
    assert resolve("yank")[0] == {"yank": "1.0"}
    assert resolve("yank==2.0")[0] == {"yank": "2.0"}  # pip installs a yanked release pinned with ==


def test_a_later_constraint_restarts_with_it_known(index):
    assert resolve("a", "b")[0] == {"a": "1.0", "b": "1.0", "c": "1.5"}  # not c 2.0, which b rules out


def test_constraints_that_cant_all_be_met_refuse(index):
    with pytest.raises(DepsError, match="can't resolve c for Python 3.14 on win-arm64: no release meets <2, >=2"):
        resolve("x", "y")


def test_a_release_whose_dependencies_cant_be_read_is_an_error_not_a_clean_bill(index):
    chosen, errors, _ = resolve("nometa")
    assert chosen == {"nometa": "1.0"}
    assert errors == {"nometa": "couldn't read its dependencies: PyPI lists no dependencies for it and it has no "
                                "wheel to confirm that"}  # fmt: skip
    assert resolve("missing")[1] == {"missing": "not on index"}


def test_index_answers_are_shared_between_targets(index):
    resolver = Resolver()
    resolver.resolve([Requirement("app==1.0")], WIN)
    asked = len(index)
    resolver.resolve([Requirement("app==1.0")], LIN)
    assert len(index) == asked


def test_cli_checks_what_the_names_install(index, capsys):
    code = main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "-t", "linux-x86_64", "--json", "--explain",
                 "--no-cache", "--all"])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    rows = {p["name"]: p for p in out["packages"]}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"]["results"] == {"win-arm64": "source", "linux-x86_64": "wheel"}
    assert rows["crypt"]["via"] == {"win-arm64": ["app", "lib", "crypt"]}
    assert rows["winonly"]["results"] == {"win-arm64": "wheel", "linux-x86_64": "n/a"}
    assert code == 1


def test_cli_text_names_the_path_and_notes_how_it_resolved(index, capsys):
    assert main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--explain", "--no-cache"]) == 1
    captured = capsys.readouterr()
    assert "  via: app → lib → crypt\n" in captured.out
    assert "--deps resolved 4 packages from PyPI's metadata" in captured.err


WAY_OUT = "dropped 2.0: no newer release has a wheel for win-arm64; the last one that did is 1.0"


def test_explain_gives_the_way_out_by_default(index, capsys):
    # --deps already looks every package up on PyPI, so --suggest sends nothing more (#403).
    args = ["tool==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--explain", "--no-cache", "--color", "never"]
    assert main(args) == 1
    assert WAY_OUT in capsys.readouterr().out
    assert main([*args, "--json"]) == 1
    rows = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    assert rows["dropped"]["last_with_wheel"] == {"win-arm64": "1.0"}
    assert main([*args, "--no-suggest"]) == 1
    assert "the last one that did" not in capsys.readouterr().out


def test_suggest_stays_opt_in_without_deps_or_explain(index, capsys):
    assert main(["dropped==2.0", "-p", "3.14", "-t", "win-arm64", "--explain", "--no-cache", "--color", "never"]) == 1
    assert "the last one that did" not in capsys.readouterr().out
    assert main(["tool==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache", "--json"]) == 1
    assert "suggest" not in json.loads(capsys.readouterr().out)["packages"][0]


def test_write_constraints_works_with_deps_explain_alone(index, capsys, tmp_path):
    out = tmp_path / "constraints.txt"
    args = ["tool==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--explain", "--no-cache"]
    assert main([*args, "--write-constraints", str(out)]) == 1
    assert "dropped==1.0" in out.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main([*args, "--no-suggest", "--write-constraints", str(out)])
    assert e.value.code == 2


def test_without_deps_only_the_named_package_is_checked(index, capsys):
    assert main(["app==1.0", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache", "--all"]) == 0
    assert [p["name"] for p in json.loads(capsys.readouterr().out)["packages"]] == ["app"]


def test_python_specific_picks_are_separate_rows(index, capsys):
    assert main(["lib", "--deps", "-p", "3.14", "-p", "3.15", "-t", "linux-x86_64", "--json", "--no-cache",
                 "--all"]) == 0  # fmt: skip
    rows = [(p["version"], p["results"]) for p in json.loads(capsys.readouterr().out)["packages"]]
    assert sorted(rows) == [
        ("2.0", {"3.14 linux-x86_64": "wheel", "3.15 linux-x86_64": "n/a"}),
        ("3.0", {"3.14 linux-x86_64": "n/a", "3.15 linux-x86_64": "wheel"}),
    ]


def test_an_unresolvable_tree_is_an_input_error(index, capsys):
    assert main(["x", "y", "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]) == 2
    assert "wheelgap: error: --deps: can't resolve c" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--offline"], "--offline: --deps would look packages up on PyPI"),
        (["--index-url", "https://example.org/simple"], "--deps reads dependencies from PyPI's JSON API"),
    ],
)
def test_deps_refuses_what_it_cant_do(index, capsys, args, message):
    assert main(["app==1.0", "--deps", *args]) == 2
    assert message in capsys.readouterr().err


def test_deps_refuses_a_lockfile(fixtures, index, capsys):
    assert main([str(fixtures / "uvproj" / "uv.lock"), "--deps"]) == 2
    err = capsys.readouterr().err
    assert "--deps applies to package names on the command line" in err
    assert "a lockfile already lists what it installs" in err


def test_requirements_resolve_what_they_install(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("app\n", encoding="utf-8")
    code = main(
        [
            str(tmp_path / "requirements.txt"),
            "--deps",
            "-p",
            "3.14",
            "-t",
            "win-arm64",
            "--all",
            "--explain",
            "--no-cache",
        ]
    )
    out = capsys.readouterr().out
    assert "4 packages" in out
    for name in ("app", "lib", "crypt", "winonly"):
        assert re.search(rf"^{name}\s", out, re.M)
    assert "via: app → lib → crypt" in out  # the file's one requirement grows into the tree
    assert code == 1  # crypt has no win-arm64 wheel


def test_requirements_markers_and_extras_apply_per_target(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("lib[crypto]\nplain ; sys_platform == 'win32'\n", encoding="utf-8")
    code = main(
        [
            str(tmp_path / "requirements.txt"),
            "--deps",
            "-p",
            "3.14",
            "-t",
            "win-arm64",
            "-t",
            "linux-x86_64",
            "--json",
            "--no-cache",
        ]
    )
    pkgs = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    assert sorted(pkgs) == ["crypt", "lib", "plain", "winonly"]
    # plain's marker rules it out on linux: no install there, so the row stays n/a rather than vanishing
    assert pkgs["plain"]["results"] == {"win-arm64": "wheel", "linux-x86_64": "n/a"}
    assert pkgs["winonly"]["results"]["linux-x86_64"] == "n/a"  # lib's win32 dep doesn't install on linux either
    assert pkgs["crypt"]["results"]["linux-x86_64"] == "wheel"  # the [crypto] extra's dep resolves on both
    assert code == 1


def test_a_compiled_requirements_file_is_checked_as_written(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "lib==2.0 \\\n    --hash=sha256:aa\n    # via app\ncrypt==1.0 \\\n    --hash=sha256:bb\n    # via lib\n",
        encoding="utf-8",
    )
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"])
    err = capsys.readouterr().err
    assert "requirements.txt already lists what it installs" in err
    assert "--deps resolved" not in err
    # nothing resolved, so the only lookups are the two packages the file names
    assert index and all("/lib/" in url or "/crypt/" in url for url in index)
    assert code == 1


def test_a_pinned_file_without_pip_compiles_marks_still_resolves(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("lib==2.0\n", encoding="utf-8")
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache"])
    captured = capsys.readouterr()
    pkgs = {p["name"] for p in json.loads(captured.out)["packages"]}
    assert pkgs == {"lib", "winonly"}  # pin or not, the tree is what pip would install
    assert "--deps resolved 2 packages" in captured.err
    assert code == 0


def test_requirements_no_binary_applies_to_resolved_packages(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("--only-binary :all:\napp\n", encoding="utf-8")
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache"])
    pkgs = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    # Without the file's --only-binary, crypt builds from its sdist here; with it, the missing wheel is an error.
    assert pkgs["crypt"]["results"]["win-arm64"] == "missing"
    assert code == 1


def test_requirements_skips_stay_skipped_under_deps(index, capsys, tmp_path):
    (tmp_path / "requirements.txt").write_text("-e .\nhttps://example.org/frozen-1.0.zip\napp\n", encoding="utf-8")
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"])
    out = capsys.readouterr().out
    assert "skipped 2 non-index packages" in out
    assert "https://example.org/frozen-1.0.zip" in out
    assert code == 1  # crypt still reports its missing win-arm64 wheel


def test_requirements_constraints_apply_to_the_resolved_tree(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text("c==1.0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\na\n", encoding="utf-8")
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache"])
    pkgs = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    assert pkgs["c"]["version"] == "1.0"  # the constraint reaches the dependency, not just named packages
    assert code == 0


def test_a_broken_constraint_makes_deps_refuse(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text("app==1.0\napp==2.0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\napp\n", encoding="utf-8")
    code = main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"])
    err = capsys.readouterr().err
    # Without --deps the package reports unknown; resolving its tree would rest on a guess, so --deps refuses.
    assert "--deps can't follow this the way pip does" in err and "conflicting exact versions" in err
    assert code == 2


def test_pyproject_deps_follow_selected_extras_and_target_markers(tmp_path, index, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "workshop"
dependencies = ["plain; sys_platform == 'linux'", "b; sys_platform == 'win32'"]
[project.optional-dependencies]
crypto = ["app"]
""",
        encoding="utf-8",
    )
    args = [str(pyproject), "-p", "3.14", "-t", "win-arm64,linux-x86_64", "--explain", "--no-cache"]
    assert main(args) == 0
    assert "lists direct dependencies only" in capsys.readouterr().err

    assert main([*args, "--deps", "--extra", "crypto"]) == 1
    report = capsys.readouterr()
    assert "via: app → lib → crypt" in report.out
    assert "lists direct dependencies only" not in report.err
    assert main([*args, "--deps", "--extra", "crypto", "--json", "--all"]) == 1
    rows = {p["name"]: p["results"] for p in json.loads(capsys.readouterr().out)["packages"]}
    assert rows["b"] == {"win-arm64": "wheel", "linux-x86_64": "n/a"}
    assert rows["crypt"] == {"win-arm64": "source", "linux-x86_64": "wheel"}


def test_pyproject_deps_never_sends_private_index_roots_to_pypi(tmp_path, index, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "workshop"\ndependencies = ["private-package"]\n'
        '[tool.uv]\nindex-url = "https://example.invalid/simple"\n',
        encoding="utf-8",
    )
    assert main([str(pyproject), "--deps"]) == 2
    assert "configures an installer index or source" in capsys.readouterr().err
    assert index == []


def test_pyproject_deps_refuses_an_index_set_in_the_environment(tmp_path, index, capsys, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "workshop"\ndependencies = ["private-package"]\n', encoding="utf-8")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    assert main([str(pyproject), "--deps"]) == 2
    assert "PIP_INDEX_URL may point pip or uv at another index" in capsys.readouterr().err
    assert index == []


def test_round_limit_refuses(index, monkeypatch):
    monkeypatch.setattr(deps, "MAX_ROUNDS", 1)
    with pytest.raises(DepsError, match="can't settle on releases"):
        resolve("a", "b")


def test_dependencies_come_from_the_wheel_not_the_json_page(index):
    assert resolve("jsonliar")[0] == {"jsonliar": "1.0", "c": "2.0"}
    assert resolve("sdistonly")[0] == {"sdistonly": "1.0", "c": "2.0"}  # no wheel: the JSON page it is


@pytest.mark.parametrize("order", [("pa", "pb"), ("pb", "pa")])
def test_picks_dont_depend_on_requirement_order(index, order):
    assert resolve(*order)[0]["pc"] == "2.1b1"


def test_a_release_that_doesnt_install_on_the_target_is_passed_over(index):
    assert resolve("noarm")[0] == {"noarm": "1.0"}
    assert resolve("noarm", target=LIN)[0] == {"noarm": "2.0"}
    assert resolve("nowhere")[0] == {"nowhere": "2.0"}  # nothing installs: the newest, which shows the gap


def test_only_wheels_and_sdists_count(index):
    assert resolve("egg")[0] == {"egg": "1.0"}  # pip can't install 2.0's .egg


def test_a_pinned_yanked_release_reads_its_wheel(index):
    assert resolve("yq==1.0")[0] == {"yq": "1.0", "c": "2.0"}


def test_a_source_build_trusts_other_platforms_wheels_only_when_they_agree(index):
    assert resolve("agree")[0] == {"agree": "1.0", "c": "2.0"}
    chosen, errors, _ = resolve("differ")
    assert chosen == {"differ": "1.0"}
    assert errors["differ"].startswith("couldn't read its dependencies: its wheels for other platforms list different")
    assert resolve("differ", target=LIN)[0] == {"differ": "1.0", "c": "2.0"}  # its own wheel: no doubt
    assert resolve("sameos")[1]["sameos"].startswith("couldn't read its dependencies: its wheels for other")


def test_requirements_only_binary_picks_the_release_pip_would(index, capsys, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("--only-binary :all:\nsdnew\n", encoding="utf-8")
    assert main([str(req), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache"]) == 0
    assert [(p["name"], p["version"]) for p in json.loads(capsys.readouterr().out)["packages"]] == [("sdnew", "1.0")]
    req.write_text("sdnew\n", encoding="utf-8")  # without it, pip builds the newest from its sdist
    assert main([str(req), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache", "--all"]) == 1
    assert [(p["name"], p["version"]) for p in json.loads(capsys.readouterr().out)["packages"]] == [("sdnew", "2.0")]


def test_requirements_with_via_comments_but_no_hashes_are_still_resolved(index, capsys, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("plain==1.0\n    # via app\n", encoding="utf-8")  # hand-edited: lib, its dependency, isn't listed
    main([str(req), "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache", "--all"])
    assert "lib" in {p["name"] for p in json.loads(capsys.readouterr().out)["packages"]}


@pytest.mark.parametrize(
    ("constraint", "message"),
    [
        ("crypt @ https://example.invalid/crypt-1.0.tar.gz", "the constraint on crypt isn't an index release"),
        # No target fixes these, so whether pip applies the constraint is unknown: refuse rather than apply it.
        ('crypt<2; platform_release >= "5"', 'can\'t tell whether the constraint `crypt<2; platform_release >= "5"`'),
        ('crypt<2; platform_version == "x"', "applies on 3.14 win-arm64"),
    ],
)
def test_requirements_deps_refuses_constraints_it_cant_follow(index, capsys, tmp_path, constraint, message):
    (tmp_path / "constraints.txt").write_text(constraint + "\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\napp==1.0\n", encoding="utf-8")
    assert main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]) == 2
    assert message in capsys.readouterr().err


def test_requirements_deps_refuses_a_file_that_names_another_index(tmp_path, index, capsys):
    req = tmp_path / "requirements.txt"
    req.write_text("--index-url https://example.invalid/simple\nprivate-package\n", encoding="utf-8")
    assert main([str(req), "--deps"]) == 2
    assert "configures an installer index or source" in capsys.readouterr().err
    assert index == []


def test_deps_doesnt_turn_suggest_on_for_a_lockfile_among_the_inputs(index, capsys, tmp_path):
    # The lockfile's packages weren't looked up by --deps, so their names mustn't go to PyPI by default.
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        'lock-version = "1.0"\n[[packages]]\nname = "secretpkg"\nversion = "1.0"\n'
        '[[packages.wheels]]\nurl = "https://pypi.org/files/secretpkg-1.0-cp314-cp314-manylinux_2_17_x86_64.whl"\n',
        encoding="utf-8",
    )
    main(["tool==1.0", str(lock), "--deps", "--explain", "-p", "3.14", "-t", "win-arm64", "--no-cache"])
    capsys.readouterr()
    assert not [u for u in index if "secretpkg" in u]


def test_deps_tables_count_distinct_direct_requirements(index, capsys):
    for display in ([], ["--markdown"]):
        code = main(["parenta", "parentb", "--deps", "-p", "3.14", "-t", "win-arm64", *display])
        output = capsys.readouterr().out
        assert code == 1
        assert "crypt (via parenta +1)" in output
    assert index


def test_deps_json_keeps_original_path(index, capsys):
    code = main(["parenta", "parentb", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    crypt = next(p for p in output["packages"] if p["name"] == "crypt")
    assert "via_direct" not in crypt


def test_deps_extra_only_labels_the_requesting_direct_root(index, capsys):
    for display in ([], ["--markdown"]):
        code = main(["app", "plain", "--deps", "-p", "3.14", "-t", "win-arm64", *display])
        output = capsys.readouterr().out
        assert code == 1
        assert "crypt (via app)" in output
        assert "crypt (via app +1)" not in output
    assert index


def test_deps_direct_origins_preserve_roots_across_multiple_targets(index, capsys):
    code = main(
        [
            "parenta",
            "parentb",
            "--deps",
            "-p",
            "3.14",
            "-t",
            "win-arm64",
            "-t",
            "linux-x86_64",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "crypt (via parenta +1)" in out
    assert index


@pytest.mark.parametrize("target", [WIN, LIN])
def test_direct_roots_reads_shared_requirements_once_per_target(index, monkeypatch, target):
    monkeypatch.setitem(INDEX, "third", {"1.0": release(["lib[crypto]>=1"])})
    roots = [Requirement(name) for name in ("app", "plain", "third")]
    resolver = Resolver()
    tree = resolver.resolve(roots, target)
    calls = Counter()
    original = resolver.requires

    def requires(key, version, requested_target):
        calls[key, version, requested_target] += 1
        return original(key, version, requested_target)

    monkeypatch.setattr(resolver, "requires", requires)
    owners = deps._direct_roots(tree, roots, resolver, target)

    assert owners["lib"] == ("app", "plain", "third")
    assert owners["crypt"] == ("app", "third")  # plain does not request lib's crypto extra
    if target == WIN:
        assert owners["winonly"] == ("app", "plain", "third")
    else:
        assert "winonly" not in owners
    assert calls == Counter({(key, version, target): 1 for key, version in tree.chosen.items()})


def test_wheel_metadata_is_cached_on_disk_between_runs(index):
    """#397: a second run reads each wheel's Requires-Dist from the cache instead of the index."""
    first = Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    fetched = [u for u in index if u.endswith(".metadata")]
    assert fetched
    index.clear()
    second = Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    assert (second.chosen, second.errors) == (first.chosen, first.errors)
    assert not [u for u in index if u.endswith(".metadata")]
    assert index  # the release lists are still asked for: they change with every upload


def test_wheel_metadata_cache_follows_no_cache_and_online(index):
    Resolver(read_cache=False, write_cache=False).resolve([Requirement("app==1.0")], WIN)  # --no-cache
    index.clear()
    Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    assert [u for u in index if u.endswith(".metadata")]  # nothing was saved
    index.clear()
    Resolver(read_cache=False, write_cache=True).resolve([Requirement("app==1.0")], WIN)  # --online
    assert [u for u in index if u.endswith(".metadata")]  # asked again despite the saved answer


def test_failed_metadata_reads_arent_cached(index, monkeypatch):
    def broken(url, max_bytes, timeout=20.0, attempts=4):
        raise OSError("connection reset")

    good = pypi.fetch_text
    monkeypatch.setattr(pypi, "fetch_text", broken)
    tree = Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    assert tree.errors
    monkeypatch.setattr(pypi, "fetch_text", good)
    tree = Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    assert not tree.errors
    assert tree.chosen == Resolver().resolve([Requirement("app==1.0")], WIN).chosen  # the whole tree, nothing lost


def test_a_page_that_isnt_metadata_isnt_cached(index, monkeypatch):
    # A proxy or captive portal answers 200 with its own page: no Requires-Dist, but no Metadata-Version either.
    good = pypi.fetch_text
    monkeypatch.setattr(pypi, "fetch_text", lambda url, max_bytes, timeout=20.0, attempts=4: "<html>Sign in</html>")
    Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    monkeypatch.setattr(pypi, "fetch_text", good)
    index.clear()
    tree = Resolver(read_cache=True, write_cache=True).resolve([Requirement("app==1.0")], WIN)
    assert [u for u in index if u.endswith(".metadata")]  # asked again: the portal's page wasn't kept
    assert tree.chosen == Resolver().resolve([Requirement("app==1.0")], WIN).chosen


def test_cached_metadata_entries_are_checked(index):
    url = "https://files.example/x-1.0-py3-none-any.whl.metadata"
    cache.save_metadata(pypi.index_url(), url, ["a", 1])  # type: ignore[list-item]
    assert cache.load_metadata(pypi.index_url(), url) is None
    cache.save_metadata(pypi.index_url(), url, ["a>=1"])
    assert cache.load_metadata(pypi.index_url(), url) == ["a>=1"]
    assert cache.load_metadata(pypi.index_url(), url + "x") is None
    assert url not in "".join(p.read_text() for p in cache.cache_dir().glob("*.json"))


def test_cli_deps_reads_metadata_from_the_cache_on_a_second_run(index, capsys):
    assert main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64"]) in (0, 1)
    index.clear()
    assert main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64"]) in (0, 1)
    assert not [u for u in index if u.endswith(".metadata")]
    index.clear()
    main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"])
    assert [u for u in index if u.endswith(".metadata")]


def test_cli_deps_online_asks_for_metadata_again_and_saves_it(index, capsys):
    """#415: --online skips reading the metadata cache but still writes it, so a plain run afterwards uses it."""
    args = ["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64"]
    # --online first, on an empty cache: it fetches the metadata files...
    assert main([*args, "--online"]) in (0, 1)
    first = sorted(u for u in index if u.endswith(".metadata"))
    assert first
    index.clear()
    # ...and saves them: a plain run reads them from the cache (fails if --online didn't write).
    assert main(args) in (0, 1)
    assert not [u for u in index if u.endswith(".metadata")]
    index.clear()
    # --online again asks for every one, though they're cached (fails if --online read the cache).
    assert main([*args, "--online"]) in (0, 1)
    assert sorted(u for u in index if u.endswith(".metadata")) == first


@pytest.fixture
def crypt_issues(index, monkeypatch):
    """crypt (a transitive gap on win-arm64) publishes an Issues URL in its PyPI metadata."""
    real = pypi.fetch_json

    def fetch_json(url, *args, **kwargs):
        data = real(url, *args, **kwargs)
        if "/crypt/" in url:
            data["info"]["project_urls"] = {"Issues": "https://github.com/example/crypt/issues"}
        return data

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return index


def test_deps_explain_links_a_transitive_gaps_issue_tracker(crypt_issues, capsys):
    """#412: a package --deps found gets the `ask for a wheel` link like a listed one, and only once."""
    for run in ("fresh", "from the cache"):
        crypt_issues.clear()
        main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--explain"])
        out = capsys.readouterr().out
        assert out.count("ask for a wheel: https://github.com/example/crypt/issues") == 1, run
        pinned = [u for u in crypt_issues if re.search(r"/crypt/[^/]+/json$", u)]
        # The second run's link comes from the cache: crypt's pinned release isn't asked for again.
        assert bool(pinned) == (run == "fresh"), (run, pinned)


def test_deps_json_explain_has_issues_url_for_a_transitive_gap(crypt_issues, capsys):
    main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--explain", "--all", "--no-cache"])
    rows = {p["name"]: p for p in json.loads(capsys.readouterr().out)["packages"]}
    assert rows["crypt"]["via"] == {"win-arm64": ["app", "lib", "crypt"]}
    assert rows["crypt"]["issues_url"] == "https://github.com/example/crypt/issues"
    assert rows["lib"]["issues_url"] is None  # publishes no tracker and isn't on GitHub


def test_requirements_deps_applies_marker_constraints_per_target(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text(
        'c<2; sys_platform == "win32"\nunused<2; sys_platform == "win32"\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\na\n", encoding="utf-8")
    code = main(
        [
            str(tmp_path / "requirements.txt"),
            "--deps",
            "-p",
            "3.14",
            "-t",
            "win-arm64",
            "-t",
            "linux-x86_64",
            "--json",
            "--no-cache",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    by_version = {p["version"]: p["results"] for p in data["packages"] if p["name"] == "c"}
    assert set(by_version) == {"1.5", "2.0"}
    assert by_version["1.5"]["win-arm64"] != "n/a"
    assert by_version["1.5"]["linux-x86_64"] == "n/a"
    assert by_version["2.0"]["linux-x86_64"] != "n/a"
    assert by_version["2.0"]["win-arm64"] == "n/a"
    assert "unused" not in {p["name"] for p in data["packages"]}
    assert code == 0


def test_requirements_deps_marker_constraint_without_installed_package_is_harmless(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text('unused==999; sys_platform == "win32"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\na\n", encoding="utf-8")
    assert main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]) == 0
    assert "can't follow" not in capsys.readouterr().err


def _c_versions(capsys, tmp_path, constraints, requirements="a", targets=("win-arm64", "linux-x86_64")):
    (tmp_path / "constraints.txt").write_text(constraints, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(f"-c constraints.txt\n{requirements}\n", encoding="utf-8")
    args = [str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "--json", "--no-cache", "--all"]
    assert main([*args, *(a for t in targets for a in ("-t", t))]) == 0
    rows = json.loads(capsys.readouterr().out)["packages"]
    return {t: next(p["version"] for p in rows if p["name"] == "c" and p["results"][t] != "n/a") for t in targets}


def test_requirements_deps_marker_constraint_on_an_extra_never_applies(index, capsys, tmp_path):
    # pip evaluates a constraint's marker with extra == "", so this constraint never holds.
    assert _c_versions(capsys, tmp_path, 'c<2; extra == "s3"\n') == {"win-arm64": "2.0", "linux-x86_64": "2.0"}


def test_requirements_deps_undecidable_marker_on_a_package_not_installed_is_harmless(index, capsys, tmp_path):
    assert _c_versions(capsys, tmp_path, 'unused<2; platform_release >= "5"\n') == {
        "win-arm64": "2.0",
        "linux-x86_64": "2.0",
    }


def test_requirements_deps_refuses_platform_machine_where_the_target_doesnt_fix_it(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text('c<2; platform_machine == "armv7l"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\na\n", encoding="utf-8")
    args = [str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "--no-cache"]
    assert main([*args, "-t", "linux-aarch64"]) == 0  # aarch64 decides it: the constraint doesn't apply
    capsys.readouterr()
    assert main([*args, "-t", "linux-armv7l"]) == 2
    assert 'can\'t tell whether the constraint `c<2; platform_machine == "armv7l"`' in capsys.readouterr().err


def test_requirements_deps_applies_a_marker_constraint_to_a_direct_requirement(index, capsys, tmp_path):
    versions = _c_versions(capsys, tmp_path, 'c<2; sys_platform == "win32"\n', requirements="c")
    assert versions == {"win-arm64": "1.5", "linux-x86_64": "2.0"}


def test_requirements_deps_marker_constraint_doesnt_hide_conflicting_pins(index, capsys, tmp_path):
    (tmp_path / "constraints.txt").write_text('c==1.0\nc==1.5\nc<2; sys_platform == "win32"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\nc\n", encoding="utf-8")
    assert main([str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]) == 2
    assert "c: constraints specify conflicting exact versions" in capsys.readouterr().err


def test_deps_json_explain_direct_names_each_direct_requirement(index, capsys):
    code = main(
        ["parenta", "parentb", "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--explain", "--no-suggest"]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    crypt = next(p for p in output["packages"] if p["name"] == "crypt")
    assert sorted(crypt["direct"]["win-arm64"]) == ["parenta", "parentb"]
    assert len(crypt["via"]["win-arm64"]) > 1  # `via` still gives one full path


def test_deps_json_explain_direct_extra_only_names_the_requester(index, capsys):
    code = main(["app", "plain", "--deps", "-p", "3.14", "-t", "win-arm64", "--json", "--explain", "--no-suggest"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    crypt = next(p for p in output["packages"] if p["name"] == "crypt")
    assert crypt["direct"] == {"win-arm64": ["app"]}


def test_deps_names_the_direct_requirement_of_a_package_whose_lookup_failed(index, monkeypatch, capsys, tmp_path):
    # #453: "flaky" can't be looked up, so it's unknown, but the table and --junit still name what pulls it in.
    monkeypatch.setitem(INDEX, "usesflaky", {"1.0": release(["flaky"])})
    monkeypatch.setitem(INDEX, "alsoflaky", {"1.0": release(["flaky"])})
    fetch_json = pypi.fetch_json

    def failing(url, *args, **kwargs):
        if url.startswith(f"{pypi.index_url()}/flaky/"):
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        return fetch_json(url, *args, **kwargs)

    monkeypatch.setattr(pypi, "fetch_json", failing)
    roots = [Requirement("usesflaky"), Requirement("c")]
    resolver = Resolver()
    tree = resolver.resolve(roots, WIN)
    assert "flaky" in tree.errors and "flaky" not in tree.chosen
    assert deps._direct_roots(tree, roots, resolver, WIN) == {"flaky": ("usesflaky",)}
    for display in ([], ["--markdown"]):
        args = ["usesflaky", "c", "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache", "--color", "never"]
        assert main([*args, *display]) == 1
        assert "flaky (via usesflaky)" in capsys.readouterr().out
    report = tmp_path / "report.xml"
    assert main([*args, "--junit", str(report)]) == 1
    capsys.readouterr()
    assert "(via usesflaky)" in report.read_text(encoding="utf-8")
    both = [Requirement("usesflaky"), Requirement("alsoflaky")]
    tree = resolver.resolve(both, WIN)
    assert sorted(deps._direct_roots(tree, both, resolver, WIN)["flaky"]) == ["alsoflaky", "usesflaky"]
    # The GitHub annotation names the owner and sits on its line, as a transitive gap's does.
    (tmp_path / "requirements.txt").write_text("c\nusesflaky\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    files = [str(tmp_path / "requirements.txt"), "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]
    assert main([*files, "--github-annotations"]) == 1
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::") and "title=flaky" in ln]
    assert line.startswith("::error file=requirements.txt,line=2,") and "(via usesflaky)" in line


def test_deps_names_the_direct_requirement_of_a_package_with_no_release_for_the_python(index, monkeypatch):
    # #453: "toonew" has no release for 3.14, so it's never chosen, but it still gets its owner.
    monkeypatch.setitem(INDEX, "toonew", {"1.0": release(requires_python=">=3.15")})
    monkeypatch.setitem(INDEX, "usestoonew", {"1.0": release(["toonew"])})
    roots = [Requirement("usestoonew")]
    resolver = Resolver()
    tree = resolver.resolve(roots, WIN)
    assert "toonew" in tree.errors and "toonew" not in tree.chosen
    assert deps._direct_roots(tree, roots, resolver, WIN) == {"toonew": ("usestoonew",)}


def test_diff_deps_detects_a_new_transitive_gap_on_upgrade(index, monkeypatch, capsys):
    from wheelgap.diff import main as diff_main

    monkeypatch.setitem(
        INDEX,
        "upgrade",
        {
            "1.0": release(),
            "2.0": release(["crypt>=1"]),
        },
    )
    args = ["upgrade==1.0", "upgrade==2.0", "--deps", "-p", "3.14", "-t", "win-arm64"]
    assert diff_main([*args, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"worse": 0, "new": 1}
    assert report["new"][0]["name"] == "crypt"
    assert report["new"][0]["targets"]["win-arm64"]["status"] == "source"
    assert report["new"][0]["direct"] == {"win-arm64": ["upgrade"]}
    assert diff_main(args) == 1
    assert "crypt (via upgrade) 1.0 (new)" in capsys.readouterr().out


def test_diff_deps_comparing_same_version_has_no_regression(index, capsys):
    from wheelgap.diff import main as diff_main

    assert diff_main(["app==1.0", "app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64"]) == 0
    assert capsys.readouterr().out.strip() == "Nothing got worse."


def test_diff_deps_rejects_unpinned_and_non_index_inputs_before_lookup(index, capsys, tmp_path):
    from wheelgap.diff import main as diff_main

    unsupported = [
        "app",
        "app>=1",
        "app==1.*",
        "app @ https://example.com/app.whl",
        "app==1.0; python_version < '3.15'",
    ]
    for invalid in unsupported:
        assert diff_main([invalid, "app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]) == 2
        output = json.loads(capsys.readouterr().out)
        assert output["error"]["type"] == "SourceError"
    local = tmp_path / "uv.lock"
    local.write_text("hello", encoding="utf-8")
    assert diff_main([str(local), "app==1.0", "--deps", "--json"]) == 2
    assert "SourceError" in json.loads(capsys.readouterr().out)["error"]["type"]


def test_diff_deps_resolver_refusal_exits_2(index, monkeypatch, capsys):
    from wheelgap.diff import main as diff_main

    monkeypatch.setitem(INDEX, "xy", {"1.0": release(["x", "y"])})
    assert diff_main(["app==1.0", "xy==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "DepsError"


def test_diff_deps_a_release_pypi_lacks_exits_2(index, capsys):
    from wheelgap.diff import main as diff_main

    assert diff_main(["app==1.0", "app==9.9", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "SourceError"
    assert "app==9.9" in error["message"]


def test_diff_deps_unreadable_dependencies_compare_as_unknown(index, capsys):
    from wheelgap.diff import main as diff_main

    assert diff_main(["app==1.0", "nometa==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    nometa = next(p for p in report["new"] if p["name"] == "nometa")
    assert nometa["targets"]["win-arm64"]["status"] == "unknown"


def test_diff_deps_reports_a_dependency_that_got_worse(index, monkeypatch, capsys):
    from wheelgap.diff import main as diff_main

    monkeypatch.setitem(INDEX, "dep", {"1.0": release(files=(WIN_ARM,)), "2.0": release(files=(LINUX,))})
    monkeypatch.setitem(INDEX, "up", {"1.0": release(["dep==1.0"]), "2.0": release(["dep==2.0"])})
    assert diff_main(["up==1.0", "up==2.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"worse": 1, "new": 0}
    assert report["worse"][0]["name"] == "dep"
    assert report["worse"][0]["targets"]["win-arm64"] == {"old_status": "wheel", "new_status": "source"}


def test_diff_deps_extras_and_index_variables(index, monkeypatch, capsys):
    from wheelgap.diff import main as diff_main

    monkeypatch.setitem(INDEX, "base", {"1.0": release(), "2.0": release(['crypt>=1; extra == "fast"'])})
    args = ["base==1.0", "base[fast]==2.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--json"]
    assert diff_main(args) == 1
    assert [p["name"] for p in json.loads(capsys.readouterr().out)["new"]] == ["crypt"]
    # A bad NEW spec is refused too.
    assert diff_main(["base==1.0", "base>=2", "--deps", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "SourceError"
    monkeypatch.setenv("UV_INDEX_URL", "https://example.com/simple")
    assert diff_main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "SourceError"
