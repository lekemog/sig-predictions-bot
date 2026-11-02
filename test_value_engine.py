import json
import tempfile
import unittest
from pathlib import Path

import value_engine as V
from test_arb_engine import UniverseFake, arb_config
from trading_bot import Halt, Journal, Runner, now, value_plan


def fair_cfg(legs, **value):
    c = arb_config()
    c['risk_limits'] = {'committed': 100000, 'reserve': 2000, 'clip': 5000, 'race': 3000,
                        'directional': 60000, 'factor': 60000}
    c['value'] = dict({'enabled': True, 'min_edge': .03, 'exit_edge': .01, 'race_cap': 3000,
                       'min_profit': 5}, **value)
    c['_fair'] = {'at': now().isoformat(), 'legs': legs}
    return c


RACE = {'name': 'Auto Race', 'clip': 1000, 'auto': True, 'legs': [
    {'market_id': '3', 'exchange_id': '33', 'party': 'Democratic Party'},
    {'market_id': '4', 'exchange_id': '44', 'party': 'Republican Party'}]}


class FairTests(unittest.TestCase):
    def test_band_uses_lowest_bid_and_highest_ask(self):
        m = {'1': {'kalshi': 'K', 'poly': {'slug': 's', 'group': 'A (D)'}}}
        out = V.fair_table(m, {'K': (.90, .91)}, {('s', 'A (D)'): (.92, .93)})
        self.assertEqual((out['1']['lo'], out['1']['hi']), (.90, .93))
        self.assertEqual(out['1']['sources'], ['kalshi', 'polymarket'])

    def test_wide_or_disagreeing_venues_are_dropped(self):
        m = {'1': {'kalshi': 'K', 'poly': None}, '2': {'kalshi': 'K2', 'poly': {'slug': 's', 'group': 'g'}}}
        out = V.fair_table(m, {'K': (.50, .60), 'K2': (.50, .51)}, {('s', 'g'): (.60, .61)})
        self.assertEqual(out, {})

    def test_side_fair_flips_for_no(self):
        f = {'lo': .9, 'hi': .92, 'mid': .91}
        self.assertEqual(V.side_fair(f, 'yes'), (.9, .91, .92))
        lo, mid, hi = V.side_fair(f, 'no')
        self.assertAlmostEqual(lo, .08); self.assertAlmostEqual(hi, .1)

    def test_buy_walk_respects_price_budget_and_cap(self):
        levels = [(.80, 100), (.85, 100), (.90, 100)]
        self.assertEqual(V.buy_walk(levels, .86, 10000, 1000)[0], 200)
        self.assertEqual(V.buy_walk(levels, .86, 100, 1000)[0], 123)  # 100 @ .80 + 23 @ .85
        self.assertEqual(V.buy_walk(levels, .86, 10000, 50)[0], 50)

    def test_kalshi_batches_and_skips_closed(self):
        calls = []
        def get(url):
            calls.append(url)
            return {'markets': [{'ticker': 'A', 'status': 'active', 'yes_bid_dollars': '0.40', 'yes_ask_dollars': '0.41'},
                                {'ticker': 'B', 'status': 'closed', 'yes_bid_dollars': '0.40', 'yes_ask_dollars': '0.41'}]}
        self.assertEqual(V.kalshi_quotes(['A', 'B'], get), {'A': (.40, .41)})
        self.assertEqual(len(calls), 1)


class PlanTests(unittest.TestCase):
    # UniverseFake books for 33/44: YES bid .52, ask .53 (NO costs .48).
    def test_buys_side_sig_underprices(self):
        api = UniverseFake()
        p = value_plan(api, api.state(), fair_cfg({'33': {'lo': .60, 'hi': .61, 'mid': .605, 'sources': ['kalshi']}}), RACE)
        self.assertEqual((p['kind'], p['side'], p['prices']), ('value_entry', 'yes', [.53]))
        self.assertLessEqual(p['max_spend'], 3000)
        self.assertGreater(p['robust_profit'], 0)

    def test_no_trade_inside_band(self):
        api = UniverseFake()
        self.assertIsNone(value_plan(api, api.state(), fair_cfg({'33': {'lo': .54, 'hi': .55, 'mid': .545, 'sources': ['kalshi']}}), RACE))

    def test_stale_fair_never_trades(self):
        api = UniverseFake()
        cfg = fair_cfg({'33': {'lo': .80, 'hi': .81, 'mid': .805, 'sources': ['kalshi']}})
        cfg['_fair']['at'] = '2026-01-01T00:00:00+00:00'
        self.assertIsNone(value_plan(api, api.state(), cfg, RACE))

    def test_sells_held_side_above_band(self):
        api = UniverseFake()
        api.quantities['33'] = 100
        p = value_plan(api, api.state(), fair_cfg({'33': {'lo': .40, 'hi': .41, 'mid': .405, 'sources': ['kalshi']}}), RACE)
        self.assertEqual((p['kind'], p['action'], p['side'], p['quantity']), ('value_exit', 'sell', 'yes', 100))

    def test_race_cap_limits_spend(self):
        api = UniverseFake()
        api.quantities['33'] = 5000
        state = api.state()
        for row in state['positions']:
            row['costBasis'] = 2990
        p = value_plan(api, state, fair_cfg({'44': {'lo': .80, 'hi': .81, 'mid': .805, 'sources': ['kalshi']}}), RACE)
        self.assertIsNone(p)  # 10 left under the 3000 race cap can't clear min_profit

    def test_stop_loss_exits_when_fair_falls_far_below_cost(self):
        api = UniverseFake()
        api.quantities['33'] = 100
        state = api.state()
        for row in state['positions']:
            row['avgCost'] = .60
        band = {'33': {'lo': .52, 'hi': .54, 'mid': .53, 'sources': ['kalshi']}}
        self.assertIsNone(value_plan(api, state, fair_cfg(band), RACE))
        p = value_plan(api, state, fair_cfg(band, stop_loss=.05), RACE)
        self.assertEqual((p['kind'], p['exit_reason'], p['quantity']), ('value_exit', 'stop_loss', 100))
        self.assertIsNone(value_plan(api, state, fair_cfg(band, stop_loss=.10), RACE))

    def test_final_hours_blackout_blocks_entries(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        cfg = fair_cfg({'33': {'lo': .70, 'hi': .71, 'mid': .705, 'sources': ['kalshi']}})
        cfg['_universe'] = [RACE]
        cfg['entry_blackout_hours'] = 10 ** 6
        result = Runner(UniverseFake(), journal, cfg, live=False).cycle()
        self.assertEqual(result[0]['decision'], 'NO_TRADE')

    def test_cycle_selects_and_dry_runs_value_trade(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        cfg = fair_cfg({'33': {'lo': .70, 'hi': .71, 'mid': .705, 'sources': ['kalshi']}})
        cfg['_universe'] = [RACE]
        result = Runner(UniverseFake(), journal, cfg, live=False).cycle()
        self.assertTrue(result[0]['dry_run'])
        self.assertEqual(result[0]['plan']['kind'], 'value_entry')


if __name__ == '__main__':
    unittest.main()


class HaltRegressionTests(unittest.TestCase):
    def test_value_selection_drops_same_race_arb_basket(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        cfg = fair_cfg({'33': {'lo': .70, 'hi': .71, 'mid': .705, 'sources': ['kalshi']}})
        cfg['_universe'] = [RACE]   # UniverseFake also prices this race as an arb
        result = Runner(UniverseFake(), journal, cfg, live=False).cycle()
        self.assertEqual(result[0]['plan']['kind'], 'value_entry')
        self.assertEqual([b['name'] for b in cfg['baskets']], ['test'])

    def test_live_value_trade_completes_without_halting(self):
        # Reproduces 18 Oct 02:34 UTC: fill succeeded, then post-fill check halted.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        cfg = fair_cfg({'33': {'lo': .70, 'hi': .71, 'mid': .705, 'sources': ['kalshi']}})
        cfg['_universe'] = [RACE]
        api = UniverseFake(); api.fill = (5000, 5000)
        result = Runner(api, journal, cfg, live=True).cycle()
        self.assertIn('completed', result[0])
        self.assertEqual(journal.pending(), [])


class PostTradeReadTests(unittest.TestCase):
    def test_brief_503_after_fill_does_not_halt(self):
        # Reproduces 20 Oct 06:28 UTC: order filled, then fill reads got HTTP 503.
        import trading_bot as T
        calls = {'n': 0}
        api = T.API('test-key')
        def fake_open(req, timeout=20):
            calls['n'] += 1
            if calls['n'] <= 4:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, 503, 'busy', {}, None)
            import io
            class R(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R(b'{"ok": true}')
        api.opener.open = fake_open
        orig = T.time.sleep
        T.time.sleep = lambda s: None
        try:
            self.assertEqual(api.get('/orders/1/fills?limit=100', attempts=T.POST_TRADE_READS), {'ok': True})
            calls['n'] = 0
            with self.assertRaises(Halt):
                api.get('/orders/1/fills?limit=100')  # ordinary reads still give up after 3
        finally:
            T.time.sleep = orig


class UnmarriedExitTests(unittest.TestCase):
    # UniverseFake 33: YES bid .52 / ask .53.  We hold 100 YES.
    def plan(self, fair, hold_edge=.02):
        api = UniverseFake(); api.quantities['33'] = 100
        cfg = fair_cfg({'33': dict(fair, sources=['kalshi'])}, hold_edge=hold_edge, min_profit=5)
        return value_plan(api, api.state(), cfg, RACE)

    def test_sells_when_news_moves_fair_to_the_bid(self):
        p = self.plan({'lo': .53, 'hi': .54, 'mid': .535})   # bid .52 >= .535 - .02
        self.assertEqual((p['kind'], p['exit_reason'], p['quantity']), ('value_exit', 'edge_gone', 100))

    def test_holds_while_edge_remains(self):
        p = self.plan({'lo': .60, 'hi': .61, 'mid': .605})   # bid .52 < .585: keep holding
        self.assertNotEqual((p or {}).get('kind'), 'value_exit')

    def test_overpriced_exit_still_labelled(self):
        p = self.plan({'lo': .45, 'hi': .46, 'mid': .455})
        self.assertEqual(p['exit_reason'], 'overpriced')

    def test_disabled_without_hold_edge(self):
        p = self.plan({'lo': .53, 'hi': .54, 'mid': .535}, hold_edge=None)
        self.assertNotEqual((p or {}).get('kind'), 'value_exit')


class CalledRaceQuoteTests(unittest.TestCase):
    def test_called_race_quotes_with_empty_side_are_kept(self):
        def get(url):
            return {'markets': [{'ticker': 'A', 'status': 'active', 'yes_bid_dollars': '0.99', 'yes_ask_dollars': '1.00'},
                                {'ticker': 'B', 'status': 'active', 'yes_bid_dollars': '0.00', 'yes_ask_dollars': '0.01'}]}
        q = V.kalshi_quotes(['A', 'B'], get)
        self.assertEqual(q, {'A': (.99, 1.0), 'B': (0.0, .01)})
        out = V.fair_table({'1': {'kalshi': 'A'}, '2': {'kalshi': 'B'}}, q, {})
        self.assertEqual((out['1']['lo'], out['2']['hi']), (.99, .01))
        self.assertEqual(V.fair_table({'3': {'kalshi': 'C'}}, {'C': (0.0, 1.0)}, {}), {})  # empty book: too wide


class CapitalRecyclingTests(unittest.TestCase):
    """Cash-bound value edges must be able to pull capital out of thin arbs."""
    FAIR = {'33': {'lo': .70, 'hi': .71, 'mid': .705, 'sources': ['kalshi']}}

    def test_cash_bound_value_edge_reports_capital_blocked(self):
        api = UniverseFake(); api.cash = 2100   # 100 above reserve: below min_profit size
        events = []
        cfg = fair_cfg(self.FAIR, min_profit=50)
        self.assertIsNone(value_plan(api, api.state(), cfg, RACE, entry_observer=events.append))
        b = [e for e in events if isinstance(e, dict) and e.get('event') == 'capital_blocked']
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0]['source'], b[0]['side']), ('value', 'yes'))
        self.assertAlmostEqual(b[0]['roi'], (.70 - .53) / .53, places=3)
        self.assertGreater(b[0]['shortfall'], 2000)

    def test_directional_cap_is_not_reported_as_capital(self):
        # Selling an arb frees cash, not directional room: nothing to recycle for.
        api = UniverseFake(); api.cash = 2100
        cfg = fair_cfg(self.FAIR, min_profit=50)
        cfg['risk_limits']['directional'] = 3000
        cfg['risk_limits']['factor'] = 3000
        cfg['risk_limits']['race'] = 3000
        state = api.state()
        state['positions'].append({'exchangeId': '99', 'quantity': 6000, 'costBasis': 3000})
        events = []
        self.assertIsNone(value_plan(api, state, cfg, RACE, entry_observer=events.append))
        self.assertFalse([e for e in events if isinstance(e, dict) and e.get('event') == 'capital_blocked'])

    def test_cash_recycling_does_not_override_partial_exit_repair_budget(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        api = UniverseFake()
        api.quantities.update({'11': -5000, '22': -5000})   # held arb basket 'test'
        api.cash = 2100
        orig = api.book
        # Held arb sells at .99 per matched share (< 1.0 floor): only worth it to redeploy.
        api.book = lambda i: dict(orig(i), asks=[{'price': .505, 'quantity': 10000}]) if i in ('11', '22') else orig(i)
        cfg = fair_cfg(self.FAIR, min_profit=50)
        cfg['arb']['min_profit'] = 0
        cfg['baskets'][0]['clip'] = 5000
        cfg['_universe'] = [RACE]
        result = Runner(api, journal, cfg, live=False).cycle()
        self.assertEqual(result[0]['decision'], 'NO_TRADE')
        blocked = [json.loads(row[0]) for row in journal.db.execute(
            "SELECT body FROM events WHERE kind = 'exit_blocked'")]
        self.assertTrue(any('repair budget' in e['reason'] for e in blocked))
        self.assertEqual(journal.pending(), [])

