import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

from dip_monitor import DEFAULT_POLICY, observe_price, policy_valid, scan
from trading_bot import Halt, now
from tests.test_trading_bot import FakeAPI, config


class DipTests(unittest.TestCase):
    def setUp(self):
        self.at = now()
        self.policy = copy.deepcopy(DEFAULT_POLICY)

    def quote(self, minutes=0, cost=240, fingerprint='same', quantity=250):
        return dict(at=(self.at + timedelta(minutes=minutes)).isoformat(),
                    basket='test', quantity=quantity, contract_hash=fingerprint,
                    buy_cost=cost, sell_proceeds=239, floor=250)

    def history(self):
        return [self.quote(-30), self.quote(-20), self.quote(-10)]

    def test_executable_dip_and_no_current_quote_leakage(self):
        hist, r = observe_price(self.history(), self.quote(cost=235), self.policy, self.at)
        self.assertEqual(r['status'], 'dip')
        self.assertEqual(r['reference_buy_cost'], 240)
        self.assertEqual(r['drop_per_matched_share'], .02)
        self.assertEqual(len(hist), 4)

    def test_small_change_not_a_signal(self):
        _, r = observe_price(self.history(), self.quote(cost=239), self.policy, self.at)
        self.assertEqual(r['status'], 'no_dip')

    def test_cold_start_and_duplicate_observations(self):
        for hist in ([], [self.quote(-10)] * 10):
            _, r = observe_price(hist, self.quote(cost=230), self.policy, self.at)
            self.assertEqual(r['status'], 'warming_up')

    def test_metadata_or_quantity_change_restarts_warmup(self):
        for q in (self.quote(cost=230, fingerprint='changed'),):
            _, r = observe_price(self.history(), q, self.policy, self.at)
            self.assertEqual(r['status'], 'warming_up')
        _, r = observe_price([dict(h,quantity=100) for h in self.history()], self.quote(cost=230), self.policy, self.at)
        self.assertEqual(r['status'], 'warming_up')

    def test_gap_restarts_warmup(self):
        history = [self.quote(-150), self.quote(-140), self.quote(-130)]
        _, r = observe_price(history, self.quote(cost=230), self.policy, self.at)
        self.assertEqual(r['status'], 'warming_up')

    def test_bad_quote_rejected(self):
        for field, value in [('buy_cost',float('nan')), ('sell_proceeds',-1),
                             ('quantity',100), ('at',self.quote(-2)['at']),
                             ('at',self.quote(1)['at'])]:
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                observe_price([],dict(self.quote(),**{field:value}),self.policy,self.at)

    def test_corrupt_history_ignored(self):
        _, r = observe_price([{},dict(self.quote(-10),buy_cost=float('inf'))],self.quote(),self.policy,self.at)
        self.assertEqual(r['prior_samples'],0)

    def test_policy_cannot_enable_execution(self):
        for key, val in [('mode','live'),('quantity',1000),('min_drop_per_matched_share',.001),('min_samples',True)]:
            p=dict(self.policy,**{key:val})
            self.assertFalse(policy_valid(p))

    def test_sampler_never_posts_even_if_dip_and_gates_pass(self):
        api,cfg=FakeAPI(),config()
        cfg['dip_monitor']=self.policy
        cfg['entries_paused']=True
        state=api.state()
        old=[]
        # Seed actual metadata identity from one read-only scan.
        first, _=scan(api,state,cfg,{})
        fingerprint=first['test'][0]['contract_hash']
        for m in (-30,-20,-10):
            old.append(self.quote(m,cost=250,fingerprint=fingerprint))
        hist,r=scan(api,state,cfg,{'test':old})
        row=r['baskets'][0]
        self.assertEqual(row['status'],'dip')
        self.assertIn('New entries paused',row['entry_blockers'])
        self.assertIn('Defensive policy missing',row['entry_blockers'])
        self.assertFalse(row['execution_enabled'])
        self.assertEqual(r['orders_submitted'],0)
        self.assertEqual(api.calls,[])

    def test_missing_depth_explicit_partial_coverage(self):
        api,cfg=FakeAPI(),config(); cfg['dip_monitor']=self.policy
        with patch.object(api,'book',side_effect=Halt('Insufficient executable depth')):
            hist,r=scan(api,api.state(),cfg,{'test':self.history()})
        self.assertEqual(hist['test'],[])
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['baskets'][0]['status'],'unavailable')
        self.assertEqual(api.calls,[])

    def test_disabled_and_invalid_modes_remain_read_only(self):
        api,cfg=FakeAPI(),config()
        for enabled,mode,status in [(False,'observe','disabled'),(True,'live','invalid_policy')]:
            cfg['dip_monitor']=dict(self.policy,enabled=enabled,mode=mode)
            with patch.object(api,'book',side_effect=AssertionError('must not read')):
                _,r=scan(api,api.state(),cfg,{})
            self.assertEqual(r['status'],status)
            self.assertEqual(api.calls,[])


if __name__ == '__main__':
    unittest.main()
