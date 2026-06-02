from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .services import (
    parse_bank_balances_csv,
    parse_transactions_csv,
    reconcile,
)


SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


class ReconciliationServiceTests(SimpleTestCase):
    def test_sample_data_reconciles(self):
        with (SAMPLE_DIR / "transactions.csv").open(encoding="utf-8") as txn_file:
            transactions, warnings = parse_transactions_csv(txn_file)
        with (SAMPLE_DIR / "bank_balances.csv").open(encoding="utf-8") as bank_file:
            bank_balances = parse_bank_balances_csv(bank_file)

        self.assertEqual(len(transactions), 14)
        self.assertTrue(any("opening-balance" in w.lower() or "no date" in w.lower() for w in warnings))

        summary = reconcile(transactions, bank_balances)
        self.assertEqual(summary.mismatch_count, 0)
        self.assertIsNone(summary.first_mismatch_date)
        self.assertEqual(summary.rows[-1].expected_balance, Decimal("695.00"))
        self.assertTrue(summary.rows[-1].matches)

    def test_carry_forward_on_quiet_days(self):
        from .services import BankBalance, Transaction

        transactions = [Transaction(date(2025, 6, 1), Decimal("100"))]
        bank = [
            BankBalance(date(2025, 6, 1), Decimal("100")),
            BankBalance(date(2025, 6, 2), Decimal("100")),
        ]
        summary = reconcile(transactions, bank)
        self.assertTrue(all(r.matches for r in summary.rows))
        self.assertEqual(summary.rows[1].expected_balance, Decimal("100"))

    def test_detects_mismatch(self):
        from .services import BankBalance, Transaction

        transactions = [Transaction(date(2025, 6, 1), Decimal("100"))]
        bank = [BankBalance(date(2025, 6, 1), Decimal("90"))]
        summary = reconcile(transactions, bank)
        self.assertEqual(summary.mismatch_count, 1)
        self.assertEqual(summary.rows[0].difference, Decimal("-10"))


class UploadViewTests(TestCase):
    def test_upload_sample_files(self):
        url = reverse("upload")
        with (SAMPLE_DIR / "transactions.csv").open("rb") as txn, (
            SAMPLE_DIR / "bank_balances.csv"
        ).open("rb") as bank:
            response = self.client.post(
                url,
                {
                    "transactions_file": txn,
                    "bank_balances_file": bank,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Balances reconcile")

    def test_rejects_missing_columns(self):
        url = reverse("upload")
        bad = SimpleUploadedFile("bad.csv", b"foo,bar\n1,2\n", content_type="text/csv")
        response = self.client.post(
            url,
            {
                "transactions_file": bad,
                "bank_balances_file": bad,
            },
        )
        self.assertEqual(response.status_code, 400)
