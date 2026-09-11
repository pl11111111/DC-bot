import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_trading import NewTrading


class TimeoutChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row=dict(id='order',channel_id=8,buyer_id=1,seller_id=2,status='payment_review')
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

    async def test_expired_persisted_timer_closes_channel_only(self):
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=[{'hold':False},None,0])) as query, patch('modules.new_trading.db.setting',AsyncMock(return_value={'deadline':0})), patch('modules.new_trading.db.audit',AsyncMock()):
            await self.cog.timeout_channel(self.row)
            self.channel.delete.assert_awaited_once()
            self.assertFalse(any('UPDATE orders' in call.args[0] for call in query.await_args_list))
