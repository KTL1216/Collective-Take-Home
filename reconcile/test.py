"""
Tests for the reconcile app: CSV parsing, reconciliation logic, forms, and views.

This app has no Django ORM models; domain types are dataclasses in services.py.
Run with: python app.py test reconcile
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from reconcile.forms import ReconciliationUploadForm
from reconcile.services import (
    BankBalance,
    DailyReconciliation,
    ParseError,
    ReconciliationSummary,
    Transaction,
    parse_bank_balances_csv,
    parse_transactions_csv,
    reconcile,
)

SAMPLE_DATA_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def _csv(content: str) -> io.StringIO:
    return io.StringIO(content.strip() + "\n")


def _upload(name: str, content: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content.strip().encode("utf-8"), content_type="text/csv")


# --- Domain dataclasses (no Django models in this app) ---


class DataclassTests(SimpleTestCase):
    def test_transaction_is_frozen_and_ordered_fields(self):
        txn = Transaction(date(2025, 6, 1), Decimal("10.00"))
        self.assertEqual(txn.transaction_date, date(2025, 6, 1))
        self.assertEqual(txn.amount, Decimal("10.00"))
        with self.assertRaises(AttributeError):
            txn.amount = Decimal("0")  # type: ignore[misc]

    def test_bank_balance_and_daily_reconciliation(self):
        bal = BankBalance(date(2025, 6, 1), Decimal("100"))
        row = DailyReconciliation(
            balance_date=date(2025, 6, 1),
            expected_balance=Decimal("100"),
            actual_balance=Decimal("100"),
            difference=Decimal("0"),
            matches=True,
            transaction_total_for_day=Decimal("100"),
            transaction_count_for_day=1,
        )
        self.assertEqual(bal.balance, Decimal("100"))
        self.assertTrue(row.matches)


# --- parse_transactions_csv ---


class ParseTransactionsCsvTests(SimpleTestCase):
    def test_parses_valid_rows_and_sorts(self):
        source = _csv(
            """date,amount
2025-06-02,-25.00
2025-06-01,1000.00
"""
        )
        txns, warnings = parse_transactions_csv(source)
        self.assertEqual(warnings, [])
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].transaction_date, date(2025, 6, 1))
        self.assertEqual(txns[1].transaction_date, date(2025, 6, 2))

    def test_skips_rows_without_date_or_amount_with_warnings(self):
        source = _csv(
            """date,amount
None,
2025-06-01,100.00
2025-06-02,
"""
        )
        txns, warnings = parse_transactions_csv(source, source_name="ledger.csv")
        self.assertEqual(len(txns), 1)
        self.assertEqual(len(warnings), 2)
        self.assertIn("ledger.csv", warnings[0])
        self.assertIn("no date", warnings[0])
        self.assertIn("no amount", warnings[1])

    def test_skips_invalid_rows_with_warning(self):
        source = _csv(
            """date,amount
not-a-date,10.00
2025-06-01,not-a-number
"""
        )
        txns, warnings = parse_transactions_csv(source)
        self.assertEqual(txns, [])
        self.assertEqual(len(warnings), 2)
        self.assertIn("skipped invalid row", warnings[0])

    def test_requires_date_and_amount_columns(self):
        with self.assertRaises(ParseError) as ctx:
            parse_transactions_csv(_csv("foo,bar\n1,2\n"))
        self.assertIn("date", str(ctx.exception).lower())

    def test_empty_file_raises(self):
        with self.assertRaises(ParseError):
            parse_transactions_csv(io.StringIO(""))

    def test_normalizes_bom_and_header_case(self):
        source = io.StringIO("\ufeffDate,Amount\n2025-06-01,5.00\n")
        txns, warnings = parse_transactions_csv(source)
        self.assertEqual(warnings, [])
        self.assertEqual(txns[0].amount, Decimal("5.00"))

    def test_skips_blank_lines(self):
        source = _csv(
            """date,amount
2025-06-01,1.00

2025-06-02,2.00
"""
        )
        txns, _ = parse_transactions_csv(source)
        self.assertEqual(len(txns), 2)


# --- parse_bank_balances_csv ---


class ParseBankBalancesCsvTests(SimpleTestCase):
    def test_parses_and_sorts_by_date(self):
        source = _csv(
            """date,balance
2025-06-02,200.00
2025-06-01,100.00
"""
        )
        balances = parse_bank_balances_csv(source)
        self.assertEqual(len(balances), 2)
        self.assertEqual(balances[0].balance_date, date(2025, 6, 1))
        self.assertEqual(balances[1].balance, Decimal("200.00"))

    def test_accepts_amount_column_as_balance_alias(self):
        source = _csv(
            """date,amount
2025-06-01,99.99
"""
        )
        balances = parse_bank_balances_csv(source)
        self.assertEqual(balances[0].balance, Decimal("99.99"))

    def test_requires_columns_and_data_rows(self):
        with self.assertRaises(ParseError):
            parse_bank_balances_csv(_csv("date,balance\n"))

        with self.assertRaises(ParseError):
            parse_bank_balances_csv(_csv("only,headers\n"))

    def test_rejects_missing_or_invalid_values(self):
        with self.assertRaises(ParseError):
            parse_bank_balances_csv(_csv("date,balance\n,balance\n"))

        with self.assertRaises(ParseError):
            parse_bank_balances_csv(_csv("date,balance\n2025-13-40,10\n"))


# --- reconcile ---


class ReconcileTests(SimpleTestCase):
    def test_running_sum_and_carry_forward_on_quiet_days(self):
        transactions = [
            Transaction(date(2025, 6, 1), Decimal("100.00")),
            Transaction(date(2025, 6, 2), Decimal("-25.00")),
        ]
        bank_balances = [
            BankBalance(date(2025, 6, 1), Decimal("100.00")),
            BankBalance(date(2025, 6, 2), Decimal("75.00")),
            BankBalance(date(2025, 6, 3), Decimal("75.00")),  # no txns; carry forward
        ]
        summary = reconcile(transactions, bank_balances)

        self.assertEqual(summary.mismatch_count, 0)
        self.assertIsNone(summary.first_mismatch_date)
        self.assertEqual(summary.transaction_count, 2)
        self.assertEqual(summary.bank_statement_days, 3)

        row_quiet = summary.rows[2]
        self.assertEqual(row_quiet.expected_balance, Decimal("75.00"))
        self.assertEqual(row_quiet.transaction_total_for_day, Decimal("0"))
        self.assertEqual(row_quiet.transaction_count_for_day, 0)
        self.assertTrue(row_quiet.matches)

    def test_multiple_transactions_same_day(self):
        transactions = [
            Transaction(date(2025, 6, 1), Decimal("10.00")),
            Transaction(date(2025, 6, 1), Decimal("-3.00")),
        ]
        bank_balances = [BankBalance(date(2025, 6, 1), Decimal("7.00"))]
        summary = reconcile(transactions, bank_balances)

        self.assertTrue(summary.rows[0].matches)
        self.assertEqual(summary.rows[0].transaction_total_for_day, Decimal("7.00"))
        self.assertEqual(summary.rows[0].transaction_count_for_day, 2)

    def test_tolerance_boundary_at_one_cent(self):
        transactions = [Transaction(date(2025, 6, 1), Decimal("100.00"))]
        within = [BankBalance(date(2025, 6, 1), Decimal("100.01"))]
        outside = [BankBalance(date(2025, 6, 1), Decimal("100.02"))]

        self.assertTrue(reconcile(transactions, within).rows[0].matches)
        self.assertFalse(reconcile(transactions, outside).rows[0].matches)
        self.assertEqual(reconcile(transactions, outside).rows[0].difference, Decimal("0.02"))

    def test_flags_mismatch_and_first_mismatch_date(self):
        transactions = [
            Transaction(date(2025, 6, 1), Decimal("100.00")),
            Transaction(date(2025, 6, 2), Decimal("50.00")),
        ]
        bank_balances = [
            BankBalance(date(2025, 6, 1), Decimal("100.00")),
            BankBalance(date(2025, 6, 2), Decimal("200.00")),  # expected 150
            BankBalance(date(2025, 6, 3), Decimal("200.00")),
        ]
        summary = reconcile(transactions, bank_balances)

        self.assertEqual(summary.mismatch_count, 2)
        self.assertEqual(summary.first_mismatch_date, date(2025, 6, 2))
        self.assertTrue(summary.rows[0].matches)
        self.assertFalse(summary.rows[1].matches)

    def test_warns_on_transactions_outside_bank_date_range(self):
        transactions = [
            Transaction(date(2025, 5, 31), Decimal("1.00")),
            Transaction(date(2025, 6, 2), Decimal("1.00")),
            Transaction(date(2025, 6, 4), Decimal("1.00")),
        ]
        bank_balances = [
            BankBalance(date(2025, 6, 1), Decimal("0")),
            BankBalance(date(2025, 6, 3), Decimal("2")),
        ]
        summary = reconcile(transactions, bank_balances)

        self.assertEqual(len(summary.warnings), 2)
        self.assertTrue(any("before first bank" in w for w in summary.warnings))
        self.assertTrue(any("after last bank" in w for w in summary.warnings))

    def test_empty_transactions_still_compares_bank_balances(self):
        bank_balances = [
            BankBalance(date(2025, 6, 1), Decimal("0")),
            BankBalance(date(2025, 6, 2), Decimal("0")),
        ]
        summary = reconcile([], bank_balances)
        self.assertEqual(summary.transaction_count, 0)
        self.assertTrue(all(r.expected_balance == Decimal("0") for r in summary.rows))
        self.assertTrue(all(r.matches for r in summary.rows))

    def test_custom_tolerance(self):
        transactions = [Transaction(date(2025, 6, 1), Decimal("0"))]
        bank = [BankBalance(date(2025, 6, 1), Decimal("0.05"))]
        strict = reconcile(transactions, bank, tolerance=Decimal("0.01"))
        loose = reconcile(transactions, bank, tolerance=Decimal("0.10"))
        self.assertFalse(strict.rows[0].matches)
        self.assertTrue(loose.rows[0].matches)


class SampleDataReconciliationTests(SimpleTestCase):
    """Integration tests using bundled sample CSV files."""

    def test_sample_files_parse_and_reconcile_with_known_mismatches(self):
        txn_path = SAMPLE_DATA_DIR / "transactions.csv"
        bank_path = SAMPLE_DATA_DIR / "bank_balances.csv"

        with txn_path.open(encoding="utf-8") as txn_file:
            transactions, parse_warnings = parse_transactions_csv(txn_file)
        with bank_path.open(encoding="utf-8") as bank_file:
            bank_balances = parse_bank_balances_csv(bank_file)

        self.assertEqual(len(parse_warnings), 1)
        self.assertEqual(len(transactions), 14)

        summary = reconcile(transactions, bank_balances)
        self.assertIsInstance(summary, ReconciliationSummary)
        self.assertEqual(summary.bank_statement_days, 12)
        self.assertEqual(summary.mismatch_count, 6)
        self.assertEqual(summary.first_mismatch_date, date(2025, 6, 7))

        # First six statement days reconcile; discrepancies start 2025-06-07.
        for row in summary.rows[:6]:
            self.assertTrue(row.matches, msg=f"expected match on {row.balance_date}")
        for row in summary.rows[6:]:
            self.assertFalse(row.matches, msg=f"expected mismatch on {row.balance_date}")


# --- forms ---


class ReconciliationUploadFormTests(TestCase):
    def test_valid_csv_files_pass_validation(self):
        form = ReconciliationUploadForm(
            {},
            {
                "transactions_file": _upload("txns.csv", "date,amount\n2025-06-01,1\n"),
                "bank_balances_file": _upload("bank.csv", "date,balance\n2025-06-01,1\n"),
            },
        )
        self.assertTrue(form.is_valid())

    def test_rejects_non_csv_extension(self):
        bad = SimpleUploadedFile("data.txt", b"date,amount\n", content_type="text/plain")
        form = ReconciliationUploadForm(
            {},
            {"transactions_file": bad, "bank_balances_file": bad},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("transactions_file", form.errors)
        self.assertIn("bank_balances_file", form.errors)

    def test_rejects_files_over_two_megabytes(self):
        huge = b"x" * (2 * 1024 * 1024 + 1)
        form = ReconciliationUploadForm(
            {},
            {
                "transactions_file": SimpleUploadedFile("big.csv", huge),
                "bank_balances_file": SimpleUploadedFile("big2.csv", huge),
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("2 MB", form.errors["transactions_file"][0])


# --- views ---


class UploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("upload")

    def test_get_renders_upload_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Transactions CSV")

    def test_post_missing_files_returns_400(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)

    def test_post_invalid_csv_shows_form_error(self):
        response = self.client.post(
            self.url,
            {
                "transactions_file": _upload("txns.csv", "wrong,col\n1,2\n"),
                "bank_balances_file": _upload("bank.csv", "date,balance\n2025-06-01,1\n"),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "date", status_code=400)

    def test_post_valid_sample_files_renders_report(self):
        txn_bytes = (SAMPLE_DATA_DIR / "transactions.csv").read_bytes()
        bank_bytes = (SAMPLE_DATA_DIR / "bank_balances.csv").read_bytes()
        response = self.client.post(
            self.url,
            {
                "transactions_file": SimpleUploadedFile(
                    "transactions.csv", txn_bytes, content_type="text/csv"
                ),
                "bank_balances_file": SimpleUploadedFile(
                    "bank_balances.csv", bank_bytes, content_type="text/csv"
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2025-06-07")
        self.assertContains(response, "mismatch", count=None)  # report shows mismatch info

    def test_post_fully_reconciled_data_shows_success(self):
        transactions = _upload(
            "txns.csv",
            """date,amount
2025-06-01,100.00
2025-06-02,-25.00
""",
        )
        bank = _upload(
            "bank.csv",
            """date,balance
2025-06-01,100.00
2025-06-02,75.00
2025-06-03,75.00
""",
        )
        response = self.client.post(
            self.url,
            {"transactions_file": transactions, "bank_balances_file": bank},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Balances reconcile")
        self.assertContains(response, "banner-ok")
