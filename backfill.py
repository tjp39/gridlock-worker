"""
WhyTheSpike one-off backfill from AEMO's NEMweb archive.

AEMO keeps the last couple of days of 5-minute DispatchIS files in Reports/Current, and
rolls older days into one zip per day in Reports/Archive. This script walks the archive
day by day, parses every 5-minute file inside, and upserts prices, interconnectors and
binding constraints exactly like nem_worker.py. It also detects spikes in that history.

Usage (from the gridlock-worker folder, with .venv active and .env filled in):
  python backfill.py                  # last 30 days, spikes explained with the free template
  python backfill.py --days 60        # go back further
  python backfill.py --ai             # let Claude explain historical spikes (uses MAX_AI_PER_RUN per day, default 10)
  python backfill.py --dry-run        # download and parse only, write nothing

Safe to stop and rerun: every write is an upsert, and spikes already in the table are skipped.
"""

import argparse
import io
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta

import nem_worker as w
import spikes

ARCHIVE_DIR = "https://nemweb.com.au/Reports/Archive/DispatchIS_Reports/"
DAY_RE = re.compile(r"PUBLIC_DISPATCHIS_(\d{8})\.zip", re.IGNORECASE)


def list_archive_days() -> dict[date, str]:
    r = w.session.get(ARCHIVE_DIR, timeout=60)
    r.raise_for_status()
    out = {}
    for m in DAY_RE.finditer(r.text):
        out[datetime.strptime(m.group(1), "%Y%m%d").date()] = m.group(0)
    return out


def download(url: str) -> bytes:
    for wait in (0, 10, 30, 60, 120):
        if wait:
            time.sleep(wait)
        try:
            r = w.session.get(url, timeout=300)
        except Exception as e:  # network blip on a big file
            print(f"    … download error ({e}), retrying")
            continue
        if r.status_code in (403, 429, 503):
            print(f"    … NEMweb throttled ({r.status_code}), waiting")
            continue
        r.raise_for_status()
        return r.content
    raise RuntimeError(f"gave up downloading {url}")


def iter_csv_texts(day_zip: bytes):
    """A daily archive zip holds one small zip (or CSV) per 5-minute interval."""
    with zipfile.ZipFile(io.BytesIO(day_zip)) as outer:
        for name in sorted(outer.namelist()):
            data = outer.read(name)
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    for n in inner.namelist():
                        yield inner.read(n).decode("utf-8", errors="replace")
            elif name.lower().endswith(".csv"):
                yield data.decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--ai", action="store_true", help="explain historical spikes with Claude instead of the template")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            if not os.environ.get(v):
                sys.exit(f"Missing env var {v}. Add it to .env first.")
    if not args.ai:
        os.environ.pop("ANTHROPIC_API_KEY", None)  # template explanations only, costs nothing

    days = list_archive_days()
    if not days:
        sys.exit("Couldn't find any daily files in the NEMweb archive listing.")
    newest = max(days)
    wanted = [d for d in sorted(days) if d > newest - timedelta(days=args.days)]
    print(f"Archive has {len(days)} days ({min(days)} to {newest}). Backfilling {len(wanted)}: {wanted[0]} to {wanted[-1]}")

    prev_tail: list[dict] = []   # last hour of the previous day, so spike facts at midnight have context
    totals = {"prices": 0, "ic": 0, "cons": 0, "spikes": 0}
    started = time.time()

    for k, d in enumerate(wanted, 1):
        t0 = time.time()
        print(f"[{k}/{len(wanted)}] {d}: downloading…", end="", flush=True)
        try:
            blob = download(ARCHIVE_DIR + days[d])
        except Exception as e:
            print(f" ! skipped: {e}")
            continue

        all_p, all_i, all_c = [], [], []
        n_files = 0
        for text in iter_csv_texts(blob):
            try:
                p, i, c = w.process_text(text)
            except Exception as e:
                print(f"\n    ! bad interval file skipped: {e}", end="")
                continue
            all_p += p; all_i += i; all_c += c
            n_files += 1
        print(f" {n_files} intervals, {len(all_p)} prices, {len(all_i)} interconnector rows, {len(all_c)} constraints", end="")

        found = spikes.detect_spikes(all_p)
        if args.dry_run:
            print(f", {len(found)} spikes (dry run, nothing written)")
            prev_tail = sorted(all_p, key=lambda r: r["interval_end"])[-60:]
            continue

        w.upsert("prices", all_p, "interval_end,region")
        w.upsert("interconnectors", all_i, "interval_end,interconnector_id")
        w.upsert("binding_constraints", all_c, "interval_end,constraint_id")

        if found:
            done = w.existing_spike_keys(min(s["interval_end"] for s in found))
            new = [s for s in found if (w._utc(s["interval_end"]), s["region"]) not in done]
            rows = spikes.explain_new_spikes(new, prev_tail + all_p, all_i, all_c)
            w.upsert("spikes", rows, "interval_end,region")
            totals["spikes"] += len(rows)
            print(f", {len(rows)} new spikes", end="")

        totals["prices"] += len(all_p); totals["ic"] += len(all_i); totals["cons"] += len(all_c)
        prev_tail = sorted(all_p, key=lambda r: r["interval_end"])[-60:]  # 12 intervals × 5 regions
        print(f"  ✔ ({time.time() - t0:.0f}s)")
        time.sleep(2)  # be polite to NEMweb between big downloads

    mins = (time.time() - started) / 60
    print(f"\nDone in {mins:.1f} min. Wrote {totals['prices']:,} price rows, {totals['ic']:,} interconnector rows, "
          f"{totals['cons']:,} binding constraints, {totals['spikes']} spikes.")
    print("The last day or two come from the live worker (Reports/Current), so there's no gap.")


if __name__ == "__main__":
    main()
