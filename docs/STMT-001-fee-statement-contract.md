# Reconcilable Fee Statement — Contract

**Status:** Proposed contract (v1.0) — **NOT YET IN FORCE.** Awaiting sign-off from Rosa (ledger semantics) and Naledi (guardian/accountant usability). No later task may implement against this document until Section 7 shows both reviewers signed off; until then this is a draft under review, not the agreed reference.
**Owner:** Joseph Akpe Unimke (Joseph Siakpe)
**Applies to:** All later fee-statement generation, reconciliation, and reporting tasks in this sprint
**Supersedes:** No prior version. This is the canonical reference; later tasks point here rather than re-deriving these rules.

---

## 0. Grounding — the data this contract is written against

This contract is written against the actual operational schema, not an idealized one:

- `fees_payment` is the only operational fee table. Its columns are: `payment_id`, `student_id`, `term`, `amount_due`, `amount_paid`, `payment_date` (TEXT, no format constraint at the database level), `payment_method`, `status`.
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

| Field | Mandatory? | Source                        | Notes |
|---|---|-------------------------------|---|
| `payment_id` | **Yes** | `fees_payment.payment_id`     | The traceability key. Every line traces to exactly one payment by this id, and a given `payment_id` appears **at most once** on a statement. |
| `amount_paid` | **Yes** | `fees_payment.amount_paid`    | The monetary amount. Rendered at 2 decimal places (see Section 4 for the rounding rule). |
| `payment_date` | No | `fees_payment.payment_date`   | Included for readability; already validated as strict ISO by definition, since unplaceable payments never reach the line-generation step. |
| `payment_method` | No | `fees_payment.payment_method` | Passed through as-is. |
| `status` | No | `fees_payment.status`         | Passed through as-is. |

No other fields are added. This is deliberately minimal — a line is auditable back to a single row in `fees_payment` and nothing more.

## 4. Reconciliation Definition

A statement is **reconciled** for a given period if and only if it reaches **generation status `RECONCILED`**, defined below, and all three of the following hold at generation time:

**Generation status.** Every statement run produces exactly one of two statuses:
- `RECONCILED` — every ledger `payment_id` in the period was either placed on the statement or deterministically excluded by a rule this contract already resolves (an unplaceable date, Section 2, or an identical duplicate, below) — with no unresolved conflicting duplicates — and the set-equality and total checks below both pass.
- `BLOCKED` — one or more `payment_id`s in the period is a **conflicting duplicate** (below) that this contract deliberately does not resolve automatically. A `BLOCKED` statement may still print its includable lines and their subtotal for visibility, but **the run as a whole must never be labeled or reported as `RECONCILED`**, and no downstream process may treat a `BLOCKED` run's subtotal as a certified reconciliation.

  The distinction that matters here: an unplaceable date or an identical duplicate is not an open question — Sections 1–2 and the duplicate rule below already state exactly what happens to it (excluded-and-flagged, or deduped-and-flagged), so a run containing only those anomalies has nothing left for a human to decide and can be `RECONCILED`. A conflicting duplicate is different — this contract explicitly refuses to pick a winner, so the ambiguity is still open, and that is what forces `BLOCKED` until a person resolves it in the source ledger.

1. **Set equality:** the set of `payment_id` values on the statement equals the set of `payment_id` values in the ledger (`fees_payment`) that fall inside the period per Sections 1–2, and each id appears **exactly once** on both sides.

   **Duplicate `payment_id` rule.** `fees_payment` has no `updated_at`, no version column, and no other field that reliably indicates which of two rows sharing a `payment_id` is authoritative — so no field-based "winner" can be chosen without guessing. Comparison for this rule is over **the entire ledger row**, not just the fields that appear on a printed statement line — `student_id`, `term`, and `amount_due` must match too, not only `amount_paid`, `payment_date`, `payment_method`, and `status`. A row can render identically on a statement line while differing in a column the line doesn't show (e.g. `term`), and that is still a conflict, not a duplicate. The rule is therefore:
   - If two or more rows share a `payment_id` and are **identical across every column of `fees_payment`** (`student_id`, `term`, `amount_due`, `amount_paid`, `payment_date`, `payment_method`, `status`), they are treated as one payment: deduplicated to a single line, and the duplication is flagged in the generation output as a ledger anomaly. This case alone does not force `BLOCKED`.
   - If two or more rows share a `payment_id` and **disagree on any column of `fees_payment`** — whether or not that column is shown on the statement line — no row is selected as the winner. The `payment_id` is **excluded from the statement's counted lines** (on the same footing as an unplaceable date, Section 2), is surfaced in the output as a **conflicting duplicate** listing every disagreeing row verbatim across all columns, and forces the run's generation status to `BLOCKED`. It must be resolved in the source ledger and the statement regenerated before the period can reach `RECONCILED`.
   - This is a deliberate no-guess rule: silently picking "the higher amount" or "the later row by insertion order" would let a data-entry error quietly determine a family's fee record. Exclusion-and-flag forces a human to resolve it instead.

2. **Total correctness:** the printed statement total equals the sum of the printed line amounts — not a separately computed database sum. This ties the "total" a reader sees directly to the lines they can audit. On a `BLOCKED` run, this total is a **subtotal of includable lines only** and must be labeled as such, never as "the total."
3. **Rounding rule — exact REAL-to-decimal mechanism.** `amount_paid` is stored as a SQLite REAL, i.e. an IEEE-754 double, which cannot represent most decimal fractions exactly. To remove ambiguity about how a decimal monetary value is obtained from that stored float, every implementation must follow this exact pipeline and no other:
   1. Read the stored REAL value.
   2. Convert it to its **shortest round-trip decimal string** using the runtime's standard float-to-string conversion (e.g. Python's `repr()`/`str()` on a float, which by specification produces the shortest decimal string that parses back to the identical float). This step does **not** claim to recover whatever decimal a person originally typed — if that original decimal needed more precision than an IEEE-754 double can hold, that precision was already lost irreversibly at the moment the value was stored as a REAL, and no downstream conversion can undo it. What this step actually fixes is a narrower but still real ambiguity: given the float as stored, there are infinitely many decimal strings that would parse back to it, and different runtimes or naive conversions can pick different ones. Shortest-round-trip conversion is specified precisely so that every conformant implementation deterministically produces the same string for the same stored float — removing implementation-choice ambiguity, not data-loss ambiguity.
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

**Reconciliation check:** statement ids `{1, 2}` = ledger ids in period `{1, 2}`, each once. Printed total 500000.00 = 185000.00 + 315000.00. **Generation status: `RECONCILED`.**

### 5.2 Edge-case example (boundary date + malformed date + duplicate id)

**Period:** First Term, `2025-09-01` to `2025-12-31` (inclusive).

**Ledger rows:**

| payment_id | student_id | term | amount_due | payment_date | amount_paid | payment_method | status | Note |
|---|---|---|---|---|---|---|---|---|
| 30 | 20 | First Term | 500000.00 | 2025-12-31 | 50000.00 | pos | partial | exactly on the end boundary |
| 31 | 21 | First Term | 500000.00 | 12/31/2025 | 75000.00 | card | partial | non-ISO format |
| 30 | 20 | First Term | 500000.00 | 2025-12-31 | 50000.00 | pos | partial | duplicate export of payment_id 30, identical on every column |

**Processing:**

- `payment_id 30` (date `2025-12-31`, on the boundary): **included**, per Section 1 the end date is inclusive. It appears twice in the raw export as **identical rows across every `fees_payment` column**, not just the rendered ones — `student_id`, `term`, and `amount_due` match too, in addition to `amount_paid`, `payment_date`, `payment_method`, and `status`. Per Section 4's duplicate rule this is the non-conflicting case, so it is deduplicated to one line and the duplicate is flagged as a ledger anomaly rather than treated as a conflict.
- `payment_id 31` (date `12/31/2025`): **excluded** from the statement, per Section 2 — non-ISO format is not parsed. It is surfaced in the "unplaceable payments" output as unplaceable, not silently dropped.

**Resulting statement:**

| payment_id | amount_paid | payment_date | payment_method | status |
|---|---|---|---|---|
| 30 | 50000.00 | 2025-12-31 | pos | partial |

**Printed total:** 50000.00

**Flags surfaced alongside the statement:**
- `payment_id 30`: duplicate row detected in source ledger, identical across all columns; deduplicated to one line.
- `payment_id 31`: unplaceable — `payment_date` value `12/31/2025` is not strict ISO `YYYY-MM-DD`; excluded from period.

**Reconciliation check:** statement ids `{30}` (deduplicated) = ledger ids validly in period `{30}` (31 excluded as unplaceable, per Section 2 it is not a "ledger id in period" at all). Printed total 50000.00 = 50000.00. **Generation status: `RECONCILED`** — the only anomaly present is an identical duplicate, which by rule does not force `BLOCKED`.

### 5.3 Edge-case example (conflicting duplicate `payment_id`, including a conflict invisible on the rendered line)

**Period:** First Term, `2025-09-01` to `2025-12-31` (inclusive).

**Ledger rows (full `fees_payment` columns, not just the ones a statement line renders):**

| payment_id | student_id | term | amount_due | payment_date | amount_paid | payment_method | status | Note |
|---|---|---|---|---|---|---|---|---|
| 40 | 12 | First Term | 500000.00 | 2025-11-10 | 100000.00 | cash | partial | first row for id 40 |
| 40 | **13** | First Term | 500000.00 | 2025-11-10 | 100000.00 | cash | partial | same id, **same rendered fields**, disagreeing `student_id` |
| 41 | 14 | First Term | 500000.00 | 2025-11-15 | 90000.00 | pos | partial | unrelated, clean payment |

**Processing:**

- `payment_id 40`: the two rows are **identical on every field a statement line renders** (`amount_paid`, `payment_date`, `payment_method`, `status` all match) — a comparison limited to rendered columns would wrongly treat this as a harmless duplicate and silently dedupe it, attaching the payment to whichever `student_id` happened to be scanned first. Per Section 4's duplicate rule, comparison is over the **entire ledger row**, so the disagreeing `student_id` (12 vs 13) is caught: this is a **conflicting duplicate**, not an identical one. No row is picked as a winner. `payment_id 40` is excluded from the statement's counted lines, reported as a conflicting duplicate with both full rows listed verbatim, and the run's generation status is forced to `BLOCKED`.
- `payment_id 41`: clean, strict-ISO, in-period, no duplicate — **included** normally.

**Resulting statement (subtotal only — see status below):**

| payment_id | amount_paid | payment_date | payment_method | status |
|---|---|---|---|---|
| 41 | 90000.00 | 2025-11-15 | pos | partial |

**Printed subtotal of includable lines:** 90000.00 (explicitly labeled a subtotal, not "the total" — see Section 4.2)

**Flags surfaced alongside the statement:**
- `payment_id 40`: **conflicting duplicate** — two rows share this id and render identically on a statement line, but disagree on `student_id` (12 vs 13), a column the rendered line does not show. Excluded pending manual resolution in the source ledger.

**Generation status: `BLOCKED`.** Section 4 is explicit that a conflicting duplicate forces `BLOCKED` and that a `BLOCKED` run must never be labeled or reported as `RECONCILED` — so this run is **not** reconciled, even though the one includable line (`payment_id 41`) sums correctly on its own (90000.00 = 90000.00). That internal consistency of the includable subset is necessary but not sufficient: `payment_id 40` remains an open conflict in the period, the set of statement ids (`{41}`) does not equal the full set of in-period ledger ids (`{40, 41}`), and the statement must be regenerated after `payment_id 40` is resolved in the source ledger before this period can reach `RECONCILED`.

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