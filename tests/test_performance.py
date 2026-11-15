"""Big lockfiles across many targets (#234): the caches change nothing, and a loose time budget."""

import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from wheelgap import check as check_module
from wheelgap.check import Status, check, explain
from wheelgap.sources import load_inputs
from wheelgap.targets import Target, build_targets

PYTHONS = ["3.12", "3.13", "3.14t"]


def _benchmark():
    path = Path(__file__).parents[1] / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("wheelgap_benchmark", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _benchmark()


@pytest.fixture
def big_lock(tmp_path) -> Path:
    """A small benchmark lock that still has every kind of package, so the tests stay fast."""
    path = benchmark.write_lockfile(tmp_path, 25)
    text = path.read_text(encoding="utf-8")
    for kind in (
        "-py3-none-any.whl",
        "-abi3-",
        "-cp314-cp314t-",
        "macosx_10_9_x86_64",
        "marker = ",
        "dependencies = [{",
    ):
        assert kind in text
    files = [p.files or [] for p in load_inputs([str(path)])[0].packages]
    assert any(f and not any(n.endswith(".whl") for n in f) for f in files)  # sdist only
    assert any(f and not any(n.endswith(".tar.gz") for n in f) for f in files)  # wheels only
    return path


def _targets() -> list[Target]:
    return build_targets(PYTHONS, ["everything"]) + build_targets(["3.13"], ["mobile", "pyodide"])


def _results(lock: Path, targets: list[Target]) -> list:
    """Every status and every --explain line, as plain data."""
    out = []
    for row in check(load_inputs([str(lock)]), targets):
        out.append((row.package.name, [(t.label(True), s.value) for t, s in row.results.items()]))
        files = row.package.files or []
        out.append([(t.label(True), explain(files, t)) for t in targets if row.results[t] is not Status.WHEEL])
    return out


def _uncached_tags(filename: str):
    try:
        return parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return None


@pytest.mark.parametrize("lock", ["big", "uvproj", "armgap", "uvselect", "uvworkspace"])
def test_caches_dont_change_results(lock, big_lock, fixtures, monkeypatch):
    path = big_lock if lock == "big" else fixtures / lock / "uv.lock"
    check_module._parsed_tags.cache_clear()
    cold = _results(path, _targets())
    warm = _results(path, _targets())  # the filename cache is full now, and the targets are new
    shared = _targets()
    assert _results(path, shared) == _results(path, shared) == cold == warm
    # And the same as with no filename cache at all.
    monkeypatch.setattr(check_module, "_parsed_tags", _uncached_tags)
    assert _results(path, _targets()) == cold


def test_platform_tag_set_matches_platform_tags():
    for t in _targets() + build_targets(["3.11"], ["everything"], glibc="2.17", musl="1.1", macos="10.9"):
        assert t.platform_tag_set == frozenset(t.platform_tags())


def test_explain_parses_each_wheel_once_and_works_out_platform_tags_once_per_target(big_lock, monkeypatch):
    calls: list[Target] = []
    parsed: list[str] = []
    platform_tags = Target.platform_tags

    def counted(self):
        calls.append(self)
        return platform_tags(self)

    def counted_parse(filename):
        parsed.append(filename)
        return parse_wheel_filename(filename)

    monkeypatch.setattr(Target, "platform_tags", counted)
    monkeypatch.setattr(check_module, "parse_wheel_filename", counted_parse)
    check_module._parsed_tags.cache_clear()
    targets = _targets()
    _results(big_lock, targets)
    wheels = {f for p in load_inputs([str(big_lock)])[0].packages for f in p.files or [] if f.endswith(".whl")}
    # Each wheel at most once, though --explain looks at it on every target.
    assert parsed and set(parsed) <= wheels and len(parsed) == len(set(parsed))
    assert len(calls) <= 2 * len(targets)  # Target.tags and Target.platform_tag_set, once each


def test_filename_cache_is_bounded():
    assert check_module._parsed_tags.cache_info().maxsize is not None


def test_caches_are_thread_safe(big_lock):
    """The same answers from many threads at once (wheelgap-mcp runs checks off the main thread; 3.14t in CI)."""
    targets = _targets()
    expected = _results(big_lock, targets)
    check_module._parsed_tags.cache_clear()
    shared = _targets()  # fresh targets, so their cached properties are filled concurrently too
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _results(big_lock, shared), range(8)))
    assert all(r == expected for r in results)


@pytest.mark.slow
def test_big_lockfile_time_budget(tmp_path):
    """500 packages x -t everything x three Pythons, with --explain. About 1 s on a laptop; the budget is loose
    so slow CI runners never fail it, but it catches a slow path that multiplies the time. Skip it locally
    with -m "not slow"."""
    lock = benchmark.write_lockfile(tmp_path, 500)
    start = time.perf_counter()
    out = json.loads(benchmark.run_in_process(lock, PYTHONS, explain=True))
    elapsed = time.perf_counter() - start
    assert out["summary"]["packages"] == 500
    assert len(out["targets"]) == len(build_targets(PYTHONS, ["everything"]))
    assert elapsed < 30, f"took {elapsed:.1f} s"
