"""
One-off: write plain-English explanations for spikes that don't have one yet.

  python fill_plain.py          # free template wording
  python fill_plain.py --ai     # Claude writes them (a fraction of a cent each)

Leaves the existing technical explanations untouched. Safe to rerun.
"""
import argparse
import os
import sys

import requests

import nem_worker as w
import spikes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", action="store_true")
    args = ap.parse_args()
    for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(v):
            sys.exit(f"Missing env var {v}. Add it to .env first.")
    use_ai = args.ai and bool(os.environ.get("ANTHROPIC_API_KEY"))

    r = requests.get(f"{w._sb_base()}/spikes",
                     params={"select": "interval_end,region,facts", "explanation_plain": "is.null",
                             "order": "interval_end.desc"},
                     headers=w._sb_headers(), timeout=60)
    if r.status_code >= 300:
        sys.exit(f"Couldn't read spikes ({r.status_code}): {r.text[:300]}\n"
                 "Did you run schema_plain.sql in Supabase first?")
    todo = r.json()
    print(f"{len(todo)} spike(s) need a plain-English explanation")

    for row in todo:
        facts = row.get("facts") or {}
        plain = None
        if use_ai:
            try:
                _, plain, _ = spikes.claude_explanations(facts)
            except Exception as e:
                print(f"  ! Claude failed, using template: {e}")
        if not plain:
            plain = spikes.plain_template_explanation(facts)
        p = requests.patch(f"{w._sb_base()}/spikes",
                           params={"interval_end": f"eq.{row['interval_end']}", "region": f"eq.{row['region']}"},
                           json={"explanation_plain": plain},
                           headers={**w._sb_headers(), "Prefer": "return=minimal"}, timeout=60)
        if p.status_code >= 300:
            print(f"  ! couldn't update {row['region']} {row['interval_end']}: {p.text[:200]}")
            continue
        print(f"  ✔ {row['region']} {row['interval_end']}: {plain}")


if __name__ == "__main__":
    main()
