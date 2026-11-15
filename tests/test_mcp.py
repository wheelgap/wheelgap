import io
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import FAKE_INDEX

from wheelgap import __version__, mcp


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
    assert set(tools) == {"check_package", "check_lockfile", "check_dockerfile", "diff_lockfiles", "list_targets"}
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
    assert data["assumes"] == {"glibc": "2.36", "musl": "1.2", "macos": "14.0"}
    assert fake_pypi == ["https://pypi.org/pypi/winonly/json"]


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
    assert set(data) == {"name", "version", "error", "results", "ok", "gaps", "targets", "assumes"}


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


def test_check_lockfile_lists_skipped_packages(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert {s["name"]: s["reason"] for s in data["skipped"]} == {"demo": "editable", "gitdep": "git"}


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
    assert data["image"] == {"python": "3.13", "platform": "musl-x86_64", "glibc": None, "musl": "1.2"}
    assert data["targets"] == ["3.13 musl-x86_64"]
    assert data["path"] == str(tmp_path.resolve())  # default: the Dockerfile's folder
    assert {g["name"] for g in data["gaps"]} >= {"nativeext", "puresdist"}
    assert {s["name"] for s in data["skipped"]} == {"demo", "gitdep"}


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


def uv_package(name: str, version: str, kind: str = "wheel") -> str:
    """A uv.lock [[package]] entry that records one wheel or one sdist, so no lookup is needed."""
    url = f"https://files.pythonhosted.org/packages/aa/{name}-{version}"
    if kind == "wheel":
        files = f'wheels = [\n    {{ url = "{url}-py3-none-any.whl", hash = "sha256:00", size = 1 }},\n]\n'
    else:
        files = f'sdist = {{ url = "{url}.tar.gz", hash = "sha256:00", size = 1 }}\n'
    return (
        f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        + files
        + "\n"
    )


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
        ({"glibc": "2.28"}, "unknown argument(s): glibc"),
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


def test_mcp_docs_name_every_tool():
    """docs/mcp.md lists the server's tools; a new tool should be described there too."""
    text = (Path(__file__).resolve().parent.parent / "docs" / "mcp.md").read_text(encoding="utf-8")
    for tool in mcp.TOOLS:
        assert f"`{tool['name']}`" in text


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
