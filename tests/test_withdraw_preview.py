import unittest
from preview_withdraw_test import preview


class PreviewTests(unittest.TestCase):
    def network(self,**kwargs):
        return dict(network='BSC',withdrawEnable=True,withdrawFee='1',withdrawMin='10',
                    withdrawIntegerMultiple='0.01',**kwargs)

    def test_both_fee_interpretations_are_shown(self):
        result=preview(self.network())
        self.assertEqual(result['candidate_request_amount'],'11')
        self.assertEqual(result['if_fee_included'],{'account_debit':'11','recipient_amount':'10'})
        self.assertEqual(result['if_fee_extra'],{'account_debit':'12','recipient_amount':'11'})

    def test_zero_fee_cannot_distinguish_semantics(self):
        data=self.network(); data['withdrawFee']='0'
        self.assertFalse(preview(data)['fee_semantics_can_be_distinguished'])

    def test_invalid_or_disabled_network_refused(self):
        for changes in ({'withdrawFee':'NaN'},{'withdrawFee':'-1'},{'withdrawEnable':False},
                        {'withdrawIntegerMultiple':'0'},{'withdrawMax':'5'}):
            with self.assertRaises(ValueError): preview({**self.network(),**changes})
