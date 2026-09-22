"""The Gemini adapter, tested with stub clients and (for the real SDK) a local fake Google server."""
import http.server
import json
import threading
from types import SimpleNamespace

import pytest
from google.genai import errors

import llm

TOOL = {
    "name": "report_check",
    "description": "d",
    "input_schema": {"type": "object", "properties": {"supported": {"type": "boolean"}}, "required": ["supported"]},
}


def reply(name="report_check", args=None, **extra):
    call = SimpleNamespace(name=name, args=args if args is not None else {"supported": True})
    return SimpleNamespace(function_calls=[call], prompt_feedback=None, candidates=[], **extra)


def empty_reply(finish="STOP", blocked=None):
    return SimpleNamespace(function_calls=None, prompt_feedback=SimpleNamespace(block_reason=blocked), candidates=[SimpleNamespace(finish_reason=finish)])


class StubClient:
    """Records what would be sent to Google. `script` is a list of replies or exceptions, used in order."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def run(stub, role="main", **kw):
    return llm.GeminiBackend(stub).call_tool(role, ["RULES", "DOC"], "QUESTION", TOOL, 200, **kw)


def api_error(cls, code, message, status):
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_VERIFY_MODEL", "GEMINI_THINKING_LEVEL", "GEMINI_THINKING_BUDGET", "LLM_TEMPERATURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    llm.set_backend(None)
    yield
    llm.set_backend(None)


# ------------------------------------------------------------ request shape

def test_request_forces_the_function_and_keeps_the_document_in_the_system_instruction():
    stub = StubClient(reply(args={"supported": True}))
    assert run(stub) == {"supported": True}
    call = stub.calls[0]
    cfg = call["config"]
    assert call["model"] == "gemini-flash-latest" and call["contents"] == "QUESTION"
    assert cfg.system_instruction == "RULES\n\nDOC"
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "report_check" and decl.parameters.required == ["supported"]
    assert decl.parameters.properties["supported"].type.value == "BOOLEAN"
    fcc = cfg.tool_config.function_calling_config
    assert str(fcc.mode.value) == "ANY" and fcc.allowed_function_names == ["report_check"]
    assert cfg.max_output_tokens == 200 + llm.THINKING_HEADROOM
    assert cfg.temperature is None and cfg.thinking_config is None


def test_models_and_options_come_from_the_environment(monkeypatch):
    stub = StubClient(reply())
    run(stub, role="main")
    run(stub, role="verify")
    assert [c["model"] for c in stub.calls] == ["gemini-flash-latest"] * 2  # verify defaults to the main model
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_VERIFY_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low")
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "0")
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    run(stub, role="main")
    run(stub, role="verify")
    assert [c["model"] for c in stub.calls[2:]] == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    cfg = stub.calls[-1]["config"]
    assert cfg.thinking_config.thinking_level.value.lower() == "low" and cfg.thinking_config.thinking_budget == 0 and cfg.temperature == 0.0


def test_whole_number_floats_come_back_as_ints():
    stub = StubClient(reply(args={"page": 27.0, "covered": True, "items": [{"n": 3.0, "x": 1.5}]}))
    out = run(stub)
    assert out == {"page": 27, "covered": True, "items": [{"n": 3, "x": 1.5}]}
    assert type(out["page"]) is int and type(out["items"][0]["x"]) is float


# ------------------------------------------------------------------- errors

@pytest.mark.parametrize(
    "error,words",
    [
        (api_error(errors.ClientError, 400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"), "didn't accept the API key"),
        (api_error(errors.ClientError, 403, "Permission denied", "PERMISSION_DENIED"), "didn't accept the API key"),
        (api_error(errors.ClientError, 429, "Quota exceeded", "RESOURCE_EXHAUSTED"), "going too fast"),
        (api_error(errors.ClientError, 404, "models/nope is not found", "NOT_FOUND"), "GEMINI_MODEL"),
        (api_error(errors.ClientError, 400, "User location is not supported for the API use.", "FAILED_PRECONDITION"), "free tier isn't available in your country"),
        (api_error(errors.ServerError, 503, "The model is overloaded", "UNAVAILABLE"), "didn't respond"),
        (ConnectionError("network down"), "didn't respond"),
    ],
)
def test_errors_become_plain_messages(error, words):
    with pytest.raises(llm.LLMUnavailable) as e:
        run(StubClient(error))
    assert words in str(e.value)


def test_a_busy_or_rate_limited_reply_is_retried_then_succeeds():
    stub = StubClient(api_error(errors.ClientError, 429, "slow down", "RESOURCE_EXHAUSTED"), api_error(errors.ServerError, 503, "busy", "UNAVAILABLE"), reply())
    assert run(stub) == {"supported": True} and len(stub.calls) == 3


def test_a_wrong_key_is_not_retried():
    stub = StubClient(api_error(errors.ClientError, 400, "API key not valid", "INVALID_ARGUMENT"))
    with pytest.raises(llm.LLMUnavailable):
        run(stub)
    assert len(stub.calls) == 1


def test_persistent_rate_limits_stop_after_the_retries():
    stub = StubClient(api_error(errors.ClientError, 429, "slow down", "RESOURCE_EXHAUSTED"))
    with pytest.raises(llm.LLMUnavailable):
        run(stub)
    assert len(stub.calls) == 1 + llm.GeminiBackend.RETRIES


@pytest.mark.parametrize(
    "empty,words",
    [
        (empty_reply(finish="SAFETY"), "safety filter"),
        (empty_reply(blocked="PROHIBITED_CONTENT"), "safety filter"),
        (empty_reply(finish="MAX_TOKENS"), "GEMINI_THINKING_LEVEL"),
        (empty_reply(), "unexpected reply"),
        (SimpleNamespace(function_calls=[SimpleNamespace(name="some_other_function", args={})], prompt_feedback=None, candidates=[]), "unexpected reply"),
    ],
)
def test_a_reply_without_the_expected_function_call_is_explained(empty, words):
    with pytest.raises(llm.LLMUnavailable) as e:
        run(StubClient(empty))
    assert words in str(e.value)


# ------------------------------------------------------------------ the key

def test_no_key_says_not_connected():
    assert not llm.has_key()
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.GeminiBackend().call_tool("main", ["x"], "y", TOOL, 10)
    assert "isn't connected" in str(e.value)


def test_key_can_be_gemini_or_google_and_blank_values_are_ignored(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert not llm.has_key()
    monkeypatch.setenv("GOOGLE_API_KEY", "abc")
    assert llm.has_key() and llm.api_key() == "abc"
    monkeypatch.setenv("GEMINI_API_KEY", "  xyz  ")
    assert llm.api_key() == "xyz"
    assert llm.provider_label() == "Google Gemini"


def test_has_key_is_true_when_a_backend_is_plugged_in():
    llm.set_backend(object())
    assert llm.has_key()


# ------------------------------------------- the real SDK, against a local fake

class FakeGoogle(http.server.BaseHTTPRequestHandler):
    seen = []
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGoogle.seen.append({"path": self.path, "body": body, "key": self.headers.get("x-goog-api-key")})
        if FakeGoogle.status == 200:
            name = body["tools"][0]["functionDeclarations"][0]["name"]
            out = {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": {"supported": True, "n": 5}}}]}, "finishReason": "STOP"}]}
        else:
            out = {"error": {"code": FakeGoogle.status, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
        data = json.dumps(out).encode()
        self.send_response(FakeGoogle.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_google(monkeypatch):
    FakeGoogle.seen, FakeGoogle.status = [], 200
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_real_sdk_sends_the_request_google_expects_and_we_read_its_reply(fake_google):
    backend = llm.GeminiBackend(base_url=fake_google)
    out = backend.call_tool("main", ["RULES", "DOC"], "QUESTION", TOOL, 200)
    assert out == {"supported": True, "n": 5} and type(out["n"]) is int
    sent = FakeGoogle.seen[0]
    assert sent["path"].endswith("/models/gemini-flash-latest:generateContent") and sent["key"] == "test-key"
    body = sent["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "RULES\n\nDOC"
    assert body["contents"][0]["parts"][0]["text"] == "QUESTION"
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "report_check"
    assert decl["parameters"] == {"type": "OBJECT", "properties": {"supported": {"type": "BOOLEAN"}}, "required": ["supported"]}
    assert body["toolConfig"]["functionCallingConfig"] == {"mode": "ANY", "allowedFunctionNames": ["report_check"]}
    assert body["generationConfig"]["maxOutputTokens"] == 200 + llm.THINKING_HEADROOM


def test_every_real_tool_schema_in_the_app_is_accepted_by_the_sdk(fake_google):
    import ai

    backend = llm.GeminiBackend(base_url=fake_google)
    for tool in (ai.ANSWER_TOOL, ai.OVERVIEW_TOOL, ai.JUDGE_TOOL, ai.JUDGE_MANY_TOOL, ai.TERM_TOOL):
        assert backend.call_tool("main", ["r", "d"], "q", tool, 100) == {"supported": True, "n": 5}
    names = [x["body"]["tools"][0]["functionDeclarations"][0]["name"] for x in FakeGoogle.seen]
    assert names == ["report_answer", "report_overview", "report_check", "report_checks", "report_term"]


def test_the_real_sdk_turns_a_google_quota_error_into_a_plain_message(fake_google):
    FakeGoogle.status = 429
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.GeminiBackend(base_url=fake_google).call_tool("main", ["x"], "y", TOOL, 10)
    assert "going too fast" in str(e.value) and len(FakeGoogle.seen) == 1 + llm.GeminiBackend.RETRIES


# -------------------------------------------------------------- check_key.py

def test_check_key_reports_plain_results(capsys):
    import check_key

    class Good:
        model_main = "m"

        def call_tool(self, *a, **k):
            return {"ok": True}

    llm.set_backend(Good())
    assert check_key.main() == 0 and "Working" in capsys.readouterr().out

    class Broke(Good):
        def call_tool(self, *a, **k):
            raise llm.LLMUnavailable("Google says this key is going too fast or has used up its free quota.")

    llm.set_backend(Broke())
    assert check_key.main() == 2 and "free quota" in capsys.readouterr().out
