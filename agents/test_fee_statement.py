"""
test_fee_statement.py

Tests the STMT-001 acceptance criteria against fee_statement.py directly
(not through data_agent/orchestrator, so error strings and result shapes
are asserted exactly as the contract specifies them, not as re-wrapped
by a caller).

Uses a throwaway sqlite file per test with a minimal `fees_payment`
table -- no PRIMARY KEY constraint on payment_id, deliberately, so
Test 6 can simulate a dirty ledger with duplicate payment_ids (a real
schema would normally enforce uniqueness; the statement generator has
to be correct regardless, since STMT-001 explicitly requires it to
handle that case).

Acceptance criteria -> test map:
    1. Completeness + uniqueness  -> test_normal_statement, test_boundaries_inclusive
    2. Traceability (payment_id)  -> test_normal_statement
    3. Total correctness          -> test_normal_statement, test_conflicting_duplicates_blocks
    4. No payments                -> test_no_payments
    5. Invalid range               -> test_invalid_range_no_file
    (contract edge cases)          -> test_malformed_date_is_unplaceable,
                                       test_fake_calendar_date_is_unplaceable,
                                       test_identical_duplicates_reconciled,
                                       test_conflicting_duplicates_blocks
"""

from __future__ import annotations

import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fee_statement import generate_fee_statement, get_statement_version  # noqa: E402


# Real operational student_ids are plain integers (see db_service.py /
# data_agent.py -- students.student_id, fees_payment.student_id). "STU001"
# in the sprint brief is a human-readable label, not the literal DB value;
# tests use STUDENT_ID = 1 as that student.
STUDENT_ID = 1


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "school.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE fees_payment (
            payment_id    INTEGER,
            student_id    INTEGER,
            term          TEXT,
            amount_due    REAL,
            amount_paid   REAL,
            payment_date  TEXT,
            payment_method TEXT,
            status        TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return str(path)


def _insert(db_path: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """INSERT INTO fees_payment
           (payment_id, student_id, term, amount_due, amount_paid, payment_date, payment_method, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def statements_dir(tmp_path: Path) -> Path:
    d = tmp_path / "statements"
    return d


# =========================================================================
# Test 1 -- Normal
# =========================================================================
def test_normal_statement(db_path, statements_dir):
    _insert(db_path, [
        (101, STUDENT_ID, "Term1", 50000.0, 20000.0, "2025-09-05", "bank_transfer", "partial"),
        (102, STUDENT_ID, "Term1", 50000.0, 15000.5, "2025-09-12", "cash", "partial"),
        (103, STUDENT_ID, "Term1", 50000.0, 14999.5, "2025-09-20", "cash", "partial"),
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["status"] == "RECONCILED"
    assert result["line_count"] == 3
    assert result["total"] == "50000.00"  # 20000.00 + 15000.50 + 14999.50

    file_path = Path(result["file_path"])
    assert file_path.exists()
    content = file_path.read_text()
    for pid in (101, 102, 103):
        assert f"payment_id={pid}" in content
    assert "payment_date=2025-09-05" in content
    assert "Total: 50000.00" in content
    assert "Status: RECONCILED" in content


# =========================================================================
# Test 2 -- Boundaries
# =========================================================================
def test_boundaries_inclusive(db_path, statements_dir):
    _insert(db_path, [
        (201, STUDENT_ID, "Term1", 10000.0, 10000.0, "2025-09-01", "cash", "paid"),  # == start
        (202, STUDENT_ID, "Term1", 10000.0, 5000.0, "2025-09-30", "cash", "partial"),  # == end
        (203, STUDENT_ID, "Term1", 10000.0, 1000.0, "2025-08-31", "cash", "partial"),  # just before start
        (204, STUDENT_ID, "Term1", 10000.0, 1000.0, "2025-10-01", "cash", "partial"),  # just after end
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["line_count"] == 2
    assert result["total"] == "15000.00"
    content = Path(result["file_path"]).read_text()
    assert "payment_id=201" in content
    assert "payment_id=202" in content
    assert "payment_id=203" not in content
    assert "payment_id=204" not in content


# =========================================================================
# Test 3 -- No payments
# =========================================================================
def test_no_payments(db_path, statements_dir):
    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["status"] == "RECONCILED"
    assert result["line_count"] == 0
    assert result["total"] == "0.00"

    content = Path(result["file_path"]).read_text()
    assert "No payments in this period." in content
    assert "Total: 0.00" in content


# =========================================================================
# Test 4 -- Invalid range
# =========================================================================
def test_invalid_range_no_file(db_path, statements_dir):
    result = generate_fee_statement(STUDENT_ID, "2025-09-30", "2025-09-01",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is False
    assert result["error"] == "Start date is after end date"
    assert "file_path" not in result
    # no file at all should have been written for this request
    assert not statements_dir.exists() or not any(statements_dir.iterdir())


# =========================================================================
# Test 5 -- Malformed date
# =========================================================================
def test_malformed_date_is_unplaceable(db_path, statements_dir):
    _insert(db_path, [
        (301, STUDENT_ID, "Term1", 10000.0, 10000.0, "2025-09-15", "cash", "paid"),
        (302, STUDENT_ID, "Term1", 5000.0, 5000.0, "09/15/2025", "cash", "paid"),  # malformed
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["line_count"] == 1
    assert result["unplaceable_count"] == 1
    assert result["total"] == "10000.00"  # malformed row excluded from total

    content = Path(result["file_path"]).read_text()
    assert "payment_id=301" in content
    assert "payment_id=302" not in content.split("Unplaceable")[0]  # not in the lines section
    assert "Unplaceable payments" in content
    assert "payment_id=302" in content  # but surfaced, not silently dropped
    assert "09/15/2025" in content


# =========================================================================
# Test 5b -- Fake calendar date (right shape, not a real date)
# =========================================================================
def test_fake_calendar_date_is_unplaceable(db_path, statements_dir):
    # '2025-02-30' has the exact YYYY-MM-DD shape a naive SQL GLOB check
    # would accept -- but February never has 30 days, so no real calendar
    # can place it. This used to slip past the DB layer's old date filter
    # entirely (it matched the ISO-shape GLOB, so it wasn't routed to the
    # "malformed" branch, but it also failed a lexicographic BETWEEN
    # against a real range, so it matched neither OR-branch and was
    # silently dropped before fee_statement.py ever saw the row). Now
    # get_fees_payment_in_range() returns every row unfiltered, so this
    # payment must reach the statement generator and be caught there.
    _insert(db_path, [
        (701, STUDENT_ID, "Term1", 10000.0, 10000.0, "2025-09-15", "cash", "paid"),
        (702, STUDENT_ID, "Term1", 5000.0, 5000.0, "2025-02-30", "cash", "paid"),  # fake date
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["line_count"] == 1
    assert result["unplaceable_count"] == 1
    assert result["total"] == "10000.00"  # fake-date row excluded from total

    content = Path(result["file_path"]).read_text()
    assert "payment_id=701" in content
    assert "payment_id=702" not in content.split("Unplaceable")[0]  # not in the lines section
    assert "Unplaceable payments" in content
    assert "payment_id=702" in content  # surfaced, not silently dropped
    assert "2025-02-30" in content


# =========================================================================
# Test 6 -- Duplicate payment_id (identical + conflicting)
# =========================================================================
def test_identical_duplicates_reconciled(db_path, statements_dir):
    row = (401, STUDENT_ID, "Term1", 10000.0, 10000.0, "2025-09-10", "cash", "paid")
    _insert(db_path, [row, row])  # byte-for-byte identical duplicate

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["status"] == "RECONCILED"
    assert result["line_count"] == 1  # deduplicated to one line
    assert result["total"] == "10000.00"
    assert result["identical_duplicate_ids"] == [401]

    content = Path(result["file_path"]).read_text()
    assert "Ledger anomaly" in content
    assert "401" in content


def test_conflicting_duplicates_blocks(db_path, statements_dir):
    _insert(db_path, [
        (501, STUDENT_ID, "Term1", 10000.0, 10000.0, "2025-09-10", "cash", "paid"),
        (501, STUDENT_ID, "Term1", 10000.0, 9500.0, "2025-09-10", "cash", "partial"),  # same id, amount disagrees
        (502, STUDENT_ID, "Term1", 5000.0, 5000.0, "2025-09-15", "cash", "paid"),  # unaffected, includable
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True  # BLOCKED is a valid, written statement -- not an error
    assert result["status"] == "BLOCKED"
    assert result["conflicting_ids"] == [501]
    assert result["line_count"] == 1  # only 502 is includable
    assert result["total"] == "5000.00"  # subtotal from printed lines only, never a guessed winner for 501

    content = Path(result["file_path"]).read_text()
    assert "Status: BLOCKED" in content
    assert "CONFLICTING duplicates" in content
    assert "payment_id=501" not in content.split("CONFLICTING")[0]  # excluded from the lines section
    assert "Subtotal (BLOCKED" in content


# =========================================================================
# Decimal rounding pipeline sanity check
# =========================================================================
def test_rounding_uses_decimal_half_up_not_float_round(db_path, statements_dir):
    # 10.005 as a float is actually ~10.00499999999999989..., so a naive
    # Decimal(float_value) would round DOWN to 10.00. The contract
    # pipeline (str(float) first) must round HALF_UP to 10.01 instead.
    _insert(db_path, [
        (601, STUDENT_ID, "Term1", 100.0, 10.005, "2025-09-10", "cash", "partial"),
    ])

    result = generate_fee_statement(STUDENT_ID, "2025-09-01", "2025-09-30",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["total"] == "10.01"
    assert Decimal(result["total"]) == Decimal("10.01")


# =========================================================================
# STMT-003 -- statement versioning acceptance tests
# =========================================================================

def test_stmt003_first_statement_is_version_1(db_path, statements_dir):
    _insert(db_path, [
        (901, STUDENT_ID, "Term1", 10000.0, 10000.0, "2026-01-10", "cash", "paid"),
    ])

    result = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["ok"] is True
    assert result["version"] == 1


def test_stmt003_identical_request_returns_same_version_and_byte_identical_content(
    db_path, statements_dir
):
    _insert(db_path, [
        (902, STUDENT_ID, "Term1", 10000.0, 10000.0, "2026-01-10", "cash", "paid"),
    ])

    first = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                    db_path=db_path, output_dir=statements_dir)
    second = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                     db_path=db_path, output_dir=statements_dir)

    assert first["version"] == 1
    assert second["version"] == 1

    first_content = Path(first["file_path"]).read_text()
    second_content = Path(second["file_path"]).read_text()
    assert first_content == second_content


def test_stmt003_change_outside_period_does_not_bump_version(db_path, statements_dir):
    _insert(db_path, [
        (903, STUDENT_ID, "Term1", 10000.0, 10000.0, "2026-01-10", "cash", "paid"),
    ])

    generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                            db_path=db_path, output_dir=statements_dir)

    # A February transaction -- outside the requested January period --
    # must not affect the January statement's version.
    _insert(db_path, [
        (904, STUDENT_ID, "Term1", 5000.0, 5000.0, "2026-02-05", "cash", "paid"),
    ])

    result = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                     db_path=db_path, output_dir=statements_dir)

    assert result["version"] == 1


def test_stmt003_change_inside_period_bumps_version_and_preserves_v1(
    db_path, statements_dir
):
    _insert(db_path, [
        (905, STUDENT_ID, "Term1", 10000.0, 10000.0, "2026-01-10", "cash", "paid"),
    ])

    first = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                    db_path=db_path, output_dir=statements_dir)
    assert first["version"] == 1
    v1_content = Path(first["file_path"]).read_text()

    # A January transaction -- inside the requested period -- materially
    # changes the relevant ledger state.
    _insert(db_path, [
        (906, STUDENT_ID, "Term1", 5000.0, 5000.0, "2026-01-20", "cash", "paid"),
    ])

    second = generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                                     db_path=db_path, output_dir=statements_dir)
    assert second["version"] == 2

    stored_v1 = get_statement_version(STUDENT_ID, "2026-01-01", "2026-01-31", 1,
                                       db_path=db_path)
    assert stored_v1["ok"] is True
    assert stored_v1["content"] == v1_content  # version 1 untouched by v2's creation


def test_stmt003_nonexistent_version_returns_exact_error(db_path, statements_dir):
    _insert(db_path, [
        (907, STUDENT_ID, "Term1", 10000.0, 10000.0, "2026-01-10", "cash", "paid"),
    ])
    generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                            db_path=db_path, output_dir=statements_dir)  # version 1

    _insert(db_path, [
        (908, STUDENT_ID, "Term1", 5000.0, 5000.0, "2026-01-22", "cash", "paid"),
    ])
    generate_fee_statement(STUDENT_ID, "2026-01-01", "2026-01-31",
                            db_path=db_path, output_dir=statements_dir)  # version 2

    result = get_statement_version(STUDENT_ID, "2026-01-01", "2026-01-31", 3,
                                    db_path=db_path)

    assert result["ok"] is False
    assert result["error"] == "Version 3 has not been issued for this statement."