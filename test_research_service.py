import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from exit_validation import audit_exit_trace
from market_brain import BrainStore, CUTOFF
from research_runner import collect
from research_service import observe_books, run, watchlist
from test_market_brain import AT, book
from trading_bot import Halt, TID


class FakeAPI:
    def __init__(self):
        self.calls=[]
    def get(self,path):
        self.calls.append(path)
        data=[] if path.startswith('/relationships') else [dict(id='1',title='Question',
            contexts=[dict(tournament=dict(id=TID),status='open')],
            settlementDate='2026-11-04T17:00:00Z',exchanges=[dict(id='1')])]
        return dict(data=data,pagination=dict(hasMore=False,nextCursor=None))
    def book(self,exchange):
        return book(exchange)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=BrainStore(Path(self.directory.name)/'brain.sqlite')
        self.api=FakeAPI()
    def tearDown(self):
        self.store.db.close();self.directory.cleanup()
    def test_zero_relationships_still_preserves_full_catalogue(self):
        snapshot=collect(self.api,{},0)
        self.assertEqual(snapshot['relationship_catalog'],[])
        self.assertEqual(snapshot['markets'][0]['title'],'Question')
        self.assertEqual(snapshot['exchange_catalog']['1']['market_id'],'1')
    def test_discovery_rejects_over_candidate_budget(self):
        for maximum in (4,-1,True,1.5):
            with self.assertRaises(ValueError):collect(self.api,{},maximum)
        self.assertEqual(self.api.calls,[])
    def test_one_poll_retains_actual_book_without_fills(self):
        result=run(self.api,self.store,['1'],threading.Event(),clock=lambda:AT,once=True)
        self.assertEqual(result['observed'],['1'])
        rows=[json.loads(r[0]) for r in self.store.db.execute('SELECT body FROM observations')]
        observed=next(r for r in rows if r['kind']=='book_observation')
        self.assertEqual(observed['paper_fills'],'UNKNOWN')
        self.assertEqual(result['orders_submitted'],0)
    def test_book_identity_mismatch_records_no_observation(self):
        self.api.book=lambda _:book('1','999')
        result=observe_books(self.api,self.store,['1'],{'1':dict(market_id='1')},lambda:AT)
        self.assertEqual(result['unavailable'],['1'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)
    def test_error_text_not_persisted(self):
        self.api.book=lambda _:(_ for _ in ()).throw(Halt('secret transport message'))
        result=run(self.api,self.store,['1'],threading.Event(),clock=lambda:AT,once=True)
        self.assertEqual(result['unavailable'],['1'])
        serialized=' '.join(r[0] for r in self.store.db.execute('SELECT body FROM observations'))
        self.assertNotIn('secret transport message',serialized)
    def test_cutoff_stops_before_discovery(self):
        result=run(self.api,self.store,['1'],threading.Event(),clock=lambda:CUTOFF,once=True)
        self.assertEqual(result['status'],'STOPPED');self.assertEqual(self.api.calls,[])
    def test_watchlist_rejects_duplicate_or_unbounded(self):
        path=Path(self.directory.name)/'watch.json'
        for ids in (['1','1'],['1','2','3','4'],['https://example.org'],[1]):
            path.write_text(json.dumps(ids))
            with self.assertRaises(ValueError):watchlist(path)


class ExitTraceTests(unittest.TestCase):
    def rows(self):
        return [dict(kind='book_observation',exchange_id='1',book=book(),
                     observed_at=AT.isoformat(),paper_fills='UNKNOWN')]
    def audit(self,rows):
        return audit_exit_trace(rows,'1','yes',100,35,39)
    def test_observed_depth_is_never_a_realized_fill_or_validation(self):
        r=self.audit(self.rows())
        self.assertEqual(len(r['observed_exit_opportunities']),1)
        self.assertFalse(r['validated_for_live']);self.assertEqual(r['realized_profit'],'UNKNOWN')
    def test_duplicate_observations_cannot_inflate_validation(self):
        rows=self.rows();rows.append(copy.deepcopy(rows[0]))
        with self.assertRaises(ValueError):self.audit(rows)
    def test_stale_future_or_fabricated_fill_rejected(self):
        for change in ('stale','future','fill'):
            rows=self.rows()
            if change=='fill':rows[0]['paper_fills']='FILLED'
            else:rows[0]['book']['_received_at']='2026-10-07T'+('13:00:00Z' if change=='stale' else '15:00:00Z')
            with self.assertRaises(ValueError):self.audit(rows)
    def test_insufficient_exit_depth_is_not_opportunity(self):
        rows=self.rows();rows[0]['book']['bids'][0]['quantity']=1
        self.assertEqual(self.audit(rows)['observed_exit_opportunities'],[])


if __name__=='__main__':unittest.main()
