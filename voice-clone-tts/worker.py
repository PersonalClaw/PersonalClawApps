"""Sidecar worker for Voice Clone TTS — real OmniVoice zero-shot inference (MI-6).

Loaded by core's sidecar child (``personalclaw.local_models._sidecar_child``) inside the
app's own venv, NEVER by the gateway process: the engine is torch-heavy diffusion, and
this process boundary is what turns a native crash into a typed
``sidecar_crashed:<reason>`` instead of a dead gateway.

Contract (what the child dispatches):

``load(device=..., weights_dir=...)``
    Import the engine and construct the pipeline once. Idempotent — a second ``load``
    with the same arguments reuses the live pipeline.

``call("synthesize", payload)``
    One zero-shot cloning synthesis: ``text`` conditioned on ``ref_audio``/``ref_text``,
    written to ``output_path`` (WAV). Returns ``{"output_path": ...}``.

``unload()``
    Drop the pipeline reference so the next ``load`` starts cold.

**API adaptation, stated honestly.** The engine's public surface is probed in a fixed,
documented order (``OmniVoice.from_pretrained`` → ``load_model`` → ``Pipeline``; then
``clone`` → ``synthesize`` → ``tts`` → ``generate``) with keyword arguments filtered to
the callee's real signature. When the installed engine matches none of these, the worker
raises — the child answers a typed ``worker_contract``/``exception`` failure while
STAYING ALIVE (``SidecarWorkerError``, not a crash), so an engine-version drift reads as
"the call failed: engine_api_mismatch", never as a process death that burns a restart.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from typing import Any

_PIPELINE: Any = None
_LOADED_KEY: tuple[str, str] | None = None

#: Constructor entry points, most-documented first.
_CONSTRUCTORS = ("OmniVoice.from_pretrained", "load_model", "Pipeline")

#: Synthesis methods, most-documented first. ``clone`` is the engine's primary trained
#: task per the bake-off evidence; the rest are the conventional fallbacks.
_SYNTH_METHODS = ("clone", "synthesize", "tts", "generate")


def _filtered_kwargs(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only the kwargs *fn* actually accepts (drop the rest, never guess names).

    A ``**kwargs`` callee accepts everything; a typed signature gets exactly its own
    parameters. Signature introspection failing (C extension) passes everything through
    — the call itself is then the arbiter.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


def _construct(module: Any, *, device: str, weights_dir: str) -> Any:
    """Build the engine pipeline via the first matching documented entry point.

    Call shapes are tried SEQUENTIALLY with minimal arguments — positional weights
    path plus one device keyword, then narrower fallbacks — never a batch of aliased
    kwargs. A ``**kwargs``-forwarding constructor (OmniVoice's ``from_pretrained``
    forwards into ``__init__``) accepts everything at the signature and then chokes
    on unknown aliases inside, so signature filtering cannot protect a batched call;
    live validation against omnivoice 0.2.1 proved the minimal shapes are what work.
    """
    candidates: list[tuple[str, Any]] = []
    omni_cls = getattr(module, "OmniVoice", None)
    if omni_cls is not None and callable(getattr(omni_cls, "from_pretrained", None)):
        candidates.append(("OmniVoice.from_pretrained", omni_cls.from_pretrained))
    for name in ("load_model", "Pipeline"):
        fn = getattr(module, name, None)
        if callable(fn):
            candidates.append((name, fn))
    errors: list[str] = []
    for name, fn in candidates:
        shapes: list[tuple[tuple, dict]] = []
        if weights_dir:
            shapes += [
                ((weights_dir,), {"device_map": device}),
                ((weights_dir,), {"device": device}),
                ((weights_dir,), {}),
            ]
        shapes += [((), {"device_map": device}), ((), {"device": device}), ((), {})]
        for args, kwargs in shapes:
            try:
                return fn(*args, **kwargs)
            except TypeError as exc:
                errors.append(f"{name}{args}{kwargs}: {exc}")
    raise RuntimeError(
        "engine_api_mismatch: omnivoice exposes none of "
        f"{list(_CONSTRUCTORS)} with a compatible signature ({'; '.join(errors[-3:]) or 'no entry points found'})"
    )


def load(*, device: str = "cpu", weights_dir: str = "", **_ignored: Any) -> dict[str, Any]:
    """Import the engine + construct the pipeline (idempotent per device/weights pair)."""
    global _PIPELINE, _LOADED_KEY
    key = (device, weights_dir)
    if _PIPELINE is not None and _LOADED_KEY == key:
        return {"loaded": True, "reused": True, "device": device}
    import omnivoice  # heavy: torch + the diffusion stack; ONLY ever imported here

    _PIPELINE = _construct(omnivoice, device=device, weights_dir=weights_dir)
    _LOADED_KEY = key
    return {"loaded": True, "reused": False, "device": device}


def unload() -> None:
    global _PIPELINE, _LOADED_KEY
    _PIPELINE = None
    _LOADED_KEY = None


def _write_audio(result: Any, output_path: str, *, sample_rate: int = 0) -> str:
    """Persist the engine's return shape to *output_path*.

    Live-validated shapes, most specific first: a BATCH LIST (``generate`` is
    batch-in/batch-out, so one text returns a one-element list — unwrap it), a path,
    bytes, ``(sample_rate, array)``, and a bare audio array written at the pipeline's
    own ``sampling_rate``. Arrays are detected by duck typing (``dtype`` +
    ``__array__``) rather than an ``isinstance`` against numpy, so this module imports
    numpy's stack only when an array actually arrives.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(result, list):
        if len(result) != 1:
            raise RuntimeError(
                f"engine_api_mismatch: expected a single-clip batch, got {len(result)} elements"
            )
        return _write_audio(result[0], output_path, sample_rate=sample_rate)
    if isinstance(result, (str, Path)):
        produced = Path(result)
        if produced != out:
            out.write_bytes(produced.read_bytes())
        return str(out)
    if isinstance(result, (bytes, bytearray)):
        out.write_bytes(bytes(result))
        return str(out)
    if isinstance(result, tuple) and len(result) == 2:
        sample_rate, result = int(result[0]), result[1]
    if hasattr(result, "dtype") and hasattr(result, "__array__"):
        if not sample_rate:
            raise RuntimeError(
                "engine_api_mismatch: engine returned a bare audio array and exposes no sampling rate"
            )
        import soundfile as sf  # rides the engine's own dependency stack

        sf.write(str(out), result, int(sample_rate))
        return str(out)
    raise RuntimeError(f"engine_api_mismatch: unrecognized synthesis return type {type(result).__name__}")


def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    if method != "synthesize":
        raise ValueError(f"unknown method {method!r}")
    if _PIPELINE is None:
        raise RuntimeError("worker not loaded — call load first")
    text = str(payload.get("text") or "")
    if not text:
        raise ValueError("synthesize requires text")
    output_path = str(payload.get("output_path") or "") or tempfile.mkstemp(suffix=".wav", prefix="pc-clone-")[1]

    fn = None
    fn_name = ""
    for name in _SYNTH_METHODS:
        candidate = getattr(_PIPELINE, name, None)
        if callable(candidate):
            fn, fn_name = candidate, name
            break
    if fn is None:
        raise RuntimeError(
            f"engine_api_mismatch: pipeline exposes none of {list(_SYNTH_METHODS)}"
        )

    kwargs = _filtered_kwargs(
        fn,
        {
            "text": text,
            "ref_audio": str(payload.get("ref_audio") or ""),
            "ref_text": str(payload.get("ref_text") or ""),
            "prompt_speech": str(payload.get("ref_audio") or ""),
            "prompt_text": str(payload.get("ref_text") or ""),
            "seed": int(payload.get("seed") or 0),
            "speed": float(payload.get("speed") or 1.0),
            "instruct": str(payload.get("instruct") or ""),
        },
    )
    if "text" not in kwargs:
        result = fn(text, **kwargs)
    else:
        result = fn(**kwargs)
    rate = 0
    for attr in ("sampling_rate", "sample_rate", "sr"):
        value = getattr(_PIPELINE, attr, None)
        if value:
            rate = int(value)
            break
    return {"output_path": _write_audio(result, output_path, sample_rate=rate), "method": fn_name}
