"""Account-wide durable claims. Unknown withdrawal results are NEVER resubmitted."""
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
import hashlib
import secrets
import re
import config
from utils import new_store as db
from utils import payout_guard

def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0 or result > Decimal('1000000'):
        raise ValueError('金额必须大于 0 且不超过 1,000,000 USDT')
    rounded=result.quantize(Decimal('0.000001'))
    if rounded<=0:
        raise ValueError('金额低于支持的精度')
    return rounded

def legacy_key(value):
    return f'legacy:{config.GUILD_ID}:{value}'

async def require_ready():
    row=await db.query("SELECT value FROM payment_settings WHERE setting_key='legacy_guild'",shared=True,one=True)
    if not row or row['value']!=str(config.GUILD_ID):
        raise ValueError('共享收款切换尚未完成，请执行停服迁移检查')

async def reserve_legacy_credits(ident,user_id,fee):
    await require_ready()
    if not re.fullmatch(r'[A-Za-z0-9_]+',config.MYSQL_DATABASE): raise ValueError('Invalid legacy database name')
    key=legacy_key(ident)
    async with db.transaction(True) as cur:
        await cur.execute('SELECT amount FROM legacy_credits WHERE order_key=%s FOR UPDATE',(key,))
        old=await cur.fetchone()
        if old: return old['amount']
        await cur.execute(f'SELECT free_escrow_amount FROM `{config.MYSQL_DATABASE}`.users WHERE discord_id=%s FOR UPDATE',(user_id,))
        row=await cur.fetchone()
        credit=min(Decimal(str(row['free_escrow_amount'] or 0)),Decimal(str(fee))) if row else Decimal(0)
        await cur.execute(f'UPDATE `{config.MYSQL_DATABASE}`.users SET free_escrow_amount=free_escrow_amount-%s WHERE discord_id=%s',(credit,user_id))
        await cur.execute('INSERT INTO legacy_credits(order_key,user_id,amount) VALUES(%s,%s,%s)',(key,user_id,credit))
        return credit

async def cancel_legacy_trade(ident):
    """Cancel before payment and release the exact reserved credit in one transaction."""
    await require_ready()
    if not re.fullmatch(r'[A-Za-z0-9_]+',config.MYSQL_DATABASE): raise ValueError('Invalid database name')
    key=legacy_key(ident)
    async with db.transaction(True) as cur:
        await cur.execute(f"UPDATE `{config.MYSQL_DATABASE}`.transactions SET status='cancelled',updated_at=NOW() WHERE id=%s AND transaction_type='trade' AND status IN ('pending','confirmed')",(ident,))
        if cur.rowcount!=1: raise ValueError('订单已进入资金流程或状态变化，不能直接取消')
        await cur.execute("SELECT * FROM legacy_credits WHERE order_key=%s AND state='reserved' FOR UPDATE",(key,))
        row=await cur.fetchone()
        if row:
            await cur.execute(f'UPDATE `{config.MYSQL_DATABASE}`.users SET free_escrow_amount=free_escrow_amount+%s WHERE discord_id=%s',(row['amount'],row['user_id']))
            await cur.execute("UPDATE legacy_credits SET state='released' WHERE order_key=%s",(key,))
        return 1

async def invoice(key, address, base):
    await require_ready()
    base = money(base).quantize(Decimal('.01'))
    async with db.transaction(True) as cur:
        await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE', (key,))
        found = await cur.fetchone()
        if found:
            if found['address'] != address or found['state'] != 'waiting' or found['expires_at'] <= datetime.utcnow():
                raise ValueError('付款请求已过期或已处理，请联系管理员核对，不要重复付款')
            return found['amount']
        # Never reuse an old amount automatically: late deposits must not fund a new order.
        import pymysql
        for _ in range(100):
            amount = base + Decimal(secrets.randbelow(9000) + 1000) / Decimal(1000000)
            try:
                await cur.execute('INSERT INTO invoices(order_key,address,amount,expires_at) VALUES(%s,%s,%s,%s)',
                                  (key,address,amount,datetime.utcnow()+timedelta(minutes=30)))
                return amount
            except pymysql.IntegrityError:
                await cur.execute('SELECT * FROM invoices WHERE order_key=%s', (key,))
                row = await cur.fetchone()
                if row:
                    return row['amount']
        raise ValueError('无法分配唯一付款金额，请稍后重试；本次没有生成账单')

async def find_deposit(key, address, expected):
    await require_ready()
    from utils.binance_api import make_api_request
    inv = await db.query('SELECT * FROM invoices WHERE order_key=%s', (key,), shared=True, one=True)
    if not inv:
        raise ValueError('共享账本中没有此账单，必须先核对迁移中的旧订单')
    if inv['address']!=address or Decimal(str(inv['amount']))!=Decimal(str(expected)):
        raise ValueError('传入付款资料与持久化账单不符，禁止认领')
    existing = await db.query('SELECT txid FROM deposits WHERE order_key=%s', (key,), shared=True, one=True)
    if existing:
        if key.startswith('legacy:') and inv['state']=='received_late': return None
        return existing['txid']
    # Includes expired invoices for reconciliation, but never remaps their funds.
    start = int(inv['created_at'].replace(tzinfo=__import__('datetime').timezone.utc).timestamp()*1000)
    deposits = await make_api_request('/sapi/v1/capital/deposit/hisrec', 'GET',
        {'coin':'USDT','startTime':start,'limit':1000})
    if not isinstance(deposits, list):
        return None
    for item in deposits:
        if (item.get('coin') != 'USDT' or item.get('network') != 'BSC' or item.get('status') != 1
                or item.get('address') != address or Decimal(str(item.get('amount',0))) != Decimal(str(expected))):
            continue
        # Provider deposit ID identifies an individual credit (tx hashes alone need not).
        identity = item.get('id')
        txid = item.get('txId')
        if not identity or not txid:
            continue
        async with db.transaction(True) as cur:
            await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE', (key,))
            locked = await cur.fetchone()
            if locked['deposit_id']:
                await cur.execute('SELECT txid FROM deposits WHERE order_key=%s', (key,))
                return (await cur.fetchone())['txid']
            await cur.execute('SELECT order_key FROM deposits WHERE id=%s', (str(identity),))
            if await cur.fetchone():
                continue
            await cur.execute('INSERT INTO deposits(id,order_key,txid,amount,payload) VALUES(%s,%s,%s,%s,%s)',
                              (str(identity),key,txid,expected,db.encode(item)))
            deadline=int(locked['expires_at'].replace(tzinfo=__import__('datetime').timezone.utc).timestamp()*1000)
            late=int(item.get('insertTime',0))>deadline
            await cur.execute("UPDATE invoices SET deposit_id=%s,state=%s WHERE order_key=%s", (str(identity),'received_late' if late else 'received',key))
            return None if late and key.startswith('legacy:') else txid
    return None

async def payout_quote(address, gross):
    from utils.binance_api import make_api_request
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',address) or int(address[2:],16)==0:
        raise ValueError('请输入有效的 USDT-BEP20 地址')
    data = await make_api_request('/sapi/v1/capital/config/getall','GET',{})
    coin = next((c for c in data or [] if c.get('coin')=='USDT'), None) if isinstance(data,list) else None
    network = next((n for n in coin.get('networkList',[]) if n.get('network')=='BSC'),None) if coin else None
    if not network or not network.get('withdrawEnable'):
        raise ValueError('无法确认提现费用或 BSC 提现暂不可用，请稍后重试')
    fee = Decimal(str(network['withdrawFee']))
    gross = money(gross)
    step = Decimal(str(network.get('withdrawIntegerMultiple') or '0.000001'))
    net = ((gross-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
    if net < Decimal(str(network['withdrawMin'])) or net <= 0:
        raise ValueError('扣除网络费后低于最低提现金额，需管理员处理')
    return fee, net

async def release(key, address, gross, fee=Decimal(0), net=None):
    await require_ready()
    if not key.startswith('new:'):
        raise ValueError('旧社群及未识别订单的自动放款已停用')
    from utils.binance_api import make_api_request
    gross = money(gross)
    net = gross if net is None else money(net)
    fee=Decimal(str(fee))
    if not fee.is_finite() or fee<0 or net+fee>gross:
        raise ValueError('放款金额和费用超过订单允许的总额')
    request_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    async with db.transaction(True) as cur:
        # One persistent account gate serializes reservations and honors a safety freeze.
        await cur.execute("INSERT IGNORE INTO payment_settings(setting_key,value) VALUES('payout_freeze','')",())
        await cur.execute("SELECT value FROM payment_settings WHERE setting_key='payout_freeze' FOR UPDATE",())
        gate=await cur.fetchone()
        if not gate or gate['value']:
            raise ValueError('自动放款已暂停，请管理员核对资金安全告警')
        await cur.execute('SELECT * FROM payouts WHERE order_key=%s FOR UPDATE',(key,))
        prior = await cur.fetchone()
        if prior:
            if prior['address'] != address or prior['gross'] != gross:
                raise ValueError('此订单已有其他放款资料，禁止更换地址重复提交')
            if prior['state']=='completed':
                return True, prior['provider_id'], None
            return False, prior['provider_id'], '已经触发了资金释放，结果待核对，禁止重复提交'
        snapshot=await payout_guard.authorize(cur,key,address,gross,fee,net)
        snapshot['amount_mode']='gross'
        snapshot['wallet_type']=0
        await cur.execute('INSERT INTO payment_settings(setting_key,value) VALUES(%s,%s)',
                          ('payout_guard:'+key,db.encode(snapshot)))
        await cur.execute('INSERT INTO payouts(order_key,request_id,address,gross,fee,net) VALUES(%s,%s,%s,%s,%s,%s)',
                          (key,request_id,address,gross,fee,net))
    # The intent is committed BEFORE the external side effect. Even a crash is fail-closed.
    # Confirmed external BSC test: request 3.01, debit 3.01, receipt 3, fee .01.
    request_amount = gross
    response = await make_api_request('/sapi/v1/capital/withdraw/apply','POST',
        {'coin':'USDT','network':'BSC','address':address,'amount':format(request_amount,'f'),
         'withdrawOrderId':request_id,'transactionFeeFlag':'true','walletType':'0'})
    provider_id = response.get('id') if isinstance(response,dict) else None
    await db.query('UPDATE payouts SET state=%s,provider_id=%s,payload=%s WHERE order_key=%s',
        ('submitted' if provider_id else 'unknown',provider_id,db.encode(response),key),shared=True)
    return False, provider_id, '已经触发了资金释放，等待提现结果核对，请勿重复操作'

async def reconcile(key):
    from utils.binance_api import make_api_request
    row = await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
    if not row or row['state']=='completed':
        return row
    start=row['created_at'].replace(tzinfo=__import__('datetime').timezone.utc)
    end=min(datetime.now(__import__('datetime').timezone.utc),start+timedelta(days=6,hours=23))
    response = await make_api_request('/sapi/v1/capital/withdraw/history','GET',
        {'withdrawOrderId':row['request_id'],'startTime':int(start.timestamp()*1000),'endTime':int(end.timestamp()*1000)})
    matches = [r for r in response or [] if r.get('withdrawOrderId')==row['request_id']] if isinstance(response,list) else []
    if len(matches)==1:
        item=matches[0]
        if key.startswith('new:') and item.get('status')==6:
            import json
            saved=await db.query('SELECT value FROM payment_settings WHERE setting_key=%s',('payout_guard:'+key,),shared=True,one=True)
            try:
                payout_guard.check_result(row,item,json.loads(saved['value']) if saved else None)
            except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                await payout_guard.freeze(key,str(exc),item)
                return await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
        if item.get('address') != row['address']:
            raise ValueError('提现历史地址不符，需人工核对')
        state = 'completed' if item.get('status')==6 else ('failed' if item.get('status') in (1,3,5) else 'submitted')
        await db.query('UPDATE payouts SET state=%s,provider_id=%s,payload=%s WHERE order_key=%s',
                       (state,item.get('id'),db.encode(item),key),shared=True)
    return await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
