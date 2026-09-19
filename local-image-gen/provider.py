"""Fully-local image generation — an ``ImageGenProvider`` over a local ComfyUI runtime.

Why this app exists: every other image backend PersonalClaw can bind is a hosted
one (FAL, OpenAI-Images, Bedrock, …), so "generate an image" always meant "send the
prompt to somebody else's GPU". This bundle closes that: with a ComfyUI server
running on the user's own machine and a checkpoint sitting in its ``models/``
directory, generation happens on the host and no prompt or pixel leaves it.

Three things it deliberately does NOT do:

* **It does not ship weights.** The app is source only; the model is user-pulled
  into ComfyUI's own models directory. That is what keeps it off the packaging
  size budget — see ``test_provider.py::test_no_model_artifact_ships_in_the_bundle``.
* **It does not add a generation path.** It implements the existing
  ``ImageGenProvider`` ABC and is registered by the existing ``type: model``
  manifest seam against the existing ``image_gen`` capability, exactly as the
  ``fal-image`` bundle is. ``image_generate`` therefore still runs through core's
  one SEL-audited ``_image_generate`` dispatch; this file contains no tool, no
  route, and no audit call of its own.
* **It does not reach the public internet.** The endpoint is operator-configured,
  so it is guarded by a loopback/private-only egress policy (see
  :data:`_LOCAL_ONLY`) before any request is made. A "local" backend pointed at a
  public host would be an SSRF primitive wearing a local backend's name.

Licence discipline (C9): the *recommended* default is a genuinely OSI-permissive
model, and the catalog records why each disqualified model is disqualified. The
model tag alone is not trusted — see :data:`_CATALOG` and :func:`is_permissive`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.net import EgressPolicy, fetch

logger = logging.getLogger(__name__)

#: The bundled app manifest name — the key its saved Settings card is stored under.
_MANIFEST_NAME = "local-image-gen"

_DEFAULT_ENDPOINT = "http://127.0.0.1:8188"

# ── Egress posture ───────────────────────────────────────────────────────────

#: Loopback/private-only egress for the operator-configured runtime endpoint.
#:
#: ``loopback_only`` inverts the default public-hosts-only stance: it ALLOWS
#: 127.0.0.1 and DENIES every public host. That is the correct posture for a
#: backend whose entire purpose is to stay on the machine, and it is also the SSRF
#: control for a URL that comes out of user config — the three mitigations that
#: matter here are all carried by the policy object rather than hand-rolled:
#: host classification (``loopback_only``), DNS-rebinding defeat
#: (``pin_resolved_ip`` — the resolved IP is pinned, so a second lookup cannot
#: swing the connection to a public address), and bounded redirects
#: (``max_redirects`` — a local endpoint has no legitimate reason to redirect, so
#: it is pinned to 0 rather than left at the 5 the base policy allows).
#:
#: ``timeout_s``/``max_bytes`` are widened from the defaults on purpose: a
#: diffusion step loop is slow and a PNG is large. They are still finite.
#:
#: This must stay NARROWER than core's CONNECTOR policy, never wider — a rail in
#: ``test_provider.py`` asserts the two loopback flags and the zero redirect cap,
#: so a later "just let it reach a remote GPU box" edit fails the build instead of
#: silently converting a local backend into an egress path.
_LOCAL_ONLY = EgressPolicy(
    name="local-image-runtime",
    allow_schemes=("http", "https"),
    allow_private=True,
    loopback_only=True,
    max_redirects=0,
    max_bytes=64_000_000,
    timeout_s=600.0,
    pin_resolved_ip=True,
    on_violation="deny",
)

# ── Licence catalog (C9) ─────────────────────────────────────────────────────

#: The ONLY licence ids this app will default-recommend. Genuinely OSI-permissive,
#: per the Chairman's C9 constraint. Not a style preference: PersonalClaw binds C9
#: on whatever it *recommends*, so a RAIL / non-commercial / revenue-gated /
#: research-only default would be a licence violation shipped in help copy.
PERMISSIVE_LICENCES = frozenset({"apache-2.0", "mit"})


@dataclass(frozen=True)
class LocalImageModel:
    """A ComfyUI checkpoint this app knows how to talk about.

    ``checkpoint`` is the filename as it appears in ComfyUI's
    ``models/checkpoints`` directory — that string, not a vendor model id, is what
    the runtime is addressed by.

    ``license`` is the model card's verbatim licence tag. ``disqualified`` is the
    reason the model must never be default-recommended even when its tag looks
    fine; an empty string means nothing disqualifies it. Keeping these two fields
    SEPARATE is the whole point of the catalog: for Sana the tag reads
    ``apache-2.0`` and the model is still disqualified twice over, so a check that
    trusts the tag alone would wave it through.
    """

    name: str
    checkpoint: str
    license: str
    description: str = ""
    sizes: tuple[str, ...] = ("1024x1024", "1024x768", "768x1024")
    disqualified: str = ""
    approx_gb: float = 0.0


#: Licence facts are as recorded in the IG-1 feasibility study (HF model cards,
#: verified 2026-09-16). Do NOT edit a licence here from memory — re-read the card.
_CATALOG: tuple[LocalImageModel, ...] = (
    # ── Permissive: safe to recommend ────────────────────────────────────────
    LocalImageModel(
        name="flux.1-schnell",
        checkpoint="flux1-schnell.safetensors",
        license="apache-2.0",
        description="FLUX.1 [schnell] — 12B rectified-flow, high quality in 1-4 steps. "
        "The recommended local default: genuinely Apache-2.0 and the best "
        "quality-per-gigabyte of the permissive set.",
        approx_gb=6.8,
    ),
    LocalImageModel(
        name="qwen-image",
        checkpoint="qwen-image.safetensors",
        license="apache-2.0",
        description="Qwen-Image — 20B foundation model, strongest of the permissive set at "
        "rendering legible text inside the image. Much larger than schnell.",
        approx_gb=40.0,
    ),
    LocalImageModel(
        name="chroma",
        checkpoint="chroma.safetensors",
        license="apache-2.0",
        description="Chroma — Apache-2.0 text-to-image. Its model card is flagged "
        "Not-For-All-Audiences, so it is offered but never default-recommended.",
        approx_gb=5.5,
    ),
    # ── C9-disqualified. Present so the refusal is explicit and testable: a
    #    silently-absent trap teaches nobody and cannot be asserted against.
    LocalImageModel(
        name="flux.1-dev",
        checkpoint="flux1-dev.safetensors",
        license="flux-1-dev-non-commercial-license",
        description="FLUX.1 [dev] — higher fidelity than schnell.",
        disqualified="weights are under a non-commercial licence",
        approx_gb=6.8,
    ),
    LocalImageModel(
        name="sdxl-base-1.0",
        checkpoint="sd_xl_base_1.0.safetensors",
        license="openrail++",
        description="Stable Diffusion XL base 1.0.",
        disqualified="CreativeML Open RAIL++-M carries behavioural use restrictions — "
        "use-restricted, not OSI",
        approx_gb=6.9,
    ),
    LocalImageModel(
        name="sd-1.5",
        checkpoint="v1-5-pruned-emaonly.safetensors",
        license="openrail",
        description="Stable Diffusion 1.5.",
        disqualified="CreativeML Open RAIL-M carries behavioural use restrictions — "
        "use-restricted, not OSI",
        approx_gb=4.3,
    ),
    LocalImageModel(
        name="sd-3.5-large",
        checkpoint="sd3.5_large.safetensors",
        license="stabilityai-ai-community",
        description="Stable Diffusion 3.5 Large.",
        disqualified="Stability Community Licence is revenue-gated (an Enterprise "
        "Licence is required above $1M annual revenue) — not permissive",
        approx_gb=16.0,
    ),
    LocalImageModel(
        name="sdxl-turbo",
        checkpoint="sd_xl_turbo_1.0_fp16.safetensors",
        license="sai-nc-community",
        description="SDXL-Turbo — 1-step distillation.",
        disqualified="non-commercial licence",
        approx_gb=6.9,
    ),
    LocalImageModel(
        name="sana-1.6b",
        checkpoint="sana_1600m.safetensors",
        # The tag really does read apache-2.0 on the card. It is still disqualified.
        license="apache-2.0",
        description="Sana 1.6B — small and fast at 1024px.",
        disqualified="the apache-2.0 tag covers the code only: the composed "
        "Gemma-2-2B-IT text encoder binds Google's Gemma Terms, and the model "
        "card states research purposes only",
        approx_gb=3.3,
    ),
    LocalImageModel(
        name="janus-pro-7b",
        checkpoint="janus_pro_7b.safetensors",
        # The repo's *code* is MIT; the weights are not. Same trap shape as Sana.
        license="mit",
        description="Janus-Pro-7B — unified understand+generate.",
        disqualified="the mit tag covers the code only: the weights ship under the "
        "DeepSeek Model License, which is not OSI",
        approx_gb=14.0,
    ),
)

#: The single model the setup/help copy points a new user at. Must be permissive.
RECOMMENDED_MODEL = "flux.1-schnell"


def catalog() -> tuple[LocalImageModel, ...]:
    """Every checkpoint this app knows, permissive and disqualified alike."""
    return _CATALOG


def by_name(name: str) -> LocalImageModel | None:
    """Catalog lookup by model name OR by raw checkpoint filename."""
    key = (name or "").strip().lower()
    for m in _CATALOG:
        if key in (m.name.lower(), m.checkpoint.lower()):
            return m
    return None


def is_permissive(model: LocalImageModel) -> bool:
    """Whether ``model`` may be RECOMMENDED — both gates, not just the tag.

    A model qualifies only when its licence id is on :data:`PERMISSIVE_LICENCES`
    **and** nothing disqualifies it. The second gate is not redundant: Sana's tag
    is ``apache-2.0`` and Janus-Pro's is ``mit``, and both are disqualified by
    something the tag does not mention (a use-restricted composed text encoder; a
    separate weights licence). Dropping the ``disqualified`` gate would make this
    function return True for both.
    """
    return model.license.strip().lower() in PERMISSIVE_LICENCES and not model.disqualified


def recommended_model() -> LocalImageModel:
    """The default-recommended model, or raise if it is not a permissive one.

    Raising rather than returning a fallback is deliberate: a build whose
    recommendation is un-recommendable should fail loudly at import of the help
    copy, not quietly point users at a licence they cannot honour.
    """
    model = by_name(RECOMMENDED_MODEL)
    if model is None:
        raise ImageGenError(f"recommended model {RECOMMENDED_MODEL!r} is not in the catalog")
    if not is_permissive(model):
        raise ImageGenError(
            f"recommended model {model.name!r} is not permissively licensed "
            f"({model.license}{'; ' + model.disqualified if model.disqualified else ''})"
        )
    return model


def setup_help() -> str:
    """The setup/help copy — names exactly one model, and only a permissive one."""
    rec = recommended_model()
    others = [m for m in _CATALOG if is_permissive(m) and m.name != rec.name]
    lines = [
        "Generate images entirely on this machine — nothing is sent to a cloud provider.",
        "",
        "1. Install and start ComfyUI (https://github.com/comfyanonymous/ComfyUI). "
        f"Leave it on its default address, {_DEFAULT_ENDPOINT}, or set yours in this "
        "app's settings.",
        f"2. Download {rec.name} ({rec.license}, about {rec.approx_gb:.1f} GB) into "
        "ComfyUI's models/checkpoints directory. PersonalClaw does not ship or fetch "
        "model weights — you pull the model you want.",
        "3. Bind it to the Image Generation use case in Settings -> Models.",
        "",
        f"Recommended: {rec.name} — {rec.description}",
    ]
    if others:
        lines.append(
            "Also permissively licensed: " + ", ".join(f"{m.name} ({m.license})" for m in others)
        )
    lines.append(
        "Other checkpoints will work if ComfyUI can load them, but PersonalClaw will "
        "not recommend one whose licence is non-commercial, revenue-gated, "
        "research-only, or use-restricted."
    )
    return "\n".join(lines)


# ── Settings ─────────────────────────────────────────────────────────────────


def _resolve_endpoint(configured: str = "") -> str:
    """The runtime endpoint: explicit argument, then the saved card, then default."""
    if configured.strip():
        return configured.strip().rstrip("/")
    try:
        from personalclaw.sdk.settings import ProviderSettings

        saved = str(ProviderSettings.load(_MANIFEST_NAME).get("endpoint", "") or "")
        if saved.strip():
            return saved.strip().rstrip("/")
    except Exception:  # noqa: BLE001 — an unreadable card falls back to the default
        logger.debug("could not read %s settings; using default endpoint", _MANIFEST_NAME)
    return _DEFAULT_ENDPOINT


def _parse_size(size: str, fallback: tuple[int, int] = (1024, 1024)) -> tuple[int, int]:
    """``"1024x768"`` → ``(1024, 768)``, rounded to the multiple of 8 the sampler needs."""
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*", size or "")
    if not m:
        return fallback
    w, h = int(m.group(1)), int(m.group(2))
    return (max(64, w - w % 8), max(64, h - h % 8))


# ── ComfyUI graph ────────────────────────────────────────────────────────────


def build_graph(
    *,
    checkpoint: str,
    prompt: str,
    width: int,
    height: int,
    steps: int,
    seed: int,
    negative: str = "",
) -> dict[str, Any]:
    """The minimal ComfyUI text-to-image graph, as an API-format prompt object.

    Kept to ComfyUI's canonical single-file-checkpoint nodes
    (``CheckpointLoaderSimple`` → CLIP encode ×2 → ``EmptyLatentImage`` →
    ``KSampler`` → ``VAEDecode`` → ``SaveImage``) so it loads any all-in-one
    checkpoint, including the FLUX.1-schnell fp8 single file. A model needing a
    bespoke node layout (separate T5/CLIP/VAE files) should be driven from a
    ComfyUI workflow of the user's own instead; this app does not try to
    synthesise every possible graph.
    """
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "personalclaw", "images": ["6", 0]},
        },
    }


# ── Provider ─────────────────────────────────────────────────────────────────


class LocalComfyImageProvider(ImageGenProvider):
    """Generate images through a ComfyUI server on this machine.

    Registered by the ``local-image-gen`` manifest exactly as ``fal-image``
    registers ``FalImageProvider``: a ``type: model`` provider declaring
    ``capabilities: ["image_gen"]``, so core's ``ModelTypeHandler`` puts it in the
    one ``image_gen`` registry that ``active_image_gen`` resolves. Nothing here
    knows about the ``image_generate`` tool.
    """

    def __init__(self, *, endpoint: str = "", steps: int = 4) -> None:
        self._endpoint = _resolve_endpoint(endpoint)
        self._steps = max(1, int(steps or 4))

    @property
    def name(self) -> str:
        return "local-image"

    @property
    def display_name(self) -> str:
        return "Local (ComfyUI)"

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def info(self) -> dict[str, Any]:
        info = super().info()
        info["endpoint"] = self._endpoint
        info["local"] = True
        return info

    # ── HTTP, all of it loopback-guarded ─────────────────────────────────────

    async def _get(self, path: str) -> Any:
        """GET ``path`` from the runtime and parse JSON. Raises ImageGenError."""
        resp = await _guarded_fetch(f"{self._endpoint}{path}")
        if resp.status != 200:
            raise ImageGenError(
                f"Local image runtime returned HTTP {resp.status} for {path}. "
                f"Is ComfyUI running at {self._endpoint}?"
            )
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, ValueError) as e:
            raise ImageGenError(
                f"Local image runtime sent an unparseable response for {path}."
            ) from e

    async def installed_checkpoints(self) -> list[str]:
        """Checkpoint filenames ComfyUI can actually load right now.

        Empty when the runtime is up but the user has pulled no weights yet —
        which is the state clause 5 of IG-1 cares about, and is a calm empty list
        rather than an error.
        """
        data = await self._get("/object_info/CheckpointLoaderSimple")
        try:
            node = data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"]
            names = node[0] if isinstance(node, list) and node else []
            return [str(n) for n in names] if isinstance(names, list) else []
        except (KeyError, IndexError, TypeError):
            return []

    async def is_available(self) -> bool:
        """True when a ComfyUI server answers on the configured endpoint.

        Deliberately does NOT require a downloaded checkpoint: "runtime present,
        no weights yet" must read as available-but-empty so the surface can say so,
        not as a dead provider.
        """
        try:
            resp = await _guarded_fetch(f"{self._endpoint}/system_stats")
        except ImageGenError:
            return False
        return resp.status == 200

    async def list_models(self) -> list[ImageGenModel]:
        """Catalog entries, with ``downloaded`` answered from the live runtime.

        Never raises: an unreachable runtime yields the catalog with everything
        marked not-downloaded, which is exactly the calm no-model state.
        """
        from personalclaw.sdk.image import active_image_gen

        resolved = active_image_gen()
        active_model = resolved[1] if resolved and resolved[0].name == self.name else ""

        try:
            installed = {c.lower() for c in await self.installed_checkpoints()}
        except ImageGenError:
            installed = set()

        out: list[ImageGenModel] = []
        for m in _CATALOG:
            note = "" if is_permissive(m) else f" NOT RECOMMENDED: {m.disqualified}."
            out.append(
                ImageGenModel(
                    name=m.name,
                    description=f"{m.description} Licence: {m.license}.{note}",
                    sizes=list(m.sizes),
                    supports_edit=False,
                    downloaded=m.checkpoint.lower() in installed,
                    active=m.name == active_model,
                )
            )
        # A checkpoint the user pulled that the catalog has never heard of is still
        # bindable — the catalog is advice, not an allowlist on what may RUN. Only
        # what may be RECOMMENDED is gated (C9 binds recommendation).
        known = {m.checkpoint.lower() for m in _CATALOG}
        for ckpt in sorted(installed - known):
            out.append(
                ImageGenModel(
                    name=ckpt,
                    description="Installed in ComfyUI; licence unknown to PersonalClaw. "
                    "Check the model's own licence before using its output.",
                    sizes=["1024x1024"],
                    supports_edit=False,
                    downloaded=True,
                    active=ckpt == active_model,
                )
            )
        return out

    # ── Generation ───────────────────────────────────────────────────────────

    async def _resolve_checkpoint(self, model: str) -> str:
        """Map a bound model id to a checkpoint ComfyUI has, or explain what is missing.

        This is where the not-yet-pulled case becomes a calm sentence. Core's
        ``_image_generate`` catches ``ImageGenError`` and returns its message as
        tool output, so every raise below is a readable answer, never a 500.
        """
        entry = by_name(model)
        wanted = entry.checkpoint if entry else (model or "").strip()
        if not wanted:
            rec = recommended_model()
            raise ImageGenError(
                "No local image model is selected. "
                f"Pull {rec.name} ({rec.license}) into ComfyUI's models/checkpoints "
                "directory and bind it in Settings -> Models."
            )

        try:
            installed = await self.installed_checkpoints()
        except ImageGenError as e:
            raise ImageGenError(
                f"The local image runtime is not reachable at {self._endpoint}, so "
                f"{wanted!r} cannot be used yet. Start ComfyUI and try again. ({e})"
            ) from e

        for name in installed:
            if name.lower() == wanted.lower():
                return name
        if not installed:
            rec = recommended_model()
            raise ImageGenError(
                "The local image runtime is running but has no model weights yet. "
                f"Download {rec.name} ({rec.license}, about {rec.approx_gb:.1f} GB) into "
                "ComfyUI's models/checkpoints directory, then try again. PersonalClaw "
                "does not ship or fetch model weights."
            )
        raise ImageGenError(
            f"{wanted!r} is not installed in the local image runtime. "
            f"Available: {', '.join(sorted(installed))}."
        )

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        import secrets

        checkpoint = await self._resolve_checkpoint(model)
        width, height = _parse_size(size)
        steps = max(1, int(opts.get("steps") or self._steps))

        results: list[ImageResult] = []
        # One queued job per image: ComfyUI batches via batch_size, but a batch shares
        # a seed lineage and a single failure loses the lot. Sequential keeps each
        # image independently attributable, which matters more than throughput here.
        for _ in range(max(1, int(n or 1))):
            graph = build_graph(
                checkpoint=checkpoint,
                prompt=prompt,
                width=width,
                height=height,
                steps=steps,
                seed=secrets.randbelow(2**32),
                negative=str(opts.get("negative_prompt") or ""),
            )
            prompt_id = await self._queue(graph)
            refs = await self._await_images(prompt_id)
            for ref in refs:
                results.append(await self._download(ref))
        if not results:
            raise ImageGenError("The local image runtime produced no image.")
        return results

    async def edit(
        self,
        prompt: str,
        *,
        source_image: str,
        mask: str = "",
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        """Not supported — img2img needs a different graph per checkpoint family.

        Declared unsupported rather than approximated: ``supports_edit=False`` on
        every catalog entry already tells the surface this, and the ABC's contract
        for a provider without an edit endpoint is to raise.
        """
        raise ImageGenError(
            "The local image backend does not support editing an existing image yet. "
            "Generate a new image instead, or bind a cloud image model for edits."
        )

    async def _queue(self, graph: dict[str, Any]) -> str:
        """POST the graph to ``/prompt`` and return the queued prompt id."""
        body = json.dumps({"prompt": graph}).encode()
        resp = await _guarded_fetch(
            f"{self._endpoint}/prompt",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=body,
        )
        if resp.status not in (200, 201):
            detail = ""
            try:
                payload = json.loads(resp.text)
                detail = str(payload.get("error") or payload.get("node_errors") or "")
            except (json.JSONDecodeError, ValueError, AttributeError):
                detail = resp.text[:400]
            raise ImageGenError(
                f"The local image runtime rejected the job (HTTP {resp.status}). {detail}".strip()
            )
        try:
            prompt_id = str(json.loads(resp.text).get("prompt_id") or "")
        except (json.JSONDecodeError, ValueError) as e:
            raise ImageGenError("The local image runtime sent an unparseable queue reply.") from e
        if not prompt_id:
            raise ImageGenError("The local image runtime queued no job (no prompt_id).")
        return prompt_id

    async def _await_images(self, prompt_id: str) -> list[dict[str, str]]:
        """Poll ``/history/<id>`` until the job finishes; return its image refs.

        Owns its own poll loop inside the coroutine, as the ABC requires, bounded
        by :data:`_POLL_TIMEOUT_S`. A local GPU is slow but not infinite; a job that
        outlives the ceiling is reported as a timeout rather than hanging the turn.
        """
        import asyncio

        waited = 0.0
        while waited < _POLL_TIMEOUT_S:
            history = await self._get(f"/history/{prompt_id}")
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                if str(status.get("status_str", "")).lower() == "error":
                    raise ImageGenError("The local image runtime reported a job error.")
                refs = _image_refs(entry.get("outputs") or {})
                if refs:
                    return refs
                if status.get("completed") is True:
                    raise ImageGenError("The local image job completed with no image output.")
            await asyncio.sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
        raise ImageGenError(
            f"The local image job did not finish within {int(_POLL_TIMEOUT_S)}s. "
            "A first run can be slow while the model loads into memory — try again."
        )

    async def _download(self, ref: dict[str, str]) -> ImageResult:
        """Fetch one rendered image's bytes and return it as inline base64.

        Inline rather than a URL: the runtime's ``/view`` URL is only reachable from
        this host, so handing core a URL would produce an artifact whose bytes
        cannot be re-fetched later. Base64 makes the result self-contained.
        """
        import base64
        from urllib.parse import urlencode

        query = urlencode(
            {
                "filename": ref.get("filename", ""),
                "subfolder": ref.get("subfolder", ""),
                "type": ref.get("type", "output"),
            }
        )
        resp = await _guarded_fetch(f"{self._endpoint}/view?{query}")
        if resp.status != 200 or not resp.body:
            raise ImageGenError(
                f"Could not read the generated image back from the local runtime "
                f"(HTTP {resp.status})."
            )
        mime = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
        return ImageResult(
            b64=base64.b64encode(resp.body).decode(),
            mime=mime or "image/png",
        )


#: Poll cadence + ceiling for a local job. Generous: a cold start pages several
#: gigabytes off disk before the first step runs.
_POLL_INTERVAL_S = 1.5
_POLL_TIMEOUT_S = 900.0


def _image_refs(outputs: Any) -> list[dict[str, str]]:
    """Pull ``{filename, subfolder, type}`` refs out of a ComfyUI outputs blob."""
    refs: list[dict[str, str]] = []
    if not isinstance(outputs, dict):
        return refs
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for img in node.get("images") or []:
            if isinstance(img, dict) and img.get("filename"):
                refs.append(
                    {
                        "filename": str(img.get("filename", "")),
                        "subfolder": str(img.get("subfolder", "") or ""),
                        "type": str(img.get("type", "") or "output"),
                    }
                )
    return refs


async def _guarded_fetch(url: str, **kw: Any) -> Any:
    """``fetch`` under :data:`_LOCAL_ONLY`, with egress refusals explained.

    Every outbound request this app makes goes through here, so there is exactly
    one place the policy is applied and no path that can skip it.
    """
    from personalclaw.sdk.net import EgressBlocked

    try:
        return await fetch(url, policy=_LOCAL_ONLY, **kw)
    except EgressBlocked as e:
        raise ImageGenError(
            f"Refused to reach {url!r}: the local image backend may only talk to an "
            f"address on this machine or private network. ({e})"
        ) from e
    except ImageGenError:
        raise
    except Exception as e:  # noqa: BLE001 — a transport error must read as a calm message
        raise ImageGenError(
            f"Could not reach the local image runtime at {url!r}: {type(e).__name__}: {e}"
        ) from e


# ── Factory (manifest entry point) ───────────────────────────────────────────


def create_provider(config: dict[str, Any] | None = None) -> LocalComfyImageProvider:
    """Manifest factory — the ``provider:create_provider`` entry point.

    ``ModelTypeHandler`` calls this on enable with the saved card config, and
    registers the result into the ``image_gen`` registry because the manifest
    declares that capability. Same seam, same call shape as ``fal-image``.
    """
    cfg = config or {}
    return LocalComfyImageProvider(
        endpoint=str(cfg.get("endpoint", "") or ""),
        steps=int(cfg.get("steps") or 4),
    )


def availability() -> tuple[bool, str]:
    """Whether this backend can be useful here — a static, import-time answer.

    Always installable (it adds no Python dependency of its own); the runtime it
    talks to is the user's to install, which is what the message says.
    """
    return True, (
        "Needs a ComfyUI server running on this machine with at least one checkpoint "
        f"in models/checkpoints. Recommended: {RECOMMENDED_MODEL}."
    )
