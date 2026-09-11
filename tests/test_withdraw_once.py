import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
import withdraw_test_once as t


class SingleWithdrawalTests(unittest.TestCase):
    def test_wrong_confirmation_never_submits(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(t,'STATE',Path(folder)/'state.json'),patch.object(t,'check_fee'),patch('builtins.input',return_value='no'),patch.object(t,'api') as api:
            t.submit()
            api.assert_not_called()
            self.assertFalse(t.STATE.exists())

    def test_unknown_result_leaves_intent_and_prevents_second_post(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(t,'STATE',Path(folder)/'state.json'),patch.object(t,'ROOT',Path(folder)),patch.object(t,'check_fee'),patch.object(t,'balance',return_value={'free':'10','locked':'0','total':'10'}),patch('builtins.input',return_value='SEND 3.01 USDT TO '+t.ADDRESS),patch.object(t,'api',side_effect=ValueError('timeout')) as api:
            with self.assertRaises(ValueError): t.submit()
            self.assertEqual(json.loads(t.STATE.read_text())['amount'],'3.01')
            with self.assertRaises(ValueError): t.submit()
            api.assert_called_once()

    def test_intent_creation_is_exclusive(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(t,'STATE',Path(folder)/'state.json'),patch.object(t,'ROOT',Path(folder)):
            t.create_intent({'request_id':'first'})
            with self.assertRaises(FileExistsError): t.create_intent({'request_id':'second'})
            self.assertEqual(json.loads(t.STATE.read_text())['request_id'],'first')

    def test_fee_change_stops_test(self):
        response=[{'coin':'USDT','networkList':[{'network':'BSC','withdrawEnable':True,'withdrawFee':'0.02','withdrawMin':'3','withdrawIntegerMultiple':'0.00000001'}]}]
        with patch.object(t,'api',return_value=response):
            with self.assertRaises(ValueError): t.check_fee()
