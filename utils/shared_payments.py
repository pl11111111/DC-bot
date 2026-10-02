"""Account-wide durable claims. Unknown withdrawal results are NEVER resubmitted."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from datetime import datetime, timedelta
import hashlib
import secrets
import re
import config
from utils import new_store as db
from utils import payout_guard

MAX_PRICE = Decimal('1000000')
MAX_INVOICE_BASE = MAX_PRICE + config.NEW.FEE
MAX_SETTLEMENT = MAX_INVOICE_BASE + Decimal('.009999')

class DepositNotReady(ValueError):
    """A matching deposit exists: never treat it as an unpaid invoice."""
    def __init__(self,status):
        self.status=status
        self.waiting=type(status) is int and status in (0,6)
        labels={0:'等待确认',6:'已入账但暂不能提现',2:'被拒绝',7:'错误入款',8:'等待用户确认'}
        label=labels.get(status,'未知状态') if type(status) is int else '未知状态'
        super().__init__(f'匹配入款状态 {status!r}（{label}），保留订单，暂不允许发货或放款')

def money(value, *, maximum=MAX_PRICE):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        # Invalid public input is a validation failure, not a system exception.
        # Never include the submitted value in logs or user-facing errors.
        raise ValueError('金额格式无效，请输入有效数字') from None
    if not result.is_finite() or result <= 0 or result > maximum:
        raise ValueError(f'金额必须大于 0 且不超过 {maximum:,f} USDT')
    rounded=result.quantize(Decimal('0.000001'))
    if rounded<=0:
        raise ValueError('金额低于支持的精度')
    return rounded

def trade_price(value):
    """Validate the price text before six-decimal ledger rounding can hide digits."""
    if (not isinstance(value,str) or len(value)>20
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?',value.strip())):
        raise ValueError('商品金额格式无效，最多两位小数，例如 10.50')
    result=money(value.strip())
    if result<Decimal('5.01'):
        raise ValueError('商品金额不能低于 5.01 USDT。')
    return result

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
    # Credits are stored to six decimals. Rounding this base to cents would
    # change the actual service charge and can invalidate the payout budget.
    base = money(base, maximum=MAX_INVOICE_BASE)
    base_key='invoice_base:'+hashlib.sha256(key.encode()).hexdigest()
    async def existing_amount(cur,row):
        await cur.execute('SELECT value FROM payment_settings WHERE setting_key=%s',(base_key,))
        saved=await cur.fetchone()
        # Older invoices always used a cent-rounded base plus a sub-cent tail.
        original_base=Decimal(saved['value']) if saved else row['amount'].quantize(Decimal('.01'),rounding=ROUND_DOWN)
        if (row['address']!=address or row['state']!='waiting' or row['expires_at']<=datetime.utcnow()
                or original_base!=base
                or not Decimal('.001')<=Decimal(str(row['amount']))-base<=Decimal('.009999')):
            raise ValueError('原账单金额、地址或状态与请求不符，请核对；禁止覆盖或重新收款')
        return row['amount']
    async with db.transaction(True) as cur:
        await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE', (key,))
        found = await cur.fetchone()
        if found:
            return await existing_amount(cur,found)
        # Never reuse an old amount automatically: late deposits must not fund a new order.
        import pymysql
        for _ in range(100):
            amount = base + Decimal(secrets.randbelow(9000) + 1000) / Decimal(1000000)
            try:
                await cur.execute('INSERT INTO invoices(order_key,address,amount,expires_at) VALUES(%s,%s,%s,%s)',
                                  (key,address,amount,datetime.utcnow()+timedelta(minutes=30)))
            except pymysql.IntegrityError:
                await cur.execute('SELECT * FROM invoices WHERE order_key=%s', (key,))
                row = await cur.fetchone()
                if row:
                    return await existing_amount(cur,row)
                continue
            # Commit the exact six-decimal base with the invoice; retries must
            # reject even a sub-cent change that could resemble a different tail.
            await cur.execute('INSERT INTO payment_settings(setting_key,value) VALUES(%s,%s)',(base_key,str(base)))
            return amount
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
    end=int(__import__('time').time()*1000)
    if end-start>=89*86400000:
        raise ValueError('账单超出自动到账核对窗口，请管理员单独核实；未结束订单将保留')
    deposits=[]
    for offset in range(0,10000,1000):
        page=await make_api_request('/sapi/v1/capital/deposit/hisrec','GET',
            {'coin':'USDT','startTime':start,'endTime':end,'limit':1000,'offset':offset})
        if not isinstance(page,list) or any(not isinstance(item,dict) for item in page):
            raise ValueError('到账查询失败，本次不能判定未付款，请稍后重试')
        deposits.extend(page)
        if len(page)<1000: break
    else:
        raise ValueError('到账记录未完整查询，暂停自动结案，请管理员核对')
    for item in deposits:
        if (item.get('coin') != 'USDT' or item.get('network') != 'BSC'
                or str(item.get('address','')).lower() != address.lower()
                or Decimal(str(item.get('amount',0))) != Decimal(str(expected))):
            continue
        if type(item.get('status')) is not int or item['status']!=1:
            raise DepositNotReady(item.get('status'))
        received_at = item.get('insertTime')
        if type(received_at) is not int or not start <= received_at <= end:
            raise ValueError('匹配入款时间缺失或超出账单查询窗口，暂停自动处理')
        identity = item.get('id')
        txid = item.get('txId')
        if not identity or not txid:
            raise ValueError('匹配入款缺少流水标识，暂停自动结案')
        async with db.transaction(True) as cur:
            await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE', (key,))
            locked = await cur.fetchone()
            if locked['deposit_id']:
                await cur.execute('SELECT txid FROM deposits WHERE order_key=%s', (key,))
                return (await cur.fetchone())['txid']
            await cur.execute('SELECT order_key FROM deposits WHERE id=%s', (str(identity),))
            if await cur.fetchone():
                raise ValueError('匹配入款已关联其他订单，请管理员核对')
            await cur.execute('INSERT INTO deposits(id,order_key,txid,amount,payload) VALUES(%s,%s,%s,%s,%s)',
                              (str(identity),key,txid,expected,db.encode(item)))
            deadline=int(locked['expires_at'].replace(tzinfo=__import__('datetime').timezone.utc).timestamp()*1000)
            late=received_at>deadline
            await cur.execute("UPDATE invoices SET deposit_id=%s,state=%s WHERE order_key=%s", (str(identity),'received_late' if late else 'received',key))
            return None if late and key.startswith('legacy:') else txid
    return None

async def payout_quote(address, gross):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',address) or int(address[2:],16)==0:
        raise ValueError('请输入有效的 USDT-BEP20 地址')
    return await payout_amount_quote(gross)

async def payout_amount_quote(gross):
    """Read-only network check, also used before accepting buyer payment."""
    network=await withdrawal_network()
    fee = Decimal(str(network['withdrawFee']))
    gross = money(gross, maximum=MAX_SETTLEMENT)
    step = Decimal(str(network.get('withdrawIntegerMultiple') or '0.000001'))
    minimum = Decimal(str(network['withdrawMin']))
    maximum = Decimal(str(network['withdrawMax'])) if network.get('withdrawMax') is not None else None
    if (not all(x.is_finite() for x in (fee, step, minimum)) or fee < 0 or step <= 0
            or minimum < 0 or (maximum is not None and (not maximum.is_finite() or gross > maximum))):
        raise ValueError('提现费率或限额异常，请管理员核对')
    net = ((gross-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
    if net < Decimal(str(network['withdrawMin'])) or net <= 0:
        minimum=((Decimal(str(network['withdrawMin']))/step).to_integral_value(rounding=ROUND_UP)*step+fee).quantize(Decimal('.01'),rounding=ROUND_UP)
        raise ValueError(f"金额不足，当前最低需 {minimum:.2f} USDT。已付款请联系管理员。")
    return fee, net

async def withdrawal_network():
    from utils.binance_api import make_api_request
    data = await make_api_request('/sapi/v1/capital/config/getall','GET',{})
    coin = next((c for c in data or [] if c.get('coin')=='USDT'), None) if isinstance(data,list) else None
    network = next((n for n in coin.get('networkList',[]) if n.get('network')=='BSC'),None) if coin else None
    if not network or network.get('withdrawEnable') is not True or network.get('withdrawTag'):
        raise ValueError('无法确认提现费用或 BSC 提现暂不可用，请稍后重试')
    return network

async def release(key, address, gross, fee=Decimal(0), net=None):
    await require_ready()
    if not key.startswith('new:'):
        raise ValueError('旧社群及未识别订单的自动放款已停用')
    from utils.binance_api import make_api_request
    # A full refund includes service fees and the invoice identification tail.
    # authorize() still limits every payout to this order's confirmed deposit.
    gross = money(gross, maximum=MAX_SETTLEMENT)
    net = gross if net is None else money(net, maximum=MAX_SETTLEMENT)
    fee=Decimal(str(fee))
    if not fee.is_finite() or fee<0 or net+fee>gross:
        raise ValueError('放款金额和费用超过订单允许的总额')
    request_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    async with db.transaction(True) as cur:
        # One persistent account gate serializes reservations and honors a safety freeze.
        await cur.execute("INSERT INTO payment_settings(setting_key,value) VALUES('payout_freeze','') ON DUPLICATE KEY UPDATE setting_key=setting_key",())
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
    await db.query("UPDATE payouts SET state=%s,provider_id=%s,payload=%s WHERE state='submitting' AND order_key=%s",
        ('submitted' if provider_id else 'unknown',provider_id,db.encode(response),key),shared=True)
    return False, provider_id, '已经触发了资金释放，等待提现结果核对，请勿重复操作'

async def reconcile(key):
    from utils.binance_api import make_api_request
    row = await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
    if not row or row['state'] in ('completed','manual_refunded','review'):
        return row
    start=row['created_at'].replace(tzinfo=__import__('datetime').timezone.utc)
    end=min(datetime.now(__import__('datetime').timezone.utc),start+timedelta(days=6,hours=23))
    response = await make_api_request('/sapi/v1/capital/withdraw/history','GET',
        {'withdrawOrderId':row['request_id'],'startTime':int(start.timestamp()*1000),'endTime':int(end.timestamp()*1000)})
    if not isinstance(response,list) or any(not isinstance(item,dict) for item in response):
        raise ValueError('提现流水查询失败，不能判定成功或失败，禁止重复提交')
    matches = [r for r in response if r.get('withdrawOrderId')==row['request_id']]
    if len(matches)>1:
        await payout_guard.freeze(key,'同一提现请求匹配多条流水，需人工核对',matches)
    elif not matches and row['state']=='submitting' and datetime.now(__import__('datetime').timezone.utc)-start>=timedelta(minutes=2):
        # A crash can happen between committing the intent and receiving its result.
        # An empty lookup is not proof that it is safe to submit another withdrawal.
        await db.query("UPDATE payouts SET state='unknown' WHERE state='submitting' AND order_key=%s",(key,),shared=True)
    if len(matches)==1:
        item=matches[0]
        try:
            if (not isinstance(item.get('id'),str) or not item['id'] or len(item['id'])>100
                    or (row.get('provider_id') and item['id']!=row['provider_id'])
                    or str(item.get('address','')).lower()!=row['address'].lower()
                    or type(item.get('status')) is not int or item['status'] not in (0,1,2,3,4,5,6)):
                raise ValueError('提现流水标识、地址或状态不符，需人工核对')
            if key.startswith('new:') and item['status']==6:
                import json
                saved=await db.query('SELECT value FROM payment_settings WHERE setting_key=%s',('payout_guard:'+key,),shared=True,one=True)
                payout_guard.check_result(row,item,json.loads(saved['value']) if saved else None)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            await payout_guard.freeze(key,str(exc),item)
            return await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
        state = 'completed' if item.get('status')==6 else ('failed' if item.get('status') in (1,3,5) else 'submitted')
        # Late GET/POST responses must not clear a review hold or roll back completion.
        eligible="('submitting','submitted','unknown','failed')" if state=='completed' else "('submitting','submitted','unknown')"
        await db.query(f'UPDATE payouts SET state=%s,provider_id=%s,payload=%s WHERE state IN {eligible} AND order_key=%s',
                       (state,item.get('id'),db.encode(item),key),shared=True)
    return await db.query('SELECT * FROM payouts WHERE order_key=%s',(key,),shared=True,one=True)
