"""Regression for the Oct 8 one-cent boundary and strictly GET-only recovery."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch
from trading_bot import Halt, Runner, TID, reconcile_cash


def fixture():
    legs = [{'exchangeId': i, 'side': 'no', 'action': 'buy', 'quantity': 1000,
             'price': p, 'tournamentId': TID} for i, p in [('11', .09), ('22', .875)]]
    positions = [{'exchangeId': i, 'quantity': -1000, 'costBasis': p*1000}
                 for i,p in [('11',.09),('22',.875)]]
    before = {'cash': 80203.91, 'positions': [], 'orders': []}
    after = {'cash': 79238.90, 'positions': positions, 'orders': [], 'at': 'fresh'}
    fills = {str(n): {'orderId': n, 'exchangeId': l['exchangeId'], 'tournamentId': TID,
             'data': [{'id': n, 'quantity': -1000, 'price': l['price']}],
             'coverage': {'complete': True}, 'pagination': {'hasMore': False}}
             for n,l in enumerate(legs,1)}
    job = {'id':'test', 'status':'halted', 'repairs':[], 'plan':{'kind':'basket_entry'},
           'request':{'legs': legs}, 'response':{'results':[{'index':n-1,'data':{'orderId':n}}
                                                        for n in (1,2)]},
           'fills':fills, 'before':before, 'after':after,
           'observed_extra_cost':.010000000009313226}
    cfg = {'costs':{'fee_per_share':0}, 'baskets':[], 'kill_switch':'/nonexistent-stop'}
    api = Mock()
    api.state.return_value = copy.deepcopy(after)
    def get(path):
        oid = int(path.split('/')[2]); leg = legs[oid-1]
        if '/fills' in path: return copy.deepcopy(fills[str(oid)])
        return dict(leg, open=False, remainingQuantity=0)
    api.get.side_effect = get
    api.request.side_effect = AssertionError('Recovery must not POST or DELETE')
    return job, cfg, api


class RoundingRecoveryTests(unittest.TestCase):
    def test_exact_cent_accepted_without_widening_tolerance(self):
        job,cfg,_=fixture()
        self.assertEqual(reconcile_cash(job,cfg),Decimal('.01'))
        for cash in (79238.899,79238.89,79238.88):
            job['after']['cash']=cash
            with self.assertRaises(Halt): reconcile_cash(job,cfg)

    def test_nonfinite_cash_rejected(self):
        job,cfg,_=fixture(); job['after']['cash']=float('nan')
        with self.assertRaises(Halt): reconcile_cash(job,cfg)

    def run_recovery(self, mutate=None, live=True):
        job,cfg,api=fixture(); journal=Mock()
        if mutate: mutate(job,api)
        runner=Runner(api,journal,cfg,live=live)
        # This fixture has no configured baskets; isolate recovery's transport/cash checks.
        with patch('trading_bot.portfolio_risk',return_value=(965,{'test':965},0)):
            result=runner.reconcile_rounding_halt(job)
        api.request.assert_not_called()
        return result, journal

    def test_live_reconciliation_marks_done_without_order_writes(self):
        result,journal=self.run_recovery()
        self.assertEqual(result['orders_submitted'],0)
        self.assertEqual(journal.save.call_args.args[1],'done')

    def test_dry_run_verifies_but_does_not_clear_halt(self):
        result,journal=self.run_recovery(live=False)
        self.assertTrue(result['dry_run']); journal.save.assert_not_called()

    def test_changed_account_open_order_partial_fill_and_other_halts_fail_closed(self):
        changes=[lambda j,a:a.state.return_value.update(cash=79000),
                 lambda j,a:a.state.return_value.update(orders=[{'id':9}]),
                 lambda j,a:j.update(repairs=[{}]),
                 lambda j,a:j.update(status='repairing'),
                 lambda j,a:j.update(observed_extra_cost=.02),
                 lambda j,a:j['fills']['1']['coverage'].update(complete=False),
                 lambda j,a:j['fills']['1']['pagination'].update(hasMore=True),
                 lambda j,a:j['fills']['1']['data'][0].update(quantity=-999)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Halt): self.run_recovery(change)

    def test_mismatched_position_delta_rejected(self):
        def change(j,a): j['before']['positions']=[{'exchangeId':'11','quantity':-1}]
        with self.assertRaises(Halt): self.run_recovery(change)

    def test_changed_fill_read_and_open_order_rejected_without_cancel(self):
        for key,value in [('open',True),('side','yes')]:
            def change(j,a):
                original=a.get.side_effect
                def get(path):
                    result=original(path)
                    if '/fills' not in path:result[key]=value
                    return result
                a.get.side_effect=get
            with self.subTest(key=key),self.assertRaises(Halt): self.run_recovery(change)

if __name__=='__main__': unittest.main()
