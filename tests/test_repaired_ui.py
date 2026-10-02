"""Offline localization contracts and real callback boundaries, without services."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',
                  BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import ast
import itertools
from pathlib import Path
import string
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import discord
import config
from utils import ui_language as ui, payout_language
from modules.admin import Admin
from modules.new_trading import NewTrading
from modules.social import Social


def member(lang='English',uid=10,guild=None):
    index=list(ui.CATALOG).index(lang)+100
    return NS(id=uid,name='tester',guild=guild,roles=[NS(id=index)],guild_permissions=NS(administrator=True))


def role_config():
    return {lang:i+100 for i,lang in enumerate(ui.CATALOG)}


def context(lang):
    guild=NS(id=2,preferred_locale='zh-CN')
    return NS(guild=guild,author=member(lang,guild=guild),locale='zh-CN',
              respond=AsyncMock(),defer=AsyncMock(),followup=NS(send=AsyncMock()))


class CatalogTests(unittest.TestCase):
    def test_all_eleven_languages_have_matching_keys_and_placeholders(self):
        self.assertEqual(set(ui.CATALOG),set(payout_language.ROWS))
        formatter=string.Formatter()
        fields=lambda text:{key for _,key,_,_ in formatter.parse(text) if key is not None}
        for lang,pack in ui.CATALOG.items():
            self.assertEqual(set(pack),set(ui.CATALOG['English']))
            for key,value in pack.items():
                with self.subTest(language=lang,key=key):
                    self.assertTrue(value.strip())
                    self.assertEqual(fields(value),fields(ui.CATALOG['English'][key]))

    def test_new_guild_role_overrides_locale_and_defaults_to_english(self):
        with patch.object(config.NEW,'LANGUAGES',role_config()):
            for lang in ui.CATALOG:
                self.assertEqual(ui.language(context(lang)),lang)
            ctx=context('中文');ctx.author.roles=[]
            self.assertEqual(ui.language(ctx),'English')

    def test_legacy_locale_and_background_guild_default(self):
        guild=NS(id=1,preferred_locale='ja')
        ctx=NS(guild=guild,author=NS(roles=[]),locale='es-ES')
        self.assertEqual(ui.language(ctx),'Español')
        self.assertEqual(ui.language(guild),'日本語')
        with patch.object(config.NEW,'LANGUAGES',{'中文':123}):
            guild.roles=[NS(id=123)]
            self.assertEqual(ui.language(guild),'日本語')
        self.assertEqual(ui.language(NS(locale='unsupported')),'English')

    def test_supplied_text_is_not_translated_or_reinterpreted(self):
        supplied='用户 {reason} ?id=1 AND 1=2 `name`'
        for lang in ui.CATALOG:
            result=ui.text(lang,'legacy_019',v0=supplied,v1='123',v2='9.001234')
            self.assertIn(supplied,result)
            self.assertIn('9.001234',result)
            self.assertIn('123',result)

    def test_errors_never_expose_backend_text(self):
        for lang in ui.CATALOG:
            with self.assertLogs('utils.ui_language',level='WARNING'):
                result=ui.error(lang,ValueError('private_database.internal: password=hidden'))
            self.assertEqual(result,ui.text(lang,'operation_review'))
            self.assertNotIn('hidden',result)
            with self.assertLogs('utils.ui_language',level='WARNING'):
                self.assertEqual(ui.error(lang,ValueError('原因必须为 1–500 字')),ui.text(lang,'reason_required'))

    def test_buttons_and_full_financial_previews_fit_discord(self):
        for first,second in itertools.product(ui.CATALOG,repeat=2):
            for key in ('close_accept','close_hold'):
                self.assertLessEqual(len(' / '.join(ui.text(lang,key) for lang in dict.fromkeys((first,second)))),80)
        for lang in ui.CATALOG:
            preview=ui.text(lang,'review_preview',order='a'*32,decision=ui.decision(lang,'登记手动退款'),reason='原'*500)
            self.assertLessEqual(len(preview),2000,lang)
            preview=ui.text(lang,'manual_preview',order='a'*32,received='1000002.009999',net='1000002.009998',fee='0.000001',address='0x'+'a'*40,reason='x'*500)
            self.assertLessEqual(len(preview),2000,lang)
            receipt=ui.text(lang,'manual_receipt',net='1000002.009998',fee='0.000001',address='0x'+'a'*40,txid='0x'+'b'*64,deadline=2000000000)
            self.assertLessEqual(len(receipt),1024,lang)

    def test_registered_command_payloads_and_choice_values(self):
        commands=[Admin.admin_group,NewTrading.trade_admin,NewTrading.trade_callback,NewTrading.trade_sell_callback,
                  Social.check_boost_credits_slash,Social.check_and_grant_boost_credits]
        def check(node):
            for field,limit in (('name_localizations',32),('description_localizations',100)):
                for locale,value in node.get(field,{}).items():
                    self.assertIn(locale,ui.COMMAND_LOCALES)
                    self.assertTrue(value)
                    self.assertLessEqual(len(value),limit,(field,locale,value))
            for child in node.get('options',[]):check(child)
            for child in node.get('choices',[]):
                for value in child.get('name_localizations',{}).values():self.assertLessEqual(len(value),100)
        serialized=[]
        def detached(command):
            result=command.copy()
            if isinstance(result,discord.SlashCommandGroup):
                result.subcommands=[detached(child) for child in command.subcommands]
                for child in result.subcommands:child.parent=result
            return result
        for command in commands:
            bound=detached(command)
            bound._set_cog(NS())
            bound.integration_types={discord.IntegrationType.guild_install}
            bound.contexts={discord.InteractionContextType.guild}
            payload=bound.to_dict()
            check(payload);serialized.append(payload)
        review=next(option for option in serialized[1]['options'] if option['name']=='review')
        decisions=next(o for o in review['options'] if o['name']=='decision')['choices']
        self.assertEqual([choice['value'] for choice in decisions],list(ui.DECISIONS))
        admin_commands={o['name']:o for o in serialized[0]['options']}
        close=admin_commands['关闭抽奖']
        self.assertEqual(next(o for o in close['options'] if o['name']=='cancel')['type'],5)
        reroll=admin_commands['重新抽取']
        self.assertEqual(next(o for o in reroll['options'] if o['name']=='winners_count')['type'],4)
        market=admin_commands['市场']
        delete=next(o for o in market['options'] if o['name']=='删除用户货币交易')
        self.assertEqual(delete['options'][0]['type'],6)
        self.assertEqual(NewTrading.trade_callback.to_dict()['type'],2)
        self.assertEqual(NewTrading.trade_sell_callback.to_dict()['type'],2)
        self.assertEqual(NewTrading.trade_callback.name_localizations['ja'],ui.text('日本語','context_buy'))
        self.assertEqual(NewTrading.trade_sell_callback.name_localizations['es-ES'],ui.text('Español','context_sell'))
        self.assertEqual(NewTrading.trade_admin.default_member_permissions,discord.Permissions(administrator=True))

    def test_literal_template_calls_supply_all_required_values(self):
        formatter=string.Formatter()
        for filename in ('admin.py','social.py','new_trading.py'):
            tree=ast.parse((Path(__file__).resolve().parents[1]/'modules'/filename).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Name) or node.func.id!='ui_text':continue
                if len(node.args)<2 or not isinstance(node.args[1],ast.Constant):continue
                key=node.args[1].value
                self.assertIn(key,ui.CATALOG['English'])
                fields={field for _,field,_,_ in formatter.parse(ui.CATALOG['English'][key]) if field}
                self.assertLessEqual(fields,{kw.arg for kw in node.keywords},(filename,node.lineno,key))


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_preview_preserves_backend_decision_and_reason(self):
        cog=object.__new__(NewTrading)
        cog.order=AsyncMock(return_value={'id':'order','status':'payment_review'})
        reason='资金 {amount} ?id=1 AND 1=2'
        with patch.object(config.NEW,'LANGUAGES',role_config()),patch('modules.new_trading.db.query',AsyncMock(return_value=None)),patch('modules.new_trading.trade_review.prepare',AsyncMock(return_value={'code':'nonce'})) as prepare:
            for lang in ui.CATALOG:
                ctx=context(lang)
                await NewTrading.review.callback(cog,ctx,'order','退还买家',reason)
                prepare.assert_awaited_with('order',ctx.author.id,'退还买家',reason)
                send=ctx.followup.send.await_args
                self.assertEqual(send.args[0],ui.text(lang,'review_preview',order='order',decision=ui.decision(lang,'退还买家'),reason=reason))
                self.assertEqual(send.kwargs['view'].children[0].label,ui.text(lang,'confirm_decision'))
                self.assertFalse(send.kwargs['allowed_mentions'].everyone)

    async def test_manual_refund_notice_resolves_each_participant_language(self):
        guild=NS(id=2)
        buyer=member('日本語',1,guild);seller=member('Español',2,guild)
        guild.get_member=lambda uid:{1:buyer,2:seller}[uid]
        row={'id':'order','buyer_id':1,'seller_id':2,'channel_id':8,'status':'manual_refunded'}
        channel=NS(id=8,guild=guild,send=AsyncMock(return_value=NS(id=77)))
        cog=object.__new__(NewTrading);cog.cleanup_lock=asyncio.Lock()
        cog.bot=NS(user=NS(id=99),get_channel=lambda _:channel)
        cog.order=AsyncMock(return_value=row);cog.save_manual_close=AsyncMock()
        state={'phase':'unnotified','generation':'g1','details':{'net':'5.999999','fee':'0.010001','address':'0x'+'a'*40,'withdrawal':{'txId':'0x'+'b'*64}}}
        with patch.object(config.NEW,'LANGUAGES',role_config()),patch('modules.new_trading.db.setting',AsyncMock(return_value=state)):
            await cog.manual_close_tick('order')
        send=channel.send.await_args
        self.assertIn(ui.text('日本語','manual_buyer'),send.args[0])
        self.assertIn(ui.text('Español','manual_seller'),send.args[0])
        self.assertEqual([f.name for f in send.kwargs['embed'].fields],['日本語','Español'])
        for field in send.kwargs['embed'].fields:
            self.assertIn('5.999999',field.value);self.assertIn('0.010001',field.value)
            self.assertIn(state['details']['address'],field.value)
        self.assertFalse(send.kwargs['allowed_mentions'].everyone)
        self.assertEqual(cog.save_manual_close.await_args.args[1]['phase'],'waiting')
        self.assertEqual(send.kwargs['view'].children[0].custom_id,'new:refund_close:accept:order:g1')

    async def test_context_menu_dispatch_keeps_buy_sell_direction(self):
        cog=object.__new__(NewTrading);cog.start=AsyncMock()
        ctx=context('한국어');target=NS(id=20)
        await NewTrading.trade_callback.callback(cog,ctx,target)
        cog.start.assert_awaited_with(ctx,target,buy=True)
        await NewTrading.trade_sell_callback.callback(cog,ctx,target)
        cog.start.assert_awaited_with(ctx,target,buy=False)
