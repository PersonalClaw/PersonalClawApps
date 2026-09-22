#!/usr/bin/env python3
"""Repo rail: a setting the provider CODE reads must be DECLARED in the manifest schema.

The sibling rail ``check_settings_schema_posture.py`` audits the fields a schema
*declares* — do the tuning ones fold, are the credential-shaped ones marked sensitive. It
is structurally blind to the opposite defect, and that is the one that shipped: a setting
the code reads which the schema never mentions at all. An undeclared property is not in
``properties``, so every declared-field rule skips it, and the schema reads clean.

**Two independent failures live in that blind spot, and both are measured, not imagined.**

*Class B — a DEAD setting.* The generated config form renders exactly the schema's
properties, so a setting nobody declared has no input. The operator cannot set it, and the
code's ``config.get("x")`` silently takes its default forever. The knob is documented (often
in the factory's own docstring) and unreachable. Measured on this catalogue at the time this
rail landed: ``bedrock-models.max_tokens`` — whose ``create_provider`` docstring says out
loud that it "Reads ``region`` / ``default_model`` (or ``model``) / ``profile`` /
``system_prompt`` / ``max_tokens`` from the instance settings", while the manifest declared
four of those five — and ``claude-subscription.max_tokens``. Both are fixed in the commit
that adds this rail; the rail is what keeps the third one from landing.

*Class C — an UNMASKABLE credential.* Core's masking keys ENTIRELY off a declared
``x-meta.sensitive`` property: ``apps/secret_fields.py::sensitive_field_names`` iterates
``schema["properties"]`` and ``mask_secrets`` masks only those names. A credential read from
an undeclared key therefore cannot be masked on any route, by any masker, because none of
them can see a property that does not exist. ``is_credential_field_name`` — the name-based
fallback in the same module — is reached only for the schema-LESS document surface
(``personalclaw config get``), never for a provider settings schema, so it does not cover
this. This is the defect the four published satellite exemplars carried
(``action-home-assistant``, ``watched-source-github``, ``inbox-github-notifications``,
``channel-null``): each declared only ``timeout_secs`` while reading 2-4 more settings, and
two read a GitHub ``token`` no schema declared, hence unmasked everywhere. Those four sit in
standalone repositories with no catalogue CI. Every app under THIS CI was clean of Class C
when the rail landed — 0 of 61 — which is the measured case for keeping exemplars here
rather than in satellite repos.

**Why the factory's own parameter, and not a receiver-name match.** A first draft matched
``config``/``settings``/``cfg`` by NAME anywhere in the bundle and reported
``slack-channel`` reading three undeclared settings. It was not: that ``settings`` is
``load_use_case_settings("tts")``, a different document entirely. So this rail starts from
the manifest's own ``provider.implementation`` (``"provider:create_provider"``), resolves
that exact function, and taints only its first parameter plus locals assigned from it. That
took the false positives from 3 to 0 on this corpus.

**And then ONE HOP further, because without it the rail could not see its own motivating
defect.** A factory that merely forwards — ``return WatchedSourceGithubProvider(config)`` —
performs no reads of its own, so a factory-body-only walk reports zero and the app reads
clean. All four published exemplars have exactly that shape and do their reads in
``__init__`` off ``self._config``. Measured: the factory-only version found **0** setting
reads across all four, including both undeclared ``token``\\ s. With the hop, replayed
against those same four at their PRE-FIX manifests (``timeout_secs`` only), it reports **9**
violations — and both tokens land as Class C. See :func:`_one_hop_reads` for what the hop
does and where it deliberately stops.

Three more distinctions that each cost a false positive to learn:

* **Loads only.** ``cfg["base_url"] = endpoint`` is the app WRITING its own copy of the
  mapping (``alibaba-models``), not reading a setting. Flagged as a Store, it reported a
  field nobody reads.
* **Class A — the tolerant-reader ALIAS.** ``config.get("model") or
  config.get("default_model")`` reads an undeclared name whose fallback IS declared. The
  registry path can legitimately supply the alias, so the schema is right not to offer it as
  a user setting, and the read is right to accept it. Eight of these exist. They are
  allowlisted BY NAME WITH A REASON rather than pattern-excluded, and a stale entry reds —
  the discipline the sibling rail's ``PATH_VALUED_EXEMPT`` already established.
* **An unresolved entrypoint must RED, not skip.** Six model apps build their factory
  through a shared helper (``_factory, create_provider, create_catalog =
  register_branded_app(SPEC)``), so no ``def create_provider`` exists to walk. Silently
  skipping them is how a rail loses a sixth of its corpus and still prints "clean"; they are
  listed explicitly, so a SEVENTH app adopting that shape reds until it is either resolved
  or consciously added.

**Vacuity floors, and there are FOUR because each failure is independent.** A rail that
matches nothing reads as clean. MIN_FACTORIES guards entrypoint resolution; MIN_READS guards
the taint itself; MIN_ONE_HOP_READS guards the hop *separately*, because a broken hop drops
the total from 116 to 95 and stays far above MIN_READS while the rail goes blind to the one
shape every exemplar has; and both exemption sets must still resolve to a live subject, so an
entry that outlives its reason reds instead of silently widening the rule.

Run from the repository root:  python .github/scripts/check_settings_declared_reads.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ── Credential-shaped names (Class C) ────────────────────────────────────────────────
# Deliberately the SAME judgement core uses in `apps/secret_fields.py`
# (`_CREDENTIAL_WORDS` + `_KEY_QUALIFIERS`), word-matched not substring-matched, so this
# rail and the masker cannot disagree about what a credential is called. `token` as a
# substring also names every budget field in the config (`max_tokens_per_day`), and `key`
# alone is an identifier (`cache_key`) until it is qualified.
CRED_WORDS = frozenset(
    {"apikey", "credential", "credentials", "passphrase", "passwd", "password", "secret",
     "secrets", "token"}
)
KEY_QUALIFIERS = frozenset(
    {"access", "api", "auth", "client", "encryption", "private", "secret", "service", "signing"}
)

# ── Class A: tolerant-reader aliases, allowlisted with their reason ──────────────────
# (app, undeclared_name, the declared name it falls back to). Each must still resolve to a
# live aliased read or this rail reds — an entry that outlives its subject would silently
# widen the rule, which is the failure mode the sibling rail's stale-exemption floor exists
# to catch.
ALIAS_EXEMPT: dict[tuple[str, str], str] = {
    # The registry path (`provider_bridge` / `ProviderEntry`) can hand a factory a pinned
    # `model`; the SETTING a user picks is `default_model`. Both reads are correct, and
    # declaring `model` would put a second model picker on every form.
    ("anthropic-models", "model"): "default_model",
    ("bedrock-models", "model"): "default_model",
    ("claude-subscription", "model"): "default_model",
    ("meta-muse-spark", "model"): "default_model",
    ("openai-models", "model"): "default_model",
    ("vllm-models", "model"): "default_model",
    # `base_url` is the wire spelling of the same fact the form calls `endpoint`.
    ("claude-subscription", "base_url"): "endpoint",
    ("vllm-models", "base_url"): "endpoint",
}

# ── Entrypoints built by a shared helper rather than `def`-ined ──────────────────────
# `_factory, create_provider, create_catalog = register_branded_app(SPEC)` — the branded
# model-app shape. There is no function body to taint, so the read set cannot be derived.
# Named explicitly rather than skipped: a seventh adopter must be a deliberate decision.
HELPER_BUILT_FACTORIES = frozenset(
    {
        "anthropic-compatible",
        "deepseek-models",
        "groq-models",
        "mistral-models",
        "openai-compatible",
        "together-models",
    }
)

MIN_FACTORIES = 40
# Measured 116 tainted setting reads across 55 audited factories (61 bundles declare a schema;
# 6 build their factory through a helper and are unauditable). Floored well below that so
# ordinary catalogue churn does not trip it, while a broken taint — or a moved
# `implementation` key — drops it to zero and reds.
MIN_READS = 30
# 🔴 The ONE-HOP half needs its OWN floor, and this is the "two counters, not one" lesson the
# sibling rail wrote down. 21 of the 116 reads are found only by following a forwarding factory
# into the class it constructs. If that hop broke, the total would fall 116 -> 95 and stay far
# above MIN_READS, so the rail would keep printing "clean" while being blind to exactly the
# shape all four published exemplars have. Only this counter can see that.
MIN_ONE_HOP_READS = 8


def is_credential_name(name: str) -> bool:
    words = {w for w in name.lower().replace("-", "_").split("_") if w}
    if words & CRED_WORDS:
        return True
    return bool(words & {"key", "keys"} and words & KEY_QUALIFIERS)


def _tainted_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The factory's settings parameter, plus locals assigned from it.

    ``cfg = dict(config or {})`` and ``cfg = config`` both carry the taint; anything built
    from an unrelated source does not.
    """
    a = fn.args
    params = [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]
    if not params:
        return set()
    tainted = {params[0]}
    # Two passes so `x = config` then `y = dict(x)` both land, whatever the source order.
    for _ in range(2):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                referenced = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if referenced & tainted:
                    tainted.add(node.targets[0].id)
    return tainted


def _reads(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    tainted: set[str] | None = None,
    tainted_attrs: frozenset[str] = frozenset(),
) -> list[tuple[str, int, bool]]:
    """``(setting_name, lineno, is_alias_chain)`` for every string key read off the mapping.

    *tainted_attrs* carries the ONE-HOP case: a factory that only forwards
    (``return Provider(config)``) does its reads in ``__init__`` off ``self._config``, so the
    attribute the mapping was stored on has to be tainted too or the walk sees nothing.
    """
    names = _tainted_names(fn) if tainted is None else set(tainted)
    if not names and not tainted_attrs:
        return []

    def _is_tainted_receiver(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in names
        # `self._config` — the attribute a forwarding factory's constructor stored it on.
        return (
            isinstance(node, ast.Attribute)
            and node.attr in tainted_attrs
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    def key_of(node: ast.AST) -> tuple[str, int] | None:
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in {"get", "pop", "setdefault"}
                and node.args
                and _is_tainted_receiver(f.value)
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                return node.args[0].value, node.lineno
        if (
            isinstance(node, ast.Subscript)
            and _is_tainted_receiver(node.value)
            # LOADS only. A Store is the app writing its own copy of the mapping.
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return node.slice.value, node.lineno
        return None

    # A read inside `A or B` whose sibling is also a read of the mapping is an alias chain.
    alias_lines: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            found = [key_of(v) for v in node.values]
            if sum(1 for f in found if f) > 1:
                alias_lines.update(f[1] for f in found if f)

    out: list[tuple[str, int, bool]] = []
    for node in ast.walk(fn):
        k = key_of(node)
        if k:
            out.append((k[0], k[1], k[1] in alias_lines))
    return out


def _one_hop_reads(
    tree: ast.Module, factory: ast.FunctionDef | ast.AsyncFunctionDef
) -> list[tuple[str, int, bool]]:
    """Reads in the class a FORWARDING factory constructs.

    🔴 Without this the rail is blind to the very defect it was written for. A factory that
    only forwards — ``def create_provider(config): return WatchedSourceGithubProvider(config)``
    — performs no reads of its own, so the factory-body walk reports zero and the app reads
    clean. Measured against the four published satellite exemplars, every one of which has
    exactly that shape and does its five reads in ``__init__`` off ``self._config``: the
    factory-only walk found 0 setting reads across all four, including the undeclared
    ``token`` that motivated this rail.

    One hop, and deliberately only one. It resolves the class constructed in the factory *in
    the same module*, maps the tainted argument to its ``__init__`` parameter by position or
    keyword, follows ``self.<attr> = <that param>``, and then walks the WHOLE class for reads
    off either. Chasing further would need real call-graph resolution; a second hop that
    silently gives up is worse than a boundary the docstring states.
    """
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    tainted = _tainted_names(factory)
    out: list[tuple[str, int, bool]] = []

    for node in ast.walk(factory):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        cls = classes.get(node.func.id)
        if cls is None:
            continue
        init = next(
            (
                n
                for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"
            ),
            None,
        )
        if init is None:
            continue
        params = [p.arg for p in (init.args.posonlyargs + init.args.args + init.args.kwonlyargs)]
        # `self` is params[0]; a positional factory arg lands at params[i + 1].
        carried: set[str] = set()
        for i, arg in enumerate(node.args):
            if isinstance(arg, ast.Name) and arg.id in tainted and i + 1 < len(params):
                carried.add(params[i + 1])
        for kw in node.keywords:
            if kw.arg and isinstance(kw.value, ast.Name) and kw.value.id in tainted:
                carried.add(kw.arg)
        if not carried:
            continue

        # `self._config = config` — the attribute the reads actually go through.
        attrs: set[str] = set()
        for n in ast.walk(init):
            if (
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and isinstance(n.targets[0].value, ast.Name)
                and n.targets[0].value.id == "self"
                and {x.id for x in ast.walk(n.value) if isinstance(x, ast.Name)} & carried
            ):
                attrs.add(n.targets[0].attr)

        for member in cls.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.extend(
                    _reads(
                        member,
                        tainted=carried if member is init else set(),
                        tainted_attrs=frozenset(attrs),
                    )
                )
    return out


def main() -> int:  # noqa: C901 - one linear sweep; splitting it hides the report order
    failures: list[str] = []
    resolved = 0
    reads_seen = 0
    one_hop_reads_seen = 0
    alias_seen: set[tuple[str, str]] = set()
    helpers_seen: set[str] = set()

    for manifest in sorted(ROOT.glob("*/app.json")):
        app_dir = manifest.parent
        app = app_dir.name
        data = json.loads(manifest.read_text(encoding="utf-8"))
        raw = data.get("provider") or {}
        providers = raw if isinstance(raw, list) else [raw]

        for provider in providers:
            impl = str((provider or {}).get("implementation") or "")
            schema = (provider or {}).get("settingsSchema") or {}
            declared = set(schema.get("properties") or {})
            if not declared:
                # Nothing declared and nothing to contradict. An app with no settings form
                # has no dead-setting surface; a schema-less provider is a separate question.
                continue
            if ":" not in impl:
                continue
            module, _, func = impl.partition(":")
            if module.startswith("personalclaw."):
                # A native manifest shim pointing at a CORE module. Core's own tests own
                # that code; this rail audits app-authored bundles.
                continue
            source = app_dir / (module.replace(".", "/") + ".py")
            if not source.is_file():
                failures.append(
                    f"{app}: provider.implementation {impl!r} names {source.name}, "
                    f"which does not exist in the bundle"
                )
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            target = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
                ),
                None,
            )
            if target is None:
                if app in HELPER_BUILT_FACTORIES:
                    helpers_seen.add(app)
                    continue
                failures.append(
                    f"{app}: provider.implementation {impl!r} names no function in "
                    f"{source.name} — the factory is built by a helper, so its setting "
                    f"reads cannot be audited. Either expose a `def {func}` or add {app!r} "
                    f"to HELPER_BUILT_FACTORIES with its reason"
                )
                continue

            resolved += 1
            # Deduplicated: a factory that both reads a key AND forwards the mapping into the
            # constructor would otherwise report the same (name, line) twice.
            direct = _reads(target)
            hopped = _one_hop_reads(tree, target)
            one_hop_reads_seen += len([r for r in dict.fromkeys(hopped) if r not in set(direct)])
            found = dict.fromkeys(direct + hopped)
            for name, lineno, is_alias in found:
                reads_seen += 1
                if name in declared:
                    continue
                where = f"{source.name}:{lineno}"
                if is_credential_name(name):
                    failures.append(
                        f"{app}: reads CREDENTIAL setting {name!r} at {where}, which the "
                        f"manifest schema does not declare. Core's masking keys entirely off "
                        f"a declared x-meta.sensitive property (apps/secret_fields.py::"
                        f"sensitive_field_names iterates schema['properties']), so an "
                        f"undeclared credential is served in cleartext on every route and no "
                        f"masker can see it. Declare it with x-meta.sensitive: true (Class C)"
                    )
                elif (app, name) in ALIAS_EXEMPT:
                    if not is_alias:
                        failures.append(
                            f"{app}: {name!r} is allowlisted as an alias of "
                            f"{ALIAS_EXEMPT[(app, name)]!r} but the read at {where} no longer "
                            f"falls back to it — the exemption's premise is gone (Class A)"
                        )
                    else:
                        alias_seen.add((app, name))
                elif is_alias:
                    failures.append(
                        f"{app}: reads undeclared {name!r} at {where} as an alias. The "
                        f"fallback makes it harmless, but an unlisted alias is indistinguishable "
                        f"from a dead setting — add it to ALIAS_EXEMPT with the declared name "
                        f"it falls back to (Class A)"
                    )
                else:
                    failures.append(
                        f"{app}: reads setting {name!r} at {where}, which the manifest schema "
                        f"does not declare and no declared name backs. The config form renders "
                        f"only declared properties, so the operator cannot set it and this read "
                        f"takes its default forever — a documented, unreachable knob. Declare "
                        f"it, or stop reading it (Class B)"
                    )

    if resolved < MIN_FACTORIES:
        failures.append(
            f"vacuity floor: only {resolved} provider factories resolved (expected >= "
            f"{MIN_FACTORIES}) — did provider.implementation move?"
        )
    if reads_seen < MIN_READS:
        failures.append(
            f"vacuity floor: only {reads_seen} tainted setting reads seen (expected >= "
            f"{MIN_READS}) — the taint is matching nothing, which reads exactly like a "
            f"catalogue whose every setting is declared"
        )
    if one_hop_reads_seen < MIN_ONE_HOP_READS:
        failures.append(
            f"vacuity floor: only {one_hop_reads_seen} reads found by following a forwarding "
            f"factory into the class it constructs (expected >= {MIN_ONE_HOP_READS}) — the "
            f"one-hop walk is finding nothing. MIN_READS cannot see this, and it is the exact "
            f"shape every published exemplar has"
        )
    stale_alias = set(ALIAS_EXEMPT) - alias_seen
    if stale_alias:
        failures.append(
            f"vacuity floor: alias exemptions no longer resolve to a live undeclared read: "
            f"{sorted(stale_alias)} — delete the entry rather than leaving it to silently "
            f"widen Class A"
        )
    stale_helper = HELPER_BUILT_FACTORIES - helpers_seen
    if stale_helper:
        failures.append(
            f"vacuity floor: helper-built factory exemptions no longer resolve: "
            f"{sorted(stale_helper)} — the app now exposes a real factory, so delete the "
            f"entry and let its reads be audited"
        )

    if failures:
        print(f"settings declared-reads: {len(failures)} violation(s)")
        for f in failures:
            print("  -", f)
        return 1
    print(
        f"settings declared-reads: clean ({resolved} factories audited, {reads_seen} setting "
        f"reads ({one_hop_reads_seen} via the one-hop walk), {len(alias_seen)} allowlisted "
        f"aliases, {len(helpers_seen)} helper-built "
        f"factories unauditable)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
