import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import discord
from modules.new_trading import NewTrading


class ForumStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog=object.__new__(NewTrading)
        self.cog.forum_lock=asyncio.Lock()
        self.cog.pending_forums=set()
        self.cog.scan_done=False
        self.panel=NS(id=20,edit=AsyncMock())
        self.thread=NS(id=10,guild=NS(id=2),parent_id=5,applied_tags=[NS(id=7)],
                       archived=False,locked=False,fetch_message=AsyncMock(return_value=NS(id=10)),
                       send=AsyncMock(return_value=self.panel))
        self.stored=None
        async def setting(key,value=None):
            if value is not None: self.stored=value
            return self.stored
        self.patches=[patch('modules.new_trading.cfg.FORUM_IDS',(5,)),
                      patch('modules.new_trading.cfg.BUY_TAGS',{}),
                      patch('modules.new_trading.cfg.SELL_TAGS',{5:7}),
                      patch('modules.new_trading.db.query',AsyncMock()),
                      patch('modules.new_trading.db.setting',setting)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    async def test_40058_retried_by_worker_without_duplicate(self):
        error=discord.Forbidden(NS(status=403,reason='Forbidden'),{'code':40058,'message':'starter not ready'})
        self.thread.send.side_effect=[error,self.panel]
        await self.cog.forum(self.thread)
        self.assertIn(10,self.cog.pending_forums)
        self.assertIsNone(self.stored)
        await self.cog.retry_forums(NS(threads=[self.thread]))
        self.assertEqual(self.stored,20)
        self.assertNotIn(10,self.cog.pending_forums)
        await self.cog.retry_forums(NS(threads=[self.thread]))
        self.assertEqual(self.thread.send.await_count,2)

    async def test_missing_starter_waits_without_sending(self):
        self.thread.fetch_message.side_effect=discord.NotFound(NS(status=404,reason='Not Found'),{'code':10008,'message':'Unknown Message'})
        await self.cog.forum(self.thread)
        self.thread.send.assert_not_awaited()
        self.assertIn(10,self.cog.pending_forums)

    async def test_simultaneous_events_send_one_panel(self):
        self.thread.fetch_message.return_value=self.panel
        await asyncio.gather(self.cog.forum(self.thread),self.cog.forum(self.thread))
        self.thread.send.assert_awaited_once()
        self.panel.edit.assert_awaited_once()

    async def test_real_permission_error_is_not_hidden(self):
        self.thread.send.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),{'code':50013,'message':'Missing Permissions'})
        with self.assertRaises(discord.Forbidden): await self.cog.forum(self.thread)
        self.assertNotIn(10,self.cog.pending_forums)

    async def test_one_forum_failure_does_not_abort_scan(self):
        self.cog.forum=AsyncMock(side_effect=[RuntimeError('permissions'),None])
        await self.cog.retry_forums(NS(threads=[self.thread,NS(id=11)]))
        self.assertEqual(self.cog.forum.await_count,2)
