"""
Gridlock NEM worker (week 1)

Pulls AEMO's 5-minute DispatchIS reports from NEMweb and upserts:
  - regional prices + demand        -> prices
  - interconnector flows + limits   -> interconnectors
  - binding network constraints     -> binding_constraints

Each run re-processes the most recent N files, and upserts are idempotent,
so a skipped or late GitHub Actions run fills itself in on the next one.

Usage:
  python nem_worker.py                 # latest 12 files (~last hour)
  python nem_worker.py --files 300     # backfill everything in the Current folder (~1-2 days)
  python nem_worker.py --dry-run       # parse and print, don't write to Supabase

Env vars (put in a .env file locally, GitHub Secrets in Actions):
  SUPABASE_URL               e.g. https://abcd1234.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  Supabase -> Project Settings -> API Keys -> Secret key (sb_secret_...), keep secret!
"""

import argparse
import csv
import io
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

import spikes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

NEMWEB_DIR = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
FILE_RE = re.compile(r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip", re.IGNORECASE)
AEST = timezone(timedelta(hours=10))  # NEM market time, no daylight saving
HEADERS = {"User-Agent": "gridlock-nem-worker/0.1 (personal project)"}

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------- NEMweb

def list_latest_files(n: int) -> list[str]:
    """Return the n most recent DispatchIS zip filenames, oldest first."""
    r = session.get(NEMWEB_DIR, timeout=30)
    r.raise_for_status()
    names = sorted(set(m.group(0) for m in FILE_RE.finditer(r.text)))
    return names[-n:]


def download_csv_text(filename: str) -> str:
    # NEMweb rate-limits bursts of downloads with 403/429; back off and retry.
    for wait in (0, 5, 15, 30, 60):
        if wait:
            time.sleep(wait)
        r = session.get(NEMWEB_DIR + filename, timeout=60)
        if r.status_code not in (403, 429, 503):
            break
        print(f"  … NEMweb throttled ({r.status_code}), retrying {filename} in a moment")
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        inner = zf.namelist()[0]
        return zf.read(inner).decode("utf-8", errors="replace")


def parse_mms_csv(text: str) -> dict[str, list[dict]]:
    """
    AEMO MMS CSV format:
      C,...                                  comment rows
      I,DISPATCH,PRICE,5,COL1,COL2,...       header for a table
      D,DISPATCH,PRICE,5,val1,val2,...       data rows for that table
    Returns {"DISPATCH.PRICE": [ {COL1: val1, ...}, ... ], ...}
    """
    tables: dict[str, list[dict]] = {}
    headers: dict[str, list[str]] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        kind = row[0]
        if kind == "I":
            key = f"{row[1]}.{row[2]}"
            headers[key] = row[4:]
            tables.setdefault(key, [])
        elif kind == "D":
            key = f"{row[1]}.{row[2]}"
            cols = headers.get(key)
            if cols:
                tables[key].append(dict(zip(cols, row[4:])))
    return tables


# ---------------------------------------------------------------- transforms

def to_ts(s: str) -> str:
    """'2026/11/17 20:30:00' (AEST) -> ISO 8601 with +10:00 offset."""
    return datetime.strptime(s.strip('"'), "%Y/%m/%d %H:%M:%S").replace(tzinfo=AEST).isoformat()


def num(v):
    if v is None:
        return None
    v = str(v).strip().strip('"')
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def pricing_run(rows: list[dict]) -> list[dict]:
    """Keep the pricing run (INTERVENTION=0). During AEMO interventions there are two rows per key."""
    return [r for r in rows if str(r.get("INTERVENTION", "0")).strip() in ("0", "")]


def build_prices(t: dict) -> list[dict]:
    price_rows = pricing_run(t.get("DISPATCH.PRICE", []))
    sum_rows = {(r["SETTLEMENTDATE"], r["REGIONID"]): r for r in pricing_run(t.get("DISPATCH.REGIONSUM", []))}
    out = []
    for p in price_rows:
        s = sum_rows.get((p["SETTLEMENTDATE"], p["REGIONID"]), {})
        out.append({
            "interval_end": to_ts(p["SETTLEMENTDATE"]),
            "region": p["REGIONID"],
            "rrp": num(p.get("RRP")),
            "total_demand": num(s.get("TOTALDEMAND")),
            "available_gen": num(s.get("AVAILABLEGENERATION")),
            "dispatchable_gen": num(s.get("DISPATCHABLEGENERATION")),
            "net_interchange": num(s.get("NETINTERCHANGE")),
            "raise1sec_rrp": num(p.get("RAISE1SECRRP")),
            "raise6sec_rrp": num(p.get("RAISE6SECRRP")),
            "raise60sec_rrp": num(p.get("RAISE60SECRRP")),
            "raise5min_rrp": num(p.get("RAISE5MINRRP")),
            "raisereg_rrp": num(p.get("RAISEREGRRP")),
            "lower1sec_rrp": num(p.get("LOWER1SECRRP")),
            "lower6sec_rrp": num(p.get("LOWER6SECRRP")),
            "lower60sec_rrp": num(p.get("LOWER60SECRRP")),
            "lower5min_rrp": num(p.get("LOWER5MINRRP")),
            "lowerreg_rrp": num(p.get("LOWERREGRRP")),
        })
    return out


def build_interconnectors(t: dict) -> list[dict]:
    return [{
        "interval_end": to_ts(r["SETTLEMENTDATE"]),
        "interconnector_id": r["INTERCONNECTORID"],
        "mw_flow": num(r.get("MWFLOW")),
        "metered_mw_flow": num(r.get("METEREDMWFLOW")),
        "export_limit": num(r.get("EXPORTLIMIT")),
        "import_limit": num(r.get("IMPORTLIMIT")),
        "marginal_value": num(r.get("MARGINALVALUE")),
    } for r in pricing_run(t.get("DISPATCH.INTERCONNECTORRES", []))]


def build_constraints(t: dict) -> list[dict]:
    out = []
    for r in pricing_run(t.get("DISPATCH.CONSTRAINT", [])):
        mv = num(r.get("MARGINALVALUE"))
        if mv:  # binding only
            out.append({
                "interval_end": to_ts(r["SETTLEMENTDATE"]),
                "constraint_id": r["CONSTRAINTID"],
                "rhs": num(r.get("RHS")),
                "marginal_value": mv,
                "violation_degree": num(r.get("VIOLATIONDEGREE")),
            })
    return out


# ---------------------------------------------------------------- Supabase

def _sb_base() -> str:
    base = os.environ["SUPABASE_URL"].strip().rstrip("/")
    if base.endswith("/rest/v1"):          # accept either form of the URL
        base = base[: -len("/rest/v1")]
    return base + "/rest/v1"


def _sb_headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Content-Type": "application/json"}
    # Legacy keys are JWTs (start with "eyJ") and also go in the Authorization header.
    # New sb_secret_ keys are not JWTs and must only be sent as "apikey".
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def existing_spike_keys(since_iso: str) -> set[tuple[str, str]]:
    """(interval_end, region) pairs already in the spikes table, so we never re-explain (or re-pay for) a spike."""
    r = requests.get(
        f"{_sb_base()}/spikes",
        params={"select": "interval_end,region", "interval_end": f"gte.{since_iso}"},
        headers=_sb_headers(), timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase read from spikes failed ({r.status_code}): {r.text[:500]}")
    # Supabase returns timestamps in UTC; normalise both sides to compare
    return {(datetime.fromisoformat(x["interval_end"]).astimezone(timezone.utc).isoformat(), x["region"])
            for x in r.json()}


def _utc(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).isoformat()


def upsert(table: str, rows: list[dict], conflict_cols: str):
    if not rows:
        return
    url = f"{_sb_base()}/{table}?on_conflict={conflict_cols}"
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(rows), 500):
        r = requests.post(url, json=rows[i:i + 500], headers=headers, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase upsert into {table} failed ({r.status_code}): {r.text[:500]}")


# ---------------------------------------------------------------- main

def process_text(text: str) -> tuple[list, list, list]:
    t = parse_mms_csv(text)
    return build_prices(t), build_interconnectors(t), build_constraints(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=12, help="how many recent 5-min files to process")
    ap.add_argument("--dry-run", action="store_true", help="parse only, don't write to Supabase")
    args = ap.parse_args()

    if not args.dry_run:
        for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            if not os.environ.get(v):
                sys.exit(f"Missing env var {v}. Add it to .env (local) or GitHub Secrets (Actions).")

    files = list_latest_files(args.files)
    print(f"Processing {len(files)} file(s): {files[0] if files else '-'} … {files[-1] if files else '-'}")

    all_p, all_i, all_c = [], [], []
    for n, f in enumerate(files):
        if n:
            time.sleep(0.3)  # be polite to NEMweb on big backfills
        try:
            p, i, c = process_text(download_csv_text(f))
        except Exception as e:  # one bad file shouldn't kill the run
            print(f"  ! skipped {f}: {e}")
            continue
        all_p += p; all_i += i; all_c += c

    print(f"Parsed {len(all_p)} price rows, {len(all_i)} interconnector rows, {len(all_c)} binding constraints")

    found = spikes.detect_spikes(all_p)
    print(f"Spikes ≥ ${spikes.threshold():,.0f}/MWh in this batch: {len(found)}")

    if args.dry_run:
        latest = sorted(all_p, key=lambda r: r["interval_end"])[-5:]
        for r in latest:
            print(f"  {r['interval_end']}  {r['region']:5}  ${r['rrp']:>10,.2f}/MWh  demand {r['total_demand']:>8,.0f} MW")
        for row in spikes.explain_new_spikes(found[-3:], all_p, all_i, all_c):
            print(f"\n  [{row['explanation_source']}] {row['interval_end']} {row['region']}:\n  {row['explanation']}")
        return

    upsert("prices", all_p, "interval_end,region")
    upsert("interconnectors", all_i, "interval_end,interconnector_id")
    upsert("binding_constraints", all_c, "interval_end,constraint_id")
    print("Upserted market data to Supabase ✔")

    if found:
        since = min(s["interval_end"] for s in found)
        done = existing_spike_keys(since)
        new = [s for s in found if (_utc(s["interval_end"]), s["region"]) not in done]
        rows = spikes.explain_new_spikes(new, all_p, all_i, all_c)
        upsert("spikes", rows, "interval_end,region")
        n_ai = sum(r["explanation_source"] == "claude" for r in rows)
        print(f"Explained {len(rows)} new spike(s) ({n_ai} by Claude, {len(rows) - n_ai} by template) ✔")


if __name__ == "__main__":
    main()
