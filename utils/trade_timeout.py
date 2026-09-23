"""Atomic unpaid expiry/cancellation. Keeps invoices and monitors late deposits."""
import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from utils import new_store as db, shared_payments as payments
from utils.trade_review import database

log=logging.getLogger(__name__)


async def close_unpaid(ident,actor,reason,*,automatic=False):
    if not reason.strip(): raise ValueError('必须填写结案原因')
    name=database(); key='new:'+ident
    invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
    if not invoice: raise ValueError('缺少原付款账单，跳过并保留待核对状态')
    if await payments.find_deposit(key,invoice['address'],invoice['amount']):
        raise ValueError('已找到入款，不得取消或自动超时结束')
    expected='payment_timeout' if automatic else 'payment_review'
    target='expired' if automatic else 'cancelled'
    async with db.transaction(True) as cur:
        await cur.execute("INSERT INTO payment_settings(setting_key,value) VALUES('payout_freeze','') ON DUPLICATE KEY UPDATE setting_key=setting_key")
        await cur.execute("SELECT value FROM payment_settings WHERE setting_key='payout_freeze' FOR UPDATE")
        await cur.fetchone()
        await cur.execute('SELECT * FROM payouts WHERE order_key=%s FOR UPDATE',(key,))
        if await cur.fetchone(): raise ValueError('存在提现记录，跳过并保留订单')
        await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE',(ident,))
        row=await cur.fetchone()
        if not row or row['status']!=expected: raise ValueError('订单状态已变化，不重复结案')
        await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE',(key,))
        inv=await cur.fetchone()
        await cur.execute('SELECT id FROM deposits WHERE order_key=%s FOR UPDATE',(key,))
        deposit=await cur.fetchone()
        if not inv or inv['state'] not in ('waiting','expired') or inv['deposit_id'] or deposit:
            raise ValueError('账单状态变化或存在入款，停止结案')
        if inv['address']!=invoice['address'] or inv['amount']!=invoice['amount']:
            raise ValueError('账单金额或地址在查询后发生变化，停止结案')
        await cur.execute(f'SELECT hold FROM `{name}`.tracked_channels WHERE channel_id=%s FOR UPDATE',(row['channel_id'],))
        tracked=await cur.fetchone()
        if automatic:
            if not tracked or tracked['hold']: raise ValueError('频道已申请保留，不能自动结束')
            await cur.execute(f'SELECT value FROM `{name}`.settings WHERE setting_key=%s FOR UPDATE',('timeout_close:'+ident,))
            timer=await cur.fetchone()
            if not timer or json.loads(timer['value'])['deadline']>time.time() or inv['expires_at']>datetime.utcnow():
                raise ValueError('付款或关闭倒计时尚未到期')
        await cur.execute(f'SELECT value FROM `{name}`.settings WHERE setting_key=%s FOR UPDATE',('closed_unpaid:'+ident,))
        previous=await cur.fetchone()
        already_released=previous and json.loads(previous['value']).get('credits_released')
        credit=Decimal(str(row['credits']))
        if not credit.is_finite() or not 0<=credit<=Decimal(str(row['fee'])):
            raise ValueError('订单积分数据异常，停止结案')
        if credit and not already_released:
            await cur.execute(f'UPDATE `{name}`.balances SET reserved=reserved-%s,available=available+%s WHERE user_id=%s AND reserved>=%s',
                              (credit,credit,row['buyer_id'],credit))
            if cur.rowcount!=1: raise ValueError('积分预留不一致，停止结案')
        record={'reason':reason,'actor':actor,'status':target,'credits_released':True,
                'credit_amount':str(credit),'time':time.time(),'payment_conclusion':'no_matching_deposit; not proof of no funds'}
        await cur.execute(f'INSERT INTO `{name}`.settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)',
                          ('closed_unpaid:'+ident,db.encode(record)))
        await cur.execute(f'UPDATE `{name}`.orders SET status=%s,closed_at=UTC_TIMESTAMP() WHERE id=%s',(target,ident))
        await cur.execute("UPDATE invoices SET state='expired' WHERE order_key=%s",(key,))
        await cur.execute(f'UPDATE `{name}`.tracked_channels SET hold=FALSE,closed_at=UTC_TIMESTAMP() WHERE channel_id=%s',(row['channel_id'],))
        await cur.execute(f'INSERT INTO `{name}`.audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (actor,'payment_expired' if automatic else 'batch_cancel_review',db.encode(record),ident))
    log.warning('Unpaid order closed: order=%s actor=%s status=%s reason=%s',ident,actor,target,reason)
    return target


async def reopen_late(ident,actor):
    """Restore review only, never automatically fulfill or pay a late deposit."""
    name=database();key='new:'+ident
    async with db.transaction(True) as cur:
        await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE',(ident,))
        row=await cur.fetchone()
        if not row or row['status'] not in ('expired','cancelled'): return False
        await cur.execute(f'SELECT value FROM `{name}`.settings WHERE setting_key=%s',('closed_unpaid:'+ident,))
        if not await cur.fetchone(): return False
        await cur.execute('SELECT id FROM deposits WHERE order_key=%s',(key,))
        if not await cur.fetchone(): return False
        await cur.execute(f"UPDATE `{name}`.orders SET status='payment_review',closed_at=NULL WHERE id=%s",(ident,))
        await cur.execute(f'UPDATE `{name}`.tracked_channels SET hold=TRUE WHERE channel_id=%s OR channel_id=%s',(row['channel_id'],row['source_id']))
        await cur.execute(f'INSERT INTO `{name}`.audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (actor,'late_payment_reopened',db.encode({'from':row['status'],'automatic_payout':False}),ident))
    log.warning('Late deposit reopened for review: %s',ident)
    return True

