"""Guards (SPEC.md SEC-1, SEC-2, PRIV-2). Defense in depth: they lower risk, they do not grant anything.

- strip_external_links: removes links and images to hosts off the allowlist (enforces RESP-1).
- flags_injection: rule-based check for instruction-override or prompt-extraction attempts.
- redact: replaces emails, phone numbers and SSNs before anything is logged.
"""

from __future__ import annotations

import re
import urllib.parse

from agent.config import load_facts

_MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![(\[])\b((?:https?://|www\.)[^\s<>()\]]+)")
_HOSTLIKE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|ly|co|info|app|site|xyz)(?:/[^\s)]*)?\b", re.I)


def allowed_hosts() -> set[str]:
    return set(load_facts()["link_allowlist"])


def _host(url: str) -> str | None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return urllib.parse.urlparse(url).hostname


def strip_external_links(text: str, allow: set[str] | None = None) -> tuple[str, list[str]]:
    """Remove links to non-allowlisted hosts. Images to any host are removed (tracking pixels)."""
    allow = allowed_hosts() if allow is None else allow
    removed: list[str] = []

    def md(m: re.Match) -> str:
        is_image, label, url = m.group(1), m.group(2), m.group(3)
        if is_image or _host(url) not in allow:
            removed.append(url)
            return "" if is_image else label
        return m.group(0)

    text = _MD_LINK.sub(md, text)

    def bare(m: re.Match) -> str:
        url = m.group(1).rstrip(".,;:!?")
        if _host(url) in allow:
            return m.group(0)
        removed.append(url)
        return "[link removed]" + m.group(1)[len(url):]

    text = _BARE_URL.sub(bare, text)

    def hostlike(m: re.Match) -> str:  # "nyc-gov-tenants.com" written without a scheme
        if _host(m.group(0)) in allow or "@" in text[max(0, m.start() - 1):m.start()]:
            return m.group(0)
        removed.append(m.group(0))
        return "[link removed]"

    text = _HOSTLIKE.sub(hostlike, text)
    return text, removed


_INJECTION = [
    r"\bignore (all |any )?(of )?(your |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"\b(forget|disregard|override) (all |any )?(of )?(your |the )?(rules|instructions|guidelines)",
    r"\b(system|developer) prompt\b", r"\byour (instructions|rules|prompt)\b.*\b(print|show|repeat|reveal|summari[sz]e|list)",
    r"\b(print|show|repeat|reveal|output)\b.{0,40}\b(above|instructions|system prompt|everything before)",
    r"\bdeveloper mode\b|\bDAN\b|\bjailbreak", r"^\s*(SYSTEM|ADMIN)\s*:",
    r"\bwhat (instructions|rules) were you given\b",
]


def flags_injection(text: str) -> bool:
    return any(re.search(p, text, re.I | re.M) for p in _INJECTION)


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _SSN.sub("[ssn]", text)
    return _PHONE.sub("[phone]", text)
