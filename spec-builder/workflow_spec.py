"""Compile a spec into a workflow DEFINITION for PersonalClaw's own workflow engine.

This module is the whole reason the app is worth having, and the whole reason it is not a
planner. It emits the definition and stops. It has no scheduler, no run journal, no retry
policy, no node executor and no notion of a run at all — those are engine-owned, and a
second writer on any of them is the failure mode this design exists to avoid. The output is
handed to core's ``workflow_author`` (validate or save) and started with ``workflow_start``.

Two shapes it emits, both straight out of the spec:

* one ``stage`` per ``steps`` bullet, in the spec's own order, wrapped in a ``sequence``;
* a ``verify_command`` gate that runs the caller's own command, then a review ``stage`` that
  answers the done-when clauses one by one and an ``expression`` gate over its verdict.

The gate order is deliberate: the command runs FIRST, so a model reviewing the done-when
clauses is reading a tree that already passed its own check rather than being asked to
predict one. A model-answered gate placed before the command is a gate that can be talked
past; placed after it, the command is what settles the facts and the review only decides
whether the clauses the user wrote were actually met.

**The `background` section is never inlined into an emitted prompt.** It is the one section
that can hold text this app did not author — the seed path reads it out of a repository — and
a workflow definition is SAVED and RE-RUN. Folding fetched file content into a stored prompt
would make an injection durable: it would survive every later run of that workflow, long
after the human who reviewed the spec stopped looking. Background stays in the spec store,
fenced whenever a model reads it, and the compiled definition instead tells the run to go
read the tree it is working in.
"""

from __future__ import annotations

from typing import Any

from specs import (
    MAX_CLAUSES,
    MAX_STEPS,
    SEEDED_SECTION,
    Readiness,
    Spec,
    Step,
)

#: Prefix on the emitted definition name, so a compiled spec is distinguishable in the
#: Workflows list from a hand-authored definition or a bundled template.
DEF_PREFIX = "spec-"
#: Tags the emitted definition carries. `spec-builder` is the provenance marker a later
#: recompile keys off, and the reason a user can tell where a definition came from.
DEF_TAGS: tuple[str, ...] = ("spec", "spec-builder")

MODEL_TIER = "standard"
TOOLS_POSTURE = "full"

REVIEW_NODE_ID = "done_when_review"


class NotReady(ValueError):
    """The spec does not have the shape to compile. Carries the readiness verdict."""

    def __init__(self, readiness: Readiness) -> None:
        super().__init__(
            f"spec {readiness.spec_id!r} is not ready to compile: " + "; ".join(readiness.problems)
        )
        self.readiness = readiness


def def_name(spec_id: str) -> str:
    """The emitted definition's name. Held to core's own definition-name grammar."""
    return f"{DEF_PREFIX}{spec_id}"[:63].rstrip("-")


def _context_block(spec: Spec) -> str:
    """The shared preamble every emitted stage prompt carries.

    Repeated per stage rather than referenced from a first node's output: a stage is one
    subagent execution, so a node that cannot see the problem statement is a node working
    from its own instruction alone — which is exactly how a workflow drifts off the spec.
    """
    lines = [f"This work comes from a written spec: {spec.title}"]
    if spec.intent:
        lines.append(f"Intent: {spec.intent}")
    lines.append("")
    lines.append("The problem:")
    lines.append(spec.section("problem").strip())
    non_goals = spec.section("non_goals").strip()
    if non_goals:
        lines.append("")
        lines.append("Explicitly out of scope — do not do these:")
        lines.append(non_goals)
    constraints = spec.section("constraints").strip()
    if constraints:
        lines.append("")
        lines.append("Constraints that hold for every step:")
        lines.append(constraints)
    lines.append("")
    lines.append("Done means all of these, and the run is gated on them:")
    for clause in spec.clauses:
        lines.append(f"  - {clause}")
    return "\n".join(lines)


def _step_node(index: int, step: Step, context: str) -> dict[str, Any]:
    prompt = "\n".join(
        [
            f"Step {index}: {step.label}",
            "",
            step.instruction,
            "",
            context,
            "",
            "Do this step and stop. Read enough of the working tree first that what you do "
            "reflects what is there rather than what the spec assumed — if the step cannot be "
            "done as written, say so plainly instead of doing an adjacent thing.",
            "",
            "The spec's background material is deliberately NOT reproduced here. Go read the "
            "tree you are working in; it is the current truth and the spec is not.",
        ]
    )
    return {
        "kind": "stage",
        "id": f"step_{index}",
        "label": step.label,
        "config": {
            "prompt": prompt,
            "model_tier": MODEL_TIER,
            "tools_posture": TOOLS_POSTURE,
        },
    }


def _verify_gate(spec: Spec) -> dict[str, Any]:
    return {
        "kind": "gate",
        "id": "verification",
        "label": "The spec's own verification command",
        "config": {
            "kind": "verify_command",
            "verify": {
                "command": "{{inputs.verify_command}}",
                "cwd": "{{inputs.cwd}}",
                "label": f"{spec.id} verification",
            },
        },
    }


def _review_node(spec: Spec, context: str) -> dict[str, Any]:
    clauses = "\n".join(f"  {n}. {clause}" for n, clause in enumerate(spec.clauses, start=1))
    prompt = "\n".join(
        [
            "The steps ran and the spec's verification command passed. Decide, clause by "
            "clause, whether what the spec said done means is actually true now.",
            "",
            "The done-when clauses:",
            clauses,
            "",
            "How the spec says done-ness is decided:",
            spec.section("verification").strip(),
            "",
            context,
            "",
            "Check each clause by doing something that could come back negative — read the "
            "file, run the thing, look at the output. A clause you cannot check is UNMET, not "
            "met: report it in `unmet` with what you could not establish. `all_met` is true "
            "only when every clause is met, and the next step is a gate on it.",
            "",
            'Return JSON: {"all_met": boolean, "unmet": [string], "evidence": string}',
        ]
    )
    return {
        "kind": "stage",
        "id": REVIEW_NODE_ID,
        "label": "Done-when review",
        "config": {
            "prompt": prompt,
            "schema": {"all_met": "boolean", "unmet": "array", "evidence": "string"},
            "model_tier": MODEL_TIER,
            "tools_posture": TOOLS_POSTURE,
        },
    }


def _done_gate() -> dict[str, Any]:
    return {
        "kind": "gate",
        "id": "done_when",
        "label": "Every done-when clause met",
        "config": {
            "kind": "expression",
            "expr": f"{{{{nodes.{REVIEW_NODE_ID}.output.all_met}}}}",
        },
    }


def compile_spec(spec: Spec, *, force: bool = False) -> dict[str, Any]:
    """Compile *spec* into a workflow definition, or raise :class:`NotReady`.

    ``force`` compiles a spec readiness refused. It exists because a spec can be structurally
    incomplete and still worth seeing compiled — but it is opt-in, and the returned payload
    carries the verdict so a caller cannot lose track of what it overrode.
    """
    readiness = spec.readiness()
    if not readiness.ready and not force:
        raise NotReady(readiness)

    context = _context_block(spec)
    steps = spec.steps[:MAX_STEPS]
    children: list[dict[str, Any]] = [
        _step_node(index, step, context) for index, step in enumerate(steps, start=1)
    ]
    children.append(_verify_gate(spec))
    children.append(_review_node(spec, context))
    children.append(_done_gate())

    description = spec.intent or f"Compiled from the {spec.title!r} spec."
    definition = {
        "name": def_name(spec.id),
        "description": description[:400],
        "tags": list(DEF_TAGS),
        "inputs": {
            "cwd": {
                "type": "string",
                "default": ".",
                "help": (
                    "The working directory every step and the verification command run in. "
                    "Everything is verified here, so a wrong value verifies the wrong tree."
                ),
            },
            "verify_command": {
                "type": "string",
                "required": True,
                "help": _verify_help(spec),
            },
        },
        "root": {
            "kind": "sequence",
            "id": spec.id.replace("-", "_"),
            "children": children,
        },
    }
    return {
        "definition": definition,
        "readiness": readiness.to_dict(),
        "forced": bool(not readiness.ready and force),
        "nodes": len(children),
        "clauses": len(spec.clauses[:MAX_CLAUSES]),
        "excluded_sections": [SEEDED_SECTION],
    }


def _verify_help(spec: Spec) -> str:
    """The `verify_command` input's help text, quoting the spec's own verification section.

    The command is an INPUT rather than a literal in the definition on purpose: this app must
    never be the thing that decides what command runs on the user's machine. It writes down
    what the spec said done-ness is, and the person starting the run supplies the command.
    """
    stated = " ".join(spec.section("verification").split())[:240]
    tail = f' The spec says: "{stated}"' if stated else ""
    return f"The command that decides done-ness. The gate runs it and takes its exit code.{tail}"
