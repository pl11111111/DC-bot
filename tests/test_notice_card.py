import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,MagicMock,patch
import discord
from utils import notice_card
from modules.new_community import NewCommunity,buttons


class NoticeCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_banner_body_and_buttons_share_container(self):
        view=buttons([('Verify','verify')])
        layout=notice_card.layout('Rules','Body','https://example.com/banner.png',view)
        self.assertEqual(len(layout),1)
        self.assertEqual(layout[0]['type'],17)
        self.assertEqual([x['type'] for x in layout[0]['components']],[12,10,1])

    async def test_ephemeral_preview_uses_webhook_and_registers_button_callback(self):
        view=buttons([('Publish','publish')])
        layout=notice_card.layout('Rules','Body','',view)
        state=NS(store_view=MagicMock())
        inter=NS(response=NS(defer=AsyncMock()),followup=NS(id=1,token='test-only',session=object()),_state=state)
        adapter=NS(execute_webhook=AsyncMock(return_value={'id':'22'}))
        with patch.object(notice_card,'async_context',NS(get=lambda:adapter)):
            await notice_card.preview(inter,layout,view)
        payload=adapter.execute_webhook.await_args.kwargs['payload']
        self.assertEqual(payload['flags'],32768|64)
        self.assertNotIn('embeds',payload)
        state.store_view.assert_called_once_with(view,22)

    async def publish_case(self,failure=None):
        cog=object.__new__(NewCommunity)
        cog.bot=NS(user=NS(id=99))
        channel=MagicMock(spec=discord.TextChannel)
        channel.guild=NS(id=2); channel.id=10; channel.mention='#rules'
        actor=NS(id=7)
        ctx=NS(guild=NS(id=2),author=actor,respond=AsyncMock(),send_modal=AsyncMock())
        msg=NS(id=20,channel=channel)
        send=AsyncMock(side_effect=[failure,msg] if failure else None,return_value=msg)
        with patch('modules.new_community.admin',return_value=True),patch('modules.new_community.db.setting',AsyncMock()),patch('modules.new_community.db.audit',AsyncMock()),patch('modules.new_community.notice_card.send',send),patch('modules.new_community.notice_card.preview',AsyncMock()) as preview:
            await cog.notice_editor(ctx,channel)
            modal=ctx.send_modal.await_args.args[0]
            for field,value in zip(modal.children,['Title','Body','','']): field._input_value=value
            inter=NS(id=123,response=NS(send_message=AsyncMock()))
            await modal.callback(inter)
            button=preview.await_args.args[2].children[0]
            click=NS(user=actor,response=NS(send_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()),
                     message=NS(edit=AsyncMock(side_effect=discord.NotFound(NS(status=404,reason='Unknown message'),'Unknown Message'))))
            await button.callback(click)
            if failure:
                self.assertIn('发布未完成',click.followup.send.await_args.args[0])
                await button.callback(click)
            self.assertIn('已发布成功',click.followup.send.await_args.args[0])
            click.message.edit.assert_not_awaited()
            count=send.await_count
            await button.callback(click)
            self.assertEqual(send.await_count,count)

    async def test_preview_unknown_message_cannot_block_publication(self):
        await self.publish_case()

    async def test_permission_denial_can_retry_without_duplicate_success(self):
        await self.publish_case(discord.Forbidden(NS(status=403,reason='Forbidden'),'Missing permissions'))
