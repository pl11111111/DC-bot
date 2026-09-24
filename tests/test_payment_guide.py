import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from utils import payment_guide as guide


class GuideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved={}
        self.forum=MagicMock(spec=discord.ForumChannel)
        self.forum.id=3; self.forum.guild=NS(id=2,active_threads=AsyncMock(return_value=[]))
        self.forum.requires_tag=False; self.forum.available_tags=[]
        async def archived(**kwargs):
            for item in []: yield item
        self.forum.archived_threads=archived
        self.thread=NS(id=4,parent_id=3,archived=False,fetch_message=AsyncMock())
        self.message=NS(id=4,channel=self.thread,author=NS(id=99))
        self.thread.fetch_message.return_value=self.message
        self.bot=NS(user=NS(id=99),get_channel=lambda ident:{3:self.forum,4:self.thread}.get(ident),fetch_channel=AsyncMock())
        async def setting(key,value=None):
            if value is not None: self.saved[key]=dict(value)
            return self.saved.get(key)
        for target,value in [('GUIDES_CHANNEL_ID',3),('GUIDES_TAG_ID',0),('GUILD_ID',2)]:
            p=patch.object(guide.cfg,target,value); p.start(); self.addCleanup(p.stop)
        p=patch.object(guide.db,'setting',setting);p.start();self.addCleanup(p.stop)

    async def test_publish_once_then_link_existing_message(self):
        with patch.object(guide.notice_card,'create_forum',AsyncMock(return_value=self.message)) as create,patch.object(guide.notice_card,'edit',AsyncMock()) as edit:
            await guide.sync(self.bot)
            await guide.sync(self.bot)
        create.assert_awaited_once();edit.assert_not_awaited()
        self.assertEqual(await guide.url(),'https://discord.com/channels/2/4/4')

    async def test_ambiguous_creation_never_blindly_reposts(self):
        with patch.object(guide.notice_card,'create_forum',AsyncMock(side_effect=TimeoutError())) as create:
            with self.assertRaises(TimeoutError): await guide.sync(self.bot)
            await guide.sync(self.bot)
        create.assert_awaited_once()
        self.assertEqual(await guide.url(),'https://discord.com/channels/2/3')

    async def test_response_loss_recovers_bot_owned_forum_post(self):
        self.saved['payment_guide:3']={'phase':'publishing'}
        self.thread.owner_id=99;self.thread.name=guide.TITLE
        self.forum.guild.active_threads.return_value=[self.thread]
        with patch.object(guide.notice_card,'create_forum',AsyncMock()) as create,patch.object(guide.notice_card,'edit',AsyncMock()) as edit:
            await guide.sync(self.bot)
        create.assert_not_awaited();edit.assert_awaited_once()
        self.assertEqual(self.saved['payment_guide:3']['phase'],'ready')

    async def test_required_tag_fails_before_publication_intent(self):
        self.forum.requires_tag=True
        with patch.object(guide.notice_card,'create_forum',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'NEW_GUIDES_TAG_ID'): await guide.sync(self.bot)
        create.assert_not_awaited();self.assertEqual(self.saved,{})

    async def test_other_guild_is_rejected(self):
        self.forum.guild.id=1
        with self.assertRaises(ValueError): await guide.sync(self.bot)
        self.assertEqual(self.saved,{})
