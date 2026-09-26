"""The Slack app's calls into ``personalclaw.sdk.*`` hold on the INSTALLED core.

Core #3599 changed ``compress_thread_history(conversation_log, …)`` to
``compress_thread_history(prior_turns: list[dict], …)`` without touching this app, and Slack
raised ``TypeError`` on every fresh runtime over an existing thread while its suite stayed green.
This file holds the app to the core it is installed against, with three rails from
``apps_testkit.sdk_contract``:

* **published** and **binds** run here, over every SDK import and every call site the census can
  resolve (functions, constructors, module attributes, methods on SDK-typed parameters and their
  attributes, methods on an SDK function's annotated return);
* **conforms** runs on every test in this suite (``conftest._sdk_calls_conform``), checking each
  argument the app actually passes against the installed annotation.

Each rail is proven against a break it must catch before its clean result is trusted: a green
rail over a census that resolved nothing would say nothing.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apps_testkit import sdk_contract
from personalclaw.history import ConversationLog

_APP = Path(__file__).resolve().parents[1]


def _census():
    return sdk_contract.census(_APP)


# ── the rails, over the whole app ─────────────────────────────────────────────


def test_every_sdk_name_the_app_imports_is_published():
    census = _census()
    # Vacuity floor: the app imports ~150 SDK names; a census that found a handful is broken.
    assert len(census.imports) >= 100, len(census.imports)
    assert sdk_contract.unpublished_imports(_APP) == []


def test_every_sdk_call_the_app_makes_binds_to_the_installed_signature():
    census = _census()
    assert len(census.calls) >= 500, len(census.calls)
    assert sdk_contract.unbindable_calls(_APP) == []


def test_the_census_reaches_the_thread_history_calls():
    """The two calls #3599 and #3603 changed are inside the census — a rail that could not see
    them would be green over exactly the defect it exists for."""
    targets = {call.target for call in _census().calls}
    assert "personalclaw.context.compress_thread_history" in targets
    assert "personalclaw.history.ConversationLog.history_for_model" in targets
    assert "personalclaw.context.ContextBuilder.build_message" in targets


# ── positive controls: each rail catches the break it is for ──────────────────


def _fake_app(tmp_path: Path, body: str) -> Path:
    """A bundle directory holding one shipped package module with *body*."""
    app = tmp_path / "probe-app"
    pkg = app / "probe_runtime"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "caller.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return app


def test_the_bind_rail_names_a_call_whose_arity_no_longer_fits(tmp_path):
    app = _fake_app(
        tmp_path,
        """
        from personalclaw.sdk.channel import compress_thread_history

        async def restore(log, key):
            return await compress_thread_history(log, key)
        """,
    )
    problems = sdk_contract.unbindable_calls(app)
    assert len(problems) == 1
    assert "compress_thread_history" in problems[0] and "missing a required argument" in problems[0]


def test_the_bind_rail_names_a_method_the_installed_class_no_longer_has(tmp_path):
    app = _fake_app(
        tmp_path,
        """
        from personalclaw.sdk.channel import ConversationLog

        def restore(log: ConversationLog, key):
            return log.recent_with_provenance(key)
        """,
    )
    # ``recent_with_provenance`` is real history: #3599 deleted it from ConversationLog.
    assert any("recent_with_provenance no longer exists" in p for p in sdk_contract.unbindable_calls(app))


def test_the_published_rail_names_an_import_the_sdk_does_not_publish(tmp_path):
    app = _fake_app(tmp_path, "from personalclaw.sdk.channel import has_restorable_history\n")
    assert any("has_restorable_history" in p for p in sdk_contract.unpublished_imports(app))


def test_the_conformance_rail_catches_the_3599_break(tmp_path, monkeypatch):
    """Replays the exact defect: a ``ConversationLog`` handed to ``compress_thread_history``.
    Arity and position are right, so only the runtime rail can see it — and it must see it even
    though the call's own ``TypeError`` is swallowed, as Slack's handler swallowed it."""
    app = _fake_app(
        tmp_path,
        """
        from personalclaw.sdk.channel import compress_thread_history

        async def restore(log, key, query, sessions):
            try:
                return await compress_thread_history(log, key, query, sessions)
            except TypeError:
                return None
        """,
    )
    monkeypatch.syspath_prepend(str(app))
    sys.modules.pop("probe_runtime.caller", None)
    sys.modules.pop("probe_runtime", None)
    log = ConversationLog(base_dir=tmp_path / "conv")
    log.append("T1", "user", "hello")
    with sdk_contract.record_sdk_calls(app, monkeypatch) as recorder:
        from probe_runtime.caller import restore

        assert asyncio.run(restore(log, "T1", "follow up", MagicMock())) is None  # it did raise
    assert len(recorder.violations) == 1, recorder.violations
    violation = recorder.violations[0]
    assert "compress_thread_history(prior_turns=…) was passed ConversationLog" in violation
    assert "list[dict]" in violation
    assert "probe_runtime/caller.py" in violation


def test_the_conformance_rail_is_quiet_for_the_call_the_contract_declares(tmp_path, monkeypatch):
    app = _fake_app(
        tmp_path,
        """
        from personalclaw.sdk.channel import compress_thread_history

        async def restore(turns, key, query, sessions):
            return await compress_thread_history(turns, key, query, sessions)
        """,
    )
    monkeypatch.syspath_prepend(str(app))
    sys.modules.pop("probe_runtime.caller", None)
    sys.modules.pop("probe_runtime", None)
    with sdk_contract.record_sdk_calls(app, monkeypatch) as recorder:
        from probe_runtime.caller import restore

        turns = [{"role": "user", "content": "hello"}]
        result = asyncio.run(restore(turns, "T1", "follow up", MagicMock()))
    assert recorder.violations == []
    assert sum(recorder.seen.values()) == 1  # the call was checked, not skipped
    assert result is not None and "hello" in result


@pytest.mark.parametrize(
    ("value", "hint", "ok"),
    [
        ([{"role": "user"}], list[dict], True),
        ([], list[dict], True),
        (["a string"], list[dict], False),
        (ConversationLog, list[dict], False),
        ("text", str | None, True),
        (None, str | None, True),
        (3, float, True),
        ({"k": 1}, dict[str, int], True),
        ((1, 2), list[int], False),
        (MagicMock(), list[dict], True),  # a test double stands in for anything
    ],
)
def test_conformance_is_shallow_but_not_blind(value, hint, ok):
    assert sdk_contract._conforms(value, hint) is ok


class _HandRolledFake:
    """A test's own stand-in for a core object (the ``FakeSessionManager`` pattern)."""


def test_a_hand_rolled_fake_is_a_double_but_a_real_core_object_is_not(tmp_path):
    from personalclaw.sdk.channel import SessionManager

    assert sdk_contract._conforms(_HandRolledFake(), SessionManager) is True
    assert sdk_contract._conforms(ConversationLog(base_dir=tmp_path), SessionManager) is False
