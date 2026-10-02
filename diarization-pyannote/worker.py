"""Speaker diarization in a process of its own: what ``provider.py`` runs through the SDK's
``run_once``.

pyannote's pipeline computes with torch, which lets the interpreter lock go while it works, and
then groups the voices it heard with scipy's clustering, which holds the lock while it runs:
measured beside a 10 ms event-loop tick, clustering 8,000 voice samples (about what a two-hour
recording gives) held it for 1.16 s at a time. Run in a thread of the gateway, as this app used to
run it, that stopped the gateway answering anything for as long. In this process, the lock it
holds is this process's own.

Loaded by path in the child, with this app's packages on its import path and no PersonalClaw
import. It answers facts (the turns, or what stopped them) and the provider puts them in the
words the item shows.
"""

from __future__ import annotations

import os
import re

# pyannote.audio (from 4.0) reports each model and pipeline it loads, and each file it diarizes,
# to its makers unless this says otherwise, and turns that on as it loads when nothing has set it.
# This process does not inherit the gateway's setting, so it sets its own before the library can
# load: PersonalClaw sends no telemetry.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"


def call(method: str, payload: dict) -> dict:
    if method != "diarize":
        raise ValueError(f"unknown method {method!r}")
    return diarize(**payload)


def diarize(
    *,
    audio: str,
    model: str,
    token: str,
    cache: str,
    num_speakers: int = 0,
    min_speakers: int = 0,
    max_speakers: int = 0,
) -> dict:
    """The speaker turns in *audio*: ``{"turns": [[start, end, speaker], …]}``, or what stopped
    them: ``{"refused": True}`` when Hugging Face would not give the token the pipeline,
    ``{"gated": <repo>}`` when it would not give a model the pipeline pulls in, or
    ``{"failed": <the pipeline's own words>}``.

    The pipeline, and the models it names, load from *cache* (the PersonalClaw home) and download
    there when they are not yet in it."""
    try:
        from pyannote.audio import Pipeline

        # pyannote.audio 3.1 renamed the token's keyword from ``use_auth_token`` to ``token``.
        try:
            pipeline = Pipeline.from_pretrained(model, token=token, cache_dir=cache)
        except TypeError:
            pipeline = Pipeline.from_pretrained(model, use_auth_token=token, cache_dir=cache)
        if pipeline is None:
            return {"refused": True}
        kwargs: dict = {}
        if num_speakers:
            kwargs["num_speakers"] = int(num_speakers)
        if min_speakers:
            kwargs["min_speakers"] = int(min_speakers)
        if max_speakers:
            kwargs["max_speakers"] = int(max_speakers)
        found = pipeline(audio, **kwargs)
        # pyannote.audio 4.x wraps the result in a ``DiarizeOutput`` whose ``.speaker_diarization``
        # is the ``Annotation``; 3.x returned the ``Annotation`` itself.
        annotation = getattr(found, "speaker_diarization", found)
        return {
            "turns": [
                [float(turn.start), float(turn.end), str(speaker)]
                for turn, _track, speaker in annotation.itertracks(yield_label=True)
            ]
        }
    except Exception as exc:  # noqa: BLE001 — the pipeline's own words reach the item
        words = str(exc)
        # A gated model the token may not download: pyannote.audio 4.x pulls in an embedding
        # model whose conditions are accepted separately, so the repository is named.
        if "gated" in words.lower() or "403" in words or "accept" in words.lower():
            named = re.search(r"(pyannote/[\w.-]+)", words)
            return {"gated": named.group(1) if named else ""}
        return {"failed": words or type(exc).__name__}
