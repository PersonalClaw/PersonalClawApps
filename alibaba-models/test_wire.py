"""Alibaba Qwen (chat): what core asks of a call is in the request the model receives.

Core hands a model factory what it wants of ONE call as build kwargs: the sampling
``temperature`` best-of-N asked for, and the output ``max_tokens`` budget it derived for the
model. Both have to reach the request, and ``sampling_temperature`` (which core reads back to say
whether a candidate was really sampled at its rung) has to agree with what was sent. The instance
is built the way the product builds one saved from the Add-instance form, and called the way core
calls a bound model.

It rides core's branded-app factory (``register_branded_app``), which reads both. So this drives
the app's real ``openai`` client against a fake endpoint that records the request.

An image goes to a model as an image only when the platform's record says the model takes one: the
provider type's ``supports_vision`` and ``image_modality`` on the model's catalog row. The last
test asks that record the way a chat turn does, for a model whose id says so, one this app declares
and one that takes none, and checks that the image part reaches the request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.model_wire import (  # noqa: E402
    MODEL,
    PNG_DATA_URL,
    REPLY,
    RecordingModelServer,
    form_options,
    image_call,
    images_sent,
    one_call,
    sampling_sent,
)
from personalclaw.sdk.model import ProviderEntry  # noqa: E402

import provider  # noqa: E402 — app-local


@pytest.fixture
def server():
    with RecordingModelServer() as recording:
        yield recording


APP_DIR = Path(__file__).resolve().parent


def _entry(url: str) -> ProviderEntry:
    """An instance saved from the Add-instance form: the form's fields, no pinned model."""
    return ProviderEntry(name="wire", type="alibaba", model="", options=form_options(APP_DIR, url))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        ({"temperature": 0.7, "max_tokens": 1234}, (0.7, 1234)),
        ({}, (None, provider.SPEC.max_tokens)),
    ],
    ids=["asked", "nothing-asked"],
)
async def test_what_core_asks_of_a_call_is_what_the_request_carries(server, asked, sent):
    # Called the way core calls a bound model: the binding's model as the ``model`` build kwarg.
    built = provider._factory(entry=_entry(server.url), model=MODEL, **asked)
    assert built.sampling_temperature == sent[0]
    assert await one_call(built) == REPLY
    assert [sampling_sent(call) for call in server.calls()] == [sent]


#: A DashScope model that takes images and whose id does not say so (``provider.takes_images``).
DECLARED_VISION = "qvq-max"
#: One whose id does: core's classifier tags it from the ``qwen-vl`` marker.
NAMED_VISION = "qwen-vl-max"
TEXT_ONLY = "qwen-plus"


@pytest.fixture
def listed(monkeypatch):
    """A form-saved instance registered the way core registers one, against a server that lists
    the three models, with 127.0.0.1 allow-listed so catalog discovery may reach it."""
    from personalclaw.config.loader import AppConfig
    from personalclaw.llm.registry import get_default_registry
    from personalclaw.providers import image_input

    cfg = AppConfig()
    cfg.security.egress.allow_hosts = ["127.0.0.1"]
    monkeypatch.setattr(AppConfig, "load", staticmethod(lambda *a, **k: cfg))
    with RecordingModelServer(models=(DECLARED_VISION, NAMED_VISION, TEXT_ONLY)) as recording:
        entry = _entry(recording.url)
        registry = get_default_registry()
        registry.register_entry(entry)
        image_input.clear_cache()
        try:
            yield recording, entry
        finally:
            registry.unregister_entry(entry.name)
            image_input.clear_cache()


@pytest.mark.asyncio
async def test_a_model_that_takes_images_is_sent_the_image(listed):
    from personalclaw.providers.image_input import image_input

    recording, entry = listed
    for model in (DECLARED_VISION, NAMED_VISION):
        answer = await image_input(f"{entry.name}:{model}")
        assert answer.accepted, (model, answer.reason)
    refused = await image_input(f"{entry.name}:{TEXT_ONLY}")
    assert (refused.accepted, refused.reason) == (False, f"{TEXT_ONLY} can't take images.")

    built = provider._factory(entry=entry, model=DECLARED_VISION)
    assert await image_call(built) == REPLY
    calls = recording.calls()
    assert [images_sent(call) for call in calls] == [[PNG_DATA_URL]]
    assert calls[0]["body"]["model"] == DECLARED_VISION
