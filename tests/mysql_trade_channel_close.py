"""Local disposable MySQL races; closure must never mutate money or order outcome."""
from tests.mysql_integration import bootstrap
import asyncio
from decimal import Decimal
from utils import new_store as db,trade_channel_close as close


async def insert(ident,status='completed',held=True):
    await db.query('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,channel_id,item,terms,amount,fee,status,closed_at) VALUES(%s,1,2,1,%s,%s,%s,10,2,%s,UTC_TIMESTAMP()-INTERVAL 1 HOUR)',(ident,100+int(ident.split('_')[-1]),'Item','',status))
    row=await db.query('SELECT * FROM orders WHERE id=%s',(ident,),one=True)
    await db.query("INSERT INTO tracked_channels(channel_id,kind,hold) VALUES(%s,'trade',%s)",(row['channel_id'],held))
    return row


async def rejected(awaitable):
    try:await awaitable
    except close.CloseRejected:return
    raise AssertionError('Unsafe closure accepted')


async def main():
    try:
        for index,status in enumerate(('pending','paid','receipt_confirmed','releasing','releasing_refund','disputed','payment_review','cancelled','expired','manual_refunded')):
            ident='close_'+str(index)
            await insert(ident,status)
            await rejected(close.prepare(ident,9,'Reviewed'))
        await insert('close_10',held=False)
        await rejected(close.prepare('close_10',9,'Reviewed'))
        await insert('close_11')
        await db.setting('channel_deleted:close_11',{'time':1})
        await rejected(close.prepare('close_11',9,'Reviewed'))
        print('PASS unsettled, disputed, deleted and nonretained channels cannot use settled closure')

        row=await insert('close_12')
        await db.query("INSERT INTO payouts(order_key,request_id,address,gross,fee,net,state,provider_id) VALUES('new:close_12','test-request','0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',10,.1,9.9,'completed','verified')",shared=True)
        payout=await db.query("SELECT * FROM payouts WHERE order_key='new:close_12'",shared=True,one=True)
        data=await close.prepare('close_12',9,"Reviewed'; DROP TABLE orders; --")
        await rejected(close.confirm('close_12',8,data['code']))
        await rejected(close.confirm('close_12',9,'wrong'))
        results=await asyncio.gather(close.confirm('close_12',9,data['code']),close.confirm('close_12',9,data['code']),return_exceptions=True)
        assert sum(isinstance(value,dict) for value in results)==1
        assert (await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True))['hold']
        assert (await db.setting('channel_close:close_12'))['phase']=='queued'
        print('PASS actor-bound concurrent confirmation queues exactly once and retains channel before notice')

        state=await db.setting('channel_close:close_12')
        await close.activate('close_12',state['generation'])
        await rejected(close.activate('close_12',state['generation']))
        assert (await db.query('SELECT * FROM orders WHERE id=%s',('close_12',),one=True))==row
        assert (await db.query("SELECT * FROM payouts WHERE order_key='new:close_12'",shared=True,one=True))==payout
        assert not (await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True))['hold']
        assert (await db.setting('channel_close:close_12'))['phase']=='waiting'
        print('PASS notification activation changes channel retention only; order end time, status and payout unchanged')

        for index,status in ((13,'refunded'),(14,'test_closed')):
            await insert('close_'+str(index),status)
            await close.prepare('close_'+str(index),9,'Reviewed')
        data=await close.prepare('close_13',9,'First')
        await db.audit(1,'retain_channel',{},'close_13')
        await rejected(close.confirm('close_13',9,data['code']))
        data=await close.prepare('close_13',9,'Second')
        await close.confirm('close_13',9,data['code'])
        state=await db.setting('channel_close:close_13')
        await close.cancel('close_13')
        await rejected(close.activate('close_13',state['generation']))
        assert (await db.setting('channel_close:close_13'))['phase']=='cancelled'
        print('PASS new retention invalidates stale preview and cancels queued closure')

        data=await close.prepare('close_14',9,'Reviewed')
        data['expires']=0;await db.setting('channel_close_preview:close_14',data)
        await rejected(close.confirm('close_14',9,data['code']))
        print('PASS expired confirmation cannot queue channel closure')

        data=await close.prepare('close_14',9,'Reviewed')
        state=await close.confirm('close_14',9,data['code'])
        pools=list(db._pools.values());db._pools.clear()
        for pool in pools:pool.close()
        for pool in pools:await pool.wait_closed()
        assert (await db.setting('channel_close:close_14'))==state
        assert (await db.query('SELECT hold FROM tracked_channels WHERE channel_id=114',one=True))['hold']
        print('PASS queued closure survives fresh connections without enabling premature deletion')
    finally:
        for pool in db._pools.values():pool.close()
        for pool in db._pools.values():await pool.wait_closed()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
