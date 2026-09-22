"""API tests with a fake AI provider, so no API key or network is needed."""
import io
import json
import pymupdf
import pytest
from fastapi.testclient import TestClient

import ai
import app as appmod
import links
from documents import Page

REAL_QUOTE = "being a minimum of 15% of total Gross Floor Area of the Detailed Development Consent, being no less than, 13,592.7 square metres"
GOOD = {
    "covered": True,
    "answer": "The document says the affordable housing building must be at least 15% of the total floor area. That is no less than 13,592.7 square metres.",
    "page": 27,
    "quote": REAL_QUOTE,
    "follow_ups": [
        {"question": "How long will the homes stay affordable?", "quote": "The Affordable Housing will be in perpetuity as opposed to the 15 years mandated by the Housing SEPP."},
        {"question": "Will rent be cheap?", "quote": "This sentence is not in the document at all, honestly."},
    ],
}
Q_COST = "The Landowner must, at its cost and risk, provide the Public Benefit to the City in accordance with this document."
Q_EVER = "The Affordable Housing will be in perpetuity as opposed to the 15 years mandated by the Housing SEPP."
OVERVIEW = {
    "title": "A planning agreement about affordable housing",
    "sections": [
        {"key": "what", "covered": True, "text": "This is an agreement between the council and the owner of a site.", "quote": Q_COST},
        {"key": "why", "covered": False},
        {"key": "proposed", "covered": True, "text": "The owner would build a park beside the station.", "quote": "The council shall construct a large public park beside the station."},  # invented: must not be shown
        {"key": "residents", "covered": True, "text": "The homes stay affordable for ever, not just 15 years.", "quote": Q_EVER},
        {"key": "benefits", "covered": True, "text": "The owner pays for the affordable homes.", "quote": Q_COST},
        # visitors, businesses, changes, when, next are left out on purpose: the document says nothing
    ],
    "key_terms": [{"term": "Indemnity", "meaning": "A promise to cover costs.", "quote": "The Landowner indemnifies the City from and against any damage, expense, cost, loss or liability suffered or incurred by the City"}],
    "references": [{"name": "The Housing SEPP", "quote": Q_EVER}],
    "questions": [
        {"question": "How much affordable housing will be built?", "quote": REAL_QUOTE},
        {"question": "Who will pay the council?", "quote": "Nothing like this appears anywhere in the document."},
    ],
}


class Fake:
    """Stands in for the AI provider. `answers` is a list of raw report_answer results, used in order."""

    def __init__(self, answers=(), judge=True, term=None, overview=OVERVIEW):
        self.answers = list(answers)
        self.judge, self.term, self.overview = judge, term, overview
        self.calls = []

    def call_tool(self, role, system_parts, user, tool, max_tokens, cache=False):
        self.calls.append({"role": role, "system": system_parts, "user": user, "tool": tool, "cache": cache})
        name = tool["name"]
        if name == "report_answer":
            return self.answers.pop(0)
        if name == "report_check":
            return {"supported": self.judge, "problem": "" if self.judge else "adds a fact"}
        if name == "report_checks":  # the batch fact-check used for the overview
            items = json.loads(user.split("\n\n", 1)[1])["items"]
            return {"results": [{"id": i["id"], "supported": self.judge} for i in items]}
        if name == "report_term":
            return self.term or {"known": False}
        return self.overview


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    appmod.SESSIONS.clear()
    for lim in (appmod.ask_limiter, appmod.upload_limiter, appmod.link_limiter):
        lim.hits.clear()
    monkeypatch.setattr(ai, "VERIFY_WITH_MODEL", True)
    monkeypatch.setattr(ai, "ALLOW_GENERAL_TERMS", True)
    yield
    ai.set_client(None)


client = TestClient(appmod.app)
SID = "test-session-1234"


def load_sample(fake=None):
    ai.set_client(fake or Fake())
    r = client.post("/api/sample", json={"session_id": SID})
    assert r.status_code == 200, r.text
    return r.json()


def ask(q="How much affordable housing?", **extra):
    return client.post("/api/ask", json={"question": q, "session_id": SID, **extra})


# ------------------------------------------------------------------- uploading

def test_asking_before_adding_a_document_says_to_add_one():
    ai.set_client(Fake())
    r = ask()
    assert r.status_code == 409 and "+" in r.json()["detail"]


def test_overview_is_organised_by_reader_questions_and_never_shows_unchecked_text():
    d = load_sample()
    o = d["overview"]
    assert d["count"] == 44 and d["label"] == "page" and o["title"].startswith("A planning agreement")
    assert [x["key"] for x in o["sections"]] == ["what", "why", "proposed", "residents", "visitors", "businesses", "benefits", "changes", "when", "next"]
    status = {x["key"]: x["status"] for x in o["sections"]}
    assert status == {
        "what": "ok",
        "why": "not_in_document",  # the model said the document doesn't cover it
        "proposed": "unverified",  # the model wrote something, but its quote isn't in the document
        "residents": "ok",
        "visitors": "not_in_document",  # left out by the model
        "businesses": "not_in_document",
        "benefits": "ok",
        "changes": "not_in_document",
        "when": "not_in_document",
        "next": "not_in_document",
    }
    by_key = {x["key"]: x for x in o["sections"]}
    assert by_key["residents"]["page"] == 28 and by_key["residents"]["printed"] == "27" and "for ever" in by_key["residents"]["text"]
    assert "text" not in by_key["proposed"] and "quote" not in by_key["proposed"] and "text" not in by_key["why"]
    assert o["key_terms"][0]["term"] == "Indemnity" and o["references"][0]["name"] == "The Housing SEPP"
    assert [q["question"] for q in o["questions"]] == ["How much affordable housing will be built?"]


def test_overview_sections_a_fact_checker_rejects_are_marked_unverified_not_shown():
    d = load_sample(Fake(judge=False))
    o = d["overview"]
    assert {x["key"]: x["status"] for x in o["sections"]}["what"] == "unverified"
    assert all("text" not in x for x in o["sections"]) and o["key_terms"] == []


def test_overview_section_with_an_invented_number_is_not_shown():
    bad = {**OVERVIEW, "sections": [{"key": "what", "covered": True, "text": "The owner must build 999 homes.", "quote": Q_COST}]}
    d = load_sample(Fake(overview=bad))
    assert {x["key"]: x["status"] for x in d["overview"]["sections"]}["what"] == "unverified"


def test_the_overview_uses_three_calls_however_long_it_is():
    fake = Fake()
    load_sample(fake)
    assert [c["role"] for c in fake.calls] == ["main", "verify", "verify"]  # write it, check the sections, check the hard words
    assert [c["tool"]["name"] for c in fake.calls] == ["report_overview", "report_checks", "report_checks"]


def test_the_overview_prompt_forbids_filling_gaps_with_general_knowledge():
    fake = Fake()
    load_sample(fake)
    task = fake.calls[0]["user"]
    assert "ONLY what the DOCUMENT itself says" in task and "set covered to false" in task and "traffic" in task


def test_upload_a_pdf_finds_its_links():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "The tenant must pay rent on the first day of each month, without delay.")
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 60, 300, 80), "uri": "https://www.legislation.nsw.gov.au/tenancy"})
    ai.set_client(Fake(overview={"title": "A lease", "sections": [], "key_terms": [], "references": [], "questions": []}))
    r = client.post(f"/api/upload?session_id={SID}&filename=lease.pdf", content=doc.tobytes())
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "lease.pdf" and d["count"] == 1
    assert d["links"] == [{"url": "https://www.legislation.nsw.gov.au/tenancy", "host": "www.legislation.nsw.gov.au", "official": True, "page": 1}]


def test_unsupported_file_gets_a_plain_422():
    ai.set_client(Fake())
    r = client.post(f"/api/upload?session_id={SID}&filename=virus.exe", content=b"\x00\x01" * 50)
    assert r.status_code == 422 and "PDF" in r.json()["detail"]


def test_too_big_upload_is_refused(monkeypatch):
    monkeypatch.setattr(appmod, "MAX_UPLOAD_BYTES", 1000)
    ai.set_client(Fake())
    r = client.post(f"/api/upload?session_id={SID}&filename=a.txt", content=b"a" * 2000)
    assert r.status_code == 413


def test_without_an_api_key_the_upload_still_works_but_says_ai_is_off(monkeypatch):
    ai.set_client(None)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    d = client.post("/api/sample", json={"session_id": SID}).json()
    assert d["overview"] is None and "isn't connected" in d["ai_error"] and d["count"] == 44
    r = ask()
    assert r.status_code == 503 and "isn't connected" in r.json()["detail"]


# --------------------------------------------------------------------- asking

def test_good_answer_passes_all_gates_and_shows_the_source():
    load_sample()
    ai.set_client(Fake([GOOD]))
    r = ask().json()
    assert r["covered"] and r["origin"] == "main"
    assert r["source"]["page"] == 27 and r["source"]["printed"] == "26" and r["source"]["kind"] == "main"
    assert "13,592.7" in r["answer"]


def test_only_followups_with_real_quotes_are_offered():
    load_sample()
    ai.set_client(Fake([GOOD]))
    assert ask().json()["follow_ups"] == ["How long will the homes stay affordable?"]


def test_invented_quote_is_refused():
    load_sample()
    ai.set_client(Fake([{**GOOD, "quote": "The council promises to build a new park next to the station."}]))
    r = ask().json()
    assert not r["covered"] and "can't find that in your document" in r["answer"] and r["source"] is None


def test_invented_number_is_refused():
    load_sample()
    ai.set_client(Fake([{**GOOD, "answer": "The document says the building must be at least 77777 square metres."}]))
    assert not ask().json()["covered"]


def test_answer_the_judge_rejects_is_refused():
    load_sample()
    ai.set_client(Fake([GOOD], judge=False))
    assert not ask().json()["covered"]


def test_wrong_page_number_from_model_is_corrected_by_code():
    load_sample()
    ai.set_client(Fake([{**GOOD, "page": 3}]))
    assert ask().json()["source"]["page"] == 27


def test_document_and_rules_are_sent_as_a_stable_prefix_with_the_question_last():
    load_sample()
    fake = Fake([GOOD])
    ai.set_client(fake)
    ask(history=[{"q": "hi", "a": "hello"}])
    call = fake.calls[0]
    assert call["role"] == "main" and call["tool"]["name"] == "report_answer" and call["cache"] is True
    assert call["system"][0].startswith("You are ComVoice") and call["system"][1].startswith("DOCUMENT")
    assert "Earlier in this chat" in call["user"] and call["user"].endswith("QUESTION: How much affordable housing?")


def test_the_fact_checker_runs_as_the_verify_role():
    load_sample()
    fake = Fake([GOOD])
    ai.set_client(fake)
    ask()
    assert [c["role"] for c in fake.calls] == ["main", "verify"]


def test_long_questions_are_allowed_up_to_the_limit_and_no_further():
    load_sample()
    ai.set_client(Fake([GOOD] * 3))
    assert ask("Why? " * 600).status_code == 200  # 3000 characters
    too_long = "x" * (appmod.MAX_QUESTION_CHARS + 1)
    assert client.post("/api/ask", json={"question": too_long, "session_id": SID}).status_code == 422


def test_the_chat_can_carry_six_earlier_turns_but_not_seven():
    load_sample()
    ai.set_client(Fake([GOOD] * 3))
    turns = [{"q": f"question {i}", "a": f"answer {i}"} for i in range(6)]
    assert ask(history=turns).status_code == 200
    assert ask(history=turns + [{"q": "one more", "a": "too many"}]).status_code == 422


def test_rate_limit_applies_and_can_be_switched_off(monkeypatch):
    load_sample()
    ai.set_client(Fake([GOOD] * 200))
    monkeypatch.setattr(appmod.ask_limiter, "limit", 5)
    codes = [ask().status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    appmod.ask_limiter.hits.clear()
    monkeypatch.setattr(appmod.ask_limiter, "limit", 0)  # 0 = no limit
    assert all(ask().status_code == 200 for _ in range(100))


def test_defaults_are_generous():
    assert appmod.MAX_QUESTION_CHARS >= 4000 and appmod.ASK_LIMIT_PER_MIN >= 60


@pytest.mark.parametrize(
    "files,words",
    [
        ([], "no file named .env"),
        ([".env.txt"], "Rename it to exactly .env"),
        ([".env"], "no GEMINI_API_KEY"),
    ],
)
def test_health_says_why_the_key_was_not_found(tmp_path, monkeypatch, files, words):
    for name in files:
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(appmod, "ROOT", tmp_path)
    ai.set_client(None)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    h = client.get("/api/health").json()
    assert h["ai_connected"] is False and words in h["hint"]
    assert "sk-" not in h["hint"]


def test_health_reports_limits_and_has_no_hint_when_connected():
    ai.set_client(Fake())
    h = client.get("/api/health").json()
    assert h["hint"] == "" and h["max_question_chars"] == appmod.MAX_QUESTION_CHARS and h["max_upload_mb"] == 15


# ------------------------------------------------- legal words the document lacks

TERM_Q = {"covered": False, "term_question": True, "term": "estoppel"}


def test_unknown_legal_word_gets_a_labelled_general_meaning():
    load_sample()
    ai.set_client(Fake([TERM_Q], term={"known": True, "meaning": "It stops a person going back on something they said, if others relied on it."}))
    r = ask("What does estoppel mean?").json()
    assert r["covered"] and r["origin"] == "general" and r["source"] is None
    assert r["answer"].startswith("Estoppel:")


def test_general_meaning_with_numbers_or_doubt_is_not_shown():
    load_sample()
    ai.set_client(Fake([TERM_Q], term={"known": True, "meaning": "It applies after 30 days."}))
    assert ask().json()["origin"] == "none"
    ai.set_client(Fake([TERM_Q], term={"known": False}))
    assert ask().json()["origin"] == "none"


def test_general_meanings_can_be_switched_off(monkeypatch):
    load_sample()
    monkeypatch.setattr(ai, "ALLOW_GENERAL_TERMS", False)
    ai.set_client(Fake([TERM_Q], term={"known": True, "meaning": "A short general meaning."}))
    assert ask().json()["origin"] == "none"


def test_normal_out_of_document_question_never_reaches_the_general_path():
    load_sample()
    fake = Fake([{"covered": False}])
    ai.set_client(fake)
    r = ask("Will this increase traffic?").json()
    assert r["origin"] == "none" and not any(c["tool"]["name"] == "report_term" for c in fake.calls)


# ------------------------------------------------------------ following links

def fetched(title, text, official=False, url="https://www.legislation.nsw.gov.au/act"):
    return links.FetchedPage(url, title, [Page(1, text, local=1)], "part", official)


def patch_fetch_many(monkeypatch, results):
    monkeypatch.setattr(links, "fetch_many", lambda urls, official, max_links=10, **k: results)


def sample_with_links(monkeypatch):
    d = load_sample()
    appmod.SESSIONS[SID].links[:] = [appmod.LinkRef("https://www.legislation.nsw.gov.au/act", 3), appmod.LinkRef("https://example.org/x", 4)]
    return d


LINKED_TEXT = "The Act says a landlord must give at least 14 days notice before entering a rented home for an inspection."


def test_following_links_adds_pages_and_reports_each_result(monkeypatch):
    sample_with_links(monkeypatch)
    page = fetched("Residential Tenancies Act", LINKED_TEXT, official=True)
    patch_fetch_many(
        monkeypatch,
        [links.LinkResult(page.url, True, page.title, True, fetched=page), links.LinkResult("https://example.org/x", False, reason="That page needs a login or blocks readers, so I can't open it.")],
    )
    r = client.post("/api/follow_links", json={"session_id": SID}).json()
    assert r["read"] == 1 and r["results"][0]["ok"] and not r["results"][1]["ok"] and "login" in r["results"][1]["reason"]
    assert appmod.SESSIONS[SID].linked_source is not None


def test_questions_the_document_cannot_answer_fall_through_to_linked_pages(monkeypatch):
    sample_with_links(monkeypatch)
    page = fetched("Residential Tenancies Act", LINKED_TEXT, official=True)
    patch_fetch_many(monkeypatch, [links.LinkResult(page.url, True, page.title, True, fetched=page)])
    client.post("/api/follow_links", json={"session_id": SID})
    linked_answer = {"covered": True, "answer": "The linked page says a landlord must give at least 14 days notice before an inspection.", "page": 1, "quote": "a landlord must give at least 14 days notice before entering a rented home for an inspection"}
    ai.set_client(Fake([{"covered": False}, linked_answer]))
    r = ask("How much notice before an inspection?").json()
    assert r["covered"] and r["origin"] == "linked"
    assert r["source"] == {"kind": "linked", "page": 1, "label": "section", "name": "Residential Tenancies Act", "url": page.url, "official": True, "quote": linked_answer["quote"]}


def test_the_uploaded_document_wins_over_linked_pages(monkeypatch):
    sample_with_links(monkeypatch)
    page = fetched("Act", LINKED_TEXT)
    patch_fetch_many(monkeypatch, [links.LinkResult(page.url, True, page.title, False, fetched=page)])
    client.post("/api/follow_links", json={"session_id": SID})
    fake = Fake([GOOD])
    ai.set_client(fake)
    assert ask().json()["origin"] == "main" and sum(c["tool"]["name"] == "report_answer" for c in fake.calls) == 1


def test_refusal_mentions_linked_pages_once_they_are_added(monkeypatch):
    sample_with_links(monkeypatch)
    page = fetched("Act", LINKED_TEXT)
    patch_fetch_many(monkeypatch, [links.LinkResult(page.url, True, page.title, False, fetched=page)])
    client.post("/api/follow_links", json={"session_id": SID})
    ai.set_client(Fake([{"covered": False}, {"covered": False}]))
    assert "pages linked from it" in ask("Something else").json()["answer"]


def test_only_links_that_are_in_the_document_can_be_followed(monkeypatch):
    sample_with_links(monkeypatch)
    r = client.post("/api/follow_links", json={"session_id": SID, "urls": ["https://attacker.example/x"]})
    assert r.status_code == 422


def test_following_links_needs_a_document():
    assert client.post("/api/follow_links", json={"session_id": "no-doc-session-1"}).status_code == 409


def test_a_pasted_link_is_added_and_unsafe_ones_are_refused(monkeypatch):
    load_sample()
    monkeypatch.setattr(links, "fetch_public_page", lambda url, official: fetched("Council news", LINKED_TEXT, url=url))
    r = client.post("/api/add_link", json={"url": "https://example.org/news", "session_id": SID}).json()
    assert r["title"] == "Council news" and appmod.SESSIONS[SID].linked_source is not None
    monkeypatch.undo()
    bad = client.post("/api/add_link", json={"url": "https://127.0.0.1/admin", "session_id": SID})
    assert bad.status_code == 422 and "private" in bad.json()["detail"]


def test_workspaces_are_private_and_can_be_cleared():
    load_sample()
    assert appmod.get_workspace("another-session-9999") is None
    client.delete("/api/workspace", params={"session_id": SID})
    assert SID not in appmod.SESSIONS
    ai.set_client(Fake([GOOD]))
    assert ask().status_code == 409


def test_health_and_static_page_and_headers():
    ai.set_client(Fake())
    h = client.get("/api/health").json()
    assert h["ok"] and h["ai_connected"] and h["app_name"] == "ComVoice" and h["provider"] == "Google Gemini"
    r = client.get("/")
    assert r.status_code == 200 and "ComVoice" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_feedback_accepts_only_anonymous_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DATA", tmp_path)
    assert client.post("/api/feedback", json={"rating": "unclear", "reason": "too_hard", "origin": "main"}).json()["ok"]
    assert client.post("/api/feedback", json={"rating": "clear", "reason": "free text here"}).status_code == 422
