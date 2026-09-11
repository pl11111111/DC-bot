"""Authorize a new-guild payout from its own durable deposit, never account balance."""
import json
import re
from decimal import Decimal
import config
from utils import new_store as db


def budget(order, invoice, deposit, intent, address, gross, fee, net):
    if not order or order['status'] not in ('releasing', 'releasing_refund'):
        raise ValueError('订单未授权放款或退款')
    if not invoice or not deposit or invoice['state'] not in ('received', 'received_late'):
        raise ValueError('本订单没有已确认的到账记录，禁止放款')
    if str(invoice['deposit_id']) != str(deposit['id']) or invoice['amount'] != deposit['amount']:
        raise ValueError('账单与到账流水不一致')
    price, service, credit, received = map(Decimal, (order['amount'],order['fee'],order['credits'],deposit['amount']))
    if not all(x.is_finite() for x in (price,service,credit,received)) or price<=0 or not 0<=credit<=service:
        raise ValueError('订单金额或积分记录异常')
    charged=service-credit
    tail=received-price-charged
    if not Decimal('.001')<=tail<=Decimal('.009999'):
        raise ValueError('到账与商品金额、实际服务费及账单尾数不一致')
    refund=order['status']=='releasing_refund'
    cap=received if refund else min(price,received-charged)
    if gross!=cap or net<=0 or fee<0 or net+fee>cap:
        raise ValueError('本次账户支出超过或不符合本订单可支付额度')
    payee=order['buyer_id'] if refund else order['seller_id']
    if not intent or intent['actor_id']!=payee:
        raise ValueError('缺少收款人确认记录')
    data=json.loads(intent['details'])
    if data.get('address','').strip()!=address or any(Decimal(str(data.get(k,'NaN')))!=v for k,v in (('gross',gross),('fee',fee),('net',net))):
        raise ValueError('提现资料与收款人确认的资料不一致')
    return {'cap':cap,'received':received,'service_charged':charged,'identification_tail':tail,
            'refund':refund,'fee':fee,'net':net,'address':address}


async def authorize(cur,key,address,gross,fee,net):
    name=config.NEW.MYSQL_DATABASE
    if not re.fullmatch(r'[A-Za-z0-9_]+',name):
        raise ValueError('Invalid database name')
    await cur.execute(f'SELECT * FROM `{name}`.orders WHERE id=%s FOR UPDATE',(key[4:],))
    order=await cur.fetchone()
    await cur.execute('SELECT * FROM invoices WHERE order_key=%s FOR UPDATE',(key,))
    invoice=await cur.fetchone()
    await cur.execute('SELECT * FROM deposits WHERE order_key=%s FOR UPDATE',(key,))
    deposit=await cur.fetchone()
    await cur.execute(f'SELECT actor_id,details FROM `{name}`.audit WHERE order_id=%s AND action=%s ORDER BY id DESC LIMIT 1',
                      (key[4:],order['status'] if order else ''))
    intent=await cur.fetchone()
    return budget(order,invoice,deposit,intent,address,gross,fee,net)


async def freeze(key,reason,item):
    # A persistent latch: a restart must not silently resume automatic payouts.
    await db.query("INSERT INTO payment_settings(setting_key,value) VALUES('payout_freeze',%s) ON DUPLICATE KEY UPDATE value=VALUES(value)",
                   (db.encode({'order':key,'reason':reason}),),shared=True)
    await db.query("UPDATE payouts SET state='review',payload=%s WHERE order_key=%s",(db.encode(item),key),shared=True)


def check_result(row,item,snapshot):
    if not snapshot:
        raise ValueError('缺少放款额度快照，需人工核对历史提现')
    if item.get('coin')!='USDT' or item.get('network')!='BSC' or item.get('address')!=row['address']:
        raise ValueError('提现币种、网络或地址不符')
    amount=Decimal(str(item.get('amount','NaN')))
    fee=Decimal(str(item.get('transactionFee','NaN')))
    if not amount.is_finite() or not fee.is_finite() or amount<=0 or fee<0:
        raise ValueError('提现金额或费用缺失')
    # External BSC test confirmed: history amount is receipt, amount + fee is debit.
    # Keep the safety bounds for other outcomes (including internal transfers).
    if amount+fee>Decimal(snapshot['cap']) or amount<Decimal(snapshot['net']) or fee>Decimal(snapshot['fee']):
        raise ValueError('提现金额或网络费超出已确认范围，暂停自动放款')
