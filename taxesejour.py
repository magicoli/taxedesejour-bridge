"""taxesejour.fr HTTP client — login, read declared stays, submit new stay."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

import requests

from config import (
    TS_HOST_ID,
    TS_LODGING_ID,
    TS_PASSWORD,
    TS_REGISTRE_ID,
    TS_URL,
    TS_USERNAME,
)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class DeclaredStay:
    start_date: date
    end_date: date
    label: str = ""

    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days


# ── HTML parsing helpers ───────────────────────────────────────────────────────

class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {"action": attrs.get("action", ""), "fields": {}}
            self.forms.append(self._current)
        elif tag == "input" and self._current:
            name = attrs.get("name", "")
            if name:
                self._current["fields"][name] = attrs.get("value", "")


def _parse_first_form(html: str) -> dict:
    p = _FormParser()
    p.feed(html)
    return p.forms[0] if p.forms else {"action": "", "fields": {}}


def _extract_csrf(html: str, prefix: str = "stay") -> str:
    m = re.search(rf'name="{re.escape(prefix)}\[_token\]" value="([^"]+)"', html)
    return m.group(1) if m else ""


def _extract_calendar_events(html: str) -> list[DeclaredStay]:
    """Parse existing stays from the webix-calendar events JSON embedded in the form."""
    m = re.search(r'data-webix-calendar-events-value="([^"]+)"', html)
    if not m:
        return []
    raw = m.group(1).replace("&#x2F;", "/").replace("&amp;", "&")
    # HTML entity decode
    for ent, char in [("&quot;", '"'), ("&#x7B;", "{"), ("&#x7D;", "}"),
                      ("&#x5B;", "["), ("&#x5D;", "]"), ("&#x22;", '"')]:
        raw = raw.replace(ent, char)
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        return []

    stays = []
    for ev in events:
        try:
            # Calendar encodes dates as "YYYY/MM/DD"; normalise to ISO format
            start = date.fromisoformat(ev["startDate"].replace("/", "-"))
            end   = date.fromisoformat(ev["endDate"].replace("/", "-"))
            stays.append(DeclaredStay(start_date=start, end_date=end, label=ev.get("name", "")))
        except (KeyError, ValueError):
            continue
    return stays


# ── Client ────────────────────────────────────────────────────────────────────

class TaxeSejourClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        self._logged_in = False

    def login(self) -> None:
        r = self._session.get(f"{TS_URL}/dashboard", allow_redirects=True)
        form = _parse_first_form(r.text)
        self._session.post(
            form["action"],
            data={"username": TS_USERNAME, "password": TS_PASSWORD, "login": "Se connecter"},
            allow_redirects=True,
        )
        self._logged_in = True

    def _ensure_logged_in(self):
        if not self._logged_in:
            self.login()

    def _get_frame(self, path: str, frame: str) -> str:
        self._ensure_logged_in()
        r = self._session.get(
            f"{TS_URL}{path}",
            headers={"Turbo-Frame": frame, "Accept": "text/html, application/xhtml+xml"},
        )
        r.raise_for_status()
        return r.text

    # ── Reading ────────────────────────────────────────────────────────────────

    def get_declared_stays(self, year: int, month: int) -> list[DeclaredStay]:
        """Return stays already declared for the given month.

        Reads the calendar events JSON embedded in the "add stay" form,
        then filters to the requested month.
        """
        month_str = f"{year}-{month:02d}-01"
        path = f"/v2/host/stay/new/{TS_REGISTRE_ID}?month={month_str}"
        html = self._get_frame(path, "stay-form-frame")
        # When the frame isn't served (full page), fall back to GET
        if "webix-calendar-events-value" not in html:
            self._ensure_logged_in()
            r = self._session.get(f"{TS_URL}{path}")
            html = r.text

        all_stays = _extract_calendar_events(html)
        return [s for s in all_stays if s.start_date.year == year and s.start_date.month == month]

    def get_declared_date_set(self) -> set[tuple]:
        """Return {(start_date, end_date)} of ALL stays already declared on the site.

        Read from the calendar embedded in the stay form (covers every period,
        not just one month). Used to avoid creating duplicate declarations for
        stays that already exist on the site — including ones added manually.
        """
        path = f"/v2/host/stay/new/{TS_REGISTRE_ID}?month={date.today().strftime('%Y-%m-01')}"
        html = self._get_frame(path, "stay-form-frame")
        if "webix-calendar-events-value" not in html:
            self._ensure_logged_in()
            html = self._session.get(f"{TS_URL}{path}").text
        return {(s.start_date, s.end_date) for s in _extract_calendar_events(html)}

    def get_month_status(self, year: int, month: int) -> str:
        """Return declaration status string for the month (e.g. 'À déclarer', 'Déclaré')."""
        # We need to find the period ID for this year first
        periods = self._find_periods(year)
        for period_id, months in periods.items():
            month_str = f"{year}-{month:02d}-01"
            if month_str in months:
                return months[month_str]
        return "unknown"

    def _find_periods(self, year: int) -> dict[str, dict[str, str]]:
        """Map period_id → {month_str: status_label}."""
        html = self._get_frame(
            f"/v2/host/declarations/index/{TS_HOST_ID}",
            "sidesheet-frame",
        )
        period_ids = re.findall(
            rf"/v2/host/declarations/index/{TS_HOST_ID}/{TS_LODGING_ID}/{year}/(\d+)",
            html,
        )
        if not period_ids:
            return {}

        result: dict[str, dict[str, str]] = {}
        for pid in set(period_ids):
            month_html = self._get_frame(
                f"/v2/host/declarations/index/{TS_HOST_ID}/{TS_LODGING_ID}/{year}/{pid}",
                "sidesheet-frame",
            )
            months_in_period: dict[str, str] = {}
            month_links = re.findall(
                rf'href="(/v2/host/declarations/index/{TS_HOST_ID}/{TS_LODGING_ID}/{year}/{pid}/(\d{{4}}-\d{{2}}-\d{{2}}))"',
                month_html,
            )
            raw_statuses = re.findall(r'nt-list-item-badge-label">\s*([^<]+)\s*</span>', month_html)
            for (_, month_date), status in zip(month_links, raw_statuses):
                months_in_period[month_date] = status.strip()
            result[pid] = months_in_period

        return result

    def get_pending_months(self, year: int) -> list[tuple[int, int, str]]:
        """Return (year, month, period_id) tuples for months with status 'À déclarer'.

        Skips 'Déclaré' (already closed) and 'En anticipation' (future).
        """
        periods = self._find_periods(year)
        pending = []
        for pid, months in periods.items():
            for month_str, status in sorted(months.items()):
                if "déclarer" in status.lower():
                    y, m, _ = month_str.split("-")
                    pending.append((int(y), int(m), pid))
        return sorted(pending)

    def get_stay_ids(self, year: int, month: int, period_id: str) -> set[str]:
        """Return the set of stay IDs already declared for the given month."""
        month_str = f"{year}-{month:02d}-01"
        path = (f"/v2/host/declarations/index/{TS_HOST_ID}/{TS_LODGING_ID}"
                f"/{year}/{period_id}/{month_str}")
        html = self._get_frame(path, "right-turbo-pane")
        return set(re.findall(
            rf'/v2/host/declarations/index/{TS_HOST_ID}/{TS_LODGING_ID}'
            rf'/{year}/{period_id}/{month_str}/stay/(\d+)',
            html,
        ))

    # ── Writing ────────────────────────────────────────────────────────────────

    def add_stay(
        self,
        month: date,
        period_id: str,
        check_in: date,
        check_out: date,
        adults: int,
        children: int,
        amount: float,
    ) -> str:
        """Submit a new stay declaration (3-step form).

        Returns the taxesejour.fr stay ID assigned to the new declaration,
        or "" if it could not be captured (submission still happened).
        Raises on HTTP or validation error.
        """
        month_str = f"{month.year}-{month.month:02d}-01"
        base = f"/v2/host/stay/new/{TS_REGISTRE_ID}"

        self._ensure_logged_in()

        # Snapshot stay IDs before submission so we can diff after
        ids_before = self.get_stay_ids(month.year, month.month, period_id)

        # ── Step 1: dates ──────────────────────────────────────────────────────
        r1 = self._session.get(f"{TS_URL}{base}?month={month_str}")
        csrf1 = _extract_csrf(r1.text)
        if not csrf1:
            raise RuntimeError("Could not extract CSRF token from step 1")

        r2 = self._session.post(
            f"{TS_URL}{base}?host={TS_HOST_ID}&step=1",
            data={
                "stay[dates][startDate]":  check_in.isoformat(),
                "stay[dates][endDate]":    check_out.isoformat(),
                "stay[dayOfArrival]":      check_in.isoformat(),
                "stay[dayOfDeparture]":    check_out.isoformat(),
                "stay[_token]":            csrf1,
            },
            headers={"Turbo-Frame": "stay-form-frame"},
        )
        r2.raise_for_status()
        csrf2 = _extract_csrf(r2.text)
        if not csrf2:
            raise RuntimeError("Could not extract CSRF token from step 2")

        # ── Step 2: occupants ──────────────────────────────────────────────────
        total = adults + children
        r3 = self._session.post(
            f"{TS_URL}{base}?host={TS_HOST_ID}&step=2",
            data={
                "stay[countPersonTaxable]":    adults,
                "stay[countMinor]":            children,
                "stay[countJobSeasonal]":      0,
                "stay[countUrgency]":          0,
                "stay[countLowRent]":          0,
                "stay[countPersonNonTaxable]": 0,
                "stay[countPerson]":           total,
                "stay[_token]":                csrf2,
            },
            headers={"Turbo-Frame": "stay-form-frame"},
        )
        r3.raise_for_status()
        csrf3 = _extract_csrf(r3.text)
        if not csrf3:
            raise RuntimeError("Could not extract CSRF token from step 3")

        # ── Step 3: amount ─────────────────────────────────────────────────────
        # French site: decimal separator must be a comma.
        amount_fr = f"{amount:.2f}".replace(".", ",")
        r4 = self._session.post(
            f"{TS_URL}{base}?host={TS_HOST_ID}&step=3",
            data={
                "stay[amount]":       amount_fr,
                "stay[nightPrice]":   "",
                "stay[isNightPrice]": "",
                "stay[_token]":       csrf3,
            },
            headers={"Turbo-Frame": "stay-form-frame"},
        )
        r4.raise_for_status()

        # Success check
        if "erreur" in r4.text.lower() or "error" in r4.text.lower():
            raise RuntimeError(f"Server returned an error on step 3: {r4.text[:300]}")

        # Capture the new stay ID by diffing before/after
        ids_after = self.get_stay_ids(month.year, month.month, period_id)
        new_ids = ids_after - ids_before
        return next(iter(new_ids), "")
