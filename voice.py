"""One small door to ElevenLabs: voice in (speech to text) and voice out (text to speech).

Mirrors llm.py. The key comes from .env (ELEVENLABS_API_KEY), tests plug in a fake with
set_backend, and every failure becomes a VoiceUnavailable whose message is safe to show.
One key covers both directions. The browser never sees it: app.py proxies both calls, so
the page's Content-Security-Policy can stay at connect-src 'self'.

Without a key the app still talks and listens, using the browser's own voice features.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict


class VoiceUnavailable(Exception):
    """The voice service is not connected or did not respond. The message is safe to show to a person."""


KEY_VAR = "ELEVENLABS_API_KEY"
NOT_CONNECTED = "Voice isn't switched on (no ElevenLabs key set on the server)."
DEFAULT_VOICE_ID = "IKne3meq5aSn9XLyUdCD"  # "Charlie", the premade Australian voice. Override with ELEVENLABS_VOICE_ID.
DEFAULT_TTS_MODEL = "eleven_flash_v2_5"  # fast and cheap. eleven_multilingual_v2 speaks more languages.
DEFAULT_STT_MODEL = "scribe_v2"
OUTPUT_FORMAT = "mp3_44100_128"
MAX_TTS_CHARS = 2000  # longest text spoken in one go (an answer is about 300 characters)
MAX_STT_BYTES = 25 * 1024 * 1024
CACHE_ENTRIES = 200  # spoken answers kept in memory, so "Listen again" costs nothing


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


def api_key() -> str:
    return (os.getenv(KEY_VAR) or "").strip()


def voice_label() -> str:
    return "ElevenLabs"


class ElevenLabsBackend:
    def __init__(self, client=None) -> None:
        self._client = client

    @property
    def voice_id(self) -> str:
        return _env("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)

    @property
    def tts_model(self) -> str:
        return _env("ELEVENLABS_TTS_MODEL", DEFAULT_TTS_MODEL)

    @property
    def stt_model(self) -> str:
        return _env("ELEVENLABS_STT_MODEL", DEFAULT_STT_MODEL)

    def client(self):
        if self._client is None:
            key = api_key()
            if not key:
                raise VoiceUnavailable(NOT_CONNECTED)
            try:
                from elevenlabs.client import ElevenLabs
            except ImportError:
                raise VoiceUnavailable("The ElevenLabs package isn't installed. Run: python -m pip install -r requirements.txt")
            self._client = ElevenLabs(api_key=key, timeout=60)
        return self._client

    def tts(self, text: str, language_code: str | None = None) -> bytes:
        """Return MP3 bytes for `text`."""
        client = self.client()
        kwargs = dict(voice_id=self.voice_id, text=text, model_id=self.tts_model, output_format=OUTPUT_FORMAT)
        if language_code and "v2_5" in self.tts_model:  # only the v2.5 models accept a language hint
            kwargs["language_code"] = language_code
        try:
            return b"".join(client.text_to_speech.convert(**kwargs))
        except Exception as exc:
            raise VoiceUnavailable(self._explain(exc)) from exc

    def stt(self, data: bytes, filename: str = "speech.webm", content_type: str = "audio/webm", language_code: str | None = None) -> dict:
        """Return {"text": ..., "language_code": ...} for a recording."""
        client = self.client()
        try:
            out = client.speech_to_text.convert(
                model_id=self.stt_model,
                file=(filename, data, content_type),
                language_code=language_code or None,
                tag_audio_events=False,
            )
        except Exception as exc:
            raise VoiceUnavailable(self._explain(exc)) from exc
        return {"text": (getattr(out, "text", "") or "").strip(), "language_code": getattr(out, "language_code", None)}

    @staticmethod
    def _explain(exc) -> str:
        code = getattr(exc, "status_code", None)
        text = f"{getattr(exc, 'body', '')} {exc}".lower()
        if code in (401, 403) or "invalid_api_key" in text or "unauthorized" in text:
            return "ElevenLabs didn't accept the API key. Check ELEVENLABS_API_KEY in your .env file."
        if code == 429 or "quota" in text or "exceeded" in text:
            return "ElevenLabs says this key has used up its quota or is going too fast. Wait a minute, or add credits to the ElevenLabs account."
        if code == 404:
            return "ElevenLabs doesn't know that voice or model. Check ELEVENLABS_VOICE_ID and ELEVENLABS_TTS_MODEL in .env."
        if code in (400, 422):
            return "ElevenLabs couldn't use that. If this was voice input, try holding the button and speaking a little longer."
        return "The voice service didn't respond. Please try again in a moment."


# ------------------------------------------------------------------ front door

_injected = None  # a fake plugged in by tests
_default = None  # the real provider, created on first use
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_lock = threading.Lock()


def set_backend(backend) -> None:
    """Used by tests to plug in a fake. Pass None to go back to the real provider."""
    global _injected, _default
    _injected = backend
    _default = None
    clear_cache()


def get_backend():
    global _default
    if _injected is not None:
        return _injected
    if _default is None:
        _default = ElevenLabsBackend()
    return _default


def has_key() -> bool:
    return _injected is not None or bool(api_key())


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def tts(text: str, language_code: str | None = None) -> bytes:
    """Speak `text`, serving repeats from memory."""
    text = text.strip()
    if not text:
        raise VoiceUnavailable("There is nothing to read out.")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]
    backend = get_backend()
    key = hashlib.sha256(f"{getattr(backend, 'tts_model', '')}|{getattr(backend, 'voice_id', '')}|{language_code or ''}|{text}".encode("utf-8")).hexdigest()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    audio = backend.tts(text, language_code)
    with _cache_lock:
        _cache[key] = audio
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
    return audio


def stt(data: bytes, filename: str = "speech.webm", content_type: str = "audio/webm", language_code: str | None = None) -> dict:
    return get_backend().stt(data, filename, content_type, language_code)
