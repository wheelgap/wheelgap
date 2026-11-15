"""Tests for --junit XML output."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from wheelgap.check import Row, Status
from wheelgap.cli import main
from wheelgap.junit import junit_xml
from wheelgap.sources import Package
from wheelgap.targets import build_targets


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _suites(path: Path) -> dict[str, ET.Element]:
    root = ET.parse(path).getroot()
    # ET may wrap a single suite or a testsuites element.
    if root.tag == "testsuite":
        return {root.attrib["name"]: root}
    return {el.attrib["name"]: el for el in root.findall("testsuite")}


def _case(suite: ET.Element, name: str) -> ET.Element:
    matches = [el for el in suite.findall("testcase") if el.attrib["name"] == name]
    assert len(matches) == 1, [el.attrib["name"] for el in suite.findall("testcase")]
    return matches[0]


def test_junit_armgap_fails_only_on_musl(capsys, fixtures, tmp_path, fake_pypi):
    report = tmp_path / "report.xml"
    args = [
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64,musl-aarch64",
        "--junit",
        str(report),
    ]
    code, out, err = run(capsys, *args)
    assert code == 1, err
    assert report.is_file()
    suites = _suites(report)
    assert set(suites) == {"linux-aarch64", "musl-aarch64"}
    # armlib has a linux-aarch64 wheel and only an sdist on musl, so it fails
    # the default --fail-on source on musl only.
    glibc = _case(suites["linux-aarch64"], "armlib")
    assert glibc.find("failure") is None
    assert glibc.find("skipped") is None
    musl = _case(suites["musl-aarch64"], "armlib")
    fail = musl.find("failure")
    assert fail is not None
    assert fail.get("message") == "source"
    # newglibc has no wheel for either target and no sdist: missing on both.
    assert _case(suites["linux-aarch64"], "newglibc").find("failure").get("message") == "missing"
    for suite in suites.values():
        cases = suite.findall("testcase")
        assert int(suite.get("tests")) == len(cases) == 2
        assert int(suite.get("failures")) == sum(1 for c in cases if c.find("failure") is not None)
        assert int(suite.get("skipped")) == 0
    assert suites["linux-aarch64"].get("failures") == "1"
    assert suites["musl-aarch64"].get("failures") == "2"
    # With --fail-on missing, armlib's source build is accepted on every target.
    report2 = tmp_path / "missing.xml"
    code, out, err = run(capsys, *args[:-1], str(report2), "--fail-on", "missing")
    assert code == 1, err  # newglibc is still missing
    suites = _suites(report2)
    assert _case(suites["linux-aarch64"], "armlib").find("failure") is None
    assert _case(suites["musl-aarch64"], "armlib").find("failure") is None
    for suite in suites.values():
        assert _case(suite, "newglibc").find("failure") is not None
        assert suite.get("failures") == "1"


def test_junit_skips_packages_not_needed_on_a_target(capsys, fixtures, tmp_path, fake_pypi):
    report = tmp_path / "report.xml"
    code, out, err = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-arm64",
        "--junit",
        str(report),
    )
    assert code == 1, err  # puresdist and nativeext build from source; winonly is missing on win-arm64
    suites = _suites(report)
    winonly_linux = _case(suites["linux-x86_64"], "winonly")
    skip = winonly_linux.find("skipped")
    assert skip is not None
    assert skip.get("message") == "n/a"
    assert int(suites["linux-x86_64"].get("skipped")) >= 1
    # It is installed on Windows ARM, so that cell isn't skipped; its only wheel is for win-amd64.
    winonly_arm = _case(suites["win-arm64"], "winonly")
    assert winonly_arm.find("skipped") is None
    assert winonly_arm.find("failure").get("message") == "missing"


def test_junit_is_written_alongside_json(capsys, fixtures, tmp_path, fake_pypi):
    report = tmp_path / "report.xml"
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json"]
    _, plain, _ = run(capsys, *args)
    code, out, _ = run(capsys, *args, "--junit", str(report))
    assert code == 1
    assert out == plain  # stdout is the same report
    assert set(_suites(report)) == {"musl-aarch64"}


def test_junit_write_error_is_exit_2(capsys, fixtures, tmp_path, fake_pypi):
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64"]
    code, out, err = run(capsys, *args, "--junit", str(tmp_path / "nodir" / "report.xml"))
    assert code == 2
    assert "wheelgap: error:" in err
    # With --json, the error is on stdout too, like every other exit 2.
    code, out, err = run(capsys, *args, "--json", "--junit", str(tmp_path / "nodir" / "report.xml"))
    assert code == 2
    assert "error" in json.loads(out)
    # An empty PATH (an unset CI variable) is an error, not a silently missing report.
    code, out, err = run(capsys, *args, "--junit", "")
    assert code == 2
    assert "wheelgap: error:" in err


def test_junit_names_are_unique_within_a_suite():
    (target,) = build_targets(["3.13"], ["linux-x86_64"])

    def cases(*rows):
        (suite,) = ET.fromstring(junit_xml(list(rows), [target], "source")).findall("testsuite")
        return suite, {c.get("name"): c for c in suite.findall("testcase")}

    # Two inputs lock the same version: one testcase, and the failing cell wins.
    for statuses in ([Status.WHEEL, Status.SOURCE], [Status.SOURCE, Status.NOT_NEEDED]):
        suite, by_name = cases(*(Row(Package("armlib", "1.4.0"), {target: s}) for s in statuses))
        assert list(by_name) == ["armlib"]
        assert by_name["armlib"].find("failure") is not None
        assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("1", "1", "0")
    # A pass beats skipped, whichever input comes first.
    for statuses in ([Status.NOT_NEEDED, Status.WHEEL], [Status.WHEEL, Status.NOT_NEEDED]):
        suite, by_name = cases(*(Row(Package("armlib", "1.4.0"), {target: s}) for s in statuses))
        assert by_name["armlib"].find("skipped") is None
        assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("1", "0", "0")
    # Of two failures, the one that can't install at all is the one reported.
    for statuses in ([Status.SOURCE, Status.MISSING], [Status.MISSING, Status.SOURCE]):
        suite, by_name = cases(*(Row(Package("armlib", "1.4.0"), {target: s}) for s in statuses))
        assert by_name["armlib"].find("failure").get("message") == "missing"
    # Inputs that spell the name differently are still one testcase, named as the first writes it.
    suite, by_name = cases(
        Row(Package("PyYAML", "6.0.2"), {target: Status.WHEEL}),
        Row(Package("pyyaml", "6.0.2"), {target: Status.SOURCE}),
    )
    assert list(by_name) == ["PyYAML"]
    assert by_name["PyYAML"].find("failure") is not None
    # An unpinned requirement has no version to show.
    suite, by_name = cases(
        Row(Package("armlib", None), {target: Status.WHEEL}),
        Row(Package("armlib", "2.0"), {target: Status.WHEEL}),
    )
    assert list(by_name) == ["armlib", "armlib 2.0"]
    # Two versions of one package: the version goes in the name.
    suite, by_name = cases(
        Row(Package("armlib", "1.4.0"), {target: Status.WHEEL}),
        Row(Package("armlib", "2.0"), {target: Status.MISSING}),
    )
    assert list(by_name) == ["armlib 1.4.0", "armlib 2.0"]
    assert by_name["armlib 1.4.0"].find("failure") is None
    assert by_name["armlib 2.0"].find("failure").get("message") == "missing"
    assert (suite.get("tests"), suite.get("failures")) == ("2", "1")


def test_junit_unknown_fails_and_bundled_passes():
    (target,) = build_targets(["3.13"], ["pyodide"])
    rows = [
        Row(Package("lost", "1.0"), {target: Status.UNKNOWN}),
        Row(Package("numpy", "2.2.5"), {target: Status.BUNDLED}),
    ]
    # An unknown cell fails even with --fail-on missing, as it fails the exit code; bundled always passes.
    for fail_on in ("source", "missing"):
        (suite,) = ET.fromstring(junit_xml(rows, [target], fail_on)).findall("testsuite")
        by_name = {c.get("name"): c for c in suite.findall("testcase")}
        assert by_name["lost"].find("failure").get("message") == "unknown"
        assert by_name["numpy"].find("failure") is None
        assert by_name["numpy"].find("skipped") is None
        assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("2", "1", "0")


def test_junit_suite_names_include_the_python_when_there_are_several():
    targets = build_targets(["3.13", "3.14t"], ["linux-x86_64"])
    rows = [Row(Package("armlib", "1.4.0"), {t: Status.WHEEL for t in targets})]
    suites = ET.fromstring(junit_xml(rows, targets, "source")).findall("testsuite")
    assert [s.get("name") for s in suites] == ["3.13 linux-x86_64", "3.14t linux-x86_64"]
    # classname matches the suite, so dashboards keep the two Pythons apart.
    assert [s.find("testcase").get("classname") for s in suites] == ["3.13 linux-x86_64", "3.14t linux-x86_64"]


def test_junit_ignored_target_is_skipped_not_failed(capsys, fixtures, tmp_path):
    report = tmp_path / "report.xml"
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--junit", str(report)]
    code, _, err = run(capsys, *args, "--ignore", "armlib:musl-aarch64,newglibc:linux-aarch64")
    assert code == 1, err  # newglibc is still missing on musl-aarch64
    suites = _suites(report)
    musl = _case(suites["musl-aarch64"], "armlib")
    assert musl.find("failure") is None
    assert musl.find("skipped").get("message") == "ignored"
    assert _case(suites["linux-aarch64"], "armlib").find("skipped") is None  # a wheel on an ignored target passes
    assert _case(suites["linux-aarch64"], "newglibc").find("skipped").get("message") == "ignored"
    assert _case(suites["musl-aarch64"], "newglibc").find("failure").get("message") == "missing"
    assert (suites["musl-aarch64"].get("failures"), suites["musl-aarch64"].get("skipped")) == ("1", "1")


def test_junit_advisory_gap_is_skipped_not_failed(capsys, fixtures, tmp_path):
    report = tmp_path / "report.xml"
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--junit", str(report)]
    code, _, err = run(capsys, *args, "--advisory", "musl-aarch64")
    assert code == 1, err  # newglibc is still missing on linux-aarch64
    suites = _suites(report)
    musl = suites["musl-aarch64"]
    assert _case(musl, "armlib").find("skipped").get("message") == "advisory: source"
    assert _case(musl, "newglibc").find("skipped").get("message") == "advisory: missing"
    assert (musl.get("failures"), musl.get("skipped")) == ("0", "2")
    assert _case(suites["linux-aarch64"], "newglibc").find("failure").get("message") == "missing"
    # --fail-on missing still turns a source build into a pass, on an advisory target too.
    run(capsys, *args, "--advisory", "musl-aarch64", "--fail-on", "missing")
    musl = _suites(report)["musl-aarch64"]
    assert _case(musl, "armlib").find("skipped") is None
    assert (musl.get("failures"), musl.get("skipped")) == ("0", "1")


def test_junit_advisory_gap_beats_a_pass_from_another_input():
    (target,) = build_targets(["3.13"], ["win-arm64"])
    for statuses, worst in (
        ([Status.WHEEL, Status.SOURCE], "source"),
        ([Status.SOURCE, Status.WHEEL], "source"),
        # As on a required target, a package that can't install beats a source build, whichever input is first.
        ([Status.SOURCE, Status.MISSING], "missing"),
        ([Status.MISSING, Status.SOURCE], "missing"),
        ([Status.UNKNOWN, Status.SOURCE], "unknown"),
    ):
        rows = [Row(Package("armlib", "1.4.0"), {target: s}) for s in statuses]
        (suite,) = ET.fromstring(junit_xml(rows, [target], "source", frozenset({target}))).findall("testsuite")
        (case,) = suite.findall("testcase")
        assert case.find("skipped").get("message") == f"advisory: {worst}"
        assert (suite.get("failures"), suite.get("skipped")) == ("0", "1")


def test_junit_ignored_beats_advisory(capsys, fixtures, tmp_path):
    report = tmp_path / "report.xml"
    args = [str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--junit", str(report)]
    code, _, err = run(capsys, *args, "--advisory", "musl-aarch64", "--ignore", "armlib:musl-aarch64")
    assert code == 1, err  # newglibc is missing on linux-aarch64
    musl = _suites(report)["musl-aarch64"]
    # An ignored gap is accepted outright, so it says so rather than reporting it as an advisory gap.
    assert _case(musl, "armlib").find("skipped").get("message") == "ignored"
    assert _case(musl, "newglibc").find("skipped").get("message") == "advisory: missing"
