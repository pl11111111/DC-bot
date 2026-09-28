import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import verification_flow as f

class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original={'title':'Rules','body':'Read these rules'}
        self.cog=NS(role_locks={},verify_count=AsyncMock())
        self.inter=NS(user=NS(id=7),channel_id=3,message=NS(id=4))

    async def test_language_precedence_and_client_fallback(self):
        with patch.object(f.cfg,'LANGUAGES',{'中文':12,'日本語':13}):
            self.assertEqual(f.preferred(NS(roles=[]),'zh-TW'),'中文')
            self.assertEqual(f.preferred(NS(roles=[]),'de'),'English')
            self.assertEqual(f.preferred(NS(roles=[]),'ja','English'),'English')
            self.assertEqual(f.preferred(NS(roles=[NS(id=12)]),'ja','English'),'中文')

    async def test_initial_step_has_no_agree_and_translation_cannot_verify(self):
        view=f.VerificationView(self.cog,self.inter,self.original,'English')
        self.assertEqual(view.stage,'language')
        self.assertFalse(any('agree' in (getattr(x,'label','') or '') for x in view.children))
        with self.assertRaises(ValueError): await view.agree(self.inter)
        translated=f.VerificationView(self.cog,self.inter,self.original,'English',True)
        with self.assertRaises(ValueError): await translated.agree(self.inter)
        self.cog.verify_count.assert_not_awaited()

    async def test_stale_translation_falls_back_to_original(self):
        result,missing=f.translated(self.original,'中文')
        self.assertTrue(missing)
        self.assertEqual(result,self.original)

    async def test_wrong_owner_and_changed_rules_cannot_advance(self):
        view=f.VerificationView(self.cog,self.inter,self.original,'English')
        click=NS(guild=NS(id=2),user=NS(id=8),response=NS(send_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()))
        await view.children[-1].callback(click)
        click.response.send_message.assert_awaited_once()
        click.user.id=7
        with patch.object(f,'source',AsyncMock(return_value={'title':'Rules','body':'Changed'})):
            await view.children[-1].callback(click)
        self.assertEqual(view.stage,'language')
        self.cog.verify_count.assert_not_awaited()

    async def test_agree_grants_selected_language_and_verification_only(self):
        verified=NS(id=9); language=NS(id=12); old=NS(id=13)
        member=NS(roles=[old],remove_roles=AsyncMock(),add_roles=AsyncMock())
        guild=NS(fetch_member=AsyncMock(return_value=member),get_role=lambda ident:{9:verified,12:language}.get(ident))
        view=f.VerificationView(self.cog,self.inter,self.original,'中文')
        view.stage='rules'
        with patch.object(f.cfg,'LANGUAGES',{'中文':12,'日本語':13}),patch.object(f.cfg,'VERIFIED_ROLE_ID',9),patch('modules.new_community.safe_self_role',return_value=True),patch.object(f.db,'audit',AsyncMock()):
            await view.agree(NS(guild=guild))
        member.remove_roles.assert_awaited_once_with(old,reason='Verification language selection')
        self.assertEqual([c.args[0].id for c in member.add_roles.await_args_list],[12,9])
        self.cog.verify_count.assert_awaited_once_with(7)
        self.assertTrue(view.done)
