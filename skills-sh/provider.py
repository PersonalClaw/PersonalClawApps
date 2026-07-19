"""skills.sh marketplace client for PersonalClaw.

Implements ``SkillsMarketplace`` (a read-only source: ``search`` + ``fetch``)
against the skills.sh REST API (https://skills.sh/api/v1). If no API key is
configured the client falls back to the ``npx skills add <id>`` CLI — into a
throwaway temp dir — to read a skill's files for search/fetch. Committing to the
live skills tree is never this client's job: ``SkillsRegistry.install_guarded``
scans the fetched payload and writes the exact scanned bytes via the shared
``install_skill_files`` chokepoint.

The API key is stored as a credential named ``skills_sh_api_key`` via the SDK
``CredentialStore`` (also honoured from the matching environment variable). Set it
once with:

    personalclaw setup --credential skills_sh_api_key=sk_live_...
"""

import json
import logging
import shutil
import subprocess
import urllib.request
from pathlib import Path

from personalclaw.sdk.skill import (
    SkillDetail,
    SkillEntry,
    SkillsMarketplace,
    get_default_skills_registry,
    read_skill_file_entry,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://skills.sh/api/v1"
_CRED_NAME = "skills_sh_api_key"
_TIMEOUT = 15


class SkillsShMarketplace(SkillsMarketplace):
    """skills.sh marketplace client.

    API key is resolved lazily from the credential store; the client
    degrades to the ``npx skills add`` CLI fallback when no key is set.
    """

    @property
    def marketplace_type(self) -> str:
        return "skills.sh"

    @property
    def trust_tier(self) -> str:
        # Arbitrary community registry → the full gate: warnings require confirm,
        # dangerous is refused.
        return "community"

    def _api_key(self) -> str | None:
        """Resolve the skills.sh API key from extension config, env, or credential store."""
        import os
        # Extension config takes precedence
        try:
            from personalclaw.sdk.settings import ProviderSettings
            ext_config = ProviderSettings.load("skills-sh")
            key = ext_config.get("api_key", "")
            if key:
                return key
        except Exception:
            pass
        # Env var fallback
        key = os.environ.get("SKILLS_SH_API_KEY", "")
        if key:
            return key
        # Credential store fallback
        try:
            from personalclaw.sdk.credentials import CredentialStore
            from personalclaw.sdk.util import config_dir
            store = CredentialStore(config_dir() / "credentials.json")
            cred = store.resolve(_CRED_NAME)
            return cred.secret or None
        except Exception:
            return None

    def _get(self, path: str) -> dict:
        url = f"{_API_BASE}{path}"
        # Classify the target host through the egress guard BEFORE the raw request
        # (#41). The SkillsMarketplace ABC is SYNCHRONOUS, so the async net.fetch
        # can't be used here — ``evaluate`` is the sync egress decision (resolve +
        # host-classify + scheme check) that net.fetch runs internally. This gives
        # skills.sh the same SSRF/private-IP + scheme guard the async callers get.
        try:
            from personalclaw.sdk.net import CONNECTOR, evaluate

            decision = evaluate(url, CONNECTOR)
            if not decision.allow:
                raise RuntimeError(
                    f"skills.sh request to {path} blocked by egress guard: {decision.reason}"
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # guard indeterminate (e.g. import/DNS) — proceed to the request
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read())
        except Exception as exc:
            raise RuntimeError(f"skills.sh API request failed for {path}: {exc}") from exc

    # ── SkillsMarketplace interface ───────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[SkillEntry]:
        if self._api_key():
            try:
                data = self._get(f"/skills/search?q={urllib.request.quote(query)}&limit={limit}")
            except Exception as exc:
                logger.warning("skills.sh API search failed: %s", exc)
                return self._search_via_cli(query, limit)
            results: list[SkillEntry] = []
            for item in data.get("results", data.get("skills", [])):
                results.append(SkillEntry(
                    id=item.get("id", item.get("slug", "")),
                    name=item.get("name", item.get("slug", "")),
                    description=item.get("description", ""),
                    source="skills.sh",
                    url=item.get("url", ""),
                    installs=int(item.get("installs", 0)),
                ))
            return results
        return self._search_via_cli(query, limit)

    def _search_via_cli(self, query: str, limit: int = 20) -> list[SkillEntry]:
        """Fall back to ``npx skills find`` for search when no API key."""
        import re as _re
        npx = shutil.which("npx")
        if not npx:
            logger.warning("skills.sh: no API key and npx not found — search unavailable")
            return []
        try:
            result = subprocess.run(
                [npx, "-y", "skills", "find", query],
                capture_output=True,
                text=True,
                timeout=30,
                env={**__import__("os").environ, "NO_COLOR": "1", "FORCE_COLOR": "0"},
            )
            if result.returncode != 0:
                logger.warning("npx skills find failed: %s", result.stderr[:200])
                return []
        except Exception as exc:
            logger.warning("npx skills find error: %s", exc)
            return []

        _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")
        _INSTALLS_RE = _re.compile(r"([\d.]+)([KMB]?)\s*installs?", _re.IGNORECASE)
        results: list[SkillEntry] = []
        for line in result.stdout.strip().split("\n"):
            line = _ANSI_RE.sub("", line).strip()
            if not line:
                continue
            # The "└ https://skills.sh/owner/repo/slug" line under each hit carries
            # the CANONICAL slug. The display line above it truncates at the first
            # space of the skill's display name ("…@changelog generator" →
            # parts[0]="…@changelog"), so an id parsed from the display line can be
            # WRONG and its install fails with "No matching skills found". Rebuild
            # the id from the URL, which is authoritative.
            if line.startswith("└") and results:
                m_url = _re.search(r"skills\.sh/([\w.-]+)/([\w.-]+)/([\w.-]+)", line)
                if m_url:
                    owner, repo, slug = m_url.groups()
                    prev = results[-1]
                    prev.id = f"{owner}/{repo}@{slug}"
                    prev.name = slug
                    prev.url = f"https://skills.sh/{owner}/{repo}/{slug}"
                continue
            if line.startswith("─") or line.startswith("│") or line.startswith("└"):
                continue
            parts = line.split(None, 1)
            if len(parts) >= 1:
                skill_id = parts[0].strip()
                desc = parts[1].strip() if len(parts) > 1 else ""
                if skill_id and not skill_id.startswith("Name") and not skill_id.startswith("Install") and "/" in skill_id:
                    installs = 0
                    m = _INSTALLS_RE.search(desc)
                    if m:
                        num = float(m.group(1))
                        suffix = m.group(2).upper()
                        if suffix == "K":
                            installs = int(num * 1000)
                        elif suffix == "M":
                            installs = int(num * 1_000_000)
                        elif suffix == "B":
                            installs = int(num * 1_000_000_000)
                        else:
                            installs = int(num)
                        desc = ""
                    results.append(SkillEntry(
                        id=skill_id,
                        name=skill_id.split("/")[-1],
                        description=desc,
                        source="skills.sh",
                        installs=installs,
                    ))
        return results[:limit]

    def fetch(self, skill_id: str) -> SkillDetail:
        """Fetch full skill detail from skills.sh.

        ``skill_id`` can be ``owner/repo@skill-name`` (from npx search)
        or ``owner/repo/skill-name`` (canonical). Both are normalized.

        With an API key: uses the authenticated REST endpoint (returns
        full detail + audit status). Without one: falls back to ``npx
        skills add`` into a temp dir and reads the resulting SKILL.md.
        """
        if self._api_key():
            normalized = skill_id.replace("@", "/")
            parts = normalized.strip("/").split("/", 1)
            if len(parts) == 2:
                path = f"/skills/{parts[0]}/{parts[1]}"
            else:
                path = f"/skills/{normalized}"
            try:
                data = self._get(path)
            except Exception as exc:
                raise RuntimeError(f"skills.sh fetch failed for {skill_id!r}: {exc}") from exc

            files: list[dict[str, str]] = []
            for f in data.get("files", []):
                files.append({
                    "path": f.get("path", "SKILL.md"),
                    "contents": f.get("contents", ""),
                })
            audit = data.get("audit", {})
            audit_status = audit.get("status", "unknown") if isinstance(audit, dict) else "unknown"

            return SkillDetail(
                id=skill_id,
                name=data.get("slug", skill_id.split("/")[-1]),
                files=files,
                audit_status=str(audit_status),
            )

        return self._fetch_via_cli(skill_id)

    def _fetch_via_cli(self, skill_id: str) -> SkillDetail:
        """Fall back to a direct shallow ``git clone`` and read the slug dir.

        Used when no API key is configured. The previous fallback shelled out to
        ``npx skills add --skill <slug>`` — but that CLI matches skills by DISPLAY
        NAME ("Changelog Generator"), not the canonical slug our search returns
        ("changelog-generator"), so every install of a skill whose display name
        contains a space failed with "No matching skills found". The CLI itself
        just clones ``https://github.com/{owner}/{repo}.git`` — do that directly:
        deterministic slug→directory resolution, no fuzzy matcher, no npx.
        Returns the same SkillDetail shape as the authenticated path.
        """
        import tempfile

        git = shutil.which("git")
        if not git:
            raise RuntimeError("skills.sh: no API key and git not found — set SKILLS_SH_API_KEY or install git")

        env = {**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"}
        source = skill_id.split("@")[0] if "@" in skill_id else "/".join(skill_id.strip("/").split("/")[:2])
        skill_name = skill_id.split("@")[-1] if "@" in skill_id else skill_id.strip("/").split("/")[-1]
        url = f"https://github.com/{source}.git"

        with tempfile.TemporaryDirectory(prefix="personalclaw-skill-fetch-") as tmp:
            repo = Path(tmp) / "repo"
            try:
                result = subprocess.run(
                    [git, "clone", "--depth", "1", url, str(repo)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
            except Exception as exc:
                raise RuntimeError(f"skills.sh clone failed: {exc}") from exc
            if result.returncode != 0:
                raise RuntimeError(
                    f"skills.sh clone failed for {url}: {(result.stderr or result.stdout)[:300]}"
                )

            skill_dir = _find_skill_dir(repo, skill_name)
            if skill_dir is None:
                raise RuntimeError(f"skill {skill_name!r} not found in {url}")

            files: list[dict[str, object]] = []
            for path in sorted(skill_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(skill_dir).as_posix()
                try:
                    entry = read_skill_file_entry(path, rel)
                except OSError:
                    # Genuinely unreadable (permissions/IO) — skip. Binary is NOT skipped:
                    # read_skill_file_entry carries it as bytes so it's scanned + locked.
                    continue
                # Community repos commonly put the DISPLAY name in frontmatter
                # ("name: Changelog Generator") — PClaw's install validator requires
                # the slug form. Normalize to the canonical marketplace slug HERE,
                # before install_guarded scans: scanned bytes == installed bytes.
                if rel == "SKILL.md" and isinstance(entry.get("contents"), str):
                    entry["contents"] = _normalize_frontmatter_name(
                        entry["contents"], skill_name
                    )
                files.append(entry)

            return SkillDetail(
                id=skill_id,
                name=skill_name,
                files=files,
                audit_status="unknown",
            )

def _normalize_frontmatter_name(contents: str, slug: str) -> str:
    """Rewrite a non-slug frontmatter ``name:`` to the canonical slug.

    PClaw's installer requires ``^[a-z0-9][a-z0-9-]{0,62}$``; community repos
    often carry the human display name instead. Only rewrites when the declared
    name fails the slug pattern — a conforming name is left byte-identical.
    """
    import re as _re

    if not contents.startswith("---"):
        return contents
    end = contents.find("\n---", 3)
    if end == -1:
        return contents
    fm = contents[3:end]
    m = _re.search(r"^name:\s*(.+)$", fm, _re.MULTILINE)
    if not m:
        return contents
    declared = m.group(1).strip().strip("\"'")
    if _re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", declared):
        return contents
    new_fm = fm[: m.start()] + f"name: {slug}" + fm[m.end():]
    return contents[:3] + new_fm + contents[end:]


def _find_skill_dir(repo: Path, slug: str) -> Path | None:
    """Resolve a skill slug to its directory inside a cloned skills repo.

    Preference order: an exact directory match containing SKILL.md (the
    canonical layout — ``<repo>/<slug>/SKILL.md`` or nested one level under
    e.g. ``skills/``), then a case-insensitive match, then a SKILL.md whose
    frontmatter ``name:`` normalizes to the slug (repos whose folder names
    differ from the marketplace slug).
    """
    exact = [p.parent for p in repo.rglob("SKILL.md") if p.parent.name == slug]
    if exact:
        return sorted(exact, key=lambda p: len(p.parts))[0]
    ci = [p.parent for p in repo.rglob("SKILL.md") if p.parent.name.lower() == slug.lower()]
    if ci:
        return sorted(ci, key=lambda p: len(p.parts))[0]
    norm = slug.lower().replace("-", " ").replace("_", " ")
    for p in sorted(repo.rglob("SKILL.md"), key=lambda p: len(p.parts)):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        for line in head.splitlines():
            if line.strip().lower().startswith("name:"):
                declared = line.split(":", 1)[1].strip().strip("\"'").lower()
                if declared.replace("-", " ").replace("_", " ") == norm:
                    return p.parent
                break
    return None


# ── Auto-register on import ───────────────────────────────────────────────────

get_default_skills_registry().register("skills.sh", SkillsShMarketplace())


def create_provider(config=None):
    """Extension factory for skills.sh marketplace provider."""
    return None  # Marketplace is accessed via API, no persistent instance

