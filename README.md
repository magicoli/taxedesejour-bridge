# Beds24 - TaxeSejour.fr Bridge

Reconciles direct Beds24 bookings with tourist tax declarations on
[nordbasseterre.taxesejour.fr](https://nordbasseterre.taxesejour.fr).

Platforms (Airbnb, Booking.com) collect the tourist tax on our behalf —
only **direct bookings** are declared here.

## The issue

The official tourist tax declaration for some territories relies on a web
site which provides no documented API, making it difficult to automate
and verify declarations based on actual booking status.

This project was made to fix a precise use case (bookings managed in
Beds24, declaration for one of the territories covered by taxesejour.fr)
but should be adaptable for other use cases.

## Requirements

- Python 3.11+ including matching `python3.xx-venv` (e.g. `sudo apt install python3.11 python3.11-venv`)

## Setup

```bash
cp config.toml.example config.toml
# fill in your credentials (see comments in the file)
./run.sh --dry-run
```

Note: `run.sh` creates the virtualenv and runs `pip install -r requirements.txt` automatically on first run — no manual pip step needed -- and activates virtualenv automatically on every run.

### config.toml

```toml
[register]
url      = "https://nordbasseterre.taxesejour.fr"
username = "your_login"
password = "your_password"

[sources.beds24]
api_key  = "..."
# Beds24 → SETTINGS > ACCOUNT > ACCOUNT ACCESS > API Key
prop_key = "..."
# Beds24 → SETTINGS > PROPERTIES > ACCESS > propKey  (v1 API, see TODO)
```

## Usage

```bash
./run.sh                        # process all pending months (submit/update)
./run.sh --month 2026-05        # specific month only
./run.sh --dry-run              # report only, no submissions
./run.sh --dry-run --month 2026-05
./run.sh --no-beds24-note       # skip writing to Beds24 custom1 field
```

Only pending declarations are considered. Submissions are not final and
can be reviewed and modified on the register site until the final monthly
submission is done manually, so it is safe to run the script regularly
without `--dry-run`.

```crontab
# Example: run at 8am on the 5th of each month
0 8 5 * * /path/to/run.sh
```

## Row statuses

| Status | Meaning |
|--------|---------|
| `add` | Will be submitted on next run |
| `ready` | Already declared, ready to submit the month on the site |
| `update` | Amount changed since last declaration — will be updated |
| `error` | Beds24 data problem (invalid status, missing occupants) — fix before running |
| `failed` | Last submission attempt failed — investigate before retrying |
| `n/a` | Platform booking or zero-amount stay — not declared |
| `-` | No action needed |

## How it works

1. Fetches bookings from Beds24 for each pending month (check-out date = declaration month).
2. Fetches already-declared stays from taxesejour.fr (Keycloak SSO login).
3. Groups multi-gîte bookings from the same client into one declaration.
4. Computes the net (HT) and tourist tax using the same per-night rounding as the site.
5. Adds missing declarations and updates changed ones automatically.
6. Writes a note to the Beds24 booking (custom1 field) after each submission.
7. Persists state in `data/declarations.json` to detect amount changes on future runs.

The script only submits individual stays — the final monthly declaration
is done on the site. Each month stays open on the site until you manually
submit the final monthly declaration ("Déclarer pour le mois de ...").

## Files

| File | Purpose |
|------|---------|
| `config.py` | Credentials, constants, tax formula (single source of truth) |
| `beds24.py` | Beds24 v1 API client — fetch bookings, parse invoices, group by client |
| `taxesejour.py` | taxesejour.fr HTTP client — login, read declared stays, submit/update/delete |
| `main.py` | Orchestration, table output, CSV export |
| `state.py` | Local state in `data/declarations.json` — tracks declared amounts |
| `data/recap.csv` | Running export of all processed rows |

## Tax formula

The tourist tax is computed to match the site to the cent:

```
tarif_nuit = round(net_ht / nights / guests, 2)
taxe_nuit  = round(tarif_nuit × 5%, 2)
taxe       = taxe_nuit × nights × adults          # children exempt
```

`net_ht` is derived from the total received (TTC) by inverting:
`total = net_ht × (1 + VAT_2.1% + 5% × adults/guests)`.

## Contributing

Bug reports and suggestions welcome via [GitHub Issues](https://github.com/magicoli/taxesejour-bridge/issues).
