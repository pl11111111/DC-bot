"""Real InnoDB concurrency checks on the disposable local test server only."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',
    BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test',
    MYSQL_HOST='127.0.0.1',MYSQL_PORT='33379',MYSQL_USER='root',MYSQL_PASSWORD='',
    MYSQL_DATABASE='__test_security_giveaway')
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pymysql
import config
from utils import database, giveaway_credits as credits


def bootstrap():
    assert config.MYSQL_HOST == '127.0.0.1' and config.MYSQL_PORT == 33379
    assert config.MYSQL_DATABASE == '__test_security_giveaway'
    with pymysql.connect(host='127.0.0.1',port=33379,user='root',autocommit=True) as conn:
        with conn.cursor() as cur:
            # Refuse existing data; the runner supplies a fresh temporary server.
            cur.execute('CREATE DATABASE __test_security_giveaway')
            cur.execute('USE __test_security_giveaway')
            cur.execute('''CREATE TABLE users(discord_id BIGINT PRIMARY KEY,
                free_escrow_amount DECIMAL(18,8) NOT NULL DEFAULT 0) ENGINE=InnoDB''')
            cur.execute('''CREATE TABLE giveaways(id INT AUTO_INCREMENT PRIMARY KEY,author_id BIGINT,
                prize_name VARCHAR(100),winners_count INT,end_time DATETIME,role_ids TEXT,
                credit_requirement FLOAT,prize_image VARCHAR(255),status VARCHAR(16),
                created_at DATETIME,channel_id BIGINT,message_id BIGINT,winner_ids TEXT) ENGINE=InnoDB''')
            cur.execute('''CREATE TABLE giveaway_participants(id INT AUTO_INCREMENT PRIMARY KEY,
                giveaway_id INT,user_id BIGINT,credits_used FLOAT,joined_at DATETIME,
                UNIQUE(giveaway_id,user_id)) ENGINE=InnoDB''')
            cur.execute(credits.SCHEMA)


async def giveaway(cost=3, role_ids=''):
    ident = await credits.create(99,'test',1,(datetime.now()+timedelta(hours=1)).isoformat(),role_ids,cost)
    await database.update_giveaway_message(ident,1000+ident,77)
    return ident


async def balance(user):
    row = await database.fetch_one('SELECT free_escrow_amount FROM users WHERE discord_id=%s',(user,))
    return row['free_escrow_amount']


async def denied(awaitable):
    try: await awaitable
    except ValueError: return
    raise AssertionError('Unsafe operation was accepted')


async def main():
    bootstrap()
    try:
        await database.execute_query('INSERT INTO users(discord_id,free_escrow_amount) VALUES(1,10),(2,10),(99,0)')
        ident = await giveaway()
        results = await asyncio.gather(*(credits.join(ident,1,3,77,set()) for _ in range(25)), return_exceptions=True)
        assert sum(not isinstance(x,Exception) for x in results) == 1, results
        assert await balance(1) == 7
        assert len(await database.get_giveaway_participants(ident)) == 1
        print('PASS 25 duplicate joins: exactly one charge and one entry')

        a,b = await giveaway(5),await giveaway(5)
        results = await asyncio.gather(credits.join(a,1,5,77,set()),credits.join(b,1,5,77,set()),return_exceptions=True)
        assert sum(not isinstance(x,Exception) for x in results) == 1
        assert await balance(1) == 2
        print('PASS competing giveaways cannot overdraw one balance')

        protected = await giveaway(1,'42')
        for roles,channel,cost in ((set(),77,1),({42},78,1),({42},77,0)):
            await denied(credits.join(protected,2,cost,channel,roles))
        assert await balance(2) == 10
        await database.execute_query('UPDATE giveaways SET end_time=NOW()-INTERVAL 1 SECOND WHERE id=%s',(protected,))
        await denied(credits.join(protected,2,1,77,{42}))
        print('PASS stale role, wrong channel, changed cost and expired entry are rejected')

        results = await asyncio.gather(*(credits.finish(ident) for _ in range(10)))
        assert sum(x is not None for x in results) == 1
        await denied(credits.join(ident,2,3,77,set()))
        # The refund must use actual charges, never a later giveaway price.
        await database.execute_query('UPDATE giveaways SET credit_requirement=999 WHERE id=%s',(ident,))
        before = await balance(1)
        results = await asyncio.gather(*(credits.settle(ident,99,'refunded') for _ in range(25)),return_exceptions=True)
        assert sum(not isinstance(x,Exception) for x in results) == 1
        assert await balance(1) == before+3
        await denied(credits.settle(ident,99,'host'))
        print('PASS draw and 25 refunds settle once using the recorded charge')

        race = await giveaway(2)
        await credits.join(race,2,2,77,set())
        await credits.finish(race)
        before = await balance(2)+await balance(99)
        results = await asyncio.gather(credits.settle(race,99,'host'),credits.settle(race,99,'refunded'),return_exceptions=True)
        assert sum(not isinstance(x,Exception) for x in results) == 1
        assert await balance(2)+await balance(99) == before+2
        print('PASS simultaneous host payment and participant refund release credits only once')

        rollback = await giveaway(1)
        await credits.join(rollback,1,1,77,set())
        await credits.join(rollback,2,1,77,set())
        await credits.finish(rollback)
        before = await balance(1)
        await database.execute_query('DELETE FROM users WHERE discord_id=2')
        await denied(credits.settle(rollback,99,'refunded'))
        assert await balance(1) == before
        row = await database.fetch_one('SELECT outcome FROM giveaway_credit_settlements WHERE giveaway_id=%s',(rollback,))
        assert row['outcome'] == 'pending'
        print('PASS missing recipient rolls back all prior credits and the settlement marker')

        # Exercise transaction cancellation after its writes but before COMMIT.
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            connection_type = type(conn)
        cancelled = await giveaway(1)
        before = await balance(1)
        with patch.object(connection_type,'commit',AsyncMock(side_effect=asyncio.CancelledError)):
            try: await credits.join(cancelled,1,1,77,set())
            except asyncio.CancelledError: pass
            else: raise AssertionError('Cancellation not propagated')
        assert await balance(1) == before
        assert not await database.get_giveaway_participants(cancelled)
        print('PASS cancellation during commit rolls back charge and entry')

        historical = await giveaway(1)
        await database.execute_query('DELETE FROM giveaway_credit_settlements WHERE giveaway_id=%s',(historical,))
        await denied(credits.join(historical,1,1,77,set()))
        await credits.finish(historical)
        await denied(credits.settle(historical,99,'refunded'))
        await denied(credits.settle(historical,99,'host'))
        print('PASS historical giveaways with unknown settlement history fail closed')

        cancel = await giveaway(1)
        await credits.join(cancel,1,1,77,set())
        await credits.settle(cancel,99,'refunded',cancel=True)
        await denied(credits.settle(cancel,99,'refunded'))
        assert (await database.get_giveaway(cancel))['status'] == 'cancelled'
        print('PASS cancellation and refunds share the same once-only ledger')
    finally:
        if database._connection_pool:
            database._connection_pool.close()
            await database._connection_pool.wait_closed()


if __name__ == '__main__':
    asyncio.run(main())
