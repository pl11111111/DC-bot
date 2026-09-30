"""Abuse races against disposable localhost MySQL; no Discord/payment requests."""
from tests.mysql_integration import bootstrap
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from utils import new_store as db, trade_admission as admission
from modules.new_trading import NewTrading, OrderStateChanged
from modules.new_message_log import NewMessageLog


async def create(ident, initiator, other, channel=None):
    async with db.transaction() as cur:
        await admission.reserve(cur,initiator,other)
        await cur.execute('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,channel_id,item,terms,amount,fee) VALUES(%s,%s,%s,%s,%s,%s,%s,5.01,2)',
                          (ident,initiator,other,initiator,channel,'item',''))
    return ident


async def reset():
    for table in ('orders','settings','audit'):
        await db.query('DELETE FROM '+table)


async def now():
    return int((await db.query('SELECT UNIX_TIMESTAMP() AS now',one=True))['now'])


async def close_pools():
    for pool in db._pools.values(): pool.close()
    for pool in db._pools.values(): await pool.wait_closed()
    db._pools.clear()


async def main():
    cog=object.__new__(NewTrading)
    try:
        await reset()
        results=await asyncio.gather(*(create('duplicate'+str(i),1,100+i) for i in range(25)),return_exceptions=True)
        success=[r for r in results if isinstance(r,str)]
        assert len(success)==1,results
        assert all(isinstance(r,admission.AdmissionDenied) for r in results if not isinstance(r,str))
        print('PASS 25 simultaneous creations by one user reserve exactly one slot')
        await cog.transition(success[0],'pending','cancelled',1)
        await close_pools()  # Fresh connections; no in-process limiter state is involved.
        try: await create('after_cancel',1,200)
        except admission.AdmissionDenied: pass
        else: raise AssertionError('Cancellation/reconnection reset durable cooldown')
        print('PASS cancellation and new connections cannot reset durable cooldown')

        stamp=await now()
        await db.setting('trade_rate:1',[stamp-300,stamp-200,stamp-100])
        with patch.object(admission.cfg,'TRADE_CREATE_HOURLY',3):
            try: await create('hourly',1,200)
            except admission.AdmissionDenied as exc: assert 'hourly' in str(exc)
            else: raise AssertionError('Hourly quota bypassed')
        print('PASS hourly history still limits creation after cancelled orders')

        await reset()
        with patch.object(admission.cfg,'TRADE_MAX_CHANNELS',3):
            results=await asyncio.gather(*(create('capacity'+str(i),100+i,200+i,channel=1000+i) for i in range(12)),return_exceptions=True)
            success=[r for r in results if isinstance(r,str)]
            assert len(success)==3,results
            for ident in success: await cog.transition(ident,'pending','cancelled',100)
            try: await create('capacity_extra',999,998)
            except admission.AdmissionDenied as exc: assert 'capacity' in str(exc)
            else: raise AssertionError('Undeleted cancelled channels released capacity')
            await db.setting('channel_deleted:'+success[0],True)
            await create('capacity_after_delete',999,998)
        print('PASS concurrent global capacity includes cancelled channels until deletion')

        await reset()
        with patch.object(admission.cfg,'TRADE_MAX_CHANNELS',1):
            await create('reserved_no_channel',1,2)
            try: await create('second_no_channel',3,4)
            except admission.AdmissionDenied: pass
            else: raise AssertionError('Pending channel reservation was not counted')
        print('PASS reserved orders count before Discord channel creation completes')

        await reset()
        results=await asyncio.gather(*(create('incoming'+str(i),100+i,1) for i in range(25)),return_exceptions=True)
        success=[r for r in results if isinstance(r,str)]
        assert len(success)==admission.cfg.TRADE_MAX_INCOMING,results
        with patch.object(admission.cfg,'MAX_ACTIVE',1):
            await create('victim_own',1,500)
            try: await cog.transition(success[0],'pending','confirmed',1)
            except ValueError: pass
            else: raise AssertionError('Acceptance bypassed active trade quota')
        print('PASS hostile incoming requests are bounded and do not consume own creation slots')

        await cog.transition('victim_own','pending','cancelled',1)
        with patch.object(admission.cfg,'MAX_ACTIVE',1):
            results=await asyncio.gather(*(cog.transition(ident,'pending','confirmed',1) for ident in success),return_exceptions=True)
            assert sum(r is None for r in results)==1,results
            row=await db.query("SELECT COUNT(*) n FROM orders WHERE status='confirmed'",one=True)
            assert row['n']==1
        print('PASS concurrent invitation acceptance cannot exceed active trade quota')

        await reset()
        await create('binding',1,2,channel=700)
        for actor in (1,999):
            try: await cog.transition('binding','pending','confirmed',actor)
            except admission.AdmissionDenied: pass
            else: raise AssertionError('Uninvited actor confirmed order')
        await cog.transition('binding','pending','confirmed',2)
        try: await cog.transition('binding','pending','confirmed',2)
        except OrderStateChanged: pass
        else: raise AssertionError('Duplicate confirmation accepted')
        audits=await db.query("SELECT COUNT(*) n FROM audit WHERE action='confirmed'",one=True)
        assert audits['n']==1
        print('PASS initiator/outsider rejected and confirmation recorded exactly once')

        assert not await db.query('SELECT user_id FROM balances WHERE user_id=90101',one=True)
        await create('new_buyer',90101,90102,channel=701)
        await cog.transition('new_buyer','pending','confirmed',90102)
        cog.prepare_invoice=AsyncMock()
        inter=NS(data={'custom_id':'new:trade:pay:new_buyer'},guild=NS(id=2),channel_id=701,
                 user=NS(id=90101,roles=[]),response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch('modules.new_trading.payments.payout_amount_quote',AsyncMock()):
            await cog.on_interaction(inter)
        assert (await cog.order('new_buyer'))['status']=='invoicing'
        assert (await db.query('SELECT available,reserved FROM balances WHERE user_id=90101',one=True))==dict(available=0,reserved=0)
        cog.prepare_invoice.assert_awaited_once()
        print('PASS first-time zero-credit buyer can request payment after guarded creation')

        await reset()
        with patch.object(admission.cfg,'TRADE_MAX_CHANNELS',100):
            results=await asyncio.gather(*(create('global'+str(i),100+i,200+i) for i in range(30)),return_exceptions=True)
            assert sum(isinstance(r,str) for r in results)==10,results
        print('PASS rotating accounts share durable ten-per-minute creation budget')

        await reset()
        try:
            async with db.transaction() as cur:
                await admission.reserve(cur,1,2)
                raise RuntimeError('Simulated insertion failure')
        except RuntimeError: pass
        assert not await db.query('SELECT * FROM settings')
        await create('rollback_retry',1,2)
        assert len(await db.setting('trade_rate:1'))==1
        print('PASS failed creation rolls back quota history with the order')

        await db.query("UPDATE orders SET status='paid' WHERE id='rollback_retry'")
        with patch.object(admission.cfg,'MAX_ACTIVE',1),patch.object(admission.cfg,'TRADE_MAX_CHANNELS',1):
            await cog.transition('rollback_retry','paid','shipped',2)
        assert (await cog.order('rollback_retry'))['status']=='shipped'
        print('PASS creation limits do not prevent existing paid order progression')

        logger=object.__new__(NewMessageLog)
        logger.lock=asyncio.Lock(); logger.bytes=0
        await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(501,'trade')")
        message=NS(id=901,guild=NS(id=2),author=NS(id=1,bot=False),channel=NS(id=501),
                   content='Preserve this evidence',attachments=[],created_at=datetime.now(timezone.utc))
        await logger.on_message(message)
        original=logger.bytes
        for _ in range(10): await logger.on_message(message)
        assert logger.bytes==original and original>0
        assert (await db.query('SELECT COUNT(*) n FROM messages WHERE message_id=901',one=True))['n']==1
        print('PASS duplicate moderation evidence neither duplicates rows nor inflates storage accounting')
    finally:
        await close_pools()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
