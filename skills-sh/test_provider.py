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
    """S05 C13 regression: the `npx skills find` display line truncates the id at
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
