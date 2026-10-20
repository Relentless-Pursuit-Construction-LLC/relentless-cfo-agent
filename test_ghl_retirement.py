import importlib
import os
import unittest
from unittest.mock import patch

from keystone import ghl
from keystone.jobs import revenue_tracker as tracker


class RetirementTests(unittest.TestCase):
    def test_stale_credentials_cannot_enable_ghl(self):
        with patch.dict(os.environ, {'GHL_TOKEN': 'synthetic', 'GHL_LOCATION_ID': 'synthetic'}):
            importlib.reload(ghl)
            self.assertFalse(ghl.is_configured())
            with self.assertRaisesRegex(RuntimeError, 'retired'):
                ghl.search_all_opportunities()

    def test_report_retains_qbo_values_without_inventing_zero_sales_or_trends(self):
        with patch.object(ghl, 'search_all_opportunities', side_effect=AssertionError('GHL called')), \
             patch.object(tracker.qbo, 'get_profit_loss', return_value={}), \
             patch.object(tracker, '_extract_total_income', return_value=12345), \
             patch.object(tracker.qbo, 'get_payments_for_range', return_value={'QueryResponse': {'Payment': [{'TotalAmt': 75}]}}), \
             patch.object(tracker.qbo, 'get_deposits_for_range', return_value={'QueryResponse': {'Deposit': [{'TotalAmt': 25}]}}):
            result = tracker.run_revenue_tracker(2)
        rows = result['stats']['months']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['sales_booked'] is None for r in rows))
        self.assertTrue(all(r['sales_booked_count'] is None for r in rows))
        self.assertTrue(all(r['recognized'] == 12345 for r in rows))
        self.assertTrue(all(r['cash_collected'] == 100 for r in rows))
        self.assertIn('Unavailable', result['message_text'])
        self.assertIn('not zero', result['message_text'])
        self.assertNotIn('sales booked is 0%', result['message_text'])
        self.assertNotIn('Sales booked up', result['message_text'])
        self.assertEqual(result['stats']['sales_booked_status'], 'unavailable_ghl_retired')


if __name__ == '__main__':
    unittest.main()
