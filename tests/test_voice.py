"""The ElevenLabs voice routes, with a fake provider so no key or network is needed."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as appmod
import voice


class FakeVoice:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []
        self.tts_model, self.voice_id = "fake-model", "fake-voice"

    def tts(self, text, language_code=None):
        self.calls.append(("tts", text, language_code))
        if self.fail:
            raise voice.VoiceUnavailable(self.fail)
        return b"ID3-fake-mp3-bytes"

    def stt(self, data, filename="speech.webm", content_type="audio/webm", language_code=None):
        self.calls.append(("stt", len(data), filename, content_type, language_code))
        if self.fail:
            raise voice.VoiceUnavailable(self.fail)
        return {"text": "How much rent do I pay?", "language_code": "eng"}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for lim in (appmod.tts_limiter, appmod.stt_limiter):
        lim.hits.clear()
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    voice.set_backend(None)
    yield
    voice.set_backend(None)


client = TestClient(appmod.app)
AUDIO = b"\x1aE\xdf\xa3" + b"x" * 4000  # a webm-shaped blob, big enough to count as a recording


# --------------------------------------------------------------------- health

def test_health_says_whether_voice_is_on():
    h = client.get("/api/health").json()
    assert h["tts"] is False and h["stt"] is False and h["voice_provider"] == "ElevenLabs" and h["max_tts_chars"] == voice.MAX_TTS_CHARS
    voice.set_backend(FakeVoice())
    h = client.get("/api/health").json()
    assert h["tts"] is True and h["stt"] is True


# ------------------------------------------------------------------- speaking

def test_tts_returns_mp3_and_repeats_come_from_memory():
    fake = FakeVoice()
    voice.set_backend(fake)
    r = client.post("/api/tts", json={"text": "The document says the rent is due on the first."})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg") and r.content == b"ID3-fake-mp3-bytes"
    client.post("/api/tts", json={"text": "The document says the rent is due on the first."})
    assert sum(1 for c in fake.calls if c[0] == "tts") == 1  # second request served from the cache


def test_tts_passes_a_language_hint_and_caps_the_text():
    fake = FakeVoice()
    voice.set_backend(fake)
    assert client.post("/api/tts", json={"text": "Hola.", "language": "es"}).status_code == 200
    assert fake.calls[-1] == ("tts", "Hola.", "es")
    assert client.post("/api/tts", json={"text": "x" * (voice.MAX_TTS_CHARS + 1)}).status_code == 422
    assert client.post("/api/tts", json={"text": "hi", "language": "english"}).status_code == 422


def test_tts_without_a_key_is_a_plain_503():
    r = client.post("/api/tts", json={"text": "Hello there."})
    assert r.status_code == 503 and "isn't switched on" in r.json()["detail"]


def test_tts_provider_trouble_is_a_plain_503():
    voice.set_backend(FakeVoice(fail="ElevenLabs says this key has used up its quota or is going too fast."))
    r = client.post("/api/tts", json={"text": "Hello there."})
    assert r.status_code == 503 and "quota" in r.json()["detail"]


# ------------------------------------------------------------------ listening

def test_stt_transcribes_a_recording():
    fake = FakeVoice()
    voice.set_backend(fake)
    r = client.post("/api/stt?filename=speech.webm&language=en", content=AUDIO, headers={"Content-Type": "audio/webm;codecs=opus"})
    assert r.status_code == 200 and r.json() == {"text": "How much rent do I pay?", "language_code": "eng"}
    assert fake.calls[-1] == ("stt", len(AUDIO), "speech.webm", "audio/webm", "en")


def test_stt_rejects_a_tap_a_bad_language_and_a_huge_recording(monkeypatch):
    voice.set_backend(FakeVoice())
    assert client.post("/api/stt", content=b"tiny").status_code == 422
    assert client.post("/api/stt?language=English", content=AUDIO).status_code == 422
    monkeypatch.setattr(voice, "MAX_STT_BYTES", 1000)
    assert client.post("/api/stt", content=AUDIO).status_code == 413


def test_stt_with_no_words_is_explained():
    class Silent(FakeVoice):
        def stt(self, *a, **k):
            return {"text": "", "language_code": None}

    voice.set_backend(Silent())
    r = client.post("/api/stt", content=AUDIO)
    assert r.status_code == 422 and "couldn't hear" in r.json()["detail"]


def test_stt_without_a_key_is_a_plain_503():
    r = client.post("/api/stt", content=AUDIO)
    assert r.status_code == 503 and "isn't switched on" in r.json()["detail"]


# ----------------------------------------------------------- limits and headers

def test_voice_routes_are_rate_limited(monkeypatch):
    voice.set_backend(FakeVoice())
    monkeypatch.setattr(appmod.tts_limiter, "limit", 3)
    codes = [client.post("/api/tts", json={"text": f"line {i}"}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    monkeypatch.setattr(appmod.stt_limiter, "limit", 2)
    codes = [client.post("/api/stt", content=AUDIO).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_page_may_play_audio_from_a_blob():
    csp = client.get("/").headers["content-security-policy"]
    assert "media-src 'self' blob:" in csp and "connect-src 'self'" in csp


def test_provider_errors_become_plain_words():
    ex = voice.ElevenLabsBackend._explain
    assert "API key" in ex(SimpleNamespace(status_code=401, body={"detail": "invalid_api_key"}))
    assert "quota" in ex(SimpleNamespace(status_code=429, body="quota_exceeded"))
    assert "voice or model" in ex(SimpleNamespace(status_code=404, body=""))
    assert "speaking a little longer" in ex(SimpleNamespace(status_code=422, body=""))
    assert "didn't respond" in ex(ConnectionError("down"))


def test_the_real_backend_reads_its_settings_from_the_environment(monkeypatch):
    b = voice.ElevenLabsBackend()
    assert b.voice_id == voice.DEFAULT_VOICE_ID and b.tts_model == voice.DEFAULT_TTS_MODEL and b.stt_model == voice.DEFAULT_STT_MODEL
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v1")
    monkeypatch.setenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
    assert b.voice_id == "v1" and b.tts_model == "eleven_multilingual_v2"
    assert not voice.has_key()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "  abc ")
    assert voice.has_key() and voice.api_key() == "abc"
