"""Run the real app with a fake AI and fake link fetching, so the UI can be tested without an API key.

    python tests/ui/serve_fake.py        # serves on http://127.0.0.1:8123
    cd tests/ui && npm install && npm test
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

import ai  # noqa: E402
import app  # noqa: E402
import documents  # noqa: E402
import links  # noqa: E402
from documents import LinkRef, Page  # noqa: E402

Q27 = "being a minimum of 15% of total Gross Floor Area of the Detailed Development Consent, being no less than, 13,592.7 square metres"
GOOD = {
    "covered": True,
    "answer": "The document says the affordable housing building must be at least 15% of the total floor area. That is no less than 13,592.7 square metres.",
    "page": 27,
    "quote": Q27,
    "follow_ups": [{"question": "How long will the homes stay affordable?", "quote": "The Affordable Housing will be in perpetuity as opposed to the 15 years mandated by the Housing SEPP."}],
}
Q_COST = "The Landowner must, at its cost and risk, provide the Public Benefit to the City in accordance with this document."
Q_EVER = "The Affordable Housing will be in perpetuity as opposed to the 15 years mandated by the Housing SEPP."
OVERVIEW = {
    "title": "A planning agreement for affordable housing",
    "sections": [
        {"key": "what", "covered": True, "text": "This is an agreement between the council and the owner of a site about building affordable homes.", "quote": Q_COST},
        {"key": "why", "covered": False},
        {"key": "proposed", "covered": True, "text": "The owner would build an affordable housing building at its own cost.", "quote": Q_COST},
        {"key": "residents", "covered": True, "text": "The homes stay affordable for ever, not just 15 years.", "quote": Q_EVER},
        {"key": "benefits", "covered": True, "text": "The owner pays for the affordable homes.", "quote": Q_COST},
    ],
    "key_terms": [{"term": "Indemnity", "meaning": "A promise to cover someone's costs and losses.", "quote": "The Landowner indemnifies the City from and against any damage, expense, cost, loss or liability suffered or incurred by the City"}],
    "references": [{"name": "The Housing SEPP", "quote": Q_EVER}],
    "questions": [{"question": q, "quote": Q27} for q in ["How much affordable housing will be built?", "Who pays for the building?", "How long do the homes stay affordable?", "What is indemnity?", "Who manages the homes?"]],
}
LINKED_TEXT = "The Act says a landlord must give at least 14 days notice before entering a rented home for an inspection."


class FakeAI:
    """Stands in for the AI provider with scripted replies."""

    def call_tool(self, role, system_parts, user, tool, max_tokens, cache=False):
        name = tool["name"]
        system = " ".join(system_parts)
        if name == "report_answer":
            q = user.lower().split("question:")[-1]  # ignore the chat history
            if "estoppel" in q:
                return {"covered": False, "term_question": True, "term": "estoppel"}
            if "inspection" in q:
                if "sections of DIFFERENT" not in system:
                    return {"covered": False}
                return {"covered": True, "answer": "The linked page says a landlord must give at least 14 days notice before an inspection.",
                        "page": 1, "quote": "a landlord must give at least 14 days notice before entering a rented home for an inspection"}
            if "traffic" in q:
                return {"covered": False}
            return GOOD
        if name == "report_check":
            return {"supported": True}
        if name == "report_checks":
            return {"results": [{"id": i["id"], "supported": True} for i in json.loads(user.split("\n\n", 1)[1])["items"]]}
        if name == "report_term":
            return {"known": True, "meaning": "It stops a person going back on something they said, if others relied on it."}
        return OVERVIEW


def sample_with_links():
    ex = documents.read_upload("Sample.pdf", documents.SAMPLE_PDF.read_bytes())
    ex.links = [LinkRef("https://www.legislation.nsw.gov.au/act", 3), LinkRef("https://example.org/blocked", 4)]
    return ex


def fake_fetch_many(urls, official, max_links=10, **kw):
    page = links.FetchedPage("https://www.legislation.nsw.gov.au/act", "Residential Tenancies Act", [Page(1, LINKED_TEXT, local=1)], "part", True)
    return [
        links.LinkResult(page.url, True, page.title, True, fetched=page),
        links.LinkResult("https://example.org/blocked", False, reason="That page needs a login or blocks readers, so I can't open it."),
    ]


ai.set_client(FakeAI())
app.load_sample = sample_with_links
links.fetch_many = fake_fetch_many
uvicorn.run(app.app, host="127.0.0.1", port=8123, log_level="warning")
