"""Azure AI Speech: an audio file to text with speakers, in one request (fast transcription).

TAG's own Azure subscription, so recordings of client meetings are processed under the same
Microsoft agreement as its email and Teams. Needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION
(from the Speech resource in the Azure portal). Files up to 2 hours / 300 MB.
"""

from __future__ import annotations

import json
import mimetypes
import os
from typing import Any

import requests

API_VERSION = "2025-10-15"   # the first version that takes a phrase list (2024-11-15 rejects it)


class SpeechError(RuntimeError):
    pass


class AzureSpeech:
    def __init__(self, key: str, region: str, locale: str = "en-US", max_speakers: int = 8,
                 session: requests.Session | None = None, timeout: float = 600.0) -> None:
        self.key, self.region, self.locale, self.max_speakers = key, region, locale, max_speakers
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "AzureSpeech | None":
        key, region = os.environ.get("AZURE_SPEECH_KEY", ""), os.environ.get("AZURE_SPEECH_REGION", "")
        return cls(key, region) if key and region else None

    def transcribe(self, path: str, phrases: list[str] | None = None) -> dict[str, Any]:
        """{'phrases': [{'speaker', 'offsetMilliseconds', 'text'}...], 'combinedPhrases': [...]}.
        `phrases`: names to expect ("Peconic Landing", "Mitel"), so they're spelled right."""
        url = (f"https://{self.region}.api.cognitive.microsoft.com/speechtotext/transcriptions:transcribe"
               f"?api-version={API_VERSION}")
        definition: dict[str, Any] = {"locales": [self.locale],
                                      "diarization": {"enabled": True, "maxSpeakers": self.max_speakers},
                                      "profanityFilterMode": "None"}
        if phrases:
            definition["phraseList"] = {"phrases": list(phrases)[:500]}
        resp = self._post(url, path, definition)
        if resp.status_code == 400 and phrases:
            # A phrase Azure won't take shouldn't cost the transcript: try once without the list
            definition.pop("phraseList")
            resp = self._post(url, path, definition)
        if not resp.ok:
            raise SpeechError(f"Azure Speech {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _post(self, url: str, path: str, definition: dict[str, Any]) -> requests.Response:
        kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as audio:
            return self.session.post(url, headers={"Ocp-Apim-Subscription-Key": self.key},
                                     files={"audio": (os.path.basename(path), audio, kind),
                                            "definition": (None, json.dumps(definition), "application/json")},
                                     timeout=self.timeout)
