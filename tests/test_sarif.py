"""--sarif PATH (#401): a SARIF 2.1.0 report for code scanning, checked against the vendored OASIS schema."""

import json
import re
import shutil
from pathlib import Path

import pytest

from wheelgap.check import Row, Status
from wheelgap.cli import main
from wheelgap.sarif import sarif_log
from wheelgap.sources import Package, Source
from wheelgap.targets import Target

# tests/fixtures/sarif-schema-2.1.0.json is the OASIS schema, unchanged (git blob 0f58372b), as of the commit that last
# changed it upstream:
# https://github.com/oasis-tcs/sarif-spec/blob/a560296ca8c921f3bdb8d4a8db57ab83dae968a7/sarif-2.1/schema/sarif-schema-2.1.0.json
SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "sarif-schema-2.1.0.json").read_text(encoding="utf-8"))
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
ANNOTATIONS = {"$schema", "id", "title", "description", "default", "format"}  # format: not asserted, as by default


def check(value, schema, where="$"):
    """Validate against the draft-04 keywords the SARIF schema uses; a keyword this doesn't know fails the test."""
    known = {
        "$ref",
        "type",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "pattern",
        "oneOf",
        "anyOf",
        "definitions",
    }
    assert set(schema) <= known | ANNOTATIONS, f"{where}: unchecked {set(schema) - known - ANNOTATIONS}"
    if "$ref" in schema:
        prefix, _, name = schema["$ref"].rpartition("/")
        assert prefix == "#/definitions", schema["$ref"]
        check(value, SCHEMA["definitions"][name], where)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if isinstance(value, bool):
            assert "boolean" in names, (where, value)
        else:
            assert (
                value is None and "null" in names or isinstance(value, tuple(TYPES[n] for n in names if n in TYPES))
            ), (where, value)
    if "enum" in schema:
        assert value in schema["enum"], (where, value)
    if "minimum" in schema:
        assert value >= schema["minimum"], (where, value)
    if "maximum" in schema:
        assert value <= schema["maximum"], (where, value)
    if "pattern" in schema and isinstance(value, str):
        assert re.search(schema["pattern"], value), (where, value)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            passed = 0
            for option in schema[key]:
                try:
                    check(value, option, where)
                    passed += 1
                except AssertionError:
                    pass
            assert passed == 1 if key == "oneOf" else passed >= 1, (where, key, value)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value), (where, sorted(value))
        for key, item in value.items():
            if key in properties:
                check(item, properties[key], f"{where}.{key}")
            else:
                extra = schema.get("additionalProperties", True)
                assert extra is not False, f"{where}: {key} isn't in the schema"
                if isinstance(extra, dict):
                    check(item, extra, f"{where}.{key}")
    if isinstance(value, list):
        if "minItems" in schema:
            assert len(value) >= schema["minItems"], where
        if schema.get("uniqueItems"):
            dumped = [json.dumps(v, sort_keys=True) for v in value]
            assert len(dumped) == len(set(dumped)), where
        if "items" in schema:
            for i, item in enumerate(value):
                check(item, schema["items"], f"{where}[{i}]")


def valid(log):
    """The log's results, after checking it against the schema, and that each located result has a line, which
    GitHub needs to show it."""
    check(log, SCHEMA)
    results = log["runs"][0]["results"]
    for r in results:
        for location in r.get("locations", []):
            physical = location["physicalLocation"]
            assert physical["artifactLocation"]["uriBaseId"] == "%SRCROOT%"
            assert physical["region"]["startLine"] >= 1
    return results


def sarif(tmp_path, *args):
    out = tmp_path / "out.sarif"
    code = main([*args, "--sarif", str(out)])
    log = json.loads(out.read_text(encoding="utf-8"))
    return code, log, valid(log)


ARMGAP = ("-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--offline")


def test_one_result_per_package_and_gap_target_at_its_lockfile_line(tmp_path, fixtures, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(fixtures.parent.parent))
    code, log, results = sarif(tmp_path, str(fixtures / "armgap"), *ARMGAP)
    assert code == 1
    assert "armlib" in capsys.readouterr().out  # the usual output is still printed
    driver = log["runs"][0]["tool"]["driver"]
    assert driver["name"] == "wheelgap" and [r["id"] for r in driver["rules"]] == [
        "wheelgap/source",
        "wheelgap/missing",
    ]
    got = [(r["properties"]["package"], r["properties"]["target"], r["ruleId"], r["level"]) for r in results]
    assert got == [
        ("armlib", "musl-aarch64", "wheelgap/source", "error"),
        ("newglibc", "linux-aarch64", "wheelgap/missing", "error"),
        ("newglibc", "musl-aarch64", "wheelgap/missing", "error"),
    ]
    lock = (fixtures / "armgap" / "uv.lock").read_text(encoding="utf-8").splitlines()
    for r in results:
        (location,) = r["locations"]
        physical = location["physicalLocation"]
        assert physical["artifactLocation"]["uri"] == "tests/fixtures/armgap/uv.lock"
        assert lock[physical["region"]["startLine"] - 1] == f'name = "{r["properties"]["package"]}"'
        assert driver["rules"][r["ruleIndex"]]["id"] == r["ruleId"]
    # The --explain reason is in the message.
    assert results[1]["message"]["text"].startswith("newglibc 2.0 can't install on linux-aarch64. ")
    assert "needs glibc 2.39" in results[1]["message"]["text"]
    assert len({json.dumps(r["partialFingerprints"]) for r in results}) == 3
    assert results[0]["partialFingerprints"] == {"wheelgap/v1": "tests/fixtures/armgap/uv.lock:armlib:musl-aarch64"}


def test_advisory_is_a_note_and_allowed_source_builds_a_warning(tmp_path, fixtures):
    _, _, results = sarif(
        tmp_path, str(fixtures / "armgap"), *ARMGAP, "--advisory", "musl-aarch64", "--fail-on", "missing"
    )
    levels = {(r["properties"]["package"], r["properties"]["target"]): r["level"] for r in results}
    assert levels == {
        ("armlib", "musl-aarch64"): "note",
        ("newglibc", "linux-aarch64"): "error",
        ("newglibc", "musl-aarch64"): "note",
    }
    assert "(advisory)" in next(r for r in results if r["level"] == "note")["message"]["text"]
    _, _, results = sarif(tmp_path, str(fixtures / "armgap"), *ARMGAP, "--fail-on", "missing")
    assert [r["level"] for r in results] == ["warning", "error", "error"]


def test_ignored_targets_have_no_result(tmp_path, fixtures):
    _, _, results = sarif(tmp_path, str(fixtures / "armgap"), *ARMGAP, "--ignore", "newglibc:linux-aarch64")
    assert ("newglibc", "linux-aarch64") not in {
        (r["properties"]["package"], r["properties"]["target"]) for r in results
    }
    assert len(results) == 2


def test_no_gaps_is_an_empty_run(tmp_path, fixtures):
    code, log, results = sarif(
        tmp_path, str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64", "--offline", "--ignore", "newglibc"
    )
    assert results == [] and log["version"] == "2.1.0"


def test_a_package_named_on_the_command_line_has_no_location(tmp_path, fake_pypi):
    code, _, results = sarif(tmp_path, "nowheels", "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1 and results and all("locations" not in r for r in results)


def test_alongside_json(tmp_path, fixtures, capsys):
    code, _, results = sarif(tmp_path, str(fixtures / "armgap"), *ARMGAP, "--json")
    assert code == 1 and len(results) == 3
    assert json.loads(capsys.readouterr().out)["packages"]


@pytest.mark.parametrize("path", ["", "missing-dir/out.sarif"])
def test_a_path_that_cant_be_written_is_an_input_error(tmp_path, fixtures, capsys, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    assert main([str(fixtures / "armgap"), *ARMGAP, "--sarif", path]) == 2
    assert "wheelgap: error:" in capsys.readouterr().err


def test_a_file_outside_the_repository_has_no_location(tmp_path, fixtures, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _, _, results = sarif(tmp_path, str(fixtures / "armgap"), *ARMGAP)
    assert results and all("locations" not in r for r in results)


def test_two_lockfiles_with_the_same_gap_are_separate_alerts(tmp_path, fixtures, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    locks = []
    for folder in ("a", "b c"):  # a space, which the uri percent-encodes
        (tmp_path / folder).mkdir()
        locks.append(str(shutil.copy(fixtures / "armgap" / "uv.lock", tmp_path / folder / "uv.lock")))
    _, _, results = sarif(tmp_path, *locks, *ARMGAP)
    assert len(results) == 6
    assert len({r["partialFingerprints"]["wheelgap/v1"] for r in results}) == 6
    uris = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in results}
    assert uris == {"a/uv.lock", "b%20c/uv.lock"}


ARM = Target(3, 13, False, "linux-aarch64")


def test_a_package_at_two_versions_in_one_file_is_two_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    lock = tmp_path / "uv.lock"
    lock.write_bytes(
        b'version = 1\n\n[[package]]\nname = "dup"\nversion = "1.0"\n\n[[package]]\nname = "dup"\nversion = "2.0"\n'
    )
    old, new = Package("dup", "1.0", files=[]), Package("dup", "2.0", files=[])
    source = Source("uv.lock", packages=[old, new], path=lock, kind="uv")
    rows = [Row(old, {ARM: Status.MISSING}), Row(new, {ARM: Status.MISSING})]
    results = valid(sarif_log(rows, [ARM], [source], "source"))
    assert [r["locations"][0]["physicalLocation"]["region"]["startLine"] for r in results] == [4, 8]
    assert [r["partialFingerprints"]["wheelgap/v1"] for r in results] == [
        "uv.lock:dup==1.0:linux-aarch64",
        "uv.lock:dup==2.0:linux-aarch64",
    ]


def test_a_file_without_a_line_is_located_at_line_1(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "app"\ndependencies = ["armlib"]\n', encoding="utf-8")
    package = Package("armlib", "1.4.0", files=[])
    source = Source("pyproject.toml", packages=[package], path=pyproject, kind="pyproject")
    (result,) = valid(sarif_log([Row(package, {ARM: Status.SOURCE})], [ARM], [source], "source"))
    physical = result["locations"][0]["physicalLocation"]
    assert physical == {
        "artifactLocation": {"uri": "pyproject.toml", "uriBaseId": "%SRCROOT%"},
        "region": {"startLine": 1},
    }
