"""
agents/test_reconcile_statement.py

STMT-005 acceptance tests for reconcile_statement.py.

These tests exercise the INDEPENDENT reconciliation path -- not merely
that fee_statement.generate_fee_statement() succeeds. Ledger rows are
seeded directly via db_service.SchoolDB.record_payment() /
insert_raw_payment_row() (the raw ledger writer), statements are
produced with the real generator exactly once per scenario to get a
stored version to reconcile against, and then reconciliation is run
and checked against the *independently re-derived* expectation, not
against whatever the generator happened to say.

Acceptance criterion -> test map:
    Test 1  Payment-ID set equality      -> test_payment_id_set_equality_reconciles
                                             test_missing_payment_id_on_statement_fails
                                             test_extra_payment_id_on_statement_fails
    Test 2  Printed total                -> test_printed_total_consistent_reconciles
                                             test_printed_total_tampered_fails
    Test 3  Unchanged regeneration       -> test_unchanged_regeneration_same_version_reconciles
    Section 16  Ledger change            -> test_ledger_change_creates_new_version_old_stays_reconciled
    Section 17  Immutability             -> test_statement_versions_update_blocked_by_trigger
                                             test_statement_versions_delete_blocked_by_trigger
    Section 18  Zero-payment period      -> test_zero_payment_period_reconciles
    Section 19  Fingerprint              -> test_fingerprint_unchanged_ledger_reconciles
                                             test_fingerprint_changed_ledger_fails
    Section 20  Independence             -> test_reconciliation_survives_generator_being_broken
    Section 6   Duplicate/BLOCKED        -> test_conflicting_duplicate_ledger_rows_blocked
                                             test_blocked_explanation_names_conflicting_ids
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fee_statement  # noqa: E402
from db_service import SchoolDB  # noqa: E402
from fee_statement import generate_fee_statement  # noqa: E402
from reconcile_statement import (  # noqa: E402
    BLOCKED,
    FAILS,
    RECONCILES,
    StatementNotFoundError,
    reconcile_statement,
)
from statement_store import PeriodFormatError, get_version  # noqa: E402

STUDENT_ID = 1
START = "2026-01-01"
END = "2026-01-31"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "school.db"
    path.touch()
    return str(path)


def _seed_payment(db_path, student_id=STUDENT_ID, amount_paid=50.0,
                   payment_date="2026-01-10", term="Term1", payment_method="cash",
                   status="paid", amount_due=100.0):
    with SchoolDB(db_path) as db:
        return db.record_payment(
            student_id, term, amount_due, amount_paid, payment_date, payment_method, status
        )


def _seed_payment_with_id(db_path, payment_id, student_id=STUDENT_ID, amount_paid=50.0,
                           payment_date="2026-01-10", term="Term1", payment_method="cash",
                           status="paid", amount_due=100.0):
    with SchoolDB(db_path) as db:
        db.insert_raw_payment_row(
            payment_id, student_id, term, amount_due, amount_paid,
            payment_date, payment_method, status,
        )


def _generate(db_path, student_id=STUDENT_ID, start=START, end=END):
    result = generate_fee_statement(student_id, start, end, db_path=db_path)
    assert result["ok"], result
    return result


# =============================================================================
# TEST 1 -- payment-ID set equality
# =============================================================================
def test_payment_id_set_equality_reconciles(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    _seed_payment(db_path, payment_date="2026-01-15", amount_paid=60.0)
    gen = _generate(db_path)

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == RECONCILES
    assert set(result["ledger_payment_ids"]) == set(result["statement_payment_ids"])
    assert result["ledger_payment_ids"] == sorted(str(pid) for pid in gen["payment_ids"])


def test_missing_payment_id_on_statement_fails(db_path):
    """A payment that's on the live ledger but not on the (older,
    stored) statement text -- simulated here by generating a statement
    for just one payment, then editing the stored row directly to drop
    a line the ledger still has, proving reconciliation actually reads
    the STORED text rather than trusting the DB row wholesale."""
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)

    # Ledger now has a second payment the stored statement never saw.
    _seed_payment(db_path, payment_date="2026-01-20", amount_paid=25.0)

    # Directly mutate the STORED row's content (bypassing triggers via
    # a fresh raw connection with no application-level protection is
    # not possible -- triggers block it, so instead we build a second,
    # independently-verifiable scenario: reconcile the OLD version
    # against the NOW-CHANGED ledger. The old version's printed
    # payment-id set no longer equals the live ledger's set.
    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == FAILS
    assert "missing from statement" in result["explanation"]
    assert len(result["missing_payment_ids"]) == 1


def test_extra_payment_id_on_statement_fails(db_path, monkeypatch):
    """The reverse: the statement prints a payment_id the live ledger
    no longer has for this period -- simulated by generating a
    statement with two payments, then moving one payment's date
    outside the period (so the live ledger view for this period now
    legitimately excludes it), leaving the OLD stored statement
    printing an id the ledger no longer attributes to this period."""
    pid_a = _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    pid_b = _seed_payment(db_path, payment_date="2026-01-15", amount_paid=60.0)
    gen = _generate(db_path)
    assert set(gen["payment_ids"]) == {pid_a, pid_b}

    # Move payment B's date out of the period directly via the ledger
    # writer's own connection (a normal UPDATE on fees_payment, NOT on
    # statement_versions -- fees_payment has no immutability trigger).
    with SchoolDB(db_path) as db:
        db.conn.execute(
            "UPDATE fees_payment SET payment_date = ? WHERE payment_id = ?",
            ("2026-02-01", pid_b),
        )

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == FAILS
    assert "present on statement but not in ledger" in result["explanation"]
    assert str(pid_b) in result["extra_payment_ids"]


# =============================================================================
# TEST 2 -- printed total
# =============================================================================
def test_printed_total_consistent_reconciles(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=33.33)
    _seed_payment(db_path, payment_date="2026-01-15", amount_paid=66.67)
    gen = _generate(db_path)

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == RECONCILES
    assert result["printed_total"] == result["computed_total"] == "100.00"


def test_printed_total_tampered_fails(db_path):
    """Corrupts the STORED statement_content directly at the SQLite
    level (bypassing the immutability triggers via the trigger's own
    escape hatch is not possible -- so this test proves the total
    check works by writing a *fresh* row with a self-inconsistent total
    the normal generator would never produce, exercising exactly the
    printed-total independent-parse/sum path)."""
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)
    stored = get_version(db_path, STUDENT_ID, START, END, gen["version"])

    tampered_content = stored["statement_content"].replace(
        f"Total: {gen['total']}", "Total: 999999.99"
    )
    assert tampered_content != stored["statement_content"]

    # Insert a second, tampered version directly (this is allowed --
    # only UPDATE/DELETE of existing rows is blocked, not INSERT of a
    # new one) so reconciliation has a stored row whose printed total
    # disagrees with its own printed lines.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO statement_versions
               (student_id, period_start, period_end, version, fingerprint,
                statement_content, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (STUDENT_ID, START, END, gen["version"] + 1, stored["fingerprint"],
             tampered_content, stored["generated_at"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"] + 1, db_path=db_path)

    assert result["verdict"] == FAILS
    assert "does not equal the sum" in result["explanation"]


# =============================================================================
# TEST 3 -- unchanged regeneration: same version, no new row, reconciles
# =============================================================================
def test_unchanged_regeneration_same_version_reconciles(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    first = _generate(db_path)
    assert first["version"] == 1

    second = _generate(db_path)  # ledger unchanged
    assert second["version"] == 1
    assert second["sequence"] == first["sequence"]  # same row, not a new one

    result = reconcile_statement(STUDENT_ID, START, END, version=1, db_path=db_path)
    assert result["verdict"] == RECONCILES


# =============================================================================
# SECTION 16 -- ledger change: old version stays byte-identical, new
# version created, new version reconciles.
# =============================================================================
def test_ledger_change_creates_new_version_old_stays_reconciled(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    v1 = _generate(db_path)
    assert v1["version"] == 1
    v1_row_before = get_version(db_path, STUDENT_ID, START, END, 1)

    v1_result = reconcile_statement(STUDENT_ID, START, END, version=1, db_path=db_path)
    assert v1_result["verdict"] == RECONCILES

    # Change the ledger: a new relevant payment.
    _seed_payment(db_path, payment_date="2026-01-20", amount_paid=25.0)

    v2 = _generate(db_path)
    assert v2["version"] == 2

    v2_result = reconcile_statement(STUDENT_ID, START, END, version=2, db_path=db_path)
    assert v2_result["verdict"] == RECONCILES

    # Old version's row is byte-identical -- untouched by the new generation.
    v1_row_after = get_version(db_path, STUDENT_ID, START, END, 1)
    assert v1_row_after["statement_content"] == v1_row_before["statement_content"]
    assert v1_row_after["fingerprint"] == v1_row_before["fingerprint"]

    # Old version, reconciled again NOW (against the CHANGED ledger),
    # must FAIL -- it no longer represents the current ledger state.
    v1_after_change = reconcile_statement(STUDENT_ID, START, END, version=1, db_path=db_path)
    assert v1_after_change["verdict"] == FAILS


# =============================================================================
# SECTION 17 -- immutability: UPDATE/DELETE on statement_versions blocked
# =============================================================================
def test_statement_versions_update_blocked_by_trigger(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)
    before = get_version(db_path, STUDENT_ID, START, END, gen["version"])

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE statement_versions SET statement_content = ? WHERE version_id = ?",
                ("tampered", before["sequence"]),
            )
    finally:
        conn.close()

    after = get_version(db_path, STUDENT_ID, START, END, gen["version"])
    assert after["statement_content"] == before["statement_content"]


def test_statement_versions_delete_blocked_by_trigger(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)
    before = get_version(db_path, STUDENT_ID, START, END, gen["version"])

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM statement_versions WHERE version_id = ?",
                (before["sequence"],),
            )
    finally:
        conn.close()

    after = get_version(db_path, STUDENT_ID, START, END, gen["version"])
    assert after is not None
    assert after["statement_content"] == before["statement_content"]


# =============================================================================
# SECTION 18 -- zero-payment period reconciles
# =============================================================================
def test_zero_payment_period_reconciles(db_path):
    # No payments seeded at all for this student/period.
    gen = _generate(db_path)
    assert gen["line_count"] == 0
    assert gen["total"] == "0.00"

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == RECONCILES
    assert result["ledger_payment_ids"] == []
    assert result["statement_payment_ids"] == []
    assert result["printed_total"] == "0.00"


# =============================================================================
# SECTION 19 -- fingerprint verification
# =============================================================================
def test_fingerprint_unchanged_ledger_reconciles(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == RECONCILES
    assert result["recomputed_fingerprint"] == result["stored_fingerprint"] == gen["fingerprint"]


def test_fingerprint_changed_ledger_fails(db_path):
    pid = _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)

    # Change a column that affects the fingerprint but isn't part of
    # the payment-id set check (e.g. payment_method) -- directly on
    # the ledger, without regenerating.
    with SchoolDB(db_path) as db:
        db.conn.execute(
            "UPDATE fees_payment SET payment_method = ? WHERE payment_id = ?",
            ("bank_transfer", pid),
        )

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == FAILS
    assert result["recomputed_fingerprint"] != result["stored_fingerprint"]
    assert "Fingerprint mismatch" in result["explanation"]


# =============================================================================
# SECTION 20 -- independence: reconciliation must not depend on the
# generator being callable at all.
# =============================================================================
def test_reconciliation_survives_generator_being_broken(db_path, monkeypatch):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)

    def _boom(*args, **kwargs):
        raise AssertionError(
            "generate_fee_statement() must never be called during reconciliation"
        )

    monkeypatch.setattr(fee_statement, "generate_fee_statement", _boom)

    # reconcile_statement.py imported its own reference to nothing from
    # fee_statement -- it never imports generate_fee_statement at all.
    # This assertion documents that guarantee directly.
    import reconcile_statement as rs
    assert not hasattr(rs, "generate_fee_statement")

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)
    assert result["verdict"] == RECONCILES


# =============================================================================
# SECTION 6 -- conflicting duplicate ledger rows -> BLOCKED, not FAILS
# =============================================================================
def test_conflicting_duplicate_ledger_rows_blocked(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    gen = _generate(db_path)
    assert gen["status"] == "RECONCILED"

    # Introduce a conflicting duplicate directly on the ledger: two rows
    # sharing a payment_id but disagreeing on amount_paid.
    _seed_payment_with_id(
        db_path, payment_id=9001, payment_date="2026-01-12", amount_paid=10.0
    )
    _seed_payment_with_id(
        db_path, payment_id=9001, payment_date="2026-01-12", amount_paid=99.0
    )

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == BLOCKED
    assert result["verdict"] != FAILS


def test_blocked_explanation_names_conflicting_ids(db_path):
    _seed_payment_with_id(db_path, payment_id=42, payment_date="2026-01-12", amount_paid=10.0)
    _seed_payment_with_id(db_path, payment_id=42, payment_date="2026-01-12", amount_paid=99.0)
    _seed_payment(db_path, payment_date="2026-01-20", amount_paid=15.0)
    gen = _generate(db_path)  # generation itself also reports BLOCKED for this ledger
    assert gen["status"] == "BLOCKED"

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)

    assert result["verdict"] == BLOCKED
    assert "42" in result["explanation"]
    assert result["conflicting_payment_ids"] == ["42"]


def test_identical_duplicate_rows_do_not_block(db_path):
    """A ledger anomaly of EXACTLY identical duplicate rows (not
    conflicting) is deduplicated, not blocked -- matching generation's
    own behavior."""
    _seed_payment_with_id(db_path, payment_id=55, payment_date="2026-01-12", amount_paid=20.0)
    _seed_payment_with_id(db_path, payment_id=55, payment_date="2026-01-12", amount_paid=20.0)
    gen = _generate(db_path)
    assert gen["status"] == "RECONCILED"

    result = reconcile_statement(STUDENT_ID, START, END, version=gen["version"], db_path=db_path)
    assert result["verdict"] == RECONCILES


# =============================================================================
# Additional: not-found / malformed-period error handling
# =============================================================================
def test_nonexistent_version_raises_statement_not_found(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    _generate(db_path)

    with pytest.raises(StatementNotFoundError):
        reconcile_statement(STUDENT_ID, START, END, version=7, db_path=db_path)


def test_no_stored_versions_raises_statement_not_found(db_path):
    with pytest.raises(StatementNotFoundError):
        reconcile_statement(STUDENT_ID, START, END, db_path=db_path)


def test_inverted_period_raises_period_format_error(db_path):
    with pytest.raises(PeriodFormatError):
        reconcile_statement(STUDENT_ID, END, START, db_path=db_path)


def test_version_omitted_reconciles_latest(db_path):
    _seed_payment(db_path, payment_date="2026-01-05", amount_paid=40.0)
    v1 = _generate(db_path)
    _seed_payment(db_path, payment_date="2026-01-20", amount_paid=10.0)
    v2 = _generate(db_path)
    assert v2["version"] == 2

    result = reconcile_statement(STUDENT_ID, START, END, db_path=db_path)  # version omitted
    assert result["version"] == 2
    assert result["verdict"] == RECONCILES
