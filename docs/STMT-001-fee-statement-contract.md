# Reconcilable Fee Statement — Contract

**Status:** Proposed contract (v1.0) — **NOT YET IN FORCE.** Awaiting sign-off from Rosa (ledger semantics) and Naledi (guardian/accountant usability). No later task may implement against this document until Section 7 shows both reviewers signed off; until then this is a draft under review, not the agreed reference.
**Owner:** Joseph Akpe Unimke (Joseph Siakpe)
**Applies to:** All later fee-statement generation, reconciliation, and reporting tasks in this sprint
**Supersedes:** No prior version. This is the canonical reference; later tasks point here rather than re-deriving these rules.

---

## 0. Grounding — the data this contract is written against

This contract is written against the actual operational schema, not an idealized one:

- `fee_payments` is the only operational fee table. Its columns are: `payment_id`, `student_id`, `term`, `amount_due`, `amount_paid`, `payment_date` (TEXT, no format constraint at the database level), `payment_method`, `status`.
- The CSV that feeds this table uses ISO dates (`YYYY-MM-DD`, e.g. `2025-09-15`).
- `payment_date` has no validation anywhere upstream — nothing stops a differently formatted or malformed string from entering the table. This contract exists specifically to decide what happens when that occurs.
- `amount_paid` is stored as a REAL (float). Floating-point money is a known trap (e.g. `0.1 + 0.2 ≠ 0.3` in floating-point arithmetic), so the reconciliation definition below states the exact rounding and comparison rule rather than leaving it implicit.
- The operational tables are currently commented out of the default load order, but the CSV exists and the service-layer readers are present. The data is available whenever a later task needs it.

Out of scope for this contract: statement versioning, idempotent regeneration, and stored historical versions. Those belong to a separate ticket. This document defines one thing only — what a correct statement and a correct reconciliation look like for a single generation run.

---

## 1. Period Boundary Definition

- The date field used for period membership is **`payment_date`**, and only `payment_date`. No other timestamp (e.g. row-creation time) is used.
- The period is defined by a **start date and an end date, both inclusive**. A payment dated exactly on the start date is included. A payment dated exactly on the end date is included.
- Comparison is **date-only** — there is no time-of-day or timezone component to `payment_date`, so none is introduced by this contract. `payment_date == start` or `payment_date == end` is a plain string/date equality check once the date has been validated per Section 2, not a range or timestamp comparison.
- Timezone cutoffs (e.g. "before midnight in what timezone") are explicitly out of scope, because `payment_date` carries no timezone information to reason about.

## 2. Text Date Interpretation

- The only date format a `payment_date` value is placed into a period from is **strict ISO `YYYY-MM-DD`**.
- Any value that is **missing (NULL/empty), malformed, or in any other format** (e.g. `31-12-2025`, `12/31/2025`, a blank string) is:
  - **excluded** from every period statement, and
  - **surfaced explicitly as "unplaceable"** in the generation output (e.g. an "unplaceable payments" list alongside the statement) — never silently dropped.
- No lenient or fuzzy parsing is implemented anywhere in this pipeline. A date is either strict ISO, or it is unplaceable. This is a deliberate choice: lenient parsing is the exact ambiguity this contract exists to remove, and adding a parser that guesses formats would reintroduce it.

## 3. Statement Line Shape

Each statement line represents exactly one payment. The fields, in order:

| Field | Mandatory? | Source | Notes |
|---|---|---|---|
| `payment_id` | **Yes** | `fee_payments.payment_id` | The traceability key. Every line traces to exactly one payment by this id, and a given `payment_id` appears **at most once** on a statement. |
| `amount_paid` | **Yes** | `fee_payments.amount_paid` | The monetary amount. Rendered at 2 decimal places (see Section 4 for the rounding rule). |
| `payment_date` | No | `fee_payments.payment_date` | Included for readability; already validated as strict ISO by definition, since unplaceable payments never reach the line-generation step. |
| `payment_method` | No | `fee_payments.payment_method` | Passed through as-is. |
| `status` | No | `fee_payments.status` | Passed through as-is. |

No other fields are added. This is deliberately minimal — a line is auditable back to a single row in `fee_payments` and nothing more.

## 4. Reconciliation Definition

A statement is **reconciled** for a given period if and only if all three of the following hold at generation time:

1. **Set equality:** the set of `payment_id` values on the statement equals the set of `payment_id` values in the ledger (`fee_payments`) that fall inside the period per Sections 1–2, and each id appears **exactly once** on both sides.

   **Duplicate `payment_id` rule.** `fee_payments` has no `updated_at`, no version column, and no other field that reliably indicates which of two rows sharing a `payment_id` is authoritative — so no field-based "winner" can be chosen without guessing. The rule is therefore:
   - If two or more rows share a `payment_id` and are **identical across every other field** (`amount_paid`, `payment_date`, `payment_method`, `status`), they are treated as one payment: deduplicated to a single line, and the duplication is flagged in the generation output as a ledger anomaly.
   - If two or more rows share a `payment_id` and **disagree on any field**, no row is selected as the winner. The `payment_id` is **excluded from the statement entirely** (on the same footing as an unplaceable date, Section 2) and is surfaced in the output as a **conflicting duplicate**, listing every disagreeing row verbatim. The statement cannot be certified reconciled while a conflicting duplicate exists in its period — it must be resolved in the source ledger and the statement regenerated.
   - This is a deliberate no-guess rule: silently picking "the higher amount" or "the later row by insertion order" would let a data-entry error quietly determine a family's fee record. Exclusion-and-flag forces a human to resolve it instead.

2. **Total correctness:** the printed statement total equals the sum of the printed line amounts — not a separately computed database sum. This ties the "total" a reader sees directly to the lines they can audit.
3. **Rounding rule — exact REAL-to-decimal mechanism.** `amount_paid` is stored as a SQLite REAL, i.e. an IEEE-754 double, which cannot represent most decimal fractions exactly. To remove ambiguity about how a decimal monetary value is obtained from that stored float, every implementation must follow this exact pipeline and no other:
   1. Read the stored REAL value.
   2. Convert it to its **shortest round-trip decimal string** using the runtime's standard float-to-string conversion (e.g. Python's `repr()`/`str()` on a float, which by specification produces the shortest decimal string that parses back to the identical float). This step, not the next one, is what fixes the "which decimal did this float actually mean" question — it recovers the decimal value that was originally intended when the float was written, rather than an arbitrary nearby decimal.
   3. Parse that string into an arbitrary-precision decimal type (e.g. Python `decimal.Decimal(str(value))`) — never construct the decimal from the float object directly, since that reintroduces binary floating-point error.
   4. Quantize the resulting decimal to exactly 2 places using **ROUND_HALF_UP** (e.g. `Decimal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)`).
   5. Render that quantized decimal as the printed amount.
   - Native float rounding (e.g. calling a language's built-in `round(value, 2)` on the float itself) is **prohibited** anywhere in this pipeline — IEEE-754 double rounding does not reliably match ROUND_HALF_UP decimal rounding and would silently reintroduce the ambiguity this rule exists to remove.
   - The reconciliation test (both the set-equality check and the total-equals-sum-of-lines check) is run **on these printed, quantized decimal values** — not on the raw floats and not on any intermediate float rounding. This is what makes the test immune to float artifacts like `0.1 + 0.2 ≠ 0.3`: the contract never asks whether the raw floats sum correctly, only whether the values produced by this exact pipeline do.

---

## 5. Worked Examples

### 5.1 Normal example

**Period:** First Term, `2025-09-01` to `2025-12-31` (inclusive).

**Ledger rows for student 1:**

| payment_id | payment_date | amount_paid | payment_method | status |
|---|---|---|---|---|
| 1 | 2025-10-31 | 185000.00 | cash | partial |
| 2 | 2025-12-02 | 315000.00 | cash | paid |

Both dates are strict ISO and fall inside `[2025-09-01, 2025-12-31]`.

**Resulting statement:**

| payment_id | amount_paid | payment_date | payment_method | status |
|---|---|---|---|---|
| 1 | 185000.00 | 2025-10-31 | cash | partial |
| 2 | 315000.00 | 2025-12-02 | cash | paid |

**Printed total:** 500000.00

**Reconciliation check:** statement ids `{1, 2}` = ledger ids in period `{1, 2}`, each once. Printed total 500000.00 = 185000.00 + 315000.00. ✅ Reconciled.

### 5.2 Edge-case example (boundary date + malformed date + duplicate id)

**Period:** First Term, `2025-09-01` to `2025-12-31` (inclusive).

**Ledger rows:**

| payment_id | payment_date | amount_paid | payment_method | status | Note |
|---|---|---|---|---|---|
| 30 | 2025-12-31 | 50000.00 | pos | partial | exactly on the end boundary |
| 31 | 12/31/2025 | 75000.00 | card | partial | non-ISO format |
| 30 | 2025-12-31 | 50000.00 | pos | partial | duplicate export of payment_id 30 |

**Processing:**

- `payment_id 30` (date `2025-12-31`, on the boundary): **included**, per Section 1 the end date is inclusive. It appears twice in the raw export as **identical rows** (same amount, date, method, status); per Section 4's duplicate rule this is the non-conflicting case, so it is deduplicated to one line and the duplicate is flagged as a ledger anomaly rather than treated as a conflict.
- `payment_id 31` (date `12/31/2025`): **excluded** from the statement, per Section 2 — non-ISO format is not parsed. It is surfaced in the "unplaceable payments" output as unplaceable, not silently dropped.

**Resulting statement:**

| payment_id | amount_paid | payment_date | payment_method | status |
|---|---|---|---|---|
| 30 | 50000.00 | 2025-12-31 | pos | partial |

**Printed total:** 50000.00

**Flags surfaced alongside the statement:**
- `payment_id 30`: duplicate row detected in source ledger; deduplicated to one line.
- `payment_id 31`: unplaceable — `payment_date` value `12/31/2025` is not strict ISO `YYYY-MM-DD`; excluded from period.

**Reconciliation check:** statement ids `{30}` (deduplicated) = ledger ids validly in period `{30}` (31 excluded as unplaceable, per Section 2 it is not a "ledger id in period" at all). Printed total 50000.00 = 50000.00. ✅ Reconciled, with two flags surfaced for review.

### 5.3 Edge-case example (conflicting duplicate `payment_id`)

**Period:** First Term, `2025-09-01` to `2025-12-31` (inclusive).

**Ledger rows:**

| payment_id | payment_date | amount_paid | payment_method | status | Note |
|---|---|---|---|---|---|
| 40 | 2025-11-10 | 100000.00 | cash | partial | first row for id 40 |
| 40 | 2025-11-10 | 120000.00 | cash | partial | same id, disagreeing `amount_paid` |
| 41 | 2025-11-15 | 90000.00 | pos | partial | unrelated, clean payment |

**Processing:**

- `payment_id 40`: the two rows share an id but **disagree on `amount_paid`** (100000.00 vs 120000.00). Per Section 4's duplicate rule, no row is picked as a winner. `payment_id 40` is **excluded from the statement** and reported as a conflicting duplicate, with both disagreeing rows listed verbatim for manual resolution.
- `payment_id 41`: clean, strict-ISO, in-period — **included** normally.

**Resulting statement:**

| payment_id | amount_paid | payment_date | payment_method | status |
|---|---|---|---|---|
| 41 | 90000.00 | 2025-11-15 | pos | partial |

**Printed total:** 90000.00

**Flags surfaced alongside the statement:**
- `payment_id 40`: **conflicting duplicate** — two rows share this id with disagreeing `amount_paid` (100000.00 and 120000.00). Excluded pending manual resolution in the source ledger; statement cannot be certified reconciled for this id until resolved.

**Reconciliation check:** statement ids `{41}` = ledger ids validly in period, excluding conflicting duplicates, `{41}` (40 withheld, not silently resolved). Printed total 90000.00 = 90000.00. ✅ Reconciled for the includable set, with the conflict surfaced rather than hidden.

---

## 6. Open Questions

None. All decisions required by Sections 1–4 are resolved above; none are marked TBD.

---

## 7. Review & Sign-off

| Reviewer | Focus | Status | Date |
|---|---|---|---|
| Rosa | Ledger semantics vs. actual data layer | Pending | — |
| Naledi | Usability for a guardian/accountant reading the statement | Pending | — |

**This document is a proposed contract, not an agreed one, until both rows above show a completed status and date.** No later fee-statement task may implement against Sections 1–4 while either review is Pending. Once both reviews are folded in and both rows are updated to Approved, this document is re-stamped as v1.0 **Agreed** in project documentation, the status line in the header is updated accordingly, and this task is closed.