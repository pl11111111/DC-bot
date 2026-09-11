import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from cancel_stale_legacy_order import validate


class StaleOrderTests(unittest.TestCase):
    def test_pending_creation_without_payment_is_eligible(self):
        validate({'transaction_type':'trade','status':'pending','escrow_fee':0},[{'action':'create'}],[])

    def test_financial_or_changed_records_rejected(self):
        base={'transaction_type':'trade','status':'pending','escrow_fee':0}
        for change in ({'transaction_type':'rental'},{'status':'paid'},{'escrow_fee':1},
                       {'unique_amount':0},{'txid':'transfer'},{'paid_at':'date'},
                       {'completed_at':'date'},{'payment_address':'address'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate({**base,**change},[{'action':'create'}],[])

    def test_extra_history_and_rentals_rejected(self):
        base={'transaction_type':'trade','status':'pending','escrow_fee':0}
        for actions,rentals in (([],[]),([{'action':'create'},{'action':'paid'}],[]),([{'action':'create'}],[{'id':1}])):
            with self.assertRaises(ValueError): validate(base,actions,rentals)
