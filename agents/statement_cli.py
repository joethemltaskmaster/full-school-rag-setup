"""
agents/statement_cli.py

STMT-004 -- command-line retrieval of an already-generated fee statement
version. This module does exactly one thing: turn a stored
statement_versions row into an output file, unmodified.

    User
      -> statement_cli.py       (this file: argument parsing/validation)
      -> statement_store.py     (get_latest_version / get_version / list_versions)
      -> statement_versions row (already stored -- STMT-003's job, not this file's)
      -> write EXACT stored content to the requested output path
      -> print a one-line confirmation only

This is retrieval, not generation:
    - Never calls fee_statement.generate_fee_statement() or anything
      that computes a fingerprint or decides a version number.
    - Never inserts into statement_versions.
    - Never reformats, re-encodes, or otherwise touches the stored
      content string before writing it -- what's in the DB is exactly
      what lands in the output file.
    - Never prints statement content (lines, totals, payment ids,
      generated_at) to the terminal -- only a short confirmation
      naming the output file and version.

Student identifier: fees_payment.student_id and statement_versions.
student_id are both declared INTEGER in database/schema.py, and every
fee-statement code path inspected this session (statement_store.py,
the existing test suite) uses a plain int student_id -- unlike the
STU-xxxxx "reference student id" concept used elsewhere in this project
for a *different* subsystem (the synthetic risk-prediction dataset),
which has no bearing on fee statements. --student is therefore parsed
as int here, matching what the existing fee-statement schema actually
requires, not as a blanket assumption.

Usage:
    python agents/statement_cli.py --student 1 --start 2026-01-01 \\
        --end 2026-01-31 --output statement.txt

    python agents/statement_cli.py --student 1 --start 2026-01-01 \\
        --end 2026-01-31 --version 2 --output statement.txt --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Matches this project's established convention for a script living
# inside agents/ that needs to import a sibling module (see
# test_fee_statement.py's identical sys.path.insert line).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from statement_store import (  # noqa: E402
    PeriodFormatError,
    get_latest_version,
    get_version,
    list_versions,
)


def _format_available_versions(versions: list[int]) -> str:
    """[1, 2, 3] -> '1-3'; [1] -> '1'. The versioning algorithm always
    increments by exactly 1, so stored versions for a given student +
    period are guaranteed contiguous -- min/max fully describes the set."""
    if len(versions) == 1:
        return str(versions[0])
    return f"{min(versions)}-{max(versions)}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="statement_cli.py",
        description="Retrieve an already-generated fee statement version and write its "
                     "exact stored content to a file. Does not generate new statements.",
    )
    parser.add_argument("--student", type=int, required=True, help="Student id (integer).")
    parser.add_argument("--start", required=True, help="Statement period start, YYYY-MM-DD (inclusive).")
    parser.add_argument("--end", required=True, help="Statement period end, YYYY-MM-DD (inclusive).")
    parser.add_argument(
        "--version", type=int, default=None,
        help="Specific version to retrieve. Omit for the latest stored version.",
    )
    parser.add_argument("--output", required=True, help="Destination file path.")
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite --output if it already exists. Without this flag, an existing "
             "output file is left untouched and the command fails.",
    )
    parser.add_argument(
        "--db-path", default="school.db",
        help="Path to school.db. Defaults to 'school.db' in the current directory.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    """
    Returns a process exit code (0 success, 1 failure) rather than
    calling sys.exit() directly, so tests can call this in-process and
    assert on the return value without needing a subprocess.
    """
    args = _parse_args(argv)

    try:
        if args.version is None:
            row = get_latest_version(args.db_path, args.student, args.start, args.end)
        else:
            row = get_version(args.db_path, args.student, args.start, args.end, args.version)
    except PeriodFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if row is None:
        if args.version is None:
            # No stored versions at all for this student + period.
            print(
                "No stored versions exist for this statement. Generate the statement first.",
                file=sys.stderr,
            )
            return 1

        # A specific version was requested but doesn't exist -- distinguish
        # "nothing has ever been stored" from "some versions exist, just
        # not this one" so the error actually helps the user.
        available = list_versions(args.db_path, args.student, args.start, args.end)
        if not available:
            print(
                "No stored versions exist for this statement. Generate the statement first.",
                file=sys.stderr,
            )
            return 1

        print(
            f"Version {args.version} does not exist. "
            f"Available versions: {_format_available_versions(available)}.",
            file=sys.stderr,
        )
        return 1

    output_path = Path(args.output)
    if output_path.exists() and not args.force:
        print(f"Output file already exists: {output_path}", file=sys.stderr)
        return 1

    # Exact content, exact bytes: no reformatting, no added header, no
    # newline translation on write (newline="" disables Python's
    # universal-newline rewriting so whatever line endings are already
    # in the stored string are preserved verbatim on disk).
    output_path.write_text(row["statement_content"], encoding="utf-8", newline="")

    print(f"Statement version {row['version']} written to {output_path}.")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()