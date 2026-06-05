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

# Klíčová slova pro Facebook search — každá skupina se prohledá podle každého z nich
SEARCH_KEYWORDS = [
    "hledám",
    "sháním",
    "poptávám",
    "poptávka",
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

def _run_apify(start_urls: list, base_url: str, headers: dict, cookies: list,
               since: datetime.datetime) -> list[dict]:
    """Spustí Apify actor pro dané URL s filtrem 24h, vrátí dataset items."""
    payload = {
        "startUrls": start_urls,
        "resultsLimit": 50,
        "viewOption": "CHRONOLOGICAL",
        "minPostDate": since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if cookies:
        payload["cookies"] = cookies

    run_resp = requests.post(f"{base_url}/acts/{APIFY_ACTOR_ID}/runs",
                             json=payload, headers=headers, timeout=30)
    if not run_resp.ok:
        print(f"  WARN: {run_resp.text[:100]}")
        return []
    run_id = run_resp.json()["data"]["id"]

    for attempt in range(20):
        time.sleep(10)
        st = requests.get(f"{base_url}/actor-runs/{run_id}", headers=headers, timeout=15).json()["data"]
        if st["status"] == "SUCCEEDED":
            return requests.get(
                f"{base_url}/datasets/{st['defaultDatasetId']}/items",
                headers=headers, timeout=30
            ).json()
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            print(f"  WARN: run skončil: {st['status']}")
            return []
    return []


def scrape_groups() -> list[dict]:
    """Hledá v každé skupině podle klíčových slov, jen příspěvky za posledních 24h."""
    import urllib.parse
    base_url = "https://api.apify.com/v2"
    headers  = {"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"}
    since    = datetime.datetime.utcnow() - datetime.timedelta(hours=24)

    cookies = []
    fb_cookies_raw = os.environ.get("FB_COOKIES", "")
    if fb_cookies_raw:
        import json as _json
        try:
            cookies = _json.loads(fb_cookies_raw)
        except Exception:
            print("WARN: FB_COOKIES není validní JSON, ignoruji.")

    seen_ids = set()
    all_items = []

    for group_url in TARGET_GROUPS:
        group_id = group_url.rstrip("/").split("/groups/")[-1].rstrip("/")
        for keyword in SEARCH_KEYWORDS:
            search_url = f"https://www.facebook.com/groups/{group_id}/search/?q={urllib.parse.quote(keyword)}"
            print(f"  Hledám '{keyword}' v {group_id}…")
            items = _run_apify([{"url": search_url}], base_url, headers, cookies, since)
            print(f"    → {len(items)} výsledků za 24h")
            for item in items:
                uid = item.get("id") or item.get("url", "")
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    all_items.append(item)

    return all_items


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
    # 24h filtr řeší Apify nativně přes minPostDate — tady jen filtrujeme téma
    return [p for p in items if is_lead(p)]


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

    # Append rows (FALSE v sloupci E = checkbox přes USER_ENTERED)
    ws.append_rows(rows, value_input_option="USER_ENTERED")

    # Nastav checkboxy přes Sheets API setDataValidation
    existing = len(ws.get_all_values())
    new_start = existing - len(rows) + 1
    spreadsheet = ws.spreadsheet
    spreadsheet.batch_update({"requests": [{
        "setDataValidation": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": new_start - 1,
                "endRowIndex": existing,
                "startColumnIndex": 4,
                "endColumnIndex": 5,
            },
            "rule": {
                "condition": {"type": "BOOLEAN"},
                "strict": True,
                "showCustomUi": True,
            }
        }
    }]})

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
