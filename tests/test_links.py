import socket

import httpx
import pytest

import links

OFFICIAL = [".gov.au", ".gov.uk", ".gov"]
REAL_CLIENT = httpx.Client


def fake_dns(mapping):
    def getaddrinfo(host, port, *a, **k):
        if host not in mapping:
            raise socket.gaierror
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    return getaddrinfo


@pytest.fixture
def dns(monkeypatch):
    monkeypatch.setattr(
        links.socket,
        "getaddrinfo",
        fake_dns(
            {
                "example.org": "93.184.216.34",
                "other.example.org": "93.184.216.35",
                "internal.example": "10.0.0.5",
                "www.legislation.nsw.gov.au": "104.18.1.1",
                "meta.example": "169.254.169.254",
            }
        ),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/page",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://10.1.2.3/x",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@example.org/",
        "https://example.org:8443/",
        "https://internal.example/x",
        "https://meta.example/x",
        "https://does-not-exist.example/",
        "ftp://example.org/file",
        "not a url",
    ],
)
def test_unsafe_urls_are_rejected(dns, url):
    with pytest.raises(links.LinkError):
        links.validate_url(url)


def test_public_https_url_is_accepted(dns):
    assert links.validate_url("https://example.org/page")[1] == "example.org"


def test_official_site_matching():
    assert links.host_is_official("www.legislation.nsw.gov.au", OFFICIAL)
    assert links.host_is_official("legislation.gov.uk", OFFICIAL)
    assert not links.host_is_official("gov.au.evil.com", OFFICIAL)
    assert not links.host_is_official("notgov.au", OFFICIAL)
    assert not links.host_is_official("example.org", OFFICIAL)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://example.org/a", "https://example.org/a"),  # upgraded
        ("https://example.org/a", "https://example.org/a"),
        ("mailto:someone@example.org", None),
        ("https://example.org/logo.png", None),
        ("https://example.org/form.docx", None),
        ("javascript:alert(1)", None),
    ],
)
def test_prepare_link(raw, expected):
    assert links.prepare_link(raw) == expected


def test_html_is_reduced_to_text_without_scripts_or_menus():
    html = "<html><head><title>My page</title><script>alert(1)</script></head><body><nav>MENU</nav><main><h1>Heading</h1><p>Real text</p></main><footer>FOOT</footer></body></html>"
    title, text = links.html_to_text(html)
    assert title == "My page"
    assert "Real text" in text and "alert" not in text and "MENU" not in text and "FOOT" not in text


def _patch_transport(monkeypatch, handler):
    monkeypatch.setattr(links.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


PAGE = "<html><title>{t}</title><body><main>" + ("Plain words about the law and what it means for you. " * 12) + "</main></body></html>"


def html_response(req):
    return httpx.Response(200, text=PAGE.format(t=req.url.host), headers={"content-type": "text/html"})


def test_fetches_and_reads_a_page(dns, monkeypatch):
    _patch_transport(monkeypatch, html_response)
    page = links.fetch_public_page("https://www.legislation.nsw.gov.au/a", OFFICIAL)
    assert page.official and page.label == "part" and "law" in page.pages[0].text


def test_redirect_to_private_address_is_blocked(dns, monkeypatch):
    def handler(req):
        if req.url.host == "example.org":
            return httpx.Response(302, headers={"location": "https://internal.example/secret"})
        return httpx.Response(200, text="secret")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(links.LinkError):
        links.fetch_public_page("https://example.org/go", OFFICIAL)


def test_redirect_loop_is_stopped(dns, monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(302, headers={"location": "https://example.org/again"}))
    with pytest.raises(links.LinkError):
        links.fetch_public_page("https://example.org/a", OFFICIAL)


def test_oversized_page_is_refused(dns, monkeypatch):
    big = "x" * (links.MAX_BYTES + 10)
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, text=big, headers={"content-type": "text/html"}))
    with pytest.raises(links.LinkError):
        links.fetch_public_page("https://example.org/big", OFFICIAL)


def test_login_wall_gives_a_plain_message(dns, monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(403))
    with pytest.raises(links.LinkError) as e:
        links.fetch_public_page("https://example.org/private", OFFICIAL)
    assert "login" in str(e.value)


def test_pdf_is_read(dns, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "The council will hold a public meeting about the plan on a weekday evening.")
    data = doc.tobytes()
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, content=data, headers={"content-type": "application/pdf"}))
    page = links.fetch_public_page("https://example.org/file.pdf", OFFICIAL)
    assert page.label == "page" and "public meeting" in page.pages[0].text


# ------------------------------------------------------------------ fetch_many

def test_fetch_many_reads_official_first_skips_junk_and_reports_failures(dns, monkeypatch):
    def handler(req):
        if req.url.host == "other.example.org":
            return httpx.Response(404)
        return html_response(req)

    _patch_transport(monkeypatch, handler)
    urls = [
        "https://example.org/a",
        "https://other.example.org/missing",
        "http://www.legislation.nsw.gov.au/act",  # http is upgraded
        "https://example.org/a",  # duplicate
        "mailto:x@example.org",
        "https://example.org/pic.png",
        "https://internal.example/x",  # private, must fail safely
    ]
    results = links.fetch_many(urls, OFFICIAL, max_links=10)
    by_url = {r.url: r for r in results}
    assert results[0].url == "https://www.legislation.nsw.gov.au/act" and results[0].ok and results[0].official
    assert by_url["https://example.org/a"].ok and not by_url["https://example.org/a"].official
    assert not by_url["https://other.example.org/missing"].ok and "404" in by_url["https://other.example.org/missing"].reason
    assert not by_url["https://internal.example/x"].ok and "private" in by_url["https://internal.example/x"].reason
    assert not by_url["mailto:x@example.org"].ok
    assert sum(1 for r in results if r.url == "https://example.org/a") == 1


def test_fetch_many_stops_at_the_limit(dns, monkeypatch):
    _patch_transport(monkeypatch, html_response)
    urls = [f"https://example.org/p{i}" for i in range(15)]
    results = links.fetch_many(urls, OFFICIAL, max_links=10)
    assert sum(1 for r in results if r.ok) == 10
    assert sum(1 for r in results if "Skipped" in r.reason) == 5
