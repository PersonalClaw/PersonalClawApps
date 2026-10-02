"""Speaker diarization in a process of its own: what ``provider.py`` runs through the SDK's
``run_once``.

sherpa-onnx holds the interpreter lock for the whole of a diarization (``process()`` is one C call
that never lets it go). Run in a thread of the gateway, as this app used to run it, a recording's
diarization stopped the gateway answering anything until it was done: two minutes for a six-minute
video. In this process, the lock it holds is this process's own.

Loaded by path in the child, with this app's packages on its import path and no PersonalClaw
import: the standard library, numpy and sherpa-onnx only. It answers facts (the turns, or what
stopped them) and the provider puts them in the words the item shows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess


class _Unreadable(Exception):
    """ffmpeg could not read the recording; the message is ffmpeg's own last word."""


class _NoAudio(Exception):
    """ffmpeg read the recording and found no audio in it."""


def call(method: str, payload: dict) -> dict:
    if method != "diarize":
        raise ValueError(f"unknown method {method!r}")
    return diarize(**payload)


def diarize(
    *, audio: str, ffmpeg: str, segmentation: str, embedding: str, max_speakers: int = 0
) -> dict:
    """The speaker turns in *audio*: ``{"turns": [[start, end, speaker], …]}`` in time order, or
    what stopped them: ``{"unreadable": <ffmpeg's word>}``, ``{"no_audio": True}``, or
    ``{"failed": <the engine's own words>}``."""
    import sherpa_onnx

    clustering = (
        sherpa_onnx.FastClusteringConfig(num_clusters=int(max_speakers))
        if max_speakers
        else sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5)
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=segmentation)
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=embedding),
        clustering=clustering,
        min_duration_on=0.3,
    )
    try:
        engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        samples = decode(audio, engine.sample_rate, ffmpeg)
        found = engine.process(samples).sort_by_start_time()
    except _Unreadable as exc:
        return {"unreadable": str(exc)}
    except _NoAudio:
        return {"no_audio": True}
    except Exception as exc:  # noqa: BLE001 — the engine's own words reach the item
        return {"failed": str(exc) or type(exc).__name__}
    return {"turns": [[float(t.start), float(t.end), int(t.speaker)] for t in found]}


def decode(audio: str, sample_rate: int, ffmpeg: str):
    """The recording as mono float32 samples at *sample_rate*, decoded by the ffmpeg core found, at
    the absolute path *ffmpeg*. It is looked for in that path's folder only: nothing is looked up
    on a ``PATH``, and a path that names any other program runs nothing.

    A recording arrives in whatever format was uploaded (``.m4a``, ``.mp3``, ``.webm``, a video's
    extracted ``.wav``), and the model hears only its own rate: ffmpeg reads every format the
    product accepts and resamples on the way. A file cut off after its header decodes to no
    audio at all, with ffmpeg exiting 0, so that is said rather than diarized as silence."""
    import numpy as np

    program = shutil.which("ffmpeg", path=os.path.dirname(ffmpeg))
    if program is None:
        raise RuntimeError(f"there is no ffmpeg to run at {ffmpeg}")
    decoded = subprocess.run(
        [program, "-nostdin", "-v", "error", "-i", audio, "-vn", "-ac", "1",
         "-ar", str(int(sample_rate)), "-f", "f32le", "-"],
        capture_output=True,
        check=False,
    )
    if decoded.returncode != 0:
        raise _Unreadable(ffmpeg_said(decoded.stderr, decoded.returncode))
    if not decoded.stdout:
        raise _NoAudio()
    return np.frombuffer(decoded.stdout, dtype=np.float32)


def ffmpeg_said(stderr: bytes, status: int) -> str:
    """ffmpeg's last word on a failed decode, for the item's status line: its closing summary
    line, without the ``[in#0 @ 0x…]`` tags it starts lines with. The raw text ran several lines,
    named the recording's storage path, and pushed the other steps' reasons past the line's end."""
    lines = [re.sub(r"^(\[[^\]]*\]\s*)+", "", line).strip()
             for line in stderr.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    return lines[-1] if lines else f"ffmpeg exited with status {status}"
