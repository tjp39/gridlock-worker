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
        flow = ic.get("mw_flow") or 0
        zeroed = _ic_at_limit(ic) and abs(flow) < 5
        at_limit = _ic_at_limit(ic) and not zeroed
        # only interconnectors that touch this region matter for its price
        if not touches:
            continue
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
            "held_at_zero_by_constraint": zeroed,  # e.g. outage constraints like SVML_ZERO
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
    limited = [ic for ic in f["interconnectors"] if ic["at_limit"]]
    zeroed = [ic for ic in f["interconnectors"] if ic["held_at_zero_by_constraint"]]
    if limited:
        parts = [f"{ic['name']} ({ic['flow_mw']} MW {ic['direction']})" for ic in limited]
        s.append("Interconnectors at their limit: " + "; ".join(parts) + ".")
    if zeroed:
        s.append(", ".join(ic["name"] for ic in zeroed) + " was held at 0 MW by a constraint.")
    if f["top_binding_constraints"]:
        s.append("Top binding constraint: " + f["top_binding_constraints"][0]["id"] + ".")
    s.append("Bid data for this interval is published by AEMO next morning.")
    return " ".join(s)


REGION_FULL = {"NSW": "New South Wales", "QLD": "Queensland", "VIC": "Victoria",
               "SA": "South Australia", "TAS": "Tasmania"}
TYPICAL_PRICE = 100  # $/MWh, a rough "normal" wholesale price used to give readers a sense of scale


def plain_template_explanation(f: dict) -> str:
    """Plain-English version for anyone: no codes, no acronyms, no jargon."""
    region = REGION_FULL.get(f.get("region"), f.get("region", "the region"))
    price = f.get("price_per_mwh") or 0
    s = f"Wholesale power prices in {region} jumped to ${price:,.0f} per megawatt hour"
    times = price / TYPICAL_PRICE
    if times >= 3:
        s += f", roughly {times:,.0f} times a typical price"
    reasons = []

    def neighbour(ic):
        d = ic.get("direction") or ""
        parts = [p for p in d.split("→") if p and p != f["region"]]
        return REGION_FULL.get(parts[0], parts[0]) if parts else "a neighbouring state"

    ics = f.get("interconnectors") or []
    full_in = [ic for ic in ics if ic.get("at_limit") and ic.get("flowing_into_spike_region")]
    if full_in:
        names = sorted({neighbour(ic) for ic in full_in})
        reasons.append(f"the power lines bringing electricity in from {' and '.join(names)} were already full, "
                       f"so the state couldn't import more cheaper power")
    if any(ic.get("held_at_zero_by_constraint") for ic in ics):
        reasons.append("one of its power links to a neighbouring state was out of action")
    spare, demand = f.get("spare_capacity_mw"), f.get("demand_mw")
    if spare is not None and demand and spare < 0.15 * demand:
        reasons.append("there was very little spare generation left to call on")
    change = f.get("demand_change_over_window_mw")
    if change and demand and change > 0.05 * demand:
        reasons.append("demand for electricity was climbing quickly")
    s += "."
    if reasons:
        s += " " + reasons[0][0].upper() + reasons[0][1:] + "."
        if len(reasons) > 1:
            s += " On top of that, " + reasons[1] + "."   # keep it to the two most important causes
    else:
        s += " The full picture will be clearer once generators' price offers are published tomorrow."
    return s


SYSTEM_PROMPT = (
    "You explain Australian electricity price spikes. You will be given facts as JSON about one 5-minute "
    "price spike in the National Electricity Market. Write TWO explanations from ONLY those facts, and "
    "return them as a JSON object with exactly two keys, \"technical\" and \"plain\", and nothing else.\n\n"
    "\"technical\": for energy traders. At most 3 short sentences, under 70 words, like a sharp market "
    "commentator. Sentence 1: the price and whether it jumped or eased versus price_5min_earlier (only say "
    "'rose'/'jumped' if it is higher). Then the relevant drivers in the facts: demand moves, tight spare "
    "capacity, interconnectors at their limit (at_limit=true), or interconnectors held at zero by a "
    "constraint (held_at_zero_by_constraint=true, usually an outage). Constraint IDs and industry terms are "
    "fine here. If bid_data_available is false, end with a short clause like 'bids due from AEMO tomorrow morning'.\n\n"
    "\"plain\": for a member of the public with no energy background. At most 3 short sentences, under "
    "60 words, at a year 9 reading level. Rules: no acronyms (no NEM, AEMO, MW, FCAS), no constraint codes "
    "or IDs, no jargon. Say 'power lines between states' or 'power link' instead of interconnector, "
    "'limits on the grid' instead of constraints, 'wholesale power price' instead of spot price or RRP. "
    "Spell out state names. Write prices as dollars per megawatt hour and give a sense of scale: a typical "
    "wholesale price is around $100 per megawatt hour, so say roughly how many times higher it was. Explain "
    "the likely cause in everyday words using only the facts. Don't suggest household bills jumped by the "
    "same amount.\n\n"
    "For both: Australian English, don't repeat the date or time, don't name generators or companies, "
    "don't invent numbers or causes that aren't in the facts. Plain text inside each value, no lists."
)


def _parse_two(text: str) -> tuple[str, str]:
    import json
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.index("{"):] if "{" in t else t
    t = t[t.index("{"): t.rindex("}") + 1]
    obj = json.loads(t)
    tech, plain = str(obj.get("technical", "")).strip(), str(obj.get("plain", "")).strip()
    if not tech or not plain:
        raise ValueError("missing technical or plain text")
    return tech, plain


def claude_explanations(f: dict) -> tuple[str, str, str]:
    """Returns (technical, plain, model)."""
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
            "max_tokens": 600,
            "thinking": {"type": "disabled"},   # we want a short direct answer, not reasoning tokens
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "Facts:\n" + json.dumps(f, indent=1)}],
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
    try:
        tech, plain = _parse_two(text)
    except Exception as e:
        raise RuntimeError(f"couldn't read Claude's two explanations ({e}): {text[:200]}")
    return tech, plain, model


# ---------------------------------------------------------------- main entry

def detect_spikes(prices: list[dict]) -> list[dict]:
    th = threshold()
    return [p for p in prices if p.get("rrp") is not None and p["rrp"] >= th]


def explain_facts(facts: dict, use_ai: bool) -> dict:
    """Technical + plain explanations for one spike's facts, Claude if allowed, else templates."""
    out = {
        "explanation": template_explanation(facts),
        "explanation_plain": plain_template_explanation(facts),
        "explanation_source": "template",
        "model": None,
    }
    if use_ai:
        try:
            tech, plain, model = claude_explanations(facts)
            out.update(explanation=tech, explanation_plain=plain, explanation_source="claude", model=model)
        except Exception as e:
            print(f"  ! Claude explanation failed, using templates: {e}")
    return out


def explain_new_spikes(new_spikes, prices, ics, cons) -> list[dict]:
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    ai_budget = int(os.environ.get("MAX_AI_PER_RUN", "10"))
    rows = []
    # explain the newest spikes first so the live feed gets AI text if the budget runs out
    for sp in sorted(new_spikes, key=lambda p: p["interval_end"], reverse=True):
        facts = build_facts(sp, prices, ics, cons)
        ex = explain_facts(facts, use_ai and ai_budget > 0)
        if ex["explanation_source"] == "claude":
            ai_budget -= 1
        rows.append({
            "interval_end": sp["interval_end"],
            "region": sp["region"],
            "rrp": sp["rrp"],
            "facts": facts,
            **ex,
        })
    return rows
