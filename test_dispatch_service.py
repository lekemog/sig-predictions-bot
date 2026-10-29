import copy
import json
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path

from dispatch_service import DispatchJournal, GitHub, tick
from market_brain import CUTOFF
from test_market_brain import AT
from trading_bot import Halt, risk_limits, basket_plan
from tests.test_trading_bot import FakeAPI, config

PROFILE=dict(committed=75000,reserve=25000,basket=15000,clip=5000,
             race=2500,directional=15000,factor=7500)


class Fake:
    def __init__(self):self.calls=[];self.busy=False;self.marker={};self.error=None;self.observed=False
    def status(self):return dict(last_successful_live_sweep=self.marker),self.busy
    def find_dispatch(self,identity):self.calls.append(('GET',identity));return self.observed
    def dispatch(self,mode,identity):
        self.calls.append(('POST',mode,identity))
        if self.error:raise self.error


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.journal=DispatchJournal(Path(self.directory.name)/'dispatch.sqlite')
        self.api=Fake()
    def tearDown(self):self.journal.db.close();self.directory.cleanup()
    def test_default_mode_cannot_invent_live_activation(self):
        r=tick(self.api,self.journal,'dry-run',AT)
        self.assertEqual(r['mode'],'dry-run');self.assertEqual(self.api.calls[0][1],'dry-run')
    def test_busy_executor_blocks_trigger(self):
        self.api.busy=True
        self.assertEqual(tick(self.api,self.journal,'live',AT)['status'],'EXECUTOR_BUSY')
        self.assertEqual(self.api.calls,[])
    def test_fresh_live_sweep_blocks_trigger(self):
        self.api.marker=dict(at=AT.isoformat(),mode='live',status='success')
        self.assertEqual(tick(self.api,self.journal,'live',AT)['status'],'SWEEP_FRESH')
        self.assertEqual(self.api.calls,[])
    def test_successful_dry_run_cannot_count_as_live(self):
        self.api.marker=dict(at=AT.isoformat(),mode='dry-run',status='success')
        self.assertEqual(tick(self.api,self.journal,'live',AT)['status'],'DISPATCH_ACCEPTED')
    def test_dispatch_cooldown_survives_restart(self):
        tick(self.api,self.journal,'live',AT)
        second=DispatchJournal(Path(self.directory.name)/'dispatch.sqlite')
        try:self.assertEqual(tick(self.api,second,'live',AT+timedelta(seconds=60))['status'],'COOLDOWN')
        finally:second.db.close()
        self.assertEqual(len(self.api.calls),1)
    def test_unknown_timeout_never_retries_post(self):
        self.api.error=TimeoutError('credential must not appear')
        tick(self.api,self.journal,'live',AT)
        self.assertEqual(tick(self.api,self.journal,'live',AT+timedelta(hours=1))['status'],
                         'UNKNOWN_DISPATCH_REQUIRES_REVIEW')
        self.assertEqual(sum(c[0]=='POST' for c in self.api.calls),1)
    def test_unknown_can_be_reconciled_by_correlated_run_only(self):
        self.api.error=TimeoutError();tick(self.api,self.journal,'live',AT)
        self.api.observed=True
        r=tick(self.api,self.journal,'live',AT+timedelta(seconds=60))
        self.assertEqual(r['status'],'DISPATCH_OBSERVED_AFTER_UNKNOWN')
        self.assertEqual(sum(c[0]=='POST' for c in self.api.calls),1)
        self.assertEqual(self.journal.latest()[2],'accepted')
    def test_cutoff_prevents_dispatch(self):
        self.assertEqual(tick(self.api,self.journal,'live',CUTOFF)['status'],'CUTOFF')
        self.assertEqual(self.api.calls,[])
    def test_invalid_mode_or_short_interval_rejected(self):
        for mode,interval in (('bad',1200),('live',1)):
            with self.assertRaises(Halt):tick(self.api,self.journal,mode,AT,interval)
        self.assertEqual(self.api.calls,[])
    def test_transport_rejects_arbitrary_write_before_network(self):
        api=GitHub('synthetic')
        with self.assertRaises(Halt):api.request('POST','/orders',{})
        with self.assertRaises(Halt):api.request('GET','https://example.org')


class ProfileTests(unittest.TestCase):
    def test_absent_profile_keeps_legacy_limits(self):
        self.assertEqual(risk_limits({})['reserve'],75000)
        self.assertEqual(risk_limits({})['clip'],1000)
    def test_staged_profile_is_exact_and_consistent(self):
        self.assertEqual(risk_limits({'risk_limits':PROFILE}),PROFILE)
    def test_invalid_profile_cannot_relax_controls(self):
        for change in ({'clip':5001},{'reserve':0},{'reserve':True},{'committed':100001},
                       {'directional':999},{'factor':20000},{'basket':999999},{'extra':1}):
            with self.assertRaises(Halt):risk_limits({'risk_limits':dict(PROFILE,**change)})
    def test_profile_file_matches_reviewed_limits(self):
        staged=json.loads(Path('bot_config.aggressive.json').read_text())
        self.assertEqual(staged['risk_limits'],PROFILE)
        self.assertTrue(all(b['clip']==5000 for b in staged['baskets']))

    def test_large_clip_is_reduced_for_executable_depth(self):
        api=FakeAPI();cfg=config();cfg['risk_limits']=PROFILE
        cfg['baskets'][0]['clip']=5000
        original=api.book
        def shallow(i):
            book=original(i)
            for side in ('bids','asks'):book[side][0]['quantity']=1500
            return book
        api.book=shallow
        plan=basket_plan(api,api.state(),cfg,cfg['baskets'][0])
        self.assertEqual(plan['quantity'],1250)
        self.assertEqual(api.calls,[])
    def test_increased_clip_still_preserves_reserve(self):
        api=FakeAPI();cfg=config();cfg['risk_limits']=PROFILE
        cfg['baskets'][0]['clip']=5000;api.cash=25000
        self.assertIsNone(basket_plan(api,api.state(),cfg,cfg['baskets'][0]))
        self.assertEqual(api.calls,[])


if __name__=='__main__':unittest.main()
