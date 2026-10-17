import copy
import tempfile
import unittest
from pathlib import Path

import arb_engine as A
from tests.test_trading_bot import FakeAPI, config
from trading_bot import END, Halt, Journal, Runner, arb_policy, basket_plan, now


def book(bids, asks, i='11'):
    t = now().isoformat()
    return {'exchangeId': i, 'asOf': {'at': t}, '_received_at': t,
            'bids': [{'price': p, 'quantity': q} for p, q in bids],
            'asks': [{'price': p, 'quantity': q} for p, q in asks]}


def arb_config(**extra):
    c = config()
    c['arb'] = dict({'enabled': True, 'min_edge_per_share': .005, 'min_profit': 1,
                     'p_double_yes': .002, 'leg_buffer': 'empirical'}, **extra)
    c['_execution_stats'] = {'jobs': 25, 'partial': 0}
    return c


class MathTests(unittest.TestCase):
    def test_no_buy_lifts_yes_bids_as_complements(self):
        self.assertEqual(A.ladder(book([(.52, 10), (.50, 5)], [(.53, 9)]), 'no', 'buy'),
                         [(.48, 10), (.5, 5)])
        self.assertEqual(A.ladder(book([(.52, 10)], [(.53, 9), (.55, 1)]), 'no', 'sell'),
                         [(.47, 9), (.45, 1)])

    def test_depth_walk_finds_size_halving_misses(self):
        # Leg A: 800 @ .48 then .53; leg B deep @ .48. Profitable to exactly 800.
        a = [(.48, 800), (.53, 5000)]
        b = [(.48, 5000)]
        best = A.optimal_entry([a, b], 1.0, 0, 0, 1000, .005, 1)
        self.assertEqual(best['quantity'], 800)
        self.assertAlmostEqual(best['robust_profit'], 800 * .04)
        # The legacy halving schedule 1000 -> 500 would have left 300 shares on the table.

    def test_robust_profit_uses_worst_level_vwap_is_reported(self):
        best = A.optimal_entry([[(.40, 100), (.42, 100)], [(.50, 1000)]], 1.0, 0, 0, 200, 0, 0)
        self.assertEqual(best['quantity'], 200)
        self.assertAlmostEqual(best['robust_spend'], 200 * .92)
        self.assertAlmostEqual(best['expected_spend'], 40 + 42 + 100)

    def test_fixed_execution_charge_can_kill_tiny_clips(self):
        self.assertIsNone(A.optimal_entry([[(.49, 10)], [(.5, 10)]], 1.0, 0, 5, 10, 0, 0))

    def test_beta_leg_risk(self):
        r = A.leg_risk({'jobs': 25, 'partial': 0}, 10)
        self.assertAlmostEqual(r['p_partial'], 1 / 27)
        self.assertAlmostEqual(r['expected_loss_per_job'], 20 / 27)
        self.assertAlmostEqual(A.leg_risk({}, 10)['p_partial'], .5)

    def test_execution_stats_detects_unequal_fills(self):
        def job(q2):
            return {'status': 'done', 'plan': {'kind': 'basket_entry'},
                    'request': {'legs': [{'exchangeId': '1', 'quantity': 10}, {'exchangeId': '2', 'quantity': 10}]},
                    'fills': {'a': {'exchangeId': '1', 'data': [{'quantity': -10}]},
                              'b': {'exchangeId': '2', 'data': [{'quantity': -q2}]}}}
        self.assertEqual(A.execution_stats([job(10), job(7), {'status': 'dry_run', 'plan': {'kind': 'basket_entry'}}]),
                         {'jobs': 2, 'partial': 1})

    def test_hold_threshold_is_floor_without_capital_pressure(self):
        self.assertEqual(A.hold_threshold(1, 0, .5), 1)
        self.assertAlmostEqual(A.hold_threshold(1, .04, .5), 1 / 1.02)

    def test_fair_value_devigs_microprices(self):
        fv = A.fair_value({'a': book([(.50, 100)], [(.52, 100)]), 'b': book([(.48, 100)], [(.50, 100)])})
        self.assertAlmostEqual(fv['overround'], .0)
        self.assertAlmostEqual(sum(fv['fair_yes'].values()), 1)

    def test_discover_only_clean_dr_races_not_configured(self):
        cat = {'1': {'market_id': 'm1', 'title': 'Will the Democratic Party win the Ohio Senate?'},
               '2': {'market_id': 'm2', 'title': 'Will the Republican Party win the Ohio Senate?'},
               '3': {'market_id': 'm3', 'title': 'Will the Democratic Party win the Utah Senate?'},
               '4': {'market_id': 'm4', 'title': 'Will the Republican Party win the Utah Senate?'},
               '5': {'market_id': 'm5', 'title': 'Will the Independent Party win the Utah Senate?'},
               '6': {'market_id': 'm6', 'title': 'Will the Democratic Party win the Iowa Senate?'},
               '7': {'market_id': 'm7', 'title': 'Will the Republican Party win the Iowa Senate?'}}
        configured = [{'name': 'Iowa', 'legs': [{'exchange_id': '6'}, {'exchange_id': '7'}]}]
        out = A.discover(cat, configured)
        self.assertEqual([b['name'] for b in out], ['Ohio Senate'])
        self.assertEqual(A.tier(out[0]), 'standard')

    def test_rotation_covers_everything(self):
        items = list(range(10))
        seen = set()
        for slot in range(3):
            seen |= set(A.rotation(items, 4, slot))
        self.assertEqual(seen, set(items))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.api, self.cfg = FakeAPI(), arb_config()
        self.cfg['baskets'][0]['clip'] = 1000

    def plan(self):
        return basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0])

    def test_policy_validation(self):
        self.assertIsNone(arb_policy(config()))
        with self.assertRaises(Halt):
            arb_policy(dict(config(), arb={'enabled': True, 'p_double_yes': .5}))

    def test_small_edge_legacy_rejects_is_taken(self):
        # sum NO ask .99: legacy needs 1% edge plus .005/leg -> no trade.
        orig = self.api.book
        self.api.book = lambda i: dict(orig(i), bids=[{'price': .505, 'quantity': 10000}])
        legacy = config(); legacy['baskets'][0]['clip'] = 1000
        self.assertIsNone(basket_plan(self.api, self.api.state(), legacy, legacy['baskets'][0]))
        p = self.plan()
        self.assertEqual(p['kind'], 'basket_entry')
        self.assertEqual(p['quantity'], 1000)
        self.assertEqual(p['prices'], [.495, .495])
        self.assertAlmostEqual(p['robust_profit'], 1000 * (.998 - .99) - 20 / 27)

    def test_never_sells_below_floor_when_capital_free(self):
        self.api.quantities = {'11': -100, '22': -100}
        orig = self.api.book
        self.api.book = lambda i: dict(orig(i), asks=[{'price': .505, 'quantity': 10000}])
        self.assertNotEqual((self.plan() or {}).get('kind'), 'basket_exit')
        # With a 4% arb blocked for capital, .99 proceeds beat holding to 1.0.
        self.cfg['_redeploy_roi'] = .04
        self.cfg['arb']['min_profit'] = 0
        p = self.plan()
        self.assertEqual(p['kind'], 'basket_exit')
        self.assertEqual(p['exit_method'], 'opportunity_cost')

    def test_capital_block_is_reported_with_roi(self):
        self.api.cash = 75100
        events = []
        self.assertIsNotNone(basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0], events.append))
        self.api.cash = 75000
        events = []
        self.assertIsNone(basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0], events.append))
        self.assertEqual(events[-1]['event'], 'capital_blocked')
        self.assertGreater(events[-1]['roi'], 0)

    def test_independent_leg_requires_proof(self):
        self.cfg['baskets'][0]['legs'][1]['party'] = 'Independent'
        with self.assertRaises(Halt):  # contract check: winnerName mismatch still guards
            self.plan()

    def test_unproven_list_still_requires_proof(self):
        events = []
        self.cfg['baskets'][0]['entry_proof_required'] = True
        self.assertIsNone(basket_plan(self.api, self.api.state(), self.cfg, self.cfg['baskets'][0], events.append))
        self.assertIn('proof', events[-1])


class UniverseFake(FakeAPI):
    """Configured basket 11/22 is flat and priced fair; auto race 33/44 is cheap."""
    def __init__(self):
        super().__init__()
        self.quantities = {'11': 0, '22': 0, '33': 0, '44': 0}

    def book(self, i):
        b = super().book(i)
        if i in ('11', '22'):
            b['bids'] = [{'price': .49, 'quantity': 10000}]
        b['exchangeId'] = i
        return b

    def get(self, path):
        if '/nodes' in path:
            i = path.split('/')[2]
            r = super().get(path.replace(f'/{i}/', '/1/' if i in ('1', '3') else '/2/'))
            r['root']['contract_details']['raceId'] = 'auto' if i in ('3', '4') else 'r'
            return r
        if '/markets/' in path:
            i = path.split('/')[-1]
            return {'id': i, 'title': 't', 'status': 'open', 'settlementDate': END.isoformat(),
                    'exchanges': [{'id': {'1': '11', '2': '22', '3': '33', '4': '44'}[i]}]}
        return super().get(path)


class UniverseTests(unittest.TestCase):
    def test_cycle_finds_and_selects_auto_race(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        journal = Journal(str(Path(tmp.name) / 'j.sqlite')); self.addCleanup(journal.db.close)
        cfg = arb_config()
        cfg['_universe'] = [{'name': 'Auto Race', 'clip': 1000, 'auto': True, 'legs': [
            {'market_id': '3', 'exchange_id': '33', 'party': 'Democratic Party'},
            {'market_id': '4', 'exchange_id': '44', 'party': 'Republican Party'}]}]
        result = Runner(UniverseFake(), journal, cfg, live=False).cycle()
        self.assertTrue(result[0]['dry_run'])
        self.assertEqual(result[0]['plan']['name'], 'Auto Race')
        self.assertEqual([b['name'] for b in cfg['baskets']], ['test', 'Auto Race'])


if __name__ == '__main__':
    unittest.main()


class HostedUniverseTests(unittest.TestCase):
    def test_prepare_universe_restores_auto_baskets_from_jobs_and_rotates(self):
        from hosted_runner import prepare_universe
        cfg = arb_config(universe_races_per_sweep=10)
        cat = {'33': {'market_id': '3', 'title': 'Will the Democratic Party win the Auto Race?'},
               '44': {'market_id': '4', 'title': 'Will the Republican Party win the Auto Race?'},
               '55': {'market_id': '5', 'title': 'Will the Democratic Party win the New Race?'},
               '66': {'market_id': '6', 'title': 'Will the Republican Party win the New Race?'}}
        legs = [{'market_id': '3', 'exchange_id': '33', 'party': 'Democratic Party'},
                {'market_id': '4', 'exchange_id': '44', 'party': 'Republican Party'}]
        data = {'last_universe_scan': {'catalog': cat},
                'jobs': [{'status': 'outcome_unknown', 'plan': {'kind': 'basket_entry', 'name': 'Auto Race', 'legs': legs}}]}
        prepare_universe(cfg, data)
        self.assertEqual([b['name'] for b in cfg['baskets']], ['test', 'Auto Race'])
        self.assertEqual([b['name'] for b in cfg['_universe']], ['New Race'])


class MultiJobTests(unittest.TestCase):
    def test_repeats_only_while_live_trades_complete(self):
        from hosted_runner import run_cycles
        class R:
            live = True
            def __init__(self, outs): self.outs, self.calls = list(outs), 0
            def cycle(self):
                self.calls += 1
                return self.outs.pop(0)
        cfg = arb_config(max_jobs_per_run=3)
        r = R([[{'completed': 'a'}], [{'completed': 'b'}], [{'completed': 'c'}], [{'completed': 'd'}]])
        self.assertEqual(len(run_cycles(r, cfg, {})), 3)
        r = R([[{'completed': 'a'}], [{'decision': 'NO_TRADE'}], [{'completed': 'c'}]])
        run_cycles(r, cfg, {}); self.assertEqual(r.calls, 2)
        r = R([[{'dry_run': True}], [{'dry_run': True}]]); r.live = False
        run_cycles(r, cfg, {}); self.assertEqual(r.calls, 1)
