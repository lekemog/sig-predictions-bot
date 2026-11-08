"""Synthetic fixtures test software behavior, never electoral calibration."""
import copy
import unittest
from datetime import datetime, timezone
from directional_model import validate, forecast, probabilities

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def rows():
    return [dict(race_id=str(i), year=y, office='senate', horizon_days=21,
                 poll_margin_pp=20 if i%2 else -20,
                 final_margin_pp=(20 if i%2 else -20)+(i%5-2),
                 reviewed=True, poll_source='https://example.org/poll',
                 result_source='https://example.org/result',
                 election_at=f'{y}-11-03T00:00:00Z',
                 poll_available_at=f'{y}-10-13T00:00:00Z')
            for y in range(2012,2024,2) for i in range(20)]


class ModelTests(unittest.TestCase):
    def test_chronological_folds(self):
        r=validate(rows(),'senate',21,NOW)
        self.assertTrue(r['statistical_gates_passed'])
        self.assertEqual([f['year'] for f in r['folds']],[2018,2020,2022])
        self.assertEqual([f['train_races'] for f in r['folds']],[60,80,100])
        self.assertFalse(r['execution_authorized'])

    def test_insufficient_data(self):
        self.assertFalse(validate(rows()[:20],'senate',21,NOW)['statistical_gates_passed'])

    def test_duplicate(self):
        r=rows(); r.append(r[0])
        with self.assertRaises(ValueError): validate(r,'senate',21,NOW)

    def test_future_outcome(self):
        with self.assertRaises(ValueError):
            validate(rows(),'senate',21,datetime(2010,1,1,tzinfo=timezone.utc))

    def test_provenance(self):
        r=rows(); r[0]['reviewed']=False
        with self.assertRaises(ValueError): validate(r,'senate',21,NOW)

    def test_horizon_leak(self):
        r=rows(); r[0]['poll_available_at']='2012-11-04T00:00:00Z'
        with self.assertRaises(ValueError): validate(r,'senate',21,NOW)

    def test_bool_nan(self):
        for value in (True,float('nan'),float('inf')):
            r=rows(); r[0]['poll_margin_pp']=value
            with self.assertRaises(ValueError): validate(r,'senate',21,NOW)

    def test_year_equal_weight(self):
        r=[dict(year=2018,poll_margin_pp=0,final_margin_pp=1)]*10
        r += [dict(year=2020,poll_margin_pp=0,final_margin_pp=-1)]
        p,bounds=probabilities(r,0)
        self.assertAlmostEqual(p,(10.5/11+.25)/2)
        self.assertEqual(bounds,[.25,10.5/11])

    def test_forecast_binding_and_freshness(self):
        data=rows(); report=validate(data,'senate',21,NOW)
        obs=dict(office='senate',horizon_days=21,observed_at=NOW.isoformat(),
                 aggregate_reviewed=True,evidence=[dict(url='https://example.org/poll',
                 claim='Synthetic fixture only',verified=True,independence_group='fixture',
                 source_kind='original_poll',published_at=NOW.isoformat())],
                 poll_margin_pp=20,margin_sensitivity_pp=1)
        f=forecast(data,obs,report,NOW)
        self.assertEqual(f['yes_probability'],[20.5/21,20.5/21])
        self.assertFalse(f['execution_authorized'])
        changed=copy.deepcopy(report); changed['brier']=0.123
        with self.assertRaises(ValueError): forecast(data,obs,changed,NOW)
        obs['observed_at']='2026-10-10T00:00:00Z'
        with self.assertRaises(ValueError): forecast(data,obs,report,NOW)

    def test_bad_forecast_skill(self):
        r=rows()
        for x in r: x['final_margin_pp'] *= -1
        # A systematic bias can be learned; random near-tie labels cannot.
        for i,x in enumerate(r): x['final_margin_pp']=1 if i%3 else -1; x['poll_margin_pp']=0
        self.assertFalse(validate(r,'senate',21,NOW)['statistical_gates_passed'])


if __name__=='__main__': unittest.main()
