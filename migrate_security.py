"""Install the credit settlement ledger. Does not infer historical settlements."""
import pymysql
import config
from utils.giveaway_credits import SCHEMA


def main():
    connection = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
        user=config.MYSQL_USER, password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE, charset='utf8mb4', autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT TABLE_NAME FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ('users','giveaways','giveaway_participants')
                AND ENGINE<>'InnoDB'""", (config.MYSQL_DATABASE,))
            if cursor.fetchall():
                raise ValueError('Credit tables must use InnoDB before this migration')
            cursor.execute(SCHEMA)
        print('Security ledger installed. Historical giveaways require manual reconciliation.')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
