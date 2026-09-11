"""Offline cutover, after backup and service stop. Refuses active legacy orders."""
import hashlib
import pymysql
import config
from setup_new_db import main as install

def main():
    install()
    conn=pymysql.connect(host=config.MYSQL_HOST,port=config.MYSQL_PORT,user=config.MYSQL_USER,password=config.MYSQL_PASSWORD,
                         db=config.MYSQL_DATABASE,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    shared=config.NEW.PAYMENTS_DATABASE
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE status NOT IN ('completed','cancelled')")
            if cur.fetchone()['n']:
                raise RuntimeError('旧社群存在未结束的交易/租赁。先完成并核对资金，再停服务执行切换。')
            cur.execute('SELECT id,unique_amount,payment_address,txid FROM transactions')
            rows=cur.fetchall()
            # Reserve all historical amounts so late deposits never match a new invoice.
            for row in rows:
                key=f'legacy:{config.GUILD_ID}:{row["id"]}'
                if row['unique_amount'] and row['payment_address']:
                    cur.execute(f"INSERT IGNORE INTO `{shared}`.invoices(order_key,address,amount,expires_at,state) VALUES(%s,%s,%s,UTC_TIMESTAMP(),'historical')",(key,row['payment_address'],row['unique_amount']))
                for suffix in ('','_deposit','_rental'):
                    payout_key=key+suffix
                    # Permanent block for pre-cutover closed orders. No invented completion ID.
                    cur.execute(f"INSERT IGNORE INTO `{shared}`.payouts(order_key,request_id,address,gross,fee,net,state) VALUES(%s,%s,'historical',0,0,0,'historical')",(payout_key,hashlib.sha256(payout_key.encode()).hexdigest()[:32]))
            # Preserve existing ENUM members; add the state already used by legacy code.
            cur.execute("SHOW COLUMNS FROM transactions LIKE 'status'")
            column=cur.fetchone()
            enum=column['Type']
            if enum.startswith('enum(') and "'confirmed_receipt'" not in enum:
                enum=enum[:-1]+",'confirmed_receipt')"
                cur.execute(f'ALTER TABLE transactions MODIFY COLUMN status {enum} DEFAULT \'pending\'')
            cur.execute(f"INSERT INTO `{shared}`.payment_settings(setting_key,value) VALUES('legacy_guild',%s) ON DUPLICATE KEY UPDATE value=VALUES(value)",(str(config.GUILD_ID),))
            conn.commit()
        print('Cutover baseline ready. Enable SHARED_PAYMENTS_ENABLED only after this succeeds.')
    finally:
        conn.close()

if __name__=='__main__': main()
