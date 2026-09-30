"""Atomic entry and once-only settlement for legacy giveaway credits.

Old giveaways have no trustworthy settlement history. A missing ledger row
therefore requires manual reconciliation, never an inferred fresh balance.
"""
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
import secrets
from utils import database


SCHEMA = '''CREATE TABLE IF NOT EXISTS giveaway_credit_settlements (
    giveaway_id INT PRIMARY KEY,
    outcome VARCHAR(16) NOT NULL DEFAULT 'pending',
    actor_id BIGINT NULL,
    amount DECIMAL(18,8) NOT NULL DEFAULT 0,
    settled_at DATETIME NULL
) ENGINE=InnoDB'''


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('积分金额无效') from None
    if not result.is_finite() or not 0 <= result <= Decimal('1000000'):
        raise ValueError('积分金额无效')
    return result


@asynccontextmanager
async def transaction():
    pool = await database.get_pool()
    async with pool.acquire() as conn:
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


async def create(author_id, prize_name, winners_count, end_time, role_ids='', credit_requirement=0, prize_image=''):
    cost = amount(credit_requirement)
    if not 1 <= winners_count <= 100:
        raise ValueError('中奖人数必须在 1–100 之间')
    async with transaction() as cur:
        await cur.execute('''INSERT INTO giveaways
            (author_id,prize_name,winners_count,end_time,role_ids,credit_requirement,prize_image,status,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'active',NOW())''',
            (author_id,prize_name,winners_count,end_time,role_ids,cost,prize_image))
        ident = cur.lastrowid
        await cur.execute('INSERT INTO giveaway_credit_settlements(giveaway_id) VALUES(%s)', (ident,))
    return ident


async def join(ident, user_id, expected_cost, channel_id, role_ids):
    """Recheck the live cost, eligibility and balance under the same locks."""
    async with transaction() as cur:
        await cur.execute('SELECT *,end_time<=NOW() AS expired FROM giveaways WHERE id=%s FOR UPDATE', (ident,))
        row = await cur.fetchone()
        if not row or row['status'] != 'active' or row['expired']:
            raise ValueError('此抽奖已经结束')
        if row['channel_id'] != channel_id:
            raise ValueError('请使用原抽奖频道的按钮')
        required = {int(x) for x in (row['role_ids'] or '').split(',') if x}
        if required and not required.intersection(role_ids):
            raise ValueError('你没有参与此抽奖所需的身份组')
        cost = amount(row['credit_requirement'])
        if cost != amount(expected_cost):
            raise ValueError('参与费用已变化，请重新确认')
        if cost:
            await cur.execute('SELECT outcome FROM giveaway_credit_settlements WHERE giveaway_id=%s FOR UPDATE', (ident,))
            ledger = await cur.fetchone()
            if not ledger or ledger['outcome'] != 'pending':
                raise ValueError('此抽奖的积分记录需要管理员核对，暂不能参与')
        await cur.execute('SELECT id FROM giveaway_participants WHERE giveaway_id=%s AND user_id=%s', (ident,user_id))
        if await cur.fetchone():
            raise ValueError('您已经参与了此抽奖')
        if cost:
            await cur.execute('''UPDATE users SET free_escrow_amount=free_escrow_amount-%s
                WHERE discord_id=%s AND free_escrow_amount>=%s''', (cost,user_id,cost))
            if cur.rowcount != 1:
                raise ValueError('您的积分不足，或积分账户不存在')
        await cur.execute('''INSERT INTO giveaway_participants(giveaway_id,user_id,credits_used,joined_at)
            VALUES(%s,%s,%s,NOW())''', (ident,user_id,cost))
    return cost


async def settle(ident, actor, outcome, cancel=False):
    if outcome not in ('host','refunded') or (cancel and outcome != 'refunded'):
        raise ValueError('无效的积分结算方式')
    async with transaction() as cur:
        await cur.execute('SELECT * FROM giveaways WHERE id=%s FOR UPDATE', (ident,))
        row = await cur.fetchone()
        if not row or (row['status'] != 'active' if cancel else row['status'] not in ('ended','cancelled')):
            raise ValueError('抽奖状态已变化，或尚未结束')
        await cur.execute('SELECT outcome FROM giveaway_credit_settlements WHERE giveaway_id=%s FOR UPDATE', (ident,))
        ledger = await cur.fetchone()
        if not ledger:
            raise ValueError('历史抽奖没有可靠的结算记录，请人工核对；禁止自动补发或退款')
        if ledger['outcome'] != 'pending':
            raise ValueError('此抽奖积分已经结算，不能重复发放或退款')
        await cur.execute('SELECT user_id,credits_used FROM giveaway_participants WHERE giveaway_id=%s ORDER BY user_id FOR UPDATE', (ident,))
        participants = await cur.fetchall()
        credits = [(p['user_id'],amount(p['credits_used'])) for p in participants]
        total = sum((cost for _,cost in credits), Decimal(0))
        recipients = [(row['author_id'],total)] if outcome == 'host' else credits
        for user_id, cost in recipients:
            if not cost:
                continue
            await cur.execute('UPDATE users SET free_escrow_amount=COALESCE(free_escrow_amount,0)+%s WHERE discord_id=%s', (cost,user_id))
            if cur.rowcount != 1:
                raise ValueError('积分账户缺失，本次结算已全部撤销，请管理员核对')
        await cur.execute('''UPDATE giveaway_credit_settlements
            SET outcome=%s,actor_id=%s,amount=%s,settled_at=NOW() WHERE giveaway_id=%s''', (outcome,actor,total,ident))
        if cancel:
            await cur.execute("UPDATE giveaways SET status='cancelled' WHERE id=%s", (ident,))
    return {'total':total,'count':len(participants),'author_id':row['author_id']}


async def finish(ident):
    """Stop entries and draw winners atomically; duplicate workers are no-ops."""
    async with transaction() as cur:
        await cur.execute('SELECT * FROM giveaways WHERE id=%s FOR UPDATE', (ident,))
        row = await cur.fetchone()
        if not row or row['status'] != 'active':
            return None
        await cur.execute('SELECT * FROM giveaway_participants WHERE giveaway_id=%s', (ident,))
        participants = await cur.fetchall()
        count = min(max(0,row['winners_count']),len(participants))
        winners = secrets.SystemRandom().sample(list(participants),count)
        winner_ids = [str(p['user_id']) for p in winners]
        await cur.execute("UPDATE giveaways SET status='ended',winner_ids=%s WHERE id=%s", (','.join(winner_ids),ident))
    return row, participants, winner_ids
