#!/usr/bin/env python3
# scripts/save_metals.py

from datetime import datetime, date
from zoneinfo import ZoneInfo
import requests
import json
import os
import sys
import tempfile
import csv
from io import StringIO

TZ = "Europe/Warsaw"

BASE_OUT_DIR = os.path.join("docs", "metals")
MAX_FILES_PER_DIR = 999

BACKFILL_MARKER = os.path.join(BASE_OUT_DIR, ".backfill_done")
LAST_MARKER = os.path.join(BASE_OUT_DIR, ".last")

NBP_GOLD_LAST_URL = "https://api.nbp.pl/api/cenyzlota/last/1/?format=json"
# Stooq CSV endpoint for XAG/PLN (daily). Returns CSV with Date,Open,High,Low,Close,Volume
STOOQ_XAG_PLN_CSV = "https://stooq.pl/q/d/l/?s=xagpln&i=d"

TROY_OUNCE_GRAMS = 31.1034768  # 1 troy oz = 31.1034768 g

HEADERS = {
    "User-Agent": "metals-fetcher/1.0 (+https://github.com/yourrepo)"
}

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

def http_get_text(url, timeout=30):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.HTTPError as e:
        return e
    except Exception as e:
        print("❌ HTTP error:", e)
        return e

def fetch_gold_nbp():
    """Pobiera ostatnią cenę złota z NBP (PLN za 1 g)."""
    resp = http_get_text(NBP_GOLD_LAST_URL)
    if isinstance(resp, Exception):
        return None
    try:
        data = json.loads(resp)
        if not data:
            return None
        # data is a list of objects like {"data":"2026-01-27","cena":580.2}
        item = data[0]
        d_str = item.get("data") or item.get("Data")  # be safe
        cena = item.get("cena") or item.get("Cena")
        if d_str is None or cena is None:
            return None
        return {"date": d_str, "gold_pln_per_g": float(cena)}
    except Exception as e:
        print("❌ Błąd parsowania JSON (gold):", e)
        return None

def fetch_silver_stooq():
    """Pobiera ostatnią cenę srebra z Stooq (XAGPLN) - zwykle w PLN/uncja (troy oz).
       Konwertuje do PLN/gram."""
    resp = http_get_text(STOOQ_XAG_PLN_CSV)
    if isinstance(resp, Exception) or not isinstance(resp, str):
        print("❌ Błąd pobierania danych srebra ze Stooq:", resp)
        return None
    try:
        buf = StringIO(resp)
        reader = csv.DictReader(buf)
        rows = [r for r in reader if any(v.strip() != "" for v in r.values())]
        if not rows:
            print("ℹ Stooq zwrócił pusty CSV")
            return None
        last = rows[-1]
        # typowe nagłówki: Date,Open,High,Low,Close,Volume
        date_str = last.get("Date") or last.get("date") or last.get("DATA")
        close = last.get("Close") or last.get("close") or last.get("Close.")
        if close is None or close == '':
            # czasami separatory i lokalizacja mogą być inne; spróbuj wstępnego czyszczenia:
            values = list(last.values())
            if len(values) >= 5:
                close = values[4]
        if close is None or close == '':
            print("❌ Nie udało się znaleźć wartości Close w CSV Stooq")
            return None
        # zamień przecinek na kropkę jeżeli potrzeba
        close = str(close).replace(",", ".")
        close_val = float(close)
        silver_pln_per_g = close_val / TROY_OUNCE_GRAMS
        return {"date": date_str, "silver_pln_per_g": round(silver_pln_per_g, 6), "silver_pln_per_oz": close_val}
    except Exception as e:
        print("❌ Błąd parsowania CSV (srebro):", e)
        return None

def process_and_save():
    ensure_base_dir()
    today = datetime.now(ZoneInfo(TZ)).date()
    out_path = path_for_date(today)
    if os.path.exists(out_path):
        append_last_marker(out_path)
        print("✔ Plik na dziś już istnieje:", out_path)
        return True

    gold = fetch_gold_nbp()
    silver = fetch_silver_stooq()

    payload = {
        "date": today.isoformat(),
        "gold_pln_per_g": gold["gold_pln_per_g"] if gold else None,
        "silver_pln_per_g": silver["silver_pln_per_g"] if silver else None,
        "silver_pln_per_oz": silver["silver_pln_per_oz"] if silver else None,
        "sources": {
            "gold": "NBP api /api/cenyzlota/",
            "silver": "Stooq xagpln CSV"
        },
        "fetched_at": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
    }

    if write_json_atomic(out_path, payload):
        append_last_marker(out_path)
        return True
    return False

def main():
    ok = process_and_save()
    if not ok:
        print("❌ Nie udało się zapisać danych.")
        sys.exit(1)
    print("✅ Gotowe.")
    sys.exit(0)

if __name__ == "__main__":
    main()
