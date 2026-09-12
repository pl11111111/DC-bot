import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal as D
from unittest.mock import patch
from utils import test_order_settlement as settlement
from modules.new_trade_stats import render
from datetime import datetime,timezone


class SettlementTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self,payout=None,status='receipt_confirmed',received=D('3.001234')):
        rows=iter([{'value':''},payout,
            dict(status=status,amount=D('1'),fee=D('2'),credits=D('0'),channel_id=8),
            dict(state='received',deposit_id='deposit',amount=received),
            dict(id='deposit',amount=received)])
        self.calls=[]
        class Cursor:
            async def execute(inner,sql,args=()): self.calls.append((sql,args))
            async def fetchone(inner): return next(rows)
        @asynccontextmanager
        async def tx(shared=False):
            self.assertTrue(shared)
            yield Cursor()
        return tx

    async def test_retains_funds_and_evidence_without_payout_or_balance_write(self):
        with patch.object(settlement.db,'transaction',self.fixture()):
            result=await settlement.settle('order',1,D('1'),D('3.001234'),'本人测试')
        self.assertFalse(result['transfer_performed'])
        self.assertEqual(result['retained_in_account'],'3.001234')
        sql='\n'.join(x[0] for x in self.calls)
        self.assertIn("status='test_closed'",sql)
        self.assertNotIn('INSERT INTO payouts',sql)
        self.assertNotIn('UPDATE balances',sql)
        self.assertNotIn('DELETE',sql)

    async def test_existing_payout_any_state_rejects_settlement(self):
        for state in ('submitted','unknown','failed','completed'):
            with patch.object(settlement.db,'transaction',self.fixture(payout={'state':state})):
                with self.assertRaises(ValueError): await settlement.settle('order',1,D('1'),D('3.001234'),'test')
                self.assertFalse(any('UPDATE' in sql and 'orders' in sql for sql,args in self.calls))

    async def test_changed_state_or_deposit_rejects_settlement(self):
        for kwargs in ({'status':'releasing'},{'status':'test_closed'},{'received':D('4')}):
            with patch.object(settlement.db,'transaction',self.fixture(**kwargs)):
                with self.assertRaises(ValueError): await settlement.settle('order',1,D('1'),D('3.001234'),'test')

    def test_test_settlement_is_not_a_sale_or_exception(self):
        text=render([dict(status='test_closed',n=1,volume=D('1'))],datetime.now(timezone.utc))
        self.assertIn('Test orders settled: 1',text)
        self.assertIn('Dispute / exception review: 0',text)
        self.assertIn('Total completed volume: 0.00 USDT',text)
