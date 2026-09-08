import pymysql
import logging
import sys
import os

# Get the script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
# Add the parent directory to sys.path
sys.path.append(os.path.dirname(script_dir))

import config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def add_is_active_column():
    """Add is_active column to server_boosts table if it doesn't exist."""
    connection = pymysql.connect(
        host=config.MYSQL_HOST,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        db=config.MYSQL_DATABASE,
        charset='utf8mb4'
    )
    
    try:
        with connection.cursor() as cursor:
            # Check if is_active column exists in server_boosts table
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'server_boosts' 
                AND COLUMN_NAME = 'is_active'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # Add is_active column
                print("Adding is_active column to server_boosts table...")
                cursor.execute("""
                    ALTER TABLE server_boosts
                    ADD COLUMN is_active BOOLEAN DEFAULT 1
                """)
                print("is_active column added successfully!")
                
                # Set default values: all existing records are active by default
                cursor.execute("""
                    UPDATE server_boosts
                    SET is_active = 1
                """)
                print("Updated all existing records to have is_active = 1")
            else:
                print("is_active column already exists, skipping this migration.")
            
            # Also check for last_credits_at column which might be missing
            cursor.execute("""
                SELECT COUNT(*) 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'server_boosts' 
                AND COLUMN_NAME = 'last_credits_at'
            """, (config.MYSQL_DATABASE,))
            
            if cursor.fetchone()[0] == 0:
                # Add last_credits_at column
                print("Adding last_credits_at column to server_boosts table...")
                cursor.execute("""
                    ALTER TABLE server_boosts
                    ADD COLUMN last_credits_at TIMESTAMP NULL
                """)
                print("last_credits_at column added successfully!")
            else:
                print("last_credits_at column already exists, skipping.")
        
        connection.commit()
        print("Database migration completed successfully.")
    
    except Exception as e:
        print(f"Error during database migration: {e}")
    
    finally:
        connection.close()

if __name__ == "__main__":
    add_is_active_column() 