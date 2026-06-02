"""
CSV parsing and balance reconciliation.

For each bank statement date, expected balance is a running sum of transaction
amounts posted that day; on days with no postings the expected balance is unchanged.
A day matches when |bank_balance - expected| <= tolerance (default $0.01).

Transaction rows with missing/invalid fields are skipped with warnings; bank rows
must parse cleanly or parsing raises ParseError.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import BinaryIO, Iterable, TextIO


class ParseError(ValueError):
    """Fatal CSV shape or bank-row parse failure (see parse_transactions for soft skips)."""


@dataclass(frozen=True)
class Transaction:
    """Ledger line; amount is signed (debits negative)."""

    transaction_date: date
    amount: Decimal


@dataclass(frozen=True)
class BankBalance:
    """End-of-day balance from the bank statement."""

    balance_date: date
    balance: Decimal


@dataclass(frozen=True)
class DailyReconciliation:
    """One statement date: cumulative expected vs reported bank balance."""
    balance_date: date
    expected_balance: Decimal
    actual_balance: Decimal
    difference: Decimal
    matches: bool
    transaction_total_for_day: Decimal
    transaction_count_for_day: int


@dataclass(frozen=True)
class ReconciliationSummary:
    """Full run: per-day rows plus roll-up counts and non-fatal warnings."""

    rows: list[DailyReconciliation]
    first_mismatch_date: date | None
    mismatch_count: int
    transaction_count: int
    bank_statement_days: int
    warnings: list[str]


def _normalize_header(name: str) -> str:
    # Case-insensitive match; strip UTF-8 BOM from Excel exports.
    return name.strip().lower().lstrip("\ufeff")


def _parse_decimal(value: str, field: str, row_num: int) -> Decimal:
    raw = value.strip()
    if not raw or raw.lower() in {"none", "null", "n/a", ""}:
        raise ParseError(f"Row {row_num}: missing {field}")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ParseError(f"Row {row_num}: invalid {field} '{value}'") from exc


def _parse_date(value: str, field: str, row_num: int) -> date:
    raw = value.strip()
    if not raw or raw.lower() in {"none", "null", "n/a"}:
        raise ParseError(f"Row {row_num}: missing or invalid {field}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ParseError(f"Row {row_num}: invalid date '{value}' (use YYYY-MM-DD)") from exc


def _read_csv_rows(source: TextIO | BinaryIO) -> Iterable[dict[str, str]]:
    """Yield (row_num, header_map, row) for each non-empty data row."""
    if isinstance(source, io.TextIOBase):
        reader = csv.DictReader(source)
    else:
        # UploadedFile from Django is binary; utf-8-sig handles BOM on first column.
        text = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
    if reader.fieldnames is None:
        raise ParseError("CSV file is empty or has no header row")
    # Map normalized name -> original header key for DictReader row lookup.
    normalized = {_normalize_header(h): h for h in reader.fieldnames}
    for row_num, row in enumerate(reader, start=2):  # row 1 is the header
        if not any((v or "").strip() for v in row.values()):
            continue
        yield row_num, normalized, row


def parse_transactions_csv(
    source: TextIO | BinaryIO,
    *,
    source_name: str = "Transactions CSV",
) -> tuple[list[Transaction], list[str]]:
    """Parse transactions CSV with columns: date, amount.

    Bad rows are skipped and recorded in warnings (e.g. opening-balance placeholders).
    """
    transactions: list[Transaction] = []
    warnings: list[str] = []

    for row_num, headers, row in _read_csv_rows(source):
        date_key = headers.get("date")
        amount_key = headers.get("amount")
        if not date_key or not amount_key:
            raise ParseError("Transactions CSV must include 'date' and 'amount' columns")

        date_raw = (row.get(date_key) or "").strip()
        amount_raw = (row.get(amount_key) or "").strip()

        if not date_raw or date_raw.lower() in {"none", "null", "n/a"}:
            warnings.append(
                f"Row {row_num} in {source_name}: skipped row with no date"
            )
            continue

        if not amount_raw or amount_raw.lower() in {"none", "null", "n/a"}:
            warnings.append(
                f"Row {row_num} in {source_name}: skipped row with date {date_raw} but no amount"
            )
            continue

        try:
            txn_date = _parse_date(date_raw, "date", row_num)
            amount = _parse_decimal(amount_raw, "amount", row_num)
        except ParseError:
            warnings.append(
                f"Row {row_num} in {source_name}: skipped invalid row "
                f"(date={date_raw!r}, amount={amount_raw!r})"
            )
            continue

        transactions.append(Transaction(txn_date, amount))

    transactions.sort(key=lambda t: (t.transaction_date, t.amount))
    return transactions, warnings


def parse_bank_balances_csv(source: TextIO | BinaryIO) -> list[BankBalance]:
    """Parse bank balances CSV with columns: date, balance.

    Any invalid row aborts the whole file (bank data must be complete).
    """
    balances: list[BankBalance] = []

    for row_num, headers, row in _read_csv_rows(source):
        date_key = headers.get("date")
        balance_key = headers.get("balance")
        if not balance_key:
            # Accept 'amount' as alias for mislabeled exports
            balance_key = headers.get("amount")
        if not date_key or not balance_key:
            raise ParseError("Bank balances CSV must include 'date' and 'balance' columns")

        txn_date = _parse_date(row.get(date_key, ""), "date", row_num)
        balance = _parse_decimal(row.get(balance_key, ""), "balance", row_num)
        balances.append(BankBalance(txn_date, balance))

    if not balances:
        raise ParseError("Bank balances CSV has no data rows")

    balances.sort(key=lambda b: b.balance_date)
    return balances


def reconcile(
    transactions: list[Transaction],
    bank_balances: list[BankBalance],
    *,
    tolerance: Decimal = Decimal("0.01"),
) -> ReconciliationSummary:
    """
    Compare cumulative transaction totals to each bank statement date.

    On days with no transactions, expected balance carries forward from the prior day.
    """
    warnings: list[str] = []

    # Aggregate multiple ledger lines per calendar day.
    daily_txn_totals: dict[date, Decimal] = {}
    daily_txn_counts: dict[date, int] = {}
    for txn in transactions:
        daily_txn_totals[txn.transaction_date] = daily_txn_totals.get(txn.transaction_date, Decimal("0")) + txn.amount
        daily_txn_counts[txn.transaction_date] = daily_txn_counts.get(txn.transaction_date, 0) + 1

    # Ledger dates outside the statement window are reported but still excluded
    # from running_expected unless a statement row exists on that date.
    txn_dates = sorted(daily_txn_totals.keys())
    if txn_dates and bank_balances:
        first_bank = bank_balances[0].balance_date
        last_bank = bank_balances[-1].balance_date
        for d in txn_dates:
            if d < first_bank:
                warnings.append(
                    f"Transactions exist before first bank statement date ({first_bank}): {d}"
                )
            if d > last_bank:
                warnings.append(
                    f"Transactions exist after last bank statement date ({last_bank}): {d}"
                )

    running_expected = Decimal("0")
    rows: list[DailyReconciliation] = []
    mismatch_count = 0
    first_mismatch_date: date | None = None

    for entry in bank_balances:
        # Post that day's ledger total; quiet days leave running_expected unchanged.
        if entry.balance_date in daily_txn_totals:
            running_expected += daily_txn_totals[entry.balance_date]

        difference = entry.balance - running_expected
        matches = abs(difference) <= tolerance  # bank - expected; default ±$0.01
        if not matches:
            mismatch_count += 1
            if first_mismatch_date is None:
                first_mismatch_date = entry.balance_date

        rows.append(
            DailyReconciliation(
                balance_date=entry.balance_date,
                expected_balance=running_expected,
                actual_balance=entry.balance,
                difference=difference,
                matches=matches,
                transaction_total_for_day=daily_txn_totals.get(entry.balance_date, Decimal("0")),
                transaction_count_for_day=daily_txn_counts.get(entry.balance_date, 0),
            )
        )

    return ReconciliationSummary(
        rows=rows,
        first_mismatch_date=first_mismatch_date,
        mismatch_count=mismatch_count,
        transaction_count=len(transactions),
        bank_statement_days=len(bank_balances),
        warnings=warnings,
    )
