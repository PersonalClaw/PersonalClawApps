"""The supply-chain check: CI's inputs are pinned, and every job holds only the GitHub token
permissions it needs.

It reads ``.github/workflows/*.yml``, every ``package.json`` CI installs from (each app's
``ui/package.json``, which the ``ui-bundles`` check runs ``npm ci`` in, and any directory a
workflow step runs npm in), ``.nvmrc``, any Dockerfile in the tree, and
``.github/supply-chain.toml``, which holds the Node end-of-life table, the audited write steps
and the exceptions. ``check`` returns one violation per broken rule and never raises, so an input
it cannot read is a failure, not a crash. The rule ids are the website repository's, so one
exception or finding reads the same in both.

Pins:

- ``action-sha``: every ``uses:`` names a 40-character commit SHA with its ``vX.Y.Z`` tag as a
  comment. A local ``./`` action is the repository's own code.
- ``image-digest``: every ``container:``, ``services:`` image, ``docker://`` reference and
  Dockerfile ``FROM`` carries a digest and one platform (``# linux/amd64`` beside it, or
  ``--platform``). An image named through an ``ARG`` is judged by the ``ARG``'s default; a digest
  with no platform names an index, not one platform's image.
- ``run-install``: every tool a ``run:`` line installs names an exact version, or installs from a
  lock. ``"$CORE_SPEC"`` is the one variable an install may name, because ``test_core_lock.py``
  holds it to the commit ``core.lock`` pins; any other variable is a version the check cannot see.
- ``setup-floating``: a setup action pins an exact version or a version file and never sets
  ``check-latest``, and every Python a ``uv`` command or ``UV_PYTHON`` names is an exact
  ``x.y.z`` (uv, not setup-python, is how this repository's CI gets its Python).
- ``package-manager``: every ``package.json`` CI installs from has ``packageManager:
  npm@<exact>``.
- ``node-version-exact``, ``node-eol``, ``node-version-lock``: ``.nvmrc`` is an exact release, on
  a line the end-of-life table lists and has not passed, and meets every locked package's
  ``engines.node`` (optional platform packages excepted). The website's two ``engines`` rules have
  no subject here: no apps ``package.json`` declares ``engines``.

Least privilege:

- ``permissions-declared``: every job declares its permissions, or its workflow does at the top;
  ``write-all``: no job or workflow grants every scope.
- ``unlisted-write``: every write a job holds, its own or inherited from the top, is an audited
  write step in the policy file; ``unused-top-level-write``: a write at the top that no job
  inherits fails, because it would reach every job added later.
- ``persist-credentials``: every ``actions/checkout`` sets ``persist-credentials: false`` unless
  it is listed as an audited write step.
- ``token-env``: no workflow- or job-level ``env:`` passes ``github.token`` or a secret; a token
  goes on the steps that call the API.

The policy file's own rules (``policy-file``, ``audited-write-entry``, ``exception``) and a
workflow shape the reader cannot judge (``workflow-shape``) are never waived. An exception waives
one rule for one reference in one file, names an owner, a reason and an expiry at most 30 days
out, and fails when it has expired, is further out, matches nothing or misses a field.

What it does not see: an install a script runs rather than a ``run:`` line. The one script that
installs, ``scripts/test_bundles.py``, holds its own installs to this bar: every package from the
bundle lock with its hash, core from its locked commit, and a refusal for a package the lock holds
without a hash (``test_bundle_runner.py``). Only the canary's ``--float`` run resolves ranges.

Each rule fails on a planted violation below, and passes on the planted baseline, so a rule that
matched nothing could not pass.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import tomllib
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = ".github/supply-chain.toml"
EXCEPTION_MAX_DAYS = 30

WAIVABLE_RULES = (
    "action-sha", "image-digest", "run-install", "setup-floating", "package-manager",
    "node-version-exact", "node-version-lock", "node-eol", "permissions-declared", "write-all",
    "unlisted-write", "unused-top-level-write", "persist-credentials", "token-env",
)
AUDITED_WRITE_FIELDS = ("workflow", "job", "permission", "steps", "reason")
EXCEPTION_FIELDS = ("file", "reference", "scope", "owner", "reason", "expires")
CREDENTIALS = "persist-credentials"

COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+")
EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
ARCHITECTURE = re.compile(r"\b(?:linux/)?(?:amd64|arm64|x64|x86_64|aarch64)\b")
TOKEN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:github\.token|secrets\.[A-Za-z0-9_]+)")
DOCKERFILE = re.compile(r"^(?:Dockerfile|Containerfile)(?:\..+)?$|\.(?:Dockerfile|Containerfile)$")
SKIPPED_DIRECTORIES = {".git", ".local", ".venv", ".worktrees", "node_modules", "__pycache__",
                       ".pytest_cache", "dist", "build"}


# -- reading the workflows ------------------------------------------------------------------------

def _plain(node):
    """A composed YAML node as plain Python, the way a safe load reads it."""
    if node is None:
        return None
    if isinstance(node, yaml.MappingNode):
        return {str(_plain(k)): _plain(v) for k, v in node.value}
    if isinstance(node, yaml.SequenceNode):
        return [_plain(item) for item in node.value]
    tag, value = node.tag, node.value
    if tag.endswith(":bool"):
        return value.lower() in ("true", "yes", "on", "y")
    if tag.endswith(":null"):
        return None
    if tag.endswith(":int"):
        try:
            return int(value, 0)
        except ValueError:
            return value
    return value


def _get(node, key):
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _mapping(node) -> dict:
    value = _plain(node)
    return value if isinstance(value, dict) else {}


def _trailing_comment(lines: list[str], node) -> str:
    """The comment after a scalar on its line: an action SHA's tag, an image digest's platform."""
    if not isinstance(node, yaml.ScalarNode):
        return ""
    rest = lines[node.end_mark.line][node.end_mark.column:] if node.end_mark.line < len(lines) else ""
    found = re.match(r"\s*#\s*(.*?)\s*$", rest)
    return found[1] if found else ""


def _reference(lines, node):
    if node is None:
        return None
    return {"reference": str(_plain(node)), "comment": _trailing_comment(lines, node)}


def _permissions(node, where, problems):
    if node is None:
        return None
    if isinstance(node, yaml.MappingNode):
        scopes = _mapping(node)
        for scope, level in scopes.items():
            if level not in ("read", "write", "none"):
                problems.append((where, f"permission {scope} must be read, write or none, "
                                        f"found {level!r}"))
        return {"kind": "map", "scopes": scopes}
    value = _plain(node)
    if value in ("read-all", "write-all"):
        return {"kind": value, "scopes": {}}
    problems.append((where, f"permissions must be a map, read-all or write-all, found {value!r}"))
    return {"kind": "unreadable", "scopes": {}}


def _image(lines, node, where, images):
    if node is None:
        return
    image = _get(node, "image") if isinstance(node, yaml.MappingNode) else node
    if image is not None:
        images.append({"where": where, **_reference(lines, image)})


def parse_workflow(name: str, text: str) -> dict:
    problems: list[tuple[str, str]] = []
    images: list[dict] = []
    workflow = {"name": name, "file": f".github/workflows/{name}", "problems": problems,
                "images": images, "permissions": None, "env": {}, "jobs": []}
    try:
        if any(getattr(event, "anchor", None) or isinstance(event, yaml.AliasEvent)
               for event in yaml.parse(text, Loader=yaml.SafeLoader)):
            problems.append((name, "anchors and aliases are not read by the CI checks; write each "
                                   "value out"))
            return workflow
        top = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        problems.append((name, f"YAML: {str(exc).splitlines()[0]}"))
        return workflow
    if not isinstance(top, yaml.MappingNode):
        problems.append((name, "a workflow must be a mapping"))
        return workflow
    lines = text.splitlines()
    workflow["permissions"] = _permissions(_get(top, "permissions"), f"{name} permissions", problems)
    workflow["env"] = _mapping(_get(top, "env"))
    jobs = _get(top, "jobs")
    if not isinstance(jobs, yaml.MappingNode) or not jobs.value:
        problems.append((name, "no jobs found"))
        return workflow
    for key, node in jobs.value:
        job_id = str(key.value)
        where = f"{name} {job_id}"
        if not isinstance(node, yaml.MappingNode):
            problems.append((where, "a job must be a mapping"))
            continue
        defaults = _mapping(_get(node, "defaults"))
        job = {
            "id": job_id,
            "permissions": _permissions(_get(node, "permissions"), f"{where} permissions", problems),
            "env": _mapping(_get(node, "env")),
            "uses": _reference(lines, _get(node, "uses")),
            "steps": [],
        }
        _image(lines, _get(node, "container"), where, images)
        services = _get(node, "services")
        if isinstance(services, yaml.MappingNode):
            for _, service in services.value:
                _image(lines, service, f"{where} services", images)
        steps = _get(node, "steps")
        if steps is not None and not isinstance(steps, yaml.SequenceNode):
            problems.append((where, "steps must be a list"))
        for index, step in enumerate(steps.value if isinstance(steps, yaml.SequenceNode) else []):
            if not isinstance(step, yaml.MappingNode):
                problems.append((where, f"step {index + 1} must be a mapping"))
                continue
            run = _plain(_get(step, "run"))
            job["steps"].append({
                "index": index,
                "name": _plain(_get(step, "name")),
                "uses": _reference(lines, _get(step, "uses")),
                "with": _mapping(_get(step, "with")),
                "env": _mapping(_get(step, "env")),
                "run": run if isinstance(run, str) else None,
                "working_directory": _plain(_get(step, "working-directory"))
                or (defaults.get("run") or {}).get("working-directory"),
            })
        workflow["jobs"].append(job)
    return workflow


def step_name(step) -> str:
    return step["name"] or f"step {step['index'] + 1}"


def step_label(workflow, job, step) -> str:
    return f'{workflow["name"]} {job["id"]} "{step_name(step)}"'


# -- loading the repository -----------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


NPM_INSTALL = re.compile(r"\bnpm\s+(?:ci|install|i)\b")


def install_directories(root: Path, parsed: list[dict]) -> list[str]:
    """Each app's ``ui/`` (the ``ui-bundles`` check runs ``npm ci`` there, the same glob), and
    every working directory a workflow step runs npm in."""
    directories = {p.parent.relative_to(root).as_posix() for p in root.glob("*/ui/package.json")}
    for workflow in parsed:
        for job in workflow["jobs"]:
            for step in job["steps"]:
                if step["run"] and NPM_INSTALL.search(step["run"]):
                    directories.add(os.path.normpath(step["working_directory"] or "."))
    return sorted(directories)


def _dockerfiles(root: Path) -> dict[str, str]:
    found = {}
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d not in SKIPPED_DIRECTORIES)
        for name in sorted(files):
            if DOCKERFILE.search(name):
                path = Path(directory) / name
                found[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return found


def load_repository(root: Path = ROOT) -> dict:
    workflows = {p.name: p.read_text(encoding="utf-8")
                 for p in sorted((root / ".github" / "workflows").iterdir())
                 if p.suffix in (".yml", ".yaml")}
    parsed = [parse_workflow(name, text) for name, text in workflows.items()]
    packages = {d: {"package_json": _read_json(root / d / "package.json"),
                    "lock": _read_json(root / d / "package-lock.json")}
                for d in install_directories(root, parsed)}
    nvmrc = root / ".nvmrc"
    policy = root / POLICY_FILE
    return {
        "workflows": workflows,
        "packages": packages,
        "node_version": nvmrc.read_text(encoding="utf-8") if nvmrc.exists() else "",
        "dockerfiles": _dockerfiles(root),
        "policy": policy.read_text(encoding="utf-8") if policy.exists() else None,
    }


# -- the policy file ------------------------------------------------------------------------------

def _local_date(value):
    """A TOML local date (``2026-11-10``: no time, no offset), or None."""
    return value if type(value) is dt.date else None


def read_policy(text, fail) -> dict:
    policy = {"end_of_life": {}, "audited_writes": [], "exceptions": []}
    if not isinstance(text, str):
        fail("policy-file", POLICY_FILE, f"{POLICY_FILE} is missing")
        return policy
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        fail("policy-file", POLICY_FILE, f"{POLICY_FILE} is not valid TOML: {exc}")
        return policy
    for key in data:
        if key not in ("node_end_of_life", "audited_write", "exception"):
            fail("policy-file", POLICY_FILE, f"unknown key {key}")
    table = data.get("node_end_of_life")
    if not isinstance(table, dict) or not table:
        fail("policy-file", POLICY_FILE,
             "node_end_of_life must list each supported Node line and its end-of-life date")
    else:
        for line, value in table.items():
            if not line.isdigit() or _local_date(value) is None:
                fail("policy-file", POLICY_FILE,
                     f'node_end_of_life "{line}" must be a major line with a date, found {value!r}')
            else:
                policy["end_of_life"][line] = value
    for key, into in (("audited_write", "audited_writes"), ("exception", "exceptions")):
        if key in data and not isinstance(data[key], list):
            fail("policy-file", POLICY_FILE, f"{key} must be an array of tables ([[{key}]])")
        elif isinstance(data.get(key), list):
            policy[into] = data[key]
    return policy


# -- step 2: pins ---------------------------------------------------------------------------------

def check_image(image, file, where, fail):
    if not IMAGE_DIGEST.search(image["reference"]) or not ARCHITECTURE.search(image["comment"]):
        fail("image-digest", where, "image must be pinned by digest with its architecture in a "
                                    f"comment, found {image['reference']}", file, image["reference"])


def check_action_reference(uses, file, where, fail):
    reference, comment = uses["reference"], uses["comment"]
    if reference.startswith("./"):
        return
    if reference.startswith("docker://"):
        check_image({"reference": reference[len("docker://"):], "comment": comment}, file, where,
                    fail)
        return
    ref = reference.split("@", 1)[1] if "@" in reference else ""
    if not COMMIT_SHA.fullmatch(ref) or not RELEASE_TAG.fullmatch(comment):
        shown = f"{reference} # {comment}" if comment else reference
        fail("action-sha", where, "uses must be a full commit SHA with its release tag as a "
                                  f"comment, found {shown}", file, reference)


def dockerfile_images(text: str) -> list[dict]:
    """A Dockerfile's pulled images: an ``ARG`` is read through its default, ``FROM scratch`` and
    ``FROM <earlier stage>`` pull nothing, and the platform is ``--platform`` or a comment on the
    line above."""
    lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    args: dict[str, str] = {}
    stages: set[str] = set()
    images = []
    for index, line in enumerate(lines):
        arg = re.match(r"\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?", line, re.I)
        if arg:
            args[arg[1]] = (arg[2] or "").strip("\"'")
        found = re.match(r"\s*FROM\s+(?:--platform=(\S+)\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
        if not found:
            continue
        written = found[2]
        reference = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: args.get(m[1], ""), written)
        pulls = reference != "scratch" and reference.lower() not in stages
        if found[3]:
            stages.add(found[3].lower())
        if not pulls:
            continue
        above = re.match(r"\s*#\s*(.*?)\s*$", lines[index - 1]) if index else None
        platform = found[1] if found[1] and "$" not in found[1] else ""
        images.append({"reference": reference, "written": written,
                       "comment": f"{platform} {above[1] if above else ''}".strip()})
    return images


def locked_binaries(packages: dict) -> set[str]:
    """The direct dependencies a lock installs, and the binaries they provide."""
    binaries: set[str] = set()
    for entry in packages.values():
        package_json, lock = entry.get("package_json") or {}, entry.get("lock") or {}
        direct = {**(package_json.get("dependencies") or {}),
                  **(package_json.get("devDependencies") or {})}
        for name in direct:
            locked = (lock.get("packages") or {}).get(f"node_modules/{name}")
            if not locked:
                continue
            binaries.add(name)
            bin_ = locked.get("bin")
            if isinstance(bin_, str):
                binaries.add(re.sub(r"^@[^/]+/", "", name))
            elif isinstance(bin_, dict):
                binaries.update(bin_)
    return binaries


#: The install flags that take a value, which is not a package.
VALUE_FLAGS = {"--python", "-p", "--index-url", "-i", "--extra-index-url", "--constraint", "-c",
               "--target", "--prefix", "--find-links", "-f", "--cache-dir"}
#: The one variable an install may name: test_core_lock.py holds it to core.lock's commit.
LOCKED_VARIABLES = {"$CORE_SPEC"}
PYTHON_PIN = re.compile(r"[A-Za-z0-9._\[\],-]+==\d+(?:\.\d+)*")


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=True)
    except ValueError:
        return text.split()


def _operands(words: list[str]) -> list[str]:
    found, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in VALUE_FLAGS
        else:
            found.append(word)
    return found


def floating_install(command: str, binaries: set[str]) -> str | None:
    """Why one shell line installs a tool with no exact version, or None."""
    text = command.strip()
    if re.search(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", text):
        return "pipes a download into a shell"
    found = re.search(r"(?:^|[;&|]\s*|\s)npm\s+(install|i|add)\b(.*)$", text)
    if found:
        targets = _operands(_words(found[2]))
        if not targets:
            return "npm install rewrites the lock; use npm ci"
        loose = [t for t in targets if not re.fullmatch(r"(?:@[^/@\s]+/)?[^@\s]+@\d+\.\d+\.\d+", t)]
        if loose:
            return f"npm install of {', '.join(loose)} names no exact version"
    found = re.search(r"(?:^|[;&|]\s*)npx\s+(.*)$", text)
    if found:
        words = _words(found[1])
        flag = next((i for i, w in enumerate(words) if w in ("--package", "-p")), None)
        name = words[flag + 1] if flag is not None and flag + 1 < len(words) else next(
            (w for w in words if not w.startswith("-")), None)
        if name and not re.search(r"@\d+\.\d+\.\d+$", name) and name not in binaries:
            return f"npx {name} runs a package that is neither in the lock nor pinned"
    found = re.search(r"(?:^|[;&|]\s*)(?:uv\s+pip|pip3?|python3?\s+-m\s+pip|pipx|uv\s+tool)\s+"
                      r"install\b(.*)$", text)
    if found:
        words = _words(found[1])
        if "-r" in words or "--requirement" in words:
            if "--require-hashes" not in words:
                return "pip install -r without --require-hashes"
        else:
            unread = [w for w in _operands(words) if "$" in w and w not in LOCKED_VARIABLES]
            if unread:
                return (f"install of {', '.join(unread)} names a version the check cannot read "
                        "(only $CORE_SPEC, which core.lock pins, may be a variable)")
            loose = [w for w in _operands(words)
                     if w not in LOCKED_VARIABLES and not PYTHON_PIN.fullmatch(w)]
            if loose:
                return f"install of {', '.join(loose)} names no exact version"
    found = re.search(r"(?:^|[;&|]\s*)(?:sudo\s+)?apt(?:-get)?\s+install\b(.*)$", text)
    if found:
        loose = [w for w in _operands(_words(found[1])) if not re.search(r"=\S+$", w)]
        if loose:
            return f"apt install of {', '.join(loose)} names no exact version"
    found = re.search(r"(?:^|[;&|]\s*)(?:go\s+install|cargo\s+install|gem\s+install)\b(.*)$", text)
    if found and not re.search(r"@v?\d+\.\d+\.\d+\b|--version\s+=?\d|-v\s+\d", found[1]):
        return "install names no exact version"
    return None


#: Each setup action's version inputs: a step pins one, a literal one is exact x.y.z.
SETUP_INPUTS = {
    "actions/setup-node": ("node-version", "node-version-file"),
    "actions/setup-python": ("python-version", "python-version-file"),
    "actions/setup-go": ("go-version", "go-version-file"),
    "actions/setup-java": ("java-version", "java-version-file"),
    "astral-sh/setup-uv": ("version", "version-file"),
    "pnpm/action-setup": ("version",),
    "oven-sh/setup-bun": ("bun-version", "bun-version-file"),
    "denoland/setup-deno": ("deno-version", "deno-version-file"),
}


def check_setup(workflow, job, step, fail):
    action = step["uses"]["reference"].split("@")[0]
    inputs = SETUP_INPUTS.get(action)
    if inputs is None:
        if not re.search(r"/setup-[a-z0-9-]+$", action):
            return
        inputs = ()
    where = step_label(workflow, job, step)
    reference = f"{job['id']} {step_name(step)}"

    def floating(message):
        fail("setup-floating", where, message, workflow["file"], reference)

    if step["with"].get("check-latest") in (True, "true"):
        floating(f"{action} must not set check-latest")
    present = [i for i in inputs if i in step["with"]]
    if not present:
        floating(f"{action} must pin {' or '.join(inputs) if inputs else 'an exact version'}")
        return
    for name in present:
        if not name.endswith("-file") and not EXACT_VERSION.fullmatch(str(step["with"][name])):
            floating(f"{action} {name} must be an exact x.y.z, found {step['with'][name]}")


UV_PYTHON_FLAG = re.compile(r"(?:^|\s)(?:--python|-p)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")


def check_uv_python(workflow, job, step, fail):
    """Every Python a uv command names, or ``UV_PYTHON`` sets, is an exact x.y.z. A path (an
    environment's own interpreter) names no version to float."""
    where = step_label(workflow, job, step)
    reference = f"{job['id']} {step_name(step)}"
    for line in (step["run"] or "").splitlines():
        if not re.search(r"(?:^|[;&|]\s*|\s)uvx?\s", " " + line.strip()):
            continue
        for value in UV_PYTHON_FLAG.findall(line):
            value = value.strip("\"'")
            if "/" in value or "$" in value:
                continue
            if not EXACT_VERSION.fullmatch(value):
                fail("setup-floating", where, f"uv --python must be an exact x.y.z, found {value}",
                     workflow["file"], reference)
    for env, label, ref in ((workflow["env"], workflow["name"], "workflow env UV_PYTHON"),
                            (job["env"], f"{workflow['name']} {job['id']}",
                             f"{job['id']} env UV_PYTHON"),
                            (step["env"], where, f"{reference} env UV_PYTHON")):
        value = env.get("UV_PYTHON")
        if value is not None and not EXACT_VERSION.fullmatch(str(value)):
            fail("setup-floating", label, f"UV_PYTHON must be an exact x.y.z, found {value}",
                 workflow["file"], ref)


def node_end_of_life(version, where, file, policy, today, fail):
    found = re.match(r"v?(\d+)", str(version))
    end = policy["end_of_life"].get(found[1]) if found else None
    if end is None:
        fail("node-eol", where, f"Node {version} is not a line in {POLICY_FILE}'s "
                                "node_end_of_life table", file, str(version))
    elif end <= today:
        fail("node-eol", where, f"Node {version} reached its end of life on {end}", file,
             str(version))


# The npm range reader: enough of npm's semver to test an exact Node version against the engines
# ranges package-lock.json records (``||``, space-joined comparators, ^, ~, >=, >, <=, <, =,
# partial versions and *). A form it does not read raises, and every caller reads that as unmet.
def _version(text: str):
    found = re.fullmatch(r"v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", text)
    if not found:
        raise ValueError(f"unsupported version {text}")
    part = lambda v: None if v in (None, "x", "*") else int(v)  # noqa: E731
    return (int(found[1]), part(found[2]), part(found[3]))


def _expand(operator, version):
    major, minor, patch = version
    low = (major, minor or 0, patch or 0)
    if operator == "^":
        if major == 0:
            raise ValueError("unsupported ^0 range")
        return [(">=", low), ("<", (major + 1, 0, 0))]
    if operator == "~":
        return [(">=", low), ("<", (major + 1, 0, 0) if minor is None else (major, minor + 1, 0))]
    if operator == "=":
        if minor is None:
            return [(">=", low), ("<", (major + 1, 0, 0))]
        if patch is None:
            return [(">=", low), ("<", (major, minor + 1, 0))]
        return [(">=", low), ("<=", low)]
    if operator == "<=" and (minor is None or patch is None):
        return [("<", (major + 1, 0, 0) if minor is None else (major, minor + 1, 0))]
    if operator == ">" and (minor is None or patch is None):
        return [(">=", (major + 1, 0, 0) if minor is None else (major, minor + 1, 0))]
    return [(operator, low)]


def _comparator_sets(range_: str):
    if not isinstance(range_, str) or not range_.strip():
        raise ValueError("empty range")
    sets = []
    for part in range_.split("||"):
        text = part.strip()
        if text in ("", "*", "x"):
            sets.append([])
            continue
        comparators, index = [], 0
        pattern = re.compile(r"(\^|~|>=|<=|>|<|=)?\s*(v?\d+(?:\.(?:\d+|x|\*)){0,2})(?=\s|$)")
        while index < len(text):
            if text[index] == " ":
                index += 1
                continue
            found = pattern.match(text, index)
            if not found:
                raise ValueError(f"unsupported range {range_}")
            comparators += _expand(found[1] or "=", _version(found[2]))
            index = found.end()
        sets.append(comparators)
    return sets


def satisfies(version: str, range_: str) -> bool:
    target = _version(version)
    if None in target:
        raise ValueError(f"not an exact version {version}")
    tests = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
             "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}
    return any(all(tests[op](target, bound) for op, bound in comparators)
               for comparators in _comparator_sets(range_))


def _locked_engines(lock) -> list[tuple[str, str]]:
    """Every locked package's engines.node; an optional package is a platform binary npm skips
    when it does not fit, so it does not set the minimum."""
    found = []
    for key, entry in ((lock or {}).get("packages") or {}).items():
        if key == "" or entry.get("optional"):
            continue
        range_ = (entry.get("engines") or {}).get("node")
        if isinstance(range_, str):
            found.append((f"{key.rsplit('node_modules/', 1)[-1]}@{entry.get('version')}", range_))
    return found


def _unmet(engines, version) -> list[str]:
    unmet = []
    for name, range_ in engines:
        try:
            held = satisfies(version, range_)
        except ValueError:
            held = False
        if not held:
            unmet.append(f"{name} requires {range_}")
    return unmet


def check_packages(repository, fail) -> str | None:
    version = str(repository.get("node_version") or "").strip()
    exact = bool(EXACT_VERSION.fullmatch(version))
    if not exact:
        fail("node-version-exact", ".nvmrc", f"must be an exact x.y.z release, found {version!r}",
             ".nvmrc", version)
    for directory, entry in (repository.get("packages") or {}).items():
        file = str(PurePosixPath(directory) / "package.json")
        manager = (entry.get("package_json") or {}).get("packageManager")
        if not isinstance(manager, str) or not re.fullmatch(r"npm@\d+\.\d+\.\d+", manager):
            fail("package-manager", file, f"packageManager must be npm@<exact>, found {manager}",
                 file, str(manager))
        if exact:
            below = _unmet(_locked_engines(entry.get("lock")), version)
            if below:
                fail("node-version-lock", ".nvmrc", f"{version} is below {directory}'s lock "
                                                    f"minimum: {'; '.join(below)}", ".nvmrc", version)
    return version if exact else None


# -- step 3: least privilege ----------------------------------------------------------------------

def _write_scopes(permissions) -> list[str]:
    if not permissions or permissions["kind"] != "map":
        return []
    return sorted(s for s, level in permissions["scopes"].items() if level == "write")


def _keeps_credentials(step) -> bool:
    uses = (step["uses"] or {}).get("reference", "")
    return uses.startswith("actions/checkout@") and step["with"].get(CREDENTIALS) not in (False,
                                                                                        "false")


def _token_env(env, where, file, reference, fail):
    for name, value in env.items():
        if TOKEN_EXPRESSION.search(str(value)):
            fail("token-env", where, f"{name} passes a token to every step; set it on the steps "
                                     "that call the API", file, f"{reference} env {name}")


def _non_empty(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def check_audited_writes(parsed, policy, fail) -> list[dict]:
    """The audited write steps that hold; every other one is a violation, and lists nothing, so
    the grant it was meant to cover fails too."""
    valid_entries = []
    for index, entry in enumerate(policy["audited_writes"], 1):
        where = f"{POLICY_FILE} audited_write {index}"

        def broken(message, where=where, index=index):
            fail("audited-write-entry", where, message, POLICY_FILE, f"audited_write {index}")

        if not isinstance(entry, dict):
            broken("must be a table")
            continue
        missing = [f for f in AUDITED_WRITE_FIELDS
                   if (f == "steps" and not (isinstance(entry.get("steps"), list) and entry["steps"]
                                             and all(map(_non_empty, entry["steps"]))))
                   or (f != "steps" and not _non_empty(entry.get(f)))]
        if missing:
            broken(f"missing {', '.join(missing)}")
            continue
        unknown = [k for k in entry if k not in AUDITED_WRITE_FIELDS]
        if unknown:
            broken(f"unknown field {', '.join(unknown)}")
            continue
        workflow = next((w for w in parsed if w["name"] == entry["workflow"]), None)
        job = next((j for j in (workflow or {}).get("jobs", []) if j["id"] == entry["job"]), None)
        if job is None:
            broken(f"{entry['workflow']} {entry['job']} matches no job")
            continue
        effective = job["permissions"] or workflow["permissions"]
        if entry["permission"] == CREDENTIALS:
            holds = any(map(_keeps_credentials, job["steps"]))
        else:
            holds = bool(re.fullmatch(r"[a-z-]+: write", entry["permission"])) and \
                entry["permission"].split(":")[0] in _write_scopes(effective)
        if not holds:
            broken(f"{entry['workflow']} {entry['job']} does not hold {entry['permission']}")
            continue
        names = {step_name(s) for s in job["steps"]}
        absent = [n for n in entry["steps"] if n not in names]
        if absent:
            broken(f"no step named {', '.join(map(repr, absent))} in {entry['workflow']} "
                   f"{entry['job']}")
            continue
        if entry["permission"] == CREDENTIALS:
            stray = [s for s in job["steps"]
                     if _keeps_credentials(s) and step_name(s) not in entry["steps"]]
            if stray:
                broken(f"checkout {step_name(stray[0])!r} keeps its credentials but is not one of "
                       "the listed steps")
                continue
        valid_entries.append(entry)
    return valid_entries


def check_privilege(parsed, entries, fail):
    def listed(workflow, job, permission):
        return any(e["workflow"] == workflow["name"] and e["job"] == job["id"]
                   and e["permission"] == permission for e in entries)

    for workflow in parsed:
        top = workflow["permissions"]
        if top and top["kind"] == "write-all":
            fail("write-all", f"{workflow['name']} permissions", "write-all grants every scope",
                 workflow["file"], "permissions")
        _token_env(workflow["env"], workflow["name"], workflow["file"], "workflow", fail)
        inherited: set[str] = set()
        for job in workflow["jobs"]:
            where = f"{workflow['name']} {job['id']}"
            _token_env(job["env"], where, workflow["file"], job["id"], fail)
            if job["permissions"] is None and top is None:
                fail("permissions-declared", where, "job declares no permissions, and the "
                                                    "workflow has none at the top",
                     workflow["file"], job["id"])
            if job["permissions"] and job["permissions"]["kind"] == "write-all":
                fail("write-all", where, "write-all grants every scope", workflow["file"],
                     job["id"])
            if job["permissions"] is None:
                inherited.update(_write_scopes(top))
            for scope in _write_scopes(job["permissions"] or top):
                permission = f"{scope}: write"
                if not listed(workflow, job, permission):
                    origin = (" (inherited from the top of the workflow)"
                              if job["permissions"] is None else "")
                    fail("unlisted-write", where, f"{permission}{origin} is not an audited write "
                                                  f"step in {POLICY_FILE}", workflow["file"],
                         f"{job['id']} {permission}")
            for step in job["steps"]:
                if _keeps_credentials(step) and not listed(workflow, job, CREDENTIALS):
                    fail("persist-credentials", step_label(workflow, job, step),
                         "checkout keeps its credentials; set persist-credentials: false or list "
                         "it as an audited write step", workflow["file"],
                         f"{job['id']} {step_name(step)}")
        for scope in _write_scopes(top):
            if scope not in inherited:
                fail("unused-top-level-write", f"{workflow['name']} permissions",
                     f"{scope}: write at the top reaches no job today, and would reach every job "
                     "added later; grant it on the job", workflow["file"], f"permissions {scope}")


# -- step 4: exceptions ---------------------------------------------------------------------------

def apply_exceptions(violations, exceptions, today) -> list[dict]:
    waived: set[int] = set()
    own = []
    for index, entry in enumerate(exceptions, 1):
        where = f"{POLICY_FILE} exception {index}"

        def broken(message, where=where, index=index):
            own.append({"rule": "exception", "where": where, "message": message,
                        "file": POLICY_FILE, "reference": f"exception {index}"})

        if not isinstance(entry, dict):
            broken("must be a table")
            continue
        missing = [f for f in EXCEPTION_FIELDS
                   if (f == "expires" and "expires" not in entry)
                   or (f != "expires" and not _non_empty(entry.get(f)))]
        if missing:
            broken(f"missing {', '.join(missing)}")
            continue
        unknown = [k for k in entry if k not in EXCEPTION_FIELDS]
        if unknown:
            broken(f"unknown field {', '.join(unknown)}")
            continue
        expires = _local_date(entry["expires"])
        if expires is None:
            broken("expires must be a date, such as 2026-11-01")
            continue
        if entry["scope"] not in WAIVABLE_RULES:
            broken(f"scope {entry['scope']} is not a rule an exception may waive")
            continue
        if expires < today:
            broken(f"expired on {expires}; renew it with a new expiry and a reason, or fix the "
                   "reference")
            continue
        if (expires - today).days > EXCEPTION_MAX_DAYS:
            broken(f"expires {expires}, more than {EXCEPTION_MAX_DAYS} days out")
            continue
        matches = [i for i, v in enumerate(violations)
                   if v["rule"] == entry["scope"] and v["file"] == entry["file"]
                   and v["reference"] == entry["reference"]]
        if not matches:
            broken(f"{entry['scope']} on {entry['file']} {entry['reference']!r} matches no "
                   "violation; remove it")
            continue
        waived.update(matches)
    return [v for i, v in enumerate(violations) if i not in waived] + own


# -- the check ------------------------------------------------------------------------------------

def check(repository: dict, today: dt.date | None = None) -> list[dict]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    violations: list[dict] = []

    def fail(rule, where, message, file=None, reference=None):
        violations.append({"rule": rule, "where": where, "message": message,
                           "file": file or where, "reference": reference or where})

    policy = read_policy(repository.get("policy"), fail)
    parsed = [parse_workflow(n, t) for n, t in (repository.get("workflows") or {}).items()]
    binaries = locked_binaries(repository.get("packages") or {})
    for workflow in parsed:
        for where, message in workflow["problems"]:
            fail("workflow-shape", where, message, workflow["file"], where)
        for image in workflow["images"]:
            check_image(image, workflow["file"], image["where"], fail)
        for job in workflow["jobs"]:
            if job["uses"]:
                check_action_reference(job["uses"], workflow["file"],
                                       f"{workflow['name']} {job['id']}", fail)
            for step in job["steps"]:
                where = step_label(workflow, job, step)
                if step["uses"]:
                    check_action_reference(step["uses"], workflow["file"], where, fail)
                    check_setup(workflow, job, step, fail)
                    node = step["with"].get("node-version")
                    if step["uses"]["reference"].startswith("actions/setup-node@") and node is not None:
                        node_end_of_life(node, where, workflow["file"], policy, today, fail)
                check_uv_python(workflow, job, step, fail)
                for command in (step["run"] or "").splitlines():
                    reason = floating_install(command, binaries)
                    if reason:
                        fail("run-install", where, f"{reason}: {command.strip()}",
                             workflow["file"], f"{job['id']} {step_name(step)}")
    for file, text in (repository.get("dockerfiles") or {}).items():
        for image in dockerfile_images(text):
            check_image(image, file, f"{file} FROM {image['written']}", fail)
    version = check_packages(repository, fail)
    if version:
        node_end_of_life(version, ".nvmrc", ".nvmrc", policy, today, fail)
    check_privilege(parsed, check_audited_writes(parsed, policy, fail), fail)
    return apply_exceptions(violations, policy["exceptions"], today)


def _shown(violations) -> str:
    return "\n".join(f"[{v['rule']}] {v['where']}: {v['message']}" for v in violations)


# -- the repository -------------------------------------------------------------------------------

def test_the_committed_workflows_packages_and_policy_satisfy_the_supply_chain_check():
    violations = check(load_repository())
    assert violations == [], _shown(violations)


def test_the_check_reads_every_workflow_action_checkout_and_package_ci_has():
    # The vacuity floor: a reader that saw less than the tree holds today could pass on nothing.
    repository = load_repository()
    parsed = [parse_workflow(n, t) for n, t in repository["workflows"].items()]
    steps = [s for w in parsed for j in w["jobs"] for s in j["steps"]]
    assert {w["name"] for w in parsed} >= {"ci.yml", "core-lock-bump.yml", "core-main-canary.yml"}
    assert sum(1 for s in steps if s["uses"]) >= 18
    assert sum(1 for s in steps if (s["uses"] or {}).get("reference", "").startswith(
        "actions/checkout@")) >= 6
    assert set(repository["packages"]) >= {"growth/ui", "minutes/ui"}
    assert all(e["package_json"] and e["lock"] for e in repository["packages"].values())


# -- the planted baseline -------------------------------------------------------------------------

TODAY = dt.date(2026, 10, 17)
SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DIGEST = "sha256:" + "a" * 64
CHECKOUT = f"actions/checkout@{SHA} # v7.0.1"
SETUP_NODE = "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0"
SETUP_UV = "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78 # v7.6.0"
EOL = '[node_end_of_life]\n"20" = 2026-04-30\n"22" = 2027-04-30\n"24" = 2028-04-30\n'
ISSUES_ENTRY = "\n".join([
    "[[audited_write]]",
    'workflow = "example.yml"',
    'job = "example"',
    'permission = "issues: write"',
    'steps = ["File the issue"]',
    'reason = "Files the tracking issue."',
])
CREDENTIALS_ENTRY = "\n".join([
    "[[audited_write]]",
    'workflow = "example.yml"',
    'job = "example"',
    'permission = "persist-credentials"',
    'steps = ["Check out repository"]',
    'reason = "Pushes a generated commit."',
])
ISSUE_STEP = "\n".join([
    "      - name: File the issue",
    "        env:",
    "          GH_TOKEN: ${{ github.token }}",
    "        run: gh issue create --title example",
])
ISSUES_JOB = "    permissions:\n      contents: read\n      issues: write"


def workflow(*, top="permissions:\n  contents: read", job_keys="", checkout=CHECKOUT,
             checkout_with="        with:\n          persist-credentials: false",
             node_with="          node-version-file: .nvmrc",
             uv_with='          version: "0.13.1"',
             install='uv pip install --python "$PWD/.venv/bin/python" "$CORE_SPEC" pytest==9.1.1',
             steps="") -> str:
    """One job shaped like this repository's: a credential-free checkout, Node from .nvmrc, an
    exact uv and Python, core from core.lock, an exact pytest, and the locked vite."""
    lines = [
        "name: Example", "on:", "  push:", top, "jobs:", "  example:",
        "    runs-on: ubuntu-24.04", job_keys, "    steps:",
        "      - name: Check out repository", f"        uses: {checkout}", checkout_with,
        "      - name: Set up Node.js", f"        uses: {SETUP_NODE}", "        with:", node_with,
        "      - name: Install uv", f"        uses: {SETUP_UV}", "        with:", uv_with,
        "      - name: Install core + pytest", "        run: |",
        "          uv venv --python 3.12.15 .venv", f"          {install}",
        "      - name: Build the UI", "        working-directory: example/ui",
        "        run: npm ci && npx vite build",
        steps,
    ]
    return "\n".join(line for line in lines if line != "")


def lock(engines=None) -> dict:
    engines = {"vite": "^20.19.0 || >=22.12.0"} if engines is None else engines
    packages = {"": {"name": "example-ui"}}
    for name, range_ in engines.items():
        packages[f"node_modules/{name}"] = {"version": "1.0.0", "engines": {"node": range_}}
    packages.setdefault("node_modules/vite", {"version": "8.3.4"})["bin"] = {"vite": "bin/vite.js"}
    packages["node_modules/fsevents"] = {"version": "2.3.3", "optional": True,
                                         "engines": {"node": ">=99"}}
    return {"lockfileVersion": 3, "packages": packages}


PACKAGE_JSON = {"packageManager": "npm@11.19.0", "devDependencies": {"vite": "^8.3.4"}}


def repository(*, text=None, policy=EOL, packages=None, **rest) -> dict:
    return {
        "workflows": {"example.yml": workflow() if text is None else text},
        "packages": packages if packages is not None else {
            "example/ui": {"package_json": PACKAGE_JSON, "lock": lock()}},
        "node_version": "24.21.0\n",
        "dockerfiles": {},
        "policy": policy,
        **rest,
    }


def _rules(violations) -> list[str]:
    return sorted({v["rule"] for v in violations})


def test_the_planted_baseline_passes_so_each_row_below_fails_on_its_own_plant():
    assert check(repository(), TODAY) == []
    with_issue = workflow(top="permissions:\n  contents: read\n  issues: none", steps=ISSUE_STEP)
    assert check(repository(text=with_issue), TODAY) == []


def _docker(text):
    return {"dockerfiles": {"deploy/Dockerfile": text}}


def _ui(package_json=PACKAGE_JSON, engines=None):
    return {"example/ui": {"package_json": package_json, "lock": lock(engines)}}


UNPINNED_INSTALL = "      - name: Install the CLI\n        run: pip install pyinstaller"

# Step 2: every pin rule, one plant per violation.
PIN_PLANTS = [
    ("action-sha", "an action by tag", {"text": workflow(checkout="actions/checkout@v7")}),
    ("action-sha", "an action by SHA without its release tag",
     {"text": workflow(checkout=f"actions/checkout@{SHA}")}),
    ("action-sha", "an action by a short SHA",
     {"text": workflow(checkout=f"actions/checkout@{SHA[:7]} # v7.0.1")}),
    ("image-digest", "a docker:// reference by tag",
     {"text": workflow(steps="      - name: Lint\n        uses: docker://alpine:3.20 # linux/amd64")}),
    ("image-digest", "a workflow container without a digest",
     {"text": workflow(job_keys="    container:\n      image: python:3.12-slim # linux/amd64")}),
    ("image-digest", "a service image without a digest",
     {"text": workflow(job_keys="    services:\n      db:\n        image: postgres:17 # linux/amd64")}),
    ("image-digest", "a Dockerfile image without a digest", _docker("FROM python:3.12-slim\n")),
    ("image-digest", "an image named through an ARG whose default is a bare tag",
     _docker("ARG BASE=python:3.13-slim\nFROM ${BASE}\n")),
    ("image-digest", "an index digest with no platform", _docker(f"FROM python@{DIGEST}\n")),
    ("run-install", "a run: step installing a tool with no version",
     {"text": workflow(steps=UNPINNED_INSTALL)}),
    ("run-install", "pytest installed beside core with no version",
     {"text": workflow(install='uv pip install --python "$PWD/.venv/bin/python" "$CORE_SPEC" pytest')}),
    ("run-install", "a version range rather than an exact pin",
     {"text": workflow(install='uv pip install "$CORE_SPEC" "pytest>=9"')}),
    ("run-install", "an install of a variable the check cannot read",
     {"text": workflow(steps="      - name: Install the claims' deps\n        run: uv pip install $deps")}),
    ("run-install", "a requirements file without hashes",
     {"text": workflow(install="uv pip install -r requirements.txt")}),
    ("run-install", "a global npm install with no version",
     {"text": workflow(steps="      - name: Install the CLI\n        run: npm install -g serve")}),
    ("run-install", "npx of a package outside the lock",
     {"text": workflow(steps="      - name: Serve\n        run: npx serve dist")}),
    ("run-install", "a download piped into a shell",
     {"text": workflow(steps="      - name: Get a tool\n        run: curl -fsSL https://example.com/i.sh | sh")}),
    ("setup-floating", "a floating setup-node",
     {"text": workflow(node_with="          node-version: 24")}),
    ("setup-floating", "setup-node with check-latest",
     {"text": workflow(node_with="          node-version-file: .nvmrc\n          check-latest: true")}),
    ("setup-floating", "a floating setup-uv",
     {"text": workflow(uv_with="          enable-cache: true")}),
    ("setup-floating", "a setup-uv version that is not exact",
     {"text": workflow(uv_with='          version: "0.13"')}),
    ("setup-floating", "a uv --python that is not exact",
     {"text": workflow(install='uv run --no-project --python 3.12 python scripts/core_lock.py export')}),
    ("setup-floating", "a uv -p that is not exact",
     {"text": workflow(install="uv venv -p 3 .venv")}),
    ("setup-floating", "a UV_PYTHON that is not exact",
     {"text": workflow(top="permissions:\n  contents: read\nenv:\n  UV_PYTHON: '3.12'")}),
    ("package-manager", "a missing packageManager",
     {"packages": _ui({"devDependencies": {"vite": "^8.3.4"}})}),
    ("package-manager", "a packageManager that is not an exact npm",
     {"packages": _ui({**PACKAGE_JSON, "packageManager": "npm@11"})}),
    ("package-manager", "a directory CI runs npm in that has no package.json",
     {"packages": {**_ui(), "tools/example": {"package_json": None, "lock": None}}}),
    ("node-version-exact", "a floating .nvmrc", {"node_version": "24\n"}),
    ("node-version-exact", "an .nvmrc that names an alias", {"node_version": "lts/*\n"}),
    ("node-eol", "a Node line past its end of life", {"node_version": "20.19.5\n"}),
    ("node-eol", "a Node line the end-of-life table does not list", {"node_version": "25.0.0\n"}),
    ("node-eol", "a setup-node literal on a line past its end of life",
     {"text": workflow(node_with="          node-version: 20.19.5")}),
    ("node-version-lock", "Node below its lock's minimum", {"node_version": "22.11.0\n"}),
]

# Step 3: least privilege.
PRIVILEGE_PLANTS = [
    ("write-all", "a write-all job", {"text": workflow(job_keys="    permissions: write-all")}),
    ("write-all", "a write-all workflow", {"text": workflow(top="permissions: write-all")}),
    ("unlisted-write", "a contents: write the list does not name",
     {"text": workflow(job_keys="    permissions:\n      contents: write")}),
    ("unlisted-write", "a contents: write inherited from the top of a workflow",
     {"text": workflow(top="permissions:\n  contents: write")}),
    ("unlisted-write", "an extra permission added to a listed job",
     {"text": workflow(job_keys=f"{ISSUES_JOB}\n      pull-requests: write", steps=ISSUE_STEP),
      "policy": f"{EOL}\n{ISSUES_ENTRY}\n"}),
    ("unused-top-level-write", "a top-level write that no job inherits",
     {"text": workflow(top="permissions:\n  contents: read\n  issues: write",
                       job_keys="    permissions:\n      contents: read")}),
    ("permissions-declared", "a job without permissions in a workflow with no top-level block",
     {"text": workflow(top="")}),
    ("persist-credentials", "a checkout without persist-credentials: false",
     {"text": workflow(checkout_with="")}),
    ("persist-credentials", "a checkout that keeps its credentials on purpose, unlisted",
     {"text": workflow(checkout_with="        with:\n          persist-credentials: true")}),
    ("token-env", "a workflow-level env that passes github.token",
     {"text": workflow(top="permissions:\n  contents: read\nenv:\n  GITHUB_TOKEN: ${{ github.token }}")}),
    ("token-env", "a job-level env that passes github.token",
     {"text": workflow(job_keys="    env:\n      GH_TOKEN: ${{ github.token }}")}),
    ("token-env", "a job-level env that passes a secret",
     {"text": workflow(job_keys="    env:\n      API_KEY: ${{ secrets.EXAMPLE_KEY }}")}),
    ("workflow-shape", "a scalar permissions value the rules cannot judge",
     {"text": workflow(top="permissions: read")}),
    ("workflow-shape", "an anchor the rules cannot follow",
     {"text": workflow(top="permissions: &p\n  contents: read")}),
    ("workflow-shape", "a workflow that is not YAML", {"text": "jobs: [unclosed\n"}),
    ("audited-write-entry", "a list entry that matches no grant",
     {"policy": f"{EOL}\n{ISSUES_ENTRY}\n"}),
    ("audited-write-entry", "a list entry for a job that does not exist",
     {"policy": f"{EOL}\n{ISSUES_ENTRY.replace('job = \"example\"', 'job = \"other\"')}\n"}),
]


def _listed_issues():
    return {"text": workflow(job_keys=ISSUES_JOB, steps=ISSUE_STEP)}


def _without_line(entry, field):
    return "\n".join(line for line in entry.split("\n") if not line.startswith(f"{field} ="))


PRIVILEGE_PLANTS += [
    ("audited-write-entry", "a list entry naming a step the job does not have",
     {**_listed_issues(), "policy": f"{EOL}\n{ISSUES_ENTRY.replace('File the issue', 'Close it')}\n"},
     ["unlisted-write"]),
    ("audited-write-entry", "a list entry with an unknown field",
     {**_listed_issues(), "policy": f'{EOL}\n{ISSUES_ENTRY}\nexpires = 2026-11-01\n'},
     ["unlisted-write"]),
    *[("audited-write-entry", f"a list entry missing {field}",
       {**_listed_issues(), "policy": f"{EOL}\n{_without_line(ISSUES_ENTRY, field)}\n"},
       ["unlisted-write"]) for field in AUDITED_WRITE_FIELDS],
    ("policy-file", "a missing policy file", {"policy": None}, ["node-eol"]),
    ("policy-file", "a policy file that is not TOML", {"policy": f"{EOL}\n[[audited_write]\n"},
     ["node-eol"]),
    ("policy-file", "a policy file with an unknown key", {"policy": f"{EOL}\nwaiver = true\n"}),
    ("policy-file", "an end-of-life entry that is not a date",
     {"policy": '[node_end_of_life]\n"24" = "2028-04-30"\n'}, ["node-eol"]),
]


def _exception(fields: dict) -> str:
    def value(v):
        return v if re.fullmatch(r"\d{4}-\d\d-\d\d", v) else json.dumps(v)
    return "[[exception]]\n" + "\n".join(f"{k} = {value(v)}" for k, v in fields.items())


LIVE_EXCEPTION = {
    "file": ".github/workflows/example.yml",
    "reference": "example Install the CLI",
    "scope": "run-install",
    "owner": "maintainers",
    "reason": "The tool publishes no versioned build yet.",
    "expires": "2026-11-10",
}


def _with_exception(fields, extra=UNPINNED_INSTALL):
    return {"text": workflow(steps=extra), "policy": f"{EOL}\n{_exception(fields)}\n"}


# Step 4: exceptions. A broken exception waives nothing, so the violation it meant to cover is
# reported too.
EXCEPTION_PLANTS = [
    ("exception", "an exception past its expiry",
     _with_exception({**LIVE_EXCEPTION, "expires": "2026-10-16"}), ["run-install"]),
    ("exception", "an exception more than 30 days out",
     _with_exception({**LIVE_EXCEPTION, "expires": "2026-11-17"}), ["run-install"]),
    ("exception", "an exception that matches nothing", _with_exception(LIVE_EXCEPTION, "")),
    ("exception", "an exception for another file",
     _with_exception({**LIVE_EXCEPTION, "file": ".github/workflows/other.yml"}), ["run-install"]),
    ("exception", "an exception whose expiry is not a date",
     _with_exception({**LIVE_EXCEPTION, "expires": "soon"}), ["run-install"]),
    ("exception", "an exception for a rule that cannot be waived",
     _with_exception({**LIVE_EXCEPTION, "scope": "exception"}), ["run-install"]),
    ("exception", "an exception with an unknown field",
     _with_exception({**LIVE_EXCEPTION, "ticket": "none"}), ["run-install"]),
    *[("exception", f"an exception missing {field}",
       _with_exception({k: v for k, v in LIVE_EXCEPTION.items() if k != field}), ["run-install"])
      for field in EXCEPTION_FIELDS],
]

PLANTS = [(p + ([],))[:4] if len(p) == 3 else p
          for p in PIN_PLANTS + PRIVILEGE_PLANTS + EXCEPTION_PLANTS]


@pytest.mark.parametrize("rule, why, overrides, also", PLANTS, ids=[p[1] for p in PLANTS])
def test_the_supply_chain_check_fails_on_a_planted(rule, why, overrides, also):
    violations = check(repository(**overrides), TODAY)
    assert _rules(violations) == sorted({rule, *also}), _shown(violations)


PASSES = [
    ("a step-level env that passes github.token", {"text": workflow(steps=ISSUE_STEP)}),
    ("a listed write permission",
     {**_listed_issues(), "policy": f"{EOL}\n{ISSUES_ENTRY}\n"}),
    ("a checkout that keeps its credentials when it is listed as an audited write step",
     {"text": workflow(checkout_with=""), "policy": f"{EOL}\n{CREDENTIALS_ENTRY}\n"}),
    ("a live exception, which waives the reference it names", _with_exception(LIVE_EXCEPTION)),
    ("an exception exactly 30 days out",
     _with_exception({**LIVE_EXCEPTION, "expires": "2026-11-16"})),
    ("an exception that expires today", _with_exception({**LIVE_EXCEPTION, "expires": "2026-10-17"})),
    ("images pinned by digest for one platform",
     {"text": workflow(job_keys=f"    container:\n      image: python@{DIGEST} # linux/amd64"),
      **_docker(f"ARG BASE=python@{DIGEST}\nFROM --platform=linux/amd64 ${{BASE}} AS build\n"
                f"FROM build\n# linux/arm64\nFROM node@{DIGEST}\nFROM scratch\n")}),
    ("a local action", {"text": workflow(steps="      - name: Local\n        uses: ./.github/actions/x")}),
    ("installs that name exact versions",
     {"text": workflow(steps="      - name: Install the CLI\n        run: |\n"
                             "          pip install pyinstaller==6.11.1\n"
                             "          npx --yes serve@14.2.4 --version\n"
                             "          uv pip install -r requirements.txt --require-hashes")}),
    ("a uv --python that names an environment's interpreter",
     {"text": workflow(install='uv pip install --python "$PWD/.venv/bin/python" "$CORE_SPEC"')}),
    ("a setup-node pinned to an exact supported release",
     {"text": workflow(node_with="          node-version: 24.21.0")}),
    ("an optional platform package whose engines the pinned Node misses", {}),
]


@pytest.mark.parametrize("why, overrides", PASSES, ids=[p[0] for p in PASSES])
def test_the_supply_chain_check_passes(why, overrides):
    violations = check(repository(**overrides), TODAY)
    assert violations == [], _shown(violations)


def test_an_exception_waives_only_the_reference_it_names():
    two = UNPINNED_INSTALL + "\n      - name: Install another\n        run: pip install twine"
    violations = check(repository(**_with_exception(LIVE_EXCEPTION, two)), TODAY)
    assert [(v["rule"], v["reference"]) for v in violations] == [
        ("run-install", "example Install another")]


def test_a_second_package_json_is_held_to_the_same_pins():
    packages = {**_ui(), "other/ui": {"package_json": {"devDependencies": {}}, "lock": lock()}}
    violations = check(repository(packages=packages), TODAY)
    assert [(v["rule"], v["file"]) for v in violations] == [("package-manager",
                                                             "other/ui/package.json")]


def test_a_dockerfile_image_is_judged_through_its_arg_default_and_its_platform():
    images = dockerfile_images("ARG BASE=python:3.13-slim\nFROM ${BASE} AS build\nFROM build\n"
                               "FROM scratch\n")
    assert [i["reference"] for i in images] == ["python:3.13-slim"]
    assert dockerfile_images(f"FROM --platform=linux/arm64 node@{DIGEST}\n")[0]["comment"] == \
        "linux/arm64"


def test_the_loader_finds_a_dockerfile_and_the_package_a_workflow_installs_from(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "example.yml").write_text(workflow(), encoding="utf-8")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "Dockerfile").write_text("FROM ignored\n", encoding="utf-8")
    app_ui = tmp_path / "app" / "ui"
    app_ui.mkdir(parents=True)
    (app_ui / "package.json").write_text(json.dumps(PACKAGE_JSON), encoding="utf-8")
    loaded = load_repository(tmp_path)
    assert list(loaded["dockerfiles"]) == ["deploy/Dockerfile"]
    assert sorted(loaded["packages"]) == ["app/ui", "example/ui"]
    assert loaded["packages"]["example/ui"]["package_json"] is None
    assert loaded["policy"] is None
    # run-install: the workflow's `npx vite` has no lock in example/ui to install it from.
    assert _rules(check(loaded, TODAY)) == ["image-digest", "node-version-exact", "package-manager",
                                            "policy-file", "run-install"]


def test_the_policy_file_names_today_s_one_write_and_dates_every_exception_it_holds():
    policy = tomllib.loads((ROOT / POLICY_FILE).read_text(encoding="utf-8"))
    writes = [(e["workflow"], e["job"], e["permission"]) for e in policy.get("audited_write", [])]
    assert writes == [("core-main-canary.yml", "report", "issues: write")]
    for entry in policy.get("exception", []):
        assert type(entry["expires"]) is dt.date
