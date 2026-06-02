"""Upload two CSVs and render a daily reconciliation report."""

import json

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from .forms import ReconciliationUploadForm
from .services import ParseError, parse_bank_balances_csv, parse_transactions_csv, reconcile


class UploadView(View):
    """GET: upload form. POST: parse files, reconcile, render report."""

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "reconcile/upload.html", {"form": ReconciliationUploadForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = ReconciliationUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "reconcile/upload.html", {"form": form}, status=400)

        transactions_file = form.cleaned_data["transactions_file"]
        bank_file = form.cleaned_data["bank_balances_file"]

        try:
            # UploadedFile may have been read during validation; rewind before parsing.
            transactions_file.seek(0)
            transactions, parse_warnings = parse_transactions_csv(
                transactions_file,
                source_name=transactions_file.name,
            )
            bank_file.seek(0)
            bank_balances = parse_bank_balances_csv(bank_file)
        except ParseError as exc:
            form.add_error(None, str(exc))
            return render(request, "reconcile/upload.html", {"form": form}, status=400)

        summary = reconcile(transactions, bank_balances)
        all_warnings = parse_warnings + summary.warnings
        reconciles = summary.mismatch_count == 0

        max_abs_diff = max((abs(r.difference) for r in summary.rows), default=0)
        # Pre-serialize chart series for Chart.js in report.html (no separate API).
        chart_labels = [r.balance_date.isoformat() for r in summary.rows]
        chart_expected = [float(r.expected_balance) for r in summary.rows]
        chart_actual = [float(r.actual_balance) for r in summary.rows]

        context = {
            "summary": summary,
            "all_warnings": all_warnings,
            "reconciles": reconciles,
            "max_abs_diff": max_abs_diff,
            "chart_labels_json": json.dumps(chart_labels),
            "chart_expected_json": json.dumps(chart_expected),
            "chart_actual_json": json.dumps(chart_actual),
        }
        return render(request, "reconcile/report.html", context)
