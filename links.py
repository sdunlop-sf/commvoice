"""Read a web page or PDF that a resident pastes, safely.

A user-supplied URL is dangerous: it can point at private servers (SSRF),
huge files, or pages that try to give the AI instructions. This module:
  * accepts public https URLs only
  * refuses private, loopback, link-local and reserved addresses (also after redirects)
  * stops after a size and time limit
  * returns plain text only (never HTML) and marks it as data, not instructions
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from concurrent.futures import ThreadPoolExecutor, as_completed

import pymupdf

from documents import Page, clean_page_text, split_into_parts

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 90_000
USER_AGENT = "ComVoiceBot/1.0 (+plain-language reader; contact: site owner)"


class LinkError(Exception):
    """A problem the resident can be told about in plain words."""


@dataclass
class FetchedPage:
    url: str
    title: str
    pages: list[Page]
    label: str  # "page" or "part"
    official: bool


def host_is_official(host: str, suffixes: list[str]) -> bool:
    """True for government sites, e.g. legislation.nsw.gov.au (matches the ending of the host name)."""
    host = host.lower().rstrip(".")
    return any(host.endswith(s if s.startswith(".") else "." + s) or host == s.lstrip(".") for s in suffixes)


SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4", ".css", ".js")


def prepare_link(url: str) -> str | None:
    """Return a link worth reading (upgraded to https), or None to skip it."""
    url = url.strip()
    if url.lower().startswith("http://"):
        url = "https://" + url[7:]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    return url


def _check_ip(ip: str) -> None:
    addr = ipaddress.ip_address(ip)
    if not addr.is_global or addr.is_multicast:
        raise LinkError("That link points to a private or internal address, so I can't open it.")


def validate_url(url: str) -> tuple[str, str]:
    """Return (clean_url, hostname) or raise LinkError."""
    url = (url or "").strip()
    if len(url) > 2000:
        raise LinkError("That link is too long.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise LinkError("Please paste a link that starts with https://")
    if not parsed.hostname:
        raise LinkError("That doesn't look like a web link.")
    if parsed.username or parsed.password:
        raise LinkError("Links with a username or password aren't allowed.")
    if parsed.port not in (None, 443):
        raise LinkError("Only standard web links are allowed.")
    host = parsed.hostname
    try:
        # A literal IP address in the URL
        _check_ip(host)
    except ValueError:
        pass  # not an IP literal, so it is a name: resolve it below
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise LinkError("I couldn't find that website.")
    for info in infos:
        _check_ip(info[4][0])
    return url, host


def _read_limited(resp: httpx.Response) -> bytes:
    total = 0
    chunks: list[bytes] = []
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            raise LinkError("That page is too big for me to read (limit 2 MB).")
        chunks.append(chunk)
    return b"".join(chunks)


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [ln.strip() for ln in main.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return title, text[:MAX_TEXT_CHARS]


def fetch_public_page(url: str, official_suffixes: list[str]) -> FetchedPage:
    current, host = validate_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1"}
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT_SECONDS, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise LinkError("That link redirects nowhere.")
                        current, host = validate_url(urljoin(current, location))
                        continue
                    if resp.status_code in (401, 403):
                        raise LinkError("That page needs a login or blocks readers, so I can't open it.")
                    if resp.status_code >= 400:
                        raise LinkError(f"That page didn't open (error {resp.status_code}).")
                    ctype = resp.headers.get("content-type", "").lower()
                    body = _read_limited(resp)
            except httpx.TimeoutException:
                raise LinkError("That page took too long to open.")
            except httpx.HTTPError:
                raise LinkError("I couldn't open that link.")
            break
        else:
            raise LinkError("That link redirects too many times.")

    official = host_is_official(host, official_suffixes)

    if "pdf" in ctype or body[:5] == b"%PDF-":
        try:
            pages = _pdf_pages(body)
        except Exception:
            raise LinkError("I couldn't read that PDF.")
        if not any(p.text for p in pages):
            raise LinkError("That PDF has no text I can read (it may be a scan).")
        return FetchedPage(current, urlparse(current).path.rsplit("/", 1)[-1] or host, pages, "page", official)

    if "html" in ctype or "text" in ctype or not ctype:
        charset = "utf-8"
        title, text = html_to_text(body.decode(charset, errors="replace"))
        if len(text) < 200:
            raise LinkError("I couldn't find readable text on that page. It may need JavaScript or a login.")
        return FetchedPage(current, title or host, split_into_parts(text), "part", official)

    raise LinkError("I can only read web pages and PDFs.")


def _pdf_pages(data: bytes, max_pages: int = 60) -> list[Page]:
    pages: list[Page] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            if i > max_pages:
                break
            pages.append(Page(i, clean_page_text(page.get_text()), local=i))
    return pages


@dataclass
class LinkResult:
    url: str
    ok: bool
    title: str = ""
    official: bool = False
    reason: str = ""
    fetched: FetchedPage | None = None


def fetch_many(urls: list[str], official_suffixes: list[str], max_links: int = 10, workers: int = 5, budget_seconds: int = 45) -> list[LinkResult]:
    """Read up to `max_links` links from a document, one level deep, safely and in parallel.

    Government sites are read first because they are the likeliest to be real references.
    """
    wanted: list[str] = []
    skipped: list[LinkResult] = []
    seen: set[str] = set()
    for raw in urls:
        url = prepare_link(raw)
        if url is None:
            skipped.append(LinkResult(raw, False, reason="Not a web page or PDF I can read."))
            continue
        if url in seen:
            continue
        seen.add(url)
        wanted.append(url)
    wanted.sort(key=lambda u: 0 if host_is_official(urlparse(u).hostname or "", official_suffixes) else 1)
    extra = wanted[max_links:]
    wanted = wanted[:max_links]

    def one(url: str) -> LinkResult:
        try:
            page = fetch_public_page(url, official_suffixes)
            return LinkResult(url, True, page.title, page.official, fetched=page)
        except LinkError as exc:
            return LinkResult(url, False, reason=str(exc))
        except Exception:
            return LinkResult(url, False, reason="I couldn't open that link.")

    results: dict[str, LinkResult] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(one, u): u for u in wanted}
    try:
        for fut in as_completed(futures, timeout=budget_seconds):
            results[futures[fut]] = fut.result()
    except Exception:  # overall time budget used up
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    ordered = [results.get(u) or LinkResult(u, False, reason="That took too long, so I stopped.") for u in wanted]
    ordered += [LinkResult(u, False, reason=f"Skipped: I read {max_links} links at most.") for u in extra]
    return ordered + skipped
