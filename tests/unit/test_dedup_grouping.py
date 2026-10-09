"""Sprint 06: exact-duplicate grouping (design approved at 15afa1e; matrix §15, mutants §16)."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from research_agent.crawler.models import FetchStatus
from research_agent.dedup import (
    ERROR_REASONS,
    DedupLevel,
    DocumentSet,
    ExclusionReason,
    GroupWarning,
    group_documents,
)
from research_agent.dedup import grouping as grouping_module
from research_agent.extraction.config import ExtractionSettings
from research_agent.extraction.models import ExtractionStatus, ExtractionWarning
from tests.dedup_support import make_doc, not_fetched, sha

L1, L2, L3 = DedupLevel.L1, DedupLevel.L2, DedupLevel.L3
R = ExclusionReason
W = GroupWarning
ROOT = Path(__file__).resolve().parents[2]


def members(result: DocumentSet, level: DedupLevel) -> list[list[int]]:
    return [g.members for g in result.groups[level]]


def reasons(result: DocumentSet, position: int) -> dict[DedupLevel, ExclusionReason]:
    return {e.level: e.reason for e in result.exclusions if e.position == position}


# -- levels ----------------------------------------------------------------------------------------


def test_l1_groups_same_source_with_content_and_status_warnings() -> None:
    docs = [
        make_doc("version one", url="https://a.example/p?utm_source=x"),
        make_doc("other page", url="https://b.example/q"),
        make_doc("version two", url="https://a.example/p#frag"),
        not_fetched("https://a.example/p"),
    ]
    result = group_documents(docs)
    assert members(result, L1) == [[0, 2, 3]]
    group = result.groups[L1][0]
    assert group.key == "L1:url:https://a.example/p"
    assert group.representative == 0
    assert group.warnings == [W.SOURCE_CONTENT_DIFFERS, W.MIXED_FETCH_STATUS]
    assert group.source_identities == ["https://a.example/p"] and group.hosts == ["a.example"]


def test_l2_groups_identical_raw_bytes_even_when_texts_differ() -> None:
    """N15: content_sha256 is used as given; same bytes decoded differently stay one L2 group."""
    docs = [
        make_doc("café q", url="https://a.example/1", raw="<p>caf\\xe9 \\x93q\\x94</p>"),
        make_doc("café “q”", url="https://b.example/2", raw="<p>caf\\xe9 \\x93q\\x94</p>"),
    ]
    result = group_documents(docs)
    assert members(result, L2) == [[0, 1]]
    assert result.groups[L2][0].key == "L2:sha256:" + sha("<p>caf\\xe9 \\x93q\\x94</p>")
    assert result.groups[L2][0].warnings == [W.RAW_DUPLICATE_TEXT_DIFFERS, W.CROSS_SOURCE_DUPLICATE]
    assert members(result, L3) == []  # texts differ


def test_l3_groups_identical_verified_text_across_sources() -> None:
    text = "Same article text, copied by a mirror."
    docs = [
        make_doc(text, url="https://origin.example/a"),
        make_doc("something else", url="https://x.example/b"),
        make_doc(text, url="https://mirror.example/a", raw="different bytes"),
    ]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 2]]
    group = result.groups[L3][0]
    assert group.key == "L3:sha256:" + sha(text)
    assert group.warnings == [W.CROSS_SOURCE_DUPLICATE]
    assert group.hosts == ["origin.example", "mirror.example"]
    assert members(result, L2) == []


def test_same_source_exact_copies_are_not_cross_source() -> None:
    docs = [
        make_doc("t", url="https://a.example/p", raw="r"),
        make_doc("t", url="https://a.example/p#x", raw="r"),
    ]
    result = group_documents(docs)
    assert all(g.warnings == [] for level in (L2, L3) for g in result.groups[level])
    assert result.groups[L1][0].warnings == []


def test_n_way_groups_and_mixed_uniques() -> None:
    docs = [make_doc(f"text {i % 3}", url=f"https://h{i}.example/") for i in range(9)]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 3, 6], [1, 4, 7], [2, 5, 8]]
    assert members(result, L1) == []
    assert result.stats.grouped_documents[L3] == 9


def test_no_transitive_merging_across_levels() -> None:
    """N16: A~B only at L2, B~C only at L3 → no group contains both A and C."""
    a = make_doc("text A", url="https://a.example/", raw="shared bytes")
    b = make_doc("text B", url="https://b.example/", raw="shared bytes")
    c = make_doc("text B", url="https://c.example/", raw="other bytes")
    result = group_documents([a, b, c])
    assert members(result, L2) == [[0, 1]]
    assert members(result, L3) == [[1, 2]]
    assert members(result, L1) == []
    assert not any({0, 2} <= set(g.members) for level in (L1, L2, L3) for g in result.groups[level])


# -- eligibility -----------------------------------------------------------------------------------


def test_partial_truncation_twins_are_not_l3_but_stay_l1_and_l2() -> None:
    """N12: equal truncated prefixes do not prove equal documents (E5)."""
    prefix = "y" * 1000
    docs = [
        make_doc(prefix, url="https://a.example/p", status=ExtractionStatus.PARTIAL, raw="bytes"),
        make_doc(prefix, url="https://a.example/p", status=ExtractionStatus.PARTIAL, raw="bytes"),
    ]
    result = group_documents(docs)
    assert members(result, L3) == []
    assert members(result, L2) == [[0, 1]] and members(result, L1) == [[0, 1]]
    assert reasons(result, 0)[L3] is R.NOT_SUCCESS
    assert result.errors == []  # ineligibility is not an error


@pytest.mark.parametrize(
    "status",
    [ExtractionStatus.EMPTY, ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED],
)
def test_non_success_documents_never_join_l3(status: ExtractionStatus) -> None:
    docs = [
        make_doc("same", url="https://a.example/", status=status),
        make_doc("same", url="https://b.example/", status=status),
    ]
    result = group_documents(docs)
    assert members(result, L3) == []
    assert reasons(result, 0)[L3] is R.NOT_SUCCESS and reasons(result, 1)[L3] is R.NOT_SUCCESS


def test_not_fetched_documents_take_part_in_l1_only() -> None:
    docs = [not_fetched("https://a.example/p"), make_doc("ok", url="https://a.example/p")]
    result = group_documents(docs)
    assert members(result, L1) == [[0, 1]]
    assert reasons(result, 0) == {L2: R.NO_CONTENT_HASH, L3: R.NOT_SUCCESS}


def test_empty_text_is_never_keyed() -> None:
    """N13: two documents without text never form an L3 group."""
    docs = [
        make_doc("", url="https://a.example/", status=ExtractionStatus.EMPTY),
        make_doc("", url="https://b.example/", status=ExtractionStatus.EMPTY),
        make_doc("", url="https://c.example/"),  # SUCCESS with empty text (hand-built)
        make_doc("", url="https://d.example/"),
    ]
    result = group_documents(docs)
    assert members(result, L3) == []
    assert reasons(result, 2)[L3] is R.EMPTY_TEXT and reasons(result, 3)[L3] is R.EMPTY_TEXT


# -- hash verification (P8, P8a) -------------------------------------------------------------------


def test_real_hashes_verify_and_group() -> None:
    docs = [
        make_doc("Phở bò 45.000đ 👨\u200d👩\u200d👧", url=f"https://h{i}.example/")
        for i in range(2)
    ]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 1]] and result.errors == []


def test_tampered_hashes_are_never_grouped() -> None:
    """N11: grouping on the stored hash without recomputing would merge these two."""
    fake = sha("forged")
    docs = [
        make_doc("first text", url="https://a.example/", text_sha256=fake),
        make_doc("second text", url="https://b.example/", text_sha256=fake),
    ]
    result = group_documents(docs)
    assert members(result, L3) == []
    for position in (0, 1):
        assert reasons(result, position)[L3] is R.TEXT_HASH_MISMATCH
    assert [(e.position, e.level, e.code) for e in result.errors] == [
        (0, L3, R.TEXT_HASH_MISMATCH),
        (1, L3, R.TEXT_HASH_MISMATCH),
    ]
    assert len(result.documents) == 2  # both kept


def test_forged_copy_of_a_real_hash_is_not_grouped() -> None:
    """A verification cache keyed on the stored hash would let B ride on A's verified digest."""
    docs = [
        make_doc("alpha", url="https://a.example/"),
        make_doc("beta", url="https://b.example/", text_sha256=sha("alpha")),
        make_doc("alpha", url="https://c.example/", raw="other"),
    ]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 2]]
    assert reasons(result, 1)[L3] is R.TEXT_HASH_MISMATCH


@pytest.mark.parametrize("size", [1, 999, 100_001, 1_000_001, 10_000_000])
def test_tampered_hash_is_caught_at_every_text_size(size: int) -> None:
    """No size-based fast path skips verification."""
    fake = sha("forged")
    docs = [
        make_doc("a" * size, url="https://a.example/", text_sha256=fake, raw="r"),
        make_doc("a" * size, url="https://b.example/", text_sha256=fake, raw="r"),
    ]
    result = group_documents(docs)
    assert members(result, L3) == []
    assert reasons(result, 0)[L3] is R.TEXT_HASH_MISMATCH


def test_every_l3_member_was_hashed_from_its_own_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """No path reaches an L3 bucket without sha256(text.encode("utf-8")) of that document."""
    hashed: list[bytes] = []
    real = hashlib.sha256

    def recording(data: bytes = b"") -> Any:
        hashed.append(data)
        return real(data)

    docs = [
        make_doc("same text", url="https://a.example/", raw="r"),
        make_doc("same text", url="https://b.example/", raw="r"),
        make_doc(
            "same text",
            url="https://c.example/",
            raw="o",
            warnings=[ExtractionWarning.THIN_CONTENT],
        ),
        make_doc("other", url="https://d.example/"),
    ]
    monkeypatch.setattr("research_agent.dedup.grouping.hashlib.sha256", recording)
    result = group_documents(docs)
    monkeypatch.undo()
    assert members(result, L3) == [[0, 1, 2]]
    assert hashed.count(b"same text") == 3 and hashed.count(b"other") == 1
    assert len(hashed) == 4  # exactly one verification per eligible document


def test_hash_verification_failures_keep_the_document() -> None:
    docs = [
        make_doc("no hash", url="https://a.example/", text_sha256=None),
        make_doc("", url="https://b.example/", text_sha256=sha("x")),  # hash on empty text
        make_doc("lone \ud800 surrogate", url="https://c.example/", raw="r", text_sha256=sha("x")),
        make_doc("ok", url="https://d.example/"),
    ]
    result = group_documents(docs)
    assert reasons(result, 0)[L3] is R.MISSING_TEXT_HASH
    assert reasons(result, 1)[L3] is R.TEXT_HASH_MISMATCH
    assert reasons(result, 2)[L3] is R.TEXT_HASH_UNVERIFIABLE  # N19: no crash
    assert L3 not in reasons(result, 3)
    assert [d.document_id for d in result.documents] == [d.document_id for d in docs]
    assert {e.position for e in result.errors} == {0, 1, 2}


def test_text_longer_than_the_s05_maximum_is_a_contract_violation() -> None:
    limit = next(
        m.le for m in ExtractionSettings.model_fields["max_text_chars"].metadata if hasattr(m, "le")
    )
    assert limit == grouping_module.S05_MAX_TEXT_CHARS
    long_text = "x" * (limit + 1)
    result = group_documents(
        [
            make_doc(long_text, url="https://a.example/"),
            make_doc(long_text, url="https://b.example/"),
        ]
    )
    assert members(result, L3) == []
    assert reasons(result, 0)[L3] is R.TEXT_TOO_LONG
    exact = "x" * limit
    ok = group_documents(
        [make_doc(exact, url="https://a.example/"), make_doc(exact, url="https://b.example/")]
    )
    assert members(ok, L3) == [[0, 1]]


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "g" * 64, "sha256:" + "a" * 57])
def test_malformed_content_hash_is_excluded_from_l2(bad: str) -> None:
    docs = [
        make_doc("t1", url="https://a.example/", content_sha256=bad),
        make_doc("t2", url="https://b.example/", content_sha256=bad),
    ]
    result = group_documents(docs)
    assert members(result, L2) == []
    assert reasons(result, 0)[L2] is R.INVALID_CONTENT_HASH
    assert any(e.code is R.INVALID_CONTENT_HASH for e in result.errors)


def test_verification_cannot_be_disabled() -> None:
    assert list(inspect.signature(group_documents).parameters) == ["documents"]
    assert not hasattr(grouping_module, "VERIFY") and not hasattr(grouping_module, "settings")


def test_errors_are_exactly_the_contract_violations() -> None:
    docs = [
        not_fetched(),
        make_doc("x", text_sha256=sha("y")),
        make_doc("p", status=ExtractionStatus.PARTIAL),
    ]
    result = group_documents(docs)
    expected = [
        (e.position, e.level, e.reason) for e in result.exclusions if e.reason in ERROR_REASONS
    ]
    assert (
        [(e.position, e.level, e.code) for e in result.errors]
        == expected
        == [(1, L3, R.TEXT_HASH_MISMATCH)]
    )


# -- no second normalization (P9) ------------------------------------------------------------------


def test_s06_does_not_normalize_text() -> None:
    """N1/N2: NFC vs NFD and NFKC-sensitive texts keep their own verified hashes."""
    nfc = "Phở bò Nguyễn Trãi"
    nfd = unicodedata.normalize("NFD", nfc)
    docs = [
        make_doc(nfc, url="https://a.example/"),
        make_doc(nfd, url="https://b.example/"),
        make_doc("E=mc² ① \uff12", url="https://c.example/"),
        make_doc("E=mc2 1 2", url="https://d.example/"),
        make_doc("line\r\nbreak\u00a0x", url="https://e.example/"),
        make_doc("  padded text \n", url="https://f.example/"),
        make_doc("padded text", url="https://g.example/"),
    ]
    before = [d.model_dump() for d in docs]
    result = group_documents(docs)
    assert result.exclusions == []  # every hash verified on the unmodified text
    assert members(result, L3) == []
    assert [d.model_dump() for d in result.documents] == before


# -- provenance (P5, P14) --------------------------------------------------------------------------


def test_documents_and_references_are_complete_and_unchanged() -> None:
    docs = [
        make_doc("t", url="https://a.example/", warnings=[ExtractionWarning.THIN_CONTENT]),
        make_doc("t", url="https://b.example/"),
        not_fetched("https://c.example/"),
    ]
    snapshot = [d.model_dump() for d in docs]
    result = group_documents(docs)
    assert all(out is inp for out, inp in zip(result.documents, docs, strict=True))
    assert [d.model_dump() for d in docs] == snapshot
    assert [r.position for r in result.refs] == [0, 1, 2]
    for ref, doc in zip(result.refs, docs, strict=True):
        assert ref.document_id == doc.document_id and ref.crawl_id == doc.provenance.crawl_id
        assert (
            ref.requested_url == doc.provenance.requested_url
            and ref.final_url == doc.provenance.final_url
        )
        assert ref.status is doc.status and ref.fetch_status is doc.fetch_status
        assert ref.warnings == doc.warnings
        assert (
            ref.content_sha256 == doc.provenance.content_sha256
            and ref.text_sha256 == doc.text_sha256
        )
        assert ref.kind == "source_document" and ref.trust == "UNTRUSTED"
        assert ref.search_source is doc.provenance.source
    group = result.groups[L3][0]
    urls = [result.refs[p].final_url for p in group.members]
    assert urls == ["https://a.example/", "https://b.example/"]  # every member URL reachable


def test_group_lists_every_member_even_for_large_groups() -> None:
    """N9: no member is collapsed away."""
    docs = [make_doc("same", url=f"https://h{i}.example/") for i in range(50)]
    result = group_documents(docs)
    assert members(result, L3) == [list(range(50))]
    assert len(result.groups[L3][0].hosts) == 50


# -- representative, ordering, determinism (P6, P15) -----------------------------------------------


def test_representative_is_the_lowest_position_and_groups_are_ordered() -> None:
    docs = [
        make_doc("b", url="https://1.example/"),
        make_doc("a", url="https://2.example/"),
        make_doc("b", url="https://3.example/"),
        make_doc("a", url="https://4.example/"),
        make_doc("b", url="https://5.example/"),
    ]
    result = group_documents(docs)
    assert [(g.representative, g.members) for g in result.groups[L3]] == [
        (0, [0, 2, 4]),
        (1, [1, 3]),
    ]


def test_reversed_input_keeps_member_sets_and_moves_representatives() -> None:
    docs = [make_doc(t, url=f"https://{i}.example/") for i, t in enumerate("abab")]
    forward = group_documents(docs)
    backward = group_documents(list(reversed(docs)))

    def by_id(result: DocumentSet) -> list[set[str]]:
        return sorted(
            ({result.refs[p].document_id for p in g.members} for g in result.groups[L3]), key=sorted
        )

    assert by_id(forward) == by_id(backward)
    assert [docs[g.representative].document_id for g in forward.groups[L3]] == [
        docs[0].document_id,
        docs[1].document_id,
    ]
    rev = list(reversed(docs))
    assert [rev[g.representative].document_id for g in backward.groups[L3]] == [
        docs[3].document_id,
        docs[2].document_id,
    ]


def test_same_input_gives_identical_output() -> None:
    docs = [
        make_doc(f"t{i % 4}", url=f"https://h{i % 6}.example/", raw=f"r{i % 3}") for i in range(30)
    ]
    first = group_documents(docs).model_dump_json()
    assert all(group_documents(docs).model_dump_json() == first for _ in range(50))


_DETERMINISM_SCRIPT = """
import sys
from tests.dedup_support import make_doc
from research_agent.dedup import group_documents
docs = [
    make_doc(f"t{i % 4}", url=f"https://h{i % 6}.example/", raw=f"r{i % 3}", doc_id=f"d{i}")
    for i in range(30)
]
sys.stdout.write(group_documents(docs).model_dump_json(exclude={"documents"}))
"""


def test_output_independent_of_hash_seed() -> None:
    outputs = set()
    for seed in ("0", "1", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        outputs.add(completed.stdout.strip().splitlines()[-1])
    assert len(outputs) == 1
    assert json.loads(outputs.pop())["stats"]["documents"] == 30


# -- failure semantics (P16) and no limit (P12) ----------------------------------------------------


@pytest.mark.parametrize("bad", [None, "not documents", b"bytes", 42, (d for d in [make_doc()])])
def test_non_sequence_input_is_a_type_error(bad: object) -> None:
    with pytest.raises(TypeError):
        group_documents(bad)  # type: ignore[arg-type]


def test_foreign_item_is_rejected_before_processing() -> None:
    with pytest.raises(TypeError):
        group_documents([make_doc(), {"text": "x"}])  # type: ignore[list-item]


def test_all_bad_documents_are_kept_with_errors() -> None:
    docs = [
        make_doc(
            "a", url="javascript:x", final_url=None, text_sha256=sha("z"), content_sha256="bad"
        )
        for _ in range(3)
    ]
    result = group_documents(docs)
    assert all(result.groups[level] == [] for level in (L1, L2, L3))
    assert len(result.documents) == 3 and len(result.refs) == 3
    assert {(e.position, e.code) for e in result.errors} == {
        (p, code)
        for p in range(3)
        for code in (R.INVALID_SOURCE_URL, R.INVALID_CONTENT_HASH, R.TEXT_HASH_MISMATCH)
    }


def test_unexpected_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(document: object) -> str:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(grouping_module, "compute_source_identity", boom)
    with pytest.raises(RuntimeError):
        group_documents([make_doc()])


def test_ten_thousand_documents_are_processed_completely() -> None:
    """P12 / N20: no hidden limit or truncation."""
    docs = [make_doc(f"g{i // 5}", url=f"https://h{i}.example/") for i in range(10_000)]
    result = group_documents(docs)
    assert len(result.refs) == len(result.documents) == 10_000
    assert result.stats.groups[L3] == 2_000 and result.stats.grouped_documents[L3] == 10_000
    assert result.groups[L3][-1].members == [9995, 9996, 9997, 9998, 9999]


def test_empty_input() -> None:
    result = group_documents([])
    assert result.documents == [] and result.refs == [] and result.stats.documents == 0
    assert set(result.groups) == {L1, L2, L3}


def test_logs_carry_counts_only() -> None:
    docs = [make_doc("SECRET TEXT", url="https://secret.example/p?token=QUERYSECRET")] * 2
    with capture_logs() as logs:
        group_documents(docs)
    record = next(e for e in logs if e["event"] == "dedup.grouped")
    assert record["documents"] == 2
    assert "SECRET" not in repr(logs) and "QUERYSECRET" not in repr(logs)


def test_fetch_status_values_are_not_merged_into_identity() -> None:
    docs = [
        not_fetched("https://a.example/"),
        make_doc("x", url="https://a.example/", fetch_status=FetchStatus.OK),
    ]
    assert members(group_documents(docs), L1) == [[0, 1]]
