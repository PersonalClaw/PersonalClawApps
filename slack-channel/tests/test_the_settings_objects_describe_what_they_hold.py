"""Every object among the Slack settings says what it holds, so the form edits it with controls.

The settings form edits an object as a control per named field (``properties``) or a row per entry
by key (``additionalProperties``, the key described by ``propertyNames``), and falls back to a JSON
text area when its schema says neither. Reactions and Per-channel Config said neither, so the form
asked for ``{"thinking": "brain", "done": null}`` and ``{"C0123": {"activation": …}}`` typed by
hand. Each describes what it holds now, with the keys and values the runtime reads, so what the form
writes is what the channel uses.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from slack_runtime.handler import _DEFAULT_PHASE_EMOJIS
from slack_runtime.settings import _VALID_ACTIVATIONS, ChannelConfig

_APP_DIR = Path(__file__).resolve().parents[1]


def _properties() -> dict:
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    return manifest["provider"]["settingsSchema"]["properties"]


def test_reactions_names_every_phase_the_runtime_reacts_in():
    spec = _properties()["reactions"]
    phases = spec["properties"]
    assert set(phases) == set(_DEFAULT_PHASE_EMOJIS)
    assert spec["additionalProperties"] is False, "a key the runtime does not know is ignored"
    for phase, schema in phases.items():
        assert schema["type"] == ["string", "null"], "null is how a phase is switched off"
        assert schema["x-meta"]["placeholder"] == _DEFAULT_PHASE_EMOJIS[phase], (
            "the empty field shows the emoji a blank one falls back to"
        )


def test_per_channel_config_is_entries_by_channel_id_with_the_fields_the_runtime_reads():
    spec = _properties()["channels"]
    assert "properties" not in spec
    assert spec["propertyNames"]["x-meta"]["label"] == "Channel ID"
    entry = spec["additionalProperties"]
    assert entry["type"] == "object"
    assert set(entry["properties"]) == {f.name for f in fields(ChannelConfig)}
    activation = entry["properties"]["activation"]
    assert set(activation["enum"]) == set(_VALID_ACTIVATIONS)
    assert activation["default"] == ChannelConfig().activation


def test_no_object_left_undescribed():
    undescribed = sorted(
        key
        for key, spec in _properties().items()
        if spec["type"] == "object" and "properties" not in spec and "additionalProperties" not in spec
    )
    assert undescribed == []


def test_no_help_asks_for_a_json_shape():
    shapes = sorted(
        key
        for key, spec in _properties().items()
        if spec["type"] == "object" and "{" in spec.get("x-meta", {}).get("help", "")
    )
    assert shapes == []
