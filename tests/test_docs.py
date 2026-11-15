"""Keep the documentation site (docs/, built by MkDocs) from drifting away from the code.

These read the Markdown sources directly, so they don't need MkDocs installed.
"""

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest

import wheelgap
from wheelgap import cli, targets
from wheelgap.check import EXPLAIN_LIMIT

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def headings(path: Path) -> list[str]:
    """The Markdown headings in a file, without the leading #s. Fenced code blocks are left out, so a shell
    comment like '# --json' isn't a heading."""
    out = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (m := re.match(r"#{1,6} (.*)", line)):
            out.append(m.group(1))
    return out


def heading_tokens(page: str) -> set[str]:
    """The `code` spans in a docs page's headings, e.g. {"-p", "--python"} for '### `-p`, `--python` {#python}'."""
    return {token for heading in headings(DOCS / page) for token in re.findall(r"`([^`]+)`", heading)}


def help_flags(capsys, monkeypatch, *argv: str) -> set[str]:
    """The --flags that `wheelgap ARGV --help` prints, unwrapped and without color."""
    monkeypatch.setenv("COLUMNS", "1000")  # argparse wraps at the terminal width, and may break at a hyphen
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("PYTHON_COLORS", "0")
    with pytest.raises(SystemExit) as exc:
        cli.main([*argv, "--help"])
    assert exc.value.code == 0
    flags = set(FLAG.findall(ANSI.sub("", capsys.readouterr().out)))
    assert "--help" in flags
    return flags


def test_cli_reference_has_a_section_for_every_flag(capsys, monkeypatch):
    """docs/cli.md has a heading naming each flag `wheelgap --help` lists, and no heading for a flag that's gone."""
    flags = help_flags(capsys, monkeypatch)
    documented = {t for t in heading_tokens("cli.md") if t.startswith("--")}
    assert flags - documented == set(), "add a section to docs/cli.md for these flags"
    assert documented - flags == set(), "docs/cli.md has sections for flags wheelgap --help doesn't list"


def test_diff_page_names_every_diff_flag(capsys, monkeypatch):
    """docs/diff.md mentions each flag `wheelgap diff --help` lists."""
    text = (DOCS / "diff.md").read_text(encoding="utf-8")
    missing = {f for f in help_flags(capsys, monkeypatch, "diff") - {"--help"} if f"`{f}" not in text}
    assert missing == set(), "describe these flags in docs/diff.md"


def test_configuration_page_has_a_section_for_every_key():
    """docs/configuration.md has a heading for each [tool.wheelgap] key load_config() accepts, and only those."""
    keys = set(cli.LIST_KEYS) | set(cli.STR_KEYS) | set(cli.BOOL_KEYS)
    documented = heading_tokens("configuration.md")
    assert keys - documented == set(), "add a section to docs/configuration.md for these keys"
    assert documented - keys == set(), "docs/configuration.md has sections for keys [tool.wheelgap] doesn't accept"


@pytest.mark.parametrize("key", sorted({*cli.LIST_KEYS, *cli.STR_KEYS, *cli.BOOL_KEYS}))
def test_every_documented_config_key_is_accepted(tmp_path, key):
    """The key lists the test above checks are the ones load_config() reads (an unknown key is an error)."""
    value = {"android-api": "24", "fail-on": "missing"}.get(key, "x")
    if key in cli.BOOL_KEYS:
        value = True
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"[tool.wheelgap]\n{json.dumps(key)} = {json.dumps(value)}\n", encoding="utf-8")
    assert key in cli.load_config(pyproject)


def load_schema_hook():
    spec = importlib.util.spec_from_file_location("schema_reference", DOCS / "hooks" / "schema_reference.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_output_page_renders_every_schema_key():
    """docs/output.md gets the --json reference from docs/wheelgap-output.schema.json, with every key in it: as many
    rows ending in a key's name as the schema has properties of that name (`name` is in several objects)."""
    hook = load_schema_hook()
    assert hook.PLACEHOLDER in (DOCS / "output.md").read_text(encoding="utf-8")
    schema = json.loads((DOCS / "wheelgap-output.schema.json").read_text(encoding="utf-8"))

    def names(node):
        if isinstance(node, dict):
            yield from node.get("properties", {})
            for value in node.values():
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)

    # Each $defs entry is rendered once per $ref to it, so this assumes every definition is referenced once.
    refs = re.findall(r'"\$ref": "#/\$defs/(\w+)"', json.dumps(schema))
    assert sorted(refs) == sorted(schema.get("$defs", {}))
    expected = Counter(names(schema))
    rendered = Counter(key.strip("`").rsplit(".", 1)[-1] for key, _, _, _ in hook.rows(schema))
    rendered = Counter({name: n for name, n in rendered.items() if not name.endswith(("]", ">"))})
    assert rendered == expected


def github_anchor(heading: str) -> str:
    """The id GitHub gives a Markdown heading: lowercase, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {github_anchor(heading) for heading in headings(path)}


def test_readme_keeps_the_anchors_other_files_link_to():
    """llms.txt and others deep-link to README sections on GitHub (and PyPI shows the README): keep them."""
    readme = anchors(ROOT / "README.md")
    linked = set()
    for name in ("llms.txt", "docs/recipes.md", "docs/mcp.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        linked |= set(re.findall(r"github\.com/wheelgap/wheelgap(?:/blob/main/README\.md)?#([\w-]+)", text))
    assert linked, "expected llms.txt to link to README sections"
    assert linked - readme == set()


def test_llms_txt_links_to_docs_that_exist():
    """llms.txt links to files under docs/ on GitHub, some with an #anchor: keep those paths and headings."""
    text = (ROOT / "llms.txt").read_text(encoding="utf-8")
    links = re.findall(r"github\.com/wheelgap/wheelgap/blob/main/(docs/[\w./-]+?)(?:#([\w-]+))?\)", text)
    assert links
    for path, anchor in links:
        assert (ROOT / path).is_file(), path
        if anchor:
            assert anchor in anchors(ROOT / path), f"{path}#{anchor}"


def test_readme_keeps_the_mcp_registry_marker():
    """The MCP Registry checks that the PyPI package's README names the server."""
    name = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["name"]
    assert f"<!-- mcp-name: {name} -->" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_version_markers_name_the_current_release():
    """Docs mark what isn't released yet as "after X.Y.Z" (or "not in X.Y.Z"), X.Y.Z being the version on main. When
    a release bumps the version, this fails until those markers are removed or updated."""
    stale = []
    for path in [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for version in re.findall(r"(?i)\b(?:after|not in) (\d+\.\d+\.\d+)\b", line):
                if version != wheelgap.__version__:
                    stale.append(f"{path.relative_to(ROOT)}:{n}: {version}")
    assert stale == [], f"wheelgap is {wheelgap.__version__}"


def documented_defaults():
    glibc, musl, macos, ios, api = (
        targets.DEFAULT_GLIBC,
        targets.DEFAULT_MUSL,
        targets.DEFAULT_MACOS,
        targets.DEFAULT_IOS,
        targets.DEFAULT_ANDROID_API,
    )
    platforms = targets.DEFAULT_PLATFORMS
    spelled = ", ".join(f"`{p}`" for p in platforms[:-1]) + f" and `{platforms[-1]}`"
    native = ", ".join(f"`{p}`" for p in targets.NATIVE_PLATFORMS[:-1]) + f" and `{targets.NATIVE_PLATFORMS[-1]}`"
    return [
        ("cli.md", f"`{','.join(platforms)}`"),
        ("cli.md", f"`linux-*` targets (default {glibc})"),
        ("cli.md", f"`musl-*` targets (default {musl})"),
        ("cli.md", f"`macos-*` targets (default {macos})"),
        ("cli.md", f"`ios-*` targets (default {ios})"),
        ("cli.md", f"`android-*` targets (default {api})"),
        ("configuration.md", f"Default {glibc}."),
        ("configuration.md", f"Default {musl}."),
        ("configuration.md", f"Default {macos}."),
        ("configuration.md", f"Default {ios}."),
        ("configuration.md", f"Default {api}."),
        ("targets.md", f"| `--glibc X.Y` | `glibc` | {glibc} |"),
        ("targets.md", f"| `--musl X.Y` | `musl` | {musl} |"),
        ("targets.md", f"| `--macos X.Y` | `macos` | {macos} |"),
        ("targets.md", f"| `--ios X.Y` | `ios` | {ios} |"),
        ("targets.md", f"| `--android-api N` | `android-api` | {api} |"),
        ("targets.md", f"up to `--glibc` (default {glibc})"),
        ("targets.md", f"up to `--musl` (default {musl})"),
        ("targets.md", f"up to `--macos` (default {macos})"),
        ("targets.md", f"up to `--ios` (default {ios})"),
        ("targets.md", f"up to `--android-api` (default {api})"),
        ("targets.md", f"the built-in defaults: {spelled}."),
        ("targets.md", f"the eight common native platforms: {native}."),
        ("getting-started.md", f"on {spelled}."),
        ("faq.md", f"glibc {glibc}, musl {musl}, macOS {macos}, iOS {ios} and Android API {api}"),
        ("output.md", f"(up to {EXPLAIN_LIMIT} per target"),
        ("python-api.md", f"(the command line shows {EXPLAIN_LIMIT})"),
    ]


@pytest.mark.parametrize(("page", "text"), documented_defaults())
def test_docs_state_the_current_defaults(page, text):
    """Where a page states a default (targets, libc and OS floors, the --explain limit), it's the one in the code.
    Whitespace is normalized, since the Markdown is wrapped."""
    assert text in " ".join((DOCS / page).read_text(encoding="utf-8").split())
