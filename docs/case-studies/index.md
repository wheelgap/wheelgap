# Case studies

These pages check real open-source projects on the platforms their build configuration names; a study that also looks
beyond those platforms says so. Each study pins the project's revision and wheelgap version, records the target
assumptions and commands, and explains the gaps and possible next steps. Results describe the package files available
when the check ran; new uploads and lockfile updates can change them.

## Summary

The twenty-three studies below were scanned between 15 and 28 November 2026. Each result is for the pinned commit its
page names and for wheelgap 0.13.0, except the mitmproxy study, which used wheelgap 0.10.0, and the dlt, Home Assistant
Core, LangGraph, LangChain Core and OnlineJudge studies, which used wheelgap 0.14.0. A project, or a package it
depends on, may have changed since.

| Project | What was checked | Result | Gaps and fixes |
| --- | --- | --- | --- |
| [mitmproxy](mitmproxy.md) | Python 3.15; Linux x86_64 and ARM64, macOS Intel and Apple silicon (macOS 15.0), Windows; runtime dependencies (43 packages) | 2 source builds | MarkupSafe 3.0.3 has no CPython 3.15 wheel on any target; 3.0.4 does. cryptography 50.0.2 has no macOS Intel wheel, and no newer release has one |
| [FastAPI](fastapi.md) | Python 3.15; Linux, Windows, macOS; the CI test selection (115 packages) | 1 source build | PyYAML 6.0.3 has no CPython 3.15 wheel; 6.0.4, uploaded on 20 November 2026 after the scan, does |
| [Buzz](buzz.md) | Python 3.13; Linux (glibc 2.39), Windows, macOS (macOS 11.0); runtime dependencies (256 packages) | 22 packages with gaps: 19 source builds, 3 missing | 11 pins have no CPython 3.13 wheel on any target; intervaltree 3.2.1, julius 0.2.8 and antlr4-python3-runtime 4.11.0 do, though Hydra 1.3.2 and omegaconf 2.3.0 require `antlr4-python3-runtime==4.9.*`. 11 more have macOS wheels that need macOS 12.0 to 14.0, 3 of them with no sdist; with `--macos 15.0` only the first 11 remain |
| [Aider](aider.md) | Python 3.14; Linux, Windows; `requirements.txt` (107 packages) | all wheels | none |
| [pydantic](pydantic.md) | Python 3.14; Linux, macOS, Windows; the selection closest to the CI test install (56 packages) | all wheels | none in that selection. The whole lockfile (156 packages) adds mkdocs-exclude 1.0.2, which is sdist only, and watchdog 6.0.0, which has no CPython 3.14 macOS wheel; 7.0.0 has one |
| [HERD](herd.md) | Python 3.12; Linux x86_64, the platform it builds for (134 packages), then ARM64 Linux, Alpine and Windows on ARM beyond it (136); the whole lock | all wheels on Linux x86_64 and ARM64; 4 source builds on Alpine and Windows on ARM | cryptography 50.0.2 has no Windows on ARM wheel; 46.0.3 was the last release with one. In the `dev` group, gevent 25.9.1 and zope-interface 8.3 have none either (26.5.0 and 8.4 do), and zope-interface and playwright 1.65.0 have no musllinux wheel |
| [Textual](textual.md) | Python 3.14; Linux, Windows, macOS; runtime dependencies, the `syntax` extra and the `dev` group, as CI installs (105 packages); the same without `dev` (25) | 2 source builds, both from `dev`; all wheels without it | mkdocs-exclude 1.0.2 is sdist only. watchdog 6.0.0 has no CPython 3.14 macOS wheel; 7.0.0 and 7.1.0 do |
| [Poetry](poetry.md) | Python 3.15; Linux, Windows, macOS; the `main`, `test` and `typing` groups (67 packages); every group, as CI installs (72), has the same gaps | 2 source builds | PyYAML 6.0.3 has no CPython 3.15 wheel; 6.0.4, uploaded on 20 November 2026 after the scan, does. xattr 1.3.0, a macOS-only dependency, has none; 1.3.1 does |
| [Fava](fava.md) | Python 3.15; Linux, Windows, macOS; runtime dependencies (32 packages); with the CI `test` group (50) the gap is the same | 1 source build | Beancount 3.2.3 has wheels up to CPython 3.14 and is the newest release |
| [Typer](typer.md) | Python 3.15 and 3.15t; Linux, Windows, macOS; the CI test selection (29 packages) | all wheels | none. coverage 7.13.1 installs as its pure-Python wheel; 7.17.3 has CPython 3.15 and 3.15t wheels |
| [PDM](pdm.md) | Python 3.15; Linux, Windows, macOS; the `default` and `test` groups CI installs (50 packages) | 1 source build | MarkupSafe 3.0.3 has no CPython 3.15 wheel; 3.0.4 does. The whole lockfile (91 packages) adds cffi 2.0.0 (2.1.0 has wheels), PyYAML 6.0.3 and watchdog 6.0.0 (7.0.0 has a macOS wheel) |
| [Streamlit](streamlit.md) | Linux only; core dependencies on Python 3.12 (33 rows), the integration job on 3.12 (153) and the unit-test install on 3.11 to 3.15 (140) | all wheels, except 2 source builds in the unit-test install on 3.15 | orjson 3.13.0's CPython 3.15 wheel is tagged `manylinux_2_39`, newer than the glibc 2.36 floor; with `--glibc 2.39` it is a wheel. PyYAML 6.0.3 has no CPython 3.15 wheel |
| [Litestar](litestar.md) | Python 3.14; Linux, macOS, Windows; what CI's `uv sync` installs (214 rows) | 3 source builds | autodocsumm 0.2.6 and sphinx-paramlinks 0.6.0, both in the `docs` group, are sdist only; autodocsumm 0.2.8 has a wheel. autobahn 26.11.1's macOS arm64 wheels are tagged `macosx_15_0`, newer than the 14.0 floor; with `--macos 15.0` it is a wheel |
| [FastMCP](fastmcp.md) | Python 3.15; Linux; the whole lock, which is also what CI installs (151 rows) | 5 source builds | jiter 0.16.0, msgspec 0.21.1, pyinstrument 5.1.2 and PyYAML 6.0.3 have wheels up to CPython 3.14 only; jiter 0.17.0 and msgspec 0.22.0 have 3.15 wheels. pytest-report 0.2.1 is sdist only |
| [Pydantic AI](pydantic-ai.md) | Python 3.15; Linux; the whole lock with `--online` (370 rows) | 6 source builds and 2 missing in the report; 5 source builds in a uv install | forbiddenfruit 0.1.4 is sdist only; orjson 3.12.0's CPython 3.15 wheel is tagged `manylinux_2_39`; pandas 2.3.3, pymongo 4.18.3 and PyYAML 6.0.3 have wheels up to CPython 3.14 only, and pandas 3.0.6 and pymongo 4.19.0 have 3.15 wheels. The report's other three rows are not gaps for a uv install on 3.15: uv installs agent-client-protocol 0.11.1, and the lock doesn't select numpy 2.2.6 or onnxruntime 1.30.0 there |
| [MCP Python SDK](mcp-python-sdk.md) | Python 3.11 to 3.15; Linux, Windows; the whole lock (120 packages) | 5 source builds: 1 on every Python, 3 on 3.14 and 5 on 3.15 | cffi 2.0.0, the only gap in the runtime dependencies, has no CPython 3.15 wheel; 2.1.0 does. forbiddenfruit 0.1.4 (`dev` group) is sdist only; 0.1.5, uploaded on 25 November 2026 after the scan, has a `py3-none-any` wheel. MarkupSafe 3.0.2 and PyYAML 6.0.2 (`docs` group) have wheels up to CPython 3.13 only; MarkupSafe 3.0.4 has 3.14 and 3.15 wheels; PyYAML 6.0.3 has a 3.14 wheel but no 3.15 one, and 6.0.4, uploaded on 20 November 2026 after the scan, has both. jiter 0.16.0 (the opt-in `translate` group, which the CI test job doesn't install) has wheels up to CPython 3.14 only; 0.17.0 has 3.15 wheels |
| [Prefect](prefect.md) | Python 3.15; Linux x86_64 and ARM64, Windows; runtime dependencies (105 packages); the CI test install, with the `dev` group, on Linux x86_64 and Windows (213) | 2 source builds; 5 with the `dev` group | orjson 3.13.0's CPython 3.15 Linux wheels are tagged `manylinux_2_39`, newer than the glibc 2.36 floor; with `--glibc 2.39` it is a wheel. PyYAML 6.0.3 has no CPython 3.15 wheel; 6.0.4, uploaded on 20 November 2026, does. The `dev` group adds testslide 2.7.1, which is sdist only, pyre-check 0.9.23, which has no Windows wheel, and watchfiles 1.2.0, which has no CPython 3.15 Windows wheel; 1.3.0 has one. On Python 3.14 the runtime dependencies are all wheels |
| [urllib3](urllib3.md) | Python 3.15 and 3.14t; Linux (glibc 2.39), Windows, macOS (macOS 26.0); the CI test install (64 packages on 3.15, 63 on 3.14t); the same on 3.14t with the `brotli` extra that CI leaves out there (64) | all wheels in the CI test install; 1 source build with the `brotli` extra on 3.14t | Brotli 1.2.1 has no free-threaded wheel on any target and is the newest release. On regular CPython 3.15 it is a wheel |
| [dlt](dlt.md) | Python 3.14; Linux, Windows, macOS; runtime dependencies (39 packages) | 1 source build and 1 missing | PyYAML 6.0.2's wheels stop at CPython 3.13, so it builds everywhere on 3.14 (6.0.3 has them). pywin32 310 has neither a CPython 3.14 Windows wheel nor a buildable sdist; 311 has the wheel |
| [Home Assistant Core](home-assistant.md) | Python 3.14; Alpine on x86_64 and ARM64 (`musl-x86_64`, `musl-aarch64`); the container's `requirements.txt` (61 packages) and `requirements_all.txt` (1,255) | 4 source builds in the core file; 116 rows (112 distinct pins) in the full set | gazetteer-matcher 1.1.0, pymicro-vad 1.0.1, pyspeex-noise 1.0.2 and PyTurboJPEG 1.8.3 have no musllinux wheel on PyPI; PyTurboJPEG 2.5.0 does. Home Assistant's own wheel index lists a wheel for every one of the 112 pins, so its container build, which installs with `--no-build`, is not what these rows describe |
| [LangGraph](langgraph.md) | Python 3.10 to 3.14; Linux; `libs/langgraph` with the `test` group (103 packages) | 1 source build | forbiddenfruit 0.1.4 has no wheel; it's reached only on Python 3.11 to 3.13, through langgraph-cli and blockbuster. 0.1.5 ships a `py3-none-any` wheel |
| [LangChain Core](langchain.md) | Python 3.11 to 3.14; Linux; `libs/core` with the project dependencies, the `dev` group and the `test` group (146 packages) | 1 source build | forbiddenfruit 0.1.4 has no wheel; 0.1.5 ships a `py3-none-any` wheel |
| [OnlineJudge](onlinejudge.md) | Python 3.12; Alpine x86_64 and ARM64; backend requirements (22 direct pins) | 4 source builds on both targets | coverage 7.2.7 ships the missing wheels. Envelopes, flake8-quotes and psycopg2 have no newer release with wheels for either target |

Unless a row says otherwise, Linux is `linux-x86_64` with a glibc 2.36 floor, Windows is `win-amd64`, and macOS is
`macos-arm64` with a macOS 14.0 floor. A source build means the pinned version has an sdist but no wheel that fits the
target; missing means it has neither. Where a page runs several scans, the row says which selection each count is
for. Each page has the commands to reproduce its scan and explains each gap.

## What keeps coming up

These packages are a gap in more than one study:

- **PyYAML 6.0.3** has no CPython 3.15 wheel. Seven studies: FastAPI, Poetry, Streamlit (the unit-test install on
  3.15), FastMCP, Pydantic AI and Prefect, plus the whole-lockfile scan of PDM, where it is in the `doc` group and
  outside what the CI test job installs. The studies scanned up to 20 November found no newer release with one;
  6.0.4, uploaded that day, has CPython 3.15 wheels, and the Prefect study, scanned on 21 November, reports it. The
  MCP Python SDK pins PyYAML 6.0.2 (its `docs` group), which has wheels up to CPython 3.13 only; 6.0.3 adds the 3.14
  wheel but not 3.15.
- **watchdog 6.0.0** has macOS wheels up to CPython 3.13 only, so it is a source build on `macos-arm64` for 3.14 and
  3.15; 7.0.0 has the wheel. Its Linux and Windows wheels are `py3-none`, so those targets are covered. Three
  studies, each time through mkdocs: Textual (the `dev` group) and the whole-lockfile scans of pydantic and PDM.
- **MarkupSafe 3.0.3** has no CPython 3.15 wheel; 3.0.4 does. Two studies: mitmproxy and PDM. The MCP Python SDK
  pins 3.0.2 (its `docs` group), which has none for 3.14 or 3.15 either; 3.0.4 has both.
- **orjson** publishes a CPython 3.15 `x86_64` wheel tagged `manylinux_2_39`, so it is a source build at the default
  glibc 2.36 floor and a wheel at glibc 2.39. Three studies: Streamlit (orjson 3.13.0), Pydantic AI (3.12.0) and
  Prefect (3.13.0, where it is a runtime dependency and the `aarch64` wheel is tagged the same way).
- **mkdocs-exclude 1.0.2** publishes only an sdist. Two studies: Textual (the `dev` group) and the whole-lockfile
  scan of pydantic.
- **cryptography 50.0.2** has no macOS Intel wheel (mitmproxy) and no Windows on ARM wheel (HERD), and no newer
  release has either. Two studies.
- **cffi 2.0.0** has no CPython 3.15 wheel; 2.1.0 does. Two studies: the MCP Python SDK, where it is a runtime
  dependency through `pyjwt[crypto]` and cryptography, and the whole-lockfile scan of PDM.
- **jiter 0.16.0** has wheels up to CPython 3.14 only; 0.17.0 has 3.15 wheels. Two studies: FastMCP and the MCP
  Python SDK (the opt-in `translate` group).
- **forbiddenfruit 0.1.4** publishes only an sdist; 0.1.5, uploaded on 25 November 2026, has a `py3-none-any` wheel.
  Four studies: Pydantic AI, the MCP Python SDK (the `dev` group, through blockbuster), LangGraph (the `test`
  group, through langgraph-cli and blockbuster) and LangChain Core (the `test` group, through blockbuster).

## The studies

- [mitmproxy: runtime dependencies on five build platforms](mitmproxy.md)
- [FastAPI: CPython 3.15 test dependencies across three platforms](fastapi.md)
- [Buzz: speech-to-text dependencies across three desktop targets](buzz.md)
- [Aider: Python 3.14 wheels on Linux and Windows](aider.md)
- [pydantic: CPython 3.14 test dependencies across three platforms](pydantic.md)
- [HERD: a 136-package service platform on ARM, Alpine and Windows on ARM](herd.md)
- [Textual: syntax-highlighting wheels on Python 3.14](textual.md)
- [Poetry: test groups versus main on Python 3.15](poetry.md)
- [Fava: Beancount’s Python 3.15 wheel gap](fava.md)
- [Typer: CPython 3.15 and 3.15t test dependencies across three platforms](typer.md)
- [PDM: default dependencies versus documentation and optional groups](pdm.md)
- [Streamlit: core dependencies versus integration tests](streamlit.md)
- [Litestar: two documentation-only source builds on Python 3.14](litestar.md)
- [FastMCP: optional and development pins on Python 3.15](fastmcp.md)
- [Pydantic AI: a whole-lock audit with online metadata](pydantic-ai.md)
- [MCP Python SDK: one runtime gap and four in its development groups](mcp-python-sdk.md)
- [Prefect: a glibc-floor gap and PyYAML on Python 3.15](prefect.md)
- [urllib3: the brotli extra on free-threaded 3.14](urllib3.md)
- [dlt: two runtime gaps on Python 3.14, both fixed one release up](dlt.md)
- [Home Assistant Core: four PyPI gaps in the container requirements, covered by the project's own wheels](home-assistant.md)
- [LangGraph: test dependencies on Python 3.10–3.14](langgraph.md)
- [LangChain Core: one test-dependency gap, fixed one release up](langchain.md)
- [OnlineJudge: four source builds on Python 3.12 Alpine](onlinejudge.md)
