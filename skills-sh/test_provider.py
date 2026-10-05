"""skills-sh app: the provider loads and implements the SkillsMarketplace contract.

Loads from the app's own ``provider`` module (app dir on sys.path), not core —
skills.sh moved out of core into apps/skills-sh/ in the core/app split.
"""

from __future__ import annotations

import json
from pathlib import Path

from personalclaw.sdk.skill import SkillsMarketplace
from provider import SkillsShMarketplace, create_provider


def test_is_a_skills_marketplace():
    """The provider implements the SDK SkillsMarketplace contract (search + fetch)."""
    assert issubclass(SkillsShMarketplace, SkillsMarketplace)
    mkt = SkillsShMarketplace()
    assert hasattr(mkt, "search") and hasattr(mkt, "fetch")


def test_create_provider_returns_none_by_design():
    """skills.sh is accessed via the marketplace API (registered on import), so its
    factory has no persistent provider instance to return — it returns None."""
    assert create_provider({}) is None


def test_import_registers_the_marketplace():
    """Importing the module registers 'skills.sh' into the skills registry (the
    installed-app loader triggers this by loading provider.py). We imported `provider`
    at module top, so the registration side-effect has already run."""
    from personalclaw.sdk.skill import get_default_skills_registry
    reg = get_default_skills_registry()
    assert "skills.sh" in reg.list()
    assert isinstance(reg.get("skills.sh"), SkillsMarketplace)


def test_app_manifest_is_valid():
    """The app's manifest is well-formed: a skills provider at provider:create_provider."""
    m = json.loads((Path(__file__).parent / "app.json").read_text())
    assert m["provider"]["type"] == "skills"
    assert m["provider"]["implementation"] == "provider:create_provider"


def test_cli_search_parse_takes_canonical_slug_from_url_line():
    """Regression: the `npx skills find` display line truncates the id at
    the first space of the display name ('owner/repo@changelog generator' →
    parsed id 'owner/repo@changelog'), and installing that WRONG id fails with
    'No matching skills found'. The `└ https://skills.sh/owner/repo/slug` line
    under each hit carries the canonical slug — the parser must prefer it."""
    from unittest.mock import patch as _patch
    import subprocess as _sp

    fake_stdout = (
        "wshobson/agents@changelog-automation 10.4K installs\n"
        "└ https://skills.sh/wshobson/agents/changelog-automation\n"
        "claude-office-skills/skills@changelog generator 2.9K installs\n"
        "└ https://skills.sh/claude-office-skills/skills/changelog-generator\n"
    )
    mkt = SkillsShMarketplace()
    fake = _sp.CompletedProcess(args=[], returncode=0, stdout=fake_stdout, stderr="")
    with _patch.object(mkt, "_api_key", return_value=None), \
         _patch("subprocess.run", return_value=fake), \
         _patch("shutil.which", return_value="/usr/bin/npx"):
        results = mkt.search("changelog")
    ids = [r.id for r in results]
    assert "claude-office-skills/skills@changelog-generator" in ids, ids
    assert "claude-office-skills/skills@changelog" not in ids, ids
    # the untruncated first hit is untouched
    assert "wshobson/agents@changelog-automation" in ids


def test_find_skill_dir_resolves_slug_layouts(tmp_path):
    """The clone-based fetch resolves a slug to its dir: exact match, nested
    one level, and frontmatter-name fallback (folder != slug)."""
    from provider import _find_skill_dir

    # exact top-level
    (tmp_path / "changelog-generator").mkdir()
    (tmp_path / "changelog-generator" / "SKILL.md").write_text("---\nname: Changelog Generator\n---\n")
    assert _find_skill_dir(tmp_path, "changelog-generator").name == "changelog-generator"
    # nested under skills/
    nested = tmp_path / "skills" / "pdf-tools"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: PDF Tools\n---\n")
    assert _find_skill_dir(tmp_path, "pdf-tools") == nested
    # frontmatter-name fallback when the folder name differs from the slug
    odd = tmp_path / "SomeFolder"
    odd.mkdir()
    (odd / "SKILL.md").write_text("---\nname: my-odd-skill\n---\n")
    assert _find_skill_dir(tmp_path, "my-odd-skill") == odd
    # miss
    assert _find_skill_dir(tmp_path, "does-not-exist") is None


def test_normalize_frontmatter_name():
    """Display-name frontmatter ('Changelog Generator') is rewritten to the slug
    (the installer requires ^[a-z0-9][a-z0-9-]{0,62}$); a conforming name is
    left byte-identical."""
    from provider import _normalize_frontmatter_name

    raw = "---\nname: Changelog Generator\ndescription: makes changelogs\n---\n# Body\n"
    out = _normalize_frontmatter_name(raw, "changelog-generator")
    assert "name: changelog-generator" in out
    assert "description: makes changelogs" in out and "# Body" in out
    ok = "---\nname: already-good\ndescription: d\n---\n"
    assert _normalize_frontmatter_name(ok, "already-good") == ok


def test_the_api_key_is_read_from_the_credential_store(monkeypatch):
    """A key stored under ``skills_sh_api_key`` (Settings → Secrets, or ``personalclaw setup
    --credential``) reaches the client. It passed ``config_dir() / "credentials.json"`` where
    ``CredentialStore`` takes the home, so it looked beneath a file and never found the key."""
    from personalclaw.config.credentials import save_credential

    monkeypatch.delenv("SKILLS_SH_API_KEY", raising=False)
    monkeypatch.setenv("skills_sh_api_key", "x")
    monkeypatch.delenv("skills_sh_api_key")  # registered: teardown drops the mirrored value
    save_credential("skills_sh_api_key", "key-from-the-store")
    monkeypatch.delenv("skills_sh_api_key")  # read from the store, not the process environment

    assert SkillsShMarketplace()._api_key() == "key-from-the-store"


# ── what the children this provider starts are handed ──

#: What the gateway's environment may hold that `npx` (a package someone else publishes) and a
#: clone of someone else's repository must not see. Plain words, not key-shaped strings.
_PLANTED = {
    "ANTHROPIC_API_KEY": "planted-provider-key",
    "SLACK_BOT_TOKEN": "planted-bot-token",
    "BILLING_SERVICE_PASSWORD": "planted-password",
}


def _handed_env(monkeypatch, call) -> tuple[list[str], dict]:
    """``(names, a few chosen values)`` of the env the provider passes its one spawn."""
    import subprocess as _sp
    from unittest.mock import patch as _patch

    for name, value in _PLANTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("npm_config_registry", "https://registry.example.invalid/")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        env = kwargs.get("env")
        seen["names"] = sorted(env) if env is not None else None
        seen["values"] = {k: env.get(k) for k in ("NO_COLOR", "GIT_TERMINAL_PROMPT")} if env else {}
        seen["registry"] = (env or {}).get("npm_config_registry")
        return _sp.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

    with _patch("subprocess.run", side_effect=fake_run), _patch(
        "shutil.which", side_effect=lambda name: f"/usr/bin/{name}"
    ):
        try:
            call()
        except RuntimeError:
            pass  # the failed "clone" — only what it was handed matters here
    assert seen.get("names") is not None, "the spawn inherited the gateway's environment"
    return seen["names"], seen


def test_the_cli_search_runs_npx_without_the_gateways_secrets(monkeypatch):
    """Before, `npx -y skills find` ran with the gateway's whole environment."""
    mkt = SkillsShMarketplace()
    monkeypatch.setattr(mkt, "_api_key", lambda: None)
    names, seen = _handed_env(monkeypatch, lambda: mkt.search("changelog"))
    leaked = sorted(set(_PLANTED) & set(names))
    assert leaked == [] and "PATH" in names
    assert seen["values"]["NO_COLOR"] == "1" and seen["registry"] == "https://registry.example.invalid/"


def test_the_skill_clone_runs_git_without_the_gateways_secrets(monkeypatch):
    """Before, the clone of a skill's repository ran with the gateway's whole environment."""
    mkt = SkillsShMarketplace()
    names, seen = _handed_env(monkeypatch, lambda: mkt._fetch_via_cli("owner/repo@a-skill"))
    leaked = sorted(set(_PLANTED) & set(names))
    assert leaked == [] and "PATH" in names and seen["values"]["GIT_TERMINAL_PROMPT"] == "0"


def test_what_a_failing_npx_prints_reaches_the_log_masked(monkeypatch, caplog):
    """`npx` runs a package someone else publishes, and what it prints can carry a credential it
    read. The gateway log keeps it masked and on one line."""
    import logging
    import subprocess as _sp
    from unittest.mock import patch as _patch

    mkt = SkillsShMarketplace()
    monkeypatch.setattr(mkt, "_api_key", lambda: None)
    printed = "npm error 401 with key AKIAIOSFODNN7EXAMPLE\nnpm error see the log"

    def fake_run(argv, **kwargs):
        return _sp.CompletedProcess(args=argv, returncode=1, stdout="", stderr=printed)

    with _patch("subprocess.run", side_effect=fake_run), _patch(
        "shutil.which", side_effect=lambda name: f"/usr/bin/{name}"
    ), caplog.at_level(logging.WARNING):
        assert mkt.search("changelog") == []
    warned = [r.getMessage() for r in caplog.records if "npx skills find failed" in r.getMessage()]
    assert warned, "the failure was not logged: the test is vacuous"
    assert all("AKIAIOSFODNN7EXAMPLE" not in m and "\n" not in m for m in warned), warned
