"""GET-only venue collection and descriptive comparison; no execution adapter.

Never interpret price disagreement as a calibrated probability or causal
play-money effect. Callers must supply reviewed exact settlement mappings.
"""
import json
from itertools import combinations
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import urlopen

BASE = "https://external-api.kalshi.com/trade-api/v2"


def collect_kalshi(series_tickers, get=None):
    """Collect all open pages, preserving rules, raw quotes and source dates.

    No API key, order endpoint, automatic outcome matching or date guessing.
    `updated_time` is retained separately; it is not a quote timestamp.
    """
    if get is None:
        def get(url):
            with urlopen(url, timeout=25) as response:
                return json.load(response)
    rows = []
    for series in series_tickers:
        cursor = ""
        seen = set()
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            url = BASE + "/markets?" + urlencode(params)
            data = get(url)
            if not isinstance(data.get("markets"), list):
                raise ValueError("Incomplete Kalshi response")
            observed = datetime.now(timezone.utc).isoformat()
            for market in data["markets"]:
                rows.append({"venue": "kalshi", "series": series,
                             "observed_at": observed, "source_url": url,
                             "quote_timestamp_verified": False,
                             "execution_authorized": False, "market": market})
            cursor = data.get("cursor")
            if not isinstance(cursor, str):
                raise ValueError("Missing pagination completeness evidence")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Repeated pagination cursor")
            seen.add(cursor)
    return rows


def midpoint(quote):
    """Only two-sided, positive-size book observations qualify."""
    if quote["kind"] != "book":
        raise ValueError("Displays and fills are not synchronized books")
    bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
    if not Decimal(0) <= bid <= ask <= Decimal(1):
        raise ValueError("Invalid or crossed quote")
    if min(Decimal(str(quote["bid_size"])), Decimal(str(quote["ask_size"]))) <= 0:
        raise ValueError("Missing executable size")
    return (bid + ask) / 2


def compare_pairs(pairs, max_skew_seconds=60):
    """Descriptive raw midpoint gaps for manually reviewed exact matches.

    No normalization unless an exhaustive outcome set is separately proven.
    Multiple outcomes from the same race are not independent observations.
    """
    if max_skew_seconds <= 0:
        raise ValueError("Invalid timestamp tolerance")
    gaps = []
    for pair in pairs:
        if pair.get("settlement_match") != "reviewed_exact":
            raise ValueError("Unreviewed settlement mapping")
        a, b = pair["sig"], pair["reference"]
        for quote in (a, b):
            if quote.get("quote_timestamp_verified") is not True:
                raise ValueError("Quote timestamp is not verified")
        times = [datetime.fromisoformat(q["quote_at"].replace("Z", "+00:00"))
                 for q in (a, b)]
        if any(t.tzinfo is None for t in times):
            raise ValueError("Timezone required")
        if abs((times[0] - times[1]).total_seconds()) > max_skew_seconds:
            raise ValueError("Asynchronous prices")
        gap = float(100 * (midpoint(a) - midpoint(b)))
        gaps.append({"race": pair["race"], "outcome": pair["outcome"],
                     "reference_venue": b["venue"], "gap_pp": gap})
    values = [x["gap_pp"] for x in gaps]
    return {"execution_authorized": False, "rows": gaps,
            "outcome_count": len(gaps),
            "race_count": len({x["race"] for x in gaps}),
            "mae_pp": sum(abs(x) for x in values)/len(values) if values else None,
            "calibrated_interval": None,
            "causal_play_money_effect": None,
            "warning": "Dependent outcomes; descriptive only. No trading thresholds."}


def model_three_venues(markets, as_of, max_age_seconds=300, max_skew_seconds=60):
    """Triangulate SIG/Polymarket/Kalshi books without calibrated blending.

    Input markets contain race, outcome, settlement_match, and a quotes dict
    keyed by sig/polymarket/kalshi. Output is research only. Missing or invalid
    observations are explicit UNKNOWN, not imputed from another venue.
    The real-money hull is a descriptive bid/ask envelope, not a confidence
    interval. Cross-venue prices do not constitute independent evidence.
    """
    now = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if now.tzinfo is None or min(max_age_seconds, max_skew_seconds) <= 0:
        raise ValueError("Timezone and positive tolerances required")
    rows = []
    for market in markets:
        row = {"race": market["race"], "outcome": market["outcome"],
               "status": "UNKNOWN", "venues": {}, "pairwise": [],
               "real_money_envelope": None, "sig_envelope_gap_pp": None}
        quotes, times = {}, []
        reasons = []
        if market.get("settlement_match") != "reviewed_exact":
            reasons.append("Settlement relationships not reviewed exact")
        for venue in ("sig", "polymarket", "kalshi"):
            q = market.get("quotes", {}).get(venue)
            try:
                if not q or q.get("venue") != venue:
                    raise ValueError("Missing quote or mismatched venue")
                mid = midpoint(q)
                if q.get("quote_timestamp_verified") is not True:
                    raise ValueError("Quote time unverified")
                at = datetime.fromisoformat(q["quote_at"].replace("Z", "+00:00"))
                if at.tzinfo is None:
                    raise ValueError("Timezone required")
                age = (now - at).total_seconds()
                if not 0 <= age <= max_age_seconds:
                    raise ValueError("Stale or future quote")
                quotes[venue] = q
                times.append(at)
                row["venues"][venue] = {"midpoint": float(mid),
                    "bid": float(q["bid"]), "ask": float(q["ask"]),
                    "bid_size": float(q["bid_size"]),
                    "ask_size": float(q["ask_size"]), "quote_at": q["quote_at"]}
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                row["venues"][venue] = {"status": "UNKNOWN", "reason": str(exc)}
                reasons.append(venue + ": " + str(exc))
        if times and (max(times) - min(times)).total_seconds() > max_skew_seconds:
            reasons.append("Asynchronous venue books")
        if not reasons:
            row["status"] = "DESCRIPTIVE_ONLY"
            for a, b in combinations(("sig", "polymarket", "kalshi"), 2):
                qa, qb = quotes[a], quotes[b]
                row["pairwise"].append({"venues": [a, b],
                    "midpoint_gap_pp": float(100 * (midpoint(qa) - midpoint(qb))),
                    "book_gap_pp": [100 * (float(qa["bid"]) - float(qb["ask"])),
                                    100 * (float(qa["ask"]) - float(qb["bid"]))]})
            low = min(float(quotes[v]["bid"]) for v in ("polymarket", "kalshi"))
            high = max(float(quotes[v]["ask"]) for v in ("polymarket", "kalshi"))
            row["real_money_envelope"] = [low, high]
            row["reference_probability_range"] = {
                "range": [low, high], "kind": "market_implied_reference",
                "calibrated": False, "confidence_interval": False,
                "evidence_family": "real_money_market_prices",
                "sig_included": False, "evidence_adjustment": None,
                "validation_required": "Chronological out-of-sample venue calibration and independently validated evidence adjustment"}
            row["sig_envelope_gap_pp"] = [
                100 * (float(quotes["sig"]["bid"]) - high),
                100 * (float(quotes["sig"]["ask"]) - low)]
        row["limitations"] = reasons
        rows.append(row)
    return {"as_of": as_of, "execution_authorized": False, "rows": rows,
            "eligible_outcome_count": sum(r["status"] != "UNKNOWN" for r in rows),
            "eligible_race_count": len({r["race"] for r in rows if r["status"] != "UNKNOWN"}),
            "calibrated_probability": None, "calibrated_error_interval": None,
            "causal_play_money_effect": None,
            "note": "No venue weights, trade signals or assumed ground truth."}
