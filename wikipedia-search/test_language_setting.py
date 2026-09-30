"""Wikipedia: the Language setting is a language code, and only a code goes into the hostname.

The code names the wiki: ``https://<code>.wikipedia.org``. A value that is not one would name
another host entirely (``evil.example/#`` makes the request go to ``evil.example``), so anything
else is refused when the setting is saved, by the settings schema the save path checks, and
again when a search would use it, before any request is made.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider  # noqa: E402
from apps_testkit.egress import HOST, ProviderHost, owner_egress  # noqa: E402
from personalclaw.sdk.settings import ProviderSettings  # noqa: E402

_SCHEMA = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))[
    "provider"
]["settingsSchema"]

#: Language codes as Wikipedia names its editions, one with a variant, and ways an owner types one.
_CODES = ["en", "de", "ja", "pt-br", "zh-min-nan", "simple", "be-tarask", "EN", " fr "]

#: Values that are not a code: a hostname, a URL, a path, an address, two codes, and malformed ones.
_NOT_CODES = [
    "en.wikipedia.org",
    "https://de.wikipedia.org",
    "evil.example/#",
    "en.evil.example",
    "127.0.0.1:8080",
    "en/../w",
    "de fr",
    "en_gb",
    "x",
    "en-",
    "-en",
    "abcdefghijklm",
]


@pytest.mark.parametrize("lang", [*_CODES, ""])
def test_a_language_code_is_saved(lang):
    """Left empty, the setting is English."""
    assert ProviderSettings.validate({"lang": lang}, _SCHEMA) == []


@pytest.mark.parametrize("lang", _NOT_CODES)
def test_anything_else_is_refused_when_it_is_saved(lang):
    assert ProviderSettings.validate({"lang": lang}, _SCHEMA) == [
        "Language: does not match the required format"
    ]


def test_the_save_and_the_search_hold_the_same_rule():
    """One rule, written twice (the manifest cannot import it): the schema's pattern is the
    provider's."""
    assert _SCHEMA["properties"]["lang"]["pattern"] == provider._LANGUAGE_CODE.pattern


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", _NOT_CODES)
async def test_anything_else_is_refused_when_a_search_uses_it_and_nothing_is_sent(
    monkeypatch, lang
):
    """A value saved before the rule, or written into the settings file by hand, is refused
    too."""
    with ProviderHost("{}") as wiki:
        monkeypatch.setattr(provider, "_API", f"{wiki.url}/w/api.php")
        owner_egress(allow_hosts=[HOST])
        with pytest.raises(RuntimeError) as refused:
            await provider.WikipediaProvider(lang=lang).search("rust")
        assert str(refused.value) == (
            f"Wikipedia Search's Language is {lang.strip()!r}, which is not a Wikipedia language "
            "code such as en, de or pt-br. Change it in Settings → Providers → Wikipedia Search."
        )
        assert wiki.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("lang", "host"), [
    ("de", "de.wikipedia.org"), (" PT-BR ", "pt-br.wikipedia.org"), ("", "en.wikipedia.org"),
])
async def test_a_language_code_names_its_wiki(monkeypatch, lang, host):
    """The request goes to the edition the code names, lowercased; nothing is sent anywhere."""
    asked: list[str] = []

    class _Answer:
        status = 200
        text = json.dumps({"query": {"pages": {}}})

    async def recording_fetch(url, **kwargs):
        asked.append(url)
        return _Answer()

    monkeypatch.setattr("personalclaw.sdk.net.fetch", recording_fetch)
    await provider.WikipediaProvider(lang=lang).search("rust")
    assert [url.split("?")[0] for url in asked] == [f"https://{host}/w/api.php"]
