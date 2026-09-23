import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_trading import NewTrading


class TimeoutChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row=dict(id='order',channel_id=8,buyer_id=1,seller_id=2,status='payment_timeout')
        self.channel=NS(id=8,guild=NS(id=2),send=AsyncMock(),delete=AsyncMock())
        self.cog=object.__new__(NewTrading)
        self.cog.cleanup_lock=asyncio.Lock()
        self.cog.bot=NS(get_channel=lambda _:self.channel,user=NS(id=99))
        self.cog.order=AsyncMock(return_value=self.row)
        self.cog.post=AsyncMock()

    async def test_notice_precedes_persisted_timer_and_does_not_delete(self):
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=[{'hold':False},None])), patch('modules.new_trading.db.setting',AsyncMock(return_value=None)) as setting, patch('modules.new_trading.db.audit',AsyncMock()):
            await self.cog.timeout_channel(self.row)
            self.cog.post.assert_awaited_once()
            self.channel.send.assert_awaited_once()
            self.assertIn('deadline',setting.await_args.args[1])
            self.channel.delete.assert_not_awaited()

    async def test_failed_notice_does_not_arm_deletion(self):
        self.channel.send.side_effect=RuntimeError('send failed')
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=[{'hold':False},None])), patch('modules.new_trading.db.setting',AsyncMock(return_value=None)) as setting:
            with self.assertRaises(RuntimeError): await self.cog.timeout_channel(self.row)
            self.assertEqual(setting.await_count,1)
            self.channel.delete.assert_not_awaited()

    async def test_hold_or_deposit_prevents_close(self):
        for results in ([{'hold':True}],[{'hold':False},{'id':'deposit'}]):
            with patch('modules.new_trading.db.query',AsyncMock(side_effect=results)):
                await self.cog.timeout_channel(self.row)
        self.channel.delete.assert_not_awaited()

    async def test_expired_timer_finishes_order_before_channel_delete(self):
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=[{'hold':False},None,0])) as query, patch('modules.new_trading.db.setting',AsyncMock(return_value={'deadline':0})), patch('modules.new_trading.db.audit',AsyncMock()), patch('modules.new_trading.trade_timeout.close_unpaid',AsyncMock()) as close:
            await self.cog.timeout_channel(self.row)
            close.assert_awaited_once_with('order',99,'付款超时且倒计时内未申请保留',automatic=True)
            self.channel.delete.assert_awaited_once()
            self.assertFalse(any('UPDATE orders' in call.args[0] for call in query.await_args_list))

    async def test_review_orders_never_auto_expire(self):
        self.row['status']='payment_review'
        with patch('modules.new_trading.trade_timeout.close_unpaid',AsyncMock()) as close:
            await self.cog.timeout_channel(self.row)
            close.assert_not_awaited()
        self.channel.delete.assert_not_awaited()

    async def test_failed_financial_check_keeps_channel(self):
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=[{'hold':False},None])), patch('modules.new_trading.db.setting',AsyncMock(return_value={'deadline':0})), patch('modules.new_trading.trade_timeout.close_unpaid',AsyncMock(side_effect=ValueError('lookup failed'))):
            with self.assertRaises(ValueError): await self.cog.timeout_channel(self.row)
        self.channel.delete.assert_not_awaited()

class DepositLookupTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self,pages):
        from datetime import datetime
        from utils import shared_payments as payments
        invoice={'created_at':datetime.utcnow(),'address':'address','amount':7,'state':'waiting'}
        with patch.object(payments,'require_ready',AsyncMock()), patch.object(payments.db,'query',AsyncMock(side_effect=[invoice,None])), patch('utils.binance_api.make_api_request',AsyncMock(side_effect=pages)) as api:
            result=await payments.find_deposit('new:order','address',7)
        return result,api

    async def test_failed_or_incomplete_query_never_means_no_payment(self):
        for pages in ([None],[{'code':-1}],[{'error':'unavailable'}]):
            with self.assertRaises(ValueError): await self.lookup(pages)
        matching={'coin':'USDT','network':'BSC','address':'address','amount':7,'status':1,'id':'d','txId':'tx'}
        with self.assertRaises(ValueError):
            await self.lookup([[matching]+[{'coin':'BTC'}]*999,None])

    async def test_pagination_empty_result_and_pending_credit(self):
        result,api=await self.lookup([[{'coin':'BTC'}]*1000,[]])
        self.assertIsNone(result)
        self.assertEqual([c.args[2]['offset'] for c in api.await_args_list],[0,1000])
        with self.assertRaises(ValueError):
            await self.lookup([[{'coin':'USDT','network':'BSC','address':'address','amount':7,'status':0}]])
