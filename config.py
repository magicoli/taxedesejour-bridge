"""Load credentials and site constants from pa.toml."""

import sys
from pathlib import Path

PA_TOML = Path.home() / ".claude" / "pa.toml"

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _load() -> dict:
    with open(PA_TOML, "rb") as f:
        return tomllib.load(f)


_cfg = _load()

# ── taxesejour.fr ─────────────────────────────────────────────────────────────
TAXESEJOUR = _cfg["nordbasseterre"]["taxesejour"]["fr"]
TS_URL      = "https://nordbasseterre.taxesejour.fr"
TS_USERNAME = TAXESEJOUR["username"]
TS_PASSWORD = TAXESEJOUR["password"]
TS_HOST_ID    = 1695931
TS_LODGING_ID = 2216111
TS_REGISTRE_ID = 1248441  # unique registre for "Gîtes Mosaïques"

# ── Beds24 ────────────────────────────────────────────────────────────────────
_b24 = _cfg["mosaiques"]["beds24"]
BEDS24_API_URL  = "https://api.beds24.com/json/"
BEDS24_API_KEY  = _b24["api_key"]
BEDS24_PROP_KEY = _b24["prop_key"]

# Room IDs for each gîte
BEDS24_ROOMS = {
    "Moon":    552313,
    "Sun":     552316,
    "Zandoli": 552315,
    "Violeta": 552314,
    "Zetoil":  552312,
}

# apiSource codes for platforms that collect taxe de séjour on our behalf
# These bookings should NOT be declared by us.
PLATFORM_API_SOURCES = {
    "19",   # Booking.com
    "29",   # Airbnb iCal
    "46",   # Airbnb API
    # add Expedia code here when known
}
