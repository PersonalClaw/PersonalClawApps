"""A per-call sampling temperature reaches ollama where it reads sampling: ``options``.

best-of-N builds each candidate with a ``temperature`` build kwarg, and core reads back what the
adapter put on the request (``ModelProvider.sampling_temperature``) to say whether the slate was
really temperature-varied. The factory dropped the kwarg, so core reported every candidate "not
sent at its requested temperature". Core fixed its own native copy of this provider in #3597; the
same rule is applied here.
"""

from __future__ import annotations

from personalclaw.llm.capabilities import Capability
from personalclaw.llm.registry import ProviderEntry

_CAPS = frozenset({Capability.CHAT, Capability.STREAMING})


def _built(options: dict, **build_kwargs):
    import provider as ollama

    entry = ProviderEntry(
        name="OllamaX", type="ollama", model="llama3.2",
        options={"endpoint": "http://localhost:11434", **options}, declared_capabilities=_CAPS,
    )
    return ollama._factory(entry=entry, **build_kwargs)


def test_the_temperature_goes_into_the_requests_options_object():
    provider = _built({}, temperature=0.9)
    assert provider._extra_options["options"] == {"temperature": 0.9}
    assert provider.sampling_temperature == 0.9


def test_the_per_call_temperature_keeps_the_entrys_other_sampling_options():
    provider = _built({"options": {"num_ctx": 8192, "temperature": 0.1}}, temperature=0.9)
    assert provider._extra_options["options"] == {"num_ctx": 8192, "temperature": 0.9}


def test_no_temperature_is_reported_as_none_sent():
    assert _built({}).sampling_temperature is None
    assert _built({}, temperature=True).sampling_temperature is None  # True is not a temperature
