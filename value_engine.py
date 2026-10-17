"""Kalshi/Polymarket-anchored fair values for SIG party-winner legs.

SIG compresses longshots: favourites trade cheap and underdogs rich compared
with Kalshi's 1-cent books. We treat the outside venues as the fair price and
trade SIG only when its executable price is outside the outside BID/ASK band
by a margin, so a stale or noisy mid can't create a fake edge:

    buy YES on SIG  if  sig_yes_ask <= fair_lo - min_edge   (fair_lo = lowest outside bid)
    buy NO  on SIG  if  sig_no_ask  <= (1 - fair_hi) - min_edge
    sell held side  if  SIG bid beats the opposite edge of the band by exit_edge

GET-only; no SIG credentials and no orders here.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

KALSHI = 'https://api.elections.kalshi.com/trade-api/v2'
GAMMA = 'https://gamma-api.polymarket.com'


def http_get(url, attempts=3):
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'sig-value-reference'})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
                continue
            raise ValueError(f'reference HTTP {e.code}') from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
                continue
            raise ValueError('reference fetch failed') from None


def _quote(bid, ask):
    try:
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (0 < bid < ask < 1) or not all(map(math.isfinite, (bid, ask))):
        return None
    return bid, ask


def kalshi_quotes(tickers, get=http_get):
    out = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 100):
        chunk = ','.join(tickers[i:i + 100])
        r = get(f'{KALSHI}/markets?limit=100&tickers={urllib.parse.quote(chunk, safe=",")}')
        for m in r.get('markets', []):
            if m.get('status') not in ('active', 'open'):
                continue
            q = _quote(m.get('yes_bid_dollars'), m.get('yes_ask_dollars'))
            if q:
                out[m['ticker']] = q
    return out


def poly_quotes(slugs, get=http_get):
    out = {}
    slugs = sorted(set(slugs))
    for i in range(0, len(slugs), 40):
        qs = '&'.join('slug=' + urllib.parse.quote(s) for s in slugs[i:i + 40])
        for e in get(f'{GAMMA}/events?{qs}'):
            for m in e.get('markets', []):
                if not m.get('active') or m.get('closed'):
                    continue
                q = _quote(m.get('bestBid'), m.get('bestAsk'))
                if q:
                    out[(e['slug'], m.get('groupItemTitle'))] = q
    return out


def fair_table(mapping, kq, pq, max_spread=.03, max_disagree=.05):
    """Per SIG exchange: conservative YES band [lo, hi] across usable venues.

    lo = lowest bid, hi = highest ask. A venue counts only with a spread of at
    most max_spread; venues whose mids disagree by more than max_disagree are a
    mapping/settlement red flag, so the leg is dropped entirely.
    """
    out = {}
    for ex, row in mapping.items():
        quotes = []
        if row.get('kalshi') and row['kalshi'] in kq:
            quotes.append(('kalshi', kq[row['kalshi']]))
        p = row.get('poly')
        if p and (p['slug'], p['group']) in pq:
            quotes.append(('polymarket', pq[(p['slug'], p['group'])]))
        quotes = [(v, q) for v, q in quotes if q[1] - q[0] <= max_spread + 1e-9]
        if not quotes:
            continue
        mids = [(q[0] + q[1]) / 2 for _, q in quotes]
        if max(mids) - min(mids) > max_disagree:
            continue
        out[str(ex)] = {'lo': min(q[0] for _, q in quotes), 'hi': max(q[1] for _, q in quotes),
                        'mid': sum(mids) / len(mids), 'sources': [v for v, _ in quotes]}
    return out


def fetch_fair(mapping, get=http_get, at=None):
    kq = kalshi_quotes([r['kalshi'] for r in mapping.values() if r.get('kalshi')], get)
    pq = {}
    try:
        pq = poly_quotes([r['poly']['slug'] for r in mapping.values() if r.get('poly')], get)
    except ValueError:
        pass  # Kalshi alone is enough; Polymarket only narrows or vetoes.
    return {'at': at, 'legs': fair_table(mapping, kq, pq),
            'kalshi_quotes': len(kq), 'poly_quotes': len(pq)}


def side_fair(fair, side):
    """(conservative buy value, mid, conservative sell value) for one side."""
    if side == 'yes':
        return fair['lo'], fair['mid'], fair['hi']
    return 1 - fair['hi'], 1 - fair['mid'], 1 - fair['lo']


def buy_walk(levels, max_price, budget, max_shares):
    """Take levels priced <= max_price within cash budget and share cap."""
    q, cost, worst = 0, 0.0, None
    for price, size in levels:
        if price > max_price + 1e-9 or q >= max_shares:
            break
        room = min(size, max_shares - q, math.floor((budget - cost) / price) if price > 0 else 0)
        room = int(room)
        if room <= 0:
            break
        q += room
        cost += room * price
        worst = price
    return q, cost, worst


def sell_walk(levels, min_price, max_shares):
    q, proceeds, worst = 0, 0.0, None
    for price, size in levels:
        if price < min_price - 1e-9 or q >= max_shares:
            break
        take = int(min(size, max_shares - q))
        if take <= 0:
            break
        q += take
        proceeds += take * price
        worst = price
    return q, proceeds, worst
