"""Local disposable database only; no Discord or payment network calls."""
from tests.mysql_integration import bootstrap
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from utils import new_store as db, trade_idle
from modules.new_trading import NewTrading, OrderStateChanged


async def main():
    cog=object.__new__(NewTrading)
    cog.bot=NS(user=NS(id=99))
    async def create(ident,status='pending',credit=0):
        await db.query('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee,status,credits) VALUES(%s,1,2,1,%s,%s,5.01,2,%s,%s)',(ident,'item','',status,credit))
        return await cog.order(ident)
    deadline=datetime.utcnow()-timedelta(seconds=1)
    try:
        await create('idle')
        results=await asyncio.gather(trade_idle.expire('idle',99,'pending',deadline),trade_idle.expire('idle',99,'pending',deadline))
        assert sorted(results)==[False,True]
        assert (await cog.order('idle'))['status']=='expired'
        print('PASS concurrent expiry commits exactly once')

        await create('race')
        results=await asyncio.gather(trade_idle.expire('race',99,'pending',deadline),cog.transition('race','pending','confirmed',2),return_exceptions=True)
        status=(await cog.order('race'))['status']
        assert status in ('confirmed','expired')
        assert (status=='confirmed' and results[0] is False) or (status=='expired' and isinstance(results[1],OrderStateChanged))
        print('PASS confirmation versus expiry cannot overwrite winner')

        await create('has_invoice')
        await db.query("INSERT INTO invoices(order_key,address,amount,expires_at) VALUES('new:has_invoice','address',7.011234,UTC_TIMESTAMP())",shared=True)
        try: await trade_idle.expire('has_invoice',99,'pending',deadline)
        except ValueError: pass
        else: raise AssertionError('Invoice must block prepayment expiry')
        assert (await cog.order('has_invoice'))['status']=='pending'
        print('PASS existing financial evidence blocks idle cancellation')

        row=await create('credits','paying',2)
        await db.query('INSERT INTO balances(user_id,available,reserved) VALUES(1,0,1)')
        try: await cog.mark_paid(row,'tx')
        except ValueError: pass
        else: raise AssertionError('Insufficient reservation must roll back')
        assert (await cog.order('credits'))['status']=='paying'
        assert not await db.query("SELECT id FROM audit WHERE order_id='credits' AND action='paid'")
        await db.query('UPDATE balances SET reserved=2 WHERE user_id=1')
        results=await asyncio.gather(cog.mark_paid(row,'tx'),cog.mark_paid(row,'tx'))
        assert sorted(results)==[False,True]
        assert (await db.query('SELECT reserved FROM balances WHERE user_id=1',one=True))['reserved']==Decimal(0)
        assert (await cog.order('credits'))['status']=='paid'
        print('PASS paid/credit/audit rollback together and duplicate payment consumes credit once')
    finally:
        for pool in db._pools.values(): pool.close()
        for pool in db._pools.values(): await pool.wait_closed()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
