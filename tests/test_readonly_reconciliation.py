import unittest
from unittest.mock import Mock, patch
from reconcile_only import ReadOnlyAPI, reconcile_pending
from trading_bot import Halt


class ReadOnlyReconciliationTests(unittest.TestCase):
    def test_post_delete_and_payload_get_cannot_reach_transport(self):
        api = ReadOnlyAPI('test-placeholder')
        with patch('trading_bot.API.request') as transport:
            for method,payload in [('POST',{}),('DELETE',None),('GET',{})]:
                with self.subTest(method=method),self.assertRaises(Halt):
                    api.request(method,'/orders',payload)
            transport.assert_not_called()
            api.get('/orders/1');transport.assert_called_once_with('GET','/orders/1',None)

    def test_recovery_never_enters_trading_cycle_or_submission(self):
        journal=Mock();journal.pending.return_value=[{'id':'original'}]
        with patch('reconcile_only.Runner') as runner:
            runner.return_value.reconcile_rounding_halt.return_value={'orders_submitted':0}
            self.assertEqual(reconcile_pending(Mock(),journal,{}),[{'orders_submitted':0}])
            runner.return_value.cycle.assert_not_called()
            runner.return_value.submit.assert_not_called()
            runner.return_value.finish.assert_not_called()
            runner.return_value.repair.assert_not_called()

if __name__=='__main__':unittest.main()
