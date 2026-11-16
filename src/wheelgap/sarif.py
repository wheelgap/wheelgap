"""SARIF 2.1.0 report for code scanning (GitHub's Security tab and pull-request annotations, via upload-sarif)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .check import Row, Status, explain_lines
from .github import Locator, relative_path
from .sources import Source
from .targets import Target

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
DOCS = "https://wheelgap.github.io/wheelgap/output/#sarif"
# How many --explain lines go into a result's message; --explain prints them all.
REASON_LINES = 3

RULES = {
    Status.SOURCE: {
        "id": "wheelgap/source",
        "name": "BuildsFromSource",
        "shortDescription": {"text": "No wheel for this target: pip builds the package from source"},
        "fullDescription": {
            "text": "The release has no wheel this target accepts, so installing it builds the sdist, which needs "
            "a compiler and the package's build dependencies on the machine."
        },
        "helpUri": DOCS,
        "defaultConfiguration": {"level": "error"},
    },
    Status.MISSING: {
        "id": "wheelgap/missing",
        "name": "CannotInstall",
        "shortDescription": {"text": "No wheel or usable sdist for this target: the package can't be installed"},
        "fullDescription": {
            "text": "The release has no wheel this target accepts, and no sdist it can build, so installing it "
            "fails on this target."
        },
        "helpUri": DOCS,
        "defaultConfiguration": {"level": "error"},
    },
}
WORDS = {Status.SOURCE: "builds from source on", Status.MISSING: "can't install on"}


def level(status: Status, target: Target, fail_on: str, advisory: frozenset[Target]) -> str:
    """`note` on an --advisory target, `error` when the cell fails the run, `warning` for a source build that
    --fail-on missing allows."""
    if target in advisory:
        return "note"
    if status is Status.SOURCE and fail_on == "missing":
        return "warning"
    return "error"


def sarif_log(
    rows: list[Row],
    targets: list[Target],
    sources: list[Source],
    fail_on: str,
    advisory: frozenset[Target] = frozenset(),
) -> dict:
    """One result per package and gap target (`source` or `missing`; not `unknown`, a failed lookup, and not
    a target `--ignore NAME:TARGET` ignores), located at the package's entry in its input file (relative to
    GITHUB_WORKSPACE, else the current directory) where the reader knows the line, else at the file's first line; a
    package named on the command line, or in a file outside the repository, has no location."""
    multi_py = len({t.python for t in targets}) > 1
    rule_ids = list(RULES)
    locator = Locator(sources)
    # Each gap's file, relative to the repository, and line: (None, None) for a package named on the command line or
    # read from a file outside the repository, which GitHub couldn't show.
    gaps: list[tuple[Row, str | None, int | None]] = []
    for row in rows:
        if row.ok:
            continue
        located = locator.locate(row.package)
        path = relative_path(located[0]) if located is not None else None
        gaps.append((row, path, located[1] if located is not None and path is not None else None))
    # A package locked at several versions in one file gets the version in its fingerprint, so each is an alert.
    versions: dict[tuple[str | None, str], set[str | None]] = {}
    for row, path, _ in gaps:
        versions.setdefault((path, row.package.key), set()).add(row.package.version)
    results = []
    for row, path, line in gaps:
        pkg = row.package
        name = f"{pkg.name} {pkg.version}" if pkg.version else pkg.name
        locations = []
        if path is not None:
            # GitHub shows a result only with a line, so one in a file the reader has no line for is at line 1.
            physical: dict = {
                "artifactLocation": {"uri": quote(path, safe="/"), "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": line if line is not None else 1},
            }
            locations.append({"physicalLocation": physical})
        ident = f"{path or ''}:{pkg.key}"
        if len(versions[(path, pkg.key)]) > 1:
            ident += f"=={pkg.version}"
        for target in targets:
            status = row.results[target]
            if status not in RULES or target in row.ignored:
                continue
            label = target.label(multi_py)
            reasons = explain_lines(
                list(pkg.files or ()),
                target,
                yanked=pkg.yanked,
                requires_python_rejected=pkg.requires_python_rejected,
                limit=REASON_LINES,
                no_binary=pkg.no_binary,
                only_binary=pkg.only_binary,
            )
            text = f"{name} {WORDS[status]} {label}" + (" (advisory)" if target in advisory else "") + "."
            if reasons:
                text += " " + "; ".join(reasons) + "."
            result: dict = {
                "ruleId": RULES[status]["id"],
                "ruleIndex": rule_ids.index(status),
                "level": level(status, target, fail_on, advisory),
                "message": {"text": text},
                # The same file, package and target is the same alert from one run to the next, whatever its version
                # (unless the file locks the package at several versions).
                "partialFingerprints": {"wheelgap/v1": f"{ident}:{label}"},
                "properties": {"package": pkg.name, "version": pkg.version, "target": label, "status": status.value},
            }
            if locations:
                result["locations"] = locations
            results.append(result)
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "wheelgap",
                        "version": __version__,
                        "informationUri": "https://github.com/wheelgap/wheelgap",
                        "rules": list(RULES.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def write_sarif(
    path: str,
    rows: list[Row],
    targets: list[Target],
    sources: list[Source],
    fail_on: str,
    advisory: frozenset[Target] = frozenset(),
) -> None:
    Path(path).write_text(json.dumps(sarif_log(rows, targets, sources, fail_on, advisory), indent=2) + "\n", "utf-8")
