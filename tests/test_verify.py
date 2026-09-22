from documents import Page, Source, find_quote, load_sample, numbers_in, numbers_supported, squash, to_source

SRC = to_source(load_sample())


def test_squash_ignores_layout_and_curly_quotes():
    assert squash("the Developer\u2019s \n Works") == squash("The developer's works")


def test_real_quote_is_found_across_line_breaks():
    # In the PDF this sentence is split over several lines.
    q = "being a minimum of 15% of total Gross Floor Area of the Detailed Development Consent"
    assert find_quote(SRC, q) == 27


def test_wrong_page_hint_is_corrected():
    q = "The Affordable Housing will be in perpetuity as opposed to the 15 years mandated by the Housing SEPP."
    assert find_quote(SRC, q, prefer_page=3) == 28


def test_altered_quote_is_rejected():
    q = "being a minimum of 25% of total Gross Floor Area of the Detailed Development Consent"
    assert find_quote(SRC, q) is None


def test_invented_quote_is_rejected():
    assert find_quote(SRC, "The council will approve the development and reduce traffic on Mitchell Road.") is None


def test_too_short_quote_is_rejected():
    assert find_quote(SRC, "the City") is None


def test_stitched_quote_with_ellipsis_is_rejected():
    assert find_quote(SRC, "The Landowner must, at its cost and risk ... in accordance with this document.") is None


def test_numbers_extracted_without_clause_references():
    assert numbers_in("See clause 2.3 on page 12: 13,592.7 square metres and 15% for 12 months") == ["13592.7", "15", "12"]


def test_number_check_accepts_figures_on_the_page():
    ok, bad = numbers_supported("It must be at least 15% or 13,592.7 square metres.", SRC.page(27).text)
    assert ok and bad is None


def test_number_check_rejects_invented_figure():
    ok, bad = numbers_supported("It must be at least 77777 square metres.", SRC.page(27).text)
    assert not ok and bad == "77777"


def test_source_marks_pages_for_the_model():
    s = Source("x", "main", [Page(1, "hello"), Page(2, "world")])
    assert "[PAGE 1]\nhello" in s.text and "[PAGE 2]\nworld" in s.text


def test_sample_loaded_with_printed_numbers():
    assert len(SRC.pages) == 44 and SRC.label == "page"
    assert SRC.page(2).printed == "1"  # PDF page 2 is printed page 1
    assert SRC.page(1).printed is None  # cover
