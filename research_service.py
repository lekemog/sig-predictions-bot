"""Persistent GET-only observations. Does not execute or dispatch trading jobs.

One process, one durable disk. A watchlist holds at most three exchange IDs.
SIG_API_KEY is read from the host's secret environment, never a CLI argument.
"""
import argparse
import fcntl
import json
import os
import signal
import threading
from pathlib import Path

from market_brain import BrainStore, CUTOFF
from research_runner import ReadOnlyAPI, collect, research
from trading_bot import Halt, now


def watchlist(path):
    ids = json.loads(path.read_text()) if path else []
    if not isinstance(ids, list) or len(ids) > 3:
        raise ValueError('Watchlist must contain at most three exchange IDs')
    if any(not isinstance(i, str) or not i.isdecimal() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Watchlist needs unique numeric string IDs')
    return ids


def observe_books(api, store, ids, catalog, clock=now):
    observations, failures = [], []
    for exchange_id in ids:
        if clock() >= CUTOFF:
            break
        try:
            if exchange_id not in catalog:
                raise Halt('Watch exchange absent from current open universe')
            book = api.book(exchange_id)
            if str(book['marketId']) != catalog[exchange_id]['market_id']:
                raise Halt('Watch book identity mismatch')
            row = dict(kind='book_observation', observed_at=clock().isoformat(),
                       exchange_id=exchange_id, book=book, paper_fills='UNKNOWN')
            store.observe(row)
            observations.append(exchange_id)
        except (Halt, ValueError, KeyError, TypeError):
            failures.append(exchange_id)  # Never serialize transport exception text.
    return dict(observed=observations, unavailable=failures, orders_submitted=0)


def run(api, store, ids, stop, interval=60, catalog_interval=600, clock=now,
        once=False):
    if interval < 60 or catalog_interval < interval:
        raise ValueError('Polling interval must be >=60s; catalogue interval >= polling')
    catalog, last_catalog = {}, None
    while not stop.is_set() and clock() < CUTOFF:
        at = clock()
        try:
            if last_catalog is None or (at-last_catalog).total_seconds() >= catalog_interval:
                # Retain the entire actual catalogue. No implied relationship discovery.
                snapshot = collect(api, {}, maximum_candidates=0)
                research(snapshot, store, at)
                catalog, last_catalog = snapshot['exchange_catalog'], at
            result = observe_books(api, store, ids, catalog, clock)
            result.update(kind='feed_health', at=clock().isoformat(),
                          catalogue_at=last_catalog.isoformat(), mode='read-only-polling')
            store.observe(result)
            print(json.dumps(result), flush=True)
        except (Halt, ValueError, KeyError, TypeError):
            catalog, last_catalog = {}, None  # Failed discovery invalidates watch identities.
            result = dict(kind='feed_health', at=clock().isoformat(),
                          mode='read-only-polling', status='UNAVAILABLE', orders_submitted=0)
            store.observe(result)
            print(json.dumps(result), flush=True)
        if once:
            return result
        stop.wait(min(interval, max(0, (CUTOFF-clock()).total_seconds())))
    return dict(status='STOPPED', orders_submitted=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--watchlist', type=Path)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if now() >= CUTOFF:
        return
    ids = watchlist(args.watchlist)
    os.umask(0o077)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with Path(str(args.database)+'.service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Observation service already running') from None
        store = BrainStore(args.database)
        try:
            # Conservative research sub-budget; all retries are counted.
            run(ReadOnlyAPI(os.environ.get('SIG_API_KEY'), store, read_ceiling=20),
                store, ids, stop, once=args.once)
        finally:
            store.db.close()


if __name__ == '__main__':
    main()
