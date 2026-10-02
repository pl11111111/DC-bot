"""Hostile strings against real, disposable MySQL; no external service calls."""
from tests.mysql_integration import bootstrap
import asyncio
import json
from utils import new_store as db, database
from modules.new_trading import NewTrading

PAYLOADS=("?id=1 AND 1=2", "x' OR '1'='1", "x'; UPDATE orders SET amount=0; -- ",
          "x' UNION SELECT 1 -- ", 'x\\\"\x00\r\n', '../../.env',
          '__import__("os").system("echo injected")', '$(echo injected)',
          '<script>alert(1)</script>', "中文'🧪")


async def main():
    try:
        for index,payload in enumerate(PAYLOADS):
            ident='input_'+str(index)
            await db.query('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee) VALUES(%s,1,2,1,%s,%s,10,2)',(ident,payload,payload))
            cog=object.__new__(NewTrading)
            row=await cog.order(ident)
            assert row['item']==payload and row['terms']==payload and row['amount']==10
            assert await cog.order(payload) is None
        assert (await db.query('SELECT COUNT(*) n FROM orders WHERE amount=10',one=True))['n']==len(PAYLOADS)
        print('PASS hostile order text is preserved as data and cannot change queries or other orders')

        for index,payload in enumerate(PAYLOADS):
            await db.setting('input_'+str(index),{'text':payload})
            assert await db.setting('input_'+str(index))=={'text':payload}
            await db.audit(1,payload,{'text':payload},'input_0')
        rows=await db.query('SELECT action,details FROM audit ORDER BY id')
        assert [r['action'] for r in rows]==list(PAYLOADS)
        assert [json.loads(r['details'])['text'] for r in rows]==list(PAYLOADS)
        print('PASS settings and audit JSON store injection-like content literally')

        await database.execute_query('''CREATE TABLE currency_trades(
            id INT AUTO_INCREMENT PRIMARY KEY,user_id BIGINT,currency_type VARCHAR(100),
            trade_type VARCHAR(100),quantity DECIMAL(18,6),price DECIMAL(18,6),
            created_at DATETIME,expires_at DATETIME)''')
        for index,payload in enumerate(PAYLOADS):
            ident=await database.create_currency_trade(100+index,payload,payload,1,2)
            rows=await database.get_currency_trades_by_type(payload,payload)
            assert len(rows)==1 and rows[0]['id']==ident and rows[0]['user_id']==100+index
            # Even a known ID cannot delete another user's listing.
            assert not await database.delete_currency_trade(ident,9999)
            assert len(await database.get_currency_trades_by_user(100+index))==1
        assert not await database.get_currency_trades_by_type("' OR 7=7 -- ")
        print('PASS legacy listing filters remain parameterized and deletion enforces ownership')
    finally:
        pools=list(db._pools.values())
        if database._connection_pool: pools.append(database._connection_pool)
        for pool in pools: pool.close()
        for pool in pools: await pool.wait_closed()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
