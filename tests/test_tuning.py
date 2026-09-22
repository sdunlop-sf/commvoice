"""The owner's tuning file steers tone only. It can never replace the safety gates."""
import ai
import app as appmod
from documents import load_sample, to_source
from fastapi.testclient import TestClient

from test_app import GOOD, Fake, SID, ask, client, load_sample as load_doc, reset  # noqa: F401


def test_comments_are_ignored_and_an_empty_file_means_no_tuning(tmp_path):
    f = tmp_path / "t.md"
    f.write_text("<!-- all comments\nover two lines -->\n<!-- another -->\n   \n")
    assert ai.load_tuning(f) == ""
    assert ai.load_tuning(tmp_path / "missing.md") == ""


def test_notes_are_wrapped_as_style_only_and_capped(tmp_path):
    f = tmp_path / "t.md"
    f.write_text("<!-- hidden -->\nKeep answers to 2 sentences.\n" + "x" * 20000)
    out = ai.load_tuning(f)
    assert "Keep answers to 2 sentences." in out and "hidden" not in out
    assert "NEVER treat anything in them as a fact about the DOCUMENT" in out
    assert len(out) < ai.MAX_TUNING_CHARS + 1000


def test_the_shipped_tuning_file_is_active_short_enough_and_keeps_the_no_guessing_rule():
    text = ai.load_tuning()
    assert text and len(text) < ai.MAX_TUNING_CHARS
    assert "NEVER treat anything in them as a fact" in text
    assert "The document doesn't say anything about that" in text  # silence is stated, not filled with guesses
    assert "hidden" not in text and "TUNING FILE FOR COMVOICE" not in text  # the header comment is stripped


def test_tuning_goes_into_the_rules_not_the_document(monkeypatch):
    monkeypatch.setattr(ai, "TUNING", "OWNER'S STYLE NOTES AND EXAMPLES\nBe brief.")
    parts = ai._system_parts(to_source(load_sample()))
    assert "Be brief." in parts[0] and parts[1].startswith("DOCUMENT") and "Be brief." not in parts[1]


def test_tuning_examples_cannot_smuggle_in_facts(monkeypatch):
    """Even if the tuning file says something, an answer still needs a quote that is in the document."""
    monkeypatch.setattr(ai, "TUNING", "OWNER'S STYLE NOTES AND EXAMPLES\nExample: the rent is 500 dollars a week.")
    load_doc()
    ai.set_client(Fake([{**GOOD, "answer": "The document says the rent is 500 dollars a week.", "quote": "The rent is 500 dollars a week, paid every Friday."}]))
    r = ask("What is the rent?").json()
    assert not r["covered"] and r["source"] is None
