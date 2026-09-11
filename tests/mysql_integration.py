"""Explicit local-only integration suite for a disposable MySQL on port 33379."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test',
                  MYSQL_HOST='127.0.0.1',MYSQL_PORT='33379',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='__test_newbot_legacy',
                  NEW_MYSQL_DATABASE='__test_newbot_new',PAYMENTS_MYSQL_DATABASE='__test_newbot_payments',NEW_WITHDRAW_AMOUNT_MODE='gross')
import asyncio
from datetime import datetime,timedelta
from decimal import Decimal
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace as NS
import pymysql
import config
from utils import new_store as db,shared_payments as p
from modules.new_trading import NewTrading
from modules.new_message_log import NewMessageLog
from setup_new_db import main as install
from prepare_shared_payments import main as cutover

def bootstrap():
    assert config.MYSQL_HOST=='127.0.0.1' and config.MYSQL_PORT==33379
    conn=pymysql.connect(host='127.0.0.1',port=33379,user='root',autocommit=True)
    with conn.cursor() as cur:
        for name in (config.MYSQL_DATABASE,config.NEW.MYSQL_DATABASE,config.NEW.PAYMENTS_DATABASE):
            assert name.startswith('__test_newbot_')
            cur.execute(f'DROP DATABASE IF EXISTS `{name}`')
        cur.execute(f'CREATE DATABASE `{config.MYSQL_DATABASE}`')
        cur.execute(f'USE `{config.MYSQL_DATABASE}`')
        cur.execute("CREATE TABLE users(discord_id BIGINT PRIMARY KEY,free_escrow_amount DECIMAL(18,6) DEFAULT 0)")
        cur.execute("CREATE TABLE transactions(id INT PRIMARY KEY,status ENUM('completed','cancelled','pending','confirmed','paying') DEFAULT 'pending',unique_amount DECIMAL(18,6),payment_address VARCHAR(100),txid VARCHAR(200),transaction_type VARCHAR(20) DEFAULT 'trade',updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cur.execute("INSERT INTO transactions(id,status,unique_amount,payment_address,txid) VALUES(1,'completed',10.001234,'address','historical')")
    conn.close()
    install()
    cutover()

async def main():
    bootstrap()
    await p.require_ready()
    print('PASS schema installation and legacy cutover')
    await db.query('INSERT INTO __test_newbot_legacy.users(discord_id,free_escrow_amount) VALUES(99,2)',shared=True)
    await db.query("INSERT INTO __test_newbot_legacy.transactions(id,status) VALUES(2,'confirmed')",shared=True)
    assert await p.reserve_legacy_credits(2,99,1)==Decimal('1')
    assert await p.reserve_legacy_credits(2,99,1)==Decimal('1')
    await p.cancel_legacy_trade(2)
    balance=await db.query('SELECT free_escrow_amount FROM __test_newbot_legacy.users WHERE discord_id=99',shared=True,one=True)
    assert balance['free_escrow_amount']==Decimal('2')
    print('PASS legacy credit reservation retries once and cancellation restores it')
    amount=await p.invoice('new:first','address',100)
    assert await p.invoice('new:first','address',100)==amount
    print('PASS invoice retry returns existing amount')
    results=await asyncio.gather(*(p.invoice('new:parallel'+str(i),'address',100) for i in range(8)))
    assert len(set(results))==8
    print('PASS concurrent invoices remain unique')
    results=await asyncio.gather(*(p.invoice('new:same','address',101) for i in range(4)))
    assert len(set(results))==1
    print('PASS concurrent retries of same order')

    inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:first',),shared=True,one=True)
    deposit={'id':'credit-1','txId':'tx-1','coin':'USDT','network':'BSC','status':1,'address':'address','amount':str(amount),'insertTime':int(datetime.utcnow().timestamp()*1000)}
    with patch('utils.binance_api.make_api_request',AsyncMock(return_value=[deposit])):
        results=await asyncio.gather(*(p.find_deposit('new:first','address',amount) for i in range(4)))
        assert results==['tx-1']*4
    count=await db.query('SELECT COUNT(*) AS n FROM deposits',shared=True,one=True)
    assert count['n']==1
    print('PASS deposit claimed exactly once')

    request=AsyncMock(return_value=None)
    await db.query("INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee,status) VALUES('first',11,12,11,'item','terms',98,2,'releasing')")
    await db.audit(12,'releasing',{'address':'0x'+'a'*40,'gross':98,'fee':'.1','net':'97.9'},'first')
    with patch('utils.binance_api.make_api_request',request):
        try:
            await p.release('new:first','0x'+'a'*40,100,Decimal('.1'),Decimal('99.9'))
            raise AssertionError('Over-budget payout accepted')
        except ValueError:
            pass
        request.assert_not_awaited()
        await asyncio.gather(*(p.release('new:first','0x'+'a'*40,98,Decimal('.1'),Decimal('97.9')) for _ in range(3)))
        assert request.await_count==1
    print('PASS unknown withdrawal is never submitted twice')

    await db.query("INSERT INTO balances(user_id) VALUES(11),(12)")
    await db.query("INSERT INTO orders(id,buyer_id,seller_id,initiator_id,channel_id,item,terms,amount,fee,status) VALUES('order',11,12,11,100,'item','terms',100,2,'paid')")
    await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(100,'trade'),(101,'forum'),(102,'forum')")
    cog=object.__new__(NewTrading)
    outcomes=await asyncio.gather(cog.transition('order','paid','shipped',12),cog.transition('order','paid','disputed',11),return_exceptions=True)
    assert sum(isinstance(x,ValueError) for x in outcomes)==1
    print('PASS state changes reject racing transition')

    await db.query('INSERT INTO invitations(user_id,inviter_id) VALUES(20,11)')
    await db.query('INSERT IGNORE INTO invitations(user_id,inviter_id) VALUES(20,12)')
    await db.query('UPDATE invitations SET verified=TRUE WHERE user_id=20 AND verified=FALSE')
    row=await db.query('SELECT * FROM invitations WHERE user_id=20',one=True)
    assert row['inviter_id']==11 and row['verified']==1
    print('PASS repeated join cannot change inviter or add a second record')

    old=datetime.utcnow()-timedelta(days=200)
    for mid,cid in ((1,100),(2,101),(3,102)):
        await db.query('INSERT INTO messages VALUES(%s,%s,11,%s,%s,%s,%s,%s)',(mid,cid,'old text','[]',old,old,old))
        await db.query('INSERT INTO message_events(message_id,channel_id,author_id,kind,payload,created_at,delivered) VALUES(%s,%s,11,%s,%s,%s,TRUE)',(mid,cid,'edit','old event',old))
    await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=102')
    logger=object.__new__(NewMessageLog)
    logger.bot=NS(get_channel=lambda _:None)
    logger.lock=asyncio.Lock();logger.bytes=0;logger.warn_level=0
    await logger.cleanup()
    remaining=await db.query('SELECT channel_id FROM messages')
    assert {r['channel_id'] for r in remaining}=={100,102},remaining
    events=await db.query('SELECT channel_id FROM message_events')
    assert {r['channel_id'] for r in events}=={100,102},events
    print('PASS retention deletes expired records, preserves active and held evidence')

    await db.setting('log_pressure',True)
    await logger.cleanup()
    remaining=await db.query('SELECT channel_id FROM messages')
    assert {r['channel_id'] for r in remaining}=={100,102}
    print('PASS capacity cleanup cannot delete protected evidence')
    for pool in db._pools.values(): pool.close()
    for pool in db._pools.values(): await pool.wait_closed()
    print('ALL MYSQL INTEGRATION CHECKS PASSED')

if __name__=='__main__': asyncio.run(main())
