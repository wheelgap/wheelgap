"""Seeded differential tests of --suggest-targets' workflow scan against PyYAML.

Each generated workflow is read twice: by the scanner, and by PyYAML (`yaml.compose`, so plain and quoted scalars stay
apart) followed by GitHub's semantics written out plainly below, with their own runner and architecture tables rather
than the module's. The scanner may refuse what it can't read, but whatever it suggests must be what the YAML says: its
targets and Pythons are a subset of the oracle's, and equal to them when it refused nothing.

Three generators: realistic workflows with varied YAML (`realistic`), random atoms of YAML syntax around and inside
real values (`atoms`), and junk values next to a fixed 32-bit setup step that a misread would turn into win-amd64
(`junk`). The last two came from the PR's review.
"""

import itertools
import random
import re

import pytest

from wheelgap.suggest_targets import Suggestions, read_workflow_text

yaml = pytest.importorskip("yaml")

# The labels the generators use, with the target each runs on (GitHub's runner list), and setup-python's
# architectures where they're certain.
RUNNERS = {
    "ubuntu-latest": "linux-x86_64",
    "ubuntu-24.04-arm": "linux-aarch64",
    "windows-latest": "win-amd64",
    "windows-11-arm": "win-arm64",
    "macos-latest": "macos-arm64",
    "macos-14": "macos-arm64",
    "macos-13": "macos-x86_64",
    "macos-15-intel": "macos-x86_64",
}
ARCH = {
    ("linux-x86_64", "x64"): "linux-x86_64",
    ("linux-aarch64", "arm64"): "linux-aarch64",
    ("win-amd64", "x64"): "win-amd64",
    ("win-amd64", "x86"): "win32",
    ("win-arm64", "arm64"): "win-arm64",
    ("macos-x86_64", "x64"): "macos-x86_64",
    ("macos-arm64", "arm64"): "macos-arm64",
    ("macos-arm64", "x64"): "macos-x86_64",
}
PYTHON = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)")
MATRIX_REF = re.compile(r"\$\{\{[ \t]*matrix\.([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\}\}")
TRUE, FALSE = ("true", "True", "TRUE"), ("false", "False", "FALSE")  # core.getBooleanInput


class Skip(Exception):
    """A workflow the oracle can't judge for sure (a merge key, a comparison GitHub may make either way)."""


class S:
    """A scalar as PyYAML read it: its text, and whether it was plain (so 3.10 is a number) or quoted."""

    def __init__(self, text, plain):
        self.text, self.plain = text, plain

    def gh(self):
        """The string GitHub hands on."""
        if self.plain and re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", self.text):
            number = float(self.text)
            if number == int(number) and "." not in self.text:
                return str(int(number))
            return repr(number)
        if self.plain and self.text.lower() in ("", "~", "null"):
            return ""
        return self.text


def convert(node):
    if isinstance(node, yaml.ScalarNode):
        return S(node.value, node.style is None and node.tag != "tag:yaml.org,2002:str")
    if isinstance(node, yaml.SequenceNode):
        return [convert(n) for n in node.value]
    out = {}
    for k, v in node.value:
        if not isinstance(k, yaml.ScalarNode) or k.value in out or k.value == "<<":
            raise Skip("complex, duplicate or merge key")
        out[k.value] = convert(v)
    return out


def expand(matrix):
    """GitHub's matrix: the product, minus excludes, then each include added or appended."""
    if not isinstance(matrix, dict):
        raise Skip("matrix")
    dims = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    if not all(isinstance(v, list) and all(isinstance(x, S) for x in v) for v in dims.values()):
        raise Skip("dims")
    include, exclude = matrix.get("include", []), matrix.get("exclude", [])
    for entries in (include, exclude):
        if not isinstance(entries, list) or not all(
            isinstance(e, dict) and all(isinstance(x, S) for x in e.values()) for e in entries
        ):
            raise Skip("include/exclude")

    def same(a, b):
        equal = a.gh() == b.gh()
        if (a.plain != b.plain or a.text != b.text) and (a.text.lower() == b.text.lower() or equal):
            if not (a.text == b.text and not re.fullmatch(r"[-+.0-9eE]+|(?i:true|false|null|~|)", a.text)):
                raise Skip("a comparison GitHub may make either way")
        return equal

    combos = [dict(zip(dims, values, strict=True)) for values in itertools.product(*dims.values())] if dims else []
    combos = [c for c in combos if not any(all([k in c and same(c[k], v) for k, v in e.items()]) for e in exclude)]
    extra = []
    for entry in include:
        matching = [c for c in combos if all([same(c[k], v) for k, v in entry.items() if k in dims])]
        for c in matching:
            c.update(entry)
        if not matching:
            extra.append(dict(entry))
    return combos + extra


def value(scalar, combo):
    """An input's or runs-on's value in a combination: a string, or None when it can't be known."""
    if not isinstance(scalar, S):
        return None
    if m := MATRIX_REF.fullmatch(scalar.text):
        v = combo.get(m.group(1))
        return "" if v is None else v.gh()
    if "${{" in scalar.text:
        return None
    return scalar.gh()


def oracle(text):
    doc = convert(yaml.compose(text, Loader=yaml.SafeLoader))
    targets, pythons = set(), set()
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise Skip("jobs")
    for job in doc["jobs"].values():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        if isinstance(matrix, S):
            raise Skip("computed matrix")
        combos = expand(matrix) if matrix is not None else [{}]
        steps = job.get("steps")
        steps = (
            [] if steps is None or isinstance(steps, S) and steps.plain and steps.text in ("", "~", "null") else steps
        )
        if not isinstance(steps, list):
            continue
        setups = []
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("uses"), S):
                action = step["uses"].text.split("@")[0].lower()
                if action in ("actions/setup-python", "astral-sh/setup-uv"):
                    inputs = step.get("with") or {}
                    inputs = {k.lower(): v for k, v in inputs.items()} if isinstance(inputs, dict) else {}
                    setups.append((action == "actions/setup-python", inputs))
        for combo in combos:
            label = value(job.get("runs-on"), combo) if "container" not in job else None
            target = RUNNERS.get((label or "").lower())
            # Each step that picks an interpreter: setup-python (its architecture, the runner's own when unset or
            # ''), and setup-uv with a Python (the runner's own).
            archs = [
                (value(i["architecture"], combo) if "architecture" in i else "") if is_python else ""
                for is_python, i in setups
                if is_python or "python-version" in i or "python-arch" in i
            ] or [""]
            for arch in archs:
                if target and arch is not None:
                    mapped = target if arch == "" else ARCH.get((target, arch))
                    if mapped:
                        targets.add(mapped)
            for is_python, inputs in setups:
                if "python-version" not in inputs:
                    continue
                version = value(inputs["python-version"], combo)
                free = value(inputs["freethreaded"], combo) if is_python and "freethreaded" in inputs else "false"
                if not version or not (m := PYTHON.fullmatch(version)) or free not in TRUE + FALSE:
                    continue
                t = m.group(2) or ("t" if free in TRUE else "")
                if not (t and int(m.group(1)) < 13):
                    pythons.add(f"3.{m.group(1)}{t}")
    return targets, pythons


# --- realistic: real workflows with varied YAML ---

LABELS = [*RUNNERS, "my-box", "windows-latest\u00a0"]  # NBSP is part of the label to YAML: an unknown runner
VERSIONS = ["3.11", "3.12", "3.13", "3.14", "3.14t", "3.13.1", "3.10", "pypy3.10"]
ARCHES = ["x64", "x86", "arm64", ""]
NOISE = [
    "x:'y",
    "it's",
    'a"b',
    "plain # not quoted",
    "[not a list",
    "{not a map",
    "nbsp\u00a0here",
    "nbsp\u00a0# not a comment",
    "runs-on windows-latest",
    "architecture x86",
]


def plain_ok(text):
    return (
        bool(text)
        and text[0] not in "-?:,[]{}#&*!|>'\"%@` "
        and not any(bad in text for bad in (": ", " #", "\t", "[", "]", "{", "}", ","))
        and text.lower() not in ("yes", "no", "on", "off", "y", "n", "true", "false", "null", "~")
    )


def scalar(rng, text, flow=False):
    styles = ["single"] + (["double"] if '"' not in text and "\\" not in text else [])
    if plain_ok(text) and not (flow and any(c in text for c in ",[]{}")):
        styles += ["plain"] * 2
    style = rng.choice(styles)
    if style == "plain":
        return text
    if style == "double":
        return f'"{text}"'
    return "'" + text.replace("'", "''") + "'"


def comment(rng):
    return rng.choice(["", "", "", "  # a comment", ' # it\'s "quoted" [x', " #\u00a0nbsp"])


def seq(rng, items, indent, raw=False, flow_only=False):
    pad = " " * indent
    rendered = items if raw else [scalar(rng, x, flow=True) for x in items]
    form = rng.choice(["flow", "multi", "next"] if flow_only else ["block", "flow", "multi", "next"])
    if form == "block":
        return "".join(f"\n{pad}- {r}" for r in rendered)
    if form == "flow":
        return f" [{', '.join(rendered)}]"
    if form == "multi":
        body = f",\n{pad}  ".join(rendered)
        return f" [\n{pad}  {body}{rng.choice(['', ','])}\n{pad}]"
    return f"\n{pad}  [\n" + "".join(f"{pad}    {r},\n" for r in rendered) + f"{pad}  ]"


def flow_map(rng, entry):
    return "{" + ", ".join(f"{k}: {scalar(rng, v, flow=True)}" for k, v in entry.items()) + "}"


def noise_step(rng, pad):
    text = rng.choice(NOISE)
    kind = rng.choices(range(7), weights=[6, 6, 6, 6, 6, 1, 1])[0]
    if kind == 0:
        return f"{pad}- run: |\n{pad}    echo hi\n{pad}    {text}\n"
    if kind == 1:
        return f"{pad}- run: echo building\n{pad}    {text}\n"
    if kind == 2:
        return f"{pad}- run:\n{pad}    echo next line\n"
    if kind == 3:
        return f"{pad}- name: {scalar(rng, text)}{comment(rng)}\n{pad}  run: echo\n"
    if kind == 4:
        return f"{pad}- uses: actions/checkout@v5\n{pad}  with:\n{pad}    fetch-depth: {scalar(rng, text)}\n"
    if kind == 5:
        return f"{pad}- run: {rng.choice(['&r', '!!str'])} |\n{pad}    {text}\n"
    return f"{pad}\t# a comment after a tab\n{pad}- run: echo\n"


def realistic_job(rng):
    use_matrix = rng.random() < 0.7
    matrix, include, exclude = {}, [], []
    if use_matrix:
        matrix["os"] = rng.sample(LABELS, rng.randint(1, 3))
        matrix["python"] = rng.sample(VERSIONS, rng.randint(1, 3))
        if rng.random() < 0.4:
            matrix["arch"] = rng.sample(ARCHES, rng.randint(1, 2))
        for _ in range(rng.choice([0, 0, 1, 2])):
            entry = {"os": rng.choice(LABELS)}
            for key, pool in (("arch", ARCHES), ("python", VERSIONS)):
                if rng.random() < 0.5:
                    entry[key] = rng.choice(pool)
            include.append(entry)
        if rng.random() < 0.3:
            exclude.append({"os": rng.choice(matrix["os"]), "python": rng.choice(matrix["python"])})
    runs_on = "${{ matrix.os }}" if use_matrix and rng.random() < 0.8 else rng.choice(LABELS)
    lines = [f"    runs-on: {runs_on if '$' in runs_on else scalar(rng, runs_on)}{comment(rng)}"]
    if rng.random() < 0.4:
        lines.append("    env:")
        lines += [f"      V{n}: {scalar(rng, text)}{comment(rng)}" for n, text in enumerate(rng.sample(NOISE, 3))]
    if use_matrix:
        lines += ["    strategy:", "      matrix:"]
        lines += [f"        {key}:{seq(rng, values, 8)}" for key, values in matrix.items()]
        for name, entries in (("include", include), ("exclude", exclude)):
            if entries and rng.random() < 0.3:
                lines.append(
                    f"        {name}:{seq(rng, [flow_map(rng, e) for e in entries], 8, raw=True, flow_only=True)}"
                )
            elif entries:
                lines.append(f"        {name}:")
                for e in entries:
                    first, *rest = e.items()
                    lines.append(f"          - {first[0]}: {scalar(rng, first[1])}")
                    lines += [f"            {k}: {scalar(rng, v)}" for k, v in rest]
    lines.append("    steps:")
    steps = []
    for _ in range(rng.randint(0, 2)):
        action = rng.choice(["actions/setup-python@v6", "actions/setup-python@v6", "astral-sh/setup-uv@v7"])
        inputs = {}
        if rng.random() < 0.9:
            inputs["python-version"] = (
                "${{ matrix.python }}" if use_matrix and rng.random() < 0.6 else rng.choice(VERSIONS)
            )
        if "python" in action and rng.random() < 0.5:
            inputs["architecture"] = "${{ matrix.arch }}" if use_matrix and rng.random() < 0.5 else rng.choice(ARCHES)
        if "python" in action and rng.random() < 0.3:
            inputs["freethreaded"] = rng.choice(["true", "false", "True"])
        if rng.random() < 0.5:
            steps.append(noise_step(rng, "      "))
        step = f"      - uses: {action}\n"
        if inputs:
            step += "        with:\n" + "".join(
                f"          {k}: {v if '$' in v else scalar(rng, v)}{comment(rng)}\n" for k, v in inputs.items()
            )
        steps.append(step)
    steps.append(noise_step(rng, "      "))
    return "\n".join(lines) + "\n" + "".join(steps)


def realistic(rng):
    jobs = [realistic_job(rng) for _ in range(rng.randint(1, 2))]
    head = rng.choice(["", "---\n", "%YAML 1.2\n---\n", "# workflow\n"])
    text = head + "on: push\njobs:\n" + "".join(f"  job{n}:\n{job}" for n, job in enumerate(jobs))
    if rng.random() < 0.05:  # a line break other than \n
        at = rng.choice([i for i, c in enumerate(text) if c == "\n"])
        text = text[:at] + rng.choice(["\r", "\x85", "\u2028", "\u2029"]) + text[at:]
    return text


# --- atoms: random pieces of YAML syntax around and inside real values ---

ATOMS = [
    "a", "b", "x86", "x64", "it's", "x:'y", 'x:"y', "- ", " - ", "'", '"', "'", '"', " #", "#", " # c", "[", "]", "{",
    "}", ",", ", ", ": ", ":", " ", "''", '\\"', "\\", "!!str ", "&a ", "*a", "}}", "${{ x }}", "\u00a0", "\t", "? ",
    "|", ">", "windows-latest", "architecture: x86", "@", "`", "%", "=", "- uses: actions/setup-python@v5", "!x ",
]  # fmt: skip


def junk(rng, low=1, high=4):
    return "".join(rng.choice(ATOMS) for _ in range(rng.randint(low, high)))


def quoted_or_plain(rng, text, flow=False):
    safe = (
        text
        and not re.search(r"^[-?:,\[\]{}#&*!|>'\"%@` ]|: | #|\$", text)
        and not (flow and re.search(r"[,\[\]{}]", text))
    )
    if "${{" in text and not flow:
        safe = True
    c = rng.random()
    if safe and c < 0.45:
        return text
    if c < 0.75 or "\\" in text or '"' in text:
        return "'" + text.replace("'", "''") + "'"
    return '"' + text + '"'


def atom_comment(rng):
    return rng.choice(["", "", "", " # c", " #c 'x", ' # "', " # [", " # }", "\t# t", " #\u00a0"])


def flow_noise(rng, pad):
    items = []
    kind = rng.choice("smjJ")
    for i in range(rng.randint(1, 3)):
        v = junk(rng)
        if kind == "s":
            items.append(v)
        elif kind == "m":
            items.append(f"K{i}: {v}")
        elif kind == "j":
            items.append(f'"K{i}":{v}')
        else:
            items.append(f"K{i}:{v}" if rng.random() < 0.3 else f"{rng.choice(['? ', '&k ', '!!str ', ''])}K{i} : {v}")
    o, c = ("[", "]") if kind == "s" else ("{", "}")
    layout = rng.choice(["one", "one", "multi", "next", "nextmulti", "lead"])
    ind = pad + "  " if rng.random() < 0.8 else pad[: rng.randint(0, len(pad))]
    if layout == "one":
        return f" {o}{', '.join(items)}{c}{atom_comment(rng)}"
    if layout == "multi":
        body = "".join(f"{ind}{it},{atom_comment(rng)}\n" for it in items).rstrip("\n").rstrip(",")
        return f" {o}{atom_comment(rng)}\n{body}\n{ind}{c}{atom_comment(rng)}"
    if layout == "next":
        return f"\n{pad}  {o}{', '.join(items)}{c}{atom_comment(rng)}"
    if layout == "nextmulti":
        return f"\n{pad}  {o}\n" + "".join(f"{pad}    {it},\n" for it in items) + f"{pad}  {c}"
    return f" {o}{items[0]}" + "".join(f"\n{ind}, {it}" for it in items[1:]) + f"\n{ind}{c}"


def noise_kv(rng, pad, key):
    kind = rng.randrange(9)
    if kind == 0:
        return f"{pad}{key}:{flow_noise(rng, pad)}\n"
    if kind == 1:
        return f"{pad}{key}: {junk(rng)}{atom_comment(rng)}\n"
    if kind == 2:  # a plain scalar over several lines
        more = "".join(
            f"{pad}{' ' * rng.randint(1, 4)}{junk(rng)}{atom_comment(rng)}\n" for _ in range(rng.randint(1, 3))
        )
        return f"{pad}{key}: a{junk(rng, 0, 2)}\n" + more
    if kind == 3:  # a quoted scalar over several lines
        q = rng.choice("'\"")
        ind = pad + "  " if rng.random() < 0.7 else pad[: rng.randint(0, len(pad))]
        body = junk(rng, 0, 3).replace(q, "")
        start = f"{rng.choice(['', '', '&a ', '!!str '])}{q}a{junk(rng, 0, 2).replace(q, '')}"
        return f"{pad}{key}: {start}\n{ind}{body}\n{ind}b{q}\n"
    if kind == 4:
        header = rng.choice(["|", ">", "|-", ">+", "|2", "&a |", "!!str >"])
        return f"{pad}{key}: {header}{atom_comment(rng)}\n" + "".join(
            f"{pad}    {junk(rng)}\n" for _ in range(rng.randint(1, 2))
        )
    if kind == 5:
        return f"{pad}{key}:\n" + "".join(
            f"{pad}  S{i}: {junk(rng)}{atom_comment(rng)}\n" for i in range(rng.randint(1, 2))
        )
    if kind == 6:
        return f"{pad}{key}:\n{pad}  {junk(rng)}\n"
    if kind == 7:
        return f"{pad}{key}:\n" + "".join(
            f"{pad}  - {junk(rng)}{atom_comment(rng)}\n" for _ in range(rng.randint(1, 2))
        )
    return f"{pad}{key}: {rng.choice(['&a ', '!!str ', '!t '])}{junk(rng)}\n"


def atom_list(rng, values, pad, flow_only=False):
    layout = rng.choice(
        ["flow", "multi", "next", "nextmulti", "lead", "tight"] + ([] if flow_only else ["block", "block0"])
    )
    items = [v if flow_only else quoted_or_plain(rng, v, flow=layout not in ("block", "block0")) for v in values]
    if layout == "block":
        return "".join(f"\n{pad}  - {x}{atom_comment(rng)}" for x in items)
    if layout == "block0":
        return "".join(f"\n{pad}- {x}{atom_comment(rng)}" for x in items)
    if layout == "flow":
        return f" [{', '.join(items)}]{atom_comment(rng)}"
    if layout == "tight":
        return f" [{','.join(items)}]"
    if layout == "multi":
        return f" [{atom_comment(rng)}\n" + "".join(f"{pad}  {x},{atom_comment(rng)}\n" for x in items) + f"{pad}]"
    if layout == "next":
        return f"\n{pad}  [{', '.join(items)}]{atom_comment(rng)}"
    if layout == "nextmulti":
        body = "".join(
            f"{pad}    {x}{',' if i < len(items) - 1 or rng.random() < 0.5 else ''}{atom_comment(rng)}\n"
            for i, x in enumerate(items)
        )
        return f"\n{pad}  [\n{body}{pad}  ]"
    return f" [{items[0]}" + "".join(f"\n{pad}  , {x}" for x in items[1:]) + f"\n{pad}  ]"


def atom_map(rng, entry):
    sep = rng.choice([", ", ",", " , "])
    pairs = sep.join(
        f"{quoted_or_plain(rng, k, flow=True) if rng.random() < 0.2 else k}: {quoted_or_plain(rng, v, flow=True)}"
        for k, v in entry.items()
    )
    return "{" + rng.choice(["", " "]) + pairs + rng.choice(["", " "]) + "}"


def atoms(rng):
    labels = [
        "ubuntu-latest",
        "ubuntu-24.04-arm",
        "windows-latest",
        "windows-11-arm",
        "macos-latest",
        "macos-15-intel",
        "self-hosted",
        "Windows-Latest",
    ]
    arches = ["x86", "x64", "arm64", ""]
    versions = ["3.10", "3.12", "3.13", "3.14t", "3.9", "3.13.2", "pypy3.10", "3.x"]
    out = ["on: push\n"]
    if rng.random() < 0.3:
        out.append(noise_kv(rng, "", "env"))
    out.append("jobs:\n  b:\n")
    pad = "    "
    use_matrix = rng.random() < 0.65
    dims, include, exclude = {}, [], []
    if use_matrix:
        for key, pool, chance in (("os", labels, 0.8), ("arch", arches, 0.5), ("py", versions, 0.5)):
            if rng.random() < chance:
                dims[key] = rng.sample(pool, rng.randint(1, min(3, len(pool))))
        for _ in range(rng.choice([0, 0, 1, 2])):
            entry = {
                k: rng.choice(pool)
                for k, pool in (("os", labels), ("arch", arches), ("py", versions))
                if rng.random() < 0.5
            }
            if entry:
                include.append(entry)
        if dims and rng.random() < 0.3:
            first = next(iter(dims))
            exclude.append({k: rng.choice(v) for k, v in dims.items() if rng.random() < 0.6} or {first: dims[first][0]})
    parts = []
    runs_on = "${{ matrix.os }}" if use_matrix and rng.random() < 0.7 else rng.choice(labels)
    parts.append(f"{pad}runs-on: {quoted_or_plain(rng, runs_on)}{atom_comment(rng)}\n")
    if use_matrix:
        keys = list(dims)
        rng.shuffle(keys)
        body = [f"{pad}    {k}:{atom_list(rng, dims[k], pad + '    ')}\n" for k in keys]
        for name, entries in (("include", include), ("exclude", exclude)):
            if not entries:
                continue
            if rng.random() < 0.4:
                body.append(
                    f"{pad}    {name}:"
                    f"{atom_list(rng, [atom_map(rng, e) for e in entries], pad + '    ', flow_only=True)}\n"
                )
            else:
                text = f"{pad}    {name}:\n"
                for e in entries:
                    if rng.random() < 0.3:
                        text += f"{pad}      - {atom_map(rng, e)}{atom_comment(rng)}\n"
                    else:
                        (k0, v0), *rest = e.items()
                        text += f"{pad}      - {k0}: {quoted_or_plain(rng, v0)}{atom_comment(rng)}\n"
                        text += "".join(
                            f"{pad}        {k}: {quoted_or_plain(rng, v)}{atom_comment(rng)}\n" for k, v in rest
                        )
                body.append(text)
        rng.shuffle(body)
        if rng.random() < 0.15:
            body.insert(rng.randint(0, len(body)), noise_kv(rng, pad + "    ", "zz"))
        parts.append(f"{pad}strategy:\n{pad}  matrix:\n" + "".join(body))
    for _ in range(rng.choice([0, 1, 1, 2])):
        parts.append(noise_kv(rng, pad, rng.choice(["env", "name", "if", "env2"])))
    rng.shuffle(parts)
    out += parts
    out.append(f"{pad}steps:\n")
    step_pad = pad + "  "

    def noise():
        k = rng.random()
        if k < 0.5:
            return f"{step_pad}- name: n\n" + noise_kv(rng, step_pad + "  ", rng.choice(["run", "env", "shell"]))
        if k < 0.8:
            return noise_kv(rng, step_pad, "- run")
        return f"{step_pad}- run: echo {junk(rng, 1, 3)}\n"

    steps = []
    for _ in range(rng.choice([0, 1, 1, 1, 2])):
        action = rng.choice(["actions/setup-python@v5"] * 3 + ["astral-sh/setup-uv@v6", "Actions/Setup-Python@v6"])
        inputs = []
        if rng.random() < 0.85:
            inputs.append(
                ("python-version", "${{ matrix.py }}" if use_matrix and rng.random() < 0.5 else rng.choice(versions))
            )
        if "ython" in action and rng.random() < 0.7:
            inputs.append(
                ("architecture", "${{ matrix.arch }}" if use_matrix and rng.random() < 0.5 else rng.choice(arches))
            )
        rng.shuffle(inputs)
        text = f"{step_pad}- uses: {quoted_or_plain(rng, action)}{atom_comment(rng)}\n"
        if inputs:
            lines = [f"{step_pad}    {k}: {quoted_or_plain(rng, v)}{atom_comment(rng)}\n" for k, v in inputs]
            if rng.random() < 0.3:
                lines.insert(rng.randint(0, len(lines)), noise_kv(rng, step_pad + "    ", "cache"))
            text += f"{step_pad}  with:\n" + "".join(lines)
        for _ in range(rng.choice([0, 0, 1])):
            steps.append(noise())
        steps.append(text)
    for _ in range(rng.choice([0, 1, 1, 2])):
        steps.append(noise())
    return "".join(out + steps)


# --- junk: junk values next to a fixed setup-python with architecture x86 on windows-latest (win32) ---

JUNK = [
    "[", "]", "{", "}", ", ", ",", ": ", ":", "'", '"', "'", '"', " #", " # ", "#", "- ", " - ", "? ", "!!str ", "&a ",
    "*a", "a", "b c", "x:", " ", "\\", "''", "|", ">", "NL", "NL", "x86", "windows-latest", "K: ", '"K":', "'K':",
    "\t", "\u00a0", "@", "%", "`", "=", "$", "${{ m }}", "}}",
]  # fmt: skip


def junk_text(rng, low, high, newline):
    return "".join(rng.choice(JUNK) for _ in range(rng.randint(low, high))).replace("NL", newline)


def junk_value(rng, pad):
    newline = "\n" + pad + "  " + " " * rng.randint(0, 2)
    kind = rng.randrange(6)
    if kind == 0:
        return "{" + junk_text(rng, 1, 8, newline) + "}"
    if kind == 1:
        return "[" + junk_text(rng, 1, 8, newline) + "]"
    if kind == 2:
        return junk_text(rng, 1, 8, newline)
    if kind == 3:
        return rng.choice("'\"") + junk_text(rng, 1, 6, newline) + rng.choice("'\"")
    if kind == 4:
        return "\n" + pad + "  " + rng.choice("[{") + junk_text(rng, 1, 6, newline) + rng.choice("]}")
    return (
        rng.choice(["{", "["])
        + junk_text(rng, 1, 4, newline)
        + newline
        + junk_text(rng, 0, 4, newline)
        + rng.choice("]}")
    )


def junk_workflow(rng):
    pad = "    "
    shape = rng.randrange(8)
    closer = junk_text(rng, 0, 3, " ")
    setup_noise = f"          cache: {junk_value(rng, '          ')}\n" if shape == 1 else ""
    pre = f"{pad}env: {junk_value(rng, pad)}\n" if shape in (0, 2, 3) else ""
    prestep = f"      - run: {junk_value(rng, '        ')}\n" if shape in (3, 4) else ""
    prestep2 = f"      - name: n\n        env: {junk_value(rng, '        ')}\n" if shape == 5 else ""
    matrix, runs_on = "", "windows-latest"
    if shape in (6, 7):
        runs_on = "${{ matrix.os }}"
        items = ["windows-latest", junk_text(rng, 1, 4, "\n            ")]
        rng.shuffle(items)
        if shape == 6:
            body = " [" + ", ".join(items) + "]"
        else:
            body = (
                "\n          [\n            " + ",\n            ".join(items) + rng.choice(["", ","]) + "\n          ]"
            )
        matrix = f"{pad}strategy:\n{pad}  matrix:\n{pad}    os:{body}\n"
    return (
        f"on: push\njobs:\n  b:\n{pad}runs-on: {runs_on}\n{matrix}{pre}{pad}steps:\n{prestep}{prestep2}"
        f"      - uses: actions/setup-python@v5\n        with:\n{setup_noise}"
        f"          python-version: '3.12'\n          architecture: x86\n      - run: echo {closer}\n"
    )


@pytest.mark.parametrize(
    ("generator", "cases", "least_valid", "least_exact"),
    [(realistic, 3000, 1500, 300), (atoms, 6000, 300, 50), (junk_workflow, 6000, 600, 200)],
)
def test_scan_never_disagrees_with_pyyaml(generator, cases, least_valid, least_exact):
    rng = random.Random(f"wheelgap-228-{generator.__name__}")
    valid = exact = 0
    for case in range(cases):
        text = generator(rng)
        try:
            expected_targets, expected_pythons = oracle(text)
        except (yaml.YAMLError, Skip):
            continue
        out = Suggestions()
        read_workflow_text(text, "ci.yml", out)
        targets, pythons = {f.value for f in out.targets}, {f.value for f in out.pythons}
        context = f"case {case}:\n{text}\nscan: {targets} {pythons} {out.unreadable}\n"
        context += f"pyyaml: {expected_targets} {expected_pythons}"
        assert targets <= expected_targets, context
        assert pythons <= expected_pythons, context
        if not out.unreadable:
            assert (targets, pythons) == (expected_targets, expected_pythons), context
            exact += 1
        valid += 1
    print(f"{generator.__name__}: {valid} valid YAML, {exact} read in full")
    assert valid >= least_valid
    assert exact >= least_exact


# --- round5: the review's generator for the whitelist's widenings (continuation lines, anchors and aliases, escaped
# strings, nested flows, `key: {a: b}`, object matrices), with its own oracle and tables. ---

RUN5 = {
    "ubuntu-latest": "linux-x86_64",
    "ubuntu-24.04-arm": "linux-aarch64",
    "windows-latest": "win-amd64",
    "windows-11-arm": "win-arm64",
    "macos-latest": "macos-arm64",
    "macos-15-intel": "macos-x86_64",
    "ubuntu-22.04": "linux-x86_64",
}
ARCH5 = {
    ("linux-x86_64", "x64"): "linux-x86_64",
    ("linux-aarch64", "arm64"): "linux-aarch64",
    ("win-amd64", "x64"): "win-amd64",
    ("win-amd64", "x86"): "win32",
    ("win-arm64", "arm64"): "win-arm64",
    ("macos-x86_64", "x64"): "macos-x86_64",
    ("macos-arm64", "arm64"): "macos-arm64",
    ("macos-arm64", "x64"): "macos-x86_64",
}
PYTHON5 = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)")
REF5 = re.compile(r"\$\{\{[ \t]*matrix\.([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\}\}")
NUM5 = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


class S5:
    __slots__ = ("text", "plain")

    def __init__(s, text, plain):
        s.text, s.plain = text, plain

    def gh(s):
        if s.plain and NUM5.fullmatch(s.text):
            f = float(s.text)
            if f == int(f) and "." not in s.text and "e" not in s.text.lower():
                return str(int(f))
            return f"{f:.15g}"
        if s.plain and s.text.lower() in ("", "~", "null"):
            return ""
        if s.plain and re.fullmatch(
            r"(?i:yes|no|on|off|y|n|true|false)|[-+]?0[xo][0-9a-fA-F_]+|[-+]?\.(?i:inf)|\.(?i:nan)|[-+]?[0-9][0-9_:.]*",
            s.text,
        ):
            if s.text not in ("true", "false"):
                raise Skip("yaml1.1-ambiguous")
        return s.text


def conv5(node):
    if node is None:
        raise Skip("empty")
    if isinstance(node, yaml.ScalarNode):
        return S5(node.value, node.style is None)
    if isinstance(node, yaml.SequenceNode):
        return [conv5(n) for n in node.value]
    out = {}
    for k, v in node.value:
        if not isinstance(k, yaml.ScalarNode):
            raise Skip("complex key")
        if k.value in out or (k.value == "<<" and k.style is None):
            raise Skip("dup/merge key")
        out[k.value] = conv5(v)
    return out


def eq5(a, b):
    if not isinstance(a, S5) or not isinstance(b, S5):
        if isinstance(a, S5) != isinstance(b, S5):
            return False
        raise Skip("object compare")
    same = a.gh() == b.gh()
    if (a.plain != b.plain or a.text != b.text) and (a.text.lower() == b.text.lower() or same):
        raise Skip("ambiguous compare")
    if (
        a.plain
        and (NUM5.fullmatch(a.text) or a.text.lower() in ("true", "false", "null", "~", ""))
        and a.text != b.text
    ):
        if same:
            raise Skip("ambiguous compare")
    return same


def expand5(m):
    if not isinstance(m, dict):
        raise Skip("matrix")
    dims = {k: v for k, v in m.items() if k not in ("include", "exclude")}
    for v in dims.values():
        if not isinstance(v, list):
            raise Skip("dims")
    inc, exc = m.get("include", []), m.get("exclude", [])
    for L in (inc, exc):
        if not isinstance(L, list) or not all(isinstance(e, dict) for e in L):
            raise Skip("inc")
    if len({k.lower() for k in dims} | {k.lower() for e in inc + exc for k in e}) != len(
        set(dims) | {k for e in inc + exc for k in e}
    ):
        raise Skip("case keys")
    if any(k not in dims for e in exc for k in e):
        raise Skip("exclude unknown key")
    combos = [dict(zip(dims, vals, strict=True)) for vals in itertools.product(*dims.values())] if dims else []
    combos = [c for c in combos if not any(all(eq5(c[k], v) for k, v in e.items()) for e in exc)]
    extra = []
    for e in inc:
        match = [c for c in combos if all(k not in dims or eq5(c[k], v) for k, v in e.items())]
        for c in match:
            c.update(e)
        if not match:
            extra.append(dict(e))
    return combos + extra


def val5(s, combo):
    if not isinstance(s, S5):
        return None
    if m := REF5.fullmatch(s.text):
        v = combo.get(m.group(1))
        if v is None:
            return ""
        return v.gh() if isinstance(v, S5) else None
    if "${{" in s.text:
        return None
    return s.gh()


def oracle5(text):
    doc = conv5(yaml.compose(text, Loader=yaml.SafeLoader))
    T, P = set(), set()
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise Skip("jobs")
    for job in doc["jobs"].values():
        if not isinstance(job, dict):
            continue
        st = job.get("strategy")
        matrix = st.get("matrix") if isinstance(st, dict) else None
        if isinstance(matrix, S5):
            raise Skip("scalar matrix")
        combos = expand5(matrix) if matrix is not None else [{}]
        steps = job.get("steps")
        if steps is None or isinstance(steps, S5):
            steps = []
        if not isinstance(steps, list):
            continue
        setups = []
        for s in steps:
            if isinstance(s, dict) and isinstance(s.get("uses"), S5):
                a = s["uses"].text.split("@")[0].lower()
                if a in ("actions/setup-python", "astral-sh/setup-uv"):
                    w = s.get("with")
                    w = {k.lower(): v for k, v in w.items()} if isinstance(w, dict) else {}
                    setups.append((a, w))
        for combo in combos:
            label = val5(job.get("runs-on"), combo) if "container" not in job else None
            tgt = RUN5.get((label or "").lower())
            archs = [
                val5(w["architecture"], combo)
                if a.endswith("python") and "architecture" in w
                else (None if "python-arch" in w else "")
                for a, w in setups
                if a.endswith("python") or "python-version" in w or "python-arch" in w
            ] or [""]
            for arch in archs:
                if tgt and arch is not None:
                    m = tgt if arch == "" else ARCH5.get((tgt, arch))
                    if m:
                        T.add(m)
            for a, w in setups:
                if "python-version" in w:
                    v = val5(w["python-version"], combo)
                    if v and (m := PYTHON5.fullmatch(v)):
                        py = f"3.{m.group(1)}{m.group(2)}"
                        if a.endswith("python") and "freethreaded" in w:
                            f = (
                                val5(w["freethreaded"], combo)
                                if isinstance(w["freethreaded"], S5) and not REF5.fullmatch(w["freethreaded"].text)
                                else None
                            )
                            if f is None:
                                # unknown: either
                                P.add(py)
                                if not py.endswith("t") and int(m.group(1)) >= 13:
                                    P.add(py + "t")
                                continue
                            if f in ("true", "True", "TRUE"):
                                py = py if py.endswith("t") else py + "t"
                            elif f not in ("false", "False", "FALSE"):
                                continue
                        if not (py.endswith("t") and int(m.group(1)) < 13):
                            P.add(py)
    return T, P


# ---------------------------------------------------------------- generator
CONT = [
    "x",
    "architecture:x86",
    "- uses: actions/setup-python@v5",
    "runs-on:windows-latest",
    "'q'",
    "[a]",
    "{a: b}",
    "? x",
    "*a1",
    "&b",
    "|",
    "with:",
    "architecture: x86",
    "- x",
    "x # c",
    "#x",
    '"architecture": x86',
    "a :b",
    "python-version: '3.9'",
    "x:",
    "-",
    "!!str x",
    "@x",
    "\tx",
    "x\t# c",
    "architecture:\tx86",
]
JUNKLINES = CONT + [
    "architecture: x86",
    "runs-on: macos-15-intel",
    "python-version: '3.8'",
    "container: c",
    "if: x",
    "freethreaded: true",
    "- uses: actions/setup-python@v5",
    "with:",
    "matrix:",
    "include:",
    "exclude:",
    "steps:",
    "strategy:",
    "- os: windows-11-arm",
    "os: [windows-11-arm]",
    "- windows-11-arm",
    "[",
    "]",
    "],",
    "{",
    "}",
    "---",
    "...",
    "<<: *a1",
    "- *a1",
    "*a1",
    "- &q",
    "&q",
    "? a",
    ": b",
    "- - a",
    "-\t- a",
    "'k': v",
    '"k\\t": v',
    "k: 'v''w'",
    'k: "v\\"w"',
    "k: |",
    "k: >-",
    "k: |2",
    "k: [a, [b, {c: d}]]",
    "k: {a: {b: c}}",
    "k: {}",
    "k: []",
    "k: [",
    "k: &q",
    "k: *a1",
    "k: *a2",
    "k: !!str v",
    "k: &q v",
    "# c",
    "",
    "  ",
    "k: v # c",
    "k: a: b",
    "k: a:b",
    "k : v",
    "k:v",
    "- k: v",
    "-   k: v",
    "- - k",
    "%YAML 1.2",
]


def cmt5(r):
    return r.choice(["", "", "", "", " # c", " #c: 'x", ' # "', " # [", " #"] + ["\t# t"] * (r.random() < 0.02))


class G5:
    def __init__(self, r, light):
        self.r, self.light = r, light
        self.anchors = {}  # name -> kind
        self.n = 0
        self.widen = r.random() < 0.85  # use the widened styles at all

    def kq(self, k):
        c = self.r.random()
        return k if c < 0.8 else f"'{k}'" if c < 0.9 else f'"{k}"'

    def plain_ok(self, s, flow):
        if not s or re.search(r"^[-?:,\[\]{}#&*!|>'\"%@` ]|: | #|:$| $", s):
            return False
        return not (flow and re.search(r"[,\[\]{}:#]", s))

    def esc(self, s):
        r = self.r
        if not s:
            return '"\\x41"' if r.random() < 0.3 else '""'
        i = r.randrange(len(s))
        k = r.choice([0, 1, 2, 4]) if self.light else r.randrange(5)
        if k == 0:
            return '"' + s[:i] + f"\\x{ord(s[i]):02x}" + s[i + 1 :] + '"'
        if k == 1:
            return '"' + s[:i] + f"\\u{ord(s[i]):04x}" + s[i + 1 :] + '"'
        if k == 2:
            return '"' + s + '\\"' + '"'
        if k == 3:
            return '"' + s[:i] + "\\" + "\n      " + s[i:] + '"'  # escaped line break (multi-line)
        return '"' + s + r.choice(["\\t", "\\n", "\\\\", "\\ ", '\\" #', '\\": x']) + '"'

    def scalar(self, s, pad, flow=False, sem=True):
        """text to put after `key:` / `- ` (with leading space or newline)"""
        r = self.r
        c = r.random()
        ok = self.plain_ok(s, flow) or ("${{" in s and not flow)
        if not self.widen or c < 0.55:
            if ok and r.random() < 0.5:
                return " " + s
            return " '" + s.replace("'", "''") + "'" if r.random() < 0.6 or '"' in s or "\\" in s else ' "' + s + '"'
        if c < 0.65 and not (self.light and flow and r.random() < 0.9):
            return " " + self.esc(s)
        if flow:
            return " " + (s if ok else "'" + s + "'")
        if c < 0.73 and ok:
            return "\n" + pad + " " * r.randint(1, 3) + s + cmt5(r)  # plain on the next line
        if c < 0.83 and ok:
            n = r.randint(1, 2)
            return (
                " "
                + s
                + "".join(
                    "\n"
                    + pad
                    + " " * r.randint(0, 4)
                    + r.choice(
                        ["x", "y z", "more text", "&& x == 'q'", "architecture:x86", "- x", "[a]", "*a1"]
                        if self.light
                        else CONT
                    )
                    for _ in range(n)
                )
            )
        if c < 0.88:
            return " " + r.choice(["|", "|-", ">-", ">", "|+", "|2", ">1-"]) + cmt5(r) + "\n" + pad + "  " + s
        if c < 0.94 and self.anchors:
            return " *" + r.choice(list(self.anchors))
        if c < 0.97 and ok:
            self.n += 1
            name = f"a{self.n}"
            self.anchors[name] = "s"
            return f" &{name}" + (" " + s if r.random() < 0.3 and not self.light else "\n" + pad + "  " + s)
        return " " + (r.choice(["!!str ", "! "]) if not self.light else "") + (s if ok else "'" + s + "'")

    def flow(self, v, depth=0):
        r = self.r
        if isinstance(v, str):
            return self.scalar(v, "", flow=True)[1:]
        sp = r.choice(["", "", " "])
        if isinstance(v, list):
            return (
                "[" + sp + ", ".join(self.flow(x, depth + 1) for x in v) + r.choice(["", "", ","]) * bool(v) + sp + "]"
            )
        colon = ": " if r.random() < 0.95 else r.choice([":", ":  ", " : "])
        return "{" + sp + ", ".join(f"{self.kq(k)}{colon}{self.flow(x, depth + 1)}" for k, x in v.items()) + sp + "}"

    def anchor(self):
        if self.widen and self.r.random() < 0.12:
            self.n += 1
            name = f"a{self.n}"
            self.anchors[name] = "b"
            return f" &{name}"
        return ""

    def value(self, v, pad):
        """the text after `key:`; pad is the key's indentation"""
        r = self.r
        if isinstance(v, str):
            return self.scalar(v, pad) + (cmt5(r) if r.random() < 0.3 else "")
        if self.widen and self.anchors and r.random() < 0.06:
            return " *" + r.choice(list(self.anchors))
        fl = r.random()
        if isinstance(v, dict):
            if fl < (0.3 if self.widen else 0.05):
                t = " " + self.flow(v) + cmt5(r)
                if r.random() < 0.08:
                    t += "\n" + pad + " " * r.randint(1, 3) + r.choice(JUNKLINES)
                return t
            a = self.anchor()
            ind = pad + " " * r.choice([2, 2, 2, 1, 4])
            return (
                a + cmt5(r) + "\n" + "\n".join(f"{ind}{self.kq(k)}:{self.value(x, ind)}" for k, x in v.items())
                if v
                else " {}"
            )
        # list
        if fl < 0.3:
            return " " + self.flow(v) + cmt5(r)
        if fl < 0.45 and all(isinstance(x, str) or r.random() < 0.5 for x in v):
            ind = pad + "  "
            items = [self.flow(x) for x in v]
            body = "".join(
                f"\n{ind}  {it}{',' if i < len(items) - 1 or r.random() < 0.5 else ''}{cmt5(r)}"
                for i, it in enumerate(items)
            )
            if r.random() < 0.5:
                return f" [{cmt5(r)}{body}\n{pad if r.random() < 0.8 else ind}]{cmt5(r)}"
            return f"\n{ind}[{body}\n{ind}]"
        a = self.anchor()
        ind = pad + r.choice(["  ", "  ", "", "    "])
        out = []
        for x in v:
            if isinstance(x, str):
                out.append(f"{ind}-{self.scalar(x, ind)}{cmt5(r)}")
            elif isinstance(x, dict) and x:
                k = r.random()
                if k < (0.25 if self.widen else 0.03):
                    out.append(f"{ind}- {self.flow(x)}{cmt5(r)}")
                elif k < 0.32 and self.widen and self.anchors:
                    out.append(f"{ind}- *{r.choice(list(self.anchors))}")
                else:
                    sub = ind + "  "
                    lines = [f"{self.kq(kk)}:{self.value(xx, sub)}" for kk, xx in x.items()]
                    if r.random() < 0.1:
                        out.append(f"{ind}-{self.anchor()}\n" + "\n".join(sub + ln for ln in lines))
                    else:
                        out.append(f"{ind}- " + lines[0] + "".join("\n" + sub + ln for ln in lines[1:]))
            else:
                out.append(f"{ind}- {self.flow(x)}")
        return a + "\n" + "\n".join(out) if out else " []"


def model5(r):
    labels = list(RUN5) + ["Windows-Latest", "self-hosted", "ubuntu-22.04"]
    pys = ["3.12", "3.13", "3.14t", "3.10", "3.13.1", "pypy3.10", "3.x", "3.9"]
    arches = ["x86", "x64", "arm64", ""]
    jobs = {}
    defs = {}
    for j in range(r.choice([1, 1, 1, 2])):
        use_m = r.random() < 0.6
        m = {}
        if use_m:
            if r.random() < 0.8:
                m["os"] = r.sample(labels, r.randint(1, 3))
            if r.random() < 0.6:
                m["py"] = r.sample(pys, r.randint(1, 2))
            if r.random() < 0.3:
                m["arch"] = r.sample(arches[:3], r.randint(1, 2))
            if r.random() < 0.3:  # object dimension
                m["cfg"] = [
                    r.choice(
                        [
                            {"name": "a", "flags": ["x", "y"]},
                            {"os": "windows-11-arm"},
                            ["a", "b"],
                            "plain",
                            {"k": {"n": "v"}},
                        ]
                    )
                    for _ in range(r.randint(1, 2))
                ]
            if r.random() < 0.08:
                m[r.choice(["os", "empty"])] = []
            if r.random() < 0.4:
                m["include"] = [
                    dict(
                        r.sample(
                            [
                                ("os", r.choice(labels)),
                                ("py", r.choice(pys)),
                                ("arch", r.choice(arches)),
                                ("extra", "e"),
                                ("obj", r.choice([{"a": "b"}, ["l"], "s"])),
                            ],
                            r.randint(1, 3),
                        )
                    )
                    for _ in range(r.randint(1, 2))
                ]
            if r.random() < 0.25 and (set(m) - {"include", "cfg", "empty"}):
                k = r.choice(sorted(set(m) - {"include", "cfg", "empty"}))
                if m[k]:
                    m["exclude"] = [{k: r.choice(m[k])}]
        parts = []
        parts.append(("runs-on", "${{ matrix.os }}" if use_m and r.random() < 0.55 else r.choice(labels)))
        if use_m:
            parts.append(("strategy", {"fail-fast": "false", "matrix": m} if r.random() < 0.5 else {"matrix": m}))
        if r.random() < 0.06:
            parts.append(("container", r.choice(["python:3.12", {"image": "x"}])))
        if r.random() < 0.15:
            parts.append(("if", "github.event_name == 'push' && x"))
        if r.random() < 0.4:
            parts.append(("env", {"A": "architecture x86 with", "B": "b"}))
        if r.random() < 0.2:
            parts.append(("name", "some long name here"))
        r.shuffle(parts)
        steps = []
        for _ in range(r.choice([0, 1, 1, 1, 2])):
            steps.append(
                r.choice(
                    [
                        {"run": "echo hi there"},
                        {"name": "n", "run": "pip install x", "env": {"K": "v"}},
                        {"uses": "actions/checkout@v5"},
                        {"uses": "actions/checkout@v5", "with": {"python-version": "3.8", "architecture": "x86"}},
                    ]
                )
            )
        for _ in range(r.choice([0, 1, 1, 1, 2])):
            act = r.choice(["actions/setup-python@v5"] * 4 + ["astral-sh/setup-uv@v6", "Actions/Setup-Python@v6"])
            w = []
            if r.random() < 0.85:
                w.append(("python-version", "${{ matrix.py }}" if use_m and r.random() < 0.5 else r.choice(pys)))
            if "ython" in act and r.random() < 0.6:
                w.append(("architecture", "${{ matrix.arch }}" if use_m and r.random() < 0.4 else r.choice(arches)))
            if "ython" in act and r.random() < 0.2:
                w.append(("freethreaded", r.choice(["true", "false", "True", "yes"])))
            if "uv" in act and r.random() < 0.2:
                w.append(("python-arch", "x86"))
            if r.random() < 0.2:
                w.append(("cache", "pip"))
            r.shuffle(w)
            st = [("uses", act)]
            if w:
                st.append(("with", dict(w)))
            if r.random() < 0.1:
                st.append(("if", "matrix.os == 'x'"))
            if r.random() < 0.3:
                st.append(("name", "set up"))
            r.shuffle(st)
            steps.insert(r.randint(0, len(steps)), dict(st))
        if steps or r.random() < 0.9:
            parts.append(("steps", steps))
        if r.random() < 0.3:
            r.shuffle(parts)
        jobs[f"j{j}"] = dict(parts)
        # definitions that aliases can point at
        if r.random() < 0.5:
            defs[f"d{j}"] = r.choice(
                [
                    {"python-version": "3.9", "architecture": "x86"},
                    {"os": ["windows-11-arm"]},
                    ["windows-11-arm", "macos-15-intel"],
                    {"uses": "actions/setup-python@v5", "with": {"python-version": "3.8"}},
                    "x86",
                    {"matrix": {"os": ["macos-15-intel"]}},
                    [{"uses": "actions/setup-python@v5", "with": {"python-version": "3.8", "architecture": "x86"}}],
                ]
            )
    return defs, jobs


def round5(r, light=False):
    g = G5(r, light)
    defs, jobs = model5(r)
    head = r.choice(["", "", "---\n", "%YAML 1.2\n---\n", "# c\n"])
    text = head + "on: push\n"
    if defs:
        g2_anchor_all = r.random() < 0.7
        text += "x-defs:\n"
        for k, v in defs.items():
            if g.widen and g2_anchor_all:
                g.n += 1
                name = f"a{g.n}"
                g.anchors[name] = "b"
                if isinstance(v, str):
                    text += f"  {k}: &{name}" + r.choice([f" {v}\n", f"\n    {v}\n"])
                    continue
                body = g.value(v, "  ")
                if body.startswith("\n"):
                    text += f"  {k}: &{name}{body}\n"
                else:
                    text += f"  {k}: &{name}{body}\n" if r.random() < 0.5 else f"  {k}:{body}\n"
            else:
                text += f"  {k}:{g.value(v, '  ')}\n"
    text += "jobs:" + g.value(jobs, "") + "\n"
    # text-level mutations
    lines = text.split("\n")
    for _ in range(r.choice([0, 0, 0, 0, 0, 0, 1] if light else [0, 0, 0, 1, 1, 2, 3])):
        if not lines:
            break
        i = r.randrange(len(lines))
        k = r.randrange(10)
        ind = len(lines[i]) - len(lines[i].lstrip(" "))
        if k == 0:
            lines[i] = " " * r.randint(1, 2) + lines[i]
        elif k == 1 and ind:
            lines[i] = lines[i][r.randint(1, min(2, ind)) :]
        elif k in (2, 3, 4):
            lines.insert(i, " " * max(0, ind + r.choice([-2, -1, 0, 0, 1, 2, 2, 4])) + r.choice(JUNKLINES))
        elif k == 5:
            del lines[i]
        elif k == 6:
            lines.insert(i, lines[i])
        elif k == 7:
            lines[i] = lines[i].replace(": ", r.choice([":", ":\t", " : ", ":  "]), 1)
        elif k == 8:
            lines[i] = lines[i][:ind] + r.choice(["\t", "- ", "? ", "&q ", "!!map ", "\u00a0"]) + lines[i][ind:]
        elif k == 9 and i + 1 < len(lines):
            lines[i] = lines[i] + r.choice([" ", ""]) + lines.pop(i + 1).lstrip(" ")
    return "\n".join(lines).replace("\n", r.choice(["\n"] * 12 + ["\r\n"]))


@pytest.mark.parametrize("light", [False, True])
def test_round5_never_disagrees_with_pyyaml(light):
    valid = exact = 0
    for seed in range(3000):
        rng = random.Random(f"wheelgap-228-round5-{light}-{seed}")
        text = round5(rng, light)
        out = Suggestions()
        read_workflow_text(text, "ci.yml", out)
        targets, pythons = {f.value for f in out.targets}, {f.value for f in out.pythons}
        context = f"seed {seed}:\n{text}\nscan: {targets} {pythons} {out.unreadable}"
        try:
            expected_targets, expected_pythons = oracle5(text)
        except yaml.YAMLError:
            # YAML PyYAML rejects: nothing may be suggested from it (tabs aside, which PyYAML is stricter about).
            assert not (targets or pythons) or "\t" in text, context
            continue
        except (Skip, RecursionError):
            continue
        context += f"\npyyaml: {expected_targets} {expected_pythons}"
        assert targets <= expected_targets and pythons <= expected_pythons, context
        if not out.unreadable:
            assert (targets, pythons) == (expected_targets, expected_pythons), context
            exact += 1
        valid += 1
    assert valid >= 800
    assert exact >= 30
