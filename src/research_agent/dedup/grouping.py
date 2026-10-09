"""Exact-duplicate grouping of ``ExtractedDocument``s (Sprint 06 design, approved at ``15afa1e``).

- L1 same source identity, L2 same raw ``content_sha256``, L3 same verified ``text_sha256``
  (``SUCCESS`` only); three independent group lists, no transitive merging.
- Non-destructive: every input document is returned unchanged, in order, with a reference;
  groups list all members; nothing is merged into a synthetic document.
- Deterministic for the exact input sequence: one pass in input order, insertion-ordered dicts,
  representative = lowest position; keys never come from uuids, time, ``hash()`` or sets.
- No text normalization, no document-count limit, no deadline. Pure, synchronous, no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from research_agent.dedup.identity import host_of, is_sha256_hex, l1_key, l2_key, l3_key
from research_agent.dedup.identity import source_identity as compute_source_identity
from research_agent.dedup.models import (
    ERROR_REASONS,
    DedupLevel,
    DedupStats,
    DocumentError,
    DocumentRef,
    DocumentSet,
    DuplicateGroup,
    Exclusion,
    ExclusionReason,
    GroupWarning,
)
from research_agent.extraction.models import ExtractedDocument, ExtractionStatus
from research_agent.logging import get_logger

log = get_logger(__name__)

S06_VERSION = "s06.1"
S05_MAX_TEXT_CHARS = 10_000_000
"""Upper bound of S05's configurable ``max_text_chars``; a longer text violates the S05 contract."""

_R = ExclusionReason
_LEVELS = (DedupLevel.L1, DedupLevel.L2, DedupLevel.L3)


def _verified_text_key(document: ExtractedDocument) -> tuple[str | None, ExclusionReason | None]:
    """L3 key = SHA-256 recomputed from ``text`` (S05 input semantics), or an exclusion reason.

    The key returned is the recomputed digest, so no L3 grouping can happen without
    verification."""
    if document.status is not ExtractionStatus.SUCCESS:
        return None, _R.NOT_SUCCESS
    text = document.text
    if not text:
        return None, (_R.TEXT_HASH_MISMATCH if document.text_sha256 is not None else _R.EMPTY_TEXT)
    if len(text) > S05_MAX_TEXT_CHARS:
        return None, _R.TEXT_TOO_LONG
    if document.text_sha256 is None:
        return None, _R.MISSING_TEXT_HASH
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None, _R.TEXT_HASH_UNVERIFIABLE
    if digest != document.text_sha256:
        return None, _R.TEXT_HASH_MISMATCH
    return digest, None


def _distinct(values: list[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is not None:
            seen.setdefault(value, None)
    return list(seen)


def _group_warnings(level: DedupLevel, members: list[DocumentRef]) -> list[GroupWarning]:
    warnings: list[GroupWarning] = []
    if level is DedupLevel.L1:
        if len(_distinct([m.content_sha256 for m in members])) > 1:
            warnings.append(GroupWarning.SOURCE_CONTENT_DIFFERS)
        if len({m.fetch_status for m in members}) > 1:
            warnings.append(GroupWarning.MIXED_FETCH_STATUS)
        return warnings
    if level is DedupLevel.L2 and len({m.text_sha256 for m in members}) > 1:
        warnings.append(GroupWarning.RAW_DUPLICATE_TEXT_DIFFERS)
    if len(_distinct([m.source_identity for m in members])) > 1:
        warnings.append(GroupWarning.CROSS_SOURCE_DUPLICATE)
    return warnings


def group_documents(documents: Sequence[ExtractedDocument]) -> DocumentSet:
    """Group ``documents`` into exact-duplicate groups at L1, L2 and L3.

    Raises ``TypeError`` before any processing if the input is not a sequence of
    ``ExtractedDocument``. Per-document problems never raise: the document is kept, excluded from
    the affected level and reported in ``errors``. Unexpected exceptions propagate."""
    if isinstance(documents, str | bytes) or not isinstance(documents, Sequence):
        raise TypeError("documents must be a sequence of ExtractedDocument")
    items = list(documents)
    for item in items:
        if not isinstance(item, ExtractedDocument):
            raise TypeError("documents must be a sequence of ExtractedDocument")

    refs: list[DocumentRef] = []
    buckets: dict[DedupLevel, dict[str, list[int]]] = {level: {} for level in _LEVELS}
    exclusions: list[Exclusion] = []

    def exclude(position: int, level: DedupLevel, reason: ExclusionReason) -> None:
        exclusions.append(Exclusion(position=position, level=level, reason=reason))

    for position, document in enumerate(items):
        provenance = document.provenance
        identity = compute_source_identity(document)
        refs.append(
            DocumentRef(
                position=position,
                document_id=document.document_id,
                crawl_id=provenance.crawl_id,
                source_identity=identity,
                host=host_of(identity),
                requested_url=provenance.requested_url,
                final_url=provenance.final_url,
                status=document.status,
                fetch_status=document.fetch_status,
                warnings=list(document.warnings),
                content_sha256=provenance.content_sha256,
                text_sha256=document.text_sha256,
                kind=document.kind,
                trust=document.trust,
                search_source=provenance.source,
            )
        )

        if identity is None:
            exclude(position, DedupLevel.L1, _R.INVALID_SOURCE_URL)
        else:
            buckets[DedupLevel.L1].setdefault(l1_key(identity), []).append(position)

        content_hash = provenance.content_sha256
        if content_hash is None:
            exclude(position, DedupLevel.L2, _R.NO_CONTENT_HASH)
        elif not is_sha256_hex(content_hash):
            exclude(position, DedupLevel.L2, _R.INVALID_CONTENT_HASH)
        else:
            buckets[DedupLevel.L2].setdefault(l2_key(content_hash), []).append(position)

        text_key, reason = _verified_text_key(document)
        if text_key is None:
            exclude(position, DedupLevel.L3, reason or _R.NOT_SUCCESS)
        else:
            buckets[DedupLevel.L3].setdefault(l3_key(text_key), []).append(position)

    groups: dict[DedupLevel, list[DuplicateGroup]] = {}
    for level in _LEVELS:
        level_groups: list[DuplicateGroup] = []
        for key, members in buckets[level].items():  # insertion order = first appearance
            if len(members) < 2:
                continue
            member_refs = [refs[p] for p in members]
            level_groups.append(
                DuplicateGroup(
                    key=key,
                    level=level,
                    representative=members[0],
                    members=list(members),
                    source_identities=_distinct([m.source_identity for m in member_refs]),
                    hosts=_distinct([m.host for m in member_refs]),
                    warnings=_group_warnings(level, member_refs),
                )
            )
        groups[level] = level_groups

    errors = [
        DocumentError(position=e.position, level=e.level, code=e.reason)
        for e in exclusions
        if e.reason in ERROR_REASONS
    ]
    stats = DedupStats(
        documents=len(items),
        groups={level: len(groups[level]) for level in _LEVELS},
        grouped_documents={level: sum(len(g.members) for g in groups[level]) for level in _LEVELS},
        exclusions=len(exclusions),
        errors=len(errors),
    )
    log.info(
        "dedup.grouped",
        documents=stats.documents,
        groups={level.value: count for level, count in stats.groups.items()},
        errors=stats.errors,
    )
    return DocumentSet(
        s06_version=S06_VERSION,
        documents=items,
        refs=refs,
        groups=groups,
        exclusions=exclusions,
        errors=errors,
        stats=stats,
    )
