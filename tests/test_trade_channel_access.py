import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,MagicMock,patch
import discord
from modules.new_trading import NewTrading
from modules.new_community import NewCommunity


class TradeAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_terms_new_channel_history_access_and_mentions(self):
        cog=object.__new__(NewTrading)
        buyer=MagicMock(spec=discord.Member); buyer.id=1; buyer.bot=False
        seller=MagicMock(spec=discord.Member); seller.id=2; seller.bot=False
        guild=NS(id=2,default_role=MagicMock(),me=MagicMock(),fetch_member=AsyncMock(side_effect=[seller,buyer]),get_role=lambda _:None)
        category=MagicMock(spec=discord.CategoryChannel); category.guild=guild
        channel=NS(id=8,mention='#trade',send=AsyncMock())
        guild.create_text_channel=AsyncMock(return_value=channel)
        cog.bot=NS(get_channel=lambda _:category)
        cog.post=AsyncMock(); cog.order=AsyncMock(return_value={})
        ctx=NS(guild=guild,user=buyer,send_modal=AsyncMock())
        calls=[]
        class Cursor:
            async def execute(self,*args): calls.append(args)
            async def fetchone(self): return {'n':0}
        @asynccontextmanager
        async def tx(): yield Cursor()
        with patch('modules.new_trading.cfg.PAYMENTS_ENABLED',True),patch('modules.new_trading.payments.payout_amount_quote',AsyncMock()),patch('modules.new_trading.db.transaction',tx),patch('modules.new_trading.db.query',AsyncMock()),patch('modules.new_trading.db.audit',AsyncMock()):
            await cog.start(ctx,seller)
            modal=ctx.send_modal.await_args.args[0]
            self.assertFalse(modal.children[2].required)
            for field,value in zip(modal.children,['Item','3.01','']): field._input_value=value
            inter=NS(user=buyer,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            await modal.callback(inter)
        overwrites=guild.create_text_channel.await_args.kwargs['overwrites']
        self.assertTrue(overwrites[buyer].read_message_history)
        self.assertTrue(overwrites[seller].read_message_history)
        self.assertFalse(overwrites[guild.default_role].view_channel)
        create=next(args for args in calls if args[0].startswith('INSERT INTO orders'))
        self.assertEqual(create[1][6],'')
        mention=channel.send.await_args.kwargs['allowed_mentions'].to_dict()
        self.assertEqual(mention['users'],[1,2])
        self.assertNotIn('everyone',mention['parse'])

    async def test_repair_changes_only_participant_overwrites(self):
        cog=object.__new__(NewTrading)
        member=NS(id=1)
        guild=NS(id=2,get_member=lambda _:member)
        channel=MagicMock(spec=discord.TextChannel); channel.id=8; channel.guild=guild
        channel.overwrites_for.return_value=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=False,manage_messages=False)
        channel.set_permissions=AsyncMock()
        cog.bot=NS(get_channel=lambda _:channel,user=NS(id=99))
        with patch('modules.new_trading.db.query',AsyncMock(return_value=[dict(id='order',channel_id=8,buyer_id=1,seller_id=2)])),patch('modules.new_trading.db.audit',AsyncMock()):
            await cog.repair_trade_access(guild)
        overwrite=channel.set_permissions.await_args.kwargs['overwrite']
        self.assertTrue(overwrite.read_message_history)
        self.assertFalse(overwrite.manage_messages)
        self.assertTrue(cog.access_ready)

    async def test_language_panel_uses_local_banner_attachment(self):
        cog=object.__new__(NewCommunity)
        cog.channel=AsyncMock(return_value=NS(id=8))
        cog._publish_panel_message=AsyncMock()
        await cog._save_panel(8,'language','Languages','Choose one')
        call=cog._publish_panel_message.await_args
        self.assertEqual(call.args[2][0]['components'][0]['items'][0]['media']['url'],'attachment://Language.png')
        self.assertEqual(call.args[3].filename,'Language.png')
