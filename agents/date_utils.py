"""
agents/date_utils.py

Single source of truth for strict-ISO (YYYY-MM-DD) date validation.

Both fee_statement.py (period validation, payment-date placement) and
statement_store.py (period validation for version persistence/lookup)
need exactly this check. Living in one shared module means there is
one implementation to get right, instead of two copies that could
silently drift apart.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_strict_iso_date(value: Any) -> bool:
    """True only for a real calendar date in exactly YYYY-MM-DD form --
    no other separators, no two-digit years, no fuzzy/lenient parsing,
    and rejects shapes that pass the regex but aren't real dates
    (e.g. 2025-02-30)."""
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        return False
    try:
        year, month, day = (int(part) for part in value.split("-"))
        date(year, month, day)
        return True
    except ValueError:
        return False