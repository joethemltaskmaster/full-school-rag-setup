"""
agents/test_statement_cli.py

STMT-004 acceptance tests for statement_cli.py.

Dedicated test file rather than appending to test_fee_statement.py --
explicitly permitted by the STMT-004 spec ("create a dedicated test
file if that is cleaner"), and keeps CLI-layer concerns (argv parsing,
stdout/stderr capture, --force, file-write safety) separate from
STMT-001/002/003's statement-generation/versioning tests.

Test data is seeded directly via statement_store.get_or_create_version()
rather than through fee_statement.generate_fee_statement() -- this
exercises the REAL storage/versioning code path (not hand-written SQL),
while keeping these tests focused purely on retrieval: statement_cli.py
must never generate anything, so nothing here should depend on how
content is generated, only on what's already stored.

Acceptance criteria -> test map:
    Test 1  Latest version              -> test_latest_version_omitted_version_flag
    Test 2  Specific version             -> test_specific_version_survives_later_versions
    Test 3  Terminal confirmation only   -> test_terminal_output_contains_only_confirmation
    Test 4  Non-existent version         -> test_nonexistent_version_reports_available_range
    Test 5  No stored versions           -> test_no_stored_versions_produces_clear_error
    Additional  Existing output file     -> test_existing_output_refused_without_force
                                             test_existing_output_overwritten_with_force
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from statement_cli import run  # noqa: E402
from statement_store import get_or_create_version, get_version  # noqa: E402

STUDENT_ID = 1
START = "2026-01-01"
END = "2026-01-31"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """statement_store.get_or_create_version() creates statement_versions
    itself (ensure_table=True on the writer path) -- no schema setup
    needed here beyond a fresh sqlite file."""
    return str(tmp_path / "school.db")


def _seed_version(db_path: str, body_text: str) -> dict:
    """Creates the next version for STUDENT_ID/START/END using the real
    writer, with a distinct fingerprint each call (so it always creates
    rather than reusing) and a trivial finalize_content that embeds the
    version number -- enough for these CLI tests to tell versions apart
    without depending on fee_statement.py's actual header format."""
    fingerprint = f"fp-{body_text}"  # distinct content -> distinct fingerprint -> new version
    return get_or_create_version(
        db_path, STUDENT_ID, START, END, fingerprint, body_text,
        finalize_content=lambda version, generated_at: (
            f"Version: {version}\nGenerated: {generated_at}\n\n{body_text}\n"
        ),
    )


# ---- Test 1: latest version, --version omitted -----------------------------
def test_latest_version_omitted_version_flag(db_path, tmp_path, capsys):
    _seed_version(db_path, "first body")
    _seed_version(db_path, "second body")
    third = _seed_version(db_path, "third body")
    assert third["version"] == 3

    out_path = tmp_path / "out.txt"
    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code == 0
    assert out_path.exists()
    assert out_path.read_text() == third["statement_content"]
    assert "third body" in out_path.read_text()

    captured = capsys.readouterr()
    assert "third body" not in captured.out
    assert "version 3" in captured.out.lower()
    assert str(out_path) in captured.out


# ---- Test 2: specific version survives later versions being created --------
def test_specific_version_survives_later_versions(db_path, tmp_path):
    v1 = _seed_version(db_path, "original body")
    assert v1["version"] == 1
    _seed_version(db_path, "second body")
    _seed_version(db_path, "third body")  # later versions now exist

    out_path = tmp_path / "v1.txt"
    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--version", "1", "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code == 0
    content = out_path.read_text()
    assert content == v1["statement_content"]
    assert "original body" in content
    assert "second body" not in content
    assert "third body" not in content

    # Re-requesting version 1 again must still be byte-identical -- proves
    # retrieval, not regeneration.
    out_path_2 = tmp_path / "v1_again.txt"
    run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--version", "1", "--output", str(out_path_2), "--db-path", db_path,
    ])
    assert out_path_2.read_bytes() == out_path.read_bytes()

    # Also confirm the DB row itself was never touched by any of this.
    stored_v1 = get_version(db_path, STUDENT_ID, START, END, 1)
    assert stored_v1["statement_content"] == v1["statement_content"]


# ---- Test 3: terminal shows confirmation only, never statement content -----
def test_terminal_output_contains_only_confirmation(db_path, tmp_path, capsys):
    seeded = _seed_version(db_path, "super secret payment details 12345")
    out_path = tmp_path / "out.txt"

    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "super secret payment details 12345" not in captured.out
    assert "super secret payment details 12345" not in captured.err
    assert str(out_path) in captured.out
    assert f"version {seeded['version']}" in captured.out.lower()
    # The confirmation is short -- not the whole rendered statement.
    assert len(captured.out.strip().splitlines()) == 1


# ---- Test 4: non-existent version -> clear error, available range, no file -
def test_nonexistent_version_reports_available_range(db_path, tmp_path, capsys):
    _seed_version(db_path, "v1")
    _seed_version(db_path, "v2")
    _seed_version(db_path, "v3")

    out_path = tmp_path / "out.txt"
    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--version", "7", "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code != 0
    assert not out_path.exists()

    captured = capsys.readouterr()
    assert "Version 7 does not exist" in captured.err
    assert "1-3" in captured.err
    assert captured.out == ""  # nothing on stdout for a failed request


# ---- Test 5: no stored versions at all -> clear error, no file -------------
def test_no_stored_versions_produces_clear_error(db_path, tmp_path, capsys):
    out_path = tmp_path / "out.txt"
    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code != 0
    assert not out_path.exists()

    captured = capsys.readouterr()
    assert "generate the statement first" in captured.err.lower()
    assert captured.out == ""


def test_no_stored_versions_specific_version_requested_still_clear_error(db_path, tmp_path, capsys):
    """Requesting a specific version on a student+period that has never
    been generated at all is still 'no stored versions', not 'version N
    does not exist' (there's no range to report)."""
    out_path = tmp_path / "out.txt"
    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--version", "1", "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code != 0
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "generate the statement first" in captured.err.lower()


# ---- Additional: existing output file safety -------------------------------
def test_existing_output_refused_without_force(db_path, tmp_path, capsys):
    _seed_version(db_path, "the real content")
    out_path = tmp_path / "out.txt"
    out_path.write_text("pre-existing content that must survive")

    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--output", str(out_path), "--db-path", db_path,
    ])

    assert exit_code != 0
    assert out_path.read_text() == "pre-existing content that must survive"  # untouched
    captured = capsys.readouterr()
    assert "already exists" in captured.err.lower()


def test_existing_output_overwritten_with_force(db_path, tmp_path):
    seeded = _seed_version(db_path, "the real content")
    out_path = tmp_path / "out.txt"
    out_path.write_text("stale content")

    exit_code = run([
        "--student", str(STUDENT_ID), "--start", START, "--end", END,
        "--output", str(out_path), "--db-path", db_path, "--force",
    ])

    assert exit_code == 0
    assert out_path.read_text() == seeded["statement_content"]

    # The stored DB version itself is never touched by --force -- only
    # the destination FILE gets overwritten.
    stored = get_version(db_path, STUDENT_ID, START, END, seeded["version"])
    assert stored["statement_content"] == seeded["statement_content"]