import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from trading_bot import (Halt, Journal, Runner, basket_plan, defensive_entry_reason,
                         defensive_price_reason, directional_plan)
from tests.test_trading_bot import FakeAPI, config
POLICY = dict(enabled=True, account_value_floor=99750, max_entry_clip=250,
              max_new_basket_cost=1000, min_settlement_roi=.03,
              max_round_trip_loss_fraction=.01)
class DefensiveTests(unittest.TestCase):
    def setUp(self):
        self.api, self.cfg = FakeAPI(), config()
        self.cfg['defensive_entries'] = copy.deepcopy(POLICY)
        self.basket = self.cfg['baskets'][0]
        self.state = self.api.state()
        self.state['pnl'] = {'totalAccountValue':100000}
    def test_all_baskets_need_proof(self):
        self.assertIn('settlement proof',defensive_entry_reason(self.state,self.cfg,self.basket,[]))
    def test_no_adding_to_holdings(self):
        self.api.quantities = {'11':-100,'22':-100}
        self.state['positions'] = self.api.state()['positions']
        self.assertIn('held basket',defensive_entry_reason(self.state,self.cfg,self.basket,[]))
    def test_drawdown_and_unknown_value(self):
        for v in (99749,None,float('nan'),True):
            self.state['pnl']['totalAccountValue'] = v
            self.assertIn('Account value',defensive_entry_reason(self.state,self.cfg,self.basket,[]))
    def test_malformed_and_loose_policy(self):
        for k,v in [('max_entry_clip',251),('min_settlement_roi',.01),('max_round_trip_loss_fraction',.02),('max_new_basket_cost',float('nan'))]:
            cfg = copy.deepcopy(self.cfg); cfg['defensive_entries'][k] = v
            self.assertIsNotNone(defensive_entry_reason(self.state,cfg,self.basket,[]))
    def test_margin_spread_depth_and_size(self):
        for q,c,f,l in [(100,98,100,98),(100,96,100,94),(100,96,100,-float('inf')),(251,200,300,200),(250,1001,1200,1001)]:
            self.assertIsNotNone(defensive_price_reason(self.cfg,q,c,f,l))
        self.assertIsNone(defensive_price_reason(self.cfg,100,96,100,95.5))
    def test_exit_survives_pause_drawdown_missing_proof(self):
        self.cfg['entries_paused'] = True
        self.api.quantities = {'11':-100,'22':-100}
        state = self.api.state(); state['pnl'] = {'totalAccountValue':99000}
        observations = []
        with patch.object(self.api,'book',side_effect=lambda i: {'exchangeId':i,'_received_at':state['at'],'bids':[{'price':.48,'quantity':10000}],'asks':[{'price':.49,'quantity':10000}]}):
            plan = basket_plan(self.api,state,self.cfg,self.basket,observations.append)
        self.assertEqual(plan['kind'],'basket_exit')
        self.assertEqual(observations[0]['event'],'held_basket_diagnostic')
        self.assertGreaterEqual(plan['net_limit_proceeds'],plan['value_floor'])
    def test_directional_and_submission_bypass(self):
        self.assertIsNone(directional_plan(self.api,{},self.cfg,{}))
        with tempfile.TemporaryDirectory() as d:
            journal = Journal(str(Path(d)/'test.sqlite'))
            try:
                runner = Runner(self.api,journal,self.cfg,live=True)
                with self.assertRaisesRegex(Halt,'directional'):
                    runner.submit({'kind':'directional_entry'},self.state)
                with self.assertRaisesRegex(Halt,'settlement proof'):
                    runner.submit({'kind':'basket_entry','name':self.basket['name'],'legs':self.basket['legs'],'contracts':[]},self.state)
                self.assertEqual(journal.pending(),[])
                self.assertEqual(self.api.calls,[])
            finally:
                journal.db.close()
if __name__ == '__main__':
    unittest.main()
