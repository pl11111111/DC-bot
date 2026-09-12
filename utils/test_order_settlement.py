"""Explicit administrator accounting for personally owned test funds; no transfer."""
import re
import config
from utils import new_store as db


async def settle(ident,actor,expected_amount,expected_received,reason):
    name=config.NEW.MYSQL_DATABASE
    if not re.fullmatch(r'[A-Za-z0-9_]+',name): raise ValueError('Invalid database name')
    key='new:'+ident
    async with db.transaction(True) as cur:
        # Same lock order as release: account gate -> payout -> order -> invoice/deposit.
        await cur.execute("INSERT INTO payment_settings(setting_key,value) VALUES('payout_freeze','') ON DUPLICATE KEY UPDATE setting_key=setting_key")
        await cur.execute("SELECT value FROM payment_settings WHERE setting_key='payout_freeze' FOR UPDATE")
        await cur.fetchone()
        await cur.execute('SELECT * FROM payouts WHERE order_key=%s FOR UPDATE',(key,))
        if await cur.fetchone(): raise ValueError('已有提现记录，禁止测试结清；请先核对提现结果。')
        await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE',(ident,))
        row=await cur.fetchone()
        if not row or row['status']!='receipt_confirmed' or row['amount']!=expected_amount:
            raise ValueError('订单状态或金额已变化，仅支持尚未提交提现的已确认收货测试订单。')
        await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE',(key,))
        invoice=await cur.fetchone()
        await cur.execute('SELECT * FROM deposits WHERE order_key=%s FOR UPDATE',(key,))
        deposit=await cur.fetchone()
        if not invoice or not deposit or invoice['state']!='received' or str(invoice['deposit_id'])!=str(deposit['id']) or invoice['amount']!=deposit['amount'] or deposit['amount']!=expected_received:
            raise ValueError('到账记录不一致，不能测试结清。')
        details={'reason':reason,'funds_owner_attestation':'管理员确认全部为本人测试资金',
                 'retained_in_account':str(deposit['amount']),'item_amount':str(row['amount']),
                 'service_fee':str(row['fee']),'credits':str(row['credits']),
                 'deposit_id':deposit['id'],'transfer_performed':False,'actor_id':actor}
        await cur.execute('INSERT INTO payment_settings(setting_key,value) VALUES(%s,%s)',('test_settled:'+key,db.encode(details)))
        await cur.execute(f"UPDATE `{name}`.orders SET status='test_closed',closed_at=UTC_TIMESTAMP() WHERE id=%s",(ident,))
        await cur.execute(f'INSERT INTO `{name}`.audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',(actor,'test_funds_retained',db.encode(details),ident))
        await cur.execute(f'UPDATE `{name}`.tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=FALSE WHERE channel_id=%s',(row['channel_id'],))
    return details
