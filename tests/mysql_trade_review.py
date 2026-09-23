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
    from utils import trade_timeout as timeout
    await order('expiry',103,'4.003333')
    await db.query("UPDATE orders SET status='payment_timeout' WHERE id='expiry'")
    await db.query('UPDATE tracked_channels SET hold=FALSE WHERE channel_id=103')
    await db.query("UPDATE invoices SET expires_at=UTC_TIMESTAMP()-INTERVAL 10 MINUTE WHERE order_key='new:expiry'",shared=True)
    await db.setting('timeout_close:expiry',{'deadline':0})
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=None)):
        try: await timeout.close_unpaid('expiry',99,'timeout',automatic=True)
        except ValueError: pass
        else: raise AssertionError('API failure closed order')
    assert (await db.query("SELECT status FROM orders WHERE id='expiry'",one=True))['status']=='payment_timeout'
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[])):
        await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=103')
        try: await timeout.close_unpaid('expiry',99,'timeout',automatic=True)
        except ValueError: pass
        else: raise AssertionError('held order expired')
        await db.query('UPDATE tracked_channels SET hold=FALSE WHERE channel_id=103')
        outcome=await asyncio.gather(timeout.close_unpaid('expiry',99,'timeout',automatic=True),timeout.close_unpaid('expiry',99,'timeout',automatic=True),return_exceptions=True)
        assert outcome.count('expired')==1,outcome
        assert sum(isinstance(x,ValueError) for x in outcome)==1,outcome
    balance=await db.query('SELECT * FROM balances WHERE user_id=11',one=True)
    assert balance['available']==4 and balance['reserved']==16,balance
    assert (await db.query("SELECT status FROM orders WHERE id='expiry'",one=True))['status']=='expired'
    print('PASS expiry commits once, releases credits, refuses API failure or hold')
    inv=await db.query("SELECT * FROM invoices WHERE order_key='new:expiry'",shared=True,one=True)
    late={**deposit,'id':'late','txId':'late-tx','amount':str(inv['amount']),'insertTime':int(datetime.now(timezone.utc).timestamp()*1000)}
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[late])):
        assert await p.find_deposit('new:expiry',ADDRESS,inv['amount'])=='late-tx'
    assert await timeout.reopen_late('expiry',99)
    assert not await timeout.reopen_late('expiry',99)
    assert (await db.query('SELECT hold FROM tracked_channels WHERE channel_id=103',one=True))['hold']
    data=await r.prepare('expiry',99,'退还买家','late payment refund')
    assert await r.confirm('expiry',99,data['code'])=='refund_ready'
    balance=await db.query('SELECT * FROM balances WHERE user_id=11',one=True)
    assert balance['available']==4 and balance['reserved']==16,balance
    print('PASS late deposit reopens review, refund does not return credits twice')
    await order('batch-unpaid',104,'6.004444')
    await order('batch-funded',106,'6.006666')
    funded={**late,'id':'funded','txId':'funded-tx','amount':'6.006666'}
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[funded])):
        await p.find_deposit('new:batch-funded',ADDRESS,D('6.006666'))
    await order('batch-payout',107,'6.007777')
    await db.query("INSERT INTO payouts(order_key,request_id,address,gross,fee,net,state) VALUES('new:batch-payout','already-submitted',%s,6,.01,5.99,'unknown')",(RETURN,),shared=True)

    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[])):
        batch=await db.query("SELECT id FROM orders WHERE status='payment_review' ORDER BY created_at,id")
        for entry in batch:
            try: await timeout.close_unpaid(entry['id'],99,'administrator requested historical cleanup')
            except ValueError: pass
    assert (await db.query("SELECT status FROM orders WHERE id='batch-unpaid'",one=True))['status']=='cancelled'
    assert (await db.query("SELECT status FROM orders WHERE id='manual1'",one=True))['status']=='manual_refunded'
    assert (await db.query("SELECT status FROM orders WHERE id='expiry'",one=True))['status']=='refund_ready'
    assert (await db.query("SELECT status FROM orders WHERE id='batch-funded'",one=True))['status']=='payment_review'
    assert (await db.query("SELECT status FROM orders WHERE id='batch-payout'",one=True))['status']=='payment_review'
    print('PASS authorized batch cancellation, funded and unknown-payout review orders skipped')
    await order('late-continue',105,'4.005555')
    await db.setting('closed_unpaid:late-continue',{'credits_released':True,'status':'expired'})
    await db.query('UPDATE balances SET available=0 WHERE user_id=11')
    late2={**late,'id':'late2','txId':'late-tx2','amount':'4.005555'}
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[late2])):
        await p.find_deposit('new:late-continue',ADDRESS,D('4.005555'))
    data=await r.prepare('late-continue',99,'继续履约','late payment approved')
    try: await r.confirm('late-continue',99,data['code'])
    except ValueError: pass
    else: raise AssertionError('continued late discounted invoice without available credits')
    assert (await db.query("SELECT status FROM orders WHERE id='late-continue'",one=True))['status']=='payment_review'
    await db.query('UPDATE balances SET available=2 WHERE user_id=11')
    assert await r.confirm('late-continue',99,data['code'])=='paid'
    assert (await db.query('SELECT available FROM balances WHERE user_id=11',one=True))['available']==0
    assert not (await db.setting('closed_unpaid:late-continue'))['credits_released']
    print('PASS late fulfillment recharges returned credits, insufficient balance rolls back')
    for pool in db._pools.values(): pool.close()
    for pool in db._pools.values(): await pool.wait_closed()
    print('ALL MANUAL REFUND INTEGRATION CHECKS PASSED')

if __name__=='__main__': asyncio.run(main())
