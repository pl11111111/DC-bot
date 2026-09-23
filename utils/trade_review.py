"""Read-only provider evidence and atomic, command-confirmed review settlement.

This module never calls a withdrawal endpoint with POST.
"""
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
import config
from utils import new_store as db, binance_api

TARGETS={'继续履约':'paid','允许卖家收款':'receipt_confirmed','退还买家':'refund_ready','无到账关闭':'cancelled'}


def database():
    name=config.NEW.MYSQL_DATABASE
    if not re.fullmatch(r'[A-Za-z0-9_]+',name): raise ValueError('Invalid database name')
    return name


def milliseconds(value):
    if isinstance(value,str): value=datetime.fromisoformat(value)
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return int(value.timestamp()*1000)


def evidence(invoice,deposit,withdrawal,address):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',address) or int(address[2:],16)==0:
        raise ValueError('退款地址必须为有效 BSC 地址')
    if (deposit.get('coin')!='USDT' or deposit.get('network')!='BSC' or deposit.get('status')!=1
            or not deposit.get('id') or not deposit.get('txId')
            or deposit.get('address','').lower()!=invoice['address'].lower()
            or int(deposit.get('insertTime',0))<milliseconds(invoice['created_at'])):
        raise ValueError('原入款的币种、网络、地址、时间或成功状态不符')
    if (withdrawal.get('coin')!='USDT' or withdrawal.get('network')!='BSC' or withdrawal.get('status')!=6
            or not withdrawal.get('id') or not withdrawal.get('txId') or not withdrawal.get('completeTime')
            or withdrawal.get('address','').lower()!=address.lower()
            or withdrawal.get('transferType')!=0):
        raise ValueError('仅支持核实本账户已完成的外部 USDT-BSC 退款，退款地址或状态不符')
    if milliseconds(withdrawal['applyTime'])<int(deposit['insertTime']):
        raise ValueError('退款时间早于原入款')
    received=Decimal(str(deposit.get('amount','NaN')))
    net=Decimal(str(withdrawal.get('amount','NaN')))
    fee=Decimal(str(withdrawal.get('transactionFee','NaN')))
    if not all(x.is_finite() for x in (received,net,fee)) or received<=0 or net<=0 or fee<0 or net+fee!=received:
        raise ValueError('退款到账金额＋网络费必须等于实际入款，不扣服务费；部分退款或金额不符不能结案')
    if any(x!=x.quantize(Decimal('.000001')) for x in (received,net,fee)):
        raise ValueError('流水金额超出账本支持的六位小数精度，需单独核对')
    return {'received':str(received),'net':str(net),'fee':str(fee),'address':address,
            'deposit':deposit,'withdrawal':withdrawal}


async def verify_manual(ident,deposit_txid,withdrawal_id,address):
    invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+ident,),shared=True,one=True)
    if not invoice: raise ValueError('订单没有原始付款账单')
    start=milliseconds(invoice['created_at'])
    # Conservative supported range: fail closed for older evidence, never infer absence.
    if time.time()*1000-start>89*86400000:
        raise ValueError('账单超过自动核对的 89 天窗口，请保留频道单独核对')
    params={'coin':'USDT','startTime':start,'endTime':int(time.time()*1000),'limit':1000}
    deposits=await binance_api.make_api_request('/sapi/v1/capital/deposit/hisrec','GET',{**params,'txId':deposit_txid})
    withdrawals=await binance_api.make_api_request('/sapi/v1/capital/withdraw/history','GET',{**params,'idList':withdrawal_id})
    if not isinstance(deposits,list) or not isinstance(withdrawals,list):
        raise ValueError('无法查询账户流水，保留频道，不登记成功')
    found=[x for x in deposits if str(x.get('txId','')).lower()==deposit_txid.lower()]
    paid=[x for x in withdrawals if str(x.get('id',''))==withdrawal_id]
    if len(found)!=1 or len(paid)!=1:
        raise ValueError('未找到唯一入款／退款流水，请核对充值交易哈希和币安提现记录 ID；不是截图或链上退款哈希')
    return evidence(invoice,found[0],paid[0],address)


async def prepare(ident,actor,decision,reason,manual=None):
    if not reason.strip() or len(reason)>500: raise ValueError('原因必须为 1–500 字')
    if decision not in TARGETS and decision!='登记手动退款': raise ValueError('处理决定无效')
    async with db.transaction() as cur:
        await cur.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(ident,))
        row=await cur.fetchone()
        if not row or row['status'] not in ('payment_review','disputed'):
            raise ValueError('仅能处理付款待核对或争议订单，放款中／已结案订单不能改判')
        await cur.execute('SELECT COALESCE(MAX(id),0) AS revision FROM audit WHERE order_id=%s',(ident,))
        revision=(await cur.fetchone())['revision']
        code=secrets.token_hex(4)
        data={'actor':actor,'decision':decision,'reason':reason.strip(),'status':row['status'],
              'revision':revision,'expires':time.time()+300,'code':code,'manual':manual}
        encoded=db.encode(data)
        await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)',
                          ('review_confirm:'+ident,encoded))
    return data


def validate_confirmation(data,actor,code,status,revision):
    if not data or data.get('used') or data.get('actor')!=actor or not secrets.compare_digest(str(data.get('code','')),code):
        raise ValueError('确认码不符、已使用或不属于你，请重新提交核对指令')
    if time.time()>data['expires']: raise ValueError('确认码已过期，请重新提交核对指令')
    if status!=data['status'] or revision!=data['revision']:
        raise ValueError('订单或核对记录已变化，请重新预览')


async def confirm(ident,actor,code):
    pending=await db.setting('review_confirm:'+ident)
    if not pending or pending.get('actor')!=actor or pending.get('used') or pending.get('code')!=code or time.time()>pending['expires']:
        raise ValueError('确认码无效或已过期，请重新预览')
    verified=None
    if pending['decision']=='登记手动退款':
        m=pending['manual']
        verified=await verify_manual(ident,m['deposit_txid'],m['withdrawal_id'],m['address'])
        if verified!=m['evidence']: raise ValueError('账户流水已变化，请重新预览')
    else:
        from utils import shared_payments
        invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+ident,),shared=True,one=True)
        if invoice:
            await shared_payments.find_deposit('new:'+ident,invoice['address'],invoice['amount'])
    name=database(); key='new:'+ident
    async with db.transaction(True) as cur:
        # Identical account gate / payout / order lock order to automatic release.
        await cur.execute("INSERT INTO payment_settings(setting_key,value) VALUES('payout_freeze','') ON DUPLICATE KEY UPDATE setting_key=setting_key")
        await cur.execute("SELECT value FROM payment_settings WHERE setting_key='payout_freeze' FOR UPDATE")
        await cur.fetchone()
        await cur.execute('SELECT * FROM payouts WHERE order_key=%s FOR UPDATE',(key,))
        if await cur.fetchone(): raise ValueError('已有提现或退款记录，禁止重复处理（包括未知、失败和处理中）')
        await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE',(ident,))
        row=await cur.fetchone()
        if not row: raise ValueError('订单不存在')
        await cur.execute(f'SELECT value FROM `{name}`.settings WHERE setting_key=%s FOR UPDATE',('review_confirm:'+ident,))
        stored=await cur.fetchone(); data=json.loads(stored['value']) if stored else None
        await cur.execute(f'SELECT COALESCE(MAX(id),0) AS revision FROM `{name}`.audit WHERE order_id=%s',(ident,))
        revision=(await cur.fetchone())['revision']
        validate_confirmation(data,actor,code,row['status'],revision)
        if data!=pending: raise ValueError('处理预览已被替换，请使用最新确认码')
        await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE',(key,))
        invoice=await cur.fetchone()
        await cur.execute('SELECT * FROM deposits WHERE order_key=%s FOR UPDATE',(key,))
        deposit=await cur.fetchone()
        decision=data['decision']; details={'decision':decision,'reason':data['reason'],'from':row['status']}
        if verified:
            if not invoice: raise ValueError('原账单不存在')
            d,w=verified['deposit'],verified['withdrawal']
            evidence(invoice,d,w,verified['address'])
            await cur.execute('SELECT * FROM deposits WHERE id=%s FOR UPDATE',(str(d['id']),))
            used=await cur.fetchone()
            if used and used['order_key']!=key: raise ValueError('原入款已关联其他订单')
            if deposit and str(deposit['id'])!=str(d['id']): raise ValueError('订单已关联另一笔入款，请单独核对多笔付款')
            if invoice['deposit_id'] and str(invoice['deposit_id'])!=str(d['id']): raise ValueError('账单入款关联不一致')
            await cur.execute('SELECT order_key FROM invoices WHERE amount=%s AND LOWER(address)=LOWER(%s) AND order_key<>%s FOR UPDATE',
                              (verified['received'],invoice['address'],key))
            if await cur.fetchone(): raise ValueError('入款金额与另一账单冲突，请保留频道单独核对归属')
            claim='manual_withdraw:'+hashlib.sha256(str(w['id']).encode()).hexdigest()[:60]
            await cur.execute('SELECT value FROM payment_settings WHERE setting_key=%s FOR UPDATE',(claim,))
            if await cur.fetchone(): raise ValueError('该退款流水已登记，不能重复使用')
            await cur.execute('SELECT order_key FROM payouts WHERE provider_id=%s OR request_id=%s FOR UPDATE',
                              (str(w['id']),str(w.get('withdrawOrderId') or '__no_request__')))
            if await cur.fetchone(): raise ValueError('该提现已属于其他账本记录')
            if not deposit:
                await cur.execute('INSERT INTO deposits(id,order_key,txid,amount,payload) VALUES(%s,%s,%s,%s,%s)',
                    (str(d['id']),key,d['txId'],verified['received'],db.encode(d)))
            await cur.execute("UPDATE invoices SET state='manual_refunded',deposit_id=%s WHERE order_key=%s",(str(d['id']),key))
            await cur.execute('INSERT INTO payment_settings(setting_key,value) VALUES(%s,%s)',(claim,key))
            await cur.execute("INSERT INTO payouts(order_key,request_id,address,gross,fee,net,state,provider_id,payload) VALUES(%s,%s,%s,%s,%s,%s,'manual_refunded',%s,%s)",
                (key,'manual-'+hashlib.sha256(key.encode()).hexdigest()[:48],verified['address'],verified['received'],verified['fee'],verified['net'],str(w['id']),db.encode(verified)))
            target='manual_refunded'; details.update(verified)
        else:
            if decision=='无到账关闭':
                if deposit or (invoice and invoice['deposit_id']): raise ValueError('存在已关联入款，不能无到账关闭')
                # Explicit administrator attestation is required; an empty match is not proof.
            elif not invoice or not deposit or invoice['amount']!=deposit['amount'] or str(invoice['deposit_id'])!=str(deposit['id']) or invoice['state'] not in ('received','received_late'):
                raise ValueError('没有金额一致的已核对入款；金额错误只能核实后手动退款')
            target=TARGETS[decision]
        credit=row['credits']
        await cur.execute(f'SELECT value FROM `{name}`.settings WHERE setting_key=%s FOR UPDATE',('closed_unpaid:'+ident,))
        released_record=await cur.fetchone()
        released=json.loads(released_record['value']) if released_record else {}
        credits_released=released.get('credits_released',False)
        if credits_released and credit and target in ('paid','receipt_confirmed'):
            # Expiry already returned the credit. Continuing a late-paid order must
            # charge it again, or the original discounted invoice would be underfunded.
            await cur.execute(f'UPDATE `{name}`.balances SET available=available-%s WHERE user_id=%s AND available>=%s',(credit,row['buyer_id'],credit))
            if cur.rowcount!=1: raise ValueError('超时返还的积分已不足，不能继续履约，请核实后退款')
            released['credits_released']=False
            await cur.execute(f'UPDATE `{name}`.settings SET value=%s WHERE setting_key=%s',(db.encode(released),'closed_unpaid:'+ident))
        if not credits_released and row['status']=='payment_review' and credit:
            await cur.execute(f'UPDATE `{name}`.balances SET reserved=reserved-%s WHERE user_id=%s AND reserved>=%s',(credit,row['buyer_id'],credit))
            if cur.rowcount!=1: raise ValueError('积分预留不一致，不能结案')
        if not credits_released and target in ('refund_ready','cancelled','manual_refunded') and credit:
            await cur.execute(f'UPDATE `{name}`.balances SET available=available+%s WHERE user_id=%s',(credit,row['buyer_id']))
            if cur.rowcount!=1: raise ValueError('积分账户缺失')
        await cur.execute(f'UPDATE `{name}`.orders SET status=%s WHERE id=%s',(target,ident))
        if target in ('cancelled','manual_refunded'):
            await cur.execute(f'UPDATE `{name}`.orders SET closed_at=UTC_TIMESTAMP() WHERE id=%s',(ident,))
            await cur.execute(f'UPDATE `{name}`.tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=%s WHERE channel_id=%s',(target=='manual_refunded',row['channel_id']))
        if target=='cancelled' and invoice:
            await cur.execute("UPDATE invoices SET state='expired' WHERE order_key=%s",(key,))
            await cur.execute(f'INSERT INTO `{name}`.settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=VALUES(value)',
                ('closed_unpaid:'+ident,db.encode({'credits_released':True,'actor':actor,'reason':data['reason'],'status':'cancelled','time':time.time()})))
        if target=='manual_refunded':
            await cur.execute(f'INSERT INTO `{name}`.settings(setting_key,value) VALUES(%s,%s)',
                              ('manual_close:'+ident,db.encode({'phase':'unnotified','generation':secrets.token_hex(4),'details':verified})))
        data['used']=True
        await cur.execute(f'UPDATE `{name}`.settings SET value=%s WHERE setting_key=%s',(db.encode(data),'review_confirm:'+ident))
        await cur.execute(f'INSERT INTO `{name}`.audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (actor,'manual_refund_recorded' if verified else 'review_decision',db.encode(details),ident))
    return target
