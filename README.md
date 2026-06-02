# Balance Reconciliation Web App

A small Django app for investigating balance discrepancies between a transaction ledger CSV and daily bank statement balance CSVs. Upload both files, get a per-day reconciliation report with warnings, a comparison chart, and mismatch highlighting.

## Architecture

<img width="4801" height="6447" alt="CSV Transaction-2026-06-02-202022" src="https://github.com/user-attachments/assets/185f65c0-8f2f-436b-a3dc-34a0180f37d3" />


## Reconciliation logic

Reconciliation runs in `reconcile/services.py` and compares **cumulative expected balance** to **bank-reported balance** on each statement date.

1. **Aggregate transactions by date** — Sum all `amount` values that share the same `date` (multiple rows per day are allowed).

2. **Walk bank statement dates in order** — For each row in the bank balances CSV:
   - If that date has transactions, add that day’s total to a **running expected balance**.
   - If that date has no transactions, the running expected balance **carries forward unchanged**.

3. **Flag each day** — Let `difference = bank_balance − expected_balance`. The day **reconciles** when `|difference| ≤ $0.01` (configurable via `tolerance` in `reconcile()`).

4. **Warnings (non-fatal)** — Transaction rows with missing/invalid `date` or `amount` are skipped and listed as warnings. Transaction dates before the first or after the last bank statement date also produce warnings. Bank balance rows must parse cleanly; otherwise parsing fails with an error.

The report shows per-day txn count, daily txn total, cumulative expected balance, bank balance, difference, and OK/Mismatch status. Sample data in `sample_data/` intentionally diverges starting **2025-06-07** for demo purposes.

## CSV format

| File | Required columns | Notes |
|------|------------------|--------|
| **Transactions** | `date`, `amount` | ISO dates (`YYYY-MM-DD`). Signed decimals (debits negative). Header names are case-insensitive; UTF-8 BOM supported. |
| **Bank balances** | `date`, `balance` | End-of-day balances. `amount` is accepted as an alias for `balance`. |

Empty lines are ignored. Max upload size: **2 MB** per file.

## Project structure

```
├── app.py                 # Django management entrypoint (runserver, test, …)
├── config/                # Django settings, root URLs, WSGI
├── reconcile/             # App: services, views, forms, templates, tests
│   ├── services.py        # Parsing + reconciliation
│   ├── views.py           # Upload + report
│   ├── forms.py           # File validation
│   └── test.py            # Unit / integration tests
├── sample_data/           # Example transactions.csv, bank_balances.csv
└── requirements.txt
```

There is no ORM persistence for uploads; each POST is parsed in memory and rendered once.

## Prerequisites

- **Python 3.11+** (3.11 tested)
- `pip`

## Local setup

```bash
# Clone or cd into the project directory
cd Collective-Take-Home

# Create and activate a virtual environment
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

## Run the web app

```bash
python app.py runserver
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/), upload the two CSVs, and submit **Run reconciliation**.

To use the bundled samples, upload `sample_data/transactions.csv` and `sample_data/bank_balances.csv`.

Optional: bind another host/port:

```bash
python app.py runserver 0.0.0.0:8080
```

## Run tests

```bash
python app.py test reconcile
```

Verbose output:

```bash
python app.py test reconcile -v 2
```

## Technical details

| Topic | Detail |
|-------|--------|
| **Framework** | Django 5.x (`requirements.txt`) |
| **Entrypoint** | `app.py` sets `DJANGO_SETTINGS_MODULE=config.settings` |
| **URLs** | `/` → upload form and report (`reconcile.urls`) |
| **Settings** | `config/settings.py` — `DEBUG=True`, dev `SECRET_KEY`, SQLite configured but unused for reconciliation |
| **Money math** | `decimal.Decimal` end-to-end; chart JSON uses `float` for display only |
| **Dependencies** | Chart.js 4.x loaded from CDN on the report page |

**Production:** Change `SECRET_KEY`, set `DEBUG=False`, configure `ALLOWED_HOSTS`, and serve via a production WSGI server (e.g. gunicorn + `config.wsgi:application`). Do not rely on `runserver` in production.
