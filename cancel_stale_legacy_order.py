"""Cancel one zero-fee pending trade with no payment/history evidence; preview by default."""
import argparse
import json
import re
from decimal import Decimal
import pymysql
import config


def validate(row,actions,rentals):
    if not row or row['transaction_type']!='trade' or row['status']!='pending':
        raise ValueError('Only an existing pending trade can be handled by this tool')
    if Decimal(str(row['escrow_fee'] or 0))!=0:
        raise ValueError('Nonzero service fee: credit/payment review required')
    if any(row.get(k) is not None for k in ('unique_amount','paid_at','completed_at')) or row.get('txid') or row.get('payment_address'):
        raise ValueError('Payment fields exist: refusing cancellation')
    if rentals or len(actions)!=1 or actions[0]['action']!='create':
        raise ValueError('History is not a single creation event; manual review required')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id',type=int,required=True)
    parser.add_argument('--expected-amount',type=Decimal,required=True)
    parser.add_argument('--apply',action='store_true',help='Apply after stopping the bot and backing up databases')
    args=parser.parse_args()
    if args.id<=0 or not args.expected_amount.is_finite() or args.expected_amount<=0:
        parser.error('Invalid ID or expected amount')
    shared=config.NEW.PAYMENTS_DATABASE
    new=config.NEW.MYSQL_DATABASE
    if not all(re.fullmatch(r'[A-Za-z0-9_]+',n) for n in (shared,new)):
        raise ValueError('Invalid database name')
    conn=pymysql.connect(host=config.MYSQL_HOST,port=config.MYSQL_PORT,user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,db=config.MYSQL_DATABASE,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM transactions WHERE id=%s FOR UPDATE',(args.id,))
            row=cur.fetchone()
            cur.execute('SELECT action FROM transaction_logs WHERE transaction_id=%s FOR UPDATE',(args.id,))
            actions=cur.fetchall()
            cur.execute('SELECT id FROM rentals WHERE transaction_id=%s FOR UPDATE',(args.id,))
            validate(row,actions,cur.fetchall())
            if row['amount']!=args.expected_amount:
                raise ValueError('Amount differs from the inspected order')
            key=f'legacy:{config.GUILD_ID}:{args.id}'
            for table in ('invoices','deposits','payouts','legacy_credits'):
                cur.execute(f'SELECT order_key FROM `{shared}`.`{table}` WHERE order_key IN (%s,%s,%s) FOR UPDATE',
                            (key,key+'_deposit',key+'_rental'))
                if cur.fetchone(): raise ValueError('Shared payment/credit ledger evidence exists; manual review required')
            print(f'Order {args.id}: pending, amount={row["amount"]}, no recorded payment or credit evidence.')
            if not args.apply:
                print('PREVIEW ONLY. No changes. Stop the bot and back up before repeating with --apply.')
                return
            cur.execute("UPDATE transactions SET status='cancelled',updated_at=NOW() WHERE id=%s AND status='pending'",(args.id,))
            if cur.rowcount!=1: raise ValueError('Order changed; refusing cancellation')
            cur.execute('UPDATE users SET active_transaction_id=NULL WHERE discord_id IN (%s,%s) AND active_transaction_id=%s',
                        (row['buyer_id'],row['seller_id'],args.id))
            cur.execute(f'INSERT INTO `{new}`.audit(actor_id,action,details) VALUES(0,%s,%s)',
                        ('legacy_stale_cancel',json.dumps({'legacy_guild':config.GUILD_ID,'legacy_order':args.id,
                        'from':'pending','to':'cancelled','amount':str(row['amount']),
                        'operator':'server maintenance script','reason':'Reviewed stale pre-payment trade; creation-only history'},ensure_ascii=False)))
        conn.commit()
        print('Cancelled the specified order and recorded maintenance audit. Historical records and funds were not deleted or transferred.')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        conn.close()


if __name__=='__main__': main()
