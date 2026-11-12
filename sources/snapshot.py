"""Save the approved law sources as Markdown sections.

Reads sources.yaml and, for every `kind: snapshot` source, downloads the
source, splits it into sections, and writes one Markdown file per section to
data/sources/<source_id>/. The search_sources and get_source_section tools
read only these files, so the agent can cite nothing that is not saved here
(SPEC.md GROUND-1, GROUND-2).

Each file has YAML front matter:

    source_id   the source's id in sources.yaml
    section_id  stable id within the source, used by get_source_section
    title       section heading
    url         the page (with an anchor where the site has one) for RESP-1
    order       position of the section within the source
    retrieved   date the snapshot was taken
    sha256      hash of the body, so a re-run shows which sections changed

Usage:

    uv run python -m sources.snapshot                  # every snapshot source
    uv run python -m sources.snapshot --only hstpa-2019

Re-running replaces a source's folder only when the download succeeds, so a
failed or skipped source keeps its previous snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_YAML = REPO_ROOT / "sources.yaml"
OUT_DIR = REPO_ROOT / "data" / "sources"

# The Rent Guidelines Board and nyc.gov reject short user agents; DHCR rejects
# browser-like ones. Each fetcher picks the one its site accepts.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PLAIN_UA = "Mozilla/5.0"

RGB_WP_API = "https://rentguidelinesboard.cityofnewyork.us/wp-json/wp/v2/pages"
NYC_CONTENT_API = "https://apps.nyc.gov/content-api/v1/content"
NYSENATE_API = "https://legislation.nysenate.gov/api/3/laws"


@dataclass
class Section:
    section_id: str
    title: str
    url: str
    text: str


class SkipSource(Exception):
    """The source cannot be fetched now for a known reason (e.g. no API key)."""


# --- HTTP and text helpers --------------------------------------------------


def fetch(url: str, ua: str = BROWSER_UA) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_json(url: str, ua: str = BROWSER_UA):
    return json.loads(fetch(url, ua))


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].rstrip("-") or "section"


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def inline_markdown(node: Tag, base_url: str) -> str:
    """Text of an element, with links kept as Markdown links."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif child.name == "a" and child.get("href"):
            label = child.get_text(" ", strip=True)
            href = urllib.parse.urljoin(base_url, child["href"])
            parts.append(f"[{label}]({href})" if label else "")
        elif child.name == "br":
            parts.append("\n")
        elif isinstance(child, Tag):
            parts.append(inline_markdown(child, base_url))
    return re.sub(r"[ \t\n]+", " ", "".join(parts)).strip()


# Page-navigation blocks that carry no content ("Back to Top", "On this page").
NAV_TEXT = re.compile(r"^\[?(back to top|on this page|skip nav)\b", re.I)

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
BLOCKS = HEADINGS + ["p", "li", "tr", "blockquote", "pre"]


def html_sections(
    html: str,
    page_url: str,
    page_title: str,
    page_slug: str,
    split_levels: tuple[str, ...] = ("h2", "h3", "h4", "h5"),
) -> list[Section]:
    """Split an HTML fragment into sections at headings.

    Text before the first heading becomes a section named after the page. A
    heading with an `id` attribute gives its section a URL anchor.
    """
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select("script, style, nav, .sr-only"):
        junk.decompose()

    sections: list[Section] = []
    current = Section(page_slug, page_title, page_url, "")
    lines: list[str] = []

    def close() -> None:
        current.text = clean_text("\n\n".join(lines))
        # Keep only sections with content beyond headings.
        if any(not line.startswith("#") for line in current.text.splitlines() if line):
            sections.append(current)

    for el in soup.find_all(BLOCKS):
        # Skip blocks nested in another block we already emit (e.g. p in li).
        if el.find_parent(BLOCKS):
            continue
        text = inline_markdown(el, page_url)
        if not text or NAV_TEXT.match(text):
            continue
        if el.name in split_levels:
            close()
            anchor = el.get("id") or (el.find(attrs={"id": True}) or {}).get("id")
            title = el.get_text(" ", strip=True)
            current = Section(
                f"{page_slug}--{slugify(anchor or title)}",
                title,
                f"{page_url}#{anchor}" if anchor else page_url,
                "",
            )
            lines = []
        elif el.name in HEADINGS:
            lines.append(f"{'#' * (int(el.name[1]) + 1)} {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        elif el.name == "tr":
            cells = [inline_markdown(c, page_url) for c in el.find_all(["th", "td"])]
            lines.append(" | ".join(cells))
        else:
            lines.append(text)
    close()
    return sections


# --- Fetchers, one per `fetch.method` in sources.yaml -----------------------


def fetch_wordpress(source: dict) -> list[Section]:
    sections: list[Section] = []
    for slug in source["fetch"]["slugs"]:
        url = f"{RGB_WP_API}?slug={slug}&_fields=link,title,content,modified"
        pages = fetch_json(url)
        if not pages:
            raise RuntimeError(f"no Rent Guidelines Board page with slug {slug!r}")
        page = pages[0]
        title = BeautifulSoup(page["title"]["rendered"], "html.parser").get_text()
        sections += html_sections(page["content"]["rendered"], page["link"], title, slug)
    return sections


def fetch_nyc_content_api(source: dict) -> list[Section]:
    site = source["fetch"]["site"]
    sections: list[Section] = []
    for page in source["fetch"]["pages"]:
        data = fetch_json(f"{NYC_CONTENT_API}/{site}/{page}")
        page_url = f"https://www.nyc.gov/content/{site}/pages/{page}"
        html = data.get("description") or ""
        if not html.strip():
            raise RuntimeError(f"nyc.gov content API returned no content for {page!r}")
        # The page's own h1 repeats the title, so split below it.
        sections += html_sections(html, page_url, data["name"], page, ("h2", "h3"))
    return sections


PDF_LINE_NUMBER = re.compile(r"^\s{0,8}\d{1,2}(\s{2,}|$)")
PDF_RUNNING_HEAD = re.compile(r"^\s*S\.\s*6458\s+\d+\s+A\.\s*8281\s*$")
HSTPA_PART = re.compile(r"^\s*PART\s+([A-Z])\s*$")


def pdf_pages(data: bytes) -> list[str]:
    return [page.extract_text(extraction_mode="layout") or "" for page in PdfReader(io.BytesIO(data)).pages]


HSTPA_SECTION = re.compile(r"^(?:§|Section)\s*(\d+(?:-[a-z])?)\.\s+(.*)")
HSTPA_SUMMARY_END = re.compile(r"\s(?:is|are)\s+(?:hereby\s+)?(?:amended|added|repealed|renumbered)|\sto read as follows")


def _law_name(summary: str) -> str | None:
    """A plain name for the law a section amends, so searches in plain words find it."""
    low = summary.lower()
    m = re.search(r"section 26-(\d+)", low)
    if m and "administrative code" in low:
        n = int(m.group(1))
        if 501 <= n <= 520:
            return "NYC Rent Stabilization Law (rent-stabilized apartments)"
        if 401 <= n <= 415:
            return "NYC rent control law (rent-controlled apartments)"
    for pattern, name in (
        (r"chapter 576 of the laws of 1974", "Emergency Tenant Protection Act (rent stabilization)"),
        (r"chapter 274 of the laws of 1946", "Emergency Housing Rent Control Law (rent control)"),
        (r"general obligations law", "General Obligations Law"),
        (r"real property actions and proceedings law", "Real Property Actions and Proceedings Law (evictions)"),
        (r"real property law", "Real Property Law"),
        (r"real property tax law", "Real Property Tax Law"),
    ):
        if re.search(pattern, low):
            return name
    return None


def _hstpa_summary(text: str) -> str:
    """The opening of a section, up to "is amended" or similar: which law it changes."""
    flat = re.sub(r"\[PDF page \d+\]", " ", text)
    flat = re.sub(r"-\s*\n\s*", "", flat)  # rejoin words hyphenated across lines
    flat = " ".join(flat.split())
    flat = HSTPA_SECTION.sub(r"\2", flat, count=1)
    m = HSTPA_SUMMARY_END.search(flat)
    summary = flat[: m.start()] if m else flat[:110]
    return summary[:140].rstrip(" ,;") + ("…" if len(summary) > 140 or not m else "")


def fetch_pdf_hstpa(source: dict) -> list[Section]:
    """Split the HSTPA bill text into its numbered sections, grouped by Part (A through O).

    Each section is titled with its Part, number, and the law it changes
    ("Part I, § 3: Section 26-511 of the administrative code…"), so a search
    lands on the paragraph that answers a question instead of a whole Part.
    The bill marks deleted text with [brackets] and new text with
    underlining; extraction keeps the brackets but loses the underlining, so
    each section carries a note saying how to read it.
    """
    url = source["url"]
    note = (
        "> 2019 bill text as passed. Text in [brackets] was deleted by the act; added text is not marked "
        "in this copy. This act is the approved source for rent-stabilized and rent-controlled rules, "
        "which the NY Senate snapshots do not include."
    )
    sections: list[Section] = []
    part, number, start_page, lines = None, None, 1, []

    def close(end_page: int) -> None:
        body = clean_text("\n".join(lines))
        if not re.sub(r"\[PDF page \d+\]", "", body).strip():
            return
        pages = f"{start_page}" if start_page == end_page else f"{start_page}-{end_page}"
        if part is None:
            title, sid = "Title and summary", "title-and-summary"
        elif number is None:
            title, sid = f"{part} heading", slugify(f"{part} heading")
        else:
            summary = _hstpa_summary(body)
            law = _law_name(summary)
            title = f"{part}, § {number}" + (f", {law}" if law else "") + f": {summary}"
            sid = slugify(f"{part} s {number}")
        sections.append(Section(sid, f"{title} (PDF p. {pages})", f"{url}#page={start_page}", f"{note}\n\n{body}"))

    page_no = 1
    for page_no, text in enumerate(pdf_pages(fetch(url)), start=1):
        lines.append(f"[PDF page {page_no}]")
        for raw in text.splitlines():
            if PDF_RUNNING_HEAD.match(raw):
                continue
            line = PDF_LINE_NUMBER.sub("", raw, count=1).strip()
            if m := HSTPA_PART.match(line):
                close(page_no)
                part, number, start_page, lines = f"Part {m.group(1)}", None, page_no, [f"[PDF page {page_no}]"]
                continue
            if part is not None and (m := HSTPA_SECTION.match(line)):
                close(page_no)
                number, start_page, lines = m.group(1), page_no, [f"[PDF page {page_no}]"]
            lines.append(line)
    close(page_no)
    return sections


def fetch_pdf_pages(source: dict) -> list[Section]:
    """A short PDF, one section per page, titled from `fetch.page_titles`.

    Uses pypdf's plain extraction: layout mode splits some numbers apart
    ("$296,8    00"). Spaces after a dollar sign are closed up so amounts read
    as "$50,880".
    """
    url = source["url"]
    titles = source["fetch"].get("page_titles", [])
    sections = []
    ua = PLAIN_UA if source["fetch"].get("user_agent") == "plain" else BROWSER_UA
    for page_no, page in enumerate(PdfReader(io.BytesIO(fetch(url, ua))).pages, start=1):
        text = clean_text(re.sub(r"\$\s+(?=\d)", "$", page.extract_text() or ""))
        text = re.sub(r"^\d+ of \d+ \w+ \d{4}\s*", "", text)  # running footer pypdf puts first
        if not text:
            continue
        title = titles[page_no - 1] if page_no <= len(titles) else f"{source['title']}, page {page_no}"
        sections.append(Section(slugify(title), title, f"{url}#page={page_no}", text))
    return sections


def fetch_dhcr(source: dict) -> list[Section]:
    html = fetch(source["url"], PLAIN_UA).decode("utf-8", "replace")
    main = BeautifulSoup(html, "html.parser").find("main")
    if main is None:
        raise RuntimeError("DHCR page has no <main> element")
    # Drop the "On this page" menu, whose items repeat the h2 headings.
    for menu in main.find_all(lambda t: t.name in ("ul", "ol") and t.find("a", href=re.compile(r"^#"))):
        menu.decompose()
    sections = html_sections(str(main), source["url"], "Good Cause Eviction", "overview", ("h2",))

    notice_url = source["fetch"]["notice_pdf"]
    for page_no, text in enumerate(pdf_pages(fetch(notice_url, PLAIN_UA)), start=1):
        body = clean_text(text)
        if body:
            sections.append(
                Section(
                    f"notice-page-{page_no}",
                    f"Good Cause Eviction Law Notice, page {page_no}",
                    f"{notice_url}#page={page_no}",
                    body,
                )
            )
    return sections


def fetch_nysenate_api(source: dict) -> list[Section]:
    key = os.environ.get("NYSENATE_API_KEY", "").strip()
    if not key:
        raise SkipSource("NYSENATE_API_KEY is not set (see .env.example)")
    law, article = source["fetch"]["law"], source["fetch"]["article"]
    tree = fetch_json(f"{NYSENATE_API}/{law}?full=true&key={urllib.parse.quote(key)}")
    if not tree.get("success"):
        raise RuntimeError(f"Open Legislation API error: {tree.get('message')}")

    def find(doc: dict) -> dict | None:
        if doc.get("docType") == "ARTICLE" and doc.get("locationId") == article:
            return doc
        for child in (doc.get("documents") or {}).get("items", []):
            if (hit := find(child)) is not None:
                return hit
        return None

    def sections_under(doc: dict):
        for child in (doc.get("documents") or {}).get("items", []):
            if child.get("docType") == "SECTION":
                yield child
            else:
                yield from sections_under(child)

    article_doc = find(tree["result"]["documents"])
    if article_doc is None:
        raise RuntimeError(f"article {article} not found in {law}")

    sections: list[Section] = []
    for doc in sections_under(article_doc):
        if doc.get("repealed"):
            continue
        loc = doc["locationId"]
        # The API returns line breaks as the two characters "\n".
        text = (doc.get("text") or "").replace("\\n", "\n")
        sections.append(
            Section(
                slugify(f"section-{loc}"),
                f"§ {doc['docLevelId']} {doc.get('title') or ''}".strip(),
                f"https://www.nysenate.gov/legislation/laws/{law}/{loc}",
                clean_text(text),
            )
        )
    if not sections:
        raise RuntimeError(f"no sections found under {law} {article}")
    return sections


FETCHERS = {
    "wordpress": fetch_wordpress,
    "nyc_content_api": fetch_nyc_content_api,
    "pdf": fetch_pdf_hstpa,
    "pdf_pages": fetch_pdf_pages,
    "dhcr": fetch_dhcr,
    "nysenate_api": fetch_nysenate_api,
}


# --- Writing ----------------------------------------------------------------


def write_source(source_id: str, sections: list[Section], out_dir: Path = OUT_DIR) -> Path:
    """Replace data/sources/<source_id>/ with the new sections."""
    seen: dict[str, int] = {}
    target = out_dir / source_id
    tmp = out_dir / f".{source_id}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    today = date.today().isoformat()
    for order, sec in enumerate(sections, start=1):
        # Two headings with the same text would collide; number the repeats.
        n = seen.get(sec.section_id, 0)
        seen[sec.section_id] = n + 1
        section_id = sec.section_id if n == 0 else f"{sec.section_id}-{n + 1}"
        front = {
            "source_id": source_id,
            "section_id": section_id,
            "title": sec.title,
            "url": sec.url,
            "order": order,
            "retrieved": today,
            "sha256": hashlib.sha256(sec.text.encode()).hexdigest(),
        }
        body = f"---\n{yaml.safe_dump(front, sort_keys=False, allow_unicode=True)}---\n\n# {sec.title}\n\n{sec.text}\n"
        (tmp / f"{order:03d}-{section_id}.md").write_text(body)
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    return target


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=value lines from .env without overriding the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="source ids to snapshot")
    args = parser.parse_args(argv)
    load_env()

    registry = yaml.safe_load(SOURCES_YAML.read_text())["sources"]
    targets = [s for s in registry if s["kind"] == "snapshot"]
    if args.only:
        unknown = set(args.only) - {s["id"] for s in targets}
        if unknown:
            parser.error(f"not snapshot sources: {', '.join(sorted(unknown))}")
        targets = [s for s in targets if s["id"] in args.only]

    failed = False
    for source in targets:
        try:
            sections = FETCHERS[source["fetch"]["method"]](source)
            write_source(source["id"], sections)
            print(f"ok       {source['id']}: {len(sections)} sections")
        except SkipSource as e:
            print(f"skipped  {source['id']}: {e}")
        except Exception as e:  # keep going; report every failure at the end
            failed = True
            print(f"FAILED   {source['id']}: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
