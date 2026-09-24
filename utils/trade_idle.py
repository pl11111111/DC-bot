"""Expire only pre-invoice orders, serialized with invoice creation/admission."""
from datetime import datetime
from utils import new_store as db
from utils.trade_review import database
from utils.trade_states import IDLE_SECONDS


async def expire(ident, actor, expected, deadline):
    if expected not in IDLE_SECONDS or datetime.utcnow() < deadline:
        return False
    name = database()
    async with db.transaction(True) as cur:
        await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE', (ident,))
        row = await cur.fetchone()
        if not row or row['status'] != expected:
            return False
        for table in ('invoices', 'deposits', 'payouts'):
            await cur.execute(f'SELECT order_key FROM {table} WHERE order_key=%s', ('new:'+ident,))
            if await cur.fetchone():
                raise ValueError('待确认订单存在资金记录，必须人工核实')
        if row['credits']:
            raise ValueError('待确认订单存在积分预留，必须人工核实')
        await cur.execute(f"UPDATE `{name}`.orders SET status='expired',closed_at=UTC_TIMESTAMP() WHERE id=%s", (ident,))
        await cur.execute(f'UPDATE `{name}`.tracked_channels SET closed_at=UTC_TIMESTAMP() WHERE channel_id=%s', (row['channel_id'],))
        await cur.execute(f'INSERT INTO `{name}`.audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (actor,'idle_expired',db.encode({'from':expected,'deadline':deadline}),ident))
    return True
