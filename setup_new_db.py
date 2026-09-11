"""Run manually after backup; creates ONLY the two new databases/tables."""
import re
import pymysql
import config
from utils.new_store import NEW_SCHEMA, SHARED_SCHEMA

def main():
    names = [config.NEW.MYSQL_DATABASE, config.NEW.PAYMENTS_DATABASE]
    if len(set(names + [config.MYSQL_DATABASE])) != 3:
        raise ValueError('New, shared payment, and legacy database names must be different')
    if not all(re.fullmatch(r'[A-Za-z0-9_]+', n) for n in names):
        raise ValueError('Invalid database name')
    conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER, password=config.MYSQL_PASSWORD, charset='utf8mb4')
    try:
        with conn.cursor() as cur:
            for name, schema in zip(names, [NEW_SCHEMA, SHARED_SCHEMA]):
                cur.execute(f'CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4')
                cur.execute(f'USE `{name}`')
                for statement in schema:
                    cur.execute(statement + ' ENGINE=InnoDB')
        conn.commit()
        print('New guild and shared payment schemas installed; legacy tables untouched.')
    finally:
        conn.close()

if __name__ == '__main__':
    main()
