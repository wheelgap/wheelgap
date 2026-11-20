import io
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import FAKE_INDEX, FIXTURES
from test_deps import INDEX, LINUX, WIN_AMD, WIN_ARM, index, release  # noqa: F401  (index: --deps' fake PyPI)
from test_dist import LINUX_WHEEL, SDIST, WIN_ARM_WHEEL, dist

from wheelgap import __version__, cli, mcp
from wheelgap.suggest_targets import Found, Suggestions, Unreadable


def run(*messages: object, raw: bytes = b"") -> list:
    """Send messages to a fresh server over in-memory stdio; return the parsed replies, one per output line."""
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages) + raw)
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines]


def init(version: str = "2025-06-18") -> dict:
    return {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": version}}


def call(tool: str, msg_id: int = 1, **arguments: object) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


@pytest.mark.parametrize(
    ("asked", "agreed"),
    [("2024-11-05", "2024-11-05"), ("2025-11-25", "2025-11-25"), ("2099-01-01", "2025-11-25"), (None, "2025-11-25")],
)
def test_initialize_negotiates_version(asked, agreed):
    (reply,) = run(init(asked))
    result = reply["result"]
    assert reply["id"] == 0
    assert result["protocolVersion"] == agreed
    assert result["serverInfo"] == {"name": "wheelgap", "version": __version__}
    assert result["capabilities"] == {"tools": {"listChanged": False}}


def test_notifications_get_no_reply_and_unknown_methods_are_errors():
    replies = run(
        init(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    )
    assert [r["id"] for r in replies] == [0, 2, 3]
    assert replies[1]["result"] == {}
    assert replies[2]["error"]["code"] == mcp.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json\n", mcp.PARSE_ERROR),
        (b"\xff\xfe\n", mcp.PARSE_ERROR),
        (b'{"id": 1, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}\n', mcp.INVALID_PARAMS),
        (b"[]\n", mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": ' + b"9" * 5000 + b', "method": "ping"}\n', mcp.PARSE_ERROR),
        (b"[" * 100_000 + b"\n", mcp.PARSE_ERROR),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["x"]}}\n', mcp.INVALID_PARAMS),
    ],
)
def test_bad_messages_get_errors_and_the_server_keeps_going(raw, code):
    replies = run(raw=raw + b'\n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\r\n')
    assert replies[0]["error"]["code"] == code
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_responses_from_the_client_get_no_reply():
    replies = run({"jsonrpc": "2.0", "id": 3, "result": {}}, {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert [r["id"] for r in replies] == [4]


def test_utf8_bom_on_the_first_line():
    (reply,) = run(raw=b"\xef\xbb\xbf" + json.dumps(init()).encode() + b"\n")
    assert reply["result"]["serverInfo"]["name"] == "wheelgap"


def test_batches_reply_with_a_list():
    (reply,) = run([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])
    assert reply == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tools_list_has_valid_schemas():
    (_, reply) = run(init(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert set(tools) == {
        "check_package",
        "check_lockfile",
        "check_dockerfile",
        "check_dist",
        "diff_lockfiles",
        "explain_install_failure",
        "suggest_targets",
        "list_targets",
    }
    assert tools["suggest_targets"]["annotations"] == mcp._READ_ONLY | {"openWorldHint": False}
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert set(tool["inputSchema"].get("required", [])) <= set(tool["inputSchema"]["properties"])


def test_check_package(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="winonly", python="3.13", targets=["win-amd64", "linux/arm64"]))
    result = reply["result"]
    data = json.loads(result["content"][0]["text"])
    assert result["isError"] is False
    assert result["structuredContent"] == data
    assert data["results"] == {"3.13 win-amd64": "wheel", "3.13 linux-aarch64": "missing"}
    assert data["ok"] is False
    assert data["gaps"] == ["3.13 linux-aarch64"]
    assert data["version"] == "311"
    assert data["assumes"] == {"glibc": "2.36", "musl": "1.2", "macos": "14.0", "ios": "13.0", "android_api": "24"}
    assert data["suggest"] == {} and data["last_with_wheel"] == {}
    # The second request reads the older releases for the way out, as --suggest does.
    assert fake_pypi == ["https://pypi.org/pypi/winonly/json"] * 2


WHEELSTOPPED = {
    "1.0": ["wheelstopped-1.0-cp313-cp313-win_arm64.whl", "wheelstopped-1.0.tar.gz"],
    "1.1": ["wheelstopped-1.1-cp313-cp313-win_arm64.whl", "wheelstopped-1.1.tar.gz"],
    "1.2": ["wheelstopped-1.2.tar.gz"],
    "2.0": ["wheelstopped-2.0.tar.gz"],
}


def test_check_package_names_the_last_older_release_with_a_wheel(fake_pypi, monkeypatch):
    # As --explain --suggest --json does: by gap target no newer release has a wheel for, the newest older one with one.
    monkeypatch.setitem(FAKE_INDEX, "wheelstopped", WHEELSTOPPED)
    targets = ["win-arm64", "linux-riscv64"]
    (_, reply) = run(init(), call("check_package", name="wheelstopped", python="3.13", targets=targets))
    data = reply["result"]["structuredContent"]
    assert data["version"] == "2.0"
    assert data["gaps"] == ["3.13 win-arm64", "3.13 linux-riscv64"]
    assert data["suggest"] == {}  # no newer release has a wheel for either gap
    assert data["last_with_wheel"] == {"3.13 win-arm64": "1.1"}


LATEWHEEL = {
    "1.0": ["latewheel-1.0-cp313-cp313-win_amd64.whl", "latewheel-1.0.tar.gz"],
    "2.0": [
        "latewheel-2.0-cp313-cp313-win_amd64.whl",
        "latewheel-2.0-cp313-cp313-win_arm64.whl",
        "latewheel-2.0.tar.gz",
    ],
}


def test_check_package_suggests_the_newer_release_with_the_wheel(fake_pypi, monkeypatch):
    # As --explain --suggest --json's suggest: by gap target a newer release has a wheel for, that release.
    monkeypatch.setitem(FAKE_INDEX, "latewheel", LATEWHEEL)
    (_, reply) = run(
        init(), call("check_package", name="latewheel", version="1.0", python="3.13", targets=["win-arm64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["results"] == {"3.13 win-arm64": "source"}
    assert data["suggest"] == {"3.13 win-arm64": "2.0"}
    assert data["last_with_wheel"] == {}


def test_check_package_dependencies_gaps_get_the_way_out(index, monkeypatch):  # noqa: F811  (the index fixture)
    # crypt 1.0 has no Windows ARM wheel but 1.1 does; tool's dropped has one only in its older release.
    monkeypatch.setitem(INDEX, "crypt", INDEX["crypt"] | {"1.1": release(files=(LINUX, WIN_AMD, WIN_ARM))})
    monkeypatch.setitem(INDEX, "both", {"1.0": release(["crypt==1.0", "dropped"])})
    lookups = []
    real = mcp.release_suggest.look_up_all

    def spy(wanted, deadline=mcp.release_suggest.DEADLINE):
        lookups.append(sorted(wanted))
        return real(wanted, deadline)

    monkeypatch.setattr(mcp.release_suggest, "look_up_all", spy)
    (_, reply) = run(
        init(),
        call("check_package", name="both", version="1.0", python="3.14", targets=["win-arm64"], dependencies=True),
    )
    data = reply["result"]["structuredContent"]
    by_name = {d["name"]: d for d in data["dependency_gaps"]}
    assert by_name["crypt"]["suggest"] == {"3.14 win-arm64": "1.1"}
    assert by_name["crypt"]["last_with_wheel"] == {}
    assert by_name["dropped"]["suggest"] == {}
    assert by_name["dropped"]["last_with_wheel"] == {"3.14 win-arm64": "1.0"}
    # The whole tree's releases are read in one look_up_all call, not one per package.
    assert lookups == [["crypt", "dropped"]]
    assert index.count("https://pypi.org/pypi/crypt/json") == 2  # once for the tree, once in the batch
    assert index.count("https://pypi.org/pypi/dropped/json") == 2


def test_check_package_no_way_out_for_targets_it_never_checked(fake_pypi, monkeypatch):
    # 1.5 isn't on the index: every target is unknown, so there's no gap to suggest a way out of (as the CLI).
    monkeypatch.setitem(FAKE_INDEX, "latewheel", LATEWHEEL)
    (_, reply) = run(
        init(), call("check_package", name="latewheel", version="1.5", python="3.13", targets=["win-arm64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["results"] == {"3.13 win-arm64": "unknown"}
    assert "suggest" not in data and "last_with_wheel" not in data


@pytest.mark.usefixtures("index")
def test_check_package_way_out_covers_every_release_it_resolved_to(monkeypatch):
    # split resolves to 1.0 on 3.14 and 2.0 on 3.15, both without a Windows ARM wheel: each has its own way out.
    import test_deps

    monkeypatch.setitem(
        test_deps.INDEX,
        "split",
        {
            "1.0": test_deps.release(files=(test_deps.LINUX,)),
            "2.0": test_deps.release(files=(test_deps.LINUX,), requires_python=">=3.15"),
        },
    )
    newer = {"1.0": "1.5", "2.0": "2.5"}

    def fake(results):
        return {
            id(r): mcp.release_suggest.Suggestion(found={t: newer[r.version] for t in r.gaps})
            for r in results
            if r.name == "split" and r.gaps
        }

    monkeypatch.setattr(mcp, "_suggestions", fake)
    (_, reply) = run(
        init(), call("check_package", name="split", python="3.14,3.15", targets=["win-arm64"], dependencies=True)
    )
    data = reply["result"]["structuredContent"]
    assert data["versions"] == ["1.0", "2.0"]
    assert data["suggest"] == {"3.14 win-arm64": "1.5", "3.15 win-arm64": "2.5"}
    assert data["last_with_wheel"] == {}


def test_check_package_looks_up_older_releases_only_for_gaps_on_pypi(fake_pypi, monkeypatch):
    monkeypatch.setitem(FAKE_INDEX, "wheelstopped", WHEELSTOPPED)
    (_, reply) = run(init(), call("check_package", name="purewheel", python="3.13", targets=["linux-x86_64"]))
    assert "last_with_wheel" not in reply["result"]["structuredContent"]  # no gap, so nothing to look up
    assert "suggest" not in reply["result"]["structuredContent"]
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/json"]
    # Another index may hold private names next to PyPI's, so --suggest's rule applies: nothing is looked up.
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example/pypi")
    fake_pypi.clear()
    (_, reply) = run(init(), call("check_package", name="wheelstopped", python="3.13", targets=["win-arm64"]))
    assert reply["result"]["structuredContent"]["gaps"] == ["3.13 win-arm64"]
    assert "suggest" not in reply["result"]["structuredContent"]
    assert "last_with_wheel" not in reply["result"]["structuredContent"]
    assert fake_pypi == ["https://mirror.example/pypi/wheelstopped/json"]


def test_check_package_pin_respects_requires_python_per_target(fake_pypi):
    (_, reply) = run(
        init(),
        call(
            "check_package",
            name="pythonranged",
            version="2.0",
            python="3.9,3.13",
            targets=["linux-x86_64"],
            explain=True,
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["results"] == {"3.9 linux-x86_64": "missing", "3.13 linux-x86_64": "wheel"}
    assert data["rejected"] == {
        "3.9 linux-x86_64": [{"file": "pythonranged-2.0-py3-none-any.whl", "reason": "requires Python >=3.10"}]
    }
    assert data["gaps"] == ["3.9 linux-x86_64"]


def test_python_next(fake_pypi, fixtures):
    (_, reply) = run(init(), call("check_package", name="purewheel", python="3.14,next-t", targets=["linux-x86_64"]))
    assert reply["result"]["structuredContent"]["results"] == {
        "3.14 linux-x86_64": "wheel",
        "3.15t linux-x86_64": "wheel",
    }
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="next", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.15 linux-x86_64"]
    assert "next is Python 3.15, the newest CPython this wheelgap version knows" in data["notes"]
    assert "blocking_newer_python" not in data  # one Python


@pytest.mark.parametrize("python", ["3.14,Next", "3.14, NEXT-T"])
def test_python_next_any_case_and_blocking(fake_pypi, tmp_path, python):
    (tmp_path / "requirements.txt").write_text("newpylag==1.0\nnowheels==0.9\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), python=python, targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["blocking_newer_python"] == ["newpylag"]
    (_, reply) = run(init(), call("check_package", name="purewheel", python=python, targets=["linux-x86_64"]))
    assert reply["result"]["isError"] is False


def test_check_package_pinned_version_and_glibc(fake_pypi):
    (_, reply) = run(
        init(),
        call("check_package", name="fastlib", version="2.0.0", python="3.13", targets=["linux-x86_64"], glibc="2.17"),
    )
    data = reply["result"]["structuredContent"]
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]
    assert data["results"] == {"3.13 linux-x86_64": "wheel"}  # manylinux_2_17 still fits glibc 2.17
    assert data["assumes"]["glibc"] == "2.17"


@pytest.mark.parametrize("extra", [{}, {"explain": None}, {"explain": False}])
def test_check_package_without_explain_has_no_explain_keys(fake_pypi, extra):
    # explain: null means absent, like the other optional arguments
    (_, reply) = run(init(), call("check_package", name="nowheels", python="3.13", targets=["linux-x86_64"], **extra))
    data = reply["result"]["structuredContent"]
    assert data["results"] == {"3.13 linux-x86_64": "source"}
    assert set(data) == {
        "name",
        "version",
        "error",
        "results",
        "ok",
        "gaps",
        "targets",
        "assumes",
        "suggest",
        "last_with_wheel",
    }


def test_check_package_explain_caps_rejected_wheels(fake_pypi, monkeypatch):
    wheels = [f"many-1.0-cp3{m}-cp3{m}-win_amd64.whl" for m in range(5, 16)]  # 11, none for Linux
    monkeypatch.setitem(FAKE_INDEX, "many", {"1.0": [*wheels, "many-1.0.tar.gz"]})
    (_, reply) = run(
        init(),
        call("check_package", name="many", python="3.13", targets=["linux-x86_64", "win-amd64"], explain=True),
    )
    data = reply["result"]["structuredContent"]
    assert data["results"] == {"3.13 linux-x86_64": "source", "3.13 win-amd64": "wheel"}
    (cell,) = data["rejected"]["3.13 linux-x86_64"][:1]
    assert cell == {"file": "many-1.0-cp313-cp313-win_amd64.whl", "reason": "win_amd64 doesn't match linux-x86_64"}
    assert list(data["rejected"]) == ["3.13 linux-x86_64"]  # gap targets only
    assert len(data["rejected"]["3.13 linux-x86_64"]) == 8
    assert data["rejected_omitted"] == {"3.13 linux-x86_64": 3}
    assert data["via"] == {}
    (_, reply) = run(
        init(), call("check_package", name="nowheels", python="3.13", targets=["linux-x86_64"], explain=True)
    )
    data = reply["result"]["structuredContent"]
    assert (data["rejected"], data["rejected_omitted"]) == ({"3.13 linux-x86_64": []}, {})  # an sdist only


def test_check_package_explain_includes_issues_url(fake_pypi):
    (_, reply) = run(
        init(),
        call(
            "check_package", name="gapwithissues", version="1.0", python="3.13", targets=["linux-x86_64"], explain=True
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["issues_url"] == "https://github.com/example/gapwithissues/issues"

    (_, reply_no_explain) = run(
        init(),
        call("check_package", name="gapwithissues", version="1.0", python="3.13", targets=["linux-x86_64"]),
    )
    data_no_explain = reply_no_explain["result"]["structuredContent"]
    assert "issues_url" not in data_no_explain


def test_check_package_explain_issues_url_null_for_other_index(fake_pypi, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://other.example.com/simple")
    (_, reply) = run(
        init(),
        call(
            "check_package", name="gapwithissues", version="1.0", python="3.13", targets=["linux-x86_64"], explain=True
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["issues_url"] is None


def test_check_package_explain_issues_url_null_with_pip_index_url(fake_pypi, monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://other.example.com/simple")
    (_, reply) = run(
        init(),
        call(
            "check_package", name="gapwithissues", version="1.0", python="3.13", targets=["linux-x86_64"], explain=True
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["issues_url"] is None


@pytest.mark.usefixtures("index")
def test_check_package_dependencies_checks_the_tree():
    # As --deps does: app needs lib[crypto], whose crypt has no Windows ARM wheel; winonly is only on Windows.
    targets = ["win-arm64", "linux-x86_64"]
    (_, reply) = run(init(), call("check_package", name="app", version="1.0", python="3.14", targets=targets))
    data = reply["result"]["structuredContent"]
    assert data["ok"] is True  # app itself has a wheel everywhere
    assert "dependency_gaps" not in data and "dependencies_checked" not in data
    (_, reply) = run(
        init(), call("check_package", name="app", version="1.0", python="3.14", targets=targets, dependencies=True)
    )
    data = reply["result"]["structuredContent"]
    assert (data["name"], data["gaps"]) == ("app", [])  # the top-level fields stay app's own
    assert data["ok"] is False  # but ok covers the tree
    assert data["dependencies_checked"] == 3  # lib, crypt and winonly
    assert data["dependency_gaps"] == [
        {
            "name": "crypt",
            "version": "1.0",
            "results": {"3.14 win-arm64": "source", "3.14 linux-x86_64": "wheel"},
            "gaps": ["3.14 win-arm64"],
            "via": {"3.14 win-arm64": ["app", "lib", "crypt"]},
            "suggest": {},  # 1.0 is crypt's only release: looked up, nothing to move to
            "last_with_wheel": {},
        }
    ]
    (_, reply_explain) = run(
        init(),
        call(
            "check_package",
            name="app",
            version="1.0",
            python="3.14",
            targets=targets,
            dependencies=True,
            explain=True,
        ),
    )
    data_explain = reply_explain["result"]["structuredContent"]
    assert data_explain["dependency_gaps"] == [
        {
            "name": "crypt",
            "version": "1.0",
            "results": {"3.14 win-arm64": "source", "3.14 linux-x86_64": "wheel"},
            "gaps": ["3.14 win-arm64"],
            "via": {"3.14 win-arm64": ["app", "lib", "crypt"]},
            "issues_url": None,
            "suggest": {},
            "last_with_wheel": {},
        }
    ]


@pytest.mark.usefixtures("index")
def test_check_package_dependencies_ok_when_the_tree_is_clean():
    arguments = {"name": "plain", "version": "1.0", "python": "3.14", "targets": ["win-arm64"], "dependencies": True}
    (_, reply) = run(init(), call("check_package", **arguments))
    data = reply["result"]["structuredContent"]
    assert (data["ok"], data["dependencies_checked"], data["dependency_gaps"]) == (True, 2, [])  # lib and winonly


@pytest.mark.usefixtures("index")
def test_check_package_dependencies_must_be_a_boolean():
    (_, reply) = run(init(), call("check_package", name="x", python="3.14", targets=["win-arm64"], dependencies=None))
    assert "error" not in reply and reply["result"].get("isError") is not True  # null is the default, false
    (_, reply) = run(init(), call("check_package", name="x", python="3.14", targets=["win-arm64"], dependencies="yes"))
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"].endswith("'dependencies' must be true or false")


def test_structured_content_only_from_2025_06_18(fake_pypi):
    (_, reply) = run(init("2025-03-26"), call("check_package", name="purewheel", python="3.13"))
    assert "structuredContent" not in reply["result"]
    assert json.loads(reply["result"]["content"][0]["text"])["ok"] is True


def test_check_package_lookup_failure_is_unknown_not_ok(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="no-such-package", python="3.13", targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert data["results"] == {"3.13 linux-x86_64": "unknown"}
    assert data["error"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "'name' is required"),
        ({"name": ""}, "'name' must be a non-empty string"),
        ({"name": "numpy", "targets": "linux-aarch64"}, "'targets' must be an array of strings"),
        ({"name": "numpy", "targets": ["linux-sparc"]}, "linux-sparc"),
        ({"name": "numpy", "python": "three"}, "three"),
        ({"name": "numpy", "platforms": ["win32"]}, "unknown argument(s): platforms"),
        ({"name": "numpy==2.0"}, "isn't a package name"),
        ({"name": "numpy", "version": ">=2"}, "exact version"),
        ({"name": "numpy", "glibc": "new"}, "glibc"),
        ({"name": "numpy", "explain": "yes"}, "'explain' must be true or false"),
    ],
)
def test_bad_arguments_are_tool_errors_without_lookups(fake_pypi, arguments, message):
    (_, reply) = run(init(), call("check_package", **arguments))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_unknown_tool_is_a_protocol_error():
    (_, reply) = run(init(), call("rm_rf"))
    assert reply["error"]["code"] == mcp.INVALID_PARAMS


def test_check_lockfile_reports_only_gaps(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.13 linux-x86_64"]
    assert data["ok"] is False
    assert data["packages"] == 5
    assert {g["name"]: g["results"] for g in data["gaps"]} == {
        "nativeext": {"3.13 linux-x86_64": "source"},
        "puresdist": {"3.13 linux-x86_64": "source"},
    }


@pytest.mark.usefixtures("index")
def test_check_lockfile_dependencies_resolves_transitive_gaps(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("app==1.0\n", encoding="utf-8")
    args = {"path": str(requirements), "python": "3.14", "targets": ["win-arm64", "linux-x86_64"]}
    (_, direct, tree) = run(
        init(),
        call("check_lockfile", **args),
        call("check_lockfile", 2, **args, dependencies=True),
    )
    assert direct["result"]["isError"] is False
    assert direct["result"]["structuredContent"]["gaps"] == []
    assert tree["result"]["isError"] is False
    data = tree["result"]["structuredContent"]
    crypt = next(g for g in data["gaps"] if g["name"] == "crypt")
    assert crypt["results"]["3.14 win-arm64"] == "source"
    assert crypt["via"] == {"3.14 win-arm64": ["app", "lib", "crypt"]}
    assert crypt["direct"] == {"3.14 win-arm64": ["app"]}  # labelled like results, as via is
    # Only the tool's own keys: --explain's others (rejected wheels, notes...) aren't part of the answer.
    assert not set(crypt) & {"rejected", "issues_url", "notes", "build_lab", "build_requires", "suggest"}
    assert data["ok"] is False


@pytest.mark.usefixtures("index")
def test_check_lockfile_dependencies_doesnt_send_names_to_the_tracker(tmp_path, monkeypatch):
    # notes = true in the project: --explain would fetch the tracker's notes for every gap; this tool asks none.
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nnotes = true\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("app==1.0\n", encoding="utf-8")
    import wheelgap.notes

    asked = []
    monkeypatch.setattr(wheelgap.notes, "lookup", lambda names, *a, **k: asked.append(names) or ({}, {}, False))
    (_, reply) = run(
        init(),
        call(
            "check_lockfile",
            path=str(tmp_path / "requirements.txt"),
            python="3.14",
            targets=["win-arm64"],
            dependencies=True,
        ),  # fmt: skip
    )
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["gaps"]  # the tree has a gap, so notes would have been asked for
    assert asked == []


@pytest.mark.parametrize("value", ["yes", 1, [], {}])
def test_check_lockfile_dependencies_requires_boolean(tmp_path, value):
    path = tmp_path / "requirements.txt"
    path.write_text("purewheel==3.1\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(path), dependencies=value))
    assert reply["result"]["isError"] is True
    assert "'dependencies' must be true or false" in reply["result"]["content"][0]["text"]


def test_check_lockfile_dependencies_refuses_lockfiles_and_offline(tmp_path, fixtures):
    (_, lockfile) = run(
        init(),
        call("check_lockfile", path=str(fixtures / "uvproj" / "uv.lock"), dependencies=True, python="3.13"),
    )
    assert lockfile["result"]["isError"] is True
    assert "--deps" in lockfile["result"]["content"][0]["text"]
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("purewheel==3.1\n", encoding="utf-8")
    (_, offline) = run(
        init(), call("check_lockfile", path=str(requirements), dependencies=True, offline=True, python="3.13")
    )
    assert offline["result"]["isError"] is True
    assert "--deps" in offline["result"]["content"][0]["text"]


def test_check_lockfile_lists_skipped_packages(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert {s["name"]: s["reason"] for s in data["skipped"]} == {"demo": "editable", "gitdep": "git"}


@pytest.mark.parametrize("environment", [None, "default", "test"])
def test_check_lockfile_pixi_environment(fake_pypi, fixtures, environment):
    args = {} if environment is None else {"environment": environment}
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(fixtures / "pixiproj"), python="3.13", targets=["linux-x86_64"], **args),
    )
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    if environment == "test":
        assert data["packages"] == 1
        assert {g["name"]: g["results"] for g in data["gaps"]} == {"nowheels": {"3.13 linux-x86_64": "source"}}
        assert fake_pypi == ["https://pypi.org/pypi/nowheels/0.9/json"]
    else:
        assert data["packages"] == 2 and data["gaps"] == []


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ("docs", "no environment 'docs' (it has: default, test)"),
        ("", "--environment needs a name"),
        (42, "'environment' must be a string"),
        (["test"], "'environment' must be a string"),
    ],
)
def test_check_lockfile_pixi_environment_errors(fake_pypi, fixtures, environment, message):
    (_, reply) = run(init(), call("check_lockfile", path=str(fixtures / "pixiproj"), environment=environment))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_check_lockfile_uses_project_config_and_arguments_win(fake_pypi, fixtures):
    path = str(fixtures / "configproj")
    (_, configured, explicit) = run(
        init(),
        call("check_lockfile", path=path),
        call("check_lockfile", 2, path=path, python="3.13", targets=["linux-x86_64"]),
    )
    assert configured["result"]["structuredContent"]["targets"] == ["3.13 win-amd64", "3.14t win-amd64"]
    assert explicit["result"]["structuredContent"]["targets"] == ["3.13 linux-x86_64"]


def test_check_lockfile_offline_config_means_no_lookups(fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path / "requirements.txt"), python="3.13"))
    assert reply["result"]["isError"] is True
    assert "offline" in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_check_lockfile_ignores_from_config(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\nignore = ["nativeext:linux-x86_64", "puresdist"]\n', encoding="utf-8"
    )
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(tmp_path), python="3.13", targets=["linux-x86_64", "win-amd64"], offline=True),
    )
    data = reply["result"]["structuredContent"]
    assert data["ignored"] == ["puresdist"]
    # nativeext's linux gap is accepted; its Windows gap still counts.
    assert [(g["name"], g["gaps"]) for g in data["gaps"]] == [("nativeext", ["3.13 win-amd64"])]


def test_check_lockfile_advisory_from_config(fake_pypi, tmp_path):
    # fastlib has a linux-x86_64 wheel and builds from source on musl-aarch64; nowheels builds from source on both.
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nadvisory = ["musl-aarch64"]\n', encoding="utf-8")
    path = str(tmp_path / "requirements.txt")
    targets = ["linux-x86_64", "musl-aarch64"]
    (_, reply) = run(init(), call("check_lockfile", path=path, python="3.13", targets=targets))
    data = reply["result"]["structuredContent"]
    assert data["advisory"] == ["3.13 musl-aarch64"]
    assert data["ok"] is False  # a gap is a gap
    assert data["passes"] is True  # but the command line exits 0, as the gap is advisory
    assert [(g["name"], g["gaps"], g["advisory_gaps"]) for g in data["gaps"]] == [
        ("fastlib", ["3.13 musl-aarch64"], ["3.13 musl-aarch64"])
    ]
    # A gap on a required target fails it; the advisory one is still marked.
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\nnowheels==0.9\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=path, python="3.13", targets=targets))
    data = reply["result"]["structuredContent"]
    assert (data["ok"], data["passes"]) == (False, False)
    assert {g["name"]: g["advisory_gaps"] for g in data["gaps"]} == {
        "fastlib": ["3.13 musl-aarch64"],
        "nowheels": ["3.13 musl-aarch64"],
    }
    # Targets that leave the advisory one out: a note, not an error; and passes and ok agree without advisory gaps.
    (_, reply) = run(init(), call("check_lockfile", path=path, python="3.13", targets=["win-arm64"]))
    data = reply["result"]["structuredContent"]
    assert data["advisory"] == [] and data["passes"] is False
    assert any("advisory in [tool.wheelgap] names targets this run doesn't check" in n for n in data["notes"])


def test_check_lockfile_glibc_argument(fake_pypi, fixtures):
    path = str(fixtures / "uvproj")
    (_, default, old) = run(
        init(),
        call("check_lockfile", path=path, python="3.13", targets=["linux-x86_64"], offline=True),
        call("check_lockfile", 2, path=path, python="3.13", targets=["linux-x86_64"], offline=True, glibc="2.12"),
    )
    assert "fastlib" not in {g["name"] for g in default["result"]["structuredContent"]["gaps"]}
    # fastlib's only wheel is manylinux_2_17, too new for glibc 2.12.
    assert "fastlib" in {g["name"] for g in old["result"]["structuredContent"]["gaps"]}


def test_check_lockfile_counts_unknown(fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\nno-such-package==1.0\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path / "requirements.txt"), python="3.13"))
    data = reply["result"]["structuredContent"]
    assert data["unknown"] == 1 and data["ok"] is False


def test_check_lockfile_path_starting_with_a_dash(fake_pypi, tmp_path, monkeypatch, fixtures):
    (tmp_path / "-p").mkdir()
    (tmp_path / "-p" / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    monkeypatch.chdir(tmp_path)
    (_, reply) = run(init(), call("check_lockfile", path="-p", python="3.13", offline=True))
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["path"].endswith("-p")


def test_check_lockfile_argparse_exit_is_a_tool_error(monkeypatch, fixtures):
    from wheelgap import cli

    def exits(argv):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "main", exits)
    replies = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj")), {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    )
    reply, ping = (next(r for r in replies if r["id"] == i) for i in (1, 9))  # the call runs on the worker
    assert reply["result"]["isError"] is True
    assert ping["result"] == {}


def test_check_lockfile_passes_on_notes(fake_pypi, fixtures):
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(fixtures / "uvproj"), python="3.11", targets=["linux-x86_64"], offline=True),
    )
    notes = reply["result"]["structuredContent"]["notes"]
    assert "may show false gaps" in notes[0]
    assert notes[1] == "2 packages weren't checked (see skipped); ok covers the others only"
    assert not fake_pypi


def test_every_cli_run_keeps_notes_plain(fake_pypi, tmp_path, fixtures, monkeypatch):
    # A dimmed note under FORCE_COLOR starts with an ANSI escape instead of "wheelgap: ", so _run_cli would drop it
    # (#449): every run the server makes passes --color=never, whichever tool and options start it.
    monkeypatch.setenv("FORCE_COLOR", "1")
    seen = []
    real = cli.main

    def spy(argv=None, **kwargs):
        seen.append(list(argv or []))
        return real(argv, **kwargs)

    monkeypatch.setattr(cli, "main", spy)
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    run(
        init(),
        call("check_lockfile", path=str(tmp_path / "uv.lock"), python="3.13", offline=True),
        call("check_lockfile", 2, path=str(tmp_path / "requirements.txt"), python="3.13", dependencies=True),
    )
    assert len(seen) == 2
    assert all("--color=never" in argv for argv in seen)


def test_check_lockfile_explicit_python_beats_python_version_file(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), python="3.13", offline=True))
    assert {t.split()[0] for t in reply["result"]["structuredContent"]["targets"]} == {"3.13"}


def test_check_lockfile_uses_python_version_file(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), targets=["linux-x86_64"], offline=True))
    assert reply["result"]["structuredContent"]["targets"] == ["3.12 linux-x86_64"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "/no/such/uv.lock"}, "no such file or directory"),
        ({"path": "."}, None),
        ({"path": "x", "offline": "yes"}, "'offline' must be true or false"),
    ],
)
def test_check_lockfile_errors(fake_pypi, tmp_path, monkeypatch, arguments, message):
    monkeypatch.chdir(tmp_path)  # an empty directory: no lockfile to find
    (tmp_path / "x").write_text("", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", **arguments))
    assert reply["result"]["isError"] is True
    if message:
        assert message in reply["result"]["content"][0]["text"]


def dockerfile_project(tmp_path, fixtures, text: str):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "Dockerfile").write_text(text, encoding="utf-8")
    return tmp_path / "Dockerfile"


def test_check_dockerfile_alpine_image_gives_musl_targets(fake_pypi, tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "FROM python:3.12 AS build\nFROM python:3.13-alpine\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile)))
    data = reply["result"]["structuredContent"]
    assert data["image"] == {
        "python": "3.13",
        "platform": "musl-x86_64",
        "glibc": None,
        "musl": "1.2",
        "build_args": {},
    }
    assert data["targets"] == ["3.13 musl-x86_64"]
    assert data["path"] == str(tmp_path.resolve())  # default: the Dockerfile's folder
    assert {g["name"] for g in data["gaps"]} >= {"nativeext", "puresdist"}
    assert {s["name"] for s in data["skipped"]} == {"demo", "gitdep"}


@pytest.mark.parametrize(
    ("dockerfile_text", "defaults", "expected_python"),
    [
        ("ARG PYTHON_VERSION=3.12\nFROM python:${PYTHON_VERSION}-slim\n", {"PYTHON_VERSION": "3.12"}, "3.12"),
        (
            "ARG PYTHON_VERSION=3.13\nARG DISTRO=alpine\nFROM python:${PYTHON_VERSION}-${DISTRO}\n",
            {"PYTHON_VERSION": "3.13", "DISTRO": "alpine"},
            "3.13",
        ),
    ],
)
def test_check_dockerfile_reports_used_arg_defaults(
    fake_pypi, tmp_path, fixtures, dockerfile_text, defaults, expected_python
):
    dockerfile = dockerfile_project(tmp_path, fixtures, dockerfile_text)
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile)))
    assert not reply["result"].get("isError")
    data = reply["result"]["structuredContent"]
    assert data["image"]["build_args"] == defaults
    assert data["image"]["python"] == expected_python
    assert all("-p/-t" not in note for note in data["notes"])
    for name, value in defaults.items():
        assert any(f"FROM uses the default of ARG {name}={value}" in note for note in data["notes"])
    assert any("pass build_args if the build sets --build-arg" in note for note in data["notes"])


def test_check_dockerfile_build_args_replace_the_defaults(fake_pypi, tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "ARG PY=3.12\nARG DISTRO\nFROM python:${PY}-${DISTRO}\n")
    build_args = {"PY": "3.13", "DISTRO": "alpine"}
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile), build_args=build_args))
    data = reply["result"]["structuredContent"]
    assert data["image"]["build_args"] == build_args
    assert data["image"]["platform"] == "musl-x86_64"
    assert data["targets"] == ["3.13 musl-x86_64"]
    assert all("-p/-t" not in note and "default of ARG" not in note for note in data["notes"])


def test_check_dockerfile_build_args_must_be_strings(tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "FROM python:3.13-slim\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile), build_args={"PY": 3}))
    assert reply["result"]["isError"]
    assert "'build_args' must be an object of strings" in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize("key", ["PY=3", "", "1PY", "P Y"])
def test_check_dockerfile_build_args_keys_must_be_arg_names(tmp_path, fixtures, key):
    # A key with "=" would be split differently when it's passed on as --build-arg NAME=VALUE.
    dockerfile = dockerfile_project(tmp_path, fixtures, "ARG PY=3.12\nFROM python:${PY}-slim\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile), build_args={key: ".13"}))
    assert reply["result"]["isError"]
    assert "'build_args' has a key that isn't an ARG name" in reply["result"]["content"][0]["text"]


def test_check_dockerfile_ignores_unused_arg_defaults(fake_pypi, tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "ARG UNUSED=3.12\nFROM python:3.13-slim\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile)))
    assert reply["result"]["structuredContent"]["image"]["build_args"] == {}
    assert not any("FROM uses the default of ARG" in note for note in reply["result"]["structuredContent"]["notes"])


def test_check_dockerfile_matches_the_cli(fake_pypi, tmp_path, fixtures, capsys):
    from wheelgap import cli

    dockerfile = dockerfile_project(tmp_path, fixtures, "FROM --platform=linux/arm64 python:3.13-slim\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile), path=str(tmp_path / "uv.lock")))
    data = reply["result"]["structuredContent"]
    assert data["image"]["platform"] == "linux-aarch64" and data["image"]["musl"] is None
    capsys.readouterr()
    cli.main(["--json", f"--dockerfile={dockerfile}", str(tmp_path / "uv.lock")])
    expected = json.loads(capsys.readouterr().out)
    assert data["targets"] == [f"{t['python']} {t['platform']}" for t in expected["targets"]]
    assert data["packages"] == expected["summary"]["packages"]


def test_check_dockerfile_targets_override_the_image_platform(fake_pypi, tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "FROM python:3.13-alpine\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(dockerfile), targets=["linux/arm64"]))
    data = reply["result"]["structuredContent"]
    # With an Alpine image, a Docker platform means musl, as with `wheelgap --dockerfile ... -t linux/arm64`.
    assert data["targets"] == ["3.13 musl-aarch64"]
    assert data["image"]["platform"] == "musl-x86_64"


@pytest.mark.parametrize(
    ("text", "arguments", "message"),
    [
        ("FROM ubuntu:24.04\n", {}, "supported, use check_lockfile with python and targets instead"),
        ("RUN echo hi\n", {}, "no FROM line found"),
        ("FROM python:3.13\n", {"path": "/no/such/uv.lock"}, "no such file or directory"),
        ("FROM python:3.13\n", {"dockerfile": "/no/such/Dockerfile"}, "no such file or directory"),
        ("FROM python:3.13\n", {"python": "3.12"}, "unknown argument(s): python"),
    ],
)
def test_check_dockerfile_errors(fake_pypi, tmp_path, fixtures, text, arguments, message):
    dockerfile = dockerfile_project(tmp_path, fixtures, text)
    (_, reply) = run(init(), call("check_dockerfile", **({"dockerfile": str(dockerfile)} | arguments)))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_check_dockerfile_directory_is_a_tool_error(fake_pypi, tmp_path, fixtures):
    dockerfile_project(tmp_path, fixtures, "FROM python:3.13\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(tmp_path)))
    assert reply["result"]["isError"] is True
    assert "is a directory" in reply["result"]["content"][0]["text"]


def test_check_dist_names_each_dropped_target(fake_pypi, tmp_path):
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)  # no win_arm64 wheel this time
    (_, reply) = run(
        init(),
        call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "win-arm64"]),
    )
    result = reply["result"]
    data = result["structuredContent"]
    assert result["isError"] is False
    assert data["dropped"] == {"3.13 win-arm64": "1.0"}
    assert data["gaps"][0]["results"] == {"3.13 linux-x86_64": "wheel", "3.13 win-arm64": "source"}
    assert data["ok"] is False and data["passes"] is False
    assert data["path"] == str(folder.resolve())


def test_check_dist_a_clean_release_passes(fake_pypi, tmp_path):
    folder = dist(tmp_path, LINUX_WHEEL, WIN_ARM_WHEEL, SDIST)
    (_, reply) = run(
        init(),
        call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "win-arm64"]),
    )
    data = reply["result"]["structuredContent"]
    assert data["dropped"] == {} and data["gaps"] == []
    assert data["ok"] is True and data["passes"] is True


def test_check_dist_a_first_release_compares_nothing(fake_pypi, tmp_path):
    folder = dist(tmp_path, "firstpkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl")
    (_, reply) = run(init(), call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["dropped"] == {}
    assert data["ok"] is True
    assert any("no earlier release" in note for note in data["notes"])


@pytest.mark.parametrize("force_color", [False, True])
def test_check_dist_ok_is_false_when_the_comparison_didnt_run(fake_pypi, tmp_path, monkeypatch, force_color):
    # The index lookup failing must not read as "nothing dropped"; and with FORCE_COLOR set in the server's
    # environment, the note saying so must still reach notes (a dimmed line wouldn't start "wheelgap: ").
    if force_color:
        monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(mcp.pypi, "fetch_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    folder = dist(tmp_path, LINUX_WHEEL, WIN_ARM_WHEEL, SDIST)
    (_, reply) = run(
        init(),
        call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "win-arm64"]),
    )
    data = reply["result"]["structuredContent"]
    assert "dropped" not in data
    assert data["ok"] is False  # not a guess
    assert data["passes"] is True  # the gate itself passed; only the comparison didn't run
    assert any("dropped-target check didn't run" in note for note in data["notes"])


def test_check_dist_reads_the_config_beside_the_dist_folder_not_the_servers(fake_pypi, tmp_path, monkeypatch):
    # The server runs in another project whose config makes win-arm64 advisory: that must not hide this drop.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "pyproject.toml").write_text('[tool.wheelgap]\nadvisory = ["win-arm64"]\n', encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)  # no win_arm64 wheel this time
    (_, reply) = run(
        init(),
        call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "win-arm64"]),
    )
    data = reply["result"]["structuredContent"]
    assert data["dropped"] == {"3.13 win-arm64": "1.0"}
    assert data["advisory"] == []
    assert data["ok"] is False


def test_check_dist_a_dotdot_path_doesnt_reach_the_servers_project(fake_pypi, tmp_path, monkeypatch):
    # app/../legacy/dist: walking the path's text would pass through app/ and take the server's config.
    app = tmp_path / "app"
    app.mkdir()
    (app / "pyproject.toml").write_text('[tool.wheelgap]\nadvisory = ["win-arm64"]\n', encoding="utf-8")
    monkeypatch.chdir(app)
    folder = tmp_path / "legacy" / "dist"
    folder.mkdir(parents=True)
    for name in (LINUX_WHEEL, SDIST):
        (folder / name).write_bytes(b"")
    (_, reply) = run(
        init(),
        call(
            "check_dist", path=str(app / ".." / "legacy" / "dist"), python="3.13", targets=["linux-x86_64", "win-arm64"]
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["advisory"] == []
    assert data["dropped"] == {"3.13 win-arm64": "1.0"}
    assert data["ok"] is False


def test_check_dist_applies_its_own_projects_config(fake_pypi, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\npython = ["3.13"]\ntargets = ["linux-x86_64", "win-arm64"]\nadvisory = ["win-arm64"]\n',
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)
    (_, reply) = run(init(), call("check_dist", path=str(folder)))
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.13 linux-x86_64", "3.13 win-arm64"]
    assert data["dropped"] == {}  # an advisory target is never a drop
    assert data["ok"] is False and data["passes"] is True  # its source build is still a gap; it just doesn't fail


def test_check_dist_a_source_build_is_not_ok(fake_pypi, tmp_path):
    # macOS only has the sdist to build from: wheelgap doesn't know it builds, so ok is false, as in the other tools.
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)
    (_, reply) = run(
        init(), call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "macos-arm64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["dropped"] == {}
    assert data["gaps"][0]["gaps"] == ["3.13 macos-arm64"]
    assert data["ok"] is False and data["passes"] is False


def test_check_dist_a_missing_wheel_is_not_ok(fake_pypi, tmp_path):
    folder = dist(tmp_path, "firstpkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl")  # no sdist
    (_, reply) = run(init(), call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "win-amd64"]))
    data = reply["result"]["structuredContent"]
    assert data["dropped"] == {}
    assert data["gaps"][0]["results"]["3.13 win-amd64"] == "missing"
    assert data["ok"] is False


def test_check_dist_an_ignored_targets_gap_is_ok(fake_pypi, tmp_path):
    # Pyodide can't build the sdist (missing), but that target is ignored.
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nignore = ["distpkg:pyodide"]\n', encoding="utf-8")
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)
    (_, reply) = run(
        init(),
        call("check_dist", path=str(folder), python="3.13", targets=["linux-x86_64", "pyodide"]),
    )
    data = reply["result"]["structuredContent"]
    assert data["gaps"] == [] and data["dropped"] == {}
    assert data["ok"] is True


def test_check_dist_finds_the_project_above_a_nested_build_folder(fake_pypi, tmp_path, monkeypatch):
    # maturin leaves wheels in target/wheels: the settings are two folders up, not in target/.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\npython = ["3.13"]\ntargets = ["linux-x86_64", "win-arm64"]\n', encoding="utf-8"
    )
    folder = tmp_path / "target" / "wheels"
    folder.mkdir(parents=True)
    for name in (LINUX_WHEEL, SDIST):
        (folder / name).write_bytes(b"")
    monkeypatch.chdir(tmp_path / "target")
    (_, reply) = run(init(), call("check_dist", path=str(folder)))
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.13 linux-x86_64", "3.13 win-arm64"]
    assert data["dropped"] == {"3.13 win-arm64": "1.0"}
    assert data["ok"] is False


def test_check_dist_takes_a_file_uri(fake_pypi, tmp_path):
    folder = dist(tmp_path, LINUX_WHEEL, WIN_ARM_WHEEL, SDIST)
    (_, reply) = run(
        init(),
        call("check_dist", path=folder.resolve().as_uri(), python="3.13", targets=["linux-x86_64"]),
    )
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["ok"] is True


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing", "no such file or directory"),
        ("file", "is not a folder"),
    ],
)
def test_check_dist_path_errors(fake_pypi, tmp_path, path, message):
    if path == "file":
        (tmp_path / "file").write_text("x", encoding="utf-8")
    (_, reply) = run(init(), call("check_dist", path=str(tmp_path / path)))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]


def test_check_dist_a_mixed_folder_is_a_tool_error(fake_pypi, tmp_path):
    folder = dist(tmp_path, "a-1.0-py3-none-any.whl", "b-1.0-py3-none-any.whl")
    (_, reply) = run(init(), call("check_dist", path=str(folder)))
    assert reply["result"]["isError"] is True
    assert "more than one project" in reply["result"]["content"][0]["text"]


def test_check_dist_rejects_unknown_arguments(fake_pypi, tmp_path):
    folder = dist(tmp_path, LINUX_WHEEL, SDIST)
    (_, reply) = run(init(), call("check_dist", path=str(folder), platform=["linux"]))
    assert reply["result"]["isError"] is True
    assert "unknown argument(s): platform" in reply["result"]["content"][0]["text"]


def test_check_package_pyodide_lock_marks_bundled(fake_pypi, fixtures):
    lock = str(fixtures / "pyodide-lock.json")  # Pyodide 0.29.0, Python 3.13: ships fastlib 2.0.0
    (_, without, with_lock) = run(
        init(),
        call("check_package", name="fastlib", python="3.13", targets=["pyodide"]),
        call("check_package", 2, name="fastlib", python="3.13", targets=["pyodide"], pyodide_lock=lock),
    )
    assert without["result"]["structuredContent"]["results"] == {"3.13 pyodide": "missing"}
    data = with_lock["result"]["structuredContent"]
    assert with_lock["result"]["isError"] is False
    assert data["results"] == {"3.13 pyodide": "bundled"}
    assert data["ok"] is True
    assert data["gaps"] == []
    assert data["bundled"] == "2.0.0"


def test_check_lockfile_pyodide_lock_marks_bundled(fake_pypi, fixtures, tmp_path):
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\nnowheels==0.9\n", encoding="utf-8")
    (_, reply) = run(
        init(),
        call(
            "check_lockfile",
            path=str(tmp_path / "requirements.txt"),
            python="3.13",
            targets=["pyodide"],
            pyodide_lock=str(fixtures / "pyodide-lock.json"),
        ),
    )
    data = reply["result"]["structuredContent"]
    assert reply["result"]["isError"] is False
    assert {g["name"]: g["results"] for g in data["gaps"]} == {"nowheels": {"3.13 pyodide": "missing"}}


def test_check_lockfile_says_when_pyodide_ships_another_version(fake_pypi, fixtures, tmp_path):
    lock = json.loads((fixtures / "pyodide-lock.json").read_text(encoding="utf-8"))
    lock["packages"]["fastlib"]["version"] = "3.0.0"
    (tmp_path / "pyodide-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    (_, reply) = run(
        init(),
        call(
            "check_lockfile",
            path=str(tmp_path / "requirements.txt"),
            python="3.13",
            targets=["pyodide"],
            pyodide_lock=str(tmp_path / "pyodide-lock.json"),
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["ok"] is True  # bundled still passes, as in the CLI
    assert data["bundled_version_differs"] == [{"name": "fastlib", "version": "2.0.0", "bundled": "3.0.0"}]
    assert any("Pyodide ships 3.0.0, not the 2.0.0" in note for note in data["notes"])


def test_check_package_non_utf8_lock_is_a_tool_error(fake_pypi, tmp_path):
    (tmp_path / "pyodide-lock.json").write_bytes(b"\xff\xfe{}")
    (_, reply) = run(
        init(),
        call(
            "check_package",
            name="fastlib",
            python="3.13",
            targets=["pyodide"],
            pyodide_lock=str(tmp_path / "pyodide-lock.json"),
        ),
    )
    assert reply["result"]["isError"] is True
    assert "internal error" not in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize("tool", ["check_package", "check_lockfile"])
@pytest.mark.parametrize(
    ("lock", "message"),
    [
        ("pyodide-lock.json", "is for Python 3.13"),  # the fixture's Python, but the targets use 3.12
        ("no-such-lock.json", "no-such-lock.json"),
        ("http://example.com/pyodide-lock.json", "plain http:// URLs are not allowed"),
    ],
)
def test_pyodide_lock_problems_are_tool_errors(fake_pypi, fixtures, tmp_path, tool, lock, message):
    (tmp_path / "requirements.txt").write_text("fastlib==2.0.0\n", encoding="utf-8")
    spec = lock if "://" in lock else str(fixtures / lock)
    where = {"name": "fastlib"} if tool == "check_package" else {"path": str(tmp_path / "requirements.txt")}
    (_, reply) = run(init(), call(tool, **where, python="3.12", targets=["pyodide"], pyodide_lock=spec))
    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert message in text
    assert "internal error" not in text
    assert not fake_pypi


def test_list_targets():
    (_, reply) = run(init(), call("list_targets"))
    data = reply["result"]["structuredContent"]
    assert "musl-aarch64" in data["platforms"]
    assert data["groups"]["all"] and set(data["groups"]["all"]) <= set(data["platforms"])
    assert set(data["aliases"].values()) <= set(data["platforms"])
    assert data["defaults"] == {"glibc": "2.36", "musl": "1.2", "macos": "14.0", "ios": "13.0", "android_api": "24"}


@pytest.mark.parametrize("uri", [False, True])
def test_suggest_targets_reads_workflows_and_dockerfiles(fake_pypi, tmp_path, uri):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  test:\n    runs-on: windows-latest\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-alpine\n", encoding="utf-8")
    (_, reply) = run(init(), call("suggest_targets", path=tmp_path.as_uri() if uri else str(tmp_path)))
    result = reply["result"]
    assert result["isError"] is False
    data = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == data
    assert {(t["target"], t["file"], t["line"]) for t in data["targets"]} == {
        ("win-amd64", ".github/workflows/ci.yml", 3),
        ("musl-x86_64", "Dockerfile", 1),
    }
    assert [(p["python"], p["file"], p["line"]) for p in data["python"]] == [("3.13", "Dockerfile", 1)]
    assert data["unreadable"] == []
    assert data["config"] == {"python": ["3.13"], "targets": ["musl-x86_64", "win-amd64"], "musl": "1.2"}
    assert not fake_pypi


def test_suggest_targets_refuses_the_filesystem_root_and_home_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcp.Path, "home", staticmethod(lambda: home))
    for folder in (Path(tmp_path.anchor), home, tmp_path):  # tmp_path holds home: a folder above it
        (_, reply) = run(init(), call("suggest_targets", path=str(folder)))
        assert reply["result"]["isError"] is True
        assert "pass the project folder, not / or your home folder" in reply["result"]["content"][0]["text"]
    (home / "project").mkdir()
    (_, reply) = run(init(), call("suggest_targets", path=str(home / "project")))
    assert reply["result"]["isError"] is False


def test_suggest_targets_clips_unreadable_reasons(tmp_path, monkeypatch):
    found = Suggestions(unreadable=[Unreadable("Dockerfile", 1, "x" * 1000)])
    monkeypatch.setattr(mcp, "suggest", lambda folder: found)
    (_, reply) = run(init(), call("suggest_targets", path=str(tmp_path)))
    (item,) = reply["result"]["structuredContent"]["unreadable"]
    assert item == {"file": "Dockerfile", "line": 1, "reason": "x" * 197 + "..."}


def test_suggest_targets_clips_what_a_file_names(tmp_path, monkeypatch):
    long = "FROM python:3.13@" + "a" * 1000
    found = Suggestions(
        targets=[Found("linux-x86_64", "Dockerfile", 1, long)], pythons=[Found("3.13", "Dockerfile", 1, long)]
    )
    monkeypatch.setattr(mcp, "suggest", lambda folder: found)
    (_, reply) = run(init(), call("suggest_targets", path=str(tmp_path)))
    content = reply["result"]["structuredContent"]
    assert [t["from"] for t in content["targets"]] == [long[:197] + "..."]
    assert [p["from"] for p in content["python"]] == [long[:197] + "..."]


@pytest.mark.parametrize("modern", [False, True])
def test_suggest_targets_relative_path_uses_client_roots(tmp_path, monkeypatch, modern):
    project = tmp_path / "root" / "project"
    project.mkdir(parents=True)
    (project / "Dockerfile").write_text("FROM python:3.13-alpine\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    if modern:
        message = with_roots(modern_call("suggest_targets", path="project"))
        (request,) = run(message)
        assert request["result"]["resultType"] == "input_required"
        message["params"]["inputResponses"] = {"roots": roots_answer_for(project.parent)}
        (reply,) = run(message)
    else:
        (_, _, reply) = run(
            init_with_roots(), INITIALIZED, roots_answer(project.parent), call("suggest_targets", path="project")
        )
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["config"]["python"] == ["3.13"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"path": "missing"}, "no such file or directory"),
        ({"path": "Dockerfile"}, "not a folder"),
        ({}, "'path' is required"),
        ({"path": 1}, "'path' must be a non-empty string"),
        ({"path": ".", "targets": []}, "unknown argument(s): targets"),
    ],
)
def test_suggest_targets_rejects_invalid_arguments(tmp_path, monkeypatch, args, message):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-alpine\n", encoding="utf-8")
    (_, reply) = run(init(), call("suggest_targets", **args))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]


def test_stray_prints_stay_off_the_protocol_stream(monkeypatch):
    def noisy(args):
        print("progress...")
        return {"done": True}

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", noisy)
    (_, reply) = run(init(), call("list_targets"))
    assert reply["result"]["structuredContent"] == {"done": True}


def test_crash_in_a_tool_is_reported_not_fatal(monkeypatch):
    def broken(args):
        raise KeyError("boom")

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", broken)
    replies = run(init(), call("list_targets"), {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    reply, ping = (next(r for r in replies if r["id"] == i) for i in (1, 5))  # the call runs on the worker
    assert reply["result"]["isError"] is True
    assert "KeyError" in reply["result"]["content"][0]["text"]
    assert ping["result"] == {}


def test_non_ascii_output_stays_on_one_line(monkeypatch):
    monkeypatch.setitem(mcp._HANDLERS, "list_targets", lambda args: {"text": "naïve\nline"})
    stdin = io.BytesIO(json.dumps(call("list_targets")).encode() + b"\n")
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    assert stdout.getvalue().count(b"\n") == 1
    assert json.loads(stdout.getvalue())["result"]["structuredContent"]["text"] == "naïve\nline"


def test_real_stdio(fixtures):
    import subprocess
    import sys

    messages = [init(), {"jsonrpc": "2.0", "method": "notifications/initialized"}, call("list_targets")]
    proc = subprocess.run(
        [sys.executable, "-m", "wheelgap.mcp"],
        input=b"".join(json.dumps(m).encode() + b"\r\n" for m in messages),
        capture_output=True,
        timeout=60,
        check=True,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [0, 1]
    assert "linux-aarch64" in replies[1]["result"]["structuredContent"]["platforms"]


@pytest.mark.parametrize(("argv", "code"), [(["--version"], 0), (["--help"], 0), (["--bogus"], 2)])
def test_main_flags(argv, code, capsys):
    assert mcp.main(argv) == code


def test_wheelgap_mcp_subcommand_over_real_stdio(tmp_path):
    messages = [
        init(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        call("list_targets", msg_id=2),
    ]
    replies = run_wheelgap_mcp(tmp_path, messages)
    assert [r["id"] for r in replies] == [0, 1, 2]
    assert replies[0]["result"]["serverInfo"] == {"name": "wheelgap", "version": __version__}
    assert [t["name"] for t in replies[1]["result"]["tools"]] == [t["name"] for t in mcp.TOOLS]
    assert "linux-aarch64" in replies[2]["result"]["structuredContent"]["platforms"]


def run_wheelgap_mcp(cwd: Path, messages: list[dict]) -> list[dict]:
    """Run `python -m wheelgap mcp` in cwd over real stdio; return the parsed replies."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "wheelgap", "mcp"],
        input=b"".join(json.dumps(m).encode() + b"\n" for m in messages),
        capture_output=True,
        timeout=60,
        check=True,
        cwd=cwd,
    )
    return [json.loads(line) for line in proc.stdout.splitlines()]


@pytest.mark.parametrize("folder", ["mcp", "MCP"])
def test_wheelgap_mcp_starts_the_server_next_to_a_folder_named_mcp(tmp_path, folder):
    # MCP clients start the server in a directory they choose, often a project with an mcp/ folder.
    (tmp_path / folder).mkdir()
    (tmp_path / folder / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (reply,) = run_wheelgap_mcp(tmp_path, [init()])
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 0
    assert reply["result"]["serverInfo"] == {"name": "wheelgap", "version": __version__}


@pytest.mark.parametrize(("argv", "code"), [(["--version"], 0), (["-V"], 0), (["--help"], 0), (["--bogus"], 2)])
def test_wheelgap_mcp_subcommand_flags(argv, code, capsys, tmp_path, monkeypatch):
    from wheelgap.cli import main as cli_main

    monkeypatch.chdir(tmp_path)
    assert cli_main(["mcp", *argv]) == code
    out, err = capsys.readouterr()
    if argv[0] in ("--version", "-V"):
        assert out == f"wheelgap-mcp {__version__}\n"
    else:
        assert err.startswith("usage: wheelgap mcp\n")
        assert "`wheelgap ./mcp`" in err
        assert "`wheelgap mcp==VERSION`" in err


def test_wheelgap_mcp_is_the_server_even_with_a_folder_named_mcp(fake_pypi, tmp_path, capsys, monkeypatch):
    from wheelgap.cli import main as cli_main

    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli_main(["mcp", "-p", "3.13", "-t", "linux-x86_64"]) == 2  # the server takes no arguments
    out, err = capsys.readouterr()
    assert out == ""
    assert "pin it: `wheelgap mcp==VERSION`" in err
    assert cli_main(["./mcp", "-p", "3.13", "-t", "linux-x86_64"]) == 0
    assert cli_main(["mcp/", "-p", "3.13", "-t", "linux-x86_64"]) == 0
    assert not [url for url in fake_pypi if "/mcp" in url]  # none of these looked up the PyPI package mcp


def test_wheelgap_mcp_notes_on_a_terminal(monkeypatch, capsys):
    class Terminal(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Terminal(io.BytesIO()))
    assert mcp.main([], prog="wheelgap mcp") == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("wheelgap mcp: waiting for an MCP client on stdin")
    assert "`wheelgap mcp==VERSION`" in err
    assert mcp.main([]) == 0  # wheelgap-mcp says nothing, as before
    assert capsys.readouterr().err == ""


def test_server_json_matches_the_package_version():
    # The release PR bumps these with pyproject.toml; the MCP Registry checks the PyPI release they name.
    server = json.loads((Path(__file__).parent.parent / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == __version__
    assert [p["version"] for p in server["packages"]] == [__version__]


def with_token(message: dict, token: object) -> dict:
    message["params"]["_meta"] = {"progressToken": token}
    return message


def ping(msg_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "ping"}


def is_progress(m: dict) -> bool:
    return m.get("method") == "notifications/progress"


class Recorder:
    """A stdout for serve that keeps the messages and lets the test wait for one."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._changed = threading.Condition()

    def write(self, data: bytes) -> None:
        with self._changed:
            self.messages += [json.loads(line) for line in data.splitlines()]
            self._changed.notify_all()

    def flush(self) -> None:
        pass

    def wait_for(self, predicate: Callable[[dict], bool]) -> Callable[[], None]:
        def wait() -> None:
            with self._changed:
                assert self._changed.wait_for(lambda: any(predicate(m) for m in self.messages), timeout=10)

        return wait


def live(*steps: object) -> Iterator[bytes]:
    """A stdin for serve: messages are sent one per line, and callables run before the next line is read."""
    for step in steps:
        if callable(step):
            step()
        else:
            yield json.dumps(step).encode() + b"\n"


@pytest.fixture
def slow_pypi(fake_pypi, monkeypatch):
    """fake_pypi, but every lookup waits until the test sets the returned event."""
    from wheelgap import pypi

    release = threading.Event()
    fast = pypi.fetch_json

    def fetch_json(url, timeout=20.0):
        assert release.wait(10), "the test never released the lookups"
        return fast(url, timeout)

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return release


@pytest.fixture
def three_pins(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("purewheel==3.1\nnowheels==0.9\nwinonly==311\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(("version", "token"), [("2025-06-18", "tok"), ("2024-11-05", 7)])
def test_check_lockfile_sends_progress_with_a_token(fake_pypi, three_pins, tmp_path, version, token):
    other = tmp_path / "other.txt"  # not yet cached, so the call without a token really looks it up
    other.write_text("fastlib==2.0.0\n", encoding="utf-8")
    replies = run(
        init(version),
        with_token(call("check_lockfile", path=three_pins, python="3.13"), token),
        call("check_lockfile", 2, path=str(other), python="3.13"),  # no token, no progress
    )
    progress = [r["params"] for r in replies if is_progress(r)]
    assert [(p["progressToken"], p["progress"], p["total"]) for p in progress] == [(token, n, 3) for n in range(4)]
    # message arrived in 2025-03-26.
    assert all(("message" in p) == (version != "2024-11-05") for p in progress)
    assert [r.get("id") for r in replies if not is_progress(r)] == [0, 1, 2]
    assert replies.index(next(r for r in replies if r.get("id") == 1)) > replies.index(
        next(filter(is_progress, replies))
    )


def test_check_package_sends_progress_with_a_token(fake_pypi):
    replies = run(init(), with_token(call("check_package", name="purewheel", python="3.13"), "t"))
    assert [r["params"]["progress"] for r in replies if is_progress(r)] == [0, 1]
    assert replies[-1]["id"] == 1 and replies[-1]["result"]["isError"] is False


def test_progress_counts_up_over_several_lookup_runs():
    sent: list[object] = []
    server = mcp.Server(sent.append)
    call_ = mcp._Call("t")
    for done, total in [(0, 2), (1, 2), (2, 2), (0, 3), (1, 3), (2, 3), (3, 3)]:  # e.g. a lock re-check
        server._progress(call_, done, total)
    server.close()
    assert [(m["params"]["progress"], m["params"]["total"]) for m in sent] == [
        (0, 2),
        (1, 2),
        (2, 2),
        (3, 5),
        (4, 5),
        (5, 5),
    ]


def test_ping_is_answered_while_a_call_runs(slow_pypi, three_pins):
    out = Recorder()
    mcp.serve(
        live(
            init(),
            with_token(call("check_lockfile", path=three_pins, python="3.13"), "t"),
            out.wait_for(is_progress),  # the call is running, its lookups held
            ping(5),
            out.wait_for(lambda m: m.get("id") == 5),
            slow_pypi.set,
        ),
        out,
    )
    assert [m["id"] for m in out.messages if "id" in m] == [0, 5, 1]
    assert out.messages[-1]["result"]["structuredContent"]["packages"] == 3
    assert [m["params"]["progress"] for m in out.messages if is_progress(m)] == [0, 1, 2, 3]


def test_cancelled_call_sends_no_result_or_more_progress(slow_pypi, three_pins):
    out = Recorder()
    mcp.serve(
        live(
            init(),
            with_token(call("check_lockfile", path=three_pins, python="3.13"), "t"),
            out.wait_for(is_progress),
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1, "reason": "user"}},
            ping(5),
            out.wait_for(lambda m: m.get("id") == 5),  # the cancel has been handled
            slow_pypi.set,
            call("list_targets", 2),  # the server carries on
        ),
        out,
    )
    assert [m["id"] for m in out.messages if "id" in m] == [0, 5, 2]
    assert [m["params"]["progress"] for m in out.messages if is_progress(m)] == [0]


def test_call_cancelled_while_queued_never_runs(fake_pypi, slow_pypi, three_pins):
    out = Recorder()
    mcp.serve(
        live(
            init(),
            with_token(call("check_lockfile", path=three_pins, python="3.13"), "t"),
            out.wait_for(is_progress),
            call("check_package", 2, name="fastlib", python="3.13"),  # waits for the worker
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}},
            ping(5),
            out.wait_for(lambda m: m.get("id") == 5),
            slow_pypi.set,
        ),
        out,
    )
    assert [m["id"] for m in out.messages if "id" in m] == [0, 5, 1]
    assert fake_pypi and not any("fastlib" in url for url in fake_pypi)


def test_batch_with_a_call_still_gets_one_list(fake_pypi):
    (reply,) = run([call("list_targets", 1), ping(2), {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert [r["id"] for r in reply] == [1, 2]
    assert reply[0]["result"]["isError"] is False


def test_a_result_that_isnt_json_is_still_answered(monkeypatch):
    monkeypatch.setitem(mcp._HANDLERS, "list_targets", lambda args: {"x": {1}})
    (_, reply) = run(init(), call("list_targets"))
    assert reply["id"] == 1
    assert reply["result"]["isError"] is True
    assert "TypeError" in reply["result"]["content"][0]["text"]


def test_a_crash_on_the_worker_is_still_answered(monkeypatch):
    def broken(self, name, arguments, version=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp.Server, "_call", broken)
    (_, reply) = run(init(), call("list_targets"))
    assert reply["id"] == 1
    assert reply["error"]["code"] == mcp.INTERNAL_ERROR


def test_check_dockerfile_without_a_lockfile_next_to_it_says_to_pass_path(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-alpine\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(tmp_path / "Dockerfile")))
    assert reply["result"]["isError"] is True
    assert "pass 'path'" in reply["result"]["content"][0]["text"]


def test_check_dockerfile_errors_carry_no_cli_advice_where_none_applies(tmp_path):
    (tmp_path / "Dockerfile").write_text("RUN echo hi\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=str(tmp_path / "Dockerfile"), path=str(tmp_path)))
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True and "check_lockfile" not in text


def init_with_roots() -> dict:
    message = init()
    message["params"]["capabilities"] = {"roots": {"listChanged": True}}
    return message


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def roots_answer(*paths, msg_id: str = "wheelgap-roots-1") -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"roots": [{"uri": p.as_uri(), "name": p.name} for p in paths]}}


def test_check_lockfile_accepts_a_file_uri(fake_pypi, fixtures, tmp_path):
    project = tmp_path / "my app"  # a space, so the URI is percent-encoded
    project.mkdir()
    (project / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    uri = (project / "uv.lock").as_uri()
    assert "%20" in uri
    (_, reply) = run(init(), call("check_lockfile", path=uri, python="3.13", targets=["linux-x86_64"]))
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    assert data["path"] == str((project / "uv.lock").resolve())
    assert {g["name"] for g in data["gaps"]} == {"nativeext", "puresdist"}


def test_check_dockerfile_accepts_file_uris(fake_pypi, tmp_path, fixtures):
    dockerfile = dockerfile_project(tmp_path, fixtures, "FROM python:3.13-alpine\n")
    (_, reply) = run(init(), call("check_dockerfile", dockerfile=dockerfile.as_uri(), path=tmp_path.as_uri()))
    assert reply["result"]["structuredContent"]["targets"] == ["3.13 musl-x86_64"]


@pytest.mark.parametrize("uri", ["https://example.com/uv.lock", "ftp://example.com/uv.lock"])
def test_other_uris_are_tool_errors(fake_pypi, uri):
    (_, reply) = run(init(), call("check_lockfile", path=uri))
    assert reply["result"]["isError"] is True
    assert "only file:// URIs" in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_a_file_uri_for_another_host_is_unc_on_windows_only(fake_pypi):
    uri = "file://otherhost/srv/app/uv.lock"
    if os.name == "nt":  # a UNC path; not opened here, so the test doesn't wait on the network
        assert mcp._file_uri_path(uri) == mcp.Path(r"\\otherhost\srv\app\uv.lock")
        return
    assert mcp._file_uri_path(uri) is None
    (_, reply) = run(init(), call("check_lockfile", path=uri))
    assert reply["result"]["isError"] is True
    assert "only file:// URIs" in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_file_uri_to_path_handles_localhost_and_windows_drives(monkeypatch):
    assert mcp._file_uri_path("file://localhost/srv/app/uv.lock") == mcp.Path("/srv/app/uv.lock")
    assert mcp._file_uri_path("FILE:///srv/a%20b") == mcp.Path("/srv/a b")
    assert mcp._file_uri_path("https://example.com/x") is None
    # On Windows, url2pathname turns /C:/... into C:\...; here, check the drive is passed to it intact.
    seen = []
    monkeypatch.setattr(mcp.urllib.request, "url2pathname", lambda p: seen.append(p) or p)
    mcp._file_uri_path("file:///C:/Users/me/app/uv.lock")
    assert seen == ["/C:/Users/me/app/uv.lock"]


def test_relative_path_resolves_against_the_client_roots(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the server's working directory has no uvproj
    other = tmp_path / "other"
    other.mkdir()
    replies = run(
        init_with_roots(),
        INITIALIZED,
        roots_answer(other, fixtures),  # the first root that contains the path wins
        call("check_lockfile", path="uvproj/uv.lock", python="3.13", targets=["linux-x86_64"]),
    )
    (_, request, reply) = replies
    assert request == {"jsonrpc": "2.0", "id": "wheelgap-roots-1", "method": "roots/list"}
    assert reply["id"] == 1 and reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["path"] == str((fixtures / "uvproj" / "uv.lock").resolve())


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18"])
def test_first_relative_call_waits_for_delayed_roots(fake_pypi, fixtures, tmp_path, monkeypatch, version):
    monkeypatch.chdir(tmp_path)
    entered = threading.Event()
    original = mcp.Server._call

    def started(self, *args, **kwargs):
        entered.set()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mcp.Server, "_call", started)
    out = Recorder()

    def delay():
        assert entered.wait(10)
        threading.Event().wait(0.05)  # the SDK's roots callback may finish after its first tools/call

    message = init_with_roots()
    message["params"]["protocolVersion"] = version
    mcp.serve(
        live(
            message,
            INITIALIZED,
            call("check_lockfile", path="uvproj/uv.lock", python="3.13", targets=["linux-x86_64"]),
            delay,
            ping(2),
            out.wait_for(lambda m: m.get("id") == 2),  # stdin remains responsive during the wait
            roots_answer(fixtures),
        ),
        out,
    )
    reply = next(m for m in out.messages if m.get("id") == 1)
    assert reply["result"]["isError"] is False
    data = json.loads(reply["result"]["content"][0]["text"])
    assert data["path"] == str((fixtures / "uvproj" / "uv.lock").resolve())


def test_roots_timeout_is_noted_and_a_late_answer_is_used(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(fixtures)
    monkeypatch.setattr(mcp, "_ROOTS_WAIT_SECONDS", 0.01)
    out = Recorder()
    mcp.serve(
        live(
            init_with_roots(),
            INITIALIZED,
            call("check_lockfile", path="uvproj", python="3.13"),
            out.wait_for(lambda m: m.get("id") == 1),  # a silent client doesn't hold the call forever
            roots_answer(fixtures),
            call("check_lockfile", 2, path="uvproj", python="3.13"),
        ),
        out,
    )
    first, second = [m["result"]["structuredContent"] for m in out.messages if m.get("id") in (1, 2)]
    assert first["path"] == second["path"] == str((fixtures / "uvproj").resolve())
    assert sum("roots/list did not answer within 0.01 s" in note for note in first["notes"]) == 1
    assert json.loads(next(m for m in out.messages if m.get("id") == 1)["result"]["content"][0]["text"]) == first
    assert not any("roots/list" in note for note in second["notes"])


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18", "2025-11-25"])
def test_roots_timeout_is_noted_when_relative_path_fails(fake_pypi, tmp_path, monkeypatch, version):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp, "_ROOTS_WAIT_SECONDS", 0.01)
    message = init_with_roots()
    message["params"]["protocolVersion"] = version
    (_, _, reply) = run(message, INITIALIZED, call("check_lockfile", path="uvproj", python="3.13"))
    result = reply["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "uvproj" in text and "no such file" in text
    assert "\n\nroots/list did not answer within 0.01 s; relative paths use the last known roots" in text
    assert text.count("roots/list") == 1
    assert not fake_pypi


@pytest.mark.parametrize("error", [mcp.ToolError, mcp.TargetError, mcp.SourceError])
@pytest.mark.parametrize("notes", [(), ("first note", "second note")])
def test_tool_errors_preserve_notes(monkeypatch, error, notes):
    def fail(arguments):
        raise error("original error")

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", fail)
    server = mcp.Server(lambda message: None)
    result = server._tool_result("list_targets", {}, "2025-06-18", (), False, notes)
    expected = "original error" + ("\n\n" + "\n".join(notes) if notes else "")
    assert result == {"content": [{"type": "text", "text": expected}], "isError": True}


def test_roots_timeout_waits_once_per_request(fake_pypi, fixtures, monkeypatch):
    # A client that never answers roots/list costs the wait once; later calls fall back at once, with the note.
    monkeypatch.chdir(fixtures)
    monkeypatch.setattr(mcp, "_ROOTS_WAIT_SECONDS", 0.01)
    waits = []
    original_init = mcp.Server.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        wait_for = self._roots_changed.wait_for

        def counted(*a, **k):
            waits.append(1)
            return wait_for(*a, **k)

        self._roots_changed.wait_for = counted

    monkeypatch.setattr(mcp.Server, "__init__", init)
    out = Recorder()
    mcp.serve(
        live(
            init_with_roots(),
            INITIALIZED,
            call("check_lockfile", path="uvproj", python="3.13"),
            out.wait_for(lambda m: m.get("id") == 1),
            call("check_lockfile", 2, path="uvproj", python="3.13"),
            out.wait_for(lambda m: m.get("id") == 2),
            call("list_targets", 3),
            out.wait_for(lambda m: m.get("id") == 3),
        ),
        out,
    )
    results = [m["result"]["structuredContent"] for m in out.messages if m.get("id") in (1, 2)]
    assert len(results) == 2 and len(waits) == 1
    assert all(any("roots/list did not answer" in note for note in r["notes"]) for r in results)
    # A call with no relative path gets no note about them.
    (targets,) = [m["result"]["structuredContent"] for m in out.messages if m.get("id") == 3]
    assert "roots/list" not in json.dumps(targets)


@pytest.mark.parametrize("answer", [roots_answer(), {"jsonrpc": "2.0", "id": "wheelgap-roots-1", "error": {}}])
def test_empty_or_failed_roots_answer_releases_the_wait(fake_pypi, fixtures, monkeypatch, answer):
    monkeypatch.chdir(fixtures)
    (_, _, reply) = run(init_with_roots(), INITIALIZED, call("check_lockfile", path="uvproj", python="3.13"), answer)
    assert reply["result"]["isError"] is False
    assert not any("roots/list" in note for note in reply["result"]["structuredContent"]["notes"])


@pytest.mark.parametrize("kind", ["absolute", "no-path", "modern"])
def test_calls_that_need_no_handshake_roots_do_not_wait(fake_pypi, fixtures, monkeypatch, kind):
    sent = Recorder()

    def send(message):
        sent.write(json.dumps(message).encode())

    server = mcp.Server(send)

    def unexpected_wait(*args, **kwargs):
        pytest.fail("this call must not wait for handshake roots")

    monkeypatch.setattr(server._roots_changed, "wait_for", unexpected_wait)
    server.handle(init_with_roots())
    server.handle(INITIALIZED)
    if kind == "absolute":
        message = call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13")
    elif kind == "no-path":
        message = call("list_targets")
    else:
        message = with_roots(modern_call("check_lockfile", path="uvproj", python="3.13"), roots_answer_for(fixtures))
    try:
        server.handle(message)
        sent.wait_for(lambda m: m.get("id") == 1)()
    finally:
        server.close()
    reply = next(m for m in sent.messages if m.get("id") == 1)
    assert reply["result"]["isError"] is False


def test_roots_list_changed_asks_again(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    replies = run(
        init_with_roots(),
        INITIALIZED,
        roots_answer(tmp_path),
        {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
        roots_answer(fixtures, msg_id="wheelgap-roots-2"),
        call("check_lockfile", path="uvproj", python="3.13", targets=["linux-x86_64"]),
    )
    assert [r.get("method") for r in replies] == [None, "roots/list", "roots/list", None]
    assert replies[2]["id"] == "wheelgap-roots-2"
    assert replies[3]["result"]["isError"] is False


def test_no_roots_request_without_the_capability(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (_, reply) = run(init(), INITIALIZED, call("check_lockfile", path="uvproj"))
    assert "roots/list" not in json.dumps(reply)
    assert reply["result"]["isError"] is True and "no such file or directory" in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "answer",
    [
        {"jsonrpc": "2.0", "id": "wheelgap-roots-1", "error": {"code": -32601, "message": "no roots"}},
        {"jsonrpc": "2.0", "id": "wheelgap-roots-1", "result": {"roots": "nope"}},
        {"jsonrpc": "2.0", "id": "wheelgap-roots-1", "result": {"roots": [{"uri": "https://example.com/"}, 3]}},
    ],
)
def test_a_bad_roots_answer_means_the_working_directory(fake_pypi, fixtures, monkeypatch, answer):
    monkeypatch.chdir(fixtures)
    (_, _, reply) = run(init_with_roots(), INITIALIZED, answer, call("check_lockfile", path="uvproj", python="3.13"))
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["path"] == str((fixtures / "uvproj").resolve())


def test_relative_path_in_no_root_falls_back_to_the_working_directory(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(fixtures)
    (_, _, reply) = run(
        init_with_roots(), INITIALIZED, roots_answer(tmp_path), call("check_lockfile", path="uvproj", python="3.13")
    )
    assert reply["result"]["structuredContent"]["path"] == str((fixtures / "uvproj").resolve())


def test_an_error_response_with_no_id_leaves_the_roots_alone(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    replies = run(
        init_with_roots(),
        INITIALIZED,
        roots_answer(fixtures),
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},  # not an answer to us
        call("check_lockfile", path="uvproj", python="3.13", targets=["linux-x86_64"]),
    )
    assert replies[-1]["result"]["isError"] is False


def lockfile_pair(tmp_path, old: str, new: str) -> tuple[str, str]:
    """Two small uv.lock files, the root project depending on the given [[package]] entries."""
    head = 'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
    paths = []
    for side, packages in (("old", old), ("new", new)):
        names = [line.split('"')[1] for line in packages.splitlines() if line.startswith("name = ")]
        root = '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { virtual = "." }\ndependencies = [\n'
        root += "".join(f'    {{ name = "{n}" }},\n' for n in names) + "]\n\n"
        (tmp_path / side).mkdir()
        path = tmp_path / side / "uv.lock"
        path.write_text(head + root + packages, encoding="utf-8")
        paths.append(str(path))
    return paths[0], paths[1]


def uv_package(name: str, version: str, kind: str = "wheel", *, tags: tuple[str, ...] = ("py3-none-any",)) -> str:
    """A uv.lock [[package]] entry with wheels or an sdist, so no lookup is needed."""
    url = f"https://files.pythonhosted.org/packages/aa/{name}-{version}"
    if kind == "wheel":
        files = (
            "wheels = [\n"
            + "".join(f'    {{ url = "{url}-{tag}.whl", hash = "sha256:00", size = 1 }},\n' for tag in tags)
            + "]\n"
        )
    else:
        files = f'sdist = {{ url = "{url}.tar.gz", hash = "sha256:00", size = 1 }}\n'
    return (
        f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        + files
        + "\n"
    )


@pytest.mark.parametrize("tool", ["check_package", "check_lockfile", "check_dist", "diff_lockfiles"])
def test_mcp_mobile_version_floors(fake_pypi, tmp_path, monkeypatch, tool):
    tags = ("cp313-cp313-ios_15_0_arm64_iphoneos", "cp313-cp313-android_26_arm64_v8a")
    filenames = [f"mobilefloors-1.1-{tag}.whl" for tag in tags]
    monkeypatch.setitem(FAKE_INDEX, "mobilefloors", {"1.1": filenames})
    old, new = lockfile_pair(tmp_path, uv_package("mobilefloors", "1.0"), uv_package("mobilefloors", "1.1", tags=tags))
    folder = dist(tmp_path, *filenames)
    inputs = {
        "check_package": {"name": "mobilefloors", "version": "1.1"},
        "check_lockfile": {"path": new, "offline": True},
        "check_dist": {"path": str(folder)},
        "diff_lockfiles": {"old": old, "new": new},
    }
    args = inputs[tool] | {"python": "3.13", "targets": ["ios-arm64", "android-arm64"]}
    _, default, bad_ios, bad_api, good = run(
        init(),
        call(tool, **args),
        call(tool, 2, **args, ios="abc"),
        call(tool, 3, **args, android_api=26),
        call(tool, 4, **args, ios="15.0", android_api="26"),
    )
    for reply, message in ((bad_ios, "can't parse iOS version"), (bad_api, "'android_api' must be a string")):
        assert reply["result"]["isError"] is True
        assert message in reply["result"]["content"][0]["text"]
    assert default["result"]["isError"] is False and good["result"]["isError"] is False
    default_data = default["result"]["structuredContent"]
    good_data = good["result"]["structuredContent"]
    assert default_data["ok"] is False and good_data["ok"] is True
    missing = {"3.13 ios-arm64": "missing", "3.13 android-arm64": "missing"}
    if tool == "check_package":
        assert default_data["results"] == missing
        assert good_data["results"] == dict.fromkeys(missing, "wheel")
        assert good_data["assumes"]["ios"] == "15.0" and good_data["assumes"]["android_api"] == "26"
    elif tool == "diff_lockfiles":
        assert default_data["worse"][0]["targets"] == {
            target: {"old_status": "wheel", "new_status": "missing"} for target in ("ios-arm64", "android-arm64")
        }
        assert good_data["worse"] == []
    else:
        assert default_data["gaps"][0]["results"] == missing and good_data["gaps"] == []


@pytest.mark.parametrize(
    ("floor", "value", "target", "tag"),
    [
        ("glibc", "2.17", "linux-x86_64", "manylinux_2_28_x86_64"),
        ("musl", "1.1", "musl-x86_64", "musllinux_1_2_x86_64"),
        ("macos", "13.0", "macos-arm64", "macosx_14_0_arm64"),
    ],
)
def test_diff_lockfiles_desktop_version_floors(fake_pypi, tmp_path, floor, value, target, tag):
    old, new = lockfile_pair(
        tmp_path, uv_package("floorpkg", "1.0"), uv_package("floorpkg", "1.1", tags=(f"cp313-cp313-{tag}",))
    )
    args = {"old": old, "new": new, "python": "3.13", "targets": [target]}
    _, default, override = run(
        init(), call("diff_lockfiles", **args), call("diff_lockfiles", 2, **args, **{floor: value})
    )
    assert default["result"]["structuredContent"]["worse"] == []
    assert override["result"]["structuredContent"]["worse"][0]["targets"] == {
        target: {"old_status": "wheel", "new_status": "missing"}
    }
    assert not fake_pypi


def test_diff_lockfiles_reports_a_wheel_lost_to_source(fake_pypi, tmp_path):
    old, new = lockfile_pair(
        tmp_path,
        uv_package("ranged", "1.5") + uv_package("purewheel", "3.1"),
        uv_package("ranged", "1.9", "sdist") + uv_package("purewheel", "3.1") + uv_package("nowheels", "0.9", "sdist"),
    )
    (_, reply) = run(init(), call("diff_lockfiles", old=old, new=new, python="3.13", targets=["linux-x86_64"]))
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert data["worse"] == [
        {
            "name": "ranged",
            "old_versions": ["1.5"],
            "new_versions": ["1.9"],
            "targets": {"linux-x86_64": {"old_status": "wheel", "new_status": "source"}},
        }
    ]
    assert data["new"] == [{"name": "nowheels", "versions": ["0.9"], "targets": {"linux-x86_64": {"status": "source"}}}]
    assert data["summary"] == {"worse": 1, "new": 1}
    assert data["paths"] == {"old": old, "new": new}
    assert "version" not in data


def test_diff_lockfiles_matches_wheelgap_diff_json(fake_pypi, tmp_path, capsys):
    from wheelgap import diff

    old, new = lockfile_pair(tmp_path, uv_package("ranged", "1.5"), uv_package("ranged", "1.9", "sdist"))
    argv = [old, new, "-p", "3.13", "-t", "linux-x86_64", "-t", "win-amd64", "--json"]
    assert diff.main(argv) == 1
    expected = json.loads(capsys.readouterr().out)
    (_, reply) = run(
        init(), call("diff_lockfiles", old=old, new=new, python="3.13", targets=["linux-x86_64", "win-amd64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["worse"]
    for key in ("targets", "worse", "new", "unchecked", "summary"):
        assert data[key] == expected[key]


def test_diff_lockfiles_identical_files_are_ok(fake_pypi, tmp_path):
    text = uv_package("ranged", "1.9", "sdist") + uv_package("purewheel", "3.1")
    old, new = lockfile_pair(tmp_path, text, text)
    (_, reply) = run(init(), call("diff_lockfiles", old=old, new=new, python="3.13", targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["ok"] is True
    assert data["worse"] == [] and data["new"] == [] and data["summary"] == {"worse": 0, "new": 0}


def test_diff_lockfiles_looks_requirements_up(fake_pypi, tmp_path):
    # Requirements files record no files, so every package is looked up on the index.
    (tmp_path / "old.txt").write_text("ranged==1.5\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("ranged==1.9\n", encoding="utf-8")
    arguments = {"old": str(tmp_path / "old.txt"), "new": str(tmp_path / "new.txt"), "python": "3.13"}
    (_, reply) = run(init(), call("diff_lockfiles", **arguments, targets=["linux-x86_64"]))
    assert [w["name"] for w in reply["result"]["structuredContent"]["worse"]] == ["ranged"]
    assert fake_pypi


def test_diff_lockfiles_malformed_lockfile_is_a_tool_error(fake_pypi, tmp_path):
    old, new = lockfile_pair(tmp_path, uv_package("purewheel", "3.1"), uv_package("purewheel", "3.1"))
    Path(new).write_text("[[package\n", encoding="utf-8")
    (_, reply) = run(init(), call("diff_lockfiles", old=old, new=new, python="3.13", targets=["linux-x86_64"]))
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True and "internal error" not in text


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"old": "/no/such/uv.lock"}, "no such file or directory"),
        ({"new": "/no/such/uv.lock"}, "no such file or directory"),
        ({"old": None}, "'old' is required"),
        ({"offline": True}, "unknown argument(s): offline"),  # wheelgap diff has no offline mode
        ({"old": "{tmp}/old.lock"}, "don't know how to read"),  # the saved copy needs its real name
        ({"targets": ["linux-aarch46"]}, "linux-aarch46"),
        ({"python": "three"}, "can't read Python version"),
        ({"glibc": "abc"}, "can't parse glibc version"),
        ({"old": "{tmp}/notes.md"}, "don't know how to read"),
        ({"old": "{tmp}/empty"}, "no lockfile found"),
    ],
)
def test_diff_lockfiles_errors(fake_pypi, tmp_path, arguments, message):
    old, new = lockfile_pair(tmp_path, uv_package("purewheel", "3.1"), uv_package("purewheel", "3.1"))
    (tmp_path / "notes.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / "old.lock").write_text(Path(old).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "empty").mkdir()
    merged = {"old": old, "new": new} | {
        k: v.format(tmp=tmp_path) if isinstance(v, str) else v for k, v in arguments.items()
    }
    (_, reply) = run(init(), call("diff_lockfiles", **{k: v for k, v in merged.items() if v is not None}))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def install_log(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")  # universal newlines: a Windows checkout may have CRLF


def explain_failure(**arguments: object) -> dict:
    (_, reply) = run(init(), call("explain_install_failure", **arguments))
    return reply["result"]


def test_explain_install_failure_uv_log(fake_pypi):
    result = explain_failure(log=install_log("uv-ansi.log"), python="3.13", targets=["win-arm64"])
    assert result["isError"] is False
    data = result["structuredContent"]
    # One shape whatever the count: the package's own keys never land at the top level.
    assert set(data) == {"targets", "ok", "packages", "assumes", "notes"}
    assert (data["targets"], data["ok"], data["notes"]) == (["3.13 win-arm64"], False, [])
    assert data["assumes"] == {"glibc": "2.36", "musl": "1.2", "macos": "14.0"}
    (pkg,) = data["packages"]
    assert (pkg["name"], pkg["version"]) == ("nowheels", "0.9")
    assert pkg["results"] == {"3.13 win-arm64": "source"}
    assert (pkg["ok"], pkg["gaps"]) == (False, ["3.13 win-arm64"])
    assert (pkg["rejected"], pkg["rejected_omitted"]) == ({"3.13 win-arm64": []}, {})  # an sdist only
    assert (pkg["suggest"], pkg["last_with_wheel"]) == ({}, {})  # no other release
    assert sorted(fake_pypi) == ["https://pypi.org/pypi/nowheels/0.9/json", "https://pypi.org/pypi/nowheels/json"]


def test_explain_install_failure_pip_log_with_two_failures(fake_pypi):
    result = explain_failure(log=install_log("pip24-two-failed.log"), python="3.13", targets=["win-amd64"])
    data = result["structuredContent"]
    assert (data["targets"], data["ok"], data["notes"]) == (["3.13 win-amd64"], False, [])
    fastlib, laggard = data["packages"]  # in the order the log names them
    # fastlib's version is from the `Downloading` line after an unpinned `Collecting`.
    assert (fastlib["name"], fastlib["version"]) == ("fastlib", "2.0.0")
    assert fastlib["results"] == {"3.13 win-amd64": "source"}
    assert (fastlib["ok"], fastlib["gaps"]) == (False, ["3.13 win-amd64"])
    assert [w["file"] for w in fastlib["rejected"]["3.13 win-amd64"]] == [
        "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl"
    ]
    assert (fastlib["suggest"], fastlib["last_with_wheel"]) == ({}, {})
    assert (laggard["name"], laggard["version"]) == ("laggard", "1.0")
    assert laggard["results"] == {"3.13 win-amd64": "source"}
    assert (laggard["ok"], laggard["gaps"]) == (False, ["3.13 win-amd64"])
    assert laggard["rejected"] == {"3.13 win-amd64": []}
    assert (laggard["suggest"], laggard["last_with_wheel"]) == ({"3.13 win-amd64": "1.1"}, {})
    # One lookup per package, and one batch of release lists for --suggest's way out.
    assert sorted(fake_pypi) == [
        "https://pypi.org/pypi/fastlib/2.0.0/json",
        "https://pypi.org/pypi/fastlib/json",
        "https://pypi.org/pypi/laggard/1.0/json",
        "https://pypi.org/pypi/laggard/json",
    ]


def test_explain_install_failure_suggestions_are_one_lookup(fake_pypi, monkeypatch):
    batches = []
    real = mcp.release_suggest.look_up_all

    def counted(wanted):
        batches.append(sorted(wanted))
        return real(wanted)

    monkeypatch.setattr(mcp.release_suggest, "look_up_all", counted)
    explain_failure(log=install_log("pip24-two-failed.log"), python="3.13", targets=["win-amd64"])
    assert batches == [["fastlib", "laggard"]]


def test_explain_install_failure_notes_where_the_log_likely_comes_from(fake_pypi):
    # fastlib has a wheel for linux-x86_64, so the install that failed to build it didn't run there.
    result = explain_failure(
        log=install_log("pip24-two-failed.log"), python="3.13", targets=["linux-x86_64"], glibc="2.28"
    )
    data = result["structuredContent"]
    assert data["assumes"]["glibc"] == "2.28"
    assert {p["name"]: p["results"] for p in data["packages"]} == {
        "fastlib": {"3.13 linux-x86_64": "wheel"},
        "laggard": {"3.13 linux-x86_64": "source"},
    }
    assert data["ok"] is False  # laggard still builds from source
    assert data["notes"] == [
        "the install log says fastlib failed to build, but fastlib 2.0.0 has a wheel for 3.13 linux-x86_64: the log "
        "likely comes from another platform or Python than the one checked (python, targets)"
    ]


@pytest.mark.parametrize(
    ("arguments", "assumed"),
    [
        ({}, "the server's Python and the default platforms"),
        ({"python": "3.13"}, "the default platforms"),
        ({"targets": ["win-arm64"]}, "the server's Python"),
    ],
)
def test_explain_install_failure_says_what_it_assumed(fake_pypi, arguments, assumed):
    data = explain_failure(log=install_log("uv-ansi.log"), **arguments)["structuredContent"]
    assert data["notes"][0] == (
        f"the log doesn't say which Python and platform the install ran on, so this checked {assumed}: pass python "
        "and targets for the machine the install ran on"
    )
    if "targets" not in arguments:
        assert len(data["targets"]) == 4  # the default platforms


def test_explain_install_failure_log_with_no_failure_is_an_error(fake_pypi):
    result = explain_failure(log=install_log("pip-succeeded.log"), python="3.13", targets=["win-amd64"])
    assert result["isError"] is True
    assert result["content"][0]["text"] == mcp._LOG_ERRORS[mcp.NO_FAILURE]
    assert "found no package that failed to build in the log" in result["content"][0]["text"]
    assert fake_pypi == []


def test_explain_install_failure_refusal_gives_the_tools_advice(fake_pypi):
    result = explain_failure(log=install_log("pip-no-version.log"), python="3.13", targets=["win-amd64"])
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text.startswith("the log doesn't say which version of nowheels failed to build")
    assert "(call check_package for nowheels with the version)" in text
    assert "--from-log" not in text and "==VERSION" not in text
    assert fake_pypi == []


@pytest.mark.parametrize(
    "log",
    [
        "x" * (1024 * 1024 + 1),
        "é" * (512 * 1024 + 1),  # measured in UTF-8 bytes, not characters
        "\ud800" * (350 * 1024),  # a lone surrogate counts too, rather than failing to encode
    ],
)
def test_explain_install_failure_oversize_log_is_an_error(fake_pypi, log):
    result = explain_failure(log=log, python="3.13", targets=["win-amd64"])
    assert result["isError"] is True
    assert "over the 1 MB (1048576 bytes) limit" in result["content"][0]["text"]
    assert fake_pypi == []


def test_explain_install_failure_lone_surrogate_within_the_limit_is_read(fake_pypi):
    data = explain_failure(log="\ud800\n" + install_log("uv-ansi.log"), python="3.13", targets=["win-arm64"])
    assert [p["name"] for p in data["structuredContent"]["packages"]] == ["nowheels"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "'log' is required"),
        ({"log": ""}, "'log' must be a non-empty string"),
        ({"log": "  \n"}, "'log' must be a non-empty string"),
        ({"log": 1}, "'log' must be a non-empty string"),
        ({"log": "x", "platforms": ["win-amd64"]}, "unknown argument(s): platforms"),
        ({"log": "x", "ios": "15.0", "android_api": "26"}, "unknown argument(s): android_api, ios"),
        ({"log": "x", "targets": ["linux-aarch46"]}, "linux-aarch46"),
        ({"log": "x", "python": "three"}, "can't read Python version"),
    ],
)
def test_explain_install_failure_rejects_invalid_arguments(fake_pypi, arguments, message):
    result = explain_failure(**arguments)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert fake_pypi == []


SENTINEL = "SENTINEL-c0ffee"


@pytest.mark.parametrize(
    ("name", "before", "after", "kind"),
    [
        (
            "pip23-laggard.log",
            "",
            f"Looking in indexes: https://pypi.org/simple, https://{SENTINEL}.example/simple\n",
            mcp.OTHER_INDEX,
        ),
        ("pip23-laggard.log", "", f"Looking in links: /wheels/{SENTINEL}\n", mcp.FIND_LINKS),
        ("pdm-verbose.log", "https://pypi.org/simple/pyaudio/", f"https://{SENTINEL}.example/simple/pyaudio/", None),
    ],
)
def test_explain_install_failure_never_echoes_the_logs_index(fake_pypi, name, before, after, kind):
    # --from-log's message quotes the log's `Looking in indexes:` / `Looking in links:` URLs (and PDM's build link):
    # the tool says it in its own words, pointing to what an agent can do.
    text = install_log(name)
    log = text.replace(before, after) if before else after + text
    assert SENTINEL in log
    result = explain_failure(log=log, python="3.13", targets=["win-amd64"])
    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert SENTINEL not in message
    assert message == mcp._LOG_ERRORS[kind or mcp.OTHER_INDEX]
    assert "WHEELGAP_INDEX_URL" in message and "check_package" in message
    assert "--index-url" not in message
    assert fake_pypi == []


def test_explain_install_failure_log_from_another_index_is_checked_there(fake_pypi, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    log = f"Looking in indexes: https://{SENTINEL}.example/simple\n" + install_log("pip23-laggard.log")
    result = explain_failure(log=log, python="3.13", targets=["win-amd64"])
    assert result["isError"] is False
    (pkg,) = result["structuredContent"]["packages"]
    assert (pkg["name"], pkg["version"], pkg["results"]) == ("laggard", "1.0", {"3.13 win-amd64": "source"})
    assert "suggest" not in pkg  # another index may hold private names: nothing is looked up, as --suggest
    assert SENTINEL not in json.dumps(result)
    assert fake_pypi and all(url.startswith("https://mirror.example.com/pypi/") for url in fake_pypi)


def test_mcp_docs_name_every_tool():
    """docs/mcp.md lists the server's tools; a new tool should be described there too."""
    text = (Path(__file__).resolve().parent.parent / "docs" / "mcp.md").read_text(encoding="utf-8")
    for tool in mcp.TOOLS:
        assert f"`{tool['name']}`" in text


def test_mcp_docs_name_every_tool_argument():
    """docs/mcp.md lists exactly the arguments each tool's inputSchema accepts in that tool's own table, so a new one
    can't go undocumented, a removed one can't stay, and one tool's row can't stand in for another's."""
    text = (Path(__file__).resolve().parent.parent / "docs" / "mcp.md").read_text(encoding="utf-8")
    sections = dict(re.findall(r"^#### `(\w+)`\n(.*?)(?=^#|\Z)", text, re.M | re.S))
    assert set(sections) == {tool["name"] for tool in mcp.TOOLS}
    for tool in mcp.TOOLS:
        documented = set(re.findall(r"^\| `(\w+)` \|", sections[tool["name"]], re.M))
        assert documented == set(tool["inputSchema"]["properties"]), tool["name"]


# The 2026-07-28 revision: no handshake; every request carries its version and the client's capabilities in _meta.

MODERN = "2026-07-28"
SERVED = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_META = {"io.modelcontextprotocol/serverInfo": {"name": "wheelgap", "version": __version__}}


def envelope(version: object = MODERN, **extra: object) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
        **extra,
    }


def modern(method: str, msg_id: int = 1, version: object = MODERN, **params: object) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": {"_meta": envelope(version), **params}}


def modern_call(tool: str, msg_id: int = 1, **arguments: object) -> dict:
    return modern("tools/call", msg_id, name=tool, arguments=arguments)


def test_server_discover():
    (reply,) = run(modern("server/discover"))  # no initialize first
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "complete",
            "supportedVersions": [MODERN],
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": mcp.INSTRUCTIONS,
            "ttlMs": 3_600_000,
            "cacheScope": "public",
            "_meta": SERVER_META,
        },
    }


def test_modern_tools_list():
    (reply,) = run(modern("tools/list"))
    result = reply["result"]
    assert result["resultType"] == "complete"
    assert result["tools"] == mcp.TOOLS
    assert (result["ttlMs"], result["cacheScope"]) == (3_600_000, "public")
    assert result["_meta"] == SERVER_META


def test_modern_tools_call(fake_pypi):
    (reply, error) = run(
        modern_call("check_package", name="winonly", python="3.13", targets=["win-amd64", "linux/arm64"]),
        modern_call("check_package", 2, name="numpy==2.0"),
    )
    result = reply["result"]
    assert result["resultType"] == "complete"
    assert result["_meta"] == SERVER_META
    assert result["isError"] is False
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    assert result["structuredContent"]["results"] == {"3.13 win-amd64": "wheel", "3.13 linux-aarch64": "missing"}
    # A tool error is a complete result too.
    assert (error["result"]["resultType"], error["result"]["isError"]) == ("complete", True)
    assert error["result"]["_meta"] == SERVER_META


@pytest.mark.parametrize("requested", ["1900-01-01", "2027-01-01", "2025-11-25"])
def test_unsupported_protocol_version_lists_the_supported_ones(fake_pypi, requested):
    replies = run(
        modern("server/discover", version=requested),
        modern_call("list_targets", 2) | {"params": {"_meta": envelope(requested), "name": "list_targets"}},
    )
    for msg_id, reply in zip([1, 2], replies, strict=True):
        assert reply["id"] == msg_id
        assert reply["error"]["code"] == mcp.UNSUPPORTED_PROTOCOL_VERSION == -32022
        assert reply["error"]["data"] == {"supported": SERVED, "requested": requested}
    message = replies[0]["error"]["message"]
    if requested == "2025-11-25":  # supported, but not this way
        assert message == "Protocol version 2025-11-25 is served only through the initialize handshake."
    else:
        assert message == f"Unsupported protocol version {requested!r}."


def test_unsupported_protocol_version_is_echoed_shortened():
    (reply,) = run(modern("server/discover", version="9" * 10_000))
    assert reply["error"]["data"]["requested"] == "9" * 100
    assert len(reply["error"]["message"]) < 150


@pytest.mark.parametrize(
    ("meta", "missing"),
    [
        (None, "protocolVersion"),
        ({}, "protocolVersion"),
        ({"io.modelcontextprotocol/protocolVersion": MODERN}, "clientCapabilities"),
        ({"io.modelcontextprotocol/clientCapabilities": {}}, "protocolVersion"),
        ({"io.modelcontextprotocol/protocolVersion": 2026, "io.modelcontextprotocol/clientCapabilities": {}}, "string"),
        (
            {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": []},
            "object",
        ),
    ],
)
def test_malformed_envelope_is_invalid_params(meta, missing):
    # A legacy client never sends server/discover, and a 2026 client that probes with it falls back on any error but
    # -32022: so a malformed probe must not get -32022.
    message: dict = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    if meta is not None:
        message["params"] = {"_meta": meta}
    replies = run(message, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta or {}}})
    assert replies[0]["error"]["code"] == mcp.INVALID_PARAMS
    assert missing in replies[0]["error"]["message"]
    # On other methods, 2026's per-request keys make a request a 2026 one: without any, it's the handshake's.
    if meta:
        assert replies[1]["error"]["code"] == mcp.INVALID_PARAMS
    else:
        assert replies[1]["result"] == {"tools": mcp.TOOLS}


def test_ping_is_gone_in_2026_but_not_in_the_handshake():
    replies = run(modern("ping"), ping(2), init(), ping(3))
    assert replies[0]["error"]["code"] == mcp.METHOD_NOT_FOUND
    assert (replies[1]["result"], replies[3]["result"]) == ({}, {})


def test_initialize_is_the_handshake_even_with_the_2026_meta():
    message = init("2025-11-25")
    message["params"]["_meta"] = envelope()
    (reply,) = run(message)
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    assert "resultType" not in reply["result"]


def test_both_eras_in_one_process_keep_their_own_version(fake_pypi):
    # A 2024-11-05 handshake has no structuredContent; a 2026 request alongside it gets its own revision's result,
    # and the handshake's calls are unchanged by it.
    (_, legacy, new, legacy_again) = run(
        init("2024-11-05"),
        call("check_package", 1, name="purewheel", python="3.13"),
        modern_call("check_package", 2, name="purewheel", python="3.13"),
        call("check_package", 3, name="purewheel", python="3.13"),
    )
    assert [r["id"] for r in (legacy, new, legacy_again)] == [1, 2, 3]
    for reply in (legacy, legacy_again):
        assert set(reply["result"]) == {"content", "isError"}
    assert new["result"]["resultType"] == "complete"
    assert new["result"]["structuredContent"]["ok"] is True


def test_modern_calls_dont_use_the_handshake_roots(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the working directory has no uvproj; the legacy client's root does
    replies = run(
        init_with_roots(),
        INITIALIZED,
        roots_answer(fixtures),
        call("check_lockfile", 1, path="uvproj", python="3.13", targets=["linux-x86_64"]),
        modern_call("check_lockfile", 2, path="uvproj", python="3.13", targets=["linux-x86_64"]),
    )
    legacy, new = replies[-2:]
    assert legacy["result"]["isError"] is False
    assert new["result"]["isError"] is True
    assert "no such file or directory" in new["result"]["content"][0]["text"]


def test_modern_call_sends_progress(fake_pypi, three_pins):
    message = modern_call("check_lockfile", path=three_pins, python="3.13")
    message["params"]["_meta"]["progressToken"] = "tok"
    replies = run(message)
    progress = [r["params"] for r in replies if is_progress(r)]
    assert [(p["progressToken"], p["progress"], p["total"]) for p in progress] == [("tok", n, 3) for n in range(4)]
    assert all("message" in p for p in progress)
    assert replies[-1]["id"] == 1 and replies[-1]["result"]["resultType"] == "complete"


def test_modern_call_cancelled_sends_no_result(slow_pypi, three_pins):
    out = Recorder()
    message = modern_call("check_lockfile", path=three_pins, python="3.13")
    message["params"]["_meta"]["progressToken"] = "t"
    mcp.serve(
        live(
            message,
            out.wait_for(is_progress),
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            modern("tools/list", 5),  # answered while the call runs
            out.wait_for(lambda m: m.get("id") == 5),
            slow_pypi.set,
            modern_call("list_targets", 2),
        ),
        out,
    )
    assert [m["id"] for m in out.messages if "id" in m] == [5, 2]
    assert all(m["result"]["resultType"] == "complete" for m in out.messages if "id" in m)
    assert [m["params"]["progress"] for m in out.messages if is_progress(m)] == [0]


def roots_meta(*, roots: bool = True) -> dict:
    return envelope(**{"io.modelcontextprotocol/clientCapabilities": {"roots": {}} if roots else {}})


def roots_call(msg_id: int, roots: object = None, **arguments: object) -> dict:
    message = modern_call("check_lockfile", msg_id, **arguments)
    message["params"]["_meta"] = roots_meta()
    if roots is not None:
        message["params"]["inputResponses"] = {"roots": roots}
    return message


def two_projects(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """projA (the client's root) and projB (the server's working directory), each with a requirements.txt."""
    root, cwd = tmp_path / "projA", tmp_path / "projB"
    for folder, pin in [(root, "winonly==311"), (cwd, "purewheel==3.1")]:
        folder.mkdir()
        (folder / "requirements.txt").write_text(pin + "\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    return root, cwd


def test_modern_relative_path_asks_the_client_for_its_roots(fake_pypi, tmp_path, monkeypatch):
    # A client with roots may mean a file in them: checking the working directory's file instead would be a wrong
    # answer, so the server asks for roots/list (2026 servers can't send requests) and the client retries with them.
    root, _ = two_projects(tmp_path, monkeypatch)
    arguments = {"path": "requirements.txt", "python": "3.13", "targets": ["linux-x86_64"]}
    (asked, answered) = run(
        roots_call(1, **arguments),
        roots_call(2, {"roots": [{"uri": "https://example.com/"}, {"uri": root.as_uri(), "name": "A"}]}, **arguments),
    )
    assert asked == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "input_required",
            "inputRequests": {"roots": {"method": "roots/list"}},
            "_meta": SERVER_META,
        },
    }
    data = answered["result"]["structuredContent"]
    assert answered["result"]["resultType"] == "complete"
    assert data["path"] == str((root / "requirements.txt").resolve())
    assert [g["name"] for g in data["gaps"]] == ["winonly"]
    assert fake_pypi == ["https://pypi.org/pypi/winonly/311/json"]  # nothing looked up for the first try


def test_modern_roots_answer_without_the_file_is_a_tool_error(fake_pypi, tmp_path, monkeypatch):
    # The client named its projects; the working directory's requirements.txt is another project's, so it must not
    # be checked in their place.
    two_projects(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    roots = {"roots": [{"uri": other.as_uri()}, {"uri": "https://example.com/"}]}
    (reply,) = run(roots_call(1, roots, path="requirements.txt", python="3.13"))
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert text == f"requirements.txt: not in any of the client's roots ({other}); pass an absolute path"
    assert not fake_pypi


def test_modern_roots_with_no_local_folder_is_a_tool_error(fake_pypi, tmp_path, monkeypatch):
    two_projects(tmp_path, monkeypatch)
    (reply,) = run(roots_call(1, {"roots": [{"uri": "https://example.com/"}]}, path="requirements.txt"))
    assert reply["result"]["isError"] is True
    assert "none of them is a local folder" in reply["result"]["content"][0]["text"]


def test_modern_empty_roots_answer_means_the_working_directory(fake_pypi, tmp_path, monkeypatch):
    _, cwd = two_projects(tmp_path, monkeypatch)
    (reply,) = run(roots_call(1, {"roots": []}, path="requirements.txt", python="3.13"))
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["path"] == str((cwd / "requirements.txt").resolve())


@pytest.mark.parametrize(
    ("roots", "absolute"),
    [(False, False), (True, True)],
    ids=["no roots capability", "absolute path"],
)
def test_modern_call_without_roots_or_relative_paths_needs_no_roots(fake_pypi, tmp_path, monkeypatch, roots, absolute):
    _, cwd = two_projects(tmp_path, monkeypatch)
    path = str(cwd / "requirements.txt") if absolute else "requirements.txt"
    message = modern_call("check_lockfile", path=path, python="3.13")
    message["params"]["_meta"] = roots_meta(roots=roots)
    (reply,) = run(message)
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["structuredContent"]["path"] == str((cwd / "requirements.txt").resolve())


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("check_dockerfile", {"dockerfile": "/abs/Dockerfile", "path": "proj"}),
        ("check_dockerfile", {"dockerfile": "Dockerfile"}),
        ("diff_lockfiles", {"old": "/abs/uv.lock", "new": "uv.lock"}),
    ],
)
def test_every_path_argument_asks_for_roots(tool, arguments):
    message = modern_call(tool, **arguments)
    message["params"]["_meta"] = roots_meta()
    (reply,) = run(message)
    assert reply["result"]["resultType"] == "input_required"


def test_a_home_path_for_no_such_user_is_a_tool_error_not_a_roots_request(fake_pypi):
    message = roots_call(1, path="~no-such-user-wheelgap/uv.lock")
    (reply,) = run(message)
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["isError"] is True


@pytest.mark.parametrize("responses", [[], {"roots": None}, {"roots": {"roots": "nope"}}, {"roots": []}])
def test_a_malformed_roots_answer_is_invalid_params(fake_pypi, responses):
    message = roots_call(1, path="requirements.txt")
    message["params"]["inputResponses"] = responses
    (reply,) = run(message)
    if responses == {"roots": None}:  # no answer: ask again
        assert reply["result"]["resultType"] == "input_required"
    else:
        assert reply["error"]["code"] == mcp.INVALID_PARAMS
    assert not fake_pypi


def test_progress_message_follows_each_calls_version(fake_pypi):
    # 2024-11-05 progress has no message; a 2026 call in the same process gets one.
    new = modern_call("check_package", 2, name="purewheel", python="3.13")
    new["params"]["_meta"]["progressToken"] = "new"
    replies = run(init("2024-11-05"), with_token(call("check_package", 1, name="purewheel", python="3.13"), "old"), new)
    progress = [r["params"] for r in replies if is_progress(r)]
    assert {p["progressToken"]: "message" in p for p in progress} == {"old": False, "new": True}


def test_modern_call_inline_in_a_batch_and_without_send(fake_pypi):
    (batch,) = run([modern_call("list_targets", 1), ping(2)])
    assert batch[0]["result"]["resultType"] == "complete"
    assert batch[0]["result"]["_meta"] == SERVER_META
    reply = mcp.Server().handle(modern_call("check_package", 3, name="purewheel", python="3.13"))
    assert isinstance(reply, dict)
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["structuredContent"]["ok"] is True


def roots_answer_for(*paths: Path) -> dict:
    return {"roots": [{"uri": p.as_uri()} for p in paths]}


def with_roots(message: dict, roots: object = None) -> dict:
    """A 2026 call from a client with the roots capability (answering with roots, if given)."""
    message["params"]["_meta"] = roots_meta()
    if roots is not None:
        message["params"]["inputResponses"] = {"roots": roots}
    return message


@pytest.mark.parametrize("uri", ["file://", "file:", "file:.", "file://localhost", "file:../projB"])
def test_a_root_with_no_absolute_path_is_left_out(fake_pypi, tmp_path, monkeypatch, uri):
    # Each would be the server's working directory (or near it), and so another project's file.
    assert mcp._root_paths([{"uri": uri}]) == ()
    two_projects(tmp_path, monkeypatch)
    (reply,) = run(roots_call(1, {"roots": [{"uri": uri}]}, path="requirements.txt", python="3.13"))
    assert reply["result"]["isError"] is True
    assert "none of them is a local folder" in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_a_relative_path_cant_leave_its_root(fake_pypi, tmp_path, monkeypatch):
    root, cwd = two_projects(tmp_path, monkeypatch)
    (root / "sub").mkdir()
    (escape, inside, absolute) = run(
        roots_call(1, roots_answer_for(root), path="../projB/requirements.txt", python="3.13"),
        roots_call(2, roots_answer_for(root), path="sub/../requirements.txt", python="3.13"),
        roots_call(3, roots_answer_for(root), path=str(cwd / "requirements.txt"), python="3.13"),
    )
    assert escape["result"]["isError"] is True
    assert "not in any of the client's roots" in escape["result"]["content"][0]["text"]
    assert inside["result"]["structuredContent"]["path"] == str((root / "requirements.txt").resolve())
    assert absolute["result"]["structuredContent"]["path"] == str((cwd / "requirements.txt").resolve())


def test_windows_paths_with_a_drive_or_a_root_alone_are_anchored():
    from pathlib import PurePosixPath, PureWindowsPath

    assert mcp._anchored(PureWindowsPath("/foo/uv.lock"))
    assert mcp._anchored(PureWindowsPath("C:uv.lock"))
    assert not mcp._anchored(PureWindowsPath("C:/foo/uv.lock"))
    assert not mcp._anchored(PureWindowsPath("foo/uv.lock"))
    assert not mcp._anchored(PurePosixPath("/foo/uv.lock"))
    assert not mcp._anchored(PurePosixPath("foo/uv.lock"))


@pytest.mark.skipif(os.name != "nt", reason="drive- and root-relative paths exist on Windows only")
@pytest.mark.parametrize("path", ["\\requirements.txt", "C:requirements.txt"])
def test_anchored_windows_paths_are_refused_on_a_roots_call(fake_pypi, tmp_path, monkeypatch, path):
    root, _ = two_projects(tmp_path, monkeypatch)
    (reply,) = run(roots_call(1, roots_answer_for(root), path=path))
    assert reply["result"]["isError"] is True
    assert "neither relative nor absolute" in reply["result"]["content"][0]["text"]


def test_a_relative_pyodide_lock_is_found_in_the_roots(fake_pypi, fixtures, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no pyodide-lock.json here
    arguments = {"name": "fastlib", "python": "3.13", "targets": ["pyodide"], "pyodide_lock": "pyodide-lock.json"}
    (asked, answered, elsewhere) = run(
        with_roots(modern_call("check_package", 1, **arguments)),
        with_roots(modern_call("check_package", 2, **arguments), roots_answer_for(fixtures)),
        with_roots(modern_call("check_package", 3, **arguments), roots_answer_for(tmp_path)),
    )
    assert asked["result"]["resultType"] == "input_required"
    assert answered["result"]["structuredContent"]["results"] == {"3.13 pyodide": "bundled"}
    assert elsewhere["result"]["isError"] is True
    assert "not in any of the client's roots" in elsewhere["result"]["content"][0]["text"]


def test_the_first_root_with_the_file_wins(fake_pypi, tmp_path, monkeypatch):
    root, cwd = two_projects(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    (reply,) = run(roots_call(1, roots_answer_for(empty, cwd, root), path="requirements.txt", python="3.13"))
    assert reply["result"]["structuredContent"]["path"] == str((cwd / "requirements.txt").resolve())


def test_diff_lockfiles_finds_old_and_new_in_the_roots(fake_pypi, tmp_path, monkeypatch):
    lockfile_pair(tmp_path, uv_package("purewheel", "3.1"), uv_package("purewheel", "3.1", "sdist"))
    monkeypatch.chdir(tmp_path / "old")  # the working directory has old/uv.lock only as ./uv.lock
    message = modern_call("diff_lockfiles", old="old/uv.lock", new="new/uv.lock", python="3.13")
    (asked, answered) = run(
        with_roots(message), with_roots(json.loads(json.dumps(message)), roots_answer_for(tmp_path))
    )
    assert asked["result"]["resultType"] == "input_required"
    data = answered["result"]["structuredContent"]
    assert data["paths"] == {k: str((tmp_path / k / "uv.lock").resolve()) for k in ("old", "new")}
    assert [p["name"] for p in data["worse"]] == ["purewheel"]


def test_a_roots_call_sends_progress(fake_pypi, tmp_path, monkeypatch):
    root, _ = two_projects(tmp_path, monkeypatch)
    message = roots_call(1, roots_answer_for(root), path="requirements.txt", python="3.13")
    message["params"]["_meta"]["progressToken"] = "r"
    replies = run(message)
    assert [r["params"]["progressToken"] for r in replies if is_progress(r)] == ["r", "r"]
    assert replies[-1]["result"]["structuredContent"]["path"] == str((root / "requirements.txt").resolve())


def test_a_roots_call_inline_in_a_batch_and_without_send(fake_pypi, tmp_path, monkeypatch):
    root, _ = two_projects(tmp_path, monkeypatch)
    expected = str((root / "requirements.txt").resolve())
    (batch,) = run([roots_call(1, roots_answer_for(root), path="requirements.txt", python="3.13")])
    assert batch[0]["result"]["structuredContent"]["path"] == expected
    reply = mcp.Server().handle(roots_call(2, roots_answer_for(root), path="requirements.txt", python="3.13"))
    assert isinstance(reply, dict)
    assert reply["result"]["structuredContent"]["path"] == expected


@pytest.mark.parametrize(
    "key",
    [
        "io.modelcontextprotocol/protocolVersion",
        "io.modelcontextprotocol/clientCapabilities",
        "io.modelcontextprotocol/clientInfo",
        "io.modelcontextprotocol/logLevel",
    ],
)
def test_each_envelope_key_alone_marks_a_2026_request(key):
    value = {"io.modelcontextprotocol/clientInfo": {"name": "x", "version": "1"}}.get(key, {})
    if key.endswith(("protocolVersion", "logLevel")):
        value = MODERN if key.endswith("protocolVersion") else "info"
    (reply,) = run({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {key: value}}})
    assert reply["error"]["code"] == mcp.INVALID_PARAMS


def test_a_2025_related_task_key_stays_handshake():
    meta = {"io.modelcontextprotocol/related-task": {"taskId": "t"}}
    (reply,) = run({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}})
    assert reply["result"] == {"tools": mcp.TOOLS}


def test_the_roots_error_clips_long_paths_and_roots(fake_pypi, tmp_path, monkeypatch):
    two_projects(tmp_path, monkeypatch)
    deep = tmp_path / ("d" * 150) / ("e" * 150)
    deep.mkdir(parents=True)
    (reply,) = run(roots_call(1, roots_answer_for(deep, deep), path="x" * 1000))
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True and len(text) < 500 and "..." in text
    assert "not in any of the client's roots" in text  # not an OSError for the over-long name


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "uv.lock", "platforms": ["win-arm64"]}, "unknown argument(s): platforms"),
        ({"path": "uv.lock", "python": 3.13}, "'python' must be a string"),
        ({"path": "uv.lock", "offline": "yes"}, "'offline' must be true or false"),
        ({"path": "uv.lock", "targets": "linux-x86_64"}, "'targets' must be an array of strings"),
    ],
)
def test_bad_arguments_fail_before_asking_for_roots(fake_pypi, arguments, message):
    (reply,) = run(with_roots(modern_call("check_lockfile", **arguments)))
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]


@pytest.mark.parametrize(("declared", "asks"), [(True, True), ([], True), ("yes", True), (False, False), (None, False)])
def test_a_malformed_roots_capability_still_counts(declared, asks, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    message = modern_call("check_lockfile", path="uv.lock")
    message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] = {"roots": declared}
    (reply,) = run(message)
    assert (reply["result"]["resultType"] == "input_required") == asks


@pytest.mark.usefixtures("index")
def test_check_package_dependencies_counts_every_release_of_the_package_asked_about(monkeypatch):
    # split resolves to 1.0 on 3.14 (a Windows ARM wheel) and to 2.0 on 3.15 (Linux only: a source build there).
    import test_deps

    monkeypatch.setitem(
        test_deps.INDEX,
        "split",
        {
            "1.0": test_deps.release(files=(test_deps.WIN_ARM,)),
            "2.0": test_deps.release(files=(test_deps.LINUX,), requires_python=">=3.15"),
        },
    )
    (_, reply) = run(
        init(), call("check_package", name="split", python="3.14,3.15", targets=["win-arm64"], dependencies=True)
    )
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert data["gaps"] == ["3.15 win-arm64"]
    assert data["results"] == {"3.14 win-arm64": "wheel", "3.15 win-arm64": "source"}
    assert data["versions"] == ["1.0", "2.0"]
    assert data["dependencies_checked"] == 0


def test_check_lockfile_dependencies_skips_build_info_when_configured(fake_pypi, fake_files, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["nowheels==0.9"]\n\n'
        "[tool.wheelgap]\nbuild-info = true\n",
        encoding="utf-8",
    )
    (_, reply) = run(
        init(),
        call(
            "check_lockfile",
            path=str(tmp_path / "pyproject.toml"),
            python="3.13",
            targets=["linux-x86_64"],
            dependencies=True,
        ),
    )
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert fake_files.calls == []


@pytest.mark.parametrize(
    ("selection", "packages", "gaps"),
    [
        ({}, 10, {"gpu", "devtool", "docstool"}),
        ({"no_dev": True}, 4, set()),
        ({"extras": ["api"], "no_dev": True}, 7, set()),
        ({"all_extras": True}, 9, {"gpu", "devtool"}),
        ({"groups": ["docs"]}, 6, {"devtool", "docstool"}),
        ({"only_groups": ["docs"]}, 1, {"docstool"}),
    ],
)
def test_check_lockfile_selects_uv_extras_and_groups_offline(fixtures, tmp_path, fake_pypi, selection, packages, gaps):
    # Reuse the selection graph, with an sdist-only dev tool to demonstrate the production-only case.
    text = (fixtures / "uvselect" / "uv.lock").read_text(encoding="utf-8")
    text = text.replace(
        'wheels = [{ url = "https://example.invalid/devtool-1.0-py3-none-any.whl" }]',
        'sdist = { url = "https://example.invalid/devtool-1.0.tar.gz" }',
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(text, encoding="utf-8")
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(lock), python="3.13", targets=["linux-x86_64"], offline=True, **selection),
    )
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    assert data["packages"] == packages
    assert {p["name"] for p in data["gaps"]} == gaps
    assert data["ok"] is (not gaps)
    assert fake_pypi == []


def test_check_lockfile_selects_a_pyproject_group(tmp_path, fake_pypi):
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "app"\ndependencies = ["purewheel==3.1"]\n[dependency-groups]\ntest = ["winonly==311"]\n',
        encoding="utf-8",
    )
    (_, reply) = run(
        init(), call("check_lockfile", path=str(project), python="3.13", targets=["linux-x86_64"], only_groups=["test"])
    )
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    assert data["packages"] == 1
    assert [p["name"] for p in data["gaps"]] == ["winonly"]
    assert fake_pypi == ["https://pypi.org/pypi/winonly/311/json"]


@pytest.mark.parametrize("selection", [{"groups": ["absent"]}, {"no_dev": True}])
def test_check_lockfile_selection_preserves_cli_refusals(tmp_path, fixtures, selection):
    path = fixtures / "uvselect" if "groups" in selection else tmp_path / "requirements.txt"
    if path.suffix == ".txt":
        path.write_text("purewheel==3.1\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(path), **selection))
    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    expected = "unknown groups: absent" if "groups" in selection else "extra/group selection requires"
    assert expected in text
    assert "unknown argument" not in text


@pytest.mark.parametrize("key", ["extras", "groups", "only_groups", "all_extras", "no_dev"])
@pytest.mark.parametrize("value", ["test", [1]])
def test_check_lockfile_selection_types_are_checked_before_requesting_roots(key, value):
    message = with_roots(modern_call("check_lockfile", path="uv.lock", **{key: value}))
    (reply,) = run(message)
    assert reply["result"]["resultType"] == "complete"
    assert reply["result"]["isError"] is True
    expected = f"'{key}' must be " + ("true or false" if key in {"all_extras", "no_dev"} else "an array of strings")
    assert expected in reply["result"]["content"][0]["text"]
