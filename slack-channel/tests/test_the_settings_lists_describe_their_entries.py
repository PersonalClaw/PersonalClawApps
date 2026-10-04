"""Every list among the Slack settings says what its entries are, so the form can edit it as a list.

The dashboard's settings form edits a list as chips (a list of texts) or as a row per entry with a
field per key (a list of records) when the list's schema declares its ``items``, and falls back to a
JSON text area when it does not. None of these lists declared them, so the form asked for Allowed
Users as ``[{"slack_id": …, "name": …}]`` typed by hand. Each declares them now, with the keys the
runtime reads (``SlackSettings.load``), so what the form writes is what the channel uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]


def _properties() -> dict:
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    return manifest["provider"]["settingsSchema"]["properties"]


@pytest.mark.parametrize(
    ("key", "id_field"),
    [("allowed_users", "slack_id"), ("tracking_channels", "channel_id")],
)
def test_a_list_of_records_declares_its_fields_and_requires_the_id(key, id_field):
    items = _properties()[key].get("items")
    assert items is not None, f"{key} declares no items, so the form can only offer JSON"
    assert items["type"] == "object"
    assert set(items["properties"]) == {id_field, "name"}
    assert all(spec["type"] == "string" for spec in items["properties"].values())
    assert items["required"] == [id_field], "an entry without its id is dropped when loaded"


@pytest.mark.parametrize("key", ["trusted_bot_ids"])
def test_a_list_of_ids_declares_its_entries_as_text(key):
    items = _properties()[key].get("items")
    assert items is not None, f"{key} declares no items, so the form can only offer JSON"
    assert items["type"] == "string" and "enum" not in items


def test_no_list_left_undescribed():
    undescribed = sorted(
        key for key, spec in _properties().items() if spec["type"] == "array" and "items" not in spec
    )
    assert undescribed == []


def test_no_help_asks_for_a_json_shape():
    """The entries are fields on the form now; help that spells ``{slack_id, name}`` describes
    the JSON the user no longer types."""
    shapes = sorted(
        key
        for key, spec in _properties().items()
        if spec["type"] == "array" and "{" in spec.get("x-meta", {}).get("help", "")
    )
    assert shapes == []
