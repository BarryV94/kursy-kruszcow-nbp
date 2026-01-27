#!/usr/bin/env python3
# scripts/save_metals.py
# Backfill od 2002-01-01 dla złota (NBP) i srebra (Stooq), + codzienne pobranie aktualnego dnia.

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import json
import os
import sys
import tempfile
import csv
from io import StringIO
from typing import Optional, List

TZ = "Europe/Warsaw"

BASE_OUT_DIR = os.path.join("docs", "metals")
MAX_FILES_PER_DIR = 999

BACKFILL_MARKER = os.path.join(BASE_OUT_DIR, ".backfill_done")
LAST_MARKER = os.path.join(BASE_OUT_DIR, ".last")

# NBP gold endpoints
NBP_GOLD_RANGE_URL = "https://api.nbp.pl/api/cenyzlota/{start}/{end}/?format=json"
NBP_GOLD_LAST_URL = "https://api.nbp.pl/api/cenyzlota/last/1/?format=json"

# Stooq CSV base (XAGPLN)
STOOQ_CSV_BASE = "https://stooq.pl/q/d/l/?s=xagpln&i=d"

TROY_OUNCE_GRAMS = 31.1034768  # 1 troy oz = 31.1034768 g

HEADERS = {
    "User-Agent": "metals-fetcher/1.0 (+https://github.com/yourrepo)"
}

# Backfill start date (2002-01-01)
BACKFILL_START = date(2002, 1, 1)
# Chunks
NBP_CHUNK_DAYS = 365
STOOQ_CHUNK_DAYS = 365  # Stooq yearly chunks are reasonable


def ensure_base_dir():
    os.makedirs(BASE_OUT_DIR, exist_ok=True)


def existing_subdirs():
    result = []
    for name in os.listdir(BASE_OUT_DIR):
        path = os.path.join(BASE_OUT_DIR, name)
        if os.path.isdir(path) and name.isdigit():
            result.append(int(name))
    return sorted(result)


def pick_target_dir():
    subs = existing_subdirs()
    if not subs:
        target = 1
    else:
        last = subs[-1]
        last_path = os.path.join(BASE_OUT_DIR, str(last))
        count = len([f for f in os.listdir(last_path) if f.endswith(".json")])
        target = last if count < MAX_FILES_PER_DIR else last + 1
    path = os.path.join(BASE_OUT_DIR, str(target))
    os.makedirs(path, exist_ok=True)
    return path


def path_for_date(d: date):
    base = pick_target_dir()
    return os.path.join(base, d.strftime("%d_%m_%Y.json"))


def write_json_atomic(path, data):
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":"), indent=2).encode("utf-8"))
        os.replace(tmp_path, path)
        print("✅ Zapisano:", path)
        return True
    except Exception as e:
        print("❌ Błąd zapisu:", e)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False


def append_last_marker(path):
    try:
        with open(LAST_MARKER, "a", encoding="utf-8") as f:
            now_str = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{now_str}: {path}\n")
    except Exception as e:
        print("❌ Błąd zapisu .last:", e)


def http_get_text(url, timeout=60) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.HTTPError as e:
        return e
    except Exception as e:
        print("❌ HTTP error:", e)
        return e


# -----------------------
# GOLD (NBP) helpers
# -----------------------
def fetch_gold_range(start_d: date, end_d: date) -> Optional[List[dict]]:
    url = NBP_GOLD_RANGE_URL.format(start=start_d.isoformat(), end=end_d.isoformat())
    resp = http_get_text(url)
    if isinstance(resp, Exception):
        print("❌ Błąd HTTP NBP (range):", resp)
        return None
    try:
        return json.loads(resp)  # lista obiektów {Data,Cena} lub podobne
    except Exception as e:
        print("❌ Błąd parsowania JSON (NBP range):", e)
        return None


def process_gold_entry(item: dict):
    # item ma pola Data (YYYY-MM-DD) i Cena (PLN per gram)
    try:
        d_str = item.get("data") or item.get("Data") or item.get("Data")  # różne nazwy w XML/JSON
        cena = item.get("cena") or item.get("Cena") or item.get("Cena")
        if d_str is None or cena is None:
            return False
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        out_path = path_for_date(d)
        if os.path.exists(out_path):
            append_last_marker(out_path)
            return True
        payload = {
            "date": d.isoformat(),
            "gold_pln_per_g": float(cena),
            "source": "NBP"
        }
        if write_json_atomic(out_path, payload):
            append_last_marker(out_path)
            return True
    except Exception as e:
        print("❌ Błąd processing gold entry:", e)
    return False


def backfill_gold():
    print("🔁 Backfill złota (NBP) od", BACKFILL_START.isoformat())
    cur = BACKFILL_START
    today = date.today()
    while cur <= today:
        chunk_end = min(cur + timedelta(days=NBP_CHUNK_DAYS - 1), today)
        data = fetch_gold_range(cur, chunk_end)
        if data:
            for entry in data:
                process_gold_entry(entry)
        else:
            print(f"ℹ Brak danych dla zakresu {cur} — {chunk_end} (może 404)")
        cur = chunk_end + timedelta(days=1)
    print("✅ Backfill złota zakończony")


# -----------------------
# SILVER (Stooq) helpers
# -----------------------
def fetch_stooq_csv(d1: date, d2: date) -> Optional[str]:
    # Stooq supports d1/d2 in YYYYMMDD format
    d1s = d1.strftime("%Y%m%d")
    d2s = d2.strftime("%Y%m%d")
    url = f"{STOOQ_CSV_BASE}&d1={d1s}&d2={d2s}"
    resp = http_get_text(url)
    if isinstance(resp, Exception):
        print("❌ Błąd HTTP Stooq:", resp)
        return None
    return resp


def parse_stooq_csv_and_write(csv_text: str):
    try:
        buf = StringIO(csv_text)
        reader = csv.DictReader(buf)
        rows = [r for r in reader if any((v or "").strip() != "" for v in r.values())]
        if not rows:
            return 0
        written = 0
        for row in rows:
            date_str = row.get("Date") or row.get("date")
            close = row.get("Close") or row.get("close")
            if not date_str or not close:
                # spróbuj alternatywnie w wypadku innego formatu nagłówków
                vals = list(row.values())
                if len(vals) >= 5:
                    date_str = vals[0]
                    close = vals[4]
            if not date_str or not close:
                continue
            close = str(close).replace(",", ".")
            try:
                close_val = float(close)
            except:
                continue
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            out_path = path_for_date(d)
            if os.path.exists(out_path):
                # rozszerz istniejący plik o pole silver (jeśli brak)
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}
                changed = False
                if "silver_pln_per_oz" not in existing:
                    existing["silver_pln_per_oz"] = close_val
                    existing["silver_pln_per_g"] = round(close_val / TROY_OUNCE_GRAMS, 6)
                    existing.setdefault("sources", {})["silver"] = "Stooq"
                    if write_json_atomic(out_path, existing):
                        append_last_marker(out_path)
                        written += 1
                else:
                    # już jest silver -> pomiń
                    pass
            else:
                payload = {
                    "date": d.isoformat(),
                    "gold_pln_per_g": None,
                    "silver_pln_per_oz": close_val,
                    "silver_pln_per_g": round(close_val / TROY_OUNCE_GRAMS, 6),
                    "sources": {"silver": "Stooq"},
                    "fetched_at": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
                }
                if write_json_atomic(out_path, payload):
                    append_last_marker(out_path)
                    written += 1
        return written
    except Exception as e:
        print("❌ Błąd parsowania CSV Stooq:", e)
        return 0


def backfill_silver():
    print("🔁 Backfill srebra (Stooq) od", BACKFILL_START.isoformat())
    cur = BACKFILL_START
    today = date.today()
    while cur <= today:
        chunk_end = min(cur + timedelta(days=STOOQ_CHUNK_DAYS - 1), today)
        csv_text = fetch_stooq_csv(cur, chunk_end)
        if csv_text:
            got = parse_stooq_csv_and_write(csv_text)
            print(f"ℹ Zapisano {got} rekordów srebra dla zakresu {cur} - {chunk_end}")
        else:
            print(f"ℹ Brak CSV Stooq dla {cur} - {chunk_end}")
        cur = chunk_end + timedelta(days=1)
    print("✅ Backfill srebra zakończony")


# -----------------------
# Daily fetch (today)
# -----------------------
def fetch_gold_latest():
    resp = http_get_text(NBP_GOLD_LAST_URL)
    if isinstance(resp, Exception):
        print("❌ Błąd pobierania złota (last):", resp)
        return None
    try:
        data = json.loads(resp)
        if not data:
            return None
        item = data[0]
        d_str = item.get("data") or item.get("Data")
        cena = item.get("cena") or item.get("Cena")
        if not d_str or cena is None:
            return None
        return {"date": d_str, "gold_pln_per_g": float(cena)}
    except Exception as e:
        print("❌ Błąd parsowania NBP last JSON:", e)
        return None


def fetch_silver_latest():
    # pobierz ostatnie 30 dni i wybierz ostatni w CSV
    csv_text = fetch_stooq_csv(date.today() - timedelta(days=30), date.today())
    if not csv_text:
        return None
    try:
        buf = StringIO(csv_text)
        reader = csv.DictReader(buf)
        rows = [r for r in reader if any((v or "").strip() != "" for v in r.values())]
        if not rows:
            return None
        last = rows[-1]
        date_str = last.get("Date") or last.get("date")
        close = last.get("Close") or last.get("close")
        if not date_str or not close:
            vals = list(last.values())
            if len(vals) >= 5:
                date_str = vals[0]
                close = vals[4]
        close = str(close).replace(",", ".")
        close_val = float(close)
        return {"date": date_str, "silver_pln_per_oz": close_val, "silver_pln_per_g": round(close_val / TROY_OUNCE_GRAMS, 6)}
    except Exception as e:
        print("❌ Błąd parsowania CSV Stooq (latest):", e)
        return None


def process_today_and_save():
    ensure_base_dir()
    today = datetime.now(ZoneInfo(TZ)).date()
    out_path = path_for_date(today)
    if os.path.exists(out_path):
        append_last_marker(out_path)
        print("✔ Dzienny plik już istnieje:", out_path)
        return True

    gold = fetch_gold_latest()
    silver = fetch_silver_latest()

    payload = {
        "date": today.isoformat(),
        "gold_pln_per_g": gold["gold_pln_per_g"] if gold else None,
        "silver_pln_per_g": silver["silver_pln_per_g"] if silver else None,
        "silver_pln_per_oz": silver["silver_pln_per_oz"] if silver else None,
        "sources": {
            "gold": "NBP",
            "silver": "Stooq"
        },
        "fetched_at": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    }

    if write_json_atomic(out_path, payload):
        append_last_marker(out_path)
        return True
    return False


def main():
    ensure_base_dir()
    # Backfill tylko jeśli nie było jeszcze backfilla (marker)
    if not os.path.exists(BACKFILL_MARKER):
        backfill_gold()
        backfill_silver()
        with open(BACKFILL_MARKER, "w", encoding="utf-8") as f:
            f.write(datetime.utcnow().isoformat())
        print("✅ Pełny backfill wykonany, utworzono marker.")
    else:
        print("✔ Backfill już wykonany - pomijam.")

    # potem zrób codzienny fetch
    ok = process_today_and_save()
    if not ok:
        print("❌ Nie udało się zapisać danych dziennych.")
        sys.exit(1)
    print("✅ Gotowe.")
    sys.exit(0)


if __name__ == "__main__":
    main()
