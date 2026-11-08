"""Full-universe research collector and shared scanner entry point.

    python research_runner.py --snapshot input.json --database private/brain.sqlite
    python research_runner.py --collect --database private/brain.sqlite

Collection uses only GETs. No live mode, order adapter, signal-to-order conversion,
or automatic forecast calibration is exposed. Run only alongside an agreed owner
with a shared read budget; do not create independent replicas per scanner.
"""
import argparse
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from market_brain import (BrainStore, CUTOFF, digest, quote_proposal,
                          relation_candidates, scan_parallel, turnover)
from trading_bot import API, Halt, TID, now


class ReadOnlyAPI(API):
    def __init__(self, key, store, read_ceiling=80):
        super().__init__(key)
        underlying = self.opener
        class BudgetedOpener:
            def open(self, request, *args, **kwargs):
                if request.get_method() != 'GET':
                    raise Halt('Research transport prohibits writes')
                store.take_read(time.time(), read_ceiling)  # Count GET retries too.
                return underlying.open(request, *args, **kwargs)
        self.opener = BudgetedOpener()

    def request(self, method, path, payload=None):
        if method != 'GET' or payload is not None:
            raise Halt('Research client prohibits write operations')
        return super().request(method, path)


def pages(api, path, max_pages=20):
    rows, cursor, seen = [], None, set()
    for _ in range(max_pages):
        result = api.get(path + ('&cursor='+quote(cursor,safe='') if cursor else ''))
        if not isinstance(result.get('data'),list):
            raise Halt('Invalid discovery page')
        rows.extend(result['data'])
        p = result['pagination']
        if not p['hasMore']:
            return rows
        cursor = p['nextCursor']
        if not cursor or cursor in seen:
            raise Halt('Discovery pagination incomplete or looping')
        seen.add(cursor)
    raise Halt('Discovery page bound reached; universe coverage unknown')


def collect(api, proofs, maximum_candidates=3):
    """Discover all accessible open markets; inspect depth for <=3 strong leads.

    Latest prices screen leads only. Actual candidate evaluation consumes books.
    Relationship proofs are manually reviewed, hash-bound and time-limited.
    """
    if isinstance(maximum_candidates, bool) or not isinstance(maximum_candidates, int) or not 0 <= maximum_candidates <= 3:
        raise ValueError('Detailed candidate bound must be between zero and three')
    markets = pages(api, f'/markets?limit=100&status=open&tournamentId={TID}')
    catalog = {}
    for m in markets:
        contexts = [c for c in m['contexts'] if (c.get('tournament') or {}).get('id') == TID]
        if len(contexts) != 1 or contexts[0]['status'] != 'open':
            raise Halt('Discovery tournament context mismatch')
        for e in m['exchanges']:
            key = str(e['id'])
            if key in catalog:
                raise Halt('Duplicate exchange in market discovery')
            catalog[key] = dict(market_id=str(m['id']), title=m['title'],
                                settlement_date=m['settlementDate'])
    relations = pages(api, f'/relationships?limit=200&tournamentId={TID}')
    leads = []
    for r in relations:
        ids = [str(n['exchangeId']) for n in r['nodes']]
        if r['status'] != 'active' or not 2 <= len(ids) <= 10 or not all(i in catalog for i in ids):
            continue
        prices = [n.get('currentPrice') for n in r['nodes']]
        score = 0
        if all(isinstance(p,(int,float)) and not isinstance(p,bool) and 0 <= p <= 1 for p in prices):
            if r['type'] in ('complementary','boolean_not'):
                score = abs(sum(prices)-1)
            elif r['type'] == 'mutually_exclusive':
                score = max(0,sum(prices)-1)
            elif r['type'] == 'implication':
                mapping = {str(n['exchangeId']):n['currentPrice'] for n in r['nodes']}
                a,b = r['direction']['fromExchangeIds'],r['direction']['toExchangeIds']
                if len(a)==len(b)==1:
                    score = max(0,mapping[str(a[0])]-mapping[str(b[0])])
        leads.append((score,r))
    leads.sort(key=lambda x:(-x[0],x[1]['id']))
    selected = [r for _,r in leads[:maximum_candidates]]
    books, failures = {}, []
    for r in selected:
        for n in r['nodes']:
            i = str(n['exchangeId'])
            if i in books:
                continue
            try:
                books[i] = api.book(i)
                if str(books[i]['marketId']) != catalog[i]['market_id']:
                    raise Halt('Book discovery identity mismatch')
            except (Halt, ValueError):
                failures.append(dict(exchange_id=i,category='READ_UNAVAILABLE'))
    return dict(at=now().isoformat(), tournament_id=TID,
        catalog_count=len(markets), exchange_count=len(catalog),
        markets=markets, exchange_catalog=catalog, relationship_catalog=relations,
        relationship_count=len(relations), universe_discovery_complete=True,
        relations=selected, books=books, proofs=proofs, read_failures=failures,
        note='Current-price lead ranking only; selected depth is not whole-universe executable coverage')


def research(snapshot, store, now_at=None):
    at = now_at or datetime.now(timezone.utc)
    if at >= CUTOFF:
        return dict(status='CUTOFF',candidates=[])
    if snapshot['tournament_id'] != TID:
        raise ValueError('Wrong tournament snapshot')
    store.observe(dict(kind='research_snapshot', snapshot=snapshot))
    jobs = []
    for r in snapshot.get('relations',[])[:3]:
        proof = snapshot.get('proofs',{}).get(r['id'],{})
        jobs.append((r['id'],lambda r=copy.deepcopy(r),p=copy.deepcopy(proof):
            relation_candidates(r,copy.deepcopy(snapshot['books']),p,at)))
    for i,c in enumerate(snapshot.get('turnover_cases',[])[:3]):
        jobs.append(('turnover-'+str(i),lambda c=copy.deepcopy(c):turnover(c,at)))
    for i,c in enumerate(snapshot.get('quote_cases',[])[:3]):
        jobs.append(('maker-'+str(i),lambda c=copy.deepcopy(c):
            quote_proposal(c['book'],c['model'],c['inventory'],at,c.get('clip',100))))
    result = scan_parallel(jobs[:3])  # At most three detailed candidates per review.
    result.update(at=at.isoformat(),mode='research',orders_submitted=0,
                  detailed_candidates_reviewed=min(3,len(jobs)),
                  live_readiness='NOT_ASSESSED',
                  snapshot_id=digest(snapshot),universe_discovery_complete=
                  snapshot.get('universe_discovery_complete',False))
    store.observe(dict(kind='scanner_results', result=result))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--snapshot',type=Path)
    source.add_argument('--collect',action='store_true')
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--proofs',type=Path)
    p.add_argument('--evidence',type=Path)
    args = p.parse_args()
    if now() >= CUTOFF:
        print(json.dumps(dict(status='CUTOFF',orders_submitted=0))); return
    args.database.parent.mkdir(parents=True,exist_ok=True)
    store = BrainStore(args.database)
    if args.evidence:
        for item in json.loads(args.evidence.read_text()):
            store.add_evidence(item)
    if args.collect:
        proofs = json.loads(args.proofs.read_text()) if args.proofs else {}
        snapshot = collect(ReadOnlyAPI(os.environ.get('SIG_API_KEY'),store),proofs)
    else:
        snapshot = json.loads(args.snapshot.read_text())
    try:
        result = research(snapshot,store)
        print(json.dumps(result,allow_nan=False))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
