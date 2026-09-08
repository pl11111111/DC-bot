import pymysql
import config
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def migrate_database():
    """执行数据库迁移。"""
    connection = pymysql.connect(
        host=config.MYSQL_HOST,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        db=config.MYSQL_DATABASE,
        charset='utf8mb4'
    )
    
    try:
        with connection.cursor() as cursor:
            # 检查users表的payment_address列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'users' 
                AND COLUMN_NAME = 'payment_address'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加payment_address列
                print("添加payment_address列到users表...")
                cursor.execute("""
                    ALTER TABLE users
                    ADD COLUMN payment_address VARCHAR(255) DEFAULT NULL
                """)
                print("payment_address列添加成功！")
            else:
                print("payment_address列已存在，跳过此迁移。")
            
            # 检查active_rental_id列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'users' 
                AND COLUMN_NAME = 'active_rental_id'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加active_rental_id列
                print("添加active_rental_id列到users表...")
                cursor.execute("""
                    ALTER TABLE users
                    ADD COLUMN active_rental_id INT DEFAULT NULL
                    AFTER active_transaction_id
                """)
                print("active_rental_id列添加成功！")
            else:
                print("active_rental_id列已存在，跳过此迁移。")
            
            # 检查active_rentals列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'users' 
                AND COLUMN_NAME = 'active_rentals'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加active_rentals列
                print("添加active_rentals列到users表...")
                cursor.execute("""
                    ALTER TABLE users
                    ADD COLUMN active_rentals JSON DEFAULT NULL
                    AFTER active_rental_id
                """)
                print("active_rentals列添加成功！")
            else:
                print("active_rentals列已存在，跳过此迁移。")
            
            # 检查rental_count列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'users' 
                AND COLUMN_NAME = 'rental_count'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加rental_count列
                print("添加rental_count列到users表...")
                cursor.execute("""
                    ALTER TABLE users
                    ADD COLUMN rental_count INT DEFAULT 0
                    AFTER active_rentals
                """)
                print("rental_count列添加成功！")
            else:
                print("rental_count列已存在，跳过此迁移。")
            
            # 检查max_rentals列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'users' 
                AND COLUMN_NAME = 'max_rentals'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加max_rentals列
                print("添加max_rentals列到users表...")
                cursor.execute("""
                    ALTER TABLE users
                    ADD COLUMN max_rentals INT DEFAULT 2
                    AFTER rental_count
                """)
                print("max_rentals列添加成功！")
            else:
                print("max_rentals列已存在，跳过此迁移。")
            
            # 检查payment_tag列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'transactions' 
                AND COLUMN_NAME = 'payment_tag'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加payment_tag列
                print("添加payment_tag列到transactions表...")
                cursor.execute("""
                    ALTER TABLE transactions
                    ADD COLUMN payment_tag VARCHAR(100) NULL
                    AFTER payment_address
                """)
                print("payment_tag列添加成功！")
            else:
                print("payment_tag列已存在，跳过此迁移。")
            
            # 检查unique_amount列是否存在
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'transactions' 
                AND COLUMN_NAME = 'unique_amount'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # 添加unique_amount列
                print("添加unique_amount列到transactions表...")
                cursor.execute("""
                    ALTER TABLE transactions
                    ADD COLUMN unique_amount DECIMAL(18, 6) NULL
                    AFTER payment_tag
                """)
                print("unique_amount列添加成功！")
            else:
                print("unique_amount列已存在，跳过此迁移。")
            
            # 检查status枚举是否包含'returning'状态
            cursor.execute("""
                SELECT COLUMN_TYPE
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                AND table_name = 'transactions'
                AND column_name = 'status'
            """)
            
            status_type = cursor.fetchone()[0]
            if 'confirm_return' not in status_type:
                print("添加'returning'状态到transactions表的status枚举...")
                cursor.execute("""
                    ALTER TABLE transactions
                    MODIFY COLUMN status ENUM('pending', 'confirmed', 'paid', 'shipped', 'completed', 'cancelled', 'disputed', 'returning','paying','rental_started','confirm_return')
                    DEFAULT 'pending'
                """)
                print("status枚举更新成功！")
            else:
                print("status枚举已包含'returning'状态，跳过此迁移。")
        
        connection.commit()
        print("数据库迁移完成。")
    
    except Exception as e:
        print(f"执行数据库迁移时出错: {e}")
    
    finally:
        connection.close()

if __name__ == "__main__":
    migrate_database() 