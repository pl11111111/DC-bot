"""Disposable local MySQL integration: no live API or real funds."""
from tests.mysql_integration import bootstrap
import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import new_store as db,trade_review as r,shared_payments as p
from modules.new_trading import NewTrading

ADDRESS='0x1234567890123456789012345678901234567890'
RETURN='0x2234567890123456789012345678901234567890'

async def order(ident,channel,expected='6.001234'):
    await db.query("INSERT INTO orders(id,buyer_id,seller_id,initiator_id,channel_id,item,terms,amount,fee,credits,status) VALUES(%s,11,12,11,%s,'item','',4,2,2,'payment_review')",(ident,channel))
    await db.query("INSERT INTO tracked_channels(channel_id,kind,hold) VALUES(%s,'trade',TRUE)",(channel,))
    await db.query("INSERT INTO invoices(order_key,address,amount,created_at,expires_at) VALUES(%s,%s,%s,%s,%s)",('new:'+ident,ADDRESS,expected,datetime.utcnow()-timedelta(minutes=5),datetime.utcnow()+timedelta(minutes=25)),shared=True)

async def main():
    bootstrap()
    await db.query('INSERT INTO balances(user_id,available,reserved) VALUES(11,0,20),(12,0,0)')
    await order('manual1',100)
    now=datetime.now(timezone.utc)
    deposit={'id':'d1','txId':'tx1','coin':'USDT','network':'BSC','status':1,'address':ADDRESS,'amount':'6','insertTime':int((now-timedelta(minutes=3)).timestamp()*1000)}
    withdrawal={'id':'w1','txId':'tx2','coin':'USDT','network':'BSC','status':6,'address':RETURN,'amount':'5.99','transactionFee':'.01','transferType':0,'applyTime':(now-timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),'completeTime':(now-timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')}
    async def provider(endpoint,method,params):
        assert method=='GET', 'NO TRANSFERS IN THIS TEST'
        return [deposit] if 'deposit' in endpoint else [withdrawal]
    async def preview(ident):
        verified=await r.verify_manual(ident,deposit['txId'],withdrawal['id'],RETURN)
        return await r.prepare(ident,99,'登记手动退款','本人已核对买家归属及退款地址',{'deposit_txid':deposit['txId'],'withdrawal_id':withdrawal['id'],'address':RETURN,'evidence':verified})
    with patch('utils.binance_api.make_api_request',AsyncMock(side_effect=provider)):
        token=await preview('manual1')
        results=await asyncio.gather(r.confirm('manual1',99,token['code']),r.confirm('manual1',99,token['code']),return_exceptions=True)
        assert results.count('manual_refunded')==1,results
        assert sum(isinstance(x,ValueError) for x in results)==1,results
        invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:manual1',),shared=True,one=True)
        payout=await db.query('SELECT * FROM payouts WHERE order_key=%s',('new:manual1',),shared=True,one=True)
        balance=await db.query('SELECT * FROM balances WHERE user_id=11',one=True)
        assert invoice['amount']==D('6.001234') and invoice['state']=='manual_refunded'
        assert payout['gross']==D(6) and payout['net']==D('5.99') and payout['state']=='manual_refunded'
        assert balance['available']==2 and balance['reserved']==18,balance
        assert (await p.release('new:manual1',RETURN,D(6),D('.01'),D('5.99')))[0] is False
        assert (await p.reconcile('new:manual1'))['state']=='manual_refunded'
        print('PASS atomic manual ledger, concurrent duplicate, credit restoration, no second payout')
        await order('manual2',101,'6.002234')
        token=await preview('manual2')
        try: await r.confirm('manual2',99,token['code'])
        except ValueError: pass
        else: raise AssertionError('deposit reused')
        deposit={**deposit,'id':'d2','txId':'different'}
        token=await preview('manual2')
        try: await r.confirm('manual2',99,token['code'])
        except ValueError: pass
        else: raise AssertionError('withdrawal reused')
        assert not await db.query('SELECT * FROM deposits WHERE id=%s',('d2',),shared=True,one=True)
        assert (await db.query('SELECT status FROM orders WHERE id=%s',('manual2',),one=True))['status']=='payment_review'
        print('PASS deposit/withdrawal reuse rejected, complete rollback')
        withdrawal={**withdrawal,'id':'w2','txId':'tx3'}
        token=await preview('manual2')
        await db.audit(99,'new_opinion',{},'manual2')
        try: await r.confirm('manual2',99,token['code'])
        except ValueError: pass
        else: raise AssertionError('stale revision allowed')
        print('PASS stale confirmation invalidated by intervening review')
    channel=NS(id=100,guild=NS(id=2),send=AsyncMock(return_value=NS(id=999)),delete=AsyncMock())
    cog=object.__new__(NewTrading);cog.cleanup_lock=asyncio.Lock();cog.bot=NS(get_channel=lambda _:channel,user=NS(id=77));cog.alert=AsyncMock()
    await cog.manual_close_tick('manual1')
    state=await db.setting('manual_close:manual1'); assert state['phase']=='waiting'
    deadline=state['deadline']
    await cog.manual_close_tick('manual1'); assert (await db.setting('manual_close:manual1'))['deadline']==deadline
    inter=NS(user=NS(id=11),channel_id=100,channel=channel,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
    await cog.refund_close_response(inter,'new:refund_close:hold:manual1:'+state['generation'])
    state=await db.setting('manual_close:manual1');assert state['phase']=='held'
    state['deadline']=0;await db.setting('manual_close:manual1',state)
    await cog.manual_close_tick('manual1');channel.delete.assert_not_awaited()
    state['phase']='waiting';await db.setting('manual_close:manual1',state)
    await db.query('UPDATE tracked_channels SET hold=FALSE WHERE channel_id=100')
    await cog.manual_close_tick('manual1');channel.delete.assert_awaited_once()
    assert (await db.setting('manual_close:manual1'))['phase']=='deleted'
    print('PASS real database close timer, no reset, refusal hold, expiration')
    for pool in db._pools.values(): pool.close()
    for pool in db._pools.values(): await pool.wait_closed()
    print('ALL MANUAL REFUND INTEGRATION CHECKS PASSED')

if __name__=='__main__': asyncio.run(main())
