"""The provider-id rail reds on each planted defect, and passes on the real tree (#166).

The rail is ``.github/scripts/check_provider_ids.py``, which the ``static`` job's ``provider-ids``
row runs over the tree. Each test here seeds a small git repository whose ``v0.1.0`` tag holds the
released manifest, plants one defect in the manifest now, and expects the rail to name it; the
last runs the real tree. Git runs in the neutral environment the repository's conftest gives every
test.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_provider_ids.py"

_spec = importlib.util.spec_from_file_location("check_provider_ids", SCRIPT)
assert _spec and _spec.loader
rail = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rail)


def _released() -> dict[str, Any]:
    """The app as released before ids: a channel and a trigger source, with no id or label."""
    return {
        "name": "demo",
        "version": "1.0.0",
        "displayName": "Demo",
        "description": "A demo app.",
        "provider": {"type": "channel", "implementation": "demo.transport:create_provider"},
        "providers": [{"type": "trigger_source", "implementation": "demo.source:create_provider"}],
    }


def _good() -> dict[str, Any]:
    m = copy.deepcopy(_released())
    m["version"] = "1.0.1"
    m["provider"] = {"id": "channel", "label": "Chat with your agent", **m["provider"]}
    m["providers"] = [
        {"id": "triggers", "label": "Run automations on messages", **m["providers"][0]}
    ]
    return m


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _repo(
    tmp_path: Path,
    now: dict[str, Any],
    *,
    released: dict[str, Any] | None = None,
    tag: str | None = "v0.1.0",
    readme: str = "",
) -> Path:
    repo = tmp_path / "repo"
    app = repo / now["name"]
    app.mkdir(parents=True)
    _git(repo, "init", "-q")
    (app / "app.json").write_text(json.dumps(released or _released()), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    if tag:
        _git(repo, "tag", tag)
    (app / "app.json").write_text(json.dumps(now), encoding="utf-8")
    if readme:
        (app / "README.md").write_text(readme, encoding="utf-8")
    return repo


def _found(tmp_path: Path, now: dict[str, Any], **kw: Any) -> list[str]:
    found, _counted = rail.problems(_repo(tmp_path, now, **kw))
    return found


def test_a_good_app_passes(tmp_path: Path) -> None:
    assert _found(tmp_path, _good()) == []


def _set(path: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def plant(m: dict[str, Any]) -> None:
        entry = m["provider"] if path == "provider" else m["providers"][int(path[-2])]
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value

    return plant


@pytest.mark.parametrize(
    ("plant", "said"),
    [
        (_set("provider", "id", None), "provider has no id"),
        (_set("providers[0]", "label", None), "providers[0] has no label"),
        (_set("provider", "id", "Chat_Channel"), "provider.id 'Chat_Channel' is not kebab-case"),
        (_set("providers[0]", "id", "channel"), "providers[0].id 'channel' is already provider's"),
        (_set("provider", "label", "Channel"), "provider.label is its id"),
        (_set("providers[0]", "label", "trigger source"), "is a provider type"),
        (
            _set("providers[0]", "label", "Chat with your agent"),
            "providers[0].label repeats provider.label",
        ),
    ],
    ids=[
        "no-id",
        "no-label",
        "id-not-kebab",
        "id-repeated",
        "label-is-its-id",
        "label-is-a-type",
        "label-repeated",
    ],
)
def test_each_planted_entry_defect_is_named(tmp_path: Path, plant, said: str) -> None:
    m = _good()
    plant(m)
    found = _found(tmp_path, m)
    assert any(said in line for line in found), found


def test_the_first_provider_moved_into_the_list_is_named(tmp_path: Path) -> None:
    m = _good()
    m["providers"] = [m.pop("provider"), *m["providers"]]
    assert any("must stay in the singular `provider`" in line for line in _found(tmp_path, m))


def test_a_copy_of_the_singular_provider_in_the_list_is_named(tmp_path: Path) -> None:
    m = _good()
    m["providers"].append({**m["provider"], "id": "channel-copy", "label": "Another chat"})
    assert any("providers[1] repeats the singular provider" in line for line in _found(tmp_path, m))


def test_a_released_id_dropped_with_no_tombstone_is_named(tmp_path: Path) -> None:
    m = _good()
    m["providers"] = []
    found = _found(tmp_path, m)
    assert any(
        "'triggers' released in v0.1.0 is gone with no renamedFrom or retiredCapabilities" in line
        for line in found
    ), found
    m["retiredCapabilities"] = ["triggers"]
    assert _found(tmp_path / "again", m) == []


def test_a_released_id_renamed_maps_onto_one_entry(tmp_path: Path) -> None:
    m = _good()
    m["providers"][0]["id"] = "events"
    m["providers"][0]["renamedFrom"] = ["triggers"]
    assert _found(tmp_path, m) == []
    m["provider"]["renamedFrom"] = ["triggers"]
    found = _found(tmp_path / "twice", m)
    assert any("renamedFrom names 'triggers', also renamed by provider" in line for line in found)


def test_a_model_released_without_an_id_maps_by_renamed_from(tmp_path: Path) -> None:
    released = {**_released(), "provider": {"type": "model", "implementation": "p:create"}}
    released.pop("providers")
    m = {**released, "version": "1.0.1"}
    m["provider"] = {"id": "chat", "label": "Chat models", **released["provider"]}
    found = _found(tmp_path, m, released=released)
    assert any("'model' released in v0.1.0 is gone" in line for line in found), found
    m["provider"]["renamedFrom"] = ["model"]
    assert _found(tmp_path / "mapped", m, released=released) == []


def test_the_latest_release_tag_is_the_one_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _good())
    released = _good()
    released["providers"][0]["id"] = "events"
    (repo / "demo" / "app.json").write_text(json.dumps(released), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "next release")
    _git(repo, "tag", "v0.10.0")
    (repo / "demo" / "app.json").write_text(json.dumps(_good()), encoding="utf-8")
    found, _ = rail.problems(repo)
    assert any("'events' released in v0.10.0 is gone" in line for line in found), found


def test_a_checkout_with_no_release_tag_fails_and_says_how_to_fetch_one(tmp_path: Path) -> None:
    found = _found(tmp_path, _good(), tag=None)
    assert len(found) == 1 and "no v* release tag" in found[0] and "git fetch --depth 1" in found[0]


def test_a_retired_id_declared_again_is_named(tmp_path: Path) -> None:
    m = _good()
    m["retiredCapabilities"] = ["triggers"]
    assert any("'triggers' is retired" in line for line in _found(tmp_path, m))


def test_a_key_in_owner_facing_text_is_named(tmp_path: Path) -> None:
    found = _found(tmp_path, _good(), readme="Turn on demo/triggers to run automations.")
    assert any("shows the key 'demo/triggers'" in line for line in found)
    m = _good()
    m["provider"]["settingsSchema"] = {
        "type": "object",
        "properties": {"token": {"type": "string", "x-meta": {"help": "Used by demo/channel."}}},
    }
    assert any("shows the key 'demo/channel'" in line for line in _found(tmp_path / "s", m))


def test_the_real_tree_passes() -> None:
    assert rail.latest_tag(ROOT), "no v* tag here: " + rail.FETCH_HINT
    found, counted = rail.problems(ROOT)
    assert counted >= rail._MIN_APPS, counted
    assert found == [], "\n".join(found)
