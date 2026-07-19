"""The ollama factory honors the ``model`` build-kwarg over entry.model —
the contract every model-provider factory implements (moved from core
tests/test_provider_resolution_unify.py; core proves the kwarg is THREADED,
this proves the app factory HONORS it)."""

from personalclaw.llm.capabilities import Capability
from personalclaw.llm.registry import ProviderEntry

_MODEL_CAPS = frozenset({
    Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
    Capability.VISION, Capability.EMBEDDING,
})


def test_ollama_factory_honors_model_override_kwarg():
    """The core ollama factory (a stand-in for every model provider factory) builds
    the provider with a ``model`` kwarg over the entry's pinned model."""
    import provider as ollama
    entry = ProviderEntry(
        name="OllamaX", type="ollama", model="entry-default",
        options={"endpoint": "http://localhost:11434"}, declared_capabilities=_MODEL_CAPS,
    )
    prov_default = ollama._factory(entry=entry)
    prov_override = ollama._factory(entry=entry, model="override-model")
    assert prov_default._model == "entry-default"
    assert prov_override._model == "override-model"
