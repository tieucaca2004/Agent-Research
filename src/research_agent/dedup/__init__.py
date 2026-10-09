"""Normalization / dedup (Sprint 06): exact-duplicate groups over S05 documents, non-destructive."""

from research_agent.dedup.grouping import S06_VERSION, group_documents
from research_agent.dedup.identity import source_identity
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

__all__ = [
    "ERROR_REASONS",
    "S06_VERSION",
    "DedupLevel",
    "DedupStats",
    "DocumentError",
    "DocumentRef",
    "DocumentSet",
    "DuplicateGroup",
    "Exclusion",
    "ExclusionReason",
    "GroupWarning",
    "group_documents",
    "source_identity",
]
