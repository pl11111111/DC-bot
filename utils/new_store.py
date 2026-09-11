"""Small MySQL repository. Schema installation is explicit, never done on bot login."""
from contextlib import asynccontextmanager
import json
import asyncio
import aiomysql
import config

_pools = {}
_pool_lock = asyncio.Lock()

async def pool(shared=False):
    name = config.NEW.PAYMENTS_DATABASE if shared else config.NEW.MYSQL_DATABASE
    async with _pool_lock:
        if name not in _pools:
            _pools[name] = await aiomysql.create_pool(
                host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD, db=name, charset='utf8mb4',
                cursorclass=aiomysql.DictCursor, autocommit=True, minsize=1, maxsize=10,
                init_command="SET time_zone = '+00:00'")
    return _pools[name]

@asynccontextmanager
async def transaction(shared=False):
    p = await pool(shared)
    async with p.acquire() as conn:
        # Avoid gap-lock deadlocks when independent new order keys are inserted concurrently.
        await conn.query('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                yield cur
            await conn.commit()
        except BaseException:
            if not conn.closed:
                try:
                    await conn.rollback()
                except Exception:
                    conn.close()
            raise

async def query(sql, args=(), shared=False, one=False):
    p = await pool(shared)
    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, args)
            if cur.description:
                return await cur.fetchone() if one else await cur.fetchall()
            return cur.lastrowid or cur.rowcount

def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)

async def audit(actor, action, details, order_id=None):
    await query('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                (actor, action, encode(details), order_id))

async def setting(key, value=None):
    if value is not None:
        await query('INSERT INTO settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)', (key, encode(value)))
        return value
    row = await query('SELECT value FROM settings WHERE setting_key=%s', (key,), one=True)
    return json.loads(row['value']) if row else None

NEW_SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS settings(setting_key VARCHAR(100) PRIMARY KEY,value LONGTEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS audit(id BIGINT AUTO_INCREMENT PRIMARY KEY,actor_id BIGINT NOT NULL,action VARCHAR(80) NOT NULL,details LONGTEXT NOT NULL,order_id VARCHAR(40),created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS invitations(user_id BIGINT PRIMARY KEY,inviter_id BIGINT NULL,code VARCHAR(100),verified BOOLEAN NOT NULL DEFAULT FALSE,joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,verified_at DATETIME NULL)''',
    '''CREATE TABLE IF NOT EXISTS invite_links(code VARCHAR(100) PRIMARY KEY,owner_id BIGINT NOT NULL,INDEX(owner_id))''',
    '''CREATE TABLE IF NOT EXISTS orders(id VARCHAR(40) PRIMARY KEY,buyer_id BIGINT NOT NULL,seller_id BIGINT NOT NULL,initiator_id BIGINT NOT NULL,channel_id BIGINT UNIQUE,source_id BIGINT NULL,item TEXT NOT NULL,terms TEXT NOT NULL,amount DECIMAL(18,6) NOT NULL,fee DECIMAL(18,6) NOT NULL,credits DECIMAL(18,6) NOT NULL DEFAULT 0,status VARCHAR(32) NOT NULL DEFAULT 'pending',address VARCHAR(100),created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,closed_at DATETIME NULL,INDEX(buyer_id,status),INDEX(seller_id,status))''',
    '''CREATE TABLE IF NOT EXISTS balances(user_id BIGINT PRIMARY KEY,available DECIMAL(18,6) NOT NULL DEFAULT 0,reserved DECIMAL(18,6) NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS tracked_channels(channel_id BIGINT PRIMARY KEY,kind VARCHAR(16) NOT NULL,hold BOOLEAN NOT NULL DEFAULT FALSE,closed_at DATETIME NULL)''',
    '''CREATE TABLE IF NOT EXISTS messages(message_id BIGINT PRIMARY KEY,channel_id BIGINT NOT NULL,author_id BIGINT NOT NULL,body MEDIUMTEXT,attachments TEXT NOT NULL,sent_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,expires_at DATETIME NOT NULL,INDEX(expires_at),INDEX(channel_id))''',
    '''CREATE TABLE IF NOT EXISTS message_events(id BIGINT AUTO_INCREMENT PRIMARY KEY,message_id BIGINT NOT NULL,channel_id BIGINT NOT NULL,author_id BIGINT NULL,kind VARCHAR(16) NOT NULL,payload MEDIUMTEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,delivered BOOLEAN DEFAULT FALSE,INDEX(created_at),INDEX(channel_id))''',
]
SHARED_SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS legacy_credits(order_key VARCHAR(100) PRIMARY KEY,user_id BIGINT NOT NULL,amount DECIMAL(18,6) NOT NULL,state VARCHAR(20) NOT NULL DEFAULT 'reserved')''',
    '''CREATE TABLE IF NOT EXISTS payment_settings(setting_key VARCHAR(80) PRIMARY KEY,value TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS invoices(order_key VARCHAR(100) PRIMARY KEY,address VARCHAR(100) NOT NULL,amount DECIMAL(18,6) NOT NULL UNIQUE,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,expires_at DATETIME NOT NULL,state VARCHAR(24) NOT NULL DEFAULT 'waiting',deposit_id VARCHAR(150) NULL UNIQUE)''',
    '''CREATE TABLE IF NOT EXISTS deposits(id VARCHAR(150) PRIMARY KEY,order_key VARCHAR(100) NOT NULL UNIQUE,txid VARCHAR(200) NOT NULL,amount DECIMAL(18,6) NOT NULL,payload TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS payouts(order_key VARCHAR(100) PRIMARY KEY,request_id VARCHAR(64) NOT NULL UNIQUE,address VARCHAR(100) NOT NULL,gross DECIMAL(18,6) NOT NULL,fee DECIMAL(18,6) NOT NULL,net DECIMAL(18,6) NOT NULL,state VARCHAR(24) NOT NULL DEFAULT 'submitting',provider_id VARCHAR(100),payload TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
]
