"""Local disposable MySQL; every provider operation is simulated, including POST."""
from tests.mysql_integration import bootstrap
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock, patch
from utils import new_store as db, shared_payments as p, payout_guard as guard

ADDRESS='0x'+'a'*40
PAYEE='0x'+'b'*40
counter=0


async def seed(ident,refund=False,price=D(10),credit=D(0),create_invoice=False):
    global counter
    counter+=1
    key='new:'+ident
    received=price+D(2)-credit+D(1000+counter)/D(1000000)
    if create_invoice:
        received=await p.invoice(key,ADDRESS,price+D(2)-credit)
    gross=received if refund else price
    status='releasing_refund' if refund else 'releasing'
    await db.query('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee,credits,status) VALUES(%s,1,2,1,%s,%s,%s,2,%s,%s)',(ident,'item','',price,credit,status))
    if create_invoice:
        await db.query("UPDATE invoices SET state='received',deposit_id=%s WHERE order_key=%s",('deposit-'+ident,key),shared=True)
    else:
        await db.query("INSERT INTO invoices(order_key,address,amount,expires_at,state,deposit_id) VALUES(%s,%s,%s,UTC_TIMESTAMP()+INTERVAL 30 MINUTE,'received',%s)",(key,ADDRESS,received,'deposit-'+ident),shared=True)
    await db.query('INSERT INTO deposits(id,order_key,txid,amount,payload) VALUES(%s,%s,%s,%s,%s)',('deposit-'+ident,key,'tx-'+ident,received,'{}'),shared=True)
    await db.audit(1 if refund else 2,status,{'address':PAYEE,'gross':gross,'fee':D('.01'),'net':gross-D('.01')},ident)
    return key,gross


async def payout(key):
    return await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)


def history(row,status=6,**changes):
    result=dict(id='provider-'+row['order_key'],withdrawOrderId=row['request_id'],address=PAYEE,
                coin='USDT',network='BSC',status=status,amount=str(row['net']),transactionFee=str(row['fee']),
                txId='confirmed-'+row['order_key'],completeTime=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
    result.update(changes)
    return result


async def submitted(ident,refund=False):
    key,gross=await seed(ident,refund)
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value={'id':'provider-'+key})):
        await p.release(key,PAYEE,gross,D('.01'),gross-D('.01'))
    return await payout(key)


async def main():
    try:
        key,gross=await seed('late_post')
        started=asyncio.Event(); finish=asyncio.Event()
        async def provider(endpoint,method,params):
            if method=='POST':
                started.set()
                await finish.wait()
                return {'id':'provider-'+key}
            return [history(await payout(key))]
        with patch('utils.binance_api.make_api_request',AsyncMock(side_effect=provider)) as api:
            task=asyncio.create_task(p.release(key,PAYEE,gross,D('.01'),gross-D('.01')))
            try:
                await asyncio.wait_for(started.wait(),5)
                assert (await p.reconcile(key))['state']=='completed'
            finally:
                finish.set()
                await task
            assert (await payout(key))['state']=='completed'
            assert sum(c.args[1]=='POST' for c in api.await_args_list)==1
        print('PASS late withdrawal POST response cannot undo completed reconciliation')

        row=await submitted('late_history')
        started=asyncio.Event(); finish=asyncio.Event(); calls=0
        async def history_race(*args):
            nonlocal calls
            calls+=1
            if calls==1:
                started.set()
                await finish.wait()
                return [history(row,status=4)]
            return [history(row)]
        with patch('utils.binance_api.make_api_request',AsyncMock(side_effect=history_race)):
            task=asyncio.create_task(p.reconcile(row['order_key']))
            try:
                await asyncio.wait_for(started.wait(),5)
                assert (await p.reconcile(row['order_key']))['state']=='completed'
            finally:
                finish.set()
                await task
        assert (await payout(row['order_key']))['state']=='completed'
        print('PASS stale processing history cannot overwrite completed payout')

        row=await submitted('held')
        started=asyncio.Event(); finish=asyncio.Event()
        async def held_race(*args):
            started.set()
            await finish.wait()
            return [history(row)]
        with patch('utils.binance_api.make_api_request',AsyncMock(side_effect=held_race)) as api:
            task=asyncio.create_task(p.reconcile(row['order_key']))
            try:
                await asyncio.wait_for(started.wait(),5)
                await guard.freeze(row['order_key'],'simulated safety hold',{'reason':'review'})
            finally:
                finish.set()
                await task
            assert (await p.reconcile(row['order_key']))['state']=='review'
            assert api.await_count==1
        assert (await db.query("SELECT value FROM payment_settings WHERE setting_key='payout_freeze'",shared=True,one=True))['value']
        print('PASS safety review and account freeze survive an in-flight successful lookup')
        await db.query("UPDATE payment_settings SET value='' WHERE setting_key='payout_freeze'",shared=True)

        row=await submitted('interrupted')
        await db.query("UPDATE payouts SET state='submitting',provider_id=NULL,created_at=UTC_TIMESTAMP()-INTERVAL 5 MINUTE WHERE order_key=%s",(row['order_key'],),shared=True)
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[])) as api:
            assert (await p.reconcile(row['order_key']))['state']=='unknown'
            assert (await p.release(row['order_key'],PAYEE,row['gross'],row['fee'],row['net']))[0] is False
            assert all(call.args[1]=='GET' for call in api.await_args_list)
        print('PASS interrupted submission becomes unknown and is never resubmitted')
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row)])):
            assert (await p.reconcile(row['order_key']))['state']=='completed'
        print('PASS unknown submission can recover from a verified completion without another POST')

        row=await submitted('bad_id')
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row,id='different-transfer')])):
            assert (await p.reconcile(row['order_key']))['state']=='review'
        print('PASS mismatched provider withdrawal identity freezes the account')
        await db.query("UPDATE payment_settings SET value='' WHERE setting_key='payout_freeze'",shared=True)

        row=await submitted('no_evidence')
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row,txId='')])):
            assert (await p.reconcile(row['order_key']))['state']=='review'
        print('PASS success status without transfer evidence is not marked paid out')
        await db.query("UPDATE payment_settings SET value='' WHERE setting_key='payout_freeze'",shared=True)

        row=await submitted('duplicates')
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row),history(row,id='second-transfer')])):
            assert (await p.reconcile(row['order_key']))['state']=='review'
        print('PASS multiple histories for one request require review instead of being ignored')
        await db.query("UPDATE payment_settings SET value='' WHERE setting_key='payout_freeze'",shared=True)

        row=await submitted('full_refund',refund=True)
        assert row['gross']>D(12) and row['net']+row['fee']==row['gross']
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row,address=PAYEE.upper().replace('0X','0x'))])) as api:
            assert (await p.reconcile(row['order_key']))['state']=='completed'
            assert (await p.release(row['order_key'],PAYEE,row['gross'],row['fee'],row['net']))[0] is True
            assert api.await_count==1
        print('PASS full refund includes service fee and identification tail, with no repeat payout')

        results=await asyncio.gather(p.invoice('new:invoice_race',ADDRESS,20),p.invoice('new:invoice_race',PAYEE,30),return_exceptions=True)
        assert sum(isinstance(x,D) for x in results)==1 and sum(isinstance(x,ValueError) for x in results)==1,results
        persisted=await db.query("SELECT * FROM invoices WHERE order_key='new:invoice_race'",shared=True,one=True)
        base=D(20) if persisted['address']==ADDRESS else D(30)
        assert await p.invoice('new:invoice_race',persisted['address'],base)==persisted['amount']
        try: await p.invoice('new:invoice_race',persisted['address'],base+1)
        except ValueError: pass
        else: raise AssertionError('Changed invoice base was accepted')
        print('PASS concurrent invoice retries cannot change amount or address')

        key,gross=await seed('max_refund',refund=True,price=D('1000000'),create_invoice=True)
        assert D('1000002.001')<=gross<=D('1000002.009999')
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value={'id':'provider-'+key})) as api:
            await p.release(key,PAYEE,gross,D('.01'),gross-D('.01'))
            assert D(api.await_args.args[2]['amount'])==gross
        row=await payout(key)
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[history(row)])):
            assert (await p.reconcile(key))['state']=='completed'
        print('PASS maximum-price order can be invoiced and fully refunded within its own deposit')

        with patch.object(p.secrets,'randbelow',return_value=8999):
            key,gross=await seed('fractional_credit',credit=D('.004'),create_invoice=True)
        inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
        assert inv['amount']==D('12.005999'),inv
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value={'id':'provider-'+key})):
            await p.release(key,PAYEE,gross,D('.01'),gross-D('.01'))
        assert (await payout(key))['state']=='submitted'
        print('PASS fractional credits preserve the exact invoice base and remain eligible for payout')

        allocated=await p.invoice('new:precise_base',ADDRESS,D('11.996'))
        assert await p.invoice('new:precise_base',ADDRESS,D('11.996'))==allocated
        try: await p.invoice('new:precise_base',ADDRESS,D('11.996001'))
        except ValueError: pass
        else: raise AssertionError('A sub-cent change was mistaken for an unchanged invoice')
        await db.query("INSERT INTO invoices(order_key,address,amount,expires_at) VALUES('new:old_base',%s,20.001234,UTC_TIMESTAMP()+INTERVAL 30 MINUTE)",(ADDRESS,),shared=True)
        assert await p.invoice('new:old_base',ADDRESS,20)==D('20.001234')
        try: await p.invoice('new:old_base',ADDRESS,D('19.999999'))
        except ValueError: pass
        else: raise AssertionError('A changed legacy base was accepted')
        print('PASS invoice base snapshots reject sub-cent changes and support older cent-based invoices')
    finally:
        for pool in db._pools.values(): pool.close()
        for pool in db._pools.values(): await pool.wait_closed()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
