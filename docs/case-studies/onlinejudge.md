# OnlineJudge on Alpine

OnlineJudge runs programming contests with a Django backend. This scan checked revision
`df873278ab1b29510aa3a0979677d7aa9a53ca0e` against PyPI on **28 November 2026**, using wheelgap 0.14.0.
[Pinned revision](https://github.com/QingdaoU/OnlineJudge/tree/df873278ab1b29510aa3a0979677d7aa9a53ca0e)

## Inputs and targets

The backend's final Docker stage starts from `python:3.12-alpine` and installs the pins in
`deploy/requirements.txt`. The release workflow builds the image for `linux/amd64` and `linux/arm64`.
The scan checks the 22 direct pins with CPython 3.12 on `musl-x86_64` and `musl-aarch64`, using wheelgap's
default musl 1.2 floor.
[Dockerfile](https://github.com/QingdaoU/OnlineJudge/blob/df873278ab1b29510aa3a0979677d7aa9a53ca0e/Dockerfile)
· [Requirements](https://github.com/QingdaoU/OnlineJudge/blob/df873278ab1b29510aa3a0979677d7aa9a53ca0e/deploy/requirements.txt)
· [Release workflow](https://github.com/QingdaoU/OnlineJudge/blob/df873278ab1b29510aa3a0979677d7aa9a53ca0e/.github/workflows/release.yml)

## Reproduce the scan

Download the two inputs into an empty directory. The commands read the files without building the image.

```bash
mkdir -p deploy
base=https://raw.githubusercontent.com/QingdaoU/OnlineJudge/df873278ab1b29510aa3a0979677d7aa9a53ca0e
curl -fL "$base/Dockerfile" -o Dockerfile
curl -fL "$base/deploy/requirements.txt" -o deploy/requirements.txt

uvx wheelgap==0.14.0 --dockerfile Dockerfile --dockerfile-inputs
```

The Dockerfile check exits 2. The `RUN` instruction at line 23 puts the pip command inside a heredoc, which
`--dockerfile-inputs` cannot inspect. Pass the requirements file and both targets explicitly.

```bash
uvx wheelgap==0.14.0 deploy/requirements.txt -p 3.12 \
  -t musl-x86_64,musl-aarch64 --explain --suggest --no-cache
```

The scan exits 1. Eighteen pins have wheels for both targets and four build from source on both. The requirements
file isn't a full lock, so the dependencies of these pins weren't checked.

## Gaps and possible fixes

| Pin | `--explain` result on both targets | `--suggest` result and next step |
| --- | --- | --- |
| `coverage==6.5.0` | The musllinux wheels go up to CPython 3.11. | 7.2.7 has CPython 3.12 musllinux wheels for both architectures. Test the upgrade before changing the pin. |
| `Envelopes==0.4` | The release publishes only a source distribution. | No newer release has a matching wheel. Build the pinned release into a wheel once and reuse it from a wheelhouse if repeat builds are costly. |
| `flake8-quotes==3.3.2` | The release publishes only a source distribution. | No newer release has a matching wheel. A wheelhouse can also cache the wheel built from this pin. |
| `psycopg2==2.9.9` | Published wheels are for Windows. | No newer release has a matching wheel. The Dockerfile already installs the compiler and PostgreSQL headers for a source build. A wheelhouse would need a compatible build for each architecture. |

## Cross-check with pip

Run pip in wheel-only mode on both architectures. The three musllinux tags cover musl 1.2 and earlier compatible
musllinux versions, and pip also accepts plain `linux_<arch>` wheels on musl. With `--no-deps`, each command checks
only the named coverage pin.

```bash
for arch in x86_64 aarch64; do
  for version in 6.5.0 7.2.7; do
    uvx --from pip==26.3 pip download --only-binary=:all: --no-deps \
      --platform "musllinux_1_2_$arch" --platform "musllinux_1_1_$arch" \
      --platform "musllinux_1_0_$arch" --platform "linux_$arch" --implementation cp --python-version 3.12 --abi cp312 \
      --dest "wheels-$arch" "coverage==$version"
  done
done
```

For 6.5.0, pip reported `No matching distribution found` on both targets. For 7.2.7, it downloaded
`coverage-7.2.7-cp312-cp312-musllinux_1_1_x86_64.whl` and
`coverage-7.2.7-cp312-cp312-musllinux_1_1_aarch64.whl`.
