# Failed building wheel on Alpine or ARM

You see this in a Docker build:

```text
Building wheel for biopython (pyproject.toml) ... error
ERROR: Failed building wheel for biopython
Failed to build biopython
```

uv says `Failed to build \`biopython==1.88\`` instead. Both mean the same thing:
the package has no prebuilt wheel for the platform in your image, so pip is
trying to compile it. On Alpine (musl) and on ARM this is common — a package
can have a Linux x86-64 glibc wheel and nothing that fits `musl-aarch64`.

## Find the dependency before the build

Run wheelgap against your lockfile and the platforms you ship. The output below
comes from the `uv.lock` of
[ClawBio](https://github.com/ClawBio/ClawBio/blob/b3dec99cedbff0b5848182b61c8456c9c4f4cdca/uv.lock)
at commit `b3dec99c`:

```console
$ wheelgap uv.lock -p 3.13 -t musl-aarch64,linux-aarch64
wheelgap 0.14.0 · uv.lock · 246 packages · Python 3.13

package                         version   musl-aarch64  linux-aarch64
biopython                       1.88      ● source      ✓
genoio (via sushie)             0.2.0     ● source      ● source
google-crc32c (via anndata +4)  1.8.0     ● source      ✓
jaxlib (via sushie)             0.10.2    ✗ missing     ✓
...

musl-aarch64   13 from source, 1 missing
linux-aarch64  2 from source
14 of 246 packages have gaps: 13 will build from source, 1 can't install at all.
```

`● source` means pip will compile it; `✓` is a usable wheel; `✗ missing` means
there is no sdist either and the install cannot succeed. If your `Dockerfile`
holds the install command, let wheelgap read it and the files it copies:

```console
$ wheelgap --dockerfile Dockerfile --dockerfile-inputs -p 3.13 -t musl-aarch64,linux-aarch64
wheelgap: note: Dockerfile:3: RUN pip install -r reads requirements.txt
wheelgap 0.14.0 · requirements.txt (Dockerfile:3) · 2 packages · Python 3.13

✓ All 2 packages have a wheel for every one of 2 targets.
```

## Already have a failed log?

Feed the log to wheelgap and tell it which machine the install ran on:

```console
$ wheelgap --from-log install.log -p 3.13 -t musl-aarch64,linux-aarch64
wheelgap: note: the install log says these failed to build: biopython 1.88
wheelgap 0.14.0 · install log · 1 package · Python 3.13

package    version  musl-aarch64  linux-aarch64
biopython  1.88     ● source      ✓

biopython 1.88 on musl-aarch64: ● source
  biopython-1.88-cp313-cp313-manylinux2014_aarch64...whl   manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
  ... and 32 more (--json lists them all)
  ask for a wheel: https://github.com/biopython/biopython/issues

musl-aarch64  1 from source
1 of 1 package has gaps: 1 will build from source.
```

The `●` explanation tells you *why* the wheel was rejected: a glibc wheel
cannot be used on Alpine. Add `--notes` to include the tracker's notes, or
`--json` for every rejected filename.

## Fix it

Add `--fix`. If a newer release has the wheel, wheelgap prints the exact
command for your input — `uv lock --upgrade-package NAME` for a `uv.lock`, or
the new pin for a requirements file. If no release helps, it says so plainly:

```console
$ wheelgap uv.lock -p 3.13 -t musl-aarch64,linux-aarch64 --fix
--fix: 16 gaps (14 packages × their targets): 0 can be fixed by upgrading to an official release; 0 only by an unofficial gap wheel; 16 have no fix yet.

No fix yet:
  biopython 1.88: no newer release has a wheel for musl-aarch64
  genoio 0.2.0: no newer release has a wheel for musl-aarch64, linux-aarch64
  jaxlib 0.10.2: no newer release has a wheel for musl-aarch64
  ...
  statsmodels 0.15.0: no newer release has a wheel for musl-aarch64
```

When there is no official fix, you can ask upstream for a wheel (the `--fix`
output links the issue tracker) or, as a clearly labelled fallback, use the
[gap wheel index](https://wheelgap.github.io/wheels/) if it has a wheel for
that target. See
[Installing from the gap wheel index](recipes.md#installing-from-the-gap-wheel-index).

## FAQ

**Why does it work on my Mac?** Your Mac is `macos-arm64`; the build is
`musl-aarch64` or `linux-aarch64`. Different platform tags, different wheels.
A wheel that installs on one is not usable on the other.

**Should I switch to a slim Debian image?** Often, yes. A glibc image such as
`python:3.13-slim` can use `manylinux` wheels that Alpine cannot. If those
wheels exist for your packages, switching removes the build entirely. Check
with `-t linux-aarch64` (or `linux-x86_64`) before changing the image.

## Next

- [Inputs](inputs.md) for lockfiles and pinned requirements.
- [Targets](targets.md) for platform names and wheel tags.
- [Python 3.15 readiness](python-3-15-ready.md) for the new-Python version of this question.
