#!/usr/bin/env python3
"""Repo rail: one convention for ``advanced`` and ``required`` settings fields (AP-10).

The host renders every ``provider.settingsSchema`` with the same form widget, so an
uneven vocabulary reads as arbitrary: brave-search folds ``timeout_secs`` behind the
Advanced disclosure while a peer's identical field sits on the first screen. The
convention this rail pins is the one the catalog already practices by majority
(documented in docs/app-creation-guide.md, "Advanced and required — the one
convention"):

1. **Optional tuning fields fold.** A property whose name marks it as tuning/override
   class — ``timeout_secs``, an ``endpoint``/``*_endpoint``/``base_url`` override, a
   ``*_bin`` binary path — and which is NOT in the schema's ``required`` array must
   carry ``x-meta.tags: ["advanced"]``. A first-run user never needs it; the Advanced
   fold is where it lives.
2. **Required fields never fold.** A property listed in ``required`` must NOT be
   tagged ``advanced`` — required means the app cannot mount without it, and hiding a
   mandatory field behind a disclosure is a form that fails silently. (An ``endpoint``
   in ``required`` — the openai-compatible/ollama/vllm/searxng class, where pointing
   at your server IS the app — is therefore correctly untagged: requiredness, not the
   field name, is the discriminator.)
3. **``api_key`` is never required.** Every model/search provider falls back to an
   env var; marking one app's key required would make identical forms disagree about
   the same fact. The fallback is the documented convention, not an accident.
4. **A credential-shaped field carries ``x-meta.sensitive``.** That flag is the SOLE
   input to every masker core has — verified by reading them, not from memory:
   ``dashboard/handlers/apps.py::_sensitive_field_names`` (which feeds
   ``_mask_secret_config``, whose own comment names the consequence: "leaving the
   backend in cleartext on every config-panel open (#43)"),
   ``config/validation.py::_is_sensitive_path``, and the frontend
   ``pages/apps/appConfigForm.tsx``, which decides ``type="password"`` and the
   write-only blank-input behaviour from the same flag. A manifest declaring an
   ``api_key`` without it therefore gets no masking anywhere, on any route — the maskers
   are correct and the *data* is wrong. Measured 2026-09-07 over 58 schemas / 188 settings
   fields: 27 credential-shaped fields carried the flag and **one did not**
   (``openai-tools.api_key``, a bearer token). ``openai-tools`` is one of only two apps
   a cold Settings → Providers load fetches an instance config for, which made it the
   single browser-reachable cleartext credential in the catalog.

   Core has a rail for this too, but it **cannot gate this repo**: it resolves the apps
   clone as a sibling of the core checkout, which is absent on a CI runner, so its
   corpus half scans only core's 30 bundled manifests — a tree with zero
   credential-shaped fields. It therefore passes green in CI while a violation sits
   here. This is the layer that owns the manifests, so this is the layer that must gate.

**Why the name pattern is anchored.** ``CRED_CLASS`` matches only a credential noun at
the END of the name (or a whole-name special case), so ``max_tokens``, ``token_limit``,
``auth_mode`` and ``api_keys`` do not match. An unanchored substring list — the shape
core's rail uses — flags ``max_tokens`` and would red on a normal numeric field.

**Why an exemption list exists at all.** A name cannot distinguish "holds a credential"
from "names a file that holds one". ``rsync-sync.ssh_key`` is a *path* handed to ``ssh``
("the key itself is never read by PersonalClaw"), and masking it would hide a path the
user has to verify — the same class as ``staging_dir``. So it is exempted explicitly,
with its reason, rather than by loosening the pattern until it stops matching. Each
exemption must still resolve to a live field (see the floor below), so one that outlives
its subject reds instead of silently widening the rule.

**Vacuity floor.** A rail that matches nothing reads as clean — and this rail's own
first draft proved it: an early scan of this corpus looked for ``settings``/
``configSchema`` and reported "0 credential fields, 0 unmarked", which reads exactly
like a clean catalog. The real path is ``provider.settingsSchema.properties``. Guards:
at least MIN_SCHEMAS schemas discovered; every app in KNOWN_ADVANCED_ADOPTERS still
carrying an ``advanced`` tag; and every PATH_VALUED_EXEMPT entry still resolving.

Rule 4 needs **two** counters, not one, and finding that out is the reason this note is
long. ``MIN_SENSITIVE_FIELDS`` guards ``x-meta.sensitive`` itself — rename or move that
key and the count falls to zero. But it counts the flag *whatever the field is called*,
so it stays green if ``CRED_CLASS`` stops matching and rule 4 silently checks nothing.
``MIN_CRED_SHAPED_FIELDS`` is the floor that sees that case. One counter looked
sufficient and was not; the two failures are independent and each needs its own floor.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Tuning/override name classes (rule 1). Deliberately narrow: only names whose class
# the catalog has already voted on. Judgment calls that aren't mechanically decidable
# (e.g. whether a model picker is advanced) stay prose-only in the style guide.
ADVANCED_CLASS = re.compile(r"(^timeout(_secs)?$|(^|_)endpoint$|^base_url$|_bin$)")

# Credential-shaped names (rule 4). Anchored at the END of the name deliberately, so a
# plural or a qualifier cannot match: `max_tokens`, `token_limit`, `api_keys` and
# `auth_mode` are all normal fields. `access_key_id` is spelled out because it ends in
# `_id` yet is half of an AWS credential pair.
CRED_CLASS = re.compile(
    r"((^|_)(key|token|secret|password|passphrase|credential)$)|(^access_key_id$)",
    re.I,
)

# (app, field) pairs whose value is a PATH to a credential, not the credential. Masking
# these would hide something the user must be able to read back. Keep this list tiny and
# always give the reason — it is the one place the rule can be weakened.
PATH_VALUED_EXEMPT = {
    # Handed to `ssh` by path; the app never reads the key bytes. Its own help says so.
    ("rsync-sync", "ssh_key"),
}

MIN_SCHEMAS = 40
KNOWN_ADVANCED_ADOPTERS = {"brave-search", "openai-models", "claude-code-agent"}
# Measured 27 marked on 2026-09-07. Floored well below that so ordinary catalog churn
# does not trip it, while a traversal that stops seeing `x-meta.sensitive` still reds.
MIN_SENSITIVE_FIELDS = 20
# Measured 28 CRED_CLASS matches on 2026-09-07 (27 marked + 1 path-valued exempt). This
# is the floor that catches a broken PATTERN: MIN_SENSITIVE_FIELDS counts fields carrying
# the flag whatever their name, so it stays green if CRED_CLASS stops matching and rule 4
# quietly checks nothing. Only this counter can see that.
MIN_CRED_SHAPED_FIELDS = 20


def main() -> int:
    failures: list[str] = []
    schemas = 0
    adopters_seen: set[str] = set()
    sensitive_seen = 0
    cred_shaped_seen = 0
    exempt_seen: set[tuple[str, str]] = set()

    for manifest in sorted(ROOT.glob("*/app.json")):
        app = manifest.parent.name
        data = json.loads(manifest.read_text(encoding="utf-8"))
        schema = (data.get("provider") or {}).get("settingsSchema") or {}
        props = schema.get("properties") or {}
        if not props:
            continue
        schemas += 1
        required = set(schema.get("required") or [])

        for name, spec in props.items():
            tags = ((spec.get("x-meta") or {}).get("tags")) or []
            is_advanced = "advanced" in tags

            if is_advanced:
                adopters_seen.add(app)

            if ADVANCED_CLASS.search(name) and name not in required and not is_advanced:
                failures.append(
                    f"{app}: optional tuning field '{name}' must carry "
                    f'x-meta.tags ["advanced"] (rule 1)'
                )
            if name in required and is_advanced:
                failures.append(
                    f"{app}: required field '{name}' must not be tagged advanced — "
                    f"a mandatory field can't hide behind the fold (rule 2)"
                )
            if name == "api_key" and name in required:
                failures.append(
                    f"{app}: 'api_key' must not be required — the env-var fallback "
                    f"is the catalog convention (rule 3)"
                )

            is_sensitive = bool((spec.get("x-meta") or {}).get("sensitive"))
            if is_sensitive:
                sensitive_seen += 1
            if CRED_CLASS.search(name):
                cred_shaped_seen += 1
                if (app, name) in PATH_VALUED_EXEMPT:
                    exempt_seen.add((app, name))
                elif not is_sensitive:
                    failures.append(
                        f"{app}: credential-shaped field '{name}' must carry "
                        f"x-meta.sensitive: true — that flag is the ONLY input to core's "
                        f"maskers (_sensitive_field_names, _is_sensitive_path, and the "
                        f"password-input decision in appConfigForm), so without it the "
                        f"value is served in cleartext on every route (rule 4)"
                    )

    if schemas < MIN_SCHEMAS:
        failures.append(
            f"vacuity floor: only {schemas} settingsSchema blocks discovered "
            f"(expected >= {MIN_SCHEMAS}) — did provider.settingsSchema move?"
        )
    missing = KNOWN_ADVANCED_ADOPTERS - adopters_seen
    if missing:
        failures.append(
            f"vacuity floor: known advanced-tag adopters not seen carrying the tag: "
            f"{sorted(missing)} — did x-meta.tags move?"
        )
    if sensitive_seen < MIN_SENSITIVE_FIELDS:
        failures.append(
            f"vacuity floor: only {sensitive_seen} fields seen marked "
            f"x-meta.sensitive (expected >= {MIN_SENSITIVE_FIELDS}) — rule 4 would be "
            f"matching nothing, which reads exactly like a clean catalog"
        )
    if cred_shaped_seen < MIN_CRED_SHAPED_FIELDS:
        failures.append(
            f"vacuity floor: CRED_CLASS matched only {cred_shaped_seen} field names "
            f"(expected >= {MIN_CRED_SHAPED_FIELDS}) — rule 4 is checking nothing. This "
            f"is the floor MIN_SENSITIVE_FIELDS cannot see, because that one counts the "
            f"flag whatever the field is called"
        )
    stale = PATH_VALUED_EXEMPT - exempt_seen
    if stale:
        failures.append(
            f"vacuity floor: path-valued exemptions no longer resolve to a "
            f"credential-shaped field: {sorted(stale)} — delete the entry rather than "
            f"leaving it to silently widen rule 4"
        )

    if failures:
        print(f"settings-schema posture: {len(failures)} violation(s)")
        for f in failures:
            print("  -", f)
        return 1
    print(
        f"settings-schema posture: clean ({schemas} schemas checked, "
        f"{sensitive_seen} sensitive-marked fields, {len(exempt_seen)} path-valued exempt)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
