"""Dictionary approval (spec §9, §14). No model involved.

Reviewer-confirmed mappings become APPROVED aliases in the NEXT snapshot. The existing snapshot
is never modified. Only a named human may approve: fuzzy-learned aliases are the dictionary's
poisoning risk, and an AI confirming an AI's mapping is not independent evidence.
"""

import re
from datetime import datetime, timezone

from pydantic import BaseModel

from .contracts import Alias, DictionarySnapshot
from .evaluate import is_ai_verifier
from .reconcile import _LEVEL, _QUALIFIER, key_form
from .review import AliasProposal


class ApprovalResult(BaseModel):
    new_version: int
    added: list[str]  # "canonical_name <- alias"
    already_known: list[str]
    rejected: list[str]  # unknown keys, or aliases that would break snapshot rules


def alias_form(name: str) -> str:
    """The alias to store: the field name without qualifiers or level markers, original casing."""
    s = _LEVEL.sub(" ", _QUALIFIER.sub(" ", name))
    return re.sub(r"\s+", " ", s).strip(" -–—:,")


def approve(snap: DictionarySnapshot, proposals: list[AliasProposal], approver: str,
            now: datetime | None = None) -> tuple[DictionarySnapshot, ApprovalResult]:
    if not approver.strip():
        raise ValueError("an approver name is required")
    if is_ai_verifier(approver):
        raise PermissionError(f"{approver!r} looks like an AI: dictionary aliases need a human approver (spec §9)")
    entries = {e.canonical_name: e for e in snap.entries}
    added, known, rejected = [], [], []
    for p in proposals:
        form = alias_form(p.surface_form)
        entry = entries.get(p.canonical_name)
        if entry is None:
            rejected.append(f"{p.canonical_name} <- {form!r}: no such dictionary entry (create it first)")
            continue
        if not form or key_form(form) in {key_form(x) for x in entry.surface_forms(approved_only=False)}:
            known.append(f"{p.canonical_name} <- {form!r}")
            continue
        candidate = entry.model_copy(update={
            "aliases": entry.aliases + [Alias(surface_form=form, status="approved", learned_via="human",
                                              first_seen_document=p.source)],
            "version": entry.version + 1})
        trial = snap.model_copy(update={"entries": [candidate if e.canonical_name == p.canonical_name else e
                                                    for e in entries.values()]})
        try:
            DictionarySnapshot.model_validate(trial.model_dump())  # uniqueness + confusable-pair rules
        except ValueError as e:
            rejected.append(f"{p.canonical_name} <- {form!r}: {str(e).splitlines()[-1]}")
            continue
        entries[p.canonical_name] = candidate
        added.append(f"{p.canonical_name} <- {form!r}")
    now = now or datetime.now(timezone.utc)
    new = snap.model_copy(update={
        "version": snap.version + 1, "parent_version": snap.version, "created_at": now,
        "entries": list(entries.values()),
        "notes": f"v{snap.version + 1}: {len(added)} aliases approved by {approver} on {now:%Y-%m-%d} from review"})
    return DictionarySnapshot.model_validate(new.model_dump()), ApprovalResult(
        new_version=new.version, added=added, already_known=known, rejected=rejected)


def add_entries(snap: DictionarySnapshot, entries: list, approver: str,
                now: datetime | None = None) -> DictionarySnapshot:
    """A named person accepts AI-drafted new entries (meanings as drafted or edited in the proposal
    file) into the next snapshot, as approved. Snapshot rules (unique names, confusables) still apply."""
    if not approver.strip() or is_ai_verifier(approver):
        raise PermissionError(f"{approver!r}: new dictionary entries need a named human approver (spec §9)")
    known = {e.canonical_name for e in snap.entries}
    fresh = [e.model_copy(update={"status": "approved"}) for e in entries if e.canonical_name not in known]
    if not fresh:
        raise ValueError("nothing new to add")
    now = now or datetime.now(timezone.utc)
    new = snap.model_copy(update={
        "version": snap.version + 1, "parent_version": snap.version, "created_at": now,
        "entries": snap.entries + fresh,
        "notes": f"v{snap.version + 1}: {len(fresh)} entries added by {approver} on {now:%Y-%m-%d} "
                 f"({', '.join(e.canonical_name for e in fresh)})"})
    return DictionarySnapshot.model_validate(new.model_dump())
