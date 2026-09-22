"""One small door to the AI model: Google Gemini.

ComVoice needs exactly one thing from a model: "read this text, then call this
function with structured arguments". This module does that with the Google Gen AI
SDK. The key comes from your .env file (GEMINI_API_KEY, or GOOGLE_API_KEY).

Gemini models read very long text (up to about a million tokens), which suits long
legal documents and summaries.
"""
from __future__ import annotations

import os
import time


class LLMUnavailable(Exception):
    """The AI service is not connected or did not respond. The message is safe to show to a person."""


NOT_CONNECTED = "The AI service isn't connected yet (no API key set on the server)."
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
DEFAULT_MODEL = "gemini-flash-latest"  # always the newest Flash model. Pin one with GEMINI_MODEL if you prefer.
THINKING_HEADROOM = 8000  # Gemini's private "thinking" tokens count toward the output limit, so leave room


def _env(name: str, default: str) -> str:
    return os.getenv(name) or default


def api_key() -> str:
    for name in KEY_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def provider_name() -> str:
    return "gemini"


def provider_label() -> str:
    return "Google Gemini"


def _plain(value):
    """Gemini returns whole numbers as floats (27.0). Turn them back into ints, recursively."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class GeminiBackend:
    """Forces a function call, so the model must answer with structured arguments."""

    RETRIES = 2  # extra tries when Google says "too many requests" or is briefly overloaded
    BACKOFF_SECONDS = (4, 10)

    def __init__(self, client=None, base_url: str | None = None) -> None:
        self._client = client
        self._base_url = base_url  # only used by tests, to talk to a local fake server

    @property
    def model_main(self) -> str:
        return _env("GEMINI_MODEL", DEFAULT_MODEL)

    @property
    def model_verify(self) -> str:
        return _env("GEMINI_VERIFY_MODEL", self.model_main)

    def client(self):
        if self._client is None:
            key = api_key()
            if not key:
                raise LLMUnavailable(NOT_CONNECTED)
            try:
                from google import genai
                from google.genai import types
            except ImportError:
                raise LLMUnavailable("The Google AI package isn't installed. Run: python -m pip install -r requirements.txt")
            options = types.HttpOptions(timeout=120_000, **({"base_url": self._base_url} if self._base_url else {}))
            self._client = genai.Client(api_key=key, http_options=options)
        return self._client

    def call_tool(self, role: str, system_parts: list[str], user: str, tool: dict, max_tokens: int, cache: bool = False) -> dict:
        client = self.client()  # checks the key and that the package is installed
        from google.genai import errors, types

        model = self.model_main if role == "main" else self.model_verify
        config: dict = dict(
            system_instruction="\n\n".join(system_parts),  # the document sits here, unchanged between questions, so Google can cache it
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(name=tool["name"], description=tool["description"], parameters=tool["input_schema"])
                    ]
                )
            ],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=[tool["name"]])
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=max_tokens + THINKING_HEADROOM,
        )
        if os.getenv("LLM_TEMPERATURE"):
            config["temperature"] = float(os.environ["LLM_TEMPERATURE"])
        thinking: dict = {}
        if os.getenv("GEMINI_THINKING_BUDGET"):  # Gemini 2.5 models: 0 switches thinking off where allowed
            thinking["thinking_budget"] = int(os.environ["GEMINI_THINKING_BUDGET"])
        if os.getenv("GEMINI_THINKING_LEVEL"):  # Gemini 3 models: minimal | low | medium | high
            thinking["thinking_level"] = os.environ["GEMINI_THINKING_LEVEL"]
        if thinking:
            config["thinking_config"] = types.ThinkingConfig(**thinking)

        resp = None
        for attempt in range(self.RETRIES + 1):
            try:
                resp = client.models.generate_content(model=model, contents=user, config=types.GenerateContentConfig(**config))
                break
            except errors.APIError as exc:
                if self._retryable(exc) and attempt < self.RETRIES:
                    time.sleep(self.BACKOFF_SECONDS[min(attempt, len(self.BACKOFF_SECONDS) - 1)])
                    continue
                raise LLMUnavailable(self._explain(exc, model)) from exc
            except Exception as exc:  # network trouble, timeouts
                raise LLMUnavailable("The AI service didn't respond. Please try again in a moment.") from exc

        for call in getattr(resp, "function_calls", None) or []:
            if call.name == tool["name"]:
                return dict(_plain(dict(call.args or {})))
        raise LLMUnavailable(self._why_no_answer(resp))

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _retryable(exc) -> bool:
        return getattr(exc, "code", None) in (429, 500, 502, 503, 504)

    @staticmethod
    def _explain(exc, model: str) -> str:
        code = getattr(exc, "code", None)
        text = f"{getattr(exc, 'status', '')} {getattr(exc, 'message', '')} {exc}".lower()
        if "api key not valid" in text or "api_key_invalid" in text or code in (401, 403):
            return "Google didn't accept the API key. Check GEMINI_API_KEY in your .env file, or create a new key at aistudio.google.com/apikey."
        if code == 429 or "resource_exhausted" in text or "quota" in text:
            return "Google says this key is going too fast or has used up its free quota. Wait a minute and try again, or turn on billing for the key in Google AI Studio."
        if code == 404 or "not found" in text:
            return f"Google doesn't know the model '{model}'. Set GEMINI_MODEL in .env to one your key can use (run: python test_model.py --list)."
        if "location is not supported" in text or "failed_precondition" in text:
            return "Google's free tier isn't available in your country. Turn on billing for the key in Google AI Studio."
        return "The AI service didn't respond. Please try again in a moment."

    @staticmethod
    def _why_no_answer(resp) -> str:
        blocked = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
        finish = ""
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            finish = str(getattr(candidates[0], "finish_reason", "") or "")
        if blocked or "SAFETY" in finish.upper() or "BLOCK" in finish.upper():
            return "Google's safety filter blocked that request. Try rewording the question."
        if "MAX_TOKENS" in finish.upper():
            return "The AI ran out of room while thinking. Set GEMINI_THINKING_LEVEL=low (or GEMINI_THINKING_BUDGET=0 on 2.5 models) in .env."
        return "The AI service gave an unexpected reply."


# ------------------------------------------------------------------ front door

_injected = None  # a fake plugged in by tests
_default = None  # the real provider, created on first use


def set_backend(backend) -> None:
    """Used by tests to plug in a fake. Pass None to go back to the real provider."""
    global _injected, _default
    _injected = backend
    _default = None


def get_backend():
    global _default
    if _injected is not None:
        return _injected
    if _default is None:
        _default = GeminiBackend()
    return _default


def has_key() -> bool:
    return _injected is not None or bool(api_key())


def call_tool(role: str, system_parts: list[str], user: str, tool: dict, max_tokens: int = 900, cache: bool = False) -> dict:
    """role is "main" (writes answers) or "verify" (the second, independent checker)."""
    return get_backend().call_tool(role, system_parts, user, tool, max_tokens, cache)
