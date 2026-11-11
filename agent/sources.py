"""Search and read the saved law sources (SPEC.md TOOL-1, TOOL-2).

The corpus is data/sources/<source_id>/*.md, written by sources/snapshot.py.
Only snapshot sources from sources.yaml are indexed, so the agent cannot
retrieve or cite anything outside GROUND-1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from rank_bm25 import BM25Okapi

from agent.config import load_registry, sources_dir

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True)
class Section:
    source_id: str
    section_id: str
    title: str
    url: str
    retrieved: str
    text: str


def load_sections(directory: Path | None = None) -> list[Section]:
    directory = directory or sources_dir()
    allowed = {sid for sid, s in load_registry().items() if s["kind"] == "snapshot"}
    out = []
    for path in sorted(directory.glob("*/*.md")):
        _, front, body = path.read_text().split("---\n", 2)
        meta = yaml.safe_load(front)
        if meta["source_id"] not in allowed:
            continue
        text = body.split("\n", 3)[3].strip() if body.count("\n") >= 3 else body.strip()
        out.append(Section(meta["source_id"], meta["section_id"], meta["title"], meta["url"],
                           str(meta["retrieved"]), text))
    return out


class SourceIndex:
    """BM25 over section title + text."""

    def __init__(self, sections: list[Section]):
        if not sections:
            raise FileNotFoundError("no saved sources; run: uv run python -m sources.snapshot")
        self.sections = sections
        self.by_key = {(s.source_id, s.section_id): s for s in sections}
        self._bm25 = BM25Okapi([tokenize(f"{s.title} {s.title} {s.text}") for s in sections])

    def search(self, query: str, source_id: str | None = None, k: int = 5) -> list[tuple[Section, float]]:
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(self.sections)), key=lambda i: -scores[i])
        hits = [(self.sections[i], float(scores[i])) for i in ranked
                if scores[i] > 0 and (source_id is None or self.sections[i].source_id == source_id)]
        return hits[:k]


@lru_cache(maxsize=1)
def get_index() -> SourceIndex:
    return SourceIndex(load_sections())
