"""MkDocs hook: render docs/wheelgap-output.schema.json as a table on the Output page.

The schema is the source of truth for `--json` (tests/test_cli.py checks the output against it), so the page
is built from it rather than copied by hand. Only the standard library is used, so tests/test_docs.py can
import this file without MkDocs installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).resolve().parent.parent / "wheelgap-output.schema.json"
PLACEHOLDER = "<!-- wheelgap-output-schema -->"
PAGE = "output.md"


def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not ref:
        return node
    if not ref.startswith("#/"):
        raise ValueError(f"{SCHEMA.name}: can't follow $ref {ref!r}")
    target: Any = root
    for part in ref[2:].split("/"):
        target = target[part]
    # Keys next to $ref (a description) win over the definition's own.
    return {**target, **{k: v for k, v in node.items() if k != "$ref"}}


def _type(node: dict[str, Any]) -> str:
    if "enum" in node:
        return " / ".join(f"`{v}`" for v in node["enum"])
    kind = node.get("type", "")
    kinds = kind if isinstance(kind, list) else [kind]
    return " or ".join(k for k in kinds if k)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _present(name: str, parent: dict[str, Any]) -> str:
    return "always" if name in parent.get("required", ()) else "sometimes"


def rows(schema: dict[str, Any] | None = None) -> list[tuple[str, str, str, str]]:
    """(key path, type, present, description) for every key in the schema, depth first."""
    root = schema if schema is not None else json.loads(SCHEMA.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str, str]] = []

    def walk(node: dict[str, Any], path: str, present: str) -> None:
        node = _resolve(node, root)
        if path:
            out.append((f"`{path}`", _type(node), present, node.get("description", "")))
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}" if path else name, _present(name, node))
        items = node.get("items")
        if isinstance(items, dict) and (_resolve(items, root).get("properties") or "$ref" in items):
            walk_children(items, f"{path}[]")
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            # A map keyed by target label: its values are the interesting part.
            walk(extra, f"{path}.<target>", "per target")

    def walk_children(node: dict[str, Any], path: str) -> None:
        node = _resolve(node, root)
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}", _present(name, node))

    walk(root, "", "always")
    return out


def render(schema: dict[str, Any] | None = None) -> str:
    root = schema if schema is not None else json.loads(SCHEMA.read_text(encoding="utf-8"))
    lines = [
        _cell(root.get("description", "")),
        "",
        "| key | type | present | description |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {key} | {kind} | {present} | {_cell(desc)} |" for key, kind, present, desc in rows(root)]
    return "\n".join(lines)


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    if page.file.src_uri != PAGE:
        return markdown
    if PLACEHOLDER not in markdown:
        raise ValueError(f"{PAGE} has no {PLACEHOLDER} line for the JSON schema reference")
    return markdown.replace(PLACEHOLDER, render())
