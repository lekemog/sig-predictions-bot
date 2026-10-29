import json
import unittest
from pathlib import Path
from trading_bot import risk_limits, Halt

class ExpandedProfile(unittest.TestCase):
    def test_authorized_profile(self):
        c=json.loads(Path('bot_config.json').read_text())
        r=risk_limits(c)
        self.assertEqual(r['committed'],100000)
        # Owner-approved full-account profile, 18 Oct 2026.
        self.assertEqual(r['reserve'],2000)
        self.assertEqual((r['race'],r['directional'],r['factor']),(5000,60000,60000))
        self.assertEqual((r['basket'],r['clip']),(5000,5000))
        c['risk_limits']['committed']=100001
        with self.assertRaises(Halt): risk_limits(c)

    def test_hourly_cron_and_shared_lock(self):
        text=Path('.github/workflows/sig-management.yml').read_text()
        self.assertIn("17 * * * *",text)
        self.assertIn('group: sig-portfolio-execution',text)
        self.assertIn('cancel-in-progress: false',text)
