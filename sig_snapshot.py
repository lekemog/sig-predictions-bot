"""Read-only SIG snapshots. No order placement/cancellation endpoints."""
import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://sig.thesuper.market/api/v1"
TOURNAMENT = "bda92870-621e-47b0-bc3c-3602c5c26f55"


def snapshot(exchange_id, key):
    if not exchange_id.isdigit():
        raise ValueError("Numeric exchange ID required")
    path = f"/exchanges/{exchange_id}/orderbook?depth=100&tournamentId={TOURNAMENT}"
    # Never print request objects, headers, or keys. No arbitrary destination input.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        result = json.load(response)
    result["received_at"] = datetime.now(timezone.utc).isoformat()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exchange_id")
    parser.add_argument("--output", default="private/book.json")
    args = parser.parse_args()
    key = os.environ.get("SIG_API_KEY")
    if not key:
        parser.error("Set SIG_API_KEY privately in the environment")
    try:
        data = snapshot(args.exchange_id, key)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2))
        print(f"Saved read-only snapshot: {out}")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise SystemExit("Snapshot failed; no decision should use a missing/stale book")
