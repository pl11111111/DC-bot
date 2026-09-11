"""One explicitly confirmed USDT/BSC withdrawal; subsequent runs only query it."""
import argparse
import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv
from preview_withdraw_test import ADDRESS

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'withdraw-test-state.json'
AMOUNT=Decimal('3.01')
EXPECTED_FEE=Decimal('0.01')


def api(method,path,params=None):
    key=os.getenv('BINANCE_API_KEY','')
    secret=os.getenv('BINANCE_API_SECRET','')
    if not key or not secret: raise ValueError('Missing server API credentials')
    values=dict(params or {},timestamp=str(int(time.time()*1000)),recvWindow='5000')
    values['signature']=hmac.new(secret.encode(),urlencode(values).encode(),hashlib.sha256).hexdigest()
    kwargs={'params':values} if method=='GET' else {'data':values}
    try:
        response=requests.request(method,'https://api.binance.com'+path,
            headers={'X-MBX-APIKEY':key},timeout=30,allow_redirects=False,**kwargs)
        data=response.json()
    except (requests.RequestException,ValueError):
        raise ValueError(f'API response unavailable: {method} {path}. Do not resubmit a withdrawal; use status.') from None
    if response.status_code!=200:
        code=data.get('code','unknown') if isinstance(data,dict) else 'unknown'
        raise ValueError(f'API rejected request: {method} {path}, HTTP {response.status_code}, code={code}. Use status if submission was attempted.')
    return data


def balance():
    data=api('GET','/api/v3/account')
    entry=next((v for v in data['balances'] if v['asset']=='USDT'),None)
    if entry is None: raise ValueError('Spot USDT balance unavailable')
    free,locked=Decimal(entry['free']),Decimal(entry['locked'])
    if not free.is_finite() or not locked.is_finite(): raise ValueError('Invalid balance')
    return {'free':str(free),'locked':str(locked),'total':str(free+locked)}


def check_fee():
    coins=api('GET','/sapi/v1/capital/config/getall')
    coin=next((c for c in coins if c.get('coin')=='USDT'),None)
    network=next((n for n in coin.get('networkList',[]) if n.get('network')=='BSC'),None) if coin else None
    if not network or network.get('withdrawEnable') is not True or network.get('withdrawTag'):
        raise ValueError('USDT/BSC withdrawal unavailable')
    fee=Decimal(str(network['withdrawFee']))
    minimum=Decimal(str(network['withdrawMin']))
    step=Decimal(str(network['withdrawIntegerMultiple']))
    if not all(x.is_finite() for x in (fee,minimum,step)) or step<=0 or minimum<0:
        raise ValueError('Invalid network limits')
    if fee!=EXPECTED_FEE or AMOUNT-fee<minimum or AMOUNT%step!=0:
        raise ValueError('Fee/minimum/precision changed. Stop and obtain a new preview.')
    maximum=network.get('withdrawMax')
    if maximum is not None and AMOUNT>Decimal(str(maximum)):
        raise ValueError('Withdrawal maximum exceeded')


def create_intent(record):
    # Exclusive file creation prevents a second process from submitting the same test.
    fd=os.open(str(STATE),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        json.dump(record,stream,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name!='nt':
        fd=os.open(str(ROOT),os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def submit():
    if STATE.exists():
        raise ValueError('A test intent already exists. Do NOT delete it. Run: withdraw_test_once.py status')
    check_fee()
    print(f'Destination: {ADDRESS}\nNetwork: USDT / BSC\nRequest amount: {AMOUNT}\nCurrent fee: {EXPECTED_FEE}')
    print('Estimated account debit: 3.01 or 3.02 USDT; this is a real transfer, not a simulation.')
    print('Stop discordbot and all other account transfers/trades during measurement.')
    phrase='SEND 3.01 USDT TO '+ADDRESS
    print('Only proceed if this is your own external BSC wallet. To confirm, type exactly:\n'+phrase)
    if input('> ').strip()!=phrase:
        print('Cancelled. No withdrawal submitted.')
        return
    # Refresh after the human confirmation, not before an unbounded wait.
    check_fee()
    before=balance()
    if Decimal(before['free'])<AMOUNT+EXPECTED_FEE:
        raise ValueError('Insufficient spot USDT for the larger estimated debit; no submission')
    record={'request_id':'fee-test-'+uuid.uuid4().hex,'created_ms':int(time.time()*1000),
            'address':ADDRESS,'amount':str(AMOUNT),'quoted_fee':str(EXPECTED_FEE),
            'wallet_type':0,'before':before,'state':'submission_intent'}
    create_intent(record)
    # Never retry this POST, regardless of response or process interruption.
    result=api('POST','/sapi/v1/capital/withdraw/apply',
        {'coin':'USDT','network':'BSC','address':ADDRESS,'amount':str(AMOUNT),
         'withdrawOrderId':record['request_id'],'walletType':'0','transactionFeeFlag':'true'})
    print(json.dumps({'submission_response_id':result.get('id') if isinstance(result,dict) else None,
                      'request_id':record['request_id'],'next':'Run status; do not submit again.'},indent=2))


def status():
    record=json.loads(STATE.read_text(encoding='utf-8'))
    rows=api('GET','/sapi/v1/capital/withdraw/history',
        {'coin':'USDT','withdrawOrderId':record['request_id'],
         'startTime':record['created_ms']-60000,
         'endTime':min(int(time.time()*1000),record['created_ms']+6*86400000)})
    if not isinstance(rows,list): raise ValueError('Unexpected withdrawal history response')
    matches=[r for r in rows if r.get('withdrawOrderId')==record['request_id']]
    after=balance()
    result={'request_amount':record['amount'],'quoted_fee':record['quoted_fee'],
            'request_id':record['request_id'],'matching_records':len(matches),
            'spot_usdt_total_decrease':str(Decimal(record['before']['total'])-Decimal(after['total'])),
            'note':'Balance difference is evidence only if no other account activity occurred. Verify final wallet receipt separately.'}
    if len(matches)==1:
        row=matches[0]
        if row.get('address','').lower()!=record['address'].lower() or row.get('coin')!='USDT' or row.get('network')!='BSC':
            raise ValueError('History destination/coin/network mismatch; manual review required')
        result['withdrawal']={k:row.get(k) for k in ('status','amount','transactionFee','transferType','txId','applyTime','completeTime')}
        result['completed']=row.get('status')==6
    else:
        result['completed']=False
        result['next']='Wait and query status again. Missing history does not authorize resubmission.'
    print(json.dumps(result,indent=2,ensure_ascii=False))


def diagnose():
    """GET-only checks; never submit, replace, or remove a test intent."""
    output={'mode':'READ_ONLY_DIAGNOSIS','test_intent_exists':STATE.exists()}
    try:
        check_fee()
        output['fee_query']='OK'
    except (ValueError,KeyError,ArithmeticError) as exc:
        output['fee_query']=str(exc)
    try:
        balance()
        output['spot_balance_query']='OK (balance omitted)'
    except (ValueError,KeyError,ArithmeticError) as exc:
        output['spot_balance_query']=str(exc)
    try:
        data=api('GET','/sapi/v1/account/apiRestrictions')
        output['api_permissions']={k:data.get(k) for k in
            ('enableReading','enableWithdrawals','ipRestrict','enableSpotAndMarginTrading')}
    except (ValueError,KeyError,ArithmeticError,AttributeError) as exc:
        output['api_permissions']=str(exc)
    print(json.dumps(output,indent=2,ensure_ascii=False))


def main():
    load_dotenv(ROOT/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['submit','status','diagnose'])
    args=parser.parse_args()
    if args.action=='submit': submit()
    elif args.action=='status': status()
    else: diagnose()


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError,ArithmeticError,EOFError) as exc:
        print('Stopped:',str(exc))
        print('If withdraw-test-state.json exists, preserve it and use status. Never delete it to retry.')
        raise SystemExit(1)
