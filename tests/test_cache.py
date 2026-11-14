import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import FAKE_INDEX

from wheelgap import cache, check_packages, pypi
from wheelgap.cache import cache_dir as real_cache_dir
from wheelgap.cli import main
from wheelgap.sources import Package

FASTLIB = "https://pypi.org/pypi/fastlib/2.0.0/json"
FASTLIB_FILES = ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def pinned(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("fastlib==2.0.0\n", encoding="utf-8")
    return str(path)


def lookup(name, version):
    return pypi.fill(Package(name, version), read_cache=True, write_cache=True)


def test_repeat_run_reads_pinned_lookups_from_the_cache(capsys, fake_pypi, pinned, lookup_cache):
    first = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == [FASTLIB]
    second = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == [FASTLIB]  # no second lookup
    assert second == first and first[0] == 0
    assert len(list(lookup_cache.glob("*.json"))) == 1


@pytest.mark.parametrize("flag", ["--online", "--no-cache"])
def test_online_and_no_cache_look_up_again(capsys, fake_pypi, pinned, lookup_cache, flag):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64")
    code, _, _ = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", flag)
    assert code == 0
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_online_saves_its_fresh_answer_for_later_runs(capsys, fake_pypi, pinned, monkeypatch):
    def statuses(*flags):
        code, out, _ = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json", *flags)
        return json.loads(out)["packages"][0]["results"]["linux-x86_64"]

    monkeypatch.setitem(FAKE_INDEX["fastlib"], "2.0.0", ["fastlib-2.0.0.tar.gz"])
    assert statuses() == "source"  # cached
    monkeypatch.setitem(FAKE_INDEX["fastlib"], "2.0.0", ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl"])
    assert statuses() == "source"  # still the cached answer
    assert statuses("--online") == "wheel"  # asked again...
    assert statuses() == "wheel"  # ...and saved, so a plain run doesn't go back to the older answer
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_no_cache_neither_reads_nor_writes(capsys, fake_pypi, pinned, lookup_cache):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--no-cache")
    assert not list(lookup_cache.iterdir())


def test_an_expired_entry_is_looked_up_again(fake_pypi, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    lookup("fastlib", "2.0.0")
    now += cache.MAX_AGE - 1
    assert lookup("fastlib", "2.0.0").files
    assert fake_pypi == [FASTLIB]
    now += 1  # 24 hours old
    assert lookup("fastlib", "2.0.0").files
    assert fake_pypi == [FASTLIB, FASTLIB]
    now += 1  # the fresh answer was saved again
    lookup("fastlib", "2.0.0")
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_an_entry_from_the_future_is_a_miss(fake_pypi, monkeypatch):
    monkeypatch.setattr(cache, "_now", lambda: 2_000_000_000.0)
    lookup("fastlib", "2.0.0")
    monkeypatch.setattr(cache, "_now", lambda: 1_000_000_000.0)  # the clock went back
    lookup("fastlib", "2.0.0")
    assert fake_pypi == [FASTLIB, FASTLIB]


@pytest.mark.parametrize("name,version", [("nosuchpkg", "1.0"), ("fastlib", "9.9")])
def test_not_found_is_not_cached(fake_pypi, lookup_cache, name, version):
    for _ in range(2):
        assert "not on index" in lookup(name, version).error
    assert fake_pypi.count(f"https://pypi.org/pypi/{name}/{version}/json") == 2
    assert not list(lookup_cache.iterdir())


@pytest.mark.parametrize(
    "response", [None, {}, {"info": {}}, {"urls": "x"}, {"urls": None}, {"urls": [{"name": "no-filename.whl"}]}]
)
def test_an_odd_response_is_not_cached(monkeypatch, lookup_cache, response):
    calls = []

    def fetch_json(url, timeout=20.0):
        calls.append(url)
        return response

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    lookup("fastlib", "2.0.0")
    lookup("fastlib", "2.0.0")
    assert len(calls) == 2  # asked again, not answered from the cache
    assert not list(lookup_cache.iterdir())


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"{}",
        b'{"saved": "today", "files": []}',
        b'{"saved": true, "files": []}',
        b'{"saved": 1800000000, "files": "x"}',
        b'{"saved": 1800000000, "files": [{"name": "no-filename.whl"}]}',
    ],
)
def test_a_corrupt_entry_is_a_miss(fake_pypi, monkeypatch, lookup_cache, content):
    monkeypatch.setattr(cache, "_now", lambda: 1_800_000_000.0)
    lookup("fastlib", "2.0.0")
    (entry,) = lookup_cache.glob("*.json")
    entry.write_bytes(content)
    pkg = lookup("fastlib", "2.0.0")
    assert pkg.error is None and pkg.files == FASTLIB_FILES
    assert fake_pypi == [FASTLIB, FASTLIB]
    lookup("fastlib", "2.0.0")  # and the fresh answer replaced it
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_an_unwritable_cache_never_fails_a_run(capsys, fake_pypi, pinned, monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(cache, "cache_dir", lambda: blocker / "wheelgap")  # mkdir fails on every OS
    for _ in range(2):
        code, out, err = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
        assert code == 0 and not err
        assert json.loads(out)["packages"][0]["results"] == {"linux-x86_64": "wheel"}
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_yanked_files_stay_marked_when_cached(fake_pypi):
    first = lookup("halfyanked", "2.0")
    second = lookup("halfyanked", "2.0")
    assert len(fake_pypi) == 1
    assert second.files == first.files
    assert second.yanked == first.yanked == frozenset({"halfyanked-2.0-py3-none-any.whl"})


def test_requires_python_is_kept_when_cached(fake_pypi, lookup_cache):
    lookup("pythonranged", "2.0")
    (entry,) = lookup_cache.glob("*.json")
    content = json.loads(entry.read_text(encoding="utf-8"))
    assert content["files"] == [{"filename": "pythonranged-2.0-py3-none-any.whl", "requires_python": ">=3.10"}]


def test_unpinned_and_range_lookups_are_not_cached(fake_pypi, lookup_cache):
    for _ in range(2):
        pypi.fill_all(
            [Package("unpinned", None), Package("ranged", None, spec=">=1,<2")], read_cache=True, write_cache=True
        )
    assert len(fake_pypi) == 4
    assert not list(lookup_cache.iterdir())


def test_simple_index_lookups_are_not_cached(fake_simple, lookup_cache):
    for _ in range(2):
        pkg = Package("fastlib", "2.0.0")
        pypi.fill_all([pkg], simple_index="https://user:secret@index.example/simple", read_cache=True, write_cache=True)
        assert pkg.error is None and pkg.files
    assert len(fake_simple.calls) == 2
    assert not list(lookup_cache.iterdir())


def test_the_python_api_does_not_cache(fake_pypi, lookup_cache):
    for _ in range(2):
        check_packages([("fastlib", "2.0.0")])
    assert fake_pypi == [FASTLIB, FASTLIB]
    assert not list(lookup_cache.iterdir())


def test_cache_entries_hold_no_urls_or_credentials(monkeypatch, lookup_cache):
    base = "https://user:s3cret-token@mirror.example/pypi"
    monkeypatch.setenv("WHEELGAP_INDEX_URL", base)
    calls = []

    def fetch_json(url, timeout=20.0):
        calls.append(url)
        signed = "https://files.mirror.example/fastlib-2.0.0.tar.gz?sig=s3cret-signature"
        return {"info": {"version": "2.0.0"}, "urls": [{"filename": "fastlib-2.0.0.tar.gz", "url": signed}]}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    lookup("fastlib", "2.0.0")
    assert lookup("fastlib", "2.0.0").files == ["fastlib-2.0.0.tar.gz"]
    assert calls == [f"{base}/fastlib/2.0.0/json"]
    (entry,) = lookup_cache.glob("*.json")
    text = entry.read_text(encoding="utf-8")
    assert "s3cret" not in entry.name and "s3cret" not in text and "mirror.example" not in text
    assert json.loads(text)["files"] == [{"filename": "fastlib-2.0.0.tar.gz"}]


def test_credentials_are_not_part_of_the_key(monkeypatch, lookup_cache):
    calls = []

    def fetch_json(url, timeout=20.0):
        calls.append(url)
        return {"urls": [{"filename": "fastlib-2.0.0.tar.gz"}]}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    for base in ("https://alice:one@mirror.example/pypi", "https://bob:two@mirror.example/pypi"):
        monkeypatch.setenv("WHEELGAP_INDEX_URL", base)
        assert lookup("fastlib", "2.0.0").files == ["fastlib-2.0.0.tar.gz"]
    assert calls == ["https://alice:one@mirror.example/pypi/fastlib/2.0.0/json"]  # the second came from the cache
    (entry,) = lookup_cache.glob("*.json")
    assert entry == cache._path("https://mirror.example/pypi", "fastlib", "2.0.0")


def test_name_and_version_are_part_of_the_key(fake_pypi):
    pins = [("fastlib", "2.0.0"), ("purewheel", "3.1"), ("ranged", "1.5"), ("ranged", "2.0")]
    for _ in range(2):
        files = {pin: lookup(*pin).files for pin in pins}
        assert files == {pin: FAKE_INDEX[pin[0]][pin[1]] for pin in pins}  # each its own answer
    assert fake_pypi == [f"https://pypi.org/pypi/{name}/{version}/json" for name, version in pins]


def test_the_index_is_part_of_the_key(fake_pypi, monkeypatch):
    lookup("fastlib", "2.0.0")
    monkeypatch.setattr(pypi, "index_url", lambda: "https://mirror.example/pypi")
    lookup("fastlib", "2.0.0")  # not on the fake mirror, but asked: another index's answer isn't reused
    assert fake_pypi[:2] == [FASTLIB, "https://mirror.example/pypi/fastlib/2.0.0/json"]


def test_entries_are_written_atomically_in_the_cache_folder(fake_pypi, monkeypatch, lookup_cache):
    made, replaced = [], []
    mkstemp, replace = tempfile.mkstemp, os.replace

    def spy_mkstemp(*args, **kwargs):
        made.append(kwargs["dir"])
        return mkstemp(*args, **kwargs)

    def spy_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        return replace(src, dst)

    monkeypatch.setattr(cache.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(cache.os, "replace", spy_replace)
    lookup("fastlib", "2.0.0")
    ((src, dst),) = replaced
    assert made == [lookup_cache] and src.parent == dst.parent == lookup_cache  # same folder: an atomic rename
    assert list(lookup_cache.glob("*.json")) == [dst]  # the temp file is gone


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_the_cache_folder_is_private(fake_pypi, monkeypatch, tmp_path):
    folder = tmp_path / "home" / "wheelgap" / "pinned-v2"
    monkeypatch.setattr(cache, "cache_dir", lambda: folder)
    lookup("fastlib", "2.0.0")
    assert folder.stat().st_mode & 0o777 == folder.parent.stat().st_mode & 0o777 == 0o700
    (entry,) = folder.glob("*.json")
    assert entry.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "platform,env,expected",
    [
        ("linux", {"XDG_CACHE_HOME": str(Path("/xdg").absolute())}, Path("/xdg").absolute()),
        ("linux", {"XDG_CACHE_HOME": "relative/dir"}, Path.home() / ".cache"),
        ("linux", {"XDG_CACHE_HOME": ""}, Path.home() / ".cache"),
        ("darwin", {"XDG_CACHE_HOME": str(Path("/xdg").absolute())}, Path.home() / "Library" / "Caches"),
        ("win32", {"LOCALAPPDATA": str(Path("/local").absolute())}, Path("/local").absolute()),
        ("win32", {"LOCALAPPDATA": ""}, Path.home() / "AppData" / "Local"),
        ("win32", {"LOCALAPPDATA": "relative\\dir"}, Path.home() / "AppData" / "Local"),
    ],
)
def test_cache_dir_per_platform(monkeypatch, platform, env, expected):
    monkeypatch.setattr(cache.sys, "platform", platform)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert real_cache_dir() == expected / "wheelgap" / "pinned-v2"


def test_prune_removes_expired_entries_and_keeps_fresh_ones(fake_pypi, monkeypatch, lookup_cache):
    now = 1_800_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    lookup("fastlib", "2.0.0")
    (old,) = lookup_cache.glob("*.json")
    now += cache.MAX_AGE
    lookup("ranged", "1.5")  # saving prunes: the last prune, on the first save, was a day ago
    (fresh,) = lookup_cache.glob("*.json")
    assert fresh != old


def test_prune_runs_at_most_once_a_day(fake_pypi, monkeypatch, lookup_cache):
    now = 1_800_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    lookup("fastlib", "2.0.0")  # saving prunes, and leaves the marker
    (entry,) = lookup_cache.glob("*.json")
    os.utime(entry, (now - cache.MAX_AGE, now - cache.MAX_AGE))  # expired
    now += cache.PRUNE_EVERY - 1
    cache.prune()
    assert list(lookup_cache.glob("*.json")) == [entry]  # the last prune was under a day ago
    now += 1
    cache.prune()
    assert not list(lookup_cache.glob("*.json"))


def test_prune_never_fails(monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(cache, "cache_dir", lambda: blocker / "wheelgap")  # can't be listed on any OS
    cache.prune()
    monkeypatch.setattr(cache, "cache_dir", lambda: tmp_path / "missing")
    cache.prune()


def test_clear_cache_empties_the_folder(capsys, fake_pypi, pinned, lookup_cache):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64")
    assert list(lookup_cache.glob("*.json"))
    code, out, err = run(capsys, "--clear-cache")
    assert code == 0 and not out
    assert err == f"wheelgap: removed 1 cached lookup from {lookup_cache}\n"
    assert not lookup_cache.exists()
    assert fake_pypi == [FASTLIB]  # nothing was checked


def test_clear_cache_with_inputs_clears_then_runs(capsys, fake_pypi, pinned, lookup_cache):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64")
    code, _, err = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--clear-cache")
    assert code == 0 and err.startswith("wheelgap: removed 1 cached lookup")
    assert fake_pypi == [FASTLIB, FASTLIB]  # looked up again
    assert len(list(lookup_cache.glob("*.json"))) == 1  # and saved again


def test_clear_cache_with_no_cache_folder(capsys, lookup_cache):
    lookup_cache.rmdir()
    assert run(capsys, "--clear-cache") == (0, "", f"wheelgap: removed 0 cached lookups from {lookup_cache}\n")


ENTRY_NAME = "0" * 64 + ".json"


def test_prune_deletes_only_its_own_old_files(monkeypatch, lookup_cache):
    now = 1_800_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    old = now - cache.MAX_AGE
    names = [ENTRY_NAME, ".tmp-abc.json", "notes.json", "README", "1" * 64 + ".json"]
    for name in names:
        (lookup_cache / name).write_text("{}", encoding="utf-8")
        os.utime(lookup_cache / name, (old, old))
    os.utime(lookup_cache / ("1" * 64 + ".json"), (now, now))  # a fresh entry
    cache.prune()
    left = sorted(p.name for p in lookup_cache.iterdir())
    # The old entry and the leftover of an interrupted save go; someone else's files and the fresh entry stay.
    assert left == sorted(["notes.json", "README", "1" * 64 + ".json", "last-prune"])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_prune_marker_is_private(lookup_cache):
    cache.prune()
    assert (lookup_cache / "last-prune").stat().st_mode & 0o777 == 0o600


def _symlinked_cache(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / ENTRY_NAME
    victim.write_text("{}", encoding="utf-8")
    os.utime(victim, (0, 0))  # ancient
    link = tmp_path / "pinned-v2"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("can't create symlinks here")
    monkeypatch.setattr(cache, "cache_dir", lambda: link)
    return link, victim


def test_prune_and_clear_never_follow_a_symlinked_folder(capsys, monkeypatch, tmp_path):
    link, victim = _symlinked_cache(tmp_path, monkeypatch)
    cache.prune()
    assert victim.exists() and not (link / "last-prune").exists()
    code, out, err = run(capsys, "--clear-cache")
    assert code == 2 and not out
    assert "is a link or not a folder" in err
    assert victim.exists()


def test_clear_leaves_other_files_and_the_folder(capsys, lookup_cache):
    (lookup_cache / ENTRY_NAME).write_text("{}", encoding="utf-8")
    (lookup_cache / "notes.json").write_text("{}", encoding="utf-8")
    code, _, err = run(capsys, "--clear-cache")
    assert code == 0 and err == f"wheelgap: removed 1 cached lookup from {lookup_cache}\n"
    assert [p.name for p in lookup_cache.iterdir()] == ["notes.json"]


def test_no_home_folder_never_fails_a_run(capsys, fake_pypi, pinned, monkeypatch):
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(cache, "cache_dir", no_home)
    code, _, _ = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert fake_pypi == [FASTLIB]


def test_clear_cache_with_list_targets_still_lists(capsys, lookup_cache):
    code, out, err = run(capsys, "--clear-cache", "--list-targets")
    assert code == 0 and "linux-x86_64" in out and err.startswith("wheelgap: removed")
