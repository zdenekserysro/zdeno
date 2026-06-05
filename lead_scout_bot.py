#!/usr/bin/env python3
"""
Lead-scouting bot: scrapes Facebook groups via Apify,
filters Czech marketing-demand posts, logs to Google Sheet.
"""

import os
import re
import time
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

# Načti .env soubor pokud existuje
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ──────────────────────────────────────────────────────────────────
APIFY_TOKEN    = os.environ["APIFY_TOKEN"]
APIFY_ACTOR_ID = os.environ["APIFY_ACTOR_ID"]
GOOGLE_SHEET_URL = os.environ["GOOGLE_SHEET_URL"]

TARGET_GROUPS = [
    "https://www.facebook.com/groups/737452216307272",
    "https://www.facebook.com/groups/online.marketing.pro.zivnostniky/",
    "https://www.facebook.com/groups/marketingabranding/",
    "https://www.facebook.com/groups/880830738610292",
    "https://www.facebook.com/groups/2095153874099421",
    "https://www.facebook.com/groups/310025138111994/",
    "https://www.facebook.com/groups/287664108356109/",
    "https://www.facebook.com/groups/463605933996976/",
    "https://www.facebook.com/groups/2495561120477741/",
    # 417445449007644 a 291557751321339 jsou privátní skupiny — vynechány
]

DEMAND_SIGNALS = [
    "poptávám", "hledám", "sháním", "potřebuji", "kdo umí",
    "doporučte", "hledáme", "přijmeme", "nabízíme práci",
    "brigáda", "sháníme někoho", "poptávka", "hledá se",
]

RELEVANT_TOPICS = [
    "marketing", "marketingový", "ppc", "meta ads", "google ads",
    "reklam", "správa reklam", "tvorba webu", "webové stránky",
    "web na míru", "e-shop", "eshop", "landing page",
    "email marketing", "newsletter", "mailing", "copywriting",
    "obsah", "content", "sociální sítě", "grafika", "seo",
    "video", "reels", "střih", "tvorba videí",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Apify ────────────────────────────────────────────────────────────────────

def scrape_groups() -> list[dict]:
    """Run Apify actor; return raw dataset items."""
    base_url = "https://api.apify.com/v2"
    headers  = {"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"}
    payload  = {
        "startUrls": [{"url": u} for u in TARGET_GROUPS],
        "resultsLimit": 200,
        "viewOption": "CHRONOLOGICAL",
    }

    # Merge FB cookies from env if provided (export from browser via Cookie-Editor extension)
    fb_cookies_raw = os.environ.get("FB_COOKIES", "")
    if fb_cookies_raw:
        import json as _json
        try:
            payload["cookies"] = _json.loads(fb_cookies_raw)
        except Exception:
            print("WARN: FB_COOKIES není validní JSON, ignoruji.")

    # Try sync run first (300 s limit)
    sync_url = f"{base_url}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?timeout=300"
    print("Spouštím Apify actor (sync)…")
    resp = requests.post(sync_url, json=payload, headers=headers, timeout=330)

    if resp.status_code == 200:
        return resp.json()

    # Sync timed out → async run + poll
    print("Sync timeout, přepínám na async…")
    run_url = f"{base_url}/acts/{APIFY_ACTOR_ID}/runs"
    run_resp = requests.post(run_url, json=payload, headers=headers, timeout=30)
    run_resp.raise_for_status()
    run_id = run_resp.json()["data"]["id"]

    for attempt in range(60):
        time.sleep(15)
        status_resp = requests.get(f"{base_url}/actor-runs/{run_id}", headers=headers, timeout=15)
        status = status_resp.json()["data"]["status"]
        print(f"  Run status: {status} (pokus {attempt + 1})")
        if status == "SUCCEEDED":
            dataset_id = status_resp.json()["data"]["defaultDatasetId"]
            items_resp = requests.get(
                f"{base_url}/datasets/{dataset_id}/items", headers=headers, timeout=30
            )
            items_resp.raise_for_status()
            return items_resp.json()
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run skončil se stavem: {status}")

    raise RuntimeError("Apify run neprobyl do 15 minut.")


# ── Filter ───────────────────────────────────────────────────────────────────

def _text(post: dict) -> str:
    return (post.get("text") or post.get("message") or "").lower()

def _is_recent(post: dict, cutoff: datetime.datetime) -> bool:
    raw = post.get("time") or post.get("timestamp") or post.get("date") or ""
    if not raw:
        return True  # unknown → keep
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.datetime.utcfromtimestamp(raw).replace(tzinfo=datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt >= cutoff
    except Exception:
        return True

def is_lead(post: dict) -> bool:
    text = _text(post)
    has_signal = any(s in text for s in DEMAND_SIGNALS)
    has_topic  = any(t in text for t in RELEVANT_TOPICS)
    return has_signal and has_topic

def filter_posts(items: list[dict]) -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    return [p for p in items if _is_recent(p, cutoff) and is_lead(p)]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sheet_id_from_url(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Nelze získat sheet ID z URL: {url}")
    return m.group(1)

def _email_in_text(text: str) -> str:
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return m.group(0) if m else ""

def _short_desc(post: dict) -> str:
    text = (post.get("text") or post.get("message") or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:150]

def _post_url(post: dict) -> str:
    return post.get("url") or post.get("facebookUrl") or post.get("link") or ""

def _group_name(post: dict) -> str:
    return post.get("groupTitle") or post.get("title") or ""


# ── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet_client():
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)

def write_to_sheet(leads: list[dict]):
    gc         = get_sheet_client()
    sheet_id   = _sheet_id_from_url(GOOGLE_SHEET_URL)
    spreadsheet = gc.open_by_key(sheet_id)
    today      = datetime.date.today().isoformat()  # YYYY-MM-DD

    # Get or create today's tab
    try:
        ws = spreadsheet.worksheet(today)
        print(f"Tab '{today}' už existuje, přidávám…")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=today, rows=500, cols=5)
        print(f"Vytvářím nový tab '{today}'…")
        ws.append_row(["Popis", "Odkaz", "Skupina", "Email", "Hotovo"])

    if not leads:
        ws.append_row(["Žádné nové poptávky za posledních 24 h.", "", "", "", ""])
        print("Žádné leady — zapsán prázdný řádek.")
        return

    rows = []
    for post in leads:
        text    = post.get("text") or post.get("message") or ""
        url     = _post_url(post)
        hyperlink = f'=HYPERLINK("{url}";"ODKAZ")' if url else ""
        rows.append([
            _short_desc(post),
            hyperlink,
            _group_name(post),
            _email_in_text(text),
            False,  # checkbox
        ])

    # Append rows
    ws.append_rows(rows, value_input_option="USER_ENTERED")

    # Add checkboxes to column E for new rows
    last_header_row = 1
    existing = len(ws.get_all_values())
    new_start = existing - len(rows) + 1
    e_range = f"E{new_start}:E{existing}"
    ws.format(e_range, {"dataValidation": {
        "condition": {"type": "BOOLEAN"},
        "strict": True,
        "showCustomUi": True,
    }})

    print(f"Zapsáno {len(leads)} leadů do sheetu.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Lead Scout Bot | {datetime.datetime.now().isoformat()} ===")

    print("\n[1/3] Scraping Facebook groups via Apify…")
    items = scrape_groups()
    print(f"  Staženo {len(items)} příspěvků.")

    print("\n[2/3] Filtrování leadů…")
    leads = filter_posts(items)
    print(f"  Nalezeno {len(leads)} relevantních poptávek.")

    print("\n[3/3] Zápis do Google Sheetu…")
    write_to_sheet(leads)

    print("\nHotovo!")


if __name__ == "__main__":
    main()
