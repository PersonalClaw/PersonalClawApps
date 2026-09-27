"""The app says, before and after install, that it needs a ComfyUI the user runs.

Measured before this was written:

* The Store card's lead sentence read "Generate images on your own machine — no cloud, no API
  key.", as if the app made images by itself. It does nothing without a ComfyUI server, and
  PersonalClaw cannot install one.
* The ComfyUI address was tagged ``advanced``, the class for optional tuning that a form may
  fold away. It is the one setting this app cannot work without.
* With no ComfyUI running, asking for an image answered "The local image runtime is not
  reachable ... Start ComfyUI and try again.", without saying that PersonalClaw does not do
  that for you or where the address is set.
* The field's help, the README and the refusal said a ComfyUI on the private network would
  do. The egress policy is loopback-only, so any other host is refused.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from personalclaw.sdk.image import ImageGenError
from provider import create_provider

_APP_DIR = Path(__file__).resolve().parent
_MANIFEST = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
_ENDPOINT = _MANIFEST["provider"]["settingsSchema"]["properties"]["endpoint"]
_README = (_APP_DIR / "README.md").read_text(encoding="utf-8")


def _lead(description: str) -> str:
    """The sentence the Store card shows, found the way ``check_store_card_copy.py`` finds it."""
    match = re.match(r"^.*?[.!?](?=\s|$)", description, re.S)
    return match.group(0) if match else description


def test_the_store_card_says_it_runs_on_a_comfyui_you_run():
    """🔴 Red on main: the lead promised images from "your own machine" and named no ComfyUI."""
    assert "ComfyUI" in _lead(_MANIFEST["description"])
    assert "does not install or start ComfyUI" in _MANIFEST["description"]
    assert "Configure page" in _MANIFEST["description"]


def test_the_address_is_a_required_field_not_an_advanced_one():
    """🔴 Red on main: the field was tagged ``advanced``. It is ``required`` instead, the
    repo's one convention for an app whose whole job is to talk to your server (vLLM,
    SearXNG, openai-compatible), which ``check_settings_schema_posture.py`` holds to, and a
    required field is never folded away as tuning."""
    meta = _ENDPOINT["x-meta"]
    assert "advanced" not in meta.get("tags", [])
    assert "endpoint" in _MANIFEST["provider"]["settingsSchema"]["required"]
    assert "ComfyUI" in meta["label"]
    assert "does not install or start ComfyUI" in meta["help"]


def test_no_text_offers_an_address_the_policy_refuses():
    """🔴 Red on main: the help and the README said a private-network address would work."""
    from provider import _LOCAL_ONLY

    assert _LOCAL_ONLY.loopback_only is True, "the texts below describe a loopback-only policy"
    for text in (_ENDPOINT["x-meta"]["help"], _README, _MANIFEST["description"]):
        assert "private-network" not in text and "private network" not in text


@pytest.mark.asyncio
async def test_no_comfyui_running_says_what_to_do_about_it():
    """🔴 Red on main: "Start ComfyUI and try again", with no word on who installs it or where
    the address is set."""
    prov = create_provider({"endpoint": "http://127.0.0.1:1"})
    with pytest.raises(ImageGenError) as caught:
        await prov.generate("a bicycle", model="flux.1-schnell")
    message = str(caught.value)
    assert message.startswith("ComfyUI is not reachable at http://127.0.0.1:1, so ")
    assert "PersonalClaw does not install or start ComfyUI" in message
    assert "Configure page" in message


@pytest.mark.asyncio
async def test_an_address_on_another_machine_is_refused_in_one_plain_sentence():
    """🔴 Red on main: the refusal said this machine "or private network" was allowed."""
    prov = create_provider({"endpoint": "http://192.168.1.50:8188"})
    with pytest.raises(ImageGenError) as caught:
        await prov.generate("a bicycle", model="flux.1-schnell")
    message = str(caught.value)
    assert message.startswith("Refused to reach 'http://192.168.1.50:8188/"), message
    assert "only talks to ComfyUI on this machine" in message
    assert "not reachable" not in message, "a refusal is not an absent server"


def test_the_chat_shows_that_sentence_when_no_comfyui_is_running(tmp_path, monkeypatch):
    """The end a user reads: core's ``image_generate`` tool returns the error as text."""
    from personalclaw.artifacts import native
    from personalclaw.artifacts import registry as art_reg
    from personalclaw.image_gen import registry as ig_reg
    from personalclaw.mcp_artifacts import _call_tool_inner
    from personalclaw.providers import use_cases

    store = native.NativeArtifactProvider(root=tmp_path)
    monkeypatch.setattr(art_reg, "get_provider", lambda name="native": store)
    monkeypatch.setattr("personalclaw.mcp_artifacts._resolve_session_key", lambda: None)
    ig_reg.register_provider(create_provider({"endpoint": "http://127.0.0.1:1"}))
    monkeypatch.setattr(use_cases, "active_model_refs", lambda uc: ["local-image:flux.1-schnell"])
    try:
        out = _call_tool_inner("image_generate", {"prompt": "a bicycle"})
    finally:
        ig_reg.unregister_provider("local-image")

    assert out.startswith("Error: ComfyUI is not reachable at http://127.0.0.1:1"), out
    assert "PersonalClaw does not install or start ComfyUI" in out
    assert store.list(kind="image") == []
