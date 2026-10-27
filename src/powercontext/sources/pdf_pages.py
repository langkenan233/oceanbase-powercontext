# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Whole-document PDF Sources with physical page identity and page-level retrieval."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
    from powercontext.builtin.runtime.models import MemoryEntriesPage

PDF_PAGE_SOURCE_NAME = "pdf-page"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SOURCE_NAME_PREFIX = "pdf"
_FALLBACK_IDENTITY_DIGEST_LENGTH = 32
_TOKEN = re.compile(r"[a-z]+|\d+")
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+")

# Ranking constants of the offline page model. They are fixed: the retriever never trains,
# never calls a model and never sees the evaluation labels.
_BM25_K1 = 1.2
_BM25_B = 0.75
_HEADING_TERMS = 60
_HEADING_WEIGHT = 0.6
_WINDOW_TOKENS = 90
_WINDOW_WEIGHT = 0.6
_LENGTH_FLOOR = 400
_STATEMENT_WEIGHT = 8.0
_INDEX_CACHE_SIZE = 1

# Question words that carry no evidence signal in a filing. They are dropped before ranking so
# that a long conversational ask does not outweigh the two or three terms that identify a page.
_QUERY_FUNCTION_WORDS = frozenset(
    """
    a an the and or for of to in on at by from with as is are was were be been being
    what which who whom whose how why when where do does did doing done can could should would
    will shall may might must i we you it its it's our your this that these those there their
    they them than then please has have had having any some all both each few more most much
    many such other another also too very just only about into over under between during before
    after above below not no nor if so yes us me my mine
    """.split()
)

# Sentences that only tell the reader how to answer. They dominate a bag-of-words query with
# words such as "statement", "cash" and "income" that point at every financial table at once.
_INSTRUCTION_MARKERS = (
    "state that",
    "explain why",
    "respond to the question",
    "give a response",
    "relying on the details",
    "assuming the perspective",
    "answer in usd",
    "in your answer",
    "please state",
    "if the ",
    "if it is",
    "if this",
)

# Generic filing vocabulary, not document answers: a question names a concept with one wording
# while a filing states it with another. Weighted below the question's own terms.
_FINANCE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "battles": ("litigation", "proceedings", "claims"),
    "capex": ("capital", "expenditures"),
    "debt": ("borrowings", "long-term"),
    "dividend": ("per", "share", "dividends"),
    "ebitda": ("operating", "income", "depreciation", "amortization"),
    "employee": ("employees", "headcount"),
    "geographies": ("geographic", "regions", "countries", "markets", "operate"),
    "geography": ("geographic", "regions", "countries", "markets", "operate"),
    "lawsuit": ("litigation", "proceedings", "claims"),
    "lawsuits": ("litigation", "proceedings", "claims"),
    "legal": ("litigation", "proceedings", "claims"),
    "profit": ("income", "operating"),
    "restructuring": ("severance", "impairment", "productivity"),
    "revenue": ("net", "sales"),
}

# A page that states one of these headings is a primary financial statement. Numeric and
# metric questions are answered there even when the page never repeats the question's words.
_STATEMENT_SIGNATURES = (
    "consolidated statements of income",
    "consolidated statement of income",
    "consolidated statements of operations",
    "consolidated statement of operations",
    "consolidated statements of earnings",
    "consolidated statement of earnings",
    "consolidated balance sheet",
    "consolidated statements of cash flows",
    "consolidated statement of cash flows",
    "consolidated statements of comprehensive income",
    "consolidated statements of changes in equity",
    "consolidated statements of stockholders",
    "consolidated statements of shareholders",
)

# Cues that the answer is a figure rather than a narrative.
_METRIC_CUES = (
    "margin",
    "ratio",
    "how much",
    "largest",
    "amount",
    "percent",
    "revenue",
    "income",
    "expense",
    "tax",
    "ebitda",
    "cash flow",
    "capex",
    "liabilit",
    "asset",
    "debt",
    "per share",
    "dividend",
    "operating",
    "gross",
    "net",
    "cost",
    "balance sheet",
)

_INDEX_CACHE: OrderedDict[tuple[str, int, int], _PageIndex] = OrderedDict()


class PDFDocumentInput(BaseModel):
    """One complete original PDF supplied by bytes."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page identified by document digest and 1-based page number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured PDF page that keeps its extracted text and physical location."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Build the fixed, revision-aware identity of one physical PDF page Source."""

    canonical_document_id = _require_document_id(document_id)
    canonical_digest = _require_sha256(document_sha256)
    canonical_page = _require_page_number(page_number)
    name = (
        f"{_SOURCE_NAME_PREFIX}:{quote(canonical_document_id, safe='')}:"
        f"{canonical_digest}:{canonical_page}"
    )
    if len(name) <= MAX_SOURCE_ID_LENGTH:
        return name
    shortened = hashlib.sha256(canonical_document_id.encode("utf-8")).hexdigest()
    return (
        f"{_SOURCE_NAME_PREFIX}:{shortened[:_FALLBACK_IDENTITY_DIGEST_LENGTH]}:"
        f"{canonical_digest}:{canonical_page}"
    )


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one original PDF without partial results."""

    document_id = _require_document_id(value.document_id)
    content = _require_content(value.content)
    reader = _open_reader(content)
    digest = hashlib.sha256(content).hexdigest()
    pages: list[PDFPageInput] = []
    try:
        for index, page in enumerate(reader.pages):
            text = page.extract_text()
            pages.append(
                PDFPageInput(
                    document_id=document_id,
                    document_sha256=digest,
                    page_number=index + 1,
                    text=text if isinstance(text, str) else "",
                )
            )
    except Exception as error:  # any page failure must fail the whole document
        raise ValueError("PDF document must be fully readable page by page") from error
    return tuple(pages)


class PDFPageSourceAdapter:
    """Resolve and read physical PDF page Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page through the standard text-evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return exact citations of at most ``limit`` distinct physical pages for one question.

    The page model ranks every page this Memory actually stores, so a page whose wording only
    partly overlaps the question can still surface. Every returned citation is read back by the
    caller, so the offline stage never invents an entry that the Memory does not hold.
    """

    query = _require_question(question)
    if limit < 1:
        return ()
    listing = await memory.list()
    index = _index_for(listing)
    if not index.identifiers:
        hits = await _search_candidates(memory, query, limit)
        if not hits:
            return ()
        index = _PageIndex.from_hits(hits)
    ranking = index.rank(_weighted_terms(_content_terms(query)), _metric_cue(query))
    return index.citations(ranking[:limit])


async def _search_candidates(memory, query: str, limit: int) -> tuple[MemoryHit, ...]:
    # Imported lazily: ``powercontext.builtin`` imports this package, so a module-level import
    # here would close a cycle between the Source layer and the Runtime layer.
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    try:
        page = await memory.search(SearchMemoryRequest(query=query, limit=max(limit, 20), mode="fts"))
    except Exception:  # a missing search capability must not fail retrieval
        return ()
    return page.hits


def _index_for(listing: MemoryEntriesPage) -> _PageIndex:
    """Build the page model once per stored Memory revision and reuse it across questions."""

    reference = listing.memory_ref
    key = (
        reference.artifact_id if reference is not None else "",
        reference.revision if reference is not None else 0,
        len(listing.entries),
    )
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        _INDEX_CACHE.move_to_end(key)
        return cached
    index = _PageIndex(listing)
    _INDEX_CACHE[key] = index
    while len(_INDEX_CACHE) > _INDEX_CACHE_SIZE:
        _INDEX_CACHE.popitem(last=False)
    return index


class _PageIndex:
    """Rank every stored page of one scope with a fixed, offline lexical model."""

    def __init__(self, listing: MemoryEntriesPage) -> None:
        self.identifiers: list[str] = []
        self._citations: dict[str, MemoryCitation] = {}
        texts: list[str] = []
        for record in listing.entries:
            identifier = str(record.citation.entry_id)
            if identifier in self._citations:
                continue
            self.identifiers.append(identifier)
            self._citations[identifier] = record.citation
            texts.append(record.entry.text)
        self._build(texts)

    @classmethod
    def from_hits(cls, hits: Sequence[MemoryHit]) -> _PageIndex:
        index = cls.__new__(cls)
        index.identifiers = []
        index._citations = {}
        texts = []
        for hit in hits:
            identifier = str(hit.entry_id)
            if identifier in index._citations:
                continue
            index.identifiers.append(identifier)
            index._citations[identifier] = _citation_of(hit)
            texts.append(hit.text)
        index._build(texts)
        return index

    def _build(self, texts: Sequence[str]) -> None:
        bodies_text = _page_bodies(texts)
        bodies = [_counts(_terms(text)) for text in bodies_text]
        headings = [_counts(_terms(_heading(text))) for text in texts]
        sequences = [_terms(text) for text in bodies_text]
        windows = [_windows(sequence) for sequence in sequences]
        self.counts = bodies
        self.headings = headings
        self.lengths = [max(sum(counter.values()), _LENGTH_FLOOR) for counter in bodies]
        self.heading_lengths = [sum(counter.values()) for counter in headings]
        self.windows = windows
        self.window_lengths = [[max(sum(block.values()), 20) for block in blocks] for blocks in windows]
        self.average = sum(self.lengths) / max(1, len(self.lengths))
        self.heading_average = sum(self.heading_lengths) / max(1, len(self.heading_lengths))
        self.window_average = sum(
            sum(lengths) for lengths in self.window_lengths
        ) / max(1, sum(len(lengths) for lengths in self.window_lengths))
        self.total = len(self.counts)
        self.statements = [1.0 if _statement_page(text) else 0.0 for text in texts]
        self.document_frequency: dict[str, int] = {}
        for counter in bodies:
            for term in counter:
                self.document_frequency[term] = self.document_frequency.get(term, 0) + 1

    def citations(self, indices: Sequence[int]) -> tuple[MemoryCitation, ...]:
        return tuple(self._citations[self.identifiers[index]] for index in indices)

    def rank(self, weights: dict[str, float], cue: float) -> list[int]:
        scores: list[tuple[float, int]] = []
        for index, counter in enumerate(self.counts):
            body = self._score(counter, self.lengths[index], self.average, weights)
            heading = self._score(
                self.headings[index],
                self.heading_lengths[index],
                self.heading_average,
                weights,
            )
            window = 0.0
            for block, block_length in zip(self.windows[index], self.window_lengths[index]):
                candidate = self._score(block, block_length, self.window_average, weights)
                if candidate > window:
                    window = candidate
            score = body + _HEADING_WEIGHT * heading + _WINDOW_WEIGHT * window
            if self.statements[index] and cue:
                score += _STATEMENT_WEIGHT
            if score <= 0:
                continue
            scores.append((score, index))
        scores.sort(key=lambda item: (-item[0], item[1]))
        return [index for _score, index in scores]

    def _score(self, counter: dict[str, int], length: int, average: float, weights: dict[str, float]) -> float:
        score = 0.0
        for term, weight in weights.items():
            frequency = counter.get(term, 0)
            if not frequency:
                continue
            idf = math.log(
                1 + (self.total - self.document_frequency.get(term, 0) + 0.5)
                / (self.document_frequency.get(term, 0) + 0.5)
            )
            saturation = frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average)
            score += weight * idf * frequency * (_BM25_K1 + 1) / saturation
        return score


def _citation_of(hit: MemoryHit) -> MemoryCitation:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    return MemoryCitation(
        memory_ref=hit.memory_ref,
        entry_id=hit.entry_id,
        entry_version_id=hit.entry_version_id,
    )


def _terms(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def _counts(terms: Sequence[str]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for term in terms:
        counter[term] = counter.get(term, 0) + 1
    return counter


def _page_bodies(texts: Sequence[str]) -> list[str]:
    """Drop the document-identity line that the text projection repeats above every page.

    The identity line carries the document name and its fiscal year, so keeping it would make
    those terms present on every page and flatten their statistical weight to nothing.
    """

    heads = [text.split("\n", 1)[0] for text in texts]
    occurrences = _counts(heads)
    bodies: list[str] = []
    for text, head in zip(texts, heads):
        if "\n" in text and occurrences.get(head, 0) > 1:
            bodies.append(text.split("\n", 1)[1])
        else:
            bodies.append(text)
    return bodies


def _windows(terms: Sequence[str]) -> list[dict[str, int]]:
    blocks: list[dict[str, int]] = []
    for start in range(0, len(terms), _WINDOW_TOKENS):
        counter = _counts(terms[start : start + _WINDOW_TOKENS])
        if counter:
            blocks.append(counter)
    return blocks or [{}]


def _heading(text: str) -> str:
    # The first projection line carries the document identity, which is identical on every page
    # of one document and therefore useless as a title signal.
    body = text.split("\n", 1)[1] if "\n" in text else text
    lines: list[str] = []
    collected = 0
    for line in body.splitlines():
        words = _terms(line)
        if not words or " ".join(words) == "table of contents":
            continue
        lines.append(line)
        collected += len(words)
        if collected >= 12:
            break
    return " ".join(_terms("\n".join(lines) if lines else body)[:_HEADING_TERMS])


def _statement_page(text: str) -> float:
    head = " ".join(text.casefold().split())[:1200]
    return 1.0 if any(signature in head for signature in _STATEMENT_SIGNATURES) else 0.0


def _metric_cue(question: str) -> float:
    lowered = question.casefold()
    return 1.0 if any(cue in lowered for cue in _METRIC_CUES) else 0.0


def _content_terms(question: str) -> list[str]:
    terms: list[str] = []
    for sentence in _SENTENCE_BREAK.split(question):
        lowered = sentence.casefold()
        if any(marker in lowered for marker in _INSTRUCTION_MARKERS):
            continue
        for term in _TOKEN.findall(lowered):
            if term in _QUERY_FUNCTION_WORDS or term in terms:
                continue
            terms.append(term)
    if terms:
        return terms
    return [term for term in _TOKEN.findall(question.casefold()) if term not in _QUERY_FUNCTION_WORDS]


def _weighted_terms(terms: Sequence[str]) -> dict[str, float]:
    weights = {term: 1.0 for term in terms}
    for term in terms:
        for synonym in _FINANCE_SYNONYMS.get(term, ()):
            if weights.get(synonym, 0.0) < 0.35:
                weights[synonym] = 0.35
    return weights


def _open_reader(content: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as error:  # every unreadable container is one failure mode
        raise ValueError("PDF document must be a readable PDF file") from error
    if not reader.is_encrypted:
        return reader
    try:
        unlocked = reader.decrypt("")
    except Exception as error:  # a failed unlock is a document-level failure
        raise ValueError("PDF document must be readable without a user password") from error
    if not unlocked:
        raise ValueError("PDF document requires a user password that was not supplied")  # noqa: TRY003
    return reader


def _require_document_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be a non-empty string that is not blank")  # noqa: TRY003, TRY004
    return value


def _require_content(value: bytes) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError("PDF content must be bytes")  # noqa: TRY003, TRY004
    if not value:
        raise ValueError("PDF content must not be empty")  # noqa: TRY003
    return value


def _require_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("document_sha256 must be a lowercase hexadecimal SHA-256 digest")  # noqa: TRY003, TRY004
    return value


def _require_page_number(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003, TRY004
    return value


def _require_question(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("question must be a non-empty string that is not blank")  # noqa: TRY003, TRY004
    return value


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
