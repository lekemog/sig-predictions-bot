"""Public GET-only research collector. Never imports the SIG executor."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CUTOFF = datetime(2026, 11, 4, 17, tzinfo=timezone.utc)


def now():
    return datetime.now(timezone.utc).isoformat()


def get(url):
    # No authentication, POST, private account, or order submission endpoints.
    with urlopen(Request(url, headers={"User-Agent": "SIG-research/1.0"}), timeout=15) as response:
        return json.load(response)


def discover(venue, fetch=get, max_pages=250, deadline=None):
    rows, seen = [], set()
    cursor = ""
    for page in range(max_pages):
        if deadline is not None and time.monotonic() >= deadline:
            return rows, False, "Discovery runtime budget reached"
        if venue == "kalshi":
            params = {"status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            url = KALSHI + "/markets?" + urlencode(params)
            data = fetch(url)
            batch = data.get("markets")
            cursor = data.get("cursor")
            if not isinstance(batch, list) or not isinstance(cursor, str):
                raise ValueError("Missing Kalshi pagination completeness evidence")
            done = not cursor
        elif venue == "polymarket":
            url = GAMMA + "/markets?" + urlencode({"active": "true", "closed": "false", "limit": 100, "offset": page * 100, "order": "id", "ascending": "true"})
            batch = fetch(url)
            if not isinstance(batch, list):
                raise ValueError("Invalid Polymarket catalog response")
            done = not batch
        else:
            raise ValueError("Unknown venue")
        ids = [str(m.get("ticker") if venue == "kalshi" else m.get("id")) for m in batch]
        if any(i == "None" or i in seen for i in ids) or len(ids) != len(set(ids)):
            return rows, False, "Duplicate or missing market IDs; catalog changed during pagination"
        seen.update(ids)
        rows.extend({"venue": venue, "observed_at": now(), "source_url": url, "market": m} for m in batch)
        if done:
            return rows, True, None
        if venue == "kalshi" and cursor in seen:
            return rows, False, "Repeated cursor"
        if venue == "kalshi":
            seen.add(cursor)
    return rows, False, "Discovery page budget reached"


def election_lead(row):
    m = row["market"]
    title = " ".join(str(m.get(k, "")) for k in ("title", "question", "subtitle", "event_ticker", "ticker"))
    # Broad research lead filter, never a settlement match or trading approval.
    return bool(re.search(r"senate|governor|house|midterm|congress|election", title, re.I))


def tokens(m):
    values = m.get("clobTokenIds", [])
    if isinstance(values, str):
        values = json.loads(values)
    if not isinstance(values, list):
        raise ValueError("Invalid token IDs")
    return values


def scan(fetch=get, max_books=300, runtime_seconds=420):
    started = now()
    result = {"started_at": started, "execution_authorized": False,
              "status": "success", "catalogs": {}, "books": [], "errors": [],
              "comparison_status": "UNKNOWN", "calibrated_probability": None,
              "limitations": ["No reviewed exact cross-venue settlement mappings or synchronized SIG books.",
                              "Catalog prices and observation times are not verified executable quote timestamps.",
                              "Mutable catalogs can change during pagination; no atomic snapshot guarantee."]}
    deadline = time.monotonic() + runtime_seconds
    for venue in ("kalshi", "polymarket"):
        try:
            # Reserve time for the other venue and book collection.
            discovery_deadline = min(deadline, time.monotonic() + runtime_seconds / 4)
            rows, complete, reason = discover(venue, fetch, deadline=discovery_deadline)
            result["catalogs"][venue] = {"rows": rows, "count": len(rows), "complete": complete, "limitation": reason}
        except Exception as exc:
            result["errors"].append({"venue": venue, "stage": "discovery", "error": str(exc)})
            result["catalogs"][venue] = {"rows": [], "count": 0, "complete": False}
    tasks = []
    for venue, catalog in result["catalogs"].items():
        for row in catalog["rows"]:
            if not election_lead(row):
                continue
            m = row["market"]
            try:
                if venue == "kalshi":
                    tasks.append((venue, m["ticker"], KALSHI + "/markets/" + m["ticker"] + "/orderbook"))
                else:
                    for token in tokens(m):
                        tasks.append((venue, str(m["id"]), CLOB + "/book?" + urlencode({"token_id": token})))
            except Exception as exc:
                result["errors"].append({"venue": venue, "stage": "tokens", "error": str(exc)})
    # Both venues receive book sampling capacity; no claim of full book coverage.
    tasks.sort(key=lambda t: (t[1], t[0]))
    groups = [[t for t in tasks if t[0] == v] for v in ("kalshi", "polymarket")]
    balanced = []
    for i in range(max(map(len, groups), default=0)):
        balanced.extend(g[i] for g in groups if i < len(g))
    result["candidate_book_count"] = len(tasks)
    for venue, market_id, url in balanced[:max_books]:
        if time.monotonic() >= deadline:
            break
        try:
            book = fetch(url)
            result["books"].append({"venue": venue, "market_id": market_id, "source_url": url,
                                     "observed_at": now(), "quote_timestamp_verified": False,
                                     "raw_book": book})
        except Exception as exc:
            result["errors"].append({"venue": venue, "market_id": market_id, "stage": "book", "error": str(exc)})
    result["book_count"] = len(result["books"])
    result["book_coverage_complete"] = len(result["books"]) == len(tasks)
    if result["errors"] or not all(c["complete"] for c in result["catalogs"].values()) or not result["book_coverage_complete"]:
        result["status"] = "partial"
    result["finished_at"] = now()
    return result


if __name__ == "__main__":
    if datetime.now(timezone.utc) >= CUTOFF:
        raise SystemExit("Research collection cutoff reached")
    output = scan()
    Path("venue_observations.json").write_text(json.dumps(output, separators=(",", ":")))
    print(json.dumps({k: v for k, v in output.items() if k not in ("catalogs", "books")}))
    if not any(c["count"] for c in output["catalogs"].values()):
        raise SystemExit(1)
