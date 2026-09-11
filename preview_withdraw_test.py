"""Read-only Binance USDT/BSC fee preview. This script cannot submit a withdrawal."""
import hashlib
import hmac
import json
import os
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.parse import urlencode
import requests
from dotenv import load_dotenv

ADDRESS='0xeC33AE5f5Ea0Cc6204e3287A3cdB396Ed5Ae17EE'
ENDPOINT='/sapi/v1/capital/config/getall'


def preview(network):
    if network.get('network')!='BSC' or network.get('withdrawEnable') is not True:
        raise ValueError('BSC withdrawals are unavailable; no test proposed.')
    if network.get('withdrawTag'):
        raise ValueError('Unexpected tag requirement; manual verification required.')
    fee=Decimal(str(network['withdrawFee']))
    minimum=Decimal(str(network['withdrawMin']))
    step=Decimal(str(network['withdrawIntegerMultiple']))
    if not all(v.is_finite() for v in (fee,minimum,step)) or fee<0 or minimum<0 or step<=0:
        raise ValueError('Invalid fee, minimum or precision returned by Binance.')
    # Leave enough for the minimum received amount under either interpretation.
    amount=(max(minimum+fee,step)/step).to_integral_value(rounding=ROUND_CEILING)*step
    maximum=Decimal(str(network['withdrawMax'])) if network.get('withdrawMax') is not None else None
    if maximum is not None and (not maximum.is_finite() or amount>maximum):
        raise ValueError('Candidate exceeds withdrawal limit; no test proposed.')
    return {'mode':'READ_ONLY_PREVIEW_NO_TRANSFER','coin':'USDT','network':'BSC',
            'destination':ADDRESS,'network_fee':str(fee),'minimum':str(minimum),'step':str(step),
            'candidate_request_amount':str(amount),
            'if_fee_included':{'account_debit':str(amount),'recipient_amount':str(amount-fee)},
            'if_fee_extra':{'account_debit':str(amount+fee),'recipient_amount':str(amount)},
            'fee_semantics_can_be_distinguished':fee>0,
            'note':'Estimates only. No balance check, ownership check, or withdrawal has been performed.'}


def main():
    load_dotenv(Path(__file__).resolve().parent/'.env')
    key=os.getenv('BINANCE_API_KEY','')
    secret=os.getenv('BINANCE_API_SECRET','')
    if not key or not secret:
        raise ValueError('Missing BINANCE_API_KEY / BINANCE_API_SECRET in the server environment.')
    params={'timestamp':str(int(time.time()*1000)),'recvWindow':'5000'}
    query=urlencode(params)
    params['signature']=hmac.new(secret.encode(),query.encode(),hashlib.sha256).hexdigest()
    try:
        response=requests.get('https://api.binance.com'+ENDPOINT,params=params,
                              headers={'X-MBX-APIKEY':key},timeout=20,allow_redirects=False)
    except requests.RequestException:
        raise ValueError('Read-only request failed. Check connectivity; no transfer was submitted.') from None
    try:
        data=response.json()
    except ValueError:
        raise ValueError(f'Invalid API response (HTTP {response.status_code}); no transfer submitted.') from None
    if response.status_code!=200 or not isinstance(data,list):
        code=data.get('code','unknown') if isinstance(data,dict) else 'unknown'
        raise ValueError(f'Binance read-only request rejected: HTTP {response.status_code}, code={code}. No transfer submitted.')
    coin=next((c for c in data if c.get('coin')=='USDT'),None)
    network=next((n for n in coin.get('networkList',[]) if n.get('network')=='BSC'),None) if coin else None
    if not network: raise ValueError('USDT/BSC configuration not returned; no transfer submitted.')
    print(json.dumps(preview(network),indent=2,ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except (ValueError,KeyError,ArithmeticError) as exc:
        print('Preview failed:',str(exc))
        raise SystemExit(1)
