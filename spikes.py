"""
Gridlock spike detection + explanations.

For every 5-minute interval where a region's price is at or above SPIKE_THRESHOLD,
gather the facts (demand, interconnectors at limit, binding constraints, price just before)
and write a plain-English explanation:
  - with Claude, if ANTHROPIC_API_KEY is set (falls back to the template on any error)
  - otherwise with a simple template built from the same facts

Env vars (all optional):
  SPIKE_THRESHOLD     $/MWh, default 1000
  ANTHROPIC_API_KEY   enables Claude-written explanations
  ANTHROPIC_MODEL     default claude-haiku-5-5
  MAX_AI_PER_RUN      cap on Claude calls per run (protects your credit on big backfills), default 10
"""

import os

import requests

# Interconnector ID -> (from_region, to_region). Positive MW flow = from -> to.
IC_REGIONS = {
    "N-Q-MNSP1": ("NSW1", "QLD1"),   # Terranora / Directlink
    "NSW1-QLD1": ("NSW1", "QLD1"),   # QNI
    "VIC1-NSW1": ("VIC1", "NSW1"),
    "V-SA":      ("VIC1", "SA1"),    # Heywood
    "V-S-MNSP1": ("VIC1", "SA1"),    # Murraylink
    "T-V-MNSP1": ("TAS1", "VIC1"),   # Basslink
}
IC_NAMES = {
    "N-Q-MNSP1": "Terranora (NSW–QLD)",
    "NSW1-QLD1": "QNI (NSW–QLD)",
    "VIC1-NSW1": "VIC–NSW",
    "V-SA": "Heywood (VIC–SA)",
    "V-S-MNSP1": "Murraylink (VIC–SA)",
    "T-V-MNSP1": "Basslink (TAS–VIC)",
}
REGION_NAMES = {"NSW1": "NSW", "QLD1": "QLD", "VIC1": "VIC", "SA1": "SA", "TAS1": "TAS"}


def threshold() -> float:
    return float(os.environ.get("SPIKE_THRESHOLD", "1000"))


# ---------------------------------------------------------------- facts

def _ic_at_limit(ic: dict) -> bool:
    if ic.get("marginal_value"):
        return True
    flow, exp, imp = ic.get("mw_flow"), ic.get("export_limit"), ic.get("import_limit")
    if flow is None:
        return False
    if flow >= 0 and exp is not None:
        return flow >= exp - max(5, abs(exp) * 0.01)
    if flow < 0 and imp is not None:
        return flow <= imp + max(5, abs(imp) * 0.01)
    return False


def build_facts(spike: dict, prices: list[dict], ics: list[dict], cons: list[dict]) -> dict:
    t, region = spike["interval_end"], spike["region"]

    # price/demand history for this region from the batch we already have
    hist = sorted((p for p in prices if p["region"] == region and p["interval_end"] < t),
                  key=lambda p: p["interval_end"])
    prev = hist[-1] if hist else None
    hour_ago = hist[-12] if len(hist) >= 12 else (hist[0] if hist else None)
    window_min = None
    if hour_ago:
        from datetime import datetime
        window_min = round((datetime.fromisoformat(t) - datetime.fromisoformat(hour_ago["interval_end"]))
                           .total_seconds() / 60)

    # interconnectors touching this region, plus any other constrained ones
    ic_facts = []
    for ic in ics:
        if ic["interval_end"] != t:
            continue
        regs = IC_REGIONS.get(ic["interconnector_id"])
        touches = regs is not None and region in regs
        at_limit = _ic_at_limit(ic)
        if not (touches or at_limit):
            continue
        flow = ic.get("mw_flow") or 0
        if regs:
            direction = f"{REGION_NAMES[regs[0]]}→{REGION_NAMES[regs[1]]}" if flow >= 0 \
                else f"{REGION_NAMES[regs[1]]}→{REGION_NAMES[regs[0]]}"
            into_region = (flow >= 0 and regs[1] == region) or (flow < 0 and regs[0] == region)
        else:
            direction, into_region = None, None
        ic_facts.append({
            "id": ic["interconnector_id"],
            "name": IC_NAMES.get(ic["interconnector_id"], ic["interconnector_id"]),
            "flow_mw": round(abs(flow)),
            "direction": direction,
            "flowing_into_spike_region": into_region,
            "at_limit": at_limit,
            "held_at_zero_by_constraint": at_limit and abs(flow) < 5,  # e.g. outage constraints like SVML_ZERO
            "touches_spike_region": touches,
        })

    binding = sorted((c for c in cons if c["interval_end"] == t),
                     key=lambda c: abs(c["marginal_value"] or 0), reverse=True)[:5]

    return {
        "region": REGION_NAMES.get(region, region),
        "interval_end_aest": t,
        "price_per_mwh": round(spike["rrp"], 2),
        "price_5min_earlier": round(prev["rrp"], 2) if prev and prev["rrp"] is not None else None,
        "comparison_window_minutes": window_min,
        "price_at_window_start": round(hour_ago["rrp"], 2) if hour_ago and hour_ago["rrp"] is not None else None,
        "demand_mw": round(spike["total_demand"]) if spike.get("total_demand") is not None else None,
        "demand_change_over_window_mw": (round(spike["total_demand"] - hour_ago["total_demand"])
                                   if hour_ago and spike.get("total_demand") is not None
                                   and hour_ago.get("total_demand") is not None else None),
        "available_generation_mw": round(spike["available_gen"]) if spike.get("available_gen") is not None else None,
        "spare_capacity_mw": (round(spike["available_gen"] - spike["total_demand"])
                              if spike.get("available_gen") is not None and spike.get("total_demand") is not None else None),
        "net_interchange_mw": round(spike["net_interchange"]) if spike.get("net_interchange") is not None else None,
        "interconnectors": ic_facts,
        "top_binding_constraints": [
            {"id": c["constraint_id"], "marginal_value": round(c["marginal_value"], 2)} for c in binding
        ],
        "bid_data_available": False,  # bids are published next morning; added in a later step
    }


# ---------------------------------------------------------------- explanations

def template_explanation(f: dict) -> str:
    s = [f"{f['region']} hit ${f['price_per_mwh']:,.0f}/MWh"]
    p5 = f["price_5min_earlier"]
    if p5 is not None and p5 >= threshold():
        s[0] += f", continuing a spike (${p5:,.0f} five minutes earlier)"
    elif p5 is not None and p5 < f["price_per_mwh"]:
        s[0] += f", up from ${p5:,.0f} five minutes earlier"
    elif p5 is not None:
        s[0] += f" (${p5:,.0f} five minutes earlier)"
    s[0] += "."
    if f["demand_mw"] is not None:
        d = f"Demand was {f['demand_mw']:,} MW"
        if f["demand_change_over_window_mw"] and f["comparison_window_minutes"]:
            d += f" ({f['demand_change_over_window_mw']:+,} MW over the previous {f['comparison_window_minutes']} minutes)"
        if f["spare_capacity_mw"] is not None:
            d += f", with {f['spare_capacity_mw']:,} MW of spare available generation"
        s.append(d + ".")
    rel = [ic for ic in f["interconnectors"] if ic["at_limit"] and ic["touches_spike_region"]]
    limited = [ic for ic in rel if not ic["held_at_zero_by_constraint"]]
    zeroed = [ic for ic in rel if ic["held_at_zero_by_constraint"]]
    if limited:
        parts = [f"{ic['name']} ({ic['flow_mw']} MW {ic['direction']})" for ic in limited]
        s.append("Interconnectors at their limit: " + "; ".join(parts) + ".")
    if zeroed:
        s.append(", ".join(ic["name"] for ic in zeroed) + " was held at 0 MW by a constraint.")
    if f["top_binding_constraints"]:
        s.append("Top binding constraint: " + f["top_binding_constraints"][0]["id"] + ".")
    s.append("Bid data for this interval is published by AEMO next morning.")
    return " ".join(s)


SYSTEM_PROMPT = (
    "You explain Australian National Electricity Market (NEM) price spikes to energy traders "
    "and interested readers. Write 2–4 sentences in Australian English. Use ONLY the facts in "
    "the JSON you are given. An interconnector with held_at_zero_by_constraint=true is being "
    "held at zero flow by a constraint (often an outage), not running at full capacity. Do not speculate about causes that are not in the facts, do not "
    "name generators or companies unless they appear in the facts, and do not invent numbers. "
    "If bid_data_available is false, end by noting that bidding behaviour will be clearer once "
    "AEMO publishes bids next morning. Times are AEST (market time). Plain text, no headings."
)


def claude_explanation(f: dict) -> tuple[str, str]:
    import json
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1024,
            "thinking": {"type": "disabled"},   # we want a short direct answer, not reasoning tokens
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "Explain this spike:\n" + json.dumps(f, indent=1)}],
        },
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        kinds = [b.get("type") for b in data.get("content", [])]
        raise RuntimeError(f"no text from Anthropic API (stop_reason={data.get('stop_reason')}, "
                           f"blocks={kinds}, model={data.get('model')})")
    return text, model


# ---------------------------------------------------------------- main entry

def detect_spikes(prices: list[dict]) -> list[dict]:
    th = threshold()
    return [p for p in prices if p.get("rrp") is not None and p["rrp"] >= th]


def explain_new_spikes(new_spikes, prices, ics, cons) -> list[dict]:
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    ai_budget = int(os.environ.get("MAX_AI_PER_RUN", "10"))
    rows = []
    # explain the newest spikes first so the live feed gets AI text if the budget runs out
    for sp in sorted(new_spikes, key=lambda p: p["interval_end"], reverse=True):
        facts = build_facts(sp, prices, ics, cons)
        text, source, model = template_explanation(facts), "template", None
        if use_ai and ai_budget > 0:
            try:
                text, model = claude_explanation(facts)
                source = "claude"
                ai_budget -= 1
            except Exception as e:
                print(f"  ! Claude explanation failed, using template: {e}")
        rows.append({
            "interval_end": sp["interval_end"],
            "region": sp["region"],
            "rrp": sp["rrp"],
            "facts": facts,
            "explanation": text,
            "explanation_source": source,
            "model": model,
        })
    return rows
