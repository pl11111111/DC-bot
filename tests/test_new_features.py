"""Offline tests: no Discord login, MySQL connection or money movement."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from unittest.mock import AsyncMock,patch
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace as NS
import asyncio
import discord
from utils import shared_payments as p
from utils.guild_isolation import IsolatedBot,command_allowed
from modules.new_trading import NewTrading
from modules.new_community import embeds
from modules.new_community import NewCommunity
from modules.new_message_log import NewMessageLog

class MoneyTests(unittest.TestCase):
    def test_invalid_amounts(self):
        for value in ('NaN','Infinity','-1','0','1000001','0.0000001',
                      '?id=1 AND 1=2',"1' OR '1'='1",'',None,'10 USDT','10,50','1e99999999999999999999'):
            with self.subTest(value=value), self.assertRaises(ValueError): p.money(value)
    def test_decimal_precision(self):
        self.assertEqual(p.money('100.12'),Decimal('100.120000'))
        self.assertEqual(p.money('7.104461'),Decimal('7.104461'))
        self.assertEqual(p.money(Decimal('5.01')),Decimal('5.010000'))
    def test_trade_price_accepts_only_unambiguous_prices(self):
        for value,expected in [('5.01','5.010000'),('10','10.000000'),(' 10.50 ','10.500000'),('1000000.00','1000000.000000')]:
            with self.subTest(value=value):
                self.assertEqual(p.trade_price(value),Decimal(expected))
        for value in ('5.01000001','5.00999999','10.001','1e1','1_0','10,50','1000000.01',
                      '?id=1 AND 1=2',"1' OR '1'='1",'0','-10','','NaN','Infinity',None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.trade_price(value)
    def test_embed_limits(self):
        with self.assertRaises(ValueError): embeds('title','x'*6000)
        with self.assertRaises(ValueError): embeds('title','body','file:///private')

class PayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_refund_limit_includes_service_fee_and_invoice_tail(self):
        network=dict(withdrawFee='.01',withdrawIntegerMultiple='.000001',withdrawMin='1',withdrawMax='2000000')
        with patch.object(p,'withdrawal_network',AsyncMock(return_value=network)):
            fee,net=await p.payout_amount_quote(Decimal('1000002.009999'))
            self.assertEqual(net+fee,Decimal('1000002.009999'))
            with self.assertRaises(ValueError):
                await p.payout_amount_quote(Decimal('1000002.010000'))
        with self.assertRaises(ValueError): p.trade_price('1000000.01')

    async def test_previously_claimed_deposit_does_not_call_exchange(self):
        query=AsyncMock(side_effect=[{'state':'received','address':'address','amount':100}, {'txid':'recorded-transfer'}])
        request=AsyncMock()
        with patch.object(p,'require_ready',AsyncMock()),patch.object(p.db,'query',query),patch('utils.binance_api.make_api_request',request):
            self.assertEqual(await p.find_deposit('new:x','address',100),'recorded-transfer')
            request.assert_not_awaited()
    async def test_unknown_response_is_not_retried(self):
        state={}
        class Cursor:
            async def execute(self,sql,args):
                if 'payment_settings' in sql:
                    self.row={'value':''}
                elif sql.startswith('SELECT'): self.row=state.get(args[0])
                else:
                    key,request,address,gross,fee,net=args
                    state[key]={'state':'submitting','address':address,'gross':gross,'provider_id':None}
            async def fetchone(self): return self.row
        @asynccontextmanager
        async def tx(*args): yield Cursor()
        async def update(sql,args,**kw):
            state[args[-1]].update(state=args[0],provider_id=args[1])
        request=AsyncMock(return_value=None)
        with patch.object(p,'require_ready',AsyncMock()),patch.object(p.db,'transaction',tx),patch.object(p.db,'query',update),patch.object(p.payout_guard,'authorize',AsyncMock(return_value={})),patch('utils.binance_api.make_api_request',request),patch.dict(os.environ,NEW_WITHDRAW_AMOUNT_MODE='net'):
            first=await p.release('new:order','0x'+'a'*40,100,Decimal('.1'),Decimal('99.9'))
            second=await p.release('new:order','0x'+'a'*40,100,Decimal('.1'),Decimal('99.9'))
            self.assertFalse(first[0]);self.assertFalse(second[0])
            self.assertEqual(request.await_count,1)
            self.assertEqual(Decimal(request.await_args.args[2]['amount']),Decimal(100))
            self.assertEqual(request.await_args.args[2]['walletType'],'0')
            self.assertEqual(state['new:order']['state'],'unknown')
            with self.assertRaises(ValueError):
                await p.release('new:order','0x'+'b'*40,100,Decimal('.1'),Decimal('99.9'))
    async def test_database_failure_prevents_withdrawal(self):
        @asynccontextmanager
        async def failing(*args):
            raise RuntimeError('DB offline')
            yield
        request=AsyncMock()
        with patch.object(p,'require_ready',AsyncMock()),patch.object(p.db,'transaction',failing),patch('utils.binance_api.make_api_request',request),patch.dict(os.environ,NEW_WITHDRAW_AMOUNT_MODE='gross'):
            with self.assertRaises(RuntimeError): await p.release('new:x','0x'+'a'*40,100)
            request.assert_not_awaited()
    async def test_quote_works_without_manual_mode(self):
        result=[{'coin':'USDT','networkList':[{'network':'BSC','withdrawEnable':True,'withdrawFee':'.01','withdrawMin':'3','withdrawIntegerMultiple':'.00000001'}]}]
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=result)),patch.dict(os.environ,NEW_WITHDRAW_AMOUNT_MODE=''):
            fee,net=await p.payout_quote('0x'+'a'*40,Decimal('3.01'))
            self.assertEqual((fee,net),(Decimal('.01'),Decimal('3')))

class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_commands_rejected_before_dispatch(self):
        bot=IsolatedBot(intents=discord.Intents.none())
        response=NS(is_done=lambda:False,send_message=AsyncMock(),send_autocomplete_result=AsyncMock())
        interaction=NS(guild=None,response=response,type=discord.InteractionType.application_command)
        with patch('discord.ext.commands.Bot.process_application_commands',AsyncMock()) as dispatch:
            await bot.process_application_commands(interaction)
            dispatch.assert_not_awaited()
            response.send_message.assert_awaited_once()
            interaction.type=discord.InteractionType.auto_complete
            await bot.process_application_commands(interaction)
            response.send_autocomplete_result.assert_awaited_once_with([])
            interaction.guild=NS(id=2)
            await bot.process_application_commands(interaction)
            dispatch.assert_awaited_once()
        await bot.close()

    async def test_global_check_denies_all_dm_commands(self):
        for module in ('modules.social','modules.admin','modules.new_trading','unknown'):
            async def callback(): pass
            callback.__module__=module
            self.assertFalse(command_allowed(NS(guild=None,command=NS(callback=callback))))

    async def test_legacy_listener_does_not_receive_new_guild(self):
        bot=IsolatedBot(intents=discord.Intents.none())
        calls=[]
        async def legacy(obj): calls.append(obj)
        legacy.__module__='modules.rental'
        bot.add_listener(legacy,'on_interaction')
        listener=bot._event_handlers['on_interaction'][0]
        await listener(NS(guild=NS(id=2),data={}))
        await listener(NS(guild=None,data={'custom_id':'new:trade:x'}))
        self.assertEqual(calls,[])
        await listener(NS(guild=NS(id=1),data={}))
        self.assertEqual(len(calls),1)
        await bot.close()

class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_saves_old_and_new_before_cache_update(self):
        cog=object.__new__(NewMessageLog)
        cog.lock=asyncio.Lock()
        cog.tracked=AsyncMock(return_value={'kind':'forum'})
        cog.event=AsyncMock(return_value=True)
        old={'body':'price 100','author_id':44,'attachments':'[]'}
        query=AsyncMock(side_effect=[old,1])
        event=NS(guild_id=2,channel_id=8,message_id=9,data={'content':'price 120'})
        with patch('modules.new_message_log.db.query',query):
            await cog.on_raw_message_edit(event)
        saved=cog.event.await_args.args[-1]
        self.assertEqual(saved['before'],'price 100')
        self.assertEqual(saved['after'],'price 120')
        self.assertEqual(query.await_args_list[-1].args[1][0],'price 120')
    async def test_failed_event_write_keeps_original(self):
        cog=object.__new__(NewMessageLog)
        cog.lock=asyncio.Lock()
        cog.tracked=AsyncMock(return_value={'kind':'forum'})
        cog.event=AsyncMock(return_value=False)
        query=AsyncMock(return_value={'body':'evidence','author_id':44,'attachments':'[]'})
        with patch('modules.new_message_log.db.query',query):
            await cog.deleted(9,8)
        self.assertEqual(query.await_count,1)  # no removal when evidence cannot be saved
    async def test_embed_refresh_is_not_an_edit(self):
        cog=object.__new__(NewMessageLog)
        cog.event=AsyncMock()
        await cog.on_raw_message_edit(NS(guild_id=2,data={'embeds':[]}))
        cog.event.assert_not_awaited()
    async def test_capacity_limit_does_not_delete_to_make_room(self):
        cog=object.__new__(NewMessageLog)
        cog.bytes=3_000_000_000
        cog.warn_level=0
        cog.warn=AsyncMock()
        query=AsyncMock()
        with patch('modules.new_message_log.db.query',query):
            self.assertFalse(await cog.event(1,2,3,'delete',{'body':'x'}))
            query.assert_not_awaited()
            cog.warn.assert_awaited_once()

class InviteTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_join_does_not_guess_owner(self):
        cog=object.__new__(NewCommunity)
        cog.invite_lock=asyncio.Lock()
        cog.invites={'a':0,'b':0}
        cog.snapshot_invites=AsyncMock(return_value=[NS(code='a',uses=1),NS(code='b',uses=1)])
        query=AsyncMock()
        member=NS(guild=NS(id=2),bot=False,id=44,roles=[])
        with patch('modules.new_community.db.query',query):
            await cog.on_member_join(member)
        self.assertEqual(query.await_args.args[1],(44,None,None))

class OrderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rental_command_blocked_new_guild(self):
        def callback(): pass
        callback.__module__='modules.rental'
        self.assertFalse(command_allowed(NS(guild=NS(id=2),command=NS(callback=callback))))
        self.assertFalse(command_allowed(NS(guild=NS(id=1),command=NS(callback=callback))))
    async def test_cas_loser_writes_no_audit(self):
        calls=[]
        class Cursor:
            rowcount=0
            async def execute(self,*args): calls.append(args)
        @asynccontextmanager
        async def tx(): yield Cursor()
        cog=object.__new__(NewTrading)
        with patch('modules.new_trading.db.transaction',tx):
            with self.assertRaises(ValueError): await cog.transition('x','paid','shipped',1)
        self.assertEqual(len(calls),1)

class LoadTests(unittest.TestCase):
    def test_all_extensions_load_without_connecting(self):
        import subprocess
        import sys
        from pathlib import Path
        result = subprocess.run([sys.executable, '-m', 'tests.smoke_load_extensions'],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

class TradeCommandPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_checks_actor_role_guild_and_repeat_clicks(self):
        cog=object.__new__(NewTrading)
        cog.bot=NS()
        cog.confirm_review=AsyncMock()
        ctx=NS(author=NS(id=5))
        view=cog.review_confirmation(ctx,'order',{'code':'nonce'})
        def click(uid=5,gid=2,allowed=True):
            return NS(guild=NS(id=gid),user=NS(id=uid,guild_permissions=NS(administrator=allowed),roles=[]),
                      response=NS(send_message=AsyncMock()),edit_original_response=AsyncMock())
        for args in ((6,2,True),(5,1,True),(5,2,False)):
            await view.children[0].callback(click(*args))
        cog.confirm_review.assert_not_awaited()
        with patch('modules.new_trading.discord.ApplicationContext',return_value=ctx):
            await asyncio.gather(view.children[0].callback(click()),view.children[0].callback(click()))
        cog.confirm_review.assert_awaited_once_with(ctx,'order','nonce')

    async def test_management_callbacks_deny_ordinary_users_and_wrong_guild(self):
        from modules.new_trading import NewTrading
        cog=object.__new__(NewTrading)
        cases=[(NewTrading.review,dict(decision='无到账关闭',reason='test')),
               (NewTrading.manual_refund,dict(deposit_txid='x',withdrawal_id='x',refund_address='x',reason='test')),
               (NewTrading.confirm_review,dict(code='x')),
               (NewTrading.manage_refund_channel,dict(action='暂停关闭',reason='test'))]
        for guild,administrator in ((NS(id=2),False),(NS(id=1),True),(None,True)):
            for command,args in cases:
                ctx=NS(guild=guild,author=NS(guild_permissions=NS(administrator=administrator),roles=[]),respond=AsyncMock(),defer=AsyncMock())
                await getattr(command,'callback',command)(cog,ctx,order_id='order',**args)
                from utils.ui_language import text
                ctx.respond.assert_awaited_once_with(text(ctx,'legacy_006'),ephemeral=True)
                ctx.defer.assert_not_awaited()

    async def test_global_check_preserves_configured_admin_roles(self):
        from modules.new_trading import NewTrading
        from modules.new_community import NewCommunity
        ctx=NS(guild=NS(id=2),command=NewTrading.review,
               author=NS(guild_permissions=NS(administrator=False),roles=[]))
        with patch('config.NEW.TRADE_ADMIN_ROLES',[123]):
            self.assertFalse(command_allowed(ctx))
            ctx.author.roles=[NS(id=123)]
            self.assertTrue(command_allowed(ctx))
            ctx.guild=NS(id=1)
            self.assertFalse(command_allowed(ctx))

class RetirementAndModerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_ranking_does_not_start_or_cache(self):
        from modules.social import Social
        cog=object.__new__(Social)
        cog.bot=NS(wait_until_ready=AsyncMock())
        cog.sync_server_boosters=AsyncMock()
        cog.cache_invites=AsyncMock()
        cog.update_invite_leaderboard=AsyncMock()
        await cog.initialize_tasks()
        cog.cache_invites.assert_not_awaited()
        cog.update_invite_leaderboard.assert_not_awaited()
        cog.sync_server_boosters.assert_awaited_once()

    def message(self, guild_id=2, roles=()):
        from unittest.mock import MagicMock
        member=MagicMock(spec=discord.Member)
        member.id=42; member.bot=False; member.roles=[NS(id=r) for r in roles]
        return NS(id=9,guild=NS(id=guild_id),author=member,content='https://example.com',
                  delete=AsyncMock(),channel=NS(id=8,send=AsyncMock()))

    async def test_non_lv3_deleted_after_evidence(self):
        from modules.new_moderation import NewModeration
        calls=[]
        evidence=NS(on_message=AsyncMock(side_effect=lambda m:calls.append('evidence')))
        cog=NewModeration(NS(get_cog=lambda name:evidence,user=NS(id=99)))
        msg=self.message()
        msg.delete.side_effect=lambda **kw:calls.append('delete')
        with patch('modules.new_moderation.cfg.LV3_ROLE_IDS',(3,)),patch('modules.new_moderation.db.audit',AsyncMock()):
            await cog.moderate(msg)
        self.assertEqual(calls,['evidence','delete'])

    async def test_old_guild_and_lv3_not_deleted(self):
        from modules.new_moderation import NewModeration
        cog=NewModeration(NS())
        with patch('modules.new_moderation.cfg.LV3_ROLE_IDS',(3,)):
            for msg in (self.message(guild_id=1),self.message(roles=(3,))):
                await cog.moderate(msg)
                msg.delete.assert_not_awaited()

    async def test_link_flood_still_deleted_without_reply_amplification(self):
        from modules.new_moderation import NewModeration
        from utils.abuse_guard import TokenBucket
        cog=NewModeration(NS(get_cog=lambda name:None,user=NS(id=99)))
        cog.warning_users=TokenBucket(1,30,clock=lambda:0)
        cog.warning_budget=TokenBucket(5,30,max_keys=1,clock=lambda:0)
        msg=self.message()
        with patch('modules.new_moderation.cfg.LV3_ROLE_IDS',(3,)),patch('modules.new_moderation.db.audit',AsyncMock()):
            for _ in range(10): await cog.moderate(msg)
            self.assertEqual(msg.delete.await_count,10)
            self.assertEqual(msg.channel.send.await_count,1)
            for uid in range(100,110):
                msg.author.id=uid
                await cog.moderate(msg)
            self.assertEqual(msg.delete.await_count,20)
            self.assertEqual(msg.channel.send.await_count,5)

    async def test_everyone_role_cannot_bypass_link_restriction(self):
        from modules.new_moderation import NewModeration
        cog=NewModeration(NS(get_cog=lambda name:None,user=NS(id=99)))
        msg=self.message(roles=(2,))
        with patch('modules.new_moderation.cfg.LV3_ROLE_IDS',(2,)),patch('modules.new_moderation.db.audit',AsyncMock()):
            await cog.moderate(msg)
        msg.delete.assert_awaited_once()

    async def test_repeated_evidence_snapshot_does_not_exhaust_capacity(self):
        from datetime import datetime, timezone
        cog=object.__new__(NewMessageLog)
        cog.lock=asyncio.Lock(); cog.bytes=0
        cog.tracked=AsyncMock(return_value={'kind':'trade'})
        msg=self.message(); msg.created_at=datetime.now(timezone.utc); msg.attachments=[]
        query=AsyncMock(side_effect=[1,0,0,0])
        with patch('modules.new_message_log.db.query',query):
            await cog.on_message(msg)
            size=cog.bytes
            for _ in range(3): await cog.on_message(msg)
        self.assertGreater(size,0)
        self.assertEqual(cog.bytes,size)

    async def test_uncached_edit_checked_after_logging(self):
        cog=object.__new__(NewMessageLog)
        calls=[]
        cog.record_edit=AsyncMock(side_effect=lambda p:calls.append('edit evidence'))
        moderation=NS(check_edit=AsyncMock(side_effect=lambda p:calls.append('moderation')))
        cog.bot=NS(get_cog=lambda name:moderation)
        await cog.on_raw_message_edit(NS())
        self.assertEqual(calls,['edit evidence','moderation'])

    def test_link_detection(self):
        from modules.new_moderation import contains_link
        for text in ('[shop](https://example.com)', 'www.example.com', 'discord.gg/test', 'HTTPS://EXAMPLE.COM'):
            self.assertTrue(contains_link(text))
        self.assertFalse(contains_link('price 2.00 USDT'))

if __name__=='__main__': unittest.main()

