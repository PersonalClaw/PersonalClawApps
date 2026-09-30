"""Local image generation: the owner's Denied hosts hold for ComfyUI on this machine too.

The app reaches only a loopback address, whatever the owner allowed. A host the owner denied in
Settings → Security → Network egress is never reached either, and the app says so in those
words, rather than telling them it only talks to ComfyUI on this machine, which is where it
already is. The app's real request goes through the SDK's real guard to a server on this machine
standing in for ComfyUI (``apps_testkit.egress``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider  # noqa: E402
from apps_testkit.egress import HOST, ProviderHost, owner_egress  # noqa: E402


@pytest.fixture
def comfyui():
    with ProviderHost("{}") as host:
        yield host


@pytest.mark.asyncio
async def test_comfyui_on_this_machine_is_reached_with_nothing_set(comfyui):
    owner_egress()
    image = provider.create_provider({"endpoint": comfyui.url})
    assert await image.is_available() is True
    assert await image.unavailable_reason() == ""
    assert [r["path"] for r in comfyui.requests] == ["/system_stats", "/system_stats"]


@pytest.mark.asyncio
async def test_a_host_the_owner_denied_is_never_asked(comfyui):
    owner_egress(deny_hosts=[HOST])
    image = provider.create_provider({"endpoint": comfyui.url})
    assert await image.is_available() is False
    assert await image.unavailable_reason() == (
        f"Refused to reach '{comfyui.url}/system_stats': {HOST} is on Denied hosts in "
        "Settings → Security → Network egress."
    )
    assert comfyui.requests == []
