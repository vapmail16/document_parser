"""Stage 5 — defined-term resolution (spec §6). No model involved.

Credit agreements define a term once and use it everywhere. This builds the map
    term -> definition, where it is defined, which other terms it references, where it is used
from the assembled clauses (definitions) and the OCR of every page (usages — so terms are
linked even on pages the model has not extracted), and derives two things reconciliation uses:
  * roles a term carries: "the Company" is one of the "Borrowers", so a party introduced as the
    Company is a borrower;
  * the article a term is used in: "Threshold Amount" is used by the Cross-Default event of default.
"""

import re

from .contracts import DefinedTerm, DocumentExtraction, SourceRef
from .normalise import text_key
from .ocr import OcrPage

_Q = "[\"“”]"
_DEFINITION = re.compile(
    rf"{_Q}(?P<term>[A-Z][^\"“”]{{1,80}}){_Q}\s+(?:means|shall mean|has the meaning|shall have the meaning|refers to)\b")
_INLINE = re.compile(rf"\((?:[^()]*?\s)?(?:the\s+)?{_Q}(?P<term>[A-Z][^\"“”]{{1,60}}){_Q}")
_COLLECTIVE = re.compile(rf"collectively,?\s+(?:the\s+)?{_Q}(?P<term>[A-Z][^\"“”]{{1,60}}){_Q}")
_CAPS_ENTITY = re.compile(r"([A-Z][A-Z0-9&.,'\- ]{2,}[A-Z.])\s*,[^()]{0,160}$")

USAGE_CAP = 40  # usage locations kept per term (the count is what matters for linking)


def _definitions(doc: DocumentExtraction) -> dict[str, DefinedTerm]:
    terms: dict[str, DefinedTerm] = {}
    for s in doc.sections:
        for c in s.clauses:
            text = c.text or ""
            page = c.page_range[0]
            matches = list(_DEFINITION.finditer(text))
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                body = text[m.start():end].strip()
                terms.setdefault(m["term"].strip(), DefinedTerm(
                    term=m["term"].strip(), definition=body,
                    definition_location=SourceRef(page=page, source_text=body[:200])))
            for m in _INLINE.finditer(text):
                term = m["term"].strip()
                if term in terms:
                    continue
                before = _CAPS_ENTITY.search(text[max(0, m.start() - 200):m.start()])
                terms[term] = DefinedTerm(
                    term=term, definition=(before.group(1).strip() if before else None),
                    definition_location=SourceRef(page=page, source_text=text[max(0, m.start() - 120):m.end()][-200:]))
    return terms


def _collectives(doc: DocumentExtraction, known: set[str]) -> dict[str, set[str]]:
    """'(the "Belgian Borrower" and together with the Company ..., collectively, the "Borrowers")'
    -> {"Borrowers": {"Belgian Borrower", "Company"}}"""
    out: dict[str, set[str]] = {}
    for s in doc.sections:
        for c in s.clauses:
            text = c.text or ""
            for m in _COLLECTIVE.finditer(text):
                start = text.rfind("(", 0, m.start())
                paren = text[start if start >= 0 else max(0, m.start() - 300):m.start()]
                members = {t for t in known if t != m["term"] and re.search(rf"\b{re.escape(t)}\b", paren)}
                out.setdefault(m["term"].strip(), set()).update(members)
    return out


_SUBHEADING = re.compile(r"\(([a-z]{1,3}|[ivx]{1,4})\)\s+([A-Z][A-Za-z\-’' ]{2,40})\.\s")


def _usages(term: str, ocr_pages: list[OcrPage], skip_page: int | None) -> tuple[list[SourceRef], list[str]]:
    """Where the term is used, and the nearest preceding sub-clause heading of each use."""
    pattern = re.compile(rf"\b{re.escape(term)}\b")
    out, headings = [], []
    for p in ocr_pages:
        if p.page == skip_page:
            continue
        text = " ".join(t.text for t in p.tokens)
        for m in pattern.finditer(text):
            if len(out) < USAGE_CAP:
                out.append(SourceRef(page=p.page, source_text=text[max(0, m.start() - 60):m.end() + 60]))
            prior = list(_SUBHEADING.finditer(text[max(0, m.start() - 1500):m.start()]))
            if prior and prior[-1].group(2).strip() not in headings:
                headings.append(prior[-1].group(2).strip())
    return out, headings


def _ocr_definitions(ocr_pages: list[OcrPage], known: set[str]) -> dict[str, DefinedTerm]:
    """'"Term" means ...' definitions read from the OCR of every page — covers pages the model has not
    extracted. OCR-quality text: used to link and resolve names, never emitted as an extracted value."""
    out: dict[str, DefinedTerm] = {}
    for p in ocr_pages:
        text = " ".join(t.text for t in p.tokens)
        matches = list(_DEFINITION.finditer(text))
        for i, m in enumerate(matches):
            term = m["term"].strip()
            if term in known or term in out:
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 600)
            body = text[m.start():end].strip()
            out[term] = DefinedTerm(term=term, definition=body, from_ocr=True,
                                    definition_location=SourceRef(page=p.page, source_text=body[:200]))
    return out


_WORD = r"[A-Z][\w&'\-.]*"
_CONNECTOR = r"(?:of|the|de|du|des|la|le|y|&)"  # "Bank of America", "Banco de la Nación"
_NAME_DEF = re.compile(
    rf"\b(?:means|shall mean)\s+(?P<name>{_WORD}(?:\s+(?:{_CONNECTOR}\s+)*{_WORD})*"
    r"(?:,?\s+(?:N\.A\.|Inc\.|Ltd\.|LLC|L\.P\.|plc|S\.A\.|SRL|AG|GmbH|B\.V\.))?)")


def entity_name(term: DefinedTerm) -> str | None:
    """The legal name a defined term stands for: '"Bank of America" means Bank of America, N.A. and its
    successors' -> 'Bank of America, N.A.'; inline '(the "Company")' definitions already hold the name."""
    if not term.definition:
        return None
    if not _Q_START.match(term.definition):
        return term.definition  # inline definition: the entity itself
    m = _NAME_DEF.search(term.definition)
    return m["name"].strip().rstrip(",") if m else None


_Q_START = re.compile(r"^\s*[\"“”]")


def resolve(doc: DocumentExtraction, ocr_pages: list[OcrPage]) -> DocumentExtraction:
    terms = _definitions(doc)
    terms |= _ocr_definitions(ocr_pages, set(terms))
    names = set(terms)
    for t in terms.values():
        refs = sorted(n for n in names if n != t.term and t.definition and re.search(rf"\b{re.escape(n)}\b", t.definition))
        t.references = refs
        t.usage_locations, t.usage_headings = _usages(
            t.term, ocr_pages, t.definition_location.page if t.definition_location else None)
    for collective, members in _collectives(doc, names).items():
        entry = terms.setdefault(collective, DefinedTerm(term=collective, definition=None, definition_location=None))
        entry.members = sorted(set(entry.members) | members)
    return doc.model_copy(update={"defined_terms": sorted(terms.values(), key=lambda t: t.term)})


def term_by_name(doc: DocumentExtraction, name: str) -> DefinedTerm | None:
    k = text_key(name).strip("\"“” ")
    return next((t for t in doc.defined_terms if text_key(t.term) == k), None)
