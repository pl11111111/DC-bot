"""Participant notifications and retained settled-channel closure boundaries."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import unittest
from modules.new_trading import NewTrading
from utils import trade_language as language,ui_language as ui,trade_channel_close as close


class NoticeTests(unittest.TestCase):
    def row(self,status,buyer='中文',seller='日本語'):
        return dict(id='order',status=status,buyer_id=1,seller_id=2,initiator_id=1,
                    _languages=list(dict.fromkeys((buyer,seller))),_user_languages={'buyer_id':buyer,'seller_id':seller})

    def test_every_nonpending_stage_has_explicit_both_party_text_in_all_languages(self):
        stages=('confirmed','invoicing','paying','waiting','paid','shipped','receipt_confirmed','releasing',
                'completed','cancelled','expired','payment_timeout','payment_review','disputed','refund_ready',
                'releasing_refund','refunded','manual_refunded','test_closed')
        for lang,pack in language.PACKS.items():
            for status in stages:
                with self.subTest(language=lang,status=status):
                    self.assertEqual(set(pack['participants'][status]),{'buyer','seller'})
                    for value in pack['participants'][status].values():self.assertTrue(value.strip())
                    text,_=language.notify(self.row(status,lang,lang))
                    self.assertLessEqual(len(text),2000)

    def test_terminal_notice_contains_closure_for_both_languages_and_only_two_mentions(self):
        for status in ('completed','refunded','cancelled','test_closed'):
            text,mentions=language.notify(self.row(status))
            self.assertIn(language.PACKS['中文']['closure_notice'],text)
            self.assertIn(language.PACKS['日本語']['closure_notice'],text)
            self.assertEqual(mentions.to_dict()['users'],[1,2])
            self.assertNotIn('everyone',mentions.to_dict()['parse'])

    def test_waiting_seller_exact_requested_text(self):
        self.assertEqual(language.PACKS['中文']['participants']['waiting']['seller'],
            '系统已检测到匹配入款，正在等待支付平台确认，请继续等待。系统显示「支付已确认」并发出发货通知前，请勿交付商品。')

    def test_manual_and_idle_expiration_do_not_inherit_five_minute_closure(self):
        for status in ('expired','manual_refunded'):
            text,_=language.notify(self.row(status))
            self.assertNotIn(language.PACKS['中文']['closure_notice'],text)
            self.assertNotIn(language.PACKS['日本語']['closure_notice'],text)
        text,_=language.notify(self.row('manual_refunded'))
        self.assertIn(ui.text('中文','manual_buyer'),text)


class CloseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog=object.__new__(NewTrading);self.cog.cleanup_lock=asyncio.Lock()
        self.row=dict(id='order',status='completed',buyer_id=1,seller_id=2,channel_id=8,
                      closed_at=datetime.utcnow()-timedelta(hours=1),_languages=['English'],
                      _user_languages={'buyer_id':'English','seller_id':'English'})
        self.state=dict(phase='queued',generation='g',channel=8,status='completed',reason='Verified',actor=9)
        self.channel=NS(id=8,guild=NS(id=2),send=AsyncMock(),delete=AsyncMock())
        self.cog.order=AsyncMock(return_value=self.row)
        self.cog.bot=NS(user=NS(id=99))
        self.cog.resolve_trade_channel=AsyncMock(return_value=self.channel)

    async def test_failed_notification_keeps_hold_and_does_not_start_timer(self):
        self.channel.send.side_effect=RuntimeError('offline')
        with patch('modules.new_trading.db.setting',AsyncMock(return_value=self.state)),patch('modules.new_trading.db.query',AsyncMock(return_value={'hold':True})),patch('modules.new_trading.ui_participants',AsyncMock(return_value=self.row)),patch.object(close,'activate',AsyncMock()) as activate:
            with self.assertRaises(RuntimeError):await self.cog.deliver_channel_close_notice('order')
            activate.assert_not_awaited()
        self.channel.delete.assert_not_awaited()

    async def test_notice_precedes_timer_and_honors_two_languages(self):
        row=dict(self.row,_languages=['中文','日本語'],_user_languages={'buyer_id':'中文','seller_id':'日本語'})
        async def activated(*args):self.channel.send.assert_awaited_once()
        with patch('modules.new_trading.db.setting',AsyncMock(return_value=self.state)),patch('modules.new_trading.db.query',AsyncMock(return_value={'hold':True})),patch('modules.new_trading.ui_participants',AsyncMock(return_value=row)),patch.object(close,'activate',AsyncMock(side_effect=activated)) as activate:
            await self.cog.deliver_channel_close_notice('order')
            activate.assert_awaited_once_with('order','g')
        content=self.channel.send.await_args.args[0]
        self.assertIn(ui.text('中文','channel_close_notice',reason='Verified'),content)
        self.assertIn(ui.text('日本語','channel_close_notice',reason='Verified'),content)
        self.assertEqual(self.channel.send.await_args.kwargs['allowed_mentions'].to_dict()['users'],[1,2])
        self.assertEqual(self.channel.send.await_args.kwargs['view'].children[0].custom_id,'new:trade:keep:order')

    async def test_cancelled_queue_or_changed_state_never_enables_closure(self):
        for phase,status,held in (('cancelled','completed',True),('waiting','completed',False),
                                  ('queued','disputed',True),('queued','completed',False)):
            with patch('modules.new_trading.db.setting',AsyncMock(return_value=dict(self.state,phase=phase))),patch('modules.new_trading.db.query',AsyncMock(return_value={'hold':held})),patch.object(close,'activate',AsyncMock()) as activate:
                self.cog.order.return_value=dict(self.row,status=status)
                await self.cog.deliver_channel_close_notice('order')
                activate.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_cleanup_respects_new_countdown_without_changing_order_end_time(self):
        for state in (dict(self.state),dict(self.state,phase='waiting',announced_at=__import__('time').time())):
            async def setting(key,*args):
                if key=='channel_close:order':return state
                if key.startswith('trade_'):return dict(status='completed')
                return None
            with patch('modules.new_trading.db.query',AsyncMock(return_value=[self.row])),patch('modules.new_trading.db.setting',AsyncMock(side_effect=setting)):
                await self.cog.cleanup_finished()
            self.channel.delete.assert_not_awaited()
            self.cog.resolve_trade_channel.assert_not_awaited()

    async def test_cleanup_deletes_after_new_countdown_even_when_payments_paused(self):
        async def setting(key,*args):
            if key=='channel_close:order':return dict(self.state,phase='waiting',announced_at=1000)
            if key.startswith('trade_'):return dict(status='completed')
            return None
        async def query(sql,*args,**kwargs):return {'hold':False} if sql.startswith('SELECT hold') else [self.row]
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=query)),patch('modules.new_trading.db.setting',AsyncMock(side_effect=setting)),patch('modules.new_trading.db.audit',AsyncMock()),patch('modules.new_trading.time.time',return_value=1299):
            await self.cog.cleanup_finished(only_admin=True)
            self.channel.delete.assert_not_awaited()
        with patch('modules.new_trading.db.query',AsyncMock(side_effect=query)),patch('modules.new_trading.db.setting',AsyncMock(side_effect=setting)),patch('modules.new_trading.db.audit',AsyncMock()),patch('modules.new_trading.time.time',return_value=1301):
            await self.cog.cleanup_finished(only_admin=True)
        self.channel.delete.assert_awaited_once()

    async def test_paused_payment_worker_still_retries_admin_closure_without_money_calls(self):
        self.cog.bot=NS(get_guild=lambda _:NS(id=2))
        self.cog.retry_forums=AsyncMock(side_effect=RuntimeError('forum offline'))
        self.cog.manual_close_worker=AsyncMock()
        self.cog.retry_channel_closures=AsyncMock()
        self.cog.cleanup_finished=AsyncMock()
        self.cog.check_closed_payments=AsyncMock()
        with patch('modules.new_trading.cfg.PAYMENTS_ENABLED',False),patch('modules.new_trading.log.exception'):
            await NewTrading.worker.coro(self.cog)
        self.cog.retry_channel_closures.assert_awaited_once()
        self.cog.cleanup_finished.assert_awaited_once_with(only_admin=True)
        self.cog.check_closed_payments.assert_not_awaited()

    async def test_command_permission_and_actor_bound_single_use_confirmation(self):
        def member(uid,allowed):return NS(id=uid,roles=[],guild_permissions=NS(administrator=allowed))
        guild=NS(id=2)
        ctx=NS(guild=guild,author=member(9,True),respond=AsyncMock(),defer=AsyncMock(),followup=NS(send=AsyncMock()))
        self.cog.deliver_channel_close_notice=AsyncMock()
        data=dict(code='code',reason='Reviewed')
        with patch.object(close,'prepare',AsyncMock(return_value=data)) as prepare,patch.object(close,'confirm',AsyncMock()) as confirm:
            bad=NS(guild=guild,author=member(3,False),respond=AsyncMock())
            await NewTrading.close_settled_channel.callback(self.cog,bad,'order','Reason')
            prepare.assert_not_awaited()
            await NewTrading.close_settled_channel.callback(self.cog,ctx,'order','Reason')
            button=ctx.followup.send.await_args.kwargs['view'].children[0]
            def interaction(uid=9,allowed=True,guild_id=2):
                return NS(guild=NS(id=guild_id),user=member(uid,allowed),response=NS(send_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()))
            for wrong in (interaction(10),interaction(9,False),interaction(9,True,3)):
                await button.callback(wrong)
                confirm.assert_not_awaited()
            await button.callback(interaction());await button.callback(interaction())
            confirm.assert_awaited_once_with('order',9,'code')
            self.cog.deliver_channel_close_notice.assert_awaited_once_with('order')

    async def test_preview_failure_and_persistence_failure_do_not_schedule_close(self):
        ctx=NS(guild=NS(id=2),author=NS(id=9,roles=[],guild_permissions=NS(administrator=True)),respond=AsyncMock(),defer=AsyncMock(),followup=NS(send=AsyncMock()))
        self.cog.deliver_channel_close_notice=AsyncMock()
        with patch.object(close,'prepare',AsyncMock(side_effect=close.CloseRejected('channel_close_unavailable'))):
            await NewTrading.close_settled_channel.callback(self.cog,ctx,'order','Reason')
            self.assertNotIn('view',ctx.followup.send.await_args.kwargs)
        with patch.object(close,'prepare',AsyncMock(return_value=dict(code='code',reason='Reason'))),patch.object(close,'confirm',AsyncMock(side_effect=RuntimeError('db error'))),patch('modules.new_trading.log.exception'):
            await NewTrading.close_settled_channel.callback(self.cog,ctx,'order','Reason')
            button=ctx.followup.send.await_args.kwargs['view'].children[0]
            inter=NS(guild=ctx.guild,user=ctx.author,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            await button.callback(inter)
            self.cog.deliver_channel_close_notice.assert_not_awaited()
