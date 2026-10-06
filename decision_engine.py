"""Auditable prediction-market research. Standard library only; cannot submit orders."""
import argparse
import hashlib
import itertools
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"


def number(value, low=None, high=None):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a number")
    x = float(value)
    if not math.isfinite(x) or (low is not None and x < low) or (high is not None and x > high):
        raise ValueError(f"Invalid numeric value: {value}")
    return x


def interval(values, low=None, high=None):
    if len(values) != 2:
        raise ValueError("An interval needs two endpoints")
    a, b = [number(x, low, high) for x in values]
    if a > b:
        raise ValueError("Reversed interval")
    return a, b


def timestamp(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return t


def fresh(value, now, max_hours):
    age = (now - timestamp(value)).total_seconds() / 3600
    return 0 <= age <= max_hours


def evidence_check(items, now, max_hours):
    issues, seen = [], set()
    if not items:
        issues.append("No evidence supplied")
    for e in items:
        group = e.get("independence_group")
        if not group or group in seen:
            issues.append("Missing or duplicate evidence independence group")
        seen.add(group)
        if not e.get("url", "").startswith("https://") or not e.get("claim"):
            issues.append("Evidence needs an original-source URL and claim")
        if e.get("verified") is not True:
            issues.append("Unverified evidence; social posts are leads until corroborated")
        if not fresh(e["published_at"], now, max_hours):
            issues.append("Stale or future evidence timestamp")
    return issues


def forecast(model, now):
    """Return YES probability bounds; ranges are assumption sensitivity, not CIs."""
    issues = evidence_check(model.get("evidence", []), now, model.get("max_age_hours", 168))
    kind = model["kind"]
    if kind == "probability_range":
        lo, hi = interval(model["yes_probability"], 0, 1)
        calculation = {"method": "externally justified probability range"}
    elif kind == "margin_normal":
        margins = interval(model["final_margin_pp"], -100, 100)
        sigmas = interval(model["total_error_sd_pp"], 0.000001, 100)
        # sigma MUST cover systematic polling error, turnout and time to election.
        candidates = [0.5 * (1 + math.erf(m / s / math.sqrt(2)))
                      for m, s in itertools.product(margins, sigmas)]
        lo, hi = min(candidates), max(candidates)
        calculation = {"method": "P(final margin > 0) = Phi(margin / total_error_sd)",
                       "final_margin_pp": margins, "total_error_sd_pp": sigmas}
    else:
        raise ValueError("Unknown forecast kind")
    if not model.get("assumptions"):
        issues.append("Missing forecast assumptions")
    if not model.get("validation_reference"):
        issues.append("No out-of-sample calibration or validation reference")
    # Likelihood ratios must be independently justified, never inferred from sentiment.
    used = {e.get("independence_group") for e in model.get("evidence", [])}
    for event in model.get("news_updates", []):
        issues.extend(evidence_check([event], now, model.get("max_age_hours", 168)))
        if event.get("independence_group") in used:
            issues.append("News double-counts evidence already in the base forecast")
        used.add(event.get("independence_group"))
        a, b = interval(event["likelihood_ratio"], 0.000001, 1000000)
        if not event.get("likelihood_justification") or not event.get("not_in_base_forecast"):
            issues.append("News likelihood update lacks justification or is already priced into model")
        def bayes(p, lr):
            return p * lr / (1 - p + p * lr)
        lo, hi = bayes(lo, a), bayes(hi, b)
    return (lo, hi), calculation, issues


def executable(book, side, action, quantity):
    """Consume YES-normalized book; NO asks=1-YES bids, NO bids=1-YES asks."""
    if side not in ("yes", "no") or action not in ("buy", "sell"):
        raise ValueError("Invalid side/action")
    q = number(quantity, 1)
    if int(q) != q:
        raise ValueError("Whole shares only")
    source = "asks" if (side == "yes") == (action == "buy") else "bids"
    levels = []
    for row in book[source]:
        px = number(row["price"], 0, 1)
        size = number(row["quantity"], 0)
        levels.append((px if side == "yes" else 1 - px, size))
    levels.sort(reverse=(action == "sell"))
    remaining, total, used = q, 0.0, []
    for px, size in levels:
        fill = min(remaining, size)
        if fill:
            used.append({"price": px, "quantity": fill})
            total += px * fill
            remaining -= fill
        if remaining == 0:
            break
    return {"requested": q, "filled": q - remaining, "complete": remaining == 0,
            "notional": total, "vwap": total / (q - remaining) if q != remaining else None,
            "levels": used}


def relation_checks(relations):
    out = []
    for r in relations:
        a = number(r["a_yes_probability"], 0, 1)
        b = number(r["b_yes_probability"], 0, 1)
        kind = r["kind"]
        violation = {"a_implies_b": a > b, "mutually_exclusive": a + b > 1,
                     "exhaustive_pair": a + b < 1}.get(kind)
        if violation is None:
            raise ValueError("Unknown relation kind")
        out.append({"kind": kind, "violation": violation,
                    "verified_contract_relation": r.get("verified") is True,
                    "note": "Probability diagnostic only; not executable arbitrage"})
    return out


def evaluate(case, now=None):
    now = now or datetime.now(timezone.utc)
    issues = []
    contract = case["contract"]
    for field in ("market_id", "exchange_id", "question", "settlement_rule", "rule_url", "settles_at"):
        if not contract.get(field):
            issues.append("Missing contract field: " + field)
    if contract.get("verified") is not True:
        issues.append("Settlement rules not verified")
    if timestamp(contract["settles_at"]) <= now:
        issues.append("Contract is past settlement date")
    book, trade = case["book"], case["trade"]
    if str(book.get("exchangeId")) != str(contract["exchange_id"]):
        issues.append("Order book does not match contract")
    if not fresh(book["asOf"]["at"], now, 60 / 3600):
        issues.append("Order book is stale or future (maximum60 seconds)")
    probs, method, reasons = forecast(case["forecast"], now)
    issues.extend(reasons)
    side = trade["side"]
    if side == "no":
        probs = (1 - probs[1], 1 - probs[0])
    q = number(trade["quantity"], 1)
    buy = executable(book, side, "buy", q)
    sell = executable(book, side, "sell", q)
    costs = case["costs"]
    fee = number(costs["fee_per_share"], 0)
    slip = number(costs["slippage_per_share"], 0)
    if costs.get("verified") is not True or not costs.get("source"):
        issues.append("Fees not verified")
    if not buy["complete"] or not sell["complete"]:
        issues.append("Insufficient displayed entry or exit depth")
    if not trade.get("thesis") or not trade.get("falsification"):
        issues.append("Missing trade thesis or falsification condition")
    if trade.get("objective") != "settlement":
        issues.append("Win probability does not predict short-term resale; requires a validated exit model")
    risk = case["risk"]
    cash = number(risk["cash"], 0)
    reserve = number(risk["minimum_cash"], 0)
    max_loss = buy["notional"] + q * (fee + slip)
    for name, existing, limit in (
        ("race", risk["race_exposure"], risk["max_race_loss"]),
        ("shared political factor", risk["factor_exposure"], risk["max_factor_loss"]),
        ("portfolio", risk["total_exposure"], risk["max_total_loss"])):
        if number(existing, 0) + max_loss > number(limit, 0):
            issues.append(name + " risk cap exceeded")
    if cash - max_loss < reserve:
        issues.append("Cash reserve breached")
    ev = None if buy["vwap"] is None else [p - buy["vwap"] - fee - slip for p in probs]
    threshold = number(trade.get("minimum_edge", 0.05), 0)
    decision = "INSUFFICIENT_EVIDENCE" if issues else (
        "CANDIDATE" if ev[0] >= threshold else "NO_TRADE")
    liquidation = sell["notional"] - q * (fee + slip)
    return {"version": VERSION, "evaluated_at": now.isoformat(), "decision": decision,
            "issues": sorted(set(issues)), "probability_range": probs, "calculation": method,
            "edge_per_share_range": ev, "entry": buy, "exit_now": sell,
            "max_loss": max_loss, "immediate_round_trip_pnl": liquidation - max_loss,
            "hold_vs_sell_now_per_share": None if sell["vwap"] is None else
                [p - (sell["vwap"] - fee - slip) for p in probs],
            "related_markets": relation_checks(case.get("relations", [])),
            "external_comparisons": [{"source": r["source"],
                "yes_probability": number(r["yes_probability"], 0, 1),
                "same_settlement": r.get("same_settlement") is True,
                "fresh": fresh(r["observed_at"], now, 1),
                "note": "Comparator only; not independent evidence or a forecast update"}
                for r in case.get("external_markets", [])],
            "limitation": "Assumption sensitivity, not a calibrated confidence interval. CANDIDATE is not an order."}


def evaluate_basket(case, now=None):
    """Scenario-based joint payoff model: never multiply marginal probabilities."""
    now = now or datetime.now(timezone.utc)
    legs = case["legs"]
    if not legs:
        raise ValueError("Empty basket")
    costs, issues = 0.0, []
    for leg in legs:
        if str(leg["book"].get("exchangeId")) != str(leg["exchange_id"]):
            issues.append("Book/leg mismatch")
        if not fresh(leg["book"]["asOf"]["at"], now, 60 / 3600):
            issues.append("Stale basket book")
        execution = executable(leg["book"], leg["side"], "buy", leg["quantity"])
        if not execution["complete"]:
            issues.append("Insufficient basket depth")
        costs += execution["notional"] + number(leg["quantity"], 1) * number(leg["cost_per_share"], 0)
    scenarios = case["scenarios"]
    if not scenarios or len({s["name"] for s in scenarios}) != len(scenarios):
        raise ValueError("Scenarios must be nonempty and uniquely named")
    if case.get("exhaustive_verified") is not True or not case.get("rule_sources"):
        issues.append("Scenario coverage/settlement rules not verified")
    payoffs = []
    for s in scenarios:
        if len(s["payout_per_share"]) != len(legs):
            raise ValueError("One payout per leg required")
        payoffs.append(sum(number(p, 0, 1) * number(l["quantity"], 1)
                           for p, l in zip(s["payout_per_share"], legs)))
    evs = []
    for weights in case.get("probability_scenarios", []):
        if len(weights) != len(scenarios):
            raise ValueError("Probability scenario dimensions differ")
        weights = [number(p, 0, 1) for p in weights]
        if not math.isclose(sum(weights), 1, abs_tol=1e-9):
            raise ValueError("Joint scenario probabilities must sum to1")
        evs.append(sum(p * payoff for p, payoff in zip(weights, payoffs)) - costs)
    return {"decision": "INSUFFICIENT_EVIDENCE" if issues else "REVIEW_BASKET",
            "issues": issues, "cost": costs, "payoff_floor": min(payoffs),
            "worst_case_profit": min(payoffs) - costs,
            "expected_profit_range": [min(evs), max(evs)] if evs else None,
            "scenarios": dict(zip([s["name"] for s in scenarios], payoffs)),
            "limitation": "Floor conditional on supplied exhaustive scenarios and full fills. No automatic execution. Include cancellation/refund scenarios."}


def calibration(rows):
    """Evaluate timestamped forecasts, not in-sample fitted or post-result predictions."""
    if not rows:
        raise ValueError("No resolved predictions")
    errors = []
    for r in rows:
        p, y = number(r["probability"], 0, 1), number(r["outcome"], 0, 1)
        if y not in (0, 1) or timestamp(r["predicted_at"]) >= timestamp(r["resolved_at"]):
            raise ValueError("Invalid outcome or look-ahead prediction")
        errors.append((p - y) ** 2)
    return {"n": len(errors), "brier_score": sum(errors) / len(errors),
            "note": "Lower is better; compare against a baseline on identical events. Correlated events are not independent samples."}


def journal(path, case, result):
    blob = json.dumps(case, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, at TEXT, input_hash TEXT, input_json TEXT, output_json TEXT)")
        db.execute("INSERT INTO decisions(at,input_hash,input_json,output_json) VALUES (?,?,?,?)",
                   (result.get("evaluated_at", datetime.now(timezone.utc).isoformat()), digest, blob, json.dumps(result)))
    return digest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case")
    p.add_argument("--mode", choices=["trade", "basket", "calibration"], default="trade")
    p.add_argument("--as-of", help="Replay/demo only; omit for a live decision")
    p.add_argument("--journal", help="Private SQLite audit file; keep out of git")
    args = p.parse_args()
    case = json.loads(Path(args.case).read_text())
    now = timestamp(args.as_of) if args.as_of else None
    try:
        result = (evaluate(case, now) if args.mode == "trade" else
                  evaluate_basket(case, now) if args.mode == "basket" else calibration(case))
        if args.as_of:
            result["replay_only"] = True
        if args.journal:
            result["input_hash"] = journal(args.journal, case, result)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (KeyError, ValueError, TypeError) as exc:
        print(json.dumps({"decision": "INVALID_INPUT", "error": str(exc)}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
