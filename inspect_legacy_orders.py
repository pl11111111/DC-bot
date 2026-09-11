"""Read-only migration diagnostics. Never changes orders or contacts an exchange."""
import argparse
import json
import pymysql
import config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-id',type=int,default=0)
    parser.add_argument('--limit',type=int,default=50)
    args=parser.parse_args()
    if args.after_id<0 or not 1<=args.limit<=200:
        parser.error('after-id must be nonnegative; limit must be 1..200')
    conn=pymysql.connect(host=config.MYSQL_HOST,port=config.MYSQL_PORT,
        user=config.MYSQL_USER,password=config.MYSQL_PASSWORD,db=config.MYSQL_DATABASE,
        charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SHOW COLUMNS FROM transactions')
            columns={r['Field'] for r in cur.fetchall()}
            cur.execute("SELECT status,COUNT(*) AS count FROM transactions WHERE status NOT IN ('completed','cancelled') OR status IS NULL GROUP BY status")
            print('Blocking status counts:',json.dumps(cur.fetchall(),ensure_ascii=False,default=str))
            fields=[x for x in ('id','transaction_type','status','amount','escrow_fee','unique_amount',
                               'created_at','updated_at','paid_at','completed_at') if x in columns]
            for column in ('txid','payment_address'):
                if column in columns:
                    fields.append(f"CASE WHEN `{column}` IS NOT NULL AND `{column}`<>'' THEN 1 ELSE 0 END AS has_{column}")
            cur.execute('SELECT '+','.join(fields)+" FROM transactions WHERE (status NOT IN ('completed','cancelled') OR status IS NULL) AND id>%s ORDER BY id LIMIT %s",
                        (args.after_id,args.limit))
            rows=cur.fetchall()
            cur.execute('SHOW TABLES')
            tables={next(iter(r.values())) for r in cur.fetchall()}
            for row in rows:
                if 'rentals' in tables:
                    cur.execute('SHOW COLUMNS FROM rentals')
                    rental_columns={r['Field'] for r in cur.fetchall()}
                    selected=[c for c in ('deposit','rental_fee','start_date','end_date','returned') if c in rental_columns]
                    if selected and 'transaction_id' in rental_columns:
                        cur.execute('SELECT '+','.join(selected)+' FROM rentals WHERE transaction_id=%s',(row['id'],))
                        row['rental_records']=cur.fetchall()
                if 'transaction_logs' in tables:
                    cur.execute('SHOW COLUMNS FROM transaction_logs')
                    log_columns={r['Field'] for r in cur.fetchall()}
                    if {'transaction_id','action'}.issubset(log_columns):
                        cur.execute('SELECT action,COUNT(*) AS count FROM transaction_logs WHERE transaction_id=%s GROUP BY action',(row['id'],))
                        row['logged_actions']=cur.fetchall()
                print(json.dumps(row,ensure_ascii=False,default=str))
            if rows:
                print(f'If more records remain, run again with --after-id {rows[-1]["id"]}')
            print('READ ONLY. Missing payment fields do NOT prove that no deposit or withdrawal occurred.')
    finally:
        conn.rollback()
        conn.close()


if __name__=='__main__': main()
