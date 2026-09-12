import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,MagicMock,patch
import discord
from modules.new_community import NewCommunity


class CommunityPublishingTests(unittest.IsolatedAsyncioTestCase):
    def actor(self): return NS(id=7)
    def interaction(self):
        return NS(user=self.actor(),response=NS(send_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()),message=NS(edit=AsyncMock()))

    async def test_rules_and_verify_editor_save_separate_content_and_buttons(self):
        cog=object.__new__(NewCommunity)
        channel=MagicMock(spec=discord.TextChannel)
        channel.guild=NS(id=2)
        channel.mention='#configured-channel'
        cog.channel=AsyncMock(return_value=channel)
        cog.save_panel=AsyncMock()
        for kind in ('rules','verify'):
            channel.id=10 if kind=='rules' else 11
            ctx=NS(guild=NS(id=2),author=self.actor(),respond=AsyncMock(),send_modal=AsyncMock())
            with patch('modules.new_community.cfg.RULES_CHANNEL_ID',10),patch('modules.new_community.cfg.VERIFY_CHANNEL_ID',11),patch('modules.new_community.admin',return_value=True),patch('modules.new_community.db.setting',AsyncMock(return_value=None)) as setting,patch('modules.new_community.db.audit',AsyncMock()):
                await NewCommunity.panel.callback(cog,ctx,channel,kind=='verify')
                modal=ctx.send_modal.await_args.args[0]
                for field,value in zip(modal.children,[kind+' title',kind+' body','https://example.com/'+kind+'.png']):
                    field._input_value=value
                inter=self.interaction()
                await modal.callback(inter)
                button=inter.response.send_message.await_args.kwargs['view'].children[0]
                click=self.interaction()
                await button.callback(click)
                self.assertEqual(setting.await_args.args[0],'community_content:channel:'+str(channel.id))
                saved=cog.save_panel.await_args
                self.assertEqual(saved.args[0],10 if kind=='rules' else 11)
                self.assertEqual(saved.kwargs['banner'],'https://example.com/'+kind+'.png')
                if kind=='rules': self.assertIsNone(saved.args[4])
                else: self.assertEqual(saved.args[4].children[0].custom_id,'new:verify')
                count=cog.save_panel.await_count
                await button.callback(click)
                self.assertEqual(cog.save_panel.await_count,count)

    async def test_saved_panel_wins_over_stale_maintenance_snapshot(self):
        cog=object.__new__(NewCommunity)
        cog.panel_lock=asyncio.Lock()
        cog._save_panel=AsyncMock()
        with patch('modules.new_community.db.setting',AsyncMock(return_value=dict(title='new',body='new body',banner=''))):
            await cog.save_panel(10,'channel:10','old','old body',banner='https://example.com/old.png')
        self.assertEqual(cog._save_panel.await_args.args,(10,'channel:10','new','new body',None,''))

    async def test_old_maintenance_does_not_overwrite_channel_panel(self):
        cog=object.__new__(NewCommunity)
        cog.panel_lock=asyncio.Lock()
        cog._save_panel=AsyncMock()
        with patch('modules.new_community.db.setting',AsyncMock(return_value={'verification':False})):
            await cog.save_panel(10,'verify','old','old body')
        cog._save_panel.assert_not_awaited()

    async def forum_case(self,editing=False,pin_error=False):
        forum=MagicMock(spec=discord.ForumChannel)
        forum.id=10; forum.guild=NS(id=2); forum.available_tags=[]; forum.requires_tag=False
        thread=MagicMock(spec=discord.Thread)
        thread.id=20; thread.parent_id=10; thread.edit=AsyncMock()
        msg=NS(id=20,channel=thread,author=NS(id=99),edit=AsyncMock())
        thread.fetch_message=AsyncMock(return_value=msg)
        forum.create_thread=AsyncMock(return_value=thread)
        if pin_error:
            thread.edit.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),'Missing permissions')
        cog=object.__new__(NewCommunity)
        cog.bot=NS(user=NS(id=99))
        cog.channel=AsyncMock(return_value=thread)
        ctx=NS(guild=NS(id=2),author=self.actor(),respond=AsyncMock(),send_modal=AsyncMock())
        previous=dict(channel=20,title='old',body='old',banner='') if editing else None
        with patch('modules.new_community.admin',return_value=True),patch('modules.new_community.db.setting',AsyncMock(return_value=previous)) as setting,patch('modules.new_community.db.audit',AsyncMock()):
            await cog.notice_editor(ctx,forum,'20' if editing else '',pin=True)
            modal=ctx.send_modal.await_args.args[0]
            for field,value in zip(modal.children,['Rules','New rules','','']): field._input_value=value
            inter=self.interaction()
            await modal.callback(inter)
            button=inter.response.send_message.await_args.kwargs['view'].children[0]
            click=self.interaction()
            await button.callback(click)
            if editing:
                forum.create_thread.assert_not_awaited()
                msg.edit.assert_awaited_once()
                self.assertEqual(thread.edit.await_args_list[0].kwargs,dict(name='Rules',archived=False))
            else: forum.create_thread.assert_awaited_once()
            self.assertTrue(thread.edit.await_args.kwargs['pinned'])
            self.assertEqual(setting.await_args.args[0],'notice:20')
            if pin_error: self.assertIn('内容已保存，但置顶失败',click.followup.send.await_args.args[0])

    async def test_create_and_pin_forum_rules(self): await self.forum_case()
    async def test_edit_existing_rule_without_duplicate_thread(self): await self.forum_case(editing=True)
    async def test_pin_failure_keeps_saved_message_id(self): await self.forum_case(pin_error=True)
