"""A neutral git for this repository's tests: none of the machine's git configuration, and none of
its credential helpers.

A test on a developer's machine that runs git runs with that machine's git configuration, and on
a Mac the file Apple's git bundles names ``osxkeychain``: git hands every credential it signs in
with to the owner's real keychain, and asks the keychain for the ones it needs. On 2026-09-28 a
git-sync test's own ``git clone`` of a URL with a token in it (a fixture, standing in for an
old clone) signed in, then told ``git-credential-osxkeychain`` to keep the token, and the helper
waited on the owner's keychain for ten minutes. The scratch-home fixture that test used kept
core's git off the owner's helpers, but not a git the test ran itself.

Two parts, both installed by the repository's ``conftest.py`` before anything is collected:

* :func:`neutral_env` is what every test's git inherits: ``GIT_CONFIG_NOSYSTEM`` (no system
  file, the one a git distribution bundles included), ``GIT_CONFIG_GLOBAL`` (a scratch file of
  the test's own in place of ``~/.gitconfig``), and an empty ``credential.helper`` on git's command
  line (``GIT_CONFIG_COUNT``), which clears any helper a configuration file names. Core's own git
  reads the same files (``personalclaw.sdk.git.git_env`` keeps the first two), and clears the
  helpers on its own command line (``git_argv``). A test that wants a global setting of its own
  (an ssh stand-in) writes it to the file ``GIT_CONFIG_GLOBAL`` names.
* :class:`GitGuard` refuses, before it starts, a git a test starts that could still sign in with
  a helper of the machine's own — one that reads the system files or the owner's own global file,
  has nothing on its command line that clears the helpers the files name, or names a helper git
  would find on the machine rather than one the test wrote. The conftest fails the test that
  started it, by name.

What the guard cannot see: a git a shell starts from a command line, and a git another program
starts. Both inherit :func:`neutral_env` unless whatever starts them hands them an environment of
its own.
"""

from __future__ import annotations

import errno
import inspect
import os
import shlex
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence

#: The git subcommands that can sign in, and so ask a credential helper: the ones that talk to a
#: remote, and the ones that run git's credential machinery for another program. A
#: ``remote-<transport>`` helper, which a fetch or a push starts, can too; a ``credential-<helper>``
#: subcommand is a helper itself.
SIGNS_IN = frozenset(
    {
        "archive",
        "clone",
        "credential",
        "fetch",
        "fetch-pack",
        "http-fetch",
        "http-push",
        "imap-send",
        "lfs",
        "ls-remote",
        "maintenance",
        "pull",
        "push",
        "remote",
        "request-pull",
        "send-email",
        "send-pack",
        "submodule",
    }
)

#: git's options before the subcommand that take the next argument as their value.
_VALUE_OPTIONS = frozenset(
    {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--attr-source"}
)


def neutral_env(global_config: os.PathLike | str) -> dict[str, str]:
    """The variables every test's git runs with; *global_config* is the file that stands in for
    ``~/.gitconfig`` (it need not exist: git reads a missing one as empty, and makes it on a
    write)."""
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.fspath(global_config),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    }


def _true(value: str | None) -> bool:
    """git's reading of a boolean variable."""
    if value is None:
        return False
    text = value.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    try:
        return int(text) != 0
    except ValueError:
        return False


def _git_words(args: object, executable: object, env: Mapping[str, str]):
    """``(argv, env)`` of the git *args* starts — through ``env`` and its assignments, if that is
    how it starts — or ``None`` when it does not start git."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        words = [os.fsdecode(arg) for arg in args]  # type: ignore[union-attr]
        program = os.fsdecode(executable) if executable is not None else ""
    except TypeError:
        return None
    if os.path.basename(program) == "git":
        return ["git", *words[1:]], dict(env)
    env = dict(env)
    while words and os.path.basename(words[0]) == "env":
        words = words[1:]
        while words:
            word = words[0]
            if word in ("-i", "-", "--ignore-environment"):
                env = {}
            elif word in ("-u", "--unset") and len(words) > 1:
                env.pop(words[1], None)
                words = words[1:]
            elif word.startswith("-"):
                pass
            elif "=" in word and not word.startswith("/"):
                name, _, value = word.partition("=")
                env[name] = value
            else:
                break
            words = words[1:]
    if words and os.path.basename(words[0]) == "git":
        return words, env
    return None


def _subcommand(words: Sequence[str], env: Mapping[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """git's subcommand in *words*, and the settings its command line gives before it."""
    settings: list[tuple[str, str]] = []
    i = 1
    while i < len(words):
        word = words[i]
        if word == "-c" and i + 1 < len(words):
            key, eq, value = words[i + 1].partition("=")
            settings.append((key, value if eq else "true"))
            i += 2
        elif (word == "--config-env" and i + 1 < len(words)) or word.startswith("--config-env="):
            spec = words[i + 1] if word == "--config-env" else word.split("=", 1)[1]
            key, _, variable = spec.partition("=")
            settings.append((key, env.get(variable, "")))
            i += 2 if word == "--config-env" else 1
        elif word in _VALUE_OPTIONS:
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return word, settings
    return "", settings


def _helpers(env: Mapping[str, str], settings: Iterable[tuple[str, str]]) -> tuple[bool, list[str]]:
    """Whether git's command line clears the credential helpers its files name, and the helpers
    it names after that, in git's order: ``GIT_CONFIG_COUNT``'s settings, then
    ``GIT_CONFIG_PARAMETERS``', then the ``-c`` ones."""
    sequence: list[tuple[str, str]] = []
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        count = 0
    for n in range(count):
        sequence.append((env.get(f"GIT_CONFIG_KEY_{n}", ""), env.get(f"GIT_CONFIG_VALUE_{n}", "")))
    try:
        parameters = shlex.split(env.get("GIT_CONFIG_PARAMETERS", ""))
    except ValueError:
        parameters = []
    for parameter in parameters:
        key, eq, value = parameter.partition("=")
        sequence.append((key, value if eq else "true"))
    sequence += list(settings)
    cleared, helpers = False, []
    for key, value in sequence:
        key = key.strip().lower()
        if key == "credential.helper":
            if value:
                helpers.append(value)
            else:
                cleared, helpers = True, []
        elif key.startswith("credential.") and key.endswith(".helper") and value:
            helpers.append(value)  # a URL's own, for the URLs it matches
    return cleared, helpers


class GitGuard:
    """Refuses a git that could sign in with a credential helper of the machine's own.

    *real_home* is the home of whoever runs the tests, whose ``~/.gitconfig`` (and XDG one) is
    theirs; *own* are the folders a test's own helper may be in: the temporary folders, and
    whatever else the conftest names (the repository)."""

    def __init__(self, *, real_home: str, own: Iterable[str] = ()) -> None:
        self.real_home = os.path.realpath(real_home)
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(real_home, ".config")
        self.real_xdg = os.path.realpath(xdg)
        self.real_global = {
            os.path.realpath(os.path.join(self.real_home, ".gitconfig")),
            os.path.realpath(os.path.join(self.real_xdg, "git", "config")),
        }
        roots = [tempfile.gettempdir(), "/tmp", *own]
        self.own = tuple(sorted({os.path.realpath(root) for root in roots}))
        self._refused: list[str] = []
        self._lock = threading.Lock()
        self._undo: list = []

    def _own_global(self, env: Mapping[str, str]) -> str:
        """The owner's own global git file *env* would have git read, or ``""``."""
        if "GIT_CONFIG_GLOBAL" in env:
            named = env["GIT_CONFIG_GLOBAL"]
            real = os.path.realpath(os.path.expanduser(named)) if named else ""
            return named if real in self.real_global else ""
        home = env.get("HOME")
        if home and os.path.realpath(home) == self.real_home:
            return os.path.join(home, ".gitconfig")
        xdg = env.get("XDG_CONFIG_HOME") or (os.path.join(home, ".config") if home else "")
        if xdg and os.path.realpath(xdg) == self.real_xdg:
            return os.path.join(xdg, "git", "config")
        return ""

    def _machines(self, helper: str) -> bool:
        """Whether *helper* is one git finds on the machine, not one a test wrote: a helper named
        by a bare name (git runs ``git-credential-<name>`` from its own programs, or ``PATH``),
        or a path outside the folders a test's own helper may be in. A ``!`` helper is a command
        its caller wrote (core's token sign-in is one)."""
        text = helper.strip()
        if not text or text.startswith("!"):
            return False
        try:
            first = shlex.split(text)[0]
        except (ValueError, IndexError):
            first = text.split()[0]
        if "/" not in first and not first.startswith("~"):
            return True
        path = os.path.realpath(os.path.expanduser(first))
        return not any(path == root or path.startswith(root + os.sep) for root in self.own)

    def refusal(self, args: object, env: Mapping[str, str], executable: object = None) -> str:
        """Why the command *args*, run with *env*, is a git that could sign in with a helper of
        the machine's own; ``""`` when it isn't."""
        found = _git_words(args, executable, env)
        if found is None:
            return ""
        words, env = found
        subcommand, settings = _subcommand(words, env)
        if subcommand.startswith("credential-"):
            return f"runs a credential helper itself (git {subcommand})"
        if subcommand not in SIGNS_IN and not subcommand.startswith("remote-"):
            return ""
        if not _true(env.get("GIT_CONFIG_NOSYSTEM")):
            return (
                "reads the machine's system git configuration, which can name its keychain "
                "helper (GIT_CONFIG_NOSYSTEM isn't set)"
            )
        own = self._own_global(env)
        if own:
            return f"reads the global git configuration of whoever runs the tests ({own})"
        cleared, helpers = _helpers(env, settings)
        if not cleared:
            return (
                "can sign in with a helper a configuration file names: nothing on its command "
                "line clears credential.helper"
            )
        for helper in helpers:
            if self._machines(helper):
                return f"signs in with a credential helper of the machine's own ({helper})"
        return ""

    def take(self) -> list[str]:
        """The refusals since the last take (``"<test> -> <why>"``), cleared."""
        with self._lock:
            refused, self._refused = self._refused, []
        return refused

    def install(self) -> None:
        """Check every child this process starts, until :meth:`undo`: a refused git raises
        ``PermissionError`` in place of starting, and is noted for :meth:`take`."""
        real = subprocess.Popen._execute_child  # type: ignore[attr-defined]
        signature = inspect.signature(real)
        guard = self

        def execute_child(popen, *args, **kwargs):
            try:
                call = signature.bind(popen, *args, **kwargs).arguments
            except TypeError:
                return real(popen, *args, **kwargs)
            env = call.get("env")
            why = guard.refusal(
                call.get("args"), os.environ if env is None else env, call.get("executable")
            )
            if why:
                who = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0] or "(no test)"
                with guard._lock:
                    guard._refused.append(f"{who} -> {why}")
                raise PermissionError(
                    errno.EPERM,
                    f"refused by the test suite: this git {why}, and a test's git must never "
                    "reach the machine's own credential helper, which can be the owner's real "
                    "keychain. Run it with the environment the tests inherit (conftest.py), and "
                    "name only a helper the test wrote itself.",
                )
            return real(popen, *args, **kwargs)

        subprocess.Popen._execute_child = execute_child  # type: ignore[attr-defined]
        self._undo.append(real)

    def undo(self) -> None:
        """Put ``subprocess`` back as :meth:`install` found it."""
        while self._undo:
            subprocess.Popen._execute_child = self._undo.pop()  # type: ignore[attr-defined]
