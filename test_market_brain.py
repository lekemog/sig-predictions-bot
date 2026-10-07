import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from decision_engine import timestamp
from market_brain import (BookCache, BrainStore, checked_limits, digest,
    quote_proposal, relation_candidates, scenarios, scan_parallel, turnover, whole_basket_exit)
from research_runner import ReadOnlyAPI, pages, research
from trading_bot import Halt, TID

AT = timestamp('2026-10-07T14:00:00Z')


def book(exchange='1',market='1',bid=.4,ask=.45,sequence=10):
    return dict(exchangeId=exchange,marketId=market,asOf=dict(sequence=sequence,at=AT.isoformat()),
        _received_at=AT.isoformat(),bids=[dict(price=bid,quantity=1000)],
        asks=[dict(price=ask,quantity=1000)])


def relation(kind='mutually_exclusive',exhaustive=False):
    return dict(id='r1',version=1,status='active',type=kind,isExhaustive=exhaustive,
        nodes=[dict(exchangeId='1',marketId='1'),dict(exchangeId='2',marketId='2')],
        direction=dict(fromExchangeIds=['1'],toExchangeIds=['2']))


def proof(r):
    return dict(relation_hash=digest(r),verified=True,rule_sources=['https://example.org/rule'],
        cancellation_rules='Refund cost, not guaranteed normal payout',reviewed_at=AT.isoformat(),
        settles_at='2026-11-04T17:00:00Z')


def forecast_case():
    return dict(kind='probability_range',yes_probability=[.5,.55],assumptions='Synthetic only',
        validation_reference='Synthetic validation only',evidence=[dict(url='https://example.org/poll',
        verified=True,claim='Synthetic',published_at=AT.isoformat(),independence_group='poll1')])


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'brain.sqlite'
        self.store=BrainStore(self.path)

    def tearDown(self):
        self.store.db.close(); self.directory.cleanup()

    def test_social_post_never_becomes_verified_by_ingestion(self):
        e=dict(url='https://x.com/person/status/123',claim='Endorsement',published_at=AT.isoformat(),
               observed_at=AT.isoformat(),source_kind='social',event_id='endorsement1',verified=True)
        self.store.add_evidence(e); self.store.add_evidence(e)
        self.assertEqual(len(self.store.evidence()),1)
        self.assertFalse(self.store.evidence()[0]['verified'])

    def test_evidence_rejects_credential_bearing_url(self):
        with self.assertRaises(ValueError):
            self.store.add_evidence(dict(url='https://example.org/?token=secret'))

    def test_rate_budget_shared_across_store_instances(self):
        other=BrainStore(self.path)
        try:
            self.store.take_read(100,1)
            with self.assertRaises(ValueError): other.take_read(101,1)
            other.take_read(161,1)
        finally: other.db.close()

    def snapshot(self):
        return dict(at=AT.isoformat(),complete=True,unresolved=False,cash=75250,committed=0,
                    race_exposure={},factor_exposure={},directional=0)

    def plan(self,identity='a',exchange='1'):
        return dict(identity=identity,exchange_ids=[exchange],quantity=100,eligible=True,
            observed_at=AT.isoformat(),cash_required=200,risk_charge=200,
            risk_kind='directional',race='r'+exchange,factor='politics')

    def test_parallel_budget_cannot_spend_same_cash_twice(self):
        results=[]
        def worker(i):
            other=BrainStore(self.path)
            try:
                results.append(other.reserve(self.plan(str(i),str(i)),self.snapshot(),now=AT))
            except ValueError: results.append('rejected')
            finally: other.db.close()
        threads=[threading.Thread(target=worker,args=(i,)) for i in (1,2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(results.count('rejected'),1)

    def test_duplicate_logical_plan_blocks(self):
        self.store.reserve(self.plan(),self.snapshot(),now=AT)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            self.store.reserve(self.plan(),self.snapshot(),now=AT)

    def test_timeout_does_not_free_cash_reservation(self):
        key=self.store.reserve(self.plan(),self.snapshot(),now=AT)
        with self.assertRaises(ValueError):
            self.store.reconcile_reservation(key,dict(complete=True,status='timeout',owner_request_id='x'))
        with self.assertRaises(ValueError):
            self.store.reserve(self.plan('b','2'),self.snapshot(),now=AT)

    def test_protected_hedge_rejects_individual_leg_plan(self):
        s=self.snapshot();s['protected_hedge_exchanges']=['1']
        with self.assertRaises(ValueError): self.store.reserve(self.plan(),s,now=AT)

    def test_pending_unknown_and_old_account_block(self):
        for change in [dict(unresolved=True),dict(complete=False),dict(at='2026-10-07T13:00:00Z')]:
            s=dict(self.snapshot(),**change)
            with self.assertRaises(ValueError): self.store.reserve(self.plan(),s,now=AT)

    def test_factor_exposure_and_no_risk_discount(self):
        s=self.snapshot();s['factor_exposure']={'politics':1900}
        with self.assertRaises(ValueError):self.store.reserve(self.plan(),s,now=AT)
        p=self.plan();p['risk_charge']=0
        with self.assertRaises(ValueError):self.store.reserve(p,self.snapshot(),now=AT)

    def test_no_implicit_higher_risk_profile(self):
        for limit in [dict(reserve=25000),dict(clip=10000),dict(committed=75000)]:
            with self.assertRaises(ValueError):checked_limits(limit)

    def test_rest_receipt_refreshes_unchanged_engine_book(self):
        c=BookCache();b=book();b['asOf']['at']='2026-10-06T00:00:00Z'
        c.resync('1',[b],AT.isoformat())
        self.assertEqual(c.get('1',AT)['asOf']['sequence'],10)

    def test_revision_gap_blocks_until_explicit_rest_resync(self):
        c=BookCache();c.resync('1',[book()],AT.isoformat(),revision=1)
        p=dict(delivery=dict(revision=4,previousRevision=3),books=[book(sequence=12)],trades=[])
        self.assertEqual(c.batch('1',p,AT.isoformat()),'RESYNC_REQUIRED')
        with self.assertRaises(ValueError):c.get('1',AT)
        c.resync('1',[book(sequence=12)],AT.isoformat(),revision=4)
        self.assertEqual(c.get('1',AT)['asOf']['sequence'],12)

    def test_duplicate_batch_can_contain_newer_book(self):
        c=BookCache();c.resync('1',[book()],AT.isoformat(),revision=5)
        p=dict(delivery=dict(revision=5,previousRevision=4),books=[book(sequence=11)],trades=[])
        self.assertEqual(c.batch('1',p,AT.isoformat()),'DUPLICATE')
        self.assertEqual(c.get('1',AT)['asOf']['sequence'],11)

    def test_reconnect_compact_or_unsequenced_batch_requires_resync(self):
        for extra in [dict(resyncRequired=True),dict(trades=[dict(sequence=None)])]:
            c=BookCache();c.resync('1',[book()],AT.isoformat(),revision=1)
            p=dict(delivery=dict(revision=2,previousRevision=1),books=[],trades=[]);p.update(extra)
            self.assertEqual(c.batch('1',p,AT.isoformat()),'RESYNC_REQUIRED')
        c.invalidate()
        with self.assertRaises(ValueError):c.get('1',AT)

    def test_expired_depth_is_unavailable(self):
        c=BookCache();b=book();b['nextExpiryAt']=AT.isoformat();c.resync('1',[b],AT.isoformat())
        with self.assertRaises(ValueError):c.get('1',AT)

    def test_third_party_outcome_not_discarded(self):
        rows=scenarios(relation())
        self.assertEqual(len(rows),3);self.assertIn(dict(zip(['1','2'],[0,0])),rows)
        self.assertEqual(len(scenarios(relation(exhaustive=True))),2)

    def test_implication_direction_matters(self):
        r=relation('implication');rows=scenarios(r)
        self.assertNotIn({'1':1,'2':0},rows)
        self.assertIn({'1':0,'2':1},rows)

    def test_unknown_logical_operator_fails_closed(self):
        with self.assertRaises(ValueError):scenarios(relation('conditional_if'))

    def test_relationship_uses_executable_depth_and_proof_hash(self):
        r=relation();p=proof(r);books={'1':book(bid=.6,ask=.65),'2':book('2','2',bid=.6,ask=.65)}
        out=relation_candidates(r,books,p,AT)
        self.assertTrue(out);self.assertEqual(out[0]['normal_payout_floor'],1000)
        self.assertEqual(out[0]['cancellation_payout'],'UNKNOWN')
        r['version']=2;self.assertEqual(relation_candidates(r,books,p,AT),[])

    def test_no_mark_price_or_stale_book_arbitrage_claim(self):
        r=relation();p=proof(r);books={'1':book(bid=.4),'2':book('2','2',bid=.4)}
        self.assertEqual(relation_candidates(r,books,p,AT),[])
        books['1']['_received_at']='2026-10-07T13:00:00Z'
        self.assertEqual(relation_candidates(r,books,p,AT),[])

    def test_matched_exit_cap_floor_and_depth(self):
        legs=[dict(exchange_id='1',side='no'),dict(exchange_id='2',side='no')]
        books={'1':book(ask=.4),'2':book('2','2',ask=.4)}
        r=whole_basket_exit(legs,books,2200,1,AT)
        self.assertEqual(r['quantity'],1000)
        books['2']['asks'][0]['price']=.9
        self.assertIsNone(whole_basket_exit(legs,books,2200,1,AT))

    def turnover_case(self):
        return dict(book=book(),side='yes',quantity=100,round_trip_cost_per_share=.01,
            contract_verified=True,costs_verified=True,exit_model=dict(validation_reference='Synthetic',
            validated_at=AT.isoformat(),assumptions='Synthetic only',falsification='Thesis invalidated',
            outcomes=[dict(name='reprice',exit_price=.65,exit_depth=100,hours_to_exit=2),
                      dict(name='adverse',exit_price=.35,exit_depth=100,hours_to_exit=4)],
            probability_scenarios=[[.8,.2],[.6,.4]]))

    def test_turnover_uses_lower_sensitivity_and_adverse_exit(self):
        r=turnover(self.turnover_case(),AT)
        self.assertTrue(r['eligible']);self.assertAlmostEqual(r['conservative_profit'],7)
        self.assertAlmostEqual(r['worst_scenario_pnl'],-11)
        self.assertEqual(r['paper_fills'],'UNKNOWN')

    def test_win_probability_cannot_replace_exit_model(self):
        c=self.turnover_case();c['exit_model']['validation_reference']=''
        self.assertFalse(turnover(c,AT)['eligible'])

    def test_optimistic_exit_depth_is_rejected(self):
        c=self.turnover_case();c['exit_model']['outcomes'][1]['exit_depth']=1
        self.assertFalse(turnover(c,AT)['eligible'])

    def quote_model(self):
        return dict(forecast=forecast_case(),flow_validation_reference='Synthetic only',
            adverse_selection_bound=.01,fees_per_share=0,volatility_buffer=.005,
            inventory_penalty=.02,max_inventory=500,valid_until='2026-10-07T14:01:00Z')

    def test_inventory_skew_and_total_two_sided_quote_commitment(self):
        b=book(bid=.4,ask=.65)
        flat=quote_proposal(b,self.quote_model(),0,AT)
        long=quote_proposal(b,self.quote_model(),200,AT)
        self.assertLess(long['quotes'][0]['price'],flat['quotes'][0]['price'])
        self.assertAlmostEqual(flat['cash_required'],sum(q['price']*q['quantity'] for q in flat['quotes']))
        self.assertEqual(flat['paper_fills'],'UNKNOWN')

    def test_unverified_forecast_cannot_quote(self):
        m=self.quote_model();m['forecast']['evidence'][0]['verified']=False
        self.assertFalse(quote_proposal(book(),m,0,AT)['eligible'])

    def test_research_transport_blocks_all_writes_before_network(self):
        api=ReadOnlyAPI('synthetic-secret',self.store)
        for method,path in [('POST','/orders'),('DELETE','/orders/1'),('POST','/realtime/token')]:
            with self.assertRaises(Halt):api.request(method,path)

    def test_pagination_loop_is_not_full_universe(self):
        class Fake:
            def get(self,path):return dict(data=[],pagination=dict(hasMore=True,nextCursor='same'))
        with self.assertRaises(Halt):pages(Fake(),'/markets?limit=100')

    def test_scanner_failure_does_not_claim_complete(self):
        def bad():raise ValueError('Sensitive text must not be logged')
        r=scan_parallel([('bad',bad),('good',lambda:[])])
        self.assertFalse(r['complete']);self.assertNotIn('Sensitive',json.dumps(r))

    def test_cutoff_and_snapshot_scope(self):
        s=dict(tournament_id='other',books={},relations=[])
        with self.assertRaises(ValueError):research(s,self.store,AT)
        self.assertEqual(research(s,self.store,timestamp('2026-11-04T17:00:00Z'))['status'],'CUTOFF')


if __name__=='__main__':
    unittest.main()
