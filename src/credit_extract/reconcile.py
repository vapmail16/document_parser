"""Stage 6 — reconcile extracted field names against the field dictionary (spec §6, §9).

Tiers, first hit wins:
  exact    normalised name == an entry's display name
  alias    normalised name == an APPROVED alias
  fuzzy    the name's base (qualifier stripped) contains / closely matches a surface form,
           or the field sits in a section titled like a repeatable entry and one of that
           entry's instance rules matches the name ("context")
  none     no candidate — proposed as a new pending dictionary entry
Gates on every candidate:
  type     the value's normalised type must fit the entry's data_type (a duration is never a date)
  confusable  a name carrying the distinguishing words of a confusable partner is never
           mapped to the other side ("Consolidated Adjusted EBITDA" never -> Consolidated EBITDA)
Fuzzy mappings are always routed to review and only PROPOSE aliases (learned_via=fuzzy,
status=pending_review); nothing here writes to the dictionary. Embedding tier: deferred.
"""

import re
from collections import Counter

from pydantic import BaseModel
from rapidfuzz import fuzz

from .contracts import (DictionarySnapshot, DocumentExtraction, ExtractedField, FieldDictionaryEntry,
                        GroundingStatus, InstanceRule, NormalisedValue, SourceRef, ValueType)
from .grounding import AUTO_ACCEPT_CONFIDENCE
from .normalise import normalise, same_value, text_key

FUZZY_MIN = 80  # blended score (0-100) for a fuzzy mapping
CONTAINED_MIN = 95  # token-set score meaning "all of the surface form's words are in the name"

# Which normalised value types each dictionary data_type accepts.
_TYPE_FITS: dict[ValueType, set[ValueType]] = {
    ValueType.money: {ValueType.money},
    ValueType.percentage: {ValueType.percentage, ValueType.bps},
    ValueType.bps: {ValueType.bps, ValueType.percentage},
    ValueType.ratio: {ValueType.ratio},
    ValueType.date: {ValueType.date},
    ValueType.integer: {ValueType.integer},
    ValueType.duration: {ValueType.duration, ValueType.text},
    ValueType.table_ref: {ValueType.percentage, ValueType.bps, ValueType.table_ref, ValueType.text},
    ValueType.text: set(ValueType),
}

_QUALIFIER = re.compile(r"\(([^)]*)\)")
_LEVEL = re.compile(r"\blevel\s*([0-9]+|[ivx]+)\b", re.IGNORECASE)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def key_form(s: str) -> str:
    """Matching form: casefolded, punctuation to spaces, plurals folded."""
    words = re.sub(r"[^a-z0-9]+", " ", text_key(s)).split()
    return " ".join(_singular(w) for w in words)


def base_name(name: str) -> str:
    """Name without parenthesised qualifiers or level markers: the part that names the concept."""
    return key_form(_LEVEL.sub(" ", _QUALIFIER.sub(" ", name)))


def slug(s: str) -> str:
    return re.sub(r"_+", "_", "".join(ch if ch.isalnum() else "_" for ch in text_key(s))).strip("_")


def instance_key(entry: FieldDictionaryEntry, name: str, raw_value: str | None) -> str | None:
    if not entry.instance_rules:
        return None
    parts: list[str] = []
    lowered = text_key(name)
    for rule in entry.instance_rules:
        part = _instance_part(rule, name, lowered, raw_value)
        if part is None:
            return None
        parts.append(part)
    return "|".join(parts)


def _instance_part(rule: InstanceRule, name: str, lowered: str, raw_value: str | None) -> str | None:
    if rule.kind == "level":
        m = _LEVEL.search(name)
        if not m:
            return rule.default
        n = m.group(1).casefold()
        return f"level{int(n) if n.isdigit() else _ROMAN.get(n, n)}"
    if rule.kind == "choice":
        for key, terms in rule.choices.items():
            if any(t in lowered for t in terms):
                return key
        return rule.default
    source = (_QUALIFIER.search(name).group(1) if _QUALIFIER.search(name) else None) \
        if rule.slug_from == "qualifier" else raw_value
    return slug(source) if source else rule.default


_CROSS_REF = re.compile(r"^\W*(sections?|articles?|exhibits?|schedules?|clauses?)\s+[\w.()]+", re.IGNORECASE)


def _fits(entry: FieldDictionaryEntry, field: ExtractedField) -> bool:
    # A party field's value is a name — never a cross-reference ("Section 11.02"), a date or an amount.
    if entry.canonical_name in PARTY_ENTRIES:
        v = field.value or normalise(field.raw_value)
        if _CROSS_REF.match(field.raw_value or "") or (v is not None and v.type is not ValueType.text):
            return False
    if entry.data_type is None:
        return True
    value = field.value or normalise(field.raw_value)
    return value is None or value.type in _TYPE_FITS.get(entry.data_type, set(ValueType))


def _confusable_block(name: str, form: str, pairs: list[tuple[str, str]]) -> bool:
    """True if `name` carries a word that distinguishes form's confusable partner from form."""
    words = set(key_form(name).split())
    f = key_form(form)
    for a, b in pairs:
        ka, kb = key_form(a), key_form(b)
        for mine, other in ((ka, kb), (kb, ka)):
            if mine == f or mine in f:
                distinguishing = set(other.split()) - set(mine.split())
                if distinguishing & words:
                    return True
    return False


def _via_defined_term(field, doc, snap, ok, done):
    from .defined_terms import term_by_name

    term = term_by_name(doc, field.name) or term_by_name(doc, base_name(field.name))
    if term is None:
        return None
    # 1. a term that is a party role (the "Company" is one of the "Borrowers")
    for role in term_roles(doc, snap).get(term.term, []):
        entry = snap.entry(role)
        if ok(entry, entry.display_name):
            return done(entry, "defined_term", f"defined term “{term.term}”", 1.0)
    # 2. a term used under a sub-heading that names an instance of a repeatable entry
    #    ("Threshold Amount" is used in "(e) Cross-Default") — a heuristic, so reviewed like fuzzy
    for entry in snap.entries:
        for rule in entry.instance_rules:
            if rule.kind != "choice" or rule.default is not None:
                continue
            for heading in term.usage_headings:
                for key, words in rule.choices.items():
                    if any(w in text_key(heading) for w in words) and ok(entry, entry.display_name):
                        m = done(entry, "fuzzy", f"defined term “{term.term}” used in “{heading}”", 0.8)
                        return m.model_copy(update={"instance_key": key})
    return None


class Mapping(BaseModel):
    field_id: str
    name: str
    canonical_name: str | None
    method: str  # exact | alias | fuzzy | none
    via: str | None  # the surface form or section title that produced the match
    score: float | None
    instance_key: str | None
    type_mismatch: str | None = None  # exact/alias name match whose value type does not fit
    needs_instance: bool = False  # entry has instance rules, so an unresolved instance needs review
    value_is_name: bool = False
    rejected: list[str] = []  # candidates blocked by the type or confusable gate


def map_field(field: ExtractedField, section_title: str | None, snap: DictionarySnapshot,
              doc: DocumentExtraction | None = None) -> Mapping:
    name_k = key_form(field.name)
    base = base_name(field.name) or name_k
    rejected: list[str] = []

    def ok(entry: FieldDictionaryEntry, form: str, check_type: bool = True) -> bool:
        if check_type and not _fits(entry, field):
            rejected.append(f"{entry.canonical_name}: type")
            return False
        if _confusable_block(field.name, form, snap.confusable_pairs):
            rejected.append(f"{entry.canonical_name}: confusable")
            return False
        return True

    def done(entry, method, via, score, type_mismatch=None):
        return Mapping(field_id=field.field_id, name=field.name, canonical_name=entry.canonical_name, method=method,
                       via=via, score=score, instance_key=instance_key(entry, field.name, field.raw_value),
                       type_mismatch=type_mismatch, needs_instance=bool(entry.instance_rules),
                       value_is_name=entry.value_is_name,
                       rejected=sorted(set(rejected)))

    # exact / alias (on the full name and on the base name). The name is authoritative here, so a
    # type mismatch is a value-format problem to review, not a reason to drop the mapping.
    for entry in snap.entries:
        for form, method in [(entry.display_name, "exact"), (entry.canonical_name.replace("_", " "), "exact")] + \
                [(a.surface_form, "alias") for a in entry.aliases if a.status == "approved"]:
            if key_form(form) in (name_k, base) and ok(entry, form, check_type=False):
                mismatch = None if _fits(entry, field) else \
                    f"{(field.value or normalise(field.raw_value)).type.value} value for a {entry.data_type.value} field"
                return done(entry, method, form, 1.0, mismatch)

    # defined terms: the document's own definitions say what the name is
    if doc is not None and (m := _via_defined_term(field, doc, snap, ok, done)) is not None:
        return m

    # fuzzy on the base name
    best: tuple[float, FieldDictionaryEntry, str] | None = None
    for entry in snap.entries:
        for form in entry.surface_forms(approved_only=True):
            kf = key_form(form)
            contained = fuzz.token_set_ratio(base, kf)
            score = 0.5 * contained + 0.5 * fuzz.ratio(base, kf)
            extra = len(set(base.split()) - set(kf.split()))
            # A one-word form ("Borrower", "Collateral", "Lender") occurs inside many unrelated names
            # ("Applicant Borrower reference", "Minimum Collateral Amount"): containment only counts
            # for multi-word forms, or when the name adds at most one word.
            contains = contained >= CONTAINED_MIN and (len(kf.split()) >= 2 or extra <= 1)
            if (contains or score >= FUZZY_MIN) and (best is None or score > best[0]):
                if ok(entry, form):
                    best = (score, entry, form)
    if best:
        return done(best[1], "fuzzy", best[2], round(best[0] / 100, 3))

    # context: the section title names a repeatable entry, and an instance rule matches the name
    if section_title:
        title = key_form(section_title)
        for entry in snap.entries:
            if entry.instance_rules and fuzz.token_set_ratio(key_form(entry.display_name), title) >= CONTAINED_MIN:
                # Only when a rule genuinely matches the name — defaults ("base") would match anything.
                strict = entry.model_copy(update={"instance_rules": [
                    r.model_copy(update={"default": None}) for r in entry.instance_rules]})
                if instance_key(strict, field.name, field.raw_value) and ok(entry, entry.display_name):
                    return done(entry, "fuzzy", f"section: {section_title.strip()}", 0.8)

    return Mapping(field_id=field.field_id, name=field.name, canonical_name=None, method="none", via=None,
                   score=None, instance_key=None, rejected=rejected)


class AliasProposal(BaseModel):
    canonical_name: str
    surface_form: str
    seen: int
    example_value: str | None
    learned_via: str = "fuzzy"
    status: str = "pending_review"


class NewEntryProposal(BaseModel):
    surface_form: str
    seen: int
    example_value: str | None
    status: str = "pending_review"


class ReconcileReport(BaseModel):
    run_id: str
    dictionary_version: int
    fields: int
    pass2_agree: int = 0  # pass-1 fields confirmed by pass 2
    pass2_disagree: int = 0  # fields where the two passes read different values
    pass2_new: int = 0  # known fields only pass 2 found
    by_method: dict[str, int]
    mapped_core: list[str]
    missing_core: list[str]  # core fields not found on the extracted pages
    type_or_confusable_rejections: int
    alias_proposals: list[AliasProposal]
    new_entry_proposals: list[NewEntryProposal]
    auto_accepted: int
    needs_review: int
    review_reasons: dict[str, int]
    mappings: list[Mapping]


def _echoes_label(field: ExtractedField) -> bool:
    """'event_of_default (non-payment)' = 'Non-Payment': the model named the thing instead of giving its value."""
    v = key_form(field.raw_value or "")
    return len(v) >= 3 and not any(ch.isdigit() for ch in v) and f" {v} " in f" {key_form(field.name)} "


def resolve_name_value(field: ExtractedField, doc: DocumentExtraction | None) -> ExtractedField:
    """A party value that is itself a defined term ("Bank of America") is normalised to the legal name
    its definition gives ("Bank of America, N.A."). raw_value — what the page says — is unchanged."""
    from .defined_terms import entity_name, term_by_name

    if doc is None or field.canonical_name not in PARTY_ENTRIES or not field.raw_value:
        return field
    term = term_by_name(doc, field.raw_value)
    name = entity_name(term) if term else None
    if not name or text_key(name) == text_key(field.raw_value):
        return field
    return field.model_copy(update={"value": NormalisedValue(type=ValueType.text, text=name)})


def route(field: ExtractedField, m: Mapping) -> ExtractedField:
    """Combine value grounding (already on the field) with mapping confidence (spec §15)."""
    reasons = []
    verified = field.source.grounding is GroundingStatus.verified
    if not verified:
        reasons.append("value: not verified at source")
    elif field.model_confidence < AUTO_ACCEPT_CONFIDENCE:
        reasons.append("value: low model confidence")
    if m.type_mismatch:
        reasons.append(f"value: {m.type_mismatch}")
    if _echoes_label(field) and not m.value_is_name:
        reasons.append("value: repeats the field's own label, not a value")
    if m.canonical_name and m.instance_key is None and m.needs_instance:
        reasons.append(f"mapping: instance of repeatable {m.canonical_name} not determined")
    if m.method == "fuzzy":
        reasons.append(f"mapping: fuzzy match to {m.canonical_name} via {m.via!r}")
    auto = not reasons
    return field.model_copy(update={
        "canonical_name": m.canonical_name, "match_method": m.method, "match_score": m.score,
        "instance_key": m.instance_key, "review_reasons": reasons,
        "review_status": "auto_accepted" if auto else "needs_review",
    })


PARTY_ENTRIES = ("borrower", "administrative_agent", "lender", "guarantor")


_HEAD_NOUN_ROLES = ("borrower", "lender", "guarantor")  # "Belgian Borrower" is a borrower; "Syndication Agent" is not the agent


def term_roles(doc: DocumentExtraction, snap: DictionarySnapshot) -> dict[str, list[str]]:
    """Dictionary roles each defined term carries, from the document's own definitions.
    A member of a collective term inherits its roles: the Company is one of the "Borrowers"."""
    roles: dict[str, set[str]] = {}
    for t in doc.defined_terms:
        r = set(party_roles(t.term, snap))
        head = key_form(t.term).split()[-1:] or [""]
        r |= {n for n in _HEAD_NOUN_ROLES if head[0] == n}
        roles[t.term] = r
    for t in doc.defined_terms:
        for member in t.members:
            roles.setdefault(member, set()).update(roles.get(t.term, set()))
    return {k: sorted(v) for k, v in roles.items() if v}


def party_roles(role: str, snap: DictionarySnapshot) -> list[str]:
    """Dictionary keys a stated party role maps to. A role can name several ("Administrative Agent
    and Swing Line Lender"); matching is on whole approved surface forms only — no fuzzy tier."""
    words = f" {key_form(role)} "
    out = []
    for name in PARTY_ENTRIES:
        try:
            entry = snap.entry(name)
        except KeyError:
            continue
        forms = [key_form(f) for f in entry.surface_forms(approved_only=True)]
        # "Swing Line Lender" is a lender role; "Lender" must match as a whole role, not a suffix of one.
        if any(f" {f} " in words for f in forms if len(f.split()) >= 2) or \
                any(key_form(role) == f for f in forms):
            out.append(name)
    return out


def table_fields(doc: DocumentExtraction) -> list[ExtractedField]:
    """Each body cell of each table as a key-value field: '<column header> (<row label>)'.

    Makes schedules first-class key-values (e.g. "Commitment (Bank of America, N.A.)" = $170,000,000.00)
    so they reconcile like any field. Total rows are skipped — rows_sum_to_total already checks them.
    """
    out = []
    for s in doc.sections:
        for c in s.clauses:
            for t in c.tables:
                header = {x.col: (x.raw_value or "").strip() for x in t.cells if x.row == 0}
                label_header = header.get(0, "")
                for row in sorted({x.row for x in t.cells if x.row > 0}):
                    cells = {x.col: x for x in t.cells if x.row == row}
                    label = (cells[0].raw_value or "").strip() if 0 in cells else ""
                    if not label or text_key(label).startswith("total"):
                        continue
                    if "level" in label_header.casefold() and re.fullmatch(r"[0-9]+|[ivx]+", label, re.I):
                        label = f"Level {label}"
                    for col, cell in cells.items():
                        if not (cell.raw_value or "").strip() or not header.get(col):
                            continue
                        name = f"{header[col]} ({label})"
                        source = cell.source or SourceRef(page=t.pages[0], source_text=cell.raw_value)
                        out.append(ExtractedField(
                            field_id=f"{t.table_id}-r{row}c{col}", name=name, raw_value=cell.raw_value,
                            value=cell.value or normalise(cell.raw_value), model_confidence=0.9,
                            clause_id=t.clause_id, source=source))
    return out


def _same_mapped(a: ExtractedField, b: ExtractedField) -> bool:
    va, vb = a.value or normalise(a.raw_value), b.value or normalise(b.raw_value)
    return (a.canonical_name == b.canonical_name and a.instance_key == b.instance_key and a.source.page == b.source.page
            and va is not None and vb is not None and same_value(va, vb))


def _merge_pass2(pass1: list[ExtractedField], pass2: list[ExtractedField]) -> list[ExtractedField]:
    """Spec §8 merge. Same key + instance + page as a pass-1 field:
         same value      -> pass-1 field marked pass1_pass2_agree=True; pass-2 copy dropped
         different value -> both kept, both flagged for review (the disagreement is the signal)
       No pass-1 counterpart -> the pass-2 field is kept: something pass 1 missed.
    """
    by_id = {f.field_id: f for f in pass1}
    extra = []
    for s in pass2:
        if not s.canonical_name:
            continue  # pass 2 answers only in dictionary keys; an unmapped name is noise
        peers = [f for f in by_id.values() if f.canonical_name == s.canonical_name and
                 f.instance_key == s.instance_key and f.source.page == s.source.page]
        if not peers:
            extra.append(s)
            continue
        vs = s.value or normalise(s.raw_value)
        agree = [f for f in peers if (vf := f.value or normalise(f.raw_value)) is not None and vs is not None
                 and same_value(vf, vs)]
        if agree:
            for f in agree:
                by_id[f.field_id] = f.model_copy(update={
                    "agreement": f.agreement.model_copy(update={"pass1_pass2_agree": True})})
            continue
        note = f"value: pass 1 and pass 2 disagree ({peers[0].raw_value!r} vs {s.raw_value!r})"
        for f in peers:
            by_id[f.field_id] = f.model_copy(update={
                "agreement": f.agreement.model_copy(update={"pass1_pass2_agree": False}),
                "review_status": "needs_review", "review_reasons": f.review_reasons + [note]})
        extra.append(s.model_copy(update={
            "agreement": s.agreement.model_copy(update={"pass1_pass2_agree": False}),
            "review_status": "needs_review", "review_reasons": s.review_reasons + [note]}))
    return list(by_id.values()) + extra


def reconcile(doc: DocumentExtraction, snap: DictionarySnapshot) -> tuple[DocumentExtraction, ReconcileReport]:
    clause_section = {c.clause_id: s.title for s in doc.sections for c in s.clauses}
    all_fields = doc.flat_fields + table_fields(doc)
    mappings = {f.field_id: map_field(f, clause_section.get(f.clause_id), snap, doc) for f in all_fields}
    routed = {f.field_id: resolve_name_value(route(f, mappings[f.field_id]), doc) for f in all_fields}
    # A table cell the model also emitted as a field (same key, instance, page and value) is kept once.
    model_fields = [routed[f.field_id] for f in doc.flat_fields if f.extraction_pass == 1]
    derived = [routed[f.field_id] for f in all_fields[len(doc.flat_fields):]]
    derived = [d for d in derived if not (d.canonical_name and any(_same_mapped(d, m) for m in model_fields))]
    pass1 = _merge_pass2(model_fields + derived, [routed[f.field_id] for f in doc.flat_fields if f.extraction_pass == 2])
    model_fields, derived = pass1, []
    kept = {f.field_id for f in model_fields}
    mappings = {k: v for k, v in mappings.items() if k in kept}
    agreement = Counter(f.agreement.pass1_pass2_agree for f in model_fields)
    pass2_new = sum(f.extraction_pass == 2 and f.agreement.pass1_pass2_agree is None for f in model_fields)
    sections = [s.model_copy(update={"clauses": [
        c.model_copy(update={"fields": [routed[f.field_id] for f in c.fields]}) for c in s.clauses]})
        for s in doc.sections]
    by_term = {text_key(k): v for k, v in term_roles(doc, snap).items()}
    parties = [p.model_copy(update={"canonical_roles": sorted(set(party_roles(p.role, snap)) |
                                                             set(by_term.get(text_key(p.role).strip("\"“” "), [])))})
               for p in doc.parties]
    out = doc.model_copy(update={
        "flat_fields": model_fields + derived, "sections": sections, "parties": parties,
        "provenance": doc.provenance.model_copy(update={"dictionary_version": snap.version})})

    fields = out.flat_fields
    alias_counts: Counter = Counter()
    alias_example: dict = {}
    new_counts: Counter = Counter()
    new_example: dict = {}
    for f in fields:
        m = mappings[f.field_id]
        if m.method == "fuzzy" and not (m.via or "").startswith("section:"):
            k = (m.canonical_name, base_name(f.name))
            alias_counts[k] += 1
            alias_example.setdefault(k, f.raw_value)
        elif m.method == "none":
            k = base_name(f.name)
            new_counts[k] += 1
            new_example.setdefault(k, f.raw_value)
    mapped = {f.canonical_name for f in fields if f.canonical_name} | {r for p in parties for r in p.canonical_roles}
    reasons = Counter(r.split(":")[0] for f in fields for r in f.review_reasons)
    report = ReconcileReport(
        run_id=doc.provenance.run_id, dictionary_version=snap.version, fields=len(fields),
        pass2_agree=agreement.get(True, 0), pass2_disagree=agreement.get(False, 0), pass2_new=pass2_new,
        by_method=dict(Counter(m.method for m in mappings.values())),
        mapped_core=sorted(e.canonical_name for e in snap.core_fields if e.canonical_name in mapped),
        missing_core=sorted(e.canonical_name for e in snap.core_fields if e.canonical_name not in mapped),
        type_or_confusable_rejections=sum(len(m.rejected) for m in mappings.values()),
        alias_proposals=[AliasProposal(canonical_name=c, surface_form=s, seen=n, example_value=alias_example[(c, s)])
                         for (c, s), n in alias_counts.most_common()],
        new_entry_proposals=[NewEntryProposal(surface_form=s, seen=n, example_value=new_example[s])
                             for s, n in new_counts.most_common()],
        auto_accepted=sum(f.review_status == "auto_accepted" for f in fields),
        needs_review=sum(f.review_status == "needs_review" for f in fields),
        review_reasons=dict(reasons), mappings=list(mappings.values()),
    )
    return out, report
