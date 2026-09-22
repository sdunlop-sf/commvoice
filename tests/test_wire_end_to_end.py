"""The whole app, using the REAL Google SDK, talking to a local fake Google server.

This proves the app's requests are shaped the way the SDK and Google expect, and that the
answers Google returns are read, checked by the four gates, and shown. No key or internet needed.
"""
import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

import ai
import app as appmod
import llm

Q_COST = "The Landowner must, at its cost and risk, provide the Public Benefit to the City in accordance with this document."
Q_27 = "being a minimum of 15% of total Gross Floor Area of the Detailed Development Consent, being no less than, 13,592.7 square metres"
OVERVIEW = {
    "title": "A planning agreement about affordable housing",
    "sections": [
        {"key": "what", "covered": True, "text": "This is an agreement about building affordable homes.", "quote": Q_COST},
        {"key": "why", "covered": False},
    ],
    "key_terms": [],
    "references": [],
    "questions": [{"question": "How much affordable housing will be built?", "quote": Q_27}],
}
ANSWER = {
    "covered": True,
    "answer": "The document says the affordable housing building must be at least 15% of the total floor area. That is no less than 13,592.7 square metres.",
    "page": 27.0,  # Google sends whole numbers as floats
    "quote": Q_27,
    "follow_ups": [],
}
SEEN = []


class FakeGoogle(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = body["tools"][0]["functionDeclarations"][0]["name"]
        text = body["contents"][0]["parts"][0]["text"]
        SEEN.append(name)
        if name == "report_overview":
            args = OVERVIEW
        elif name == "report_answer":
            args = {"covered": False} if "traffic" in text.lower() else ANSWER
        elif name == "report_checks":
            args = {"results": [{"id": i["id"], "supported": True} for i in json.loads(text.split("\n\n", 1)[1])["items"]]}
        else:
            args = {"supported": True}
        data = json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}, "finishReason": "STOP"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def client(monkeypatch):
    SEEN.clear()
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm.set_backend(llm.GeminiBackend(base_url=f"http://127.0.0.1:{server.server_port}"))
    appmod.SESSIONS.clear()
    for lim in (appmod.ask_limiter, appmod.upload_limiter, appmod.link_limiter):
        lim.hits.clear()
    yield TestClient(appmod.app)
    llm.set_backend(None)
    server.shutdown()


def test_upload_overview_answer_and_refusal_through_the_real_sdk(client):
    sid = "wire-test-1234"
    d = client.post("/api/sample", json={"session_id": sid}).json()
    assert d["ai_error"] is None
    sections = {s["key"]: s for s in d["overview"]["sections"]}
    assert sections["what"]["status"] == "ok" and sections["what"]["page"] == 11
    assert sections["why"]["status"] == "not_in_document" and sections["residents"]["status"] == "not_in_document"
    assert [q["question"] for q in d["overview"]["questions"]] == ["How much affordable housing will be built?"]
    assert SEEN == ["report_overview", "report_checks"]  # one write and one batched check (this fake overview has no hard words to check)

    r = client.post("/api/ask", json={"question": "How much affordable housing?", "session_id": sid}).json()
    assert r["covered"] and r["origin"] == "main" and r["source"]["page"] == 27 and r["source"]["printed"] == "26"
    assert "13,592.7" in r["answer"]

    r = client.post("/api/ask", json={"question": "Will this increase traffic?", "session_id": sid}).json()
    assert not r["covered"] and "can't find that in your document" in r["answer"]
