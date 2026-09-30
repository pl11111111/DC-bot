import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_community import NewCommunity,cfg
from utils import community_private as cp

class CommunityPrivateTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_update_replies_in_new_language_not_old_member_snapshot(self):
        for lang in cp.ROWS:
            clear=lang=='English'
            member=NS(id=7,roles=[],add_roles=AsyncMock(),remove_roles=AsyncMock())
            guild=NS(id=2,fetch_member=AsyncMock(return_value=member),get_role=lambda ident:NS(id=ident))
            inter=NS(guild=guild,user=member,channel_id=3,data={'custom_id':'new:lang:'+('clear' if clear else '123')},response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            cog=object.__new__(NewCommunity);cog.role_locks={}
            with patch.object(cfg,'LANGUAGE_CHANNEL_ID',3),patch.object(cfg,'LANGUAGES',{lang:123}),patch('modules.new_community.safe_self_role',return_value=True),patch('modules.new_community.db.audit',AsyncMock()),patch('modules.new_community.db.setting',AsyncMock()) as setting:
                await cog.on_interaction(inter)
            self.assertEqual(inter.followup.send.call_args.args[0],cp.text(lang,'updated'))
            setting.assert_awaited_once_with('verify_language:7',lang)
            self.assertTrue(inter.followup.send.call_args.kwargs['ephemeral'])

    async def test_invite_counts_follow_member_language(self):
        member=NS(id=7,roles=[NS(id=123)])
        inter=NS(guild=NS(id=2),user=member,data={'custom_id':'new:invites'},response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch.object(cfg,'LANGUAGES',{'日本語':123}),patch('modules.new_community.db.query',AsyncMock(return_value={'total':0,'verified':None})):
            await object.__new__(NewCommunity).on_interaction(inter)
        self.assertEqual(inter.followup.send.call_args.args[0],cp.text('日本語','counts',total=0,verified=0))
