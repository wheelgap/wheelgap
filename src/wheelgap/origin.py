"""Whether a package is positively PyPI's, so its name may go to a service other than the index (--conda-forge).

A name from a private index must never leave the run, so the answer is yes only when wheelgap knows where the package
came from, and no whenever it's unsure.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterable, Mapping

from . import pypi
from .sources import Package, Source

# Settings that point pip or uv at another index, or at a config file that may: with any of them set, a name found
# on pypi.org may still be a private package's. pip lower-cases what follows PIP_, so PIP_index_url counts too; every
# UV_INDEX_* variable counts (UV_INDEX_<NAME>_USERNAME names an index configured somewhere).
INDEX_ENV = (
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_NO_INDEX",
    "PIP_CONFIG_FILE",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_NO_INDEX",
    "UV_CONFIG_FILE",
)
_PIP_INDEX_ENV = {name.removeprefix("PIP_") for name in INDEX_ENV if name.startswith("PIP_")}


def index_env(env: Mapping[str, str] = os.environ) -> str | None:
    """The first set (non-empty) environment variable that may point pip or uv at another index, or None."""
    for name, value in env.items():
        if not value:
            continue
        if name in INDEX_ENV or name.startswith("UV_INDEX_"):
            return name
        if name.startswith("PIP_") and name[4:].upper() in _PIP_INDEX_ENV:
            return name
    return None


def other_index(simple_index: str | None = None, env: Mapping[str, str] = os.environ) -> str | None:
    """What makes this run's packages come from another index, which may hold private names alongside PyPI's:
    "--index-url" (`simple_index`, from --index-url or index-url in [tool.wheelgap]) or "WHEELGAP_INDEX_URL" (set
    to a mirror). None when lookups go to pypi.org."""
    if simple_index is not None:
        return "--index-url"
    if (env.get("WHEELGAP_INDEX_URL") or pypi.DEFAULT_INDEX).rstrip("/") != pypi.DEFAULT_INDEX:
        return "WHEELGAP_INDEX_URL"
    return None


def private_names(
    sources: Iterable[Source], env: Mapping[str, str] = os.environ, simple_index: str | None = None
) -> frozenset[str]:
    """The canonical names that some input of the run doesn't place on PyPI: known_pypi is false for the package
    there (a private index, git, a path, a lock that records no index, ...), or the input read the package and a
    group or extras selection left it out (Source.unselected_keys, which keeps no index). The same name in another
    input may be that same private package, so pass these as known_pypi's `private` to keep the name in for every
    input. Call it after the run's lookups, which set Package.found_on_pypi."""
    names: set[str] = set()
    for s in sources:
        names |= s.unselected_keys
        for original in s.packages:
            # A range resolved per Python is judged by its resolutions: the lookups mark those, not the range itself.
            # One that pypi.org answered places the name there, even if another Python found no matching release.
            resolved = original.resolutions.values() or (original,)
            if not any(known_pypi(p, s, env, simple_index) for p in resolved):
                names.add(original.key)
    return frozenset(names)


def known_pypi(
    pkg: Package,
    source: Source,
    env: Mapping[str, str] = os.environ,
    simple_index: str | None = None,
    private: Collection[str] = frozenset(),
) -> bool:
    """Whether `pkg` (read from `source`) is positively a PyPI package. All of:

    - no input of the run places its name anywhere else (`private`, from private_names);

    - the run looks packages up on pypi.org: no --index-url (`simple_index`) and no WHEELGAP_INDEX_URL mirror;
    - the package comes from an index (not git, a path, a URL, or a skipped private index);
    - either its lock says it came from PyPI (uv.lock's registry, pylock.toml's index, a poetry.lock legacy source
      or a pixi.lock index is exactly https://pypi.org/simple), or this run found it on pypi.org
      (Package.found_on_pypi) and neither its input (Source.pypi_only) nor the environment (index_env) names another
      index.

    So never a pdm.lock entry, a poetry.lock or pixi.lock entry that records no index, a pylock.toml entry with no
    index, a requirements file with an --index-url line, or anything else wheelgap can't place."""
    if other_index(simple_index, env) is not None or pkg.skip or pkg.key in private:
        return False
    if isinstance(pkg.registry, str) and pkg.registry.rstrip("/") == pypi.PYPI_SIMPLE:
        return True
    return pkg.found_on_pypi and source.pypi_only and index_env(env) is None
