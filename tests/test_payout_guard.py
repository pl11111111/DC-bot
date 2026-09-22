import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import json
import unittest
from decimal import Decimal as D
from datetime import datetime,timezone
from unittest.mock import AsyncMock,patch
from contextlib import asynccontextmanager
from utils import payout_guard as g, shared_payments as p
from modules.new_trade_stats import render


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.order={'status':'releasing','amount':D(100),'fee':D(2),'credits':D(0),'buyer_id':1,'seller_id':2}
        self.invoice={'state':'received','deposit_id':'credit','amount':D('102.001234')}
        self.deposit={'id':'credit','amount':D('102.001234')}
        self.intent={'actor_id':2,'details':json.dumps({'address':'addr','gross':'100','fee':'1','net':'99'})}

    def call(self,gross='100',net='99'):
        return g.budget(self.order,self.invoice,self.deposit,self.intent,'addr',D(gross),D(1),D(net))

    def test_seller_cannot_receive_service_fee_or_tail(self):
        self.assertEqual(self.call()['cap'],D(100))
        for gross in ('100.001234','101','102.001234'):
            with self.assertRaises(ValueError): self.call(gross)

    def test_full_refund_includes_service_less_network_fee(self):
        self.order['status']='releasing_refund'
        self.intent={'actor_id':1,'details':json.dumps({'address':'addr','gross':'102.001234','fee':'1','net':'101.001234'})}
        result=self.call('102.001234','101.001234')
        self.assertEqual(result['cap'],self.deposit['amount'])
        self.assertTrue(result['refund'])

    def test_credits_reduce_service_not_principal(self):
        self.order['credits']=D(2)
        self.deposit['amount']=self.invoice['amount']=D('100.001234')
        self.assertEqual(self.call()['service_charged'],0)
        self.assertEqual(self.call()['cap'],100)

    def test_wrong_or_missing_deposit_rejected(self):
        self.deposit['id']='another-order'
        with self.assertRaises(ValueError): self.call()
        self.deposit=None
        with self.assertRaises(ValueError): self.call()

    def test_wrong_actor_address_and_status_rejected(self):
        self.intent['actor_id']=1
        with self.assertRaises(ValueError): self.call()
        self.intent['actor_id']=2
        self.intent['details']=self.intent['details'].replace('addr','different')
        with self.assertRaises(ValueError): self.call()
        self.order['status']='paid'
        with self.assertRaises(ValueError): self.call()

    def test_underfunded_or_corrupt_fee_records_rejected(self):
        self.deposit['amount']=self.invoice['amount']=D(99)
        with self.assertRaises(ValueError): self.call()
        self.order['credits']=D(3)
        with self.assertRaises(ValueError): self.call()

    def test_history_fee_and_amount_limits(self):
        row={'address':'addr'}
        item={'address':'addr','coin':'USDT','network':'BSC','amount':'99','transactionFee':'1'}
        snapshot={'cap':'100','net':'99','fee':'1'}
        g.check_result(row,item,snapshot)
        for change in ({'amount':'100'},{'transactionFee':'2'},{'coin':'BTC'},{'amount':'98'},{'transactionFee':None}):
            with self.assertRaises((ValueError,ArithmeticError)):
                g.check_result(row,{**item,**change},snapshot)

    def test_statistics_count_only_completed_volume(self):
        rows=[{'status':'completed','n':3,'volume':D(300),'today_volume':D(100),'today_n':1},
              {'status':'cancelled','n':2,'volume':D(900)}, {'status':'releasing','n':1},
              {'status':'payout_review','n':2}, {'status':'refund_ready','n':1}]
        body=render(rows,datetime.now(timezone.utc))
        self.assertIn('In progress: 1',body)
        self.assertIn('Dispute / exception review: 2',body)
        self.assertIn('Total completed volume: 300.00 USDT',body)
        self.assertNotIn('900.00 USDT',body)


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_authorization_never_sends_or_reserves(self):
        writes=[]
        class Cur:
            async def execute(self,sql,args):
                writes.append(sql)
                self.row={'value':''} if 'payout_freeze' in sql else None
            async def fetchone(self): return self.row
        @asynccontextmanager
        async def tx(*a): yield Cur()
        request=AsyncMock()
        with patch.object(p,'require_ready',AsyncMock()),patch.object(p.db,'transaction',tx),patch.object(g,'authorize',AsyncMock(side_effect=ValueError('over budget'))),patch('utils.binance_api.make_api_request',request),patch.dict(os.environ,NEW_WITHDRAW_AMOUNT_MODE='gross'):
            with self.assertRaises(ValueError): await p.release('new:test','addr',100,D(1),D(99))
        request.assert_not_awaited()
        self.assertFalse(any(sql.startswith('INSERT INTO payouts') for sql in writes))

    async def test_frozen_account_never_calls_exchange(self):
        class Cur:
            async def execute(self,*a): pass
            async def fetchone(self): return {'value':'freeze reason'}
        @asynccontextmanager
        async def tx(*a): yield Cur()
        request=AsyncMock()
        with patch.object(p,'require_ready',AsyncMock()),patch.object(p.db,'transaction',tx),patch('utils.binance_api.make_api_request',request),patch.dict(os.environ,NEW_WITHDRAW_AMOUNT_MODE='gross'):
            with self.assertRaises(ValueError): await p.release('new:test','addr',100,D(1),D(99))
        request.assert_not_awaited()
