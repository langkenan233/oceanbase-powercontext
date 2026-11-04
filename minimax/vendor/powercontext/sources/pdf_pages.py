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
from urllib.parse import quote, unquote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization, SourceRef
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
    from powercontext.builtin.runtime.models import MemoryEntriesPage

PDF_PAGE_SOURCE_NAME = "pdf-page"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SOURCE_NAME_PREFIX = "pdf"
_FALLBACK_IDENTITY_DIGEST_LENGTH = 32
_TOKEN = re.compile(r"[a-z]+|\d+")
_WORD = re.compile(r"[A-Za-z0-9]+")
_LETTER_WORD = re.compile(r"[A-Za-z]+")
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+")
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_WORD_CHARACTER = re.compile(r"[A-Za-z0-9]")
_COMPACT = re.compile(r"[^a-z0-9]+")
# A mark between two words. The platform counts a word with [a-z]+|\d+, so a mark never
# carries a word of its own, but it does keep two words apart.
_MARK = re.compile(r"[^\w\s]|_", re.UNICODE)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SHORT_YEAR = re.compile(r"(?i)(?:fy|fiscal\s+year|year)\s*'?(\d{2})(?!\d)")
_FORM_MARKERS = re.compile(r"10K|10Q|8K|EARNINGS|Q[1-4]", re.IGNORECASE)

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
_SECTION_HEAD_CHARS = 1200
_RISK_SECTION_WEIGHT = 8.0
_SEGMENT_SECTION_WEIGHT = 6.0
_OVERVIEW_SECTION_WEIGHT = 8.0
_IDENTITY_BONUS = 10.0
_PERIOD_BONUS = 10.0
_FORM_BONUS = 1.0
_INDEX_CACHE_SIZE = 1

# Filings keep a fixed table of contents, so a section heading identifies what a page is for even
# when the page never repeats the words of the question. Each cue family is only added when the
# question itself asks for that kind of content.
_RISK_SECTION_TITLES = ("item 1a", "risk factors", "risk factor")
_RISK_CUES = ("risk", "subject to", "cyclical", "cyclicality", "uncertain", "exposure", "depend")
_SEGMENT_SECTION_TITLES = (
    "business segment data",
    "business segment",
    "segment information",
    "segment reporting",
)
_SEGMENT_CUES = ("category", "categories", "segment", "segments", "product line", "service categor")
# A margin question about a bank or an insurer is answered by the business overview that explains
# why the metric does not apply, not by an income statement.
_OVERVIEW_SECTION_TITLES = ("item 1 business", "item 1 business overview", "item 1 general")
_OVERVIEW_CUES = (
    "margin",
    "gross margin",
    "operating margin",
    "primary customer",
    "customers",
    "customer concentration",
    "clients",
    "geograph",
    "region",
    "operates in",
    "products and services",
)

# A "how much did it change" question is answered by the summary table that lines two fiscal
# years up side by side.
_SUMMARY_SECTION_TITLES = (
    "summary of financial performance",
    "selected financial data",
    "summary of selected financial data",
)
_SUMMARY_CUES = ("how much", "changed", "change in", "compared", "versus", "increase", "decrease")

# A section title is laid out in a narrow column, so the extraction often breaks one word of it
# in two ("Business Segment Dat a"). The titles are therefore matched without the spaces.
_SEGMENT_SECTION_TITLES_COMPACT = tuple(title.replace(" ", "") for title in _SEGMENT_SECTION_TITLES)
_OVERVIEW_SECTION_TITLES_COMPACT = tuple(title.replace(" ", "") for title in _OVERVIEW_SECTION_TITLES)
_SUMMARY_SECTION_TITLES_COMPACT = tuple(title.replace(" ", "") for title in _SUMMARY_SECTION_TITLES)

_SECTION_WEIGHTS = {
    "risk": _RISK_SECTION_WEIGHT,
    "segment": _SEGMENT_SECTION_WEIGHT,
    "overview": _OVERVIEW_SECTION_WEIGHT,
    "summary": 6.0,
}

# The standard text projection prefixes the document id to the page text and becomes one Memory
# entry, which may not exceed 8192 UTF-8 bytes. A dense filing page can be larger than that, so
# the parsed text is bounded to the remaining budget instead of failing the whole document.
_MEMORY_TEXT_LIMIT = 8192

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
    "ppe": ("property", "plant", "equipment"),
    "ppne": ("property", "plant", "equipment"),
    "profit": ("income", "operating"),
    "restructuring": ("severance", "impairment", "productivity"),
    "revenue": ("net", "sales"),
}

# A page that states one of these headings is a primary financial statement. Numeric and
# metric questions are answered there even when the page never repeats the question's words.
_STATEMENT_TITLE_TERMS = 24
_TOC_LABELS = frozenset({"table of contents", "t able of contents", "contents"})
_STATEMENT_SIGNATURES = (
    "consolidated statements of income",
    "consolidated statement of income",
    "consolidated statements of operations",
    "consolidated statement of operations",
    "consolidated statements of earnings",
    "consolidated statement of earnings",
    "consolidated balance sheet",
    "consolidated balance sheets",
    "consolidated statements of cash flows",
    "consolidated statement of cash flows",
    "consolidated statements of comprehensive income",
    "consolidated statements of changes in equity",
    "consolidated statements of stockholders",
    "consolidated statements of shareholders",
    "consolidated statements of financial position",
)

# A filing title is laid out in a narrow column, so the extraction often breaks one word of it
# in two ("Balance Shee t"). The signature is therefore matched without the spaces.
_STATEMENT_SIGNATURES_COMPACT = tuple(signature.replace(" ", "") for signature in _STATEMENT_SIGNATURES)

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
    "capital intensive",
    "capital-intensive",
    "ppne",
    "ppe",
)

# A question may write a metric as one word ("cashflow") while the cue and the filing write it
# as two ("cash flow"). A cue made of several words is therefore also matched without the
# spaces and the punctuation of the question.
_METRIC_CUES_MULTI = tuple(cue for cue in _METRIC_CUES if len(cue.split()) > 1)
_METRIC_CUES_MULTI_COMPACT = tuple(_COMPACT.sub("", cue) for cue in _METRIC_CUES_MULTI)

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
    budget = _MEMORY_TEXT_LIMIT - len(document_id.encode("utf-8")) - 1
    pages: list[PDFPageInput] = []
    try:
        for index, page in enumerate(reader.pages):
            text = page.extract_text()
            normalized = text if isinstance(text, str) else ""
            pages.append(
                PDFPageInput(
                    document_id=document_id,
                    document_sha256=digest,
                    page_number=index + 1,
                    text=_bounded_page_text(_normalised_page_text(normalized), budget),
                )
            )
    except Exception as error:  # any page failure must fail the whole document
        raise ValueError("PDF document must be fully readable page by page") from error
    return tuple(pages)


def _normalised_page_text(text: str) -> str:
    """Collapse the layout whitespace of one page without touching its words or its punctuation.

    A filing page spends a large share of its bytes on column padding, indentation and blank
    lines. The quality gate counts words with ``[a-z]+|\\d+``, so neither the padding nor a
    blank line carries a word of its own: dropping them keeps every word of the page and
    frees the room a dense page needs inside the Memory entry budget. A page without any
    word stays empty.
    """

    lines = []
    for line in text.splitlines():
        trimmed = _INLINE_SPACE.sub(" ", line).strip()
        if trimmed:
            lines.append(trimmed)
    normalised = "\n".join(lines)
    return normalised if _WORD_CHARACTER.search(normalised) else ""


def _bounded_page_text(text: str, budget: int) -> str:
    """Keep one page inside the Memory text budget by dropping only trailing words.

    A page that fits is kept as it is. A page that does not fit has to lose its tail, and
    the tail is cut at a word boundary. Before the cut the marks of the page are turned
    into spaces: a mark never carries a word of its own, so the page keeps every word it
    had, but it spends fewer bytes on the marks and the cut lands on a boundary behind
    every word instead of behind a whole run of words.
    """

    if budget <= 0 or len(text.encode("utf-8")) <= budget:
        return text
    roomy = _INLINE_SPACE.sub(" ", _MARK.sub(" ", text).replace("\n", " "))
    raw = roomy.encode("utf-8")
    if len(raw) <= budget:
        return roomy
    trimmed = raw[:budget].decode("utf-8", "ignore")
    cut = trimmed.rfind(" ")
    return trimmed[:cut] if cut > budget // 2 else trimmed


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
    ranking = index.rank(
        query,
        _weighted_terms(_content_terms(query)),
        _metric_cue(query),
        _section_cues(query),
    )
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


class _PageModel:
    """Rank the stored pages of one document with a fixed, offline lexical model."""

    def __init__(self, bodies: Sequence[str]) -> None:
        self.counts = [_counts(_terms(text)) for text in bodies]
        self.headings = [_counts(_terms(_heading(text))) for text in bodies]
        sequences = [_terms(text) for text in bodies]
        self.windows = [_windows(sequence) for sequence in sequences]
        self.lengths = [max(sum(counter.values()), _LENGTH_FLOOR) for counter in self.counts]
        self.heading_lengths = [sum(counter.values()) for counter in self.headings]
        self.window_lengths = [[max(sum(block.values()), 20) for block in blocks] for blocks in self.windows]
        self.average = sum(self.lengths) / max(1, len(self.lengths))
        self.heading_average = sum(self.heading_lengths) / max(1, len(self.heading_lengths))
        self.window_average = sum(
            sum(lengths) for lengths in self.window_lengths
        ) / max(1, sum(len(lengths) for lengths in self.window_lengths))
        self.total = len(self.counts)
        self.statements = [1.0 if _statement_page(text) else 0.0 for text in bodies]
        self.sections = [_section_page(text) for text in bodies]
        self.document_frequency: dict[str, int] = {}
        for counter in self.counts:
            for term in counter:
                self.document_frequency[term] = self.document_frequency.get(term, 0) + 1

    def best(self, weights: dict[str, float]) -> float:
        """Strongest word match this document offers, with no filing-structure prior."""

        return max((self._page_score(index, weights) for index in range(self.total)), default=0.0)

    def rank(
        self,
        weights: dict[str, float],
        cue: float,
        sections: frozenset[str] = frozenset(),
    ) -> list[int]:
        scores = [
            (self._page_score(index, weights, cue, sections), index) for index in range(self.total)
        ]
        scores = [item for item in scores if item[0] > 0]
        scores.sort(key=lambda item: (-item[0], item[1]))
        return [index for _score, index in scores]

    def _page_score(
        self,
        index: int,
        weights: dict[str, float],
        cue: float = 0.0,
        sections: frozenset[str] = frozenset(),
    ) -> float:
        body = self._score(self.counts[index], self.lengths[index], self.average, weights)
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
        for tag in sections & self.sections[index]:
            score += _SECTION_WEIGHTS[tag]
        return score

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


class _PageIndex:
    """Rank stored pages after deciding which stored document the question is about.

    One Memory can hold several filings at once, and a question is answered inside exactly one
    of them. Ranking all pages as one pool lets a large filing crowd the answer pages of a
    smaller one out of the result, so the scope is narrowed to one document first.
    """

    def __init__(self, listing: MemoryEntriesPage) -> None:
        self.identifiers: list[str] = []
        self._citations: dict[str, MemoryCitation] = {}
        self._groups: dict[str, list[int]] = {}
        self._names: dict[str, str] = {}
        self._models: dict[str, _PageModel] = {}
        bodies: list[str] = []
        seen: set[tuple[str, int]] = set()
        for record in listing.entries:
            identifier = str(record.citation.entry_id)
            if identifier in self._citations:
                continue
            document, page = _source_identity(record.entry.sources)
            if not document:
                document = _document_of(record.entry.text)
            if page is not None:
                if (document, page) in seen:
                    continue
                seen.add((document, page))
            self.identifiers.append(identifier)
            self._citations[identifier] = record.citation
            self._groups.setdefault(document, []).append(len(self.identifiers) - 1)
            self._names.setdefault(document, document)
            bodies.append(_body_of(record.entry.text, document))
        self._build(bodies)

    @classmethod
    def from_hits(cls, hits: Sequence[MemoryHit]) -> _PageIndex:
        index = cls.__new__(cls)
        index.identifiers = []
        index._citations = {}
        index._groups = {}
        index._names = {}
        index._models = {}
        bodies: list[str] = []
        for hit in hits:
            identifier = str(hit.entry_id)
            if identifier in index._citations:
                continue
            name = _document_of(hit.text)
            index.identifiers.append(identifier)
            index._citations[identifier] = _citation_of(hit)
            index._groups.setdefault(name, []).append(len(index.identifiers) - 1)
            index._names.setdefault(name, name)
            bodies.append(_body_of(hit.text, name))
        index._build(bodies)
        return index

    def _build(self, bodies: Sequence[str]) -> None:
        self._models = {
            document: _PageModel([bodies[position] for position in positions])
            for document, positions in self._groups.items()
        }

    def citations(self, indices: Sequence[int]) -> tuple[MemoryCitation, ...]:
        return tuple(self._citations[self.identifiers[index]] for index in indices)

    def route(self, question: str, weights: dict[str, float]) -> str:
        """Pick the document the question belongs to.

        A question names the company it asks about, so the filings of that company are the
        only candidates. Within them the period and the kind of filing tell the filings
        apart, because one company files the same form again for every year.
        """

        scored: list[tuple[float, float, str]] = []
        for document, model in self._models.items():
            name = self._names.get(document, document)
            identity = _identity_match(question, name)
            period = _period_match(question, name)
            form = _form_match(question, name)
            score = model.best(weights) * (1.0 + _IDENTITY_BONUS * identity + _PERIOD_BONUS * period)
            score *= 1.0 + _FORM_BONUS * form
            scored.append((identity, score, document))
        named = [item for item in scored if item[0] >= 1.0]
        pool = named if named else scored
        return max(pool, key=lambda item: item[1])[2]

    def rank(
        self,
        question: str,
        weights: dict[str, float],
        cue: float,
        sections: frozenset[str] = frozenset(),
    ) -> list[int]:
        if not self._models:
            return []
        document = self.route(question, weights)
        positions = self._groups[document]
        return [positions[offset] for offset in self._models[document].rank(weights, cue, sections)]


def _citation_of(hit: MemoryHit) -> MemoryCitation:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    return MemoryCitation(
        memory_ref=hit.memory_ref,
        entry_id=hit.entry_id,
        entry_version_id=hit.entry_version_id,
    )


def _raw_terms(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def _stem(term: str) -> str:
    """Fold only the plural suffix so that a question matches the wording a filing uses.

    This stays deliberately shallow: it never rewrites a stem, so unrelated words keep
    their own weight and the statement headings stay recognisable.
    """

    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and term.endswith("es") and term.endswith(("ses", "xes", "zes", "ches", "shes")):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


def _terms(text: str) -> list[str]:
    return [_stem(term) for term in _TOKEN.findall(text.casefold())]


def _counts(terms: Sequence[str]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for term in terms:
        counter[term] = counter.get(term, 0) + 1
    return counter


def _document_of(text: str) -> str:
    """The document id the standard text projection prints above every page."""

    head, _separator, _rest = text.partition("\n")
    return head


def _source_identity(sources: Sequence[SourceRef]) -> tuple[str, int | None]:
    """Read the document id and the physical page number out of a page source identity.

    The source identity is built from the document id, the digest of the file and the page
    number, so it states which filing a stored page belongs to without reading the entry text.
    """

    for reference in sources:
        if reference.source_type != PDF_PAGE_SOURCE_NAME:
            continue
        parts = reference.source_id.split(":")
        if len(parts) != 4:
            continue
        document = unquote(parts[1])
        try:
            page = int(parts[3])
        except ValueError:
            continue
        return document, page
    return "", None


def _body_of(text: str, document: str) -> str:
    """The page text without the document identity line the text projection prints on top.

    The identity line is only removed when the text really starts with the document id, so a
    page whose first line is part of its own wording keeps that line.
    """

    if document and text.startswith(f"{document}\n"):
        return text[len(document) + 1 :]
    return text


def _compact(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for letter in needle:
        position = haystack.find(letter, position)
        if position < 0:
            return False
        position += 1
    return True


def _identity_match(question: str, document_id: str) -> float:
    """How strongly the question names the document it is about.

    A filing is named in full ("American Express"), by its ticker ("AMEX") or by a short brand
    ("3M"). Each form is recognised from the document id itself, so nothing is listed by hand.
    """

    lead = _WORD.match(document_id)
    stem = _compact(lead.group()) if lead is not None else _compact(document_id.split("_", 1)[0])
    if len(stem) < 2:
        return 0.0
    text = _compact(question)
    if any(word.casefold() == stem for word in _WORD.findall(question)):
        return 1.0
    if len(stem) >= 4 and stem in text:
        return 1.0
    for word in _LETTER_WORD.findall(question):
        if word.isupper() and 2 <= len(word) <= 6 and _is_subsequence(word.casefold(), stem):
            return 0.8
    if len(stem) >= 6 and stem[:5] in text:
        return 0.5
    return 0.0


def _period_match(question: str, document_id: str) -> float:
    """Tell whether the question names the period the filing covers.

    One company files the same form again for every year, so the name alone cannot tell two
    filings of that company apart. The year a filing belongs to is part of its identity, and
    a question that names a year ("as of FY2022", "in FY22") says which filing it is about.
    """

    period = _YEAR.search(document_id)
    if period is None:
        return 0.0
    return 1.0 if period.group(1) in _question_years(question) else 0.0


def _question_years(question: str) -> set[str]:
    """The years a question names, written in full or in the short form of a fiscal year."""

    years = {found.group(1) for found in _YEAR.finditer(question)}
    for found in _SHORT_YEAR.finditer(question):
        digits = found.group(1)
        years.add("20" + digits if int(digits) < 50 else "19" + digits)
    return years


def _form_match(question: str, document_id: str) -> float:
    """Tell whether the question names the kind of document the filing is.

    A company files an annual report, a quarterly report and an earnings release, and the
    three answer different questions. The kind of filing is part of its identity, so a
    question that names the form ("the 10-K", "the earnings release") says which one it is
    about.
    """

    text = _compact(question)
    for marker in _FORM_MARKERS.findall(document_id):
        key = _compact(marker)
        if len(key) >= 2 and key in text:
            return 1.0
    return 0.0


def _windows(terms: Sequence[str]) -> list[dict[str, int]]:
    blocks: list[dict[str, int]] = []
    for start in range(0, len(terms), _WINDOW_TOKENS):
        counter = _counts(terms[start : start + _WINDOW_TOKENS])
        if counter:
            blocks.append(counter)
    return blocks or [{}]


def _heading(text: str) -> str:
    """The title of one page, taken from the words it prints first."""

    lines: list[str] = []
    collected = 0
    for line in text.splitlines():
        words = _terms(line)
        if not words or " ".join(_raw_terms(line)) in _TOC_LABELS:
            continue
        lines.append(line)
        collected += len(words)
        if collected >= 12:
            break
    return " ".join(_terms("\n".join(lines) if lines else text)[:_HEADING_TERMS])


def _statement_page(text: str) -> float:
    # Only the page title decides this: a note or an auditor report that merely mentions the
    # statements further down must not be promoted over the statement pages themselves.
    head: list[str] = []
    for line in text.splitlines():
        words = _raw_terms(line)
        if not words or " ".join(words) in _TOC_LABELS:
            continue
        head.extend(words)
        if len(head) >= _STATEMENT_TITLE_TERMS:
            break
    joined = "".join(head)
    return 1.0 if any(signature in joined for signature in _STATEMENT_SIGNATURES_COMPACT) else 0.0


def _metric_cue(question: str) -> float:
    """Tell whether the question asks for a figure, so that a statement page can be preferred.

    A metric that is written as several words is also matched without the spaces, because a
    question may join it into one word ("cashflow") while the filing keeps the two words
    ("cash flows").
    """

    lowered = question.casefold()
    if any(cue in lowered for cue in _METRIC_CUES):
        return 1.0
    compact = _COMPACT.sub("", lowered)
    return 1.0 if any(cue in compact for cue in _METRIC_CUES_MULTI_COMPACT) else 0.0


def _section_page(text: str) -> frozenset[str]:
    """Flag the sections a page opens, judged from the headings the page itself prints."""

    lines = [" ".join(_raw_terms(line)) for line in text.splitlines()[:60]]
    compact = [line.replace(" ", "") for line in lines]
    tags: set[str] = set()
    if any("item1a" in line for line in compact):
        tags.add("risk")
    if any(title in line for line in compact for title in _SEGMENT_SECTION_TITLES_COMPACT):
        tags.add("segment")
    if any(title in line for line in compact for title in _OVERVIEW_SECTION_TITLES_COMPACT):
        tags.add("overview")
    if any(title in line for line in compact for title in _SUMMARY_SECTION_TITLES_COMPACT):
        tags.add("summary")
    return frozenset(tags)


def _section_cues(question: str) -> frozenset[str]:
    lowered = question.casefold()
    tags: set[str] = set()
    if any(cue in lowered for cue in _RISK_CUES):
        tags.add("risk")
    if any(cue in lowered for cue in _SEGMENT_CUES):
        tags.add("segment")
    if any(cue in lowered for cue in _OVERVIEW_CUES):
        tags.add("overview")
    if any(cue in lowered for cue in _SUMMARY_CUES):
        tags.add("summary")
    return frozenset(tags)


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
    weights: dict[str, float] = {}
    for term in terms:
        weights[_stem(term)] = 1.0
        for synonym in _FINANCE_SYNONYMS.get(term, ()):
            key = _stem(synonym)
            if weights.get(key, 0.0) < 0.35:
                weights[key] = 0.35
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
