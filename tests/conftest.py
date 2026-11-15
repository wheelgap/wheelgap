from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Fake PyPI: name -> {version: [filenames]}, plus the "latest" version for unpinned lookups.
FAKE_INDEX = {
    "fastlib": {"2.0.0": ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]},
    "unpinned": {"4.2": ["unpinned-4.2-py3-none-any.whl"]},
    "winonly": {"311": ["winonly-311-cp313-cp313-win_amd64.whl"]},
    "purewheel": {"3.1": ["purewheel-3.1-py3-none-any.whl"]},
    "nowheels": {"0.9": ["nowheels-0.9.tar.gz"]},
    # Wheels for 3.14 on Linux and Windows, but for 3.15 on Windows only: it blocks the move to 3.15 on Linux.
    "newpylag": {
        "1.0": [
            "newpylag-1.0-cp314-cp314-manylinux_2_17_x86_64.whl",
            "newpylag-1.0-cp314-cp314-win_amd64.whl",
            "newpylag-1.0-cp315-cp315-win_amd64.whl",
            "newpylag-1.0.tar.gz",
        ]
    },
    "cp314only": {"1.0": ["cp314only-1.0-cp314-cp314-manylinux_2_17_x86_64.whl"]},  # no sdist: missing on 3.15
    "forked": {"0.9": ["forked-0.9.tar.gz"], "1.0": ["forked-1.0.tar.gz"]},
    "ranged": {
        "1.5": ["ranged-1.5-py3-none-any.whl"],
        "1.9": ["ranged-1.9.tar.gz"],
        "2.0": ["ranged-2.0-py3-none-any.whl"],
    },
    "pythonranged": {
        "1.0": [{"filename": "pythonranged-1.0-py3-none-any.whl", "requires_python": ">=3.8"}],
        "2.0": [{"filename": "pythonranged-2.0-py3-none-any.whl", "requires_python": ">=3.10"}],
    },
    "yanked": {"1.0": [{"filename": "yanked-1.0-py3-none-any.whl", "yanked": True}]},
    "halfyanked": {"2.0": [{"filename": "halfyanked-2.0-py3-none-any.whl", "yanked": True}, "halfyanked-2.0.tar.gz"]},
    # Only Linux wheels for a newer glibc / musl / macOS than a flag can lower the floor to: fine at the defaults,
    # source with --glibc 2.28 (e.g. RHEL 8), --musl 1.1 or --macos 13.0 (diff tests; #286).
    "glibclag": {
        "1.0": ["glibclag-1.0-cp313-cp313-manylinux_2_28_x86_64.whl", "glibclag-1.0.tar.gz"],
        "1.1": ["glibclag-1.1-cp313-cp313-manylinux_2_34_x86_64.whl", "glibclag-1.1.tar.gz"],
    },
    # Only a wheel for a newer musl / macOS than a flag can lower the floor to: fine at the defaults (musl 1.2,
    # macOS 14.0), source at --musl 1.1 or --macos 13.0 (diff tests; #286).
    "musllag": {
        "1.0": ["musllag-1.0-cp313-cp313-musllinux_1_1_x86_64.whl", "musllag-1.0.tar.gz"],
        "1.1": ["musllag-1.1-cp313-cp313-musllinux_1_2_x86_64.whl", "musllag-1.1.tar.gz"],
    },
    "maclag": {
        "1.0": ["maclag-1.0-cp313-cp313-macosx_13_0_arm64.whl", "maclag-1.0.tar.gz"],
        "1.1": ["maclag-1.1-cp313-cp313-macosx_14_0_arm64.whl", "maclag-1.1.tar.gz"],
    },
    # Releases for --suggest. On Python 3.13, 1.0 builds from source everywhere, 1.1 adds a Windows wheel, 1.2 Windows
    # and musl (both arches) wheels, 1.3 a macOS arm64 one; 1.3's pure wheel is for 3.14 on. The yanked 1.4 and the
    # pre-release 2.0rc1 have a pure wheel, and neither counts.
    "laggard": {
        "1.0": ["laggard-1.0.tar.gz"],
        "1.1": ["laggard-1.1-cp313-cp313-win_amd64.whl", "laggard-1.1.tar.gz"],
        "1.2": [
            "laggard-1.2-cp313-cp313-win_amd64.whl",
            "laggard-1.2-cp313-cp313-musllinux_1_2_aarch64.whl",
            "laggard-1.2-cp313-cp313-musllinux_1_2_x86_64.whl",
            "laggard-1.2.tar.gz",
        ],
        "1.3": [
            "laggard-1.3-cp313-cp313-macosx_14_0_arm64.whl",
            {"filename": "laggard-1.3-py3-none-any.whl", "requires_python": ">=3.14"},
            "laggard-1.3.tar.gz",
        ],
        "2.0rc1": ["laggard-2.0rc1-py3-none-any.whl", "laggard-2.0rc1.tar.gz"],
        # Last, so it's the "latest" an unpinned lookup gets: PyPI's latest is never a pre-release.
        "1.4": [{"filename": "laggard-1.4-py3-none-any.whl", "yanked": True}, "laggard-1.4.tar.gz"],
    },
    # --suggest must not name a release that drops a wheel the checked one has: 1.1 has Windows's but not Linux's.
    "wheeldrop": {
        "1.0": ["wheeldrop-1.0-cp313-cp313-manylinux_2_17_x86_64.whl", "wheeldrop-1.0.tar.gz"],
        "1.1": ["wheeldrop-1.1-cp313-cp313-win_amd64.whl", "wheeldrop-1.1.tar.gz"],
        "1.2": [
            "wheeldrop-1.2-cp313-cp313-manylinux_2_17_x86_64.whl",
            "wheeldrop-1.2-cp313-cp313-win_amd64.whl",
            "wheeldrop-1.2.tar.gz",
        ],
    },
    "wheellost": {
        "1.0": ["wheellost-1.0-cp313-cp313-manylinux_2_17_x86_64.whl", "wheellost-1.0.tar.gz"],
        "1.1": ["wheellost-1.1-cp313-cp313-win_amd64.whl", "wheellost-1.1.tar.gz"],
    },
}


# Fake anaconda.org API for --conda-forge (https://api.anaconda.org/package/conda-forge/<name>): name -> its files
# (see conda_file), or an exception the lookup raises. A name not listed is a 404. Tests change it with
# monkeypatch.setitem.
def conda_file(version, subdir, *depends, labels=("main",)):
    return {"version": version, "labels": list(labels), "attrs": {"subdir": subdir, "depends": list(depends)}}


def cp(minor, ft=""):
    """The depends of a compiled CPython 3.<minor> build: python_abi names the ABI."""
    return ("python", f"python_abi 3.{minor}.* *_cp3{minor}{ft}")


FAKE_CONDA_FORGE: dict[str, list[dict] | Exception] = {
    "nowheels": [
        conda_file("0.9", "linux-64", *cp(13)),
        conda_file("0.9", "linux-aarch64", *cp(13)),
        conda_file("0.9", "osx-arm64", *cp(13), labels=("main", "broken")),  # marked broken: never counts
        conda_file("0.9", "win-64", *cp(12)),  # another Python
        conda_file("0.8", "linux-ppc64le", *cp(13)),  # another version
    ],
    "winonly": [conda_file("311", "noarch", "python >=3.10", "python")],
}


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: a timing test (skip it locally with -m 'not slow')")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests shouldn't depend on the developer's colour or index settings, or on running in GitHub Actions."""
    import os

    from wheelgap.origin import index_env

    for name in ("NO_COLOR", "FORCE_COLOR", "WHEELGAP_INDEX_URL", "GITHUB_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in list(os.environ.items()):  # pip and uv index settings (see origin.index_env)
        if index_env({name: value}):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def lookup_cache(monkeypatch, tmp_path_factory) -> Path:
    """Each test gets an empty cache of its own, so no test reads or writes the developer's real one."""
    from wheelgap import cache

    folder = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(cache, "cache_dir", lambda: folder)
    return folder


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP request, except to a test's own server on localhost, fails at once (for example --explain's
    sdist downloads, on a fixture whose files the lock lists)."""
    from urllib.parse import urlsplit

    from wheelgap import pypi

    real_open = pypi._OPENER.open

    def refuse(req, timeout=None):
        url = getattr(req, "full_url", req)
        if urlsplit(url).hostname in pypi.LOOPBACK:  # a test's own local server
            return real_open(req, timeout=timeout)
        raise OSError(f"no network in tests: {url}")

    monkeypatch.setattr(pypi._OPENER, "open", refuse)


class FakeFiles:
    """Files served over fake HTTPS (see fake_files): .files maps a filename to its bytes (or to an exception to raise,
    or a function that returns the response), .calls lists URLs."""

    def __init__(self):
        self.files: dict[str, object] = {}
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []


@pytest.fixture
def fake_files(monkeypatch):
    """Serves fake_files.files at https://files.example/<filename>, the URLs fake_pypi gives every file, and at
    https://files.pythonhosted.org/<any path>/<filename>, as a lock gives them."""
    import io
    import urllib.error

    from wheelgap import pypi

    served = FakeFiles()

    def open_(req, timeout=None):
        url = req.full_url
        served.calls.append(url)
        served.timeouts.append(timeout)
        host, _, path = url.removeprefix("https://").partition("/")
        name = path.rpartition("/")[2] if host in ("files.example", "files.pythonhosted.org") else ""
        if name not in served.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        body = served.files[name]
        if isinstance(body, BaseException):
            raise body
        return body() if callable(body) else io.BytesIO(body)  # type: ignore[arg-type]

    monkeypatch.setattr(pypi._OPENER, "open", open_)
    return served


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


class FakeTracker:
    """The coverage tracker's package files (wheelgap.notes), served in place of wheelgap.github.io."""

    def __init__(self):
        self.files: dict[str, object] = {}  # by name; any other name is a 404
        self.calls: list[str] = []  # the URLs asked for
        self.options: list[tuple[float, int]] = []  # (timeout, attempts) of each request
        self.respond = None  # a function of the name that answers instead (it may raise or sleep)

    def add(self, name: str, *notes: dict, version: str = "1.0") -> None:
        self.files[name] = {"schema": 1, "name": name, "version": version, "notes": list(notes)}

    def fetch_json(self, url, timeout, attempts):
        import urllib.error

        from wheelgap import notes

        self.calls.append(url)
        self.options.append((timeout, attempts))
        name = url.removeprefix(notes.TRACKER + "/").removesuffix(".json")
        if self.respond is not None:
            return self.respond(name)
        if name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.files[name]


@pytest.fixture(autouse=True)
def tracker(monkeypatch) -> FakeTracker:
    """Every test gets a fake tracker with no files (each lookup is a 404), so --notes never reaches the network.
    fake_pypi serves it too; a test that patches pypi.fetch_json itself gets no notes."""
    from wheelgap import notes, pypi

    fake, real = FakeTracker(), pypi.fetch_json

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        if url.startswith(notes.TRACKER + "/"):
            return fake.fetch_json(url, timeout, attempts)
        return real(url, timeout, attempts, max_bytes, deadline)

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return fake


@pytest.fixture
def fake_pypi(monkeypatch, tracker):
    import urllib.error

    from wheelgap import notes, pypi

    calls = []

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        if url.startswith(notes.TRACKER + "/"):
            return tracker.fetch_json(url, timeout, attempts)
        calls.append(url)
        if url.startswith("https://api.anaconda.org/package/conda-forge/"):
            assert max_bytes is not None, "anaconda.org answers can be large: cap them"
            conda = FAKE_CONDA_FORGE.get(url.rsplit("/", 1)[-1])
            if conda is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if isinstance(conda, Exception):
                raise conda
            return {"files": conda}
        # Served at whatever WHEELGAP_INDEX_URL points at, so tests can assert which index was asked.
        parts = url.removeprefix(pypi.index_url() + "/").removesuffix("/json").split("/")
        name = parts[0]
        if name not in FAKE_INDEX:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        versions = FAKE_INDEX[name]
        version = parts[1] if len(parts) > 1 else next(reversed(versions))
        files = versions.get(version)
        if files is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        def entries(fs):
            # With a URL, as PyPI gives them; fake_files serves it.
            return [
                {"url": f"https://files.example/{e['filename']}", **e}
                for e in (f if isinstance(f, dict) else {"filename": f} for f in fs)
            ]

        data = {"info": {"version": version}, "urls": entries(files)}
        if len(parts) == 1:  # the project endpoint lists every release, like PyPI
            data["releases"] = {v: entries(fs) for v, fs in versions.items()}
        return data

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return calls


def _simple_files(name):
    """Every file of every version in one flat list, like a PEP 691 project page."""
    files = []
    for fs in FAKE_INDEX[name].values():
        for f in fs:
            entry = f if isinstance(f, dict) else {"filename": f}
            item = {
                "filename": entry["filename"],
                "url": f"https://files.example/{entry['filename']}",
                "hashes": {},
            }
            if entry.get("yanked"):
                item["yanked"] = True  # PEP 592: a bool or a reason string
            if "requires_python" in entry:
                item["requires-python"] = entry["requires_python"]  # PEP 691 spelling
            files.append(item)
    return files


class FakeSimple:
    """A fake PEP 691 index: .calls records requests, .html_only makes it serve only PEP 503 HTML."""

    def __init__(self):
        self.calls = []  # (url, auth) pairs
        self.html_only = False


@pytest.fixture
def fake_simple(monkeypatch):
    import urllib.error
    from urllib.parse import urlsplit

    from wheelgap import pypi

    index = FakeSimple()

    def fetch_simple(url, auth=None, timeout=20.0):
        # Credentials must be split off before the fetch layer (they go in `auth`).
        assert "@" not in urlsplit(url).netloc, "credentials leaked into the request URL"
        index.calls.append((url, auth))
        if index.html_only:
            raise pypi.IndexFormatError(urlsplit(url).hostname or "index")
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name not in FAKE_INDEX:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return {"meta": {"api-version": "1.0"}, "name": name, "files": _simple_files(name)}

    monkeypatch.setattr(pypi, "fetch_simple", fetch_simple)
    return index
