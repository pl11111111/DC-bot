import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_trading import NewTrading


class PaymentHelpTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self,actor=1):
        row=dict(id='order',status='paying',buyer_id=1,seller_id=2,channel_id=8,source_id=9)
        cog=object.__new__(NewTrading)
        cog.order=AsyncMock(return_value=row)
        cog.cleanup_lock=asyncio.Lock()
        cog.post=AsyncMock(); cog.alert=AsyncMock()
        inter=NS(data={'custom_id':'new:trade:payment_help:order'},guild=NS(id=2),channel_id=8,channel=NS(id=8),user=NS(id=actor),
                 response=NS(defer=AsyncMock(),send_message=AsyncMock()),followup=NS(send=AsyncMock()))
        return cog,inter

    async def test_help_holds_channel_and_pauses_order_without_refund(self):
        cog,inter=self.setup_case()
        calls=[]
        class Cursor:
            async def execute(self,*args): calls.append(args)
            async def fetchone(self): return {'status':'paying'}
        @asynccontextmanager
        async def tx(): yield Cursor()
        with patch('modules.new_trading.db.setting',AsyncMock(return_value=None)),patch('modules.new_trading.db.transaction',tx),patch('modules.new_trading.payments.release',AsyncMock()) as release:
            await cog.on_interaction(inter)
            release.assert_not_awaited()
        self.assertTrue(any("status='payment_review'" in call[0] for call in calls))
        self.assertTrue(any('hold=TRUE' in call[0] and call[1]==(8,9) for call in calls))
        self.assertTrue(cog.alert.await_args.kwargs['notify_admins'])
        self.assertIn('暂停自动履约',inter.followup.send.await_args.args[0])

    async def test_repeated_help_does_not_repeat_admin_ping(self):
        cog,inter=self.setup_case()
        with patch('modules.new_trading.db.setting',AsyncMock(return_value={'time':time.time()})):
            await cog.on_interaction(inter)
        cog.alert.assert_not_awaited()
        self.assertIn('管理员已收到请求',inter.followup.send.await_args.args[0])

    async def test_outsider_cannot_request_help(self):
        cog,inter=self.setup_case(actor=3)
        await cog.on_interaction(inter)
        cog.alert.assert_not_awaited()
        self.assertIn('无权',inter.response.send_message.await_args.args[0])

    async def test_step_mentions_target_correct_participants(self):
        cog=object.__new__(NewTrading)
        channel=NS(send=AsyncMock())
        row=dict(buyer_id=1,seller_id=2)
        for status,ids in [('confirmed',[1,2]),('paying',[1,2]),('paid',[1,2]),('shipped',[1]),('receipt_confirmed',[2]),('refund_ready',[1])]:
            await cog.notify_step(channel,{**row,'status':status})
            text=channel.send.await_args.args[0]
            self.assertNotIn('卡片',text)
            for uid,label in ((1,'🛒 买家'),(2,'📦 卖家')):
                if uid in ids:
                    self.assertIn(f'{label} <@{uid}>：',text)
                    self.assertEqual(text.count(f'<@{uid}>'),1)
            sent=channel.send.await_args
            mentions=sent.kwargs['allowed_mentions'].to_dict()
            self.assertEqual(mentions['users'],ids)
            self.assertNotIn('everyone',mentions['parse'])
            if status=='paying': self.assertIn('不要替买家付款',sent.args[0])
