import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import translation_store as store, translation_editor as editor


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    def test_link_scope(self):
        self.assertEqual(store.parse_link('https://discord.com/channels/2/30/40',2),(30,40))
        for value in ['https://discord.com/channels/1/30/40','https://evil.com/channels/2/30/40','https://discord.com/channels/@me/30/40']:
            with self.assertRaises(ValueError): store.parse_link(value,2)

    async def test_stale_override_never_uses_file_translation(self):
        original={'title':'Rules','body':'new rules'}
        record={'source_hash':store.fingerprint(original),'title':'规则','body':'新规则'}
        with patch.object(store,'lookup',AsyncMock(return_value=record)):
            self.assertEqual(await store.resolve(40,original,'中文'),({'title':'规则','body':'新规则'},False))
            with patch('utils.verification_flow.translated') as fallback:
                changed=dict(original,body='changed')
                self.assertEqual(await store.resolve(40,changed,'中文'),(changed,True))
                fallback.assert_not_called()

    async def test_conflicting_save_cannot_overwrite_or_audit(self):
        cur=NS(execute=AsyncMock(),fetchone=AsyncMock(return_value={'value':json.dumps({'revision':'newer'})}))
        @asynccontextmanager
        async def transaction(): yield cur
        with patch.object(store.db,'transaction',transaction):
            with self.assertRaisesRegex(ValueError,'其他操作'):
                await store.save(40,30,{'title':'Rules','body':'Rules'},'中文','规则','正文',7,'older')
        self.assertEqual(cur.execute.await_count,2)

    async def test_editor_repeated_confirmation_saves_once(self):
        source={'title':'Rules','body':'Original'}
        ctx=NS(author=NS(id=7),defer=AsyncMock(),followup=NS(send=AsyncMock()))
        message=NS(id=40)
        channel=NS(get_partial_message=lambda ident:message)
        cog=NS(channel=AsyncMock(return_value=channel),bot=NS(user=NS(id=99)),panel_lock=asyncio.Lock())
        def interaction():
            return NS(response=NS(send_modal=AsyncMock(),defer=AsyncMock(),send_message=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch.object(editor,'authorized',AsyncMock(return_value=True)),patch.object(editor.articles,'read',AsyncMock(return_value=source)),patch.object(store,'lookup',AsyncMock(return_value=None)),patch.object(store,'resolve',AsyncMock(return_value=({'title':'规则','body':'正文'},False))),patch.object(store,'save',AsyncMock()) as save,patch.object(editor,'ensure_button',AsyncMock()):
            await editor.open_editor(cog,ctx,'https://discord.com/channels/2/30/40','中文')
            view=ctx.followup.send.call_args.kwargs['view']
            click=interaction()
            await view.children[0].callback(click)
            modal=click.response.send_modal.call_args.args[0]
            submitted=interaction()
            await modal.callback(submitted)
            preview=submitted.followup.send.call_args.kwargs['view']
            confirm=interaction()
            await preview.children[0].callback(confirm)
            await preview.children[0].callback(confirm)
            save.assert_awaited_once()

    async def test_authorization_rechecks_member_and_guild(self):
        member=NS(guild_permissions=NS(administrator=False),roles=[])
        inter=NS(guild=NS(id=2,fetch_member=AsyncMock(return_value=member)),user=NS(id=7))
        self.assertFalse(await editor.authorized(inter,7))
        member.guild_permissions.administrator=True
        self.assertTrue(await editor.authorized(inter,7))
        self.assertFalse(await editor.authorized(inter,8))
        inter.guild.id=1
        self.assertFalse(await editor.authorized(inter,7))

    async def test_existing_button_is_preserved_without_patch(self):
        raw={'author':{'id':'99'},'components':[{'type':1,'components':[{'custom_id':'new:translate'}]}]}
        http=NS(request=AsyncMock(return_value=raw))
        await editor.ensure_button(NS(channel=NS(id=30),id=40,_state=NS(http=http)),99)
        self.assertEqual(http.request.await_count,1)
