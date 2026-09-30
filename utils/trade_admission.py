"""Durable creation limits, held in the same transaction as the new order.

The settings row serializes creation/acceptance across processes. Limits never
prevent settlement, cancellation or recovery of an already accepted order.
"""
import json
import config
from utils import new_store as db
from utils.trade_states import TERMINAL_SQL

cfg = config.NEW
GATE = 'trade_admission'


class AdmissionDenied(ValueError):
    def __init__(self, chinese, english):
        self.chinese, self.english = chinese, english
        super().__init__(english)


async def lock(cur):
    await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE setting_key=setting_key', (GATE,'[]'))
    await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE', (GATE,))
    return json.loads((await cur.fetchone())['value'])


async def active(cur, uid, excluding=''):
    # A request the user has not accepted must not consume their own trade slots.
    await cur.execute(f"SELECT COUNT(*) AS n FROM orders WHERE (buyer_id=%s OR seller_id=%s) AND status NOT IN {TERMINAL_SQL} AND NOT(status='pending' AND initiator_id<>%s) AND id<>%s", (uid,uid,uid,excluding))
    return (await cur.fetchone())['n']


async def reserve(cur, initiator, other):
    recent = await lock(cur)
    await cur.execute('SELECT UNIX_TIMESTAMP() AS now')
    now = int((await cur.fetchone())['now'])
    recent = [stamp for stamp in recent if stamp>now-60]
    if len(recent)>=10:
        raise AdmissionDenied('当前创建交易较多，请一分钟后重试。','Too many new trades. Please try again in one minute.')
    key = 'trade_rate:'+str(initiator)
    await cur.execute('SELECT value FROM settings WHERE setting_key=%s', (key,))
    stored = await cur.fetchone()
    history = [stamp for stamp in json.loads(stored['value']) if stamp>now-3600] if stored else []
    if history and now-max(history)<cfg.TRADE_CREATE_COOLDOWN:
        raise AdmissionDenied(f'创建交易后请等待 {cfg.TRADE_CREATE_COOLDOWN} 秒再发起，取消订单不会重置等待时间。', f'Wait {cfg.TRADE_CREATE_COOLDOWN} seconds between new trades. Cancelling does not reset this limit.')
    if len(history)>=cfg.TRADE_CREATE_HOURLY:
        raise AdmissionDenied('本小时创建交易次数已达上限，请稍后再试。','Your hourly trade creation limit has been reached. Please try later.')
    for uid in (initiator,other):
        if await active(cur,uid)>=cfg.MAX_ACTIVE:
            raise ValueError(f'每位用户最多同时进行 {cfg.MAX_ACTIVE} 笔交易，请先完成或取消已有订单。')
    await cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status='pending' AND (buyer_id=%s OR seller_id=%s) AND initiator_id<>%s", (other,other,other))
    if (await cur.fetchone())['n']>=cfg.TRADE_MAX_INCOMING:
        raise AdmissionDenied('对方尚未处理的交易邀请较多，请等待对方处理。','This member already has pending invitations. Wait for them to respond.')
    # Cancelled/finished channels still consume real Discord capacity until deleted.
    await cur.execute(f"SELECT COUNT(*) AS n FROM orders o WHERE NOT EXISTS(SELECT 1 FROM settings s WHERE s.setting_key=CONCAT('channel_deleted:',o.id)) AND (o.channel_id IS NOT NULL OR o.status NOT IN {TERMINAL_SQL})")
    if (await cur.fetchone())['n']>=cfg.TRADE_MAX_CHANNELS:
        raise AdmissionDenied('交易频道已达容量上限，请等待已有频道处理完成。现有交易可继续。','Trade channel capacity reached. Please wait; existing trades can continue.')
    # The payment step expects both accounts to exist, even with no earned credit.
    # Preserve this creation invariant while the settings row owns admission locking.
    for uid in sorted((initiator,other)):
        await cur.execute('INSERT INTO balances(user_id) VALUES(%s) ON DUPLICATE KEY UPDATE user_id=user_id', (uid,))
    history.append(now)
    await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=%s', (key,db.encode(history),db.encode(history)))
    recent.append(now)
    await cur.execute('UPDATE settings SET value=%s WHERE setting_key=%s', (db.encode(recent),GATE))


async def confirm(cur, ident, actor):
    await lock(cur)
    await cur.execute('SELECT buyer_id,seller_id,initiator_id,status FROM orders WHERE id=%s FOR UPDATE', (ident,))
    row = await cur.fetchone()
    if not row or row['status']!='pending':
        return  # The caller's conditional UPDATE will report the stale order.
    if actor not in (row['buyer_id'],row['seller_id']) or actor==row['initiator_id']:
        raise AdmissionDenied('只有被邀请的交易对方可以确认。','Only the invited participant can accept this trade.')
    for uid in (row['buyer_id'],row['seller_id']):
        if await active(cur,uid,excluding=ident)>=cfg.MAX_ACTIVE:
            raise ValueError(f'每位用户最多同时进行 {cfg.MAX_ACTIVE} 笔交易，请先完成或取消已有订单。')
