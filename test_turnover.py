import copy
import unittest
import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from trading_bot import Halt, Journal, Runner, basket_plan, now
from turnover import dip_entry_reason, profit_exit_target
from tests.test_trading_bot import FakeAPI, config


class TurnoverTests(unittest.TestCase):
    def setUp(self):
        self.api, self.cfg = FakeAPI(), config()
        self.api.quantities = {'11':-100,'22':-100}
        self.state = self.api.state()
        # Basket cost 96; net current sale 97; conditional floor 100.
        for p in self.state['positions']:
            p['costBasis'] = 48
        self.policy = dict(mode='observe',profit_fraction=.005,min_profit=1)
        self.cfg['turnover'] = self.policy
        self.cfg['entries_paused'] = True
        self.basket=self.cfg['baskets'][0]

    def test_whole_and_partial_basis_allocation(self):
        r=profit_exit_target(self.state,self.basket,100,self.policy)
        self.assertEqual(r['allocated_cost_basis'],96)
        self.assertEqual(r['net_proceeds_target'],97)
        r=profit_exit_target(self.state,self.basket,50,self.policy)
        self.assertEqual(r['net_proceeds_target'],49)

    def test_no_exit_with_missing_invalid_or_unmatched_basis(self):
        for field,value in [('costBasis',None),('costBasis',float('nan')),('costBasis',-1),('quantity',-90),('quantity',100)]:
            state=copy.deepcopy(self.state);state['positions'][0][field]=value
            self.assertIsNone(profit_exit_target(state,self.basket,100,self.policy))

    def test_minimum_profit_and_mode_required(self):
        for k,v in [('profit_fraction',0),('profit_fraction',float('nan')),('min_profit',.1),('mode','invalid')]:
            self.assertIsNone(profit_exit_target(self.state,self.basket,100,dict(self.policy,**{k:v})))

    def book(self,i):
        return {'exchangeId':i,'_received_at':now().isoformat(),
                'bids':[{'price':.50,'quantity':10000}],
                'asks':[{'price':.51,'quantity':10000}]}

    def test_observe_reports_profitable_exit_without_returning_order(self):
        obs=[]
        with patch.object(self.api,'book',side_effect=self.book):
            plan=basket_plan(self.api,self.state,self.cfg,self.basket,obs.append)
        self.assertIsNone(plan)
        candidates=[x for x in obs if isinstance(x,dict) and x.get('event')=='turnover_exit_candidate']
        self.assertEqual(len(candidates),1)
        self.assertFalse(candidates[0]['execution_enabled'])
        self.assertEqual(self.api.calls,[])

    def test_live_takes_profit_below_settlement_floor_while_entries_paused(self):
        self.policy['mode']='live'
        with patch.object(self.api,'book',side_effect=self.book):
            plan=basket_plan(self.api,self.state,self.cfg,self.basket)
        self.assertEqual(plan['kind'],'basket_exit')
        self.assertEqual(plan['quantity'],100)
        self.assertAlmostEqual(plan['net_limit_proceeds'],97)
        self.assertLess(plan['net_limit_proceeds'],plan['normal_settlement_floor'])
        self.assertGreaterEqual(plan['net_limit_proceeds'],plan['value_floor'])
        self.assertEqual(self.api.calls,[])

    def test_live_does_not_sell_below_cost_plus_target(self):
        self.policy['mode']='live'
        for p in self.state['positions']: p['costBasis']=49
        with patch.object(self.api,'book',side_effect=self.book):
            self.assertIsNone(basket_plan(self.api,self.state,self.cfg,self.basket))

    def test_changed_price_rejected_before_submission_intent(self):
        self.policy['mode']='live'
        with patch.object(self.api,'book',side_effect=self.book):
            plan=basket_plan(self.api,self.state,self.cfg,self.basket)
        with tempfile.TemporaryDirectory() as d:
            journal=Journal(str(Path(d)/'journal.sqlite'))
            try:
                runner=Runner(self.api,journal,self.cfg,live=True)
                with self.assertRaisesRegex(Halt,'fresh price'):
                    # FakeAPI's usual book gives proceeds below the profit target.
                    runner.submit(plan,self.state)
                self.assertEqual(journal.pending(),[])
                self.assertEqual(self.api.calls,[])
            finally:
                journal.db.close()

    def test_dip_gate_rejects_missing_stale_recovered_and_changed_contract(self):
        cfg={'turnover':{'mode':'live'},'dip_monitor':{'min_drop_per_matched_share':.02}}
        self.assertIsNotNone(dip_entry_reason(cfg,self.basket,'h',100,94,now()))
        r=dict(basket=self.basket['name'],at=now().isoformat(),contract_hash='h',status='dip',
               entry_blockers=[],price_and_entry_checks_pass=True,reference_buy_cost=240,quantity=250)
        cfg['_dip_scan']={'baskets':[r]}
        self.assertIsNone(dip_entry_reason(cfg,self.basket,'h',100,94,now()))
        self.assertIsNotNone(dip_entry_reason(cfg,self.basket,'changed',100,94,now()))
        self.assertIsNotNone(dip_entry_reason(cfg,self.basket,'h',100,95,now()))
        r['at']=(now()-timedelta(minutes=2)).isoformat()
        self.assertIsNotNone(dip_entry_reason(cfg,self.basket,'h',100,94,now()))


if __name__ == '__main__':
    unittest.main()
