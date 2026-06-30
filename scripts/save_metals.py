#!/usr/bin/env python3

from datetime import date, datetime, timedelta
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

NBP_GOLD_RANGE_URL = "https://api.nbp.pl/api/cenyzlota/{start}/{end}/?format=json"
NBP_GOLD_LAST_URL = "https://api.nbp.pl/api/cenyzlota/last/1/?format=json"

STOOQ_CSV_BASE = "https://stooq.pl/q/d/l/?s=xagpln&i=d"

TROY_OUNCE_GRAMS = 31.1034768

HEADERS = {
    "User-Agent": "metals-fetcher/1.0"
}

BACKFILL_START = date(2002, 1, 1)
NBP_CHUNK_DAYS = 365
STOOQ_CHUNK_DAYS = 365


# ================= BASE =================

def ensure_base_dir():
    os.makedirs(BASE_OUT_DIR, exist_ok=True)


def existing_subdirs():
    return sorted(
        int(d) for d in os.listdir(BASE_OUT_DIR)
        if d.isdigit() and os.path.isdir(os.path.join(BASE_OUT_DIR, d))
    )


def pick_target_dir():
    subs = existing_subdirs()
    target = subs[-1] if subs else 1
    path = os.path.join(BASE_OUT_DIR, str(target))
    os.makedirs(path, exist_ok=True)

    if len([f for f in os.listdir(path) if f.endswith(".json")]) >= MAX_FILES_PER_DIR:
        target += 1
        path = os.path.join(BASE_OUT_DIR, str(target))
        os.makedirs(path, exist_ok=True)

    return path


def path_for_date(d: date):
    base = pick_target_dir()
    return os.path.join(base, d.strftime("%d_%m_%Y.json"))


def write_json_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print("✅ Zapisano:", path)
    return True


def append_last_marker(path):
    with open(LAST_MARKER, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(ZoneInfo(TZ))}: {path}\n")


def http_get_text(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


# ================= GOLD (NBP) =================

def fetch_gold_range(start_d, end_d):
    url = NBP_GOLD_RANGE_URL.format(start=start_d, end=end_d)
    return json.loads(http_get_text(url))


def process_gold_entry(item):
    d = datetime.strptime(item["data"], "%Y-%m-%d").date()
    out = path_for_date(d)
    if os.path.exists(out):
        return

    write_json_atomic(out, {
        "date": d.isoformat(),
        "gold_pln_per_g": float(item["cena"]),
        "source": "NBP"
    })


def backfill_gold():
    cur = BACKFILL_START
    today = date.today()
    while cur <= today:
        end = min(cur + timedelta(days=NBP_CHUNK_DAYS - 1), today)
        for item in fetch_gold_range(cur, end):
            process_gold_entry(item)
        cur = end + timedelta(days=1)


# ================= SILVER (Stooq) =================

def fetch_stooq_csv(d1, d2):
    url = f"{STOOQ_CSV_BASE}&d1={d1:%Y%m%d}&d2={d2:%Y%m%d}"
    return http_get_text(url)


def parse_stooq_csv_and_write(csv_text):
    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        if not row.get("Date") or not row.get("Close"):
            continue

        d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
        out = path_for_date(d)

        try:
            price_oz = float(row["Close"].replace(",", "."))
        except:
            continue

        if os.path.exists(out):
            with open(out, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"date": d.isoformat()}

        data["silver_pln_per_oz"] = price_oz
        data["silver_pln_per_g"] = round(price_oz / TROY_OUNCE_GRAMS, 6)
        data.setdefault("sources", {})["silver"] = "Stooq"

        write_json_atomic(out, data)


def backfill_silver():
    cur = BACKFILL_START
    today = date.today()
    while cur <= today:
        end = min(cur + timedelta(days=STOOQ_CHUNK_DAYS - 1), today)
        parse_stooq_csv_and_write(fetch_stooq_csv(cur, end))
        cur = end + timedelta(days=1)


# ================= INDEX.JSON =================

def rebuild_metals_index():
    index = {
        "metal": "gold",
        "unit": "PLN_per_gram",
        "source": "NBP",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d"),
        "days": {}
    }

    for folder in sorted(os.listdir(BASE_OUT_DIR)):
        if not folder.isdigit():
            continue

        folder_path = os.path.join(BASE_OUT_DIR, folder)
        for file in os.listdir(folder_path):
            if not file.endswith(".json"):
                continue

            file_path = os.path.join(folder_path, file)

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            date_str = data.get("date")
            gold = data.get("gold_pln_per_g")

            # ✅ KLUCZOWA POPRAWKA: pomijamy None
            if date_str and gold is not None:
                index["days"][date_str] = {
                    "gold_pln_per_g": gold,
                    "path": f"{folder}/{file}"
                }

    index["days"] = dict(sorted(index["days"].items()))

    with open(os.path.join(BASE_OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"📦 index.json: {len(index['days'])} dni")


# ================= MAIN =================

def main():
    ensure_base_dir()

    if not os.path.exists(BACKFILL_MARKER):
        backfill_gold()
        backfill_silver()
        with open(BACKFILL_MARKER, "w", encoding="utf-8") as f:
            f.write("done")

    rebuild_metals_index()
    print("✅ Gotowe")


if __name__ == "__main__":
    main()
