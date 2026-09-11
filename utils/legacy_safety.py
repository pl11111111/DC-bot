"""Serialize legacy trading transitions without changing legacy prices or commands."""
import functools
import asyncio
import aiomysql
import config
from contextvars import ContextVar
from utils import database

held=ContextVar('legacy_order_locks',default=frozenset())
_lock_pool=None
_creation_lock=asyncio.Lock()

async def lock_pool():
    global _lock_pool
    async with _creation_lock:
        if _lock_pool is None:
            # Lock waiters must not consume the pool used for queries inside a lock.
            _lock_pool=await aiomysql.create_pool(host=config.MYSQL_HOST,port=config.MYSQL_PORT,user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,db=config.MYSQL_DATABASE,
                cursorclass=aiomysql.DictCursor,autocommit=True,minsize=1,maxsize=5)
    return _lock_pool

def serialized(method):
    @functools.wraps(method)
    async def wrapped(self,*args,**kwargs):
        row=next((a for a in args if isinstance(a,dict) and 'id' in a),None)
        ident=row['id'] if row else next((a for a in args if isinstance(a,int)),None)
        owner=(ident,asyncio.current_task())
        if ident is None or owner in held.get(): return await method(self,*args,**kwargs)
        pool=await lock_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                name='legacy-trade:'+str(ident)
                await cur.execute('SELECT GET_LOCK(%s,10) AS acquired',(name,))
                if (await cur.fetchone())['acquired']!=1: raise ValueError('订单正在处理中，请稍后重试')
                token=held.set(held.get()|{owner})
                try:
                    if row:
                        fresh=await database.get_transaction(ident)
                        if not fresh: raise ValueError('订单不存在')
                        args=tuple(fresh if a is row else a for a in args)
                    return await method(self,*args,**kwargs)
                finally:
                    held.reset(token)
                    await cur.execute('SELECT RELEASE_LOCK(%s)',(name,))
    return wrapped
