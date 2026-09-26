import os
os.environ.update(DISCORD_TOKEN='offline-test', GUILD_ID='1', NEW_GUILD_ID='2',
                  BINANCE_API_KEY='offline-test', BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import discord
from modules.new_trading import NewTrading


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_deposit_wait_timer_survives_restart_and_limits_alerts(self):
        from utils.shared_payments import DepositNotReady
        stored={}
        async def setting(key,value=None):
            if value is not None: stored[key]=dict(value)
            return stored.get(key)
        self.cog.alert=AsyncMock()
        with patch('modules.new_trading.db.setting',setting),patch('modules.new_trading.time.time',return_value=1000) as clock:
            await self.cog.deposit_status_notice(self.row,DepositNotReady(0))
            self.cog.alert.assert_not_awaited()
            clock.return_value=1899
            # New cog has no in-memory timer; it must reuse the saved first observation.
            restarted=object.__new__(NewTrading)
            restarted.alert=self.cog.alert
            await restarted.deposit_status_notice(self.row,DepositNotReady(6))
            self.cog.alert.assert_not_awaited()
            clock.return_value=1900
            await restarted.deposit_status_notice(self.row,DepositNotReady(6))
            self.cog.alert.assert_awaited_once()
            clock.return_value=1960
            await restarted.deposit_status_notice(self.row,DepositNotReady(6))
            self.cog.alert.assert_awaited_once()
            clock.return_value=5500
            await restarted.deposit_status_notice(self.row,DepositNotReady(6))
            self.assertEqual(self.cog.alert.await_count,2)

    async def test_abnormal_deposit_alert_includes_status_immediately(self):
        from utils.shared_payments import DepositNotReady
        self.cog.alert=AsyncMock()
        with patch('modules.new_trading.db.setting',AsyncMock(return_value=None)):
            await self.cog.deposit_status_notice(self.row,DepositNotReady(7))
        self.cog.alert.assert_awaited_once()
        self.assertIn('状态 7',self.cog.alert.await_args.args[0])

    async def test_pending_deposit_worker_cannot_mark_paid_or_timeout(self):
        from utils.shared_payments import DepositNotReady
        self.cog.bot.get_guild=lambda _:NS(id=2)
        self.cog.access_ready=True
        for method in ('check_closed_payments','retry_forums','idle_worker','recover_panels',
                       'manual_close_worker','cleanup_finished','mark_paid','timeout_channel',
                       'transition','deposit_status_notice'):
            setattr(self.cog,method,AsyncMock())
        self.row['status']='paying'
        inv=dict(state='waiting',address='address',amount=7,expires_at=datetime.utcnow()-timedelta(hours=1))
        for status in (0,6):
            with patch('modules.new_trading.cfg.PAYMENTS_ENABLED',True),patch('modules.new_trading.db.query',AsyncMock(side_effect=[[self.row],inv])),patch('modules.new_trading.payments.find_deposit',AsyncMock(side_effect=DepositNotReady(status))):
                await NewTrading.worker.coro(self.cog)
        self.cog.mark_paid.assert_not_awaited()
        self.cog.timeout_channel.assert_not_awaited()
        self.cog.transition.assert_not_awaited()
        self.cog.recovery_alert.assert_not_awaited()
        self.assertEqual(self.cog.deposit_status_notice.await_count,2)

    def setUp(self):
        self.cog=object.__new__(NewTrading)
        self.channel=NS(id=8,guild=NS(id=2),send=AsyncMock(),delete=AsyncMock())
        self.cog.bot=NS(get_channel=lambda _:self.channel,fetch_channel=AsyncMock(),user=NS(id=99))
        self.cog.cleanup_lock=asyncio.Lock()
        self.cog.post_lock=asyncio.Lock()
        self.row={'id':'order','channel_id':8,'status':'pending','buyer_id':1,'seller_id':2,'initiator_id':1,
                  'created_at':datetime.utcnow()-timedelta(hours=1)}
        self.cog.order=AsyncMock(return_value=self.row)
        self.cog.post=AsyncMock()
        self.cog.recovery_alert=AsyncMock()

    async def test_expired_invoice_recovery_does_not_replace_or_publish_it(self):
        inv={'address':'original','state':'waiting','expires_at':datetime.utcnow()-timedelta(minutes=1)}
        self.cog.transition=AsyncMock()
        with patch('modules.new_trading.db.query',AsyncMock(return_value=inv)),patch('modules.new_trading.payments.invoice',AsyncMock()) as allocate,patch('modules.new_trading.binance_api.get_deposit_address',AsyncMock()) as address:
            await self.cog.prepare_invoice({'id':'order'})
        allocate.assert_not_awaited(); address.assert_not_awaited()
        self.cog.transition.assert_awaited_once_with('order','invoicing','paying',99)
        self.cog.post.assert_not_awaited()

    async def test_late_warning_gives_full_five_minutes_before_expiry(self):
        stored={}
        async def setting(key,value=None):
            if value is not None: stored[key]=dict(value)
            return stored.get(key)
        with patch('modules.new_trading.db.query',AsyncMock(return_value=[self.row])),patch('modules.new_trading.db.setting',setting),patch('modules.new_trading.trade_idle.expire',AsyncMock()) as expire:
            await self.cog.idle_worker()
        expire.assert_not_awaited()
        self.channel.send.assert_awaited_once()
        timer=stored['idle_timer:order:pending']
        self.assertTrue(timer['warned'])
        self.assertGreater(timer['deadline'],datetime.now().timestamp()+295)

    async def test_missing_preinvoice_channel_can_expire(self):
        self.cog.resolve_trade_channel=AsyncMock(return_value=None)
        with patch('modules.new_trading.db.query',AsyncMock(return_value=[self.row])),patch('modules.new_trading.db.setting',AsyncMock(return_value={'deadline':0})),patch('modules.new_trading.trade_idle.expire',AsyncMock(return_value=True)) as expire:
            await self.cog.idle_worker()
        expire.assert_awaited_once()

    async def test_forbidden_channel_is_not_treated_as_deleted(self):
        self.cog.bot.get_channel=lambda _:None
        self.cog.bot.fetch_channel.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),{'message':'denied','code':50013})
        with patch('modules.new_trading.db.query',AsyncMock(return_value=[self.row])),patch('modules.new_trading.db.setting',AsyncMock(return_value={'deadline':0})),patch('modules.new_trading.trade_idle.expire',AsyncMock()) as expire:
            await self.cog.idle_worker()
        expire.assert_not_awaited()
        self.cog.recovery_alert.assert_awaited_once()

    async def test_failed_notification_retries_without_marking_delivered(self):
        self.cog.notify_step=AsyncMock(side_effect=[RuntimeError('network'),None])
        with patch('modules.new_trading.db.setting',AsyncMock(return_value=None)) as setting:
            with self.assertRaises(RuntimeError): await self.cog.deliver_step_notification(self.channel,self.row)
            self.assertEqual(setting.await_count,1)
            await self.cog.deliver_step_notification(self.channel,self.row)
            self.assertEqual(setting.await_args.args[1],{'status':'pending'})
        self.assertEqual(self.cog.notify_step.await_count,2)

    async def test_cleanup_rechecks_hold_under_shared_lock(self):
        self.row['status']='expired'
        async def query(sql,args=(),**kw):
            if 'JOIN tracked_channels' in sql: return [self.row]
            self.assertTrue(self.cog.cleanup_lock.locked())
            return {'hold':True}
        with patch('modules.new_trading.db.query',query),patch('modules.new_trading.db.setting',AsyncMock(return_value=None)):
            await self.cog.cleanup_finished()
        self.channel.delete.assert_not_awaited()

    async def test_one_cleanup_failure_does_not_skip_next_order(self):
        self.row['status']='expired'
        self.cog.order=AsyncMock(side_effect=[self.row,{**self.row,'id':'next'}])
        self.channel.delete.side_effect=[RuntimeError('denied'),None]
        async def setting(key,value=None):
            if key.startswith(('trade_panel:','trade_notification:')): return {'status':'expired'}
        async def query(sql,args=(),**kw):
            return [self.row,{**self.row,'id':'next'}] if 'JOIN tracked_channels' in sql else {'hold':False}
        with patch('modules.new_trading.db.query',query),patch('modules.new_trading.db.setting',setting),patch('modules.new_trading.db.audit',AsyncMock()):
            await self.cog.cleanup_finished()
        self.assertEqual(self.channel.delete.await_count,2)

    async def test_missing_payment_channel_becomes_review_even_if_held(self):
        self.row['status']='payment_timeout'
        self.cog.resolve_trade_channel=AsyncMock(return_value=None)
        self.cog.transition=AsyncMock(); self.cog.alert=AsyncMock()
        with patch('modules.new_trading.db.query',AsyncMock(return_value={'hold':True})):
            await self.cog.timeout_channel(self.row)
        self.assertEqual(self.cog.transition.await_args.args[2],'payment_review')

    async def test_confirmed_timer_starts_from_confirmation_not_creation(self):
        self.row.update(status='confirmed',confirmed_at=datetime.utcnow())
        with patch('modules.new_trading.db.query',AsyncMock(return_value=[self.row])),patch('modules.new_trading.db.setting',AsyncMock(return_value=None)) as setting,patch('modules.new_trading.trade_idle.expire',AsyncMock()) as expire:
            await self.cog.idle_worker()
        deadline=setting.await_args.args[1]['deadline']
        self.assertAlmostEqual(deadline,datetime.now().timestamp()+900,delta=3)
        self.channel.send.assert_not_awaited(); expire.assert_not_awaited()

    async def test_financial_scan_runs_before_failing_cleanup(self):
        self.cog.bot.get_guild=lambda _:NS(id=2)
        self.cog.access_ready=True
        self.cog.check_closed_payments=AsyncMock()
        self.cog.retry_forums=AsyncMock()
        self.cog.idle_worker=AsyncMock()
        self.cog.recover_panels=AsyncMock()
        self.cog.manual_close_worker=AsyncMock()
        query=AsyncMock(return_value=[])
        async def broken_cleanup():
            self.assertTrue(any("SELECT * FROM orders WHERE status IN" in c.args[0] for c in query.await_args_list))
            raise RuntimeError('cleanup unavailable')
        self.cog.cleanup_finished=broken_cleanup
        with patch('modules.new_trading.cfg.PAYMENTS_ENABLED',True),patch('modules.new_trading.db.query',query):
            await NewTrading.worker.coro(self.cog)
        self.cog.manual_close_worker.assert_awaited_once()

