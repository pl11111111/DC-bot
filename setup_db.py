import pymysql
import config

def create_database():
    """如果数据库和所需表不存在，则创建它们。"""
    # 不指定数据库连接到MySQL服务器
    connection = pymysql.connect(
        host=config.MYSQL_HOST,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        charset='utf8mb4'
    )
    
    try:
        with connection.cursor() as cursor:
            # 如果数据库不存在则创建
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {config.MYSQL_DATABASE}")
            cursor.execute(f"USE {config.MYSQL_DATABASE}")
            
            # 创建用户表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id BIGINT PRIMARY KEY,
                    username VARCHAR(100) NOT NULL,
                    free_escrow_count INT DEFAULT 0,
                    free_escrow_amount DECIMAL(18, 8) DEFAULT 0,
                    total_transactions INT DEFAULT 0,
                    active_transaction_id INT DEFAULT NULL,
                    active_rentals JSON DEFAULT NULL,
                    rental_count INT DEFAULT 0,
                    max_rentals INT DEFAULT 2,
                    payment_address VARCHAR(255) DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)
            
            # 创建交易表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    transaction_type ENUM('trade', 'rental') NOT NULL,
                    buyer_id BIGINT NOT NULL,
                    seller_id BIGINT NOT NULL,
                    item_name VARCHAR(255) NOT NULL,
                    amount DECIMAL(18, 8) NOT NULL,
                    escrow_fee DECIMAL(18, 8) DEFAULT 0,
                    status ENUM('pending', 'confirmed', 'shipped', 'completed', 'cancelled', 'disputed', 'returning','paying','rental_started','confirm_return') DEFAULT 'pending',
                    channel_id BIGINT NOT NULL,
                    payment_address VARCHAR(100),
                    txid VARCHAR(100),
                    unique_amount DECIMAL(18, 6) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP NULL,
                    completed_at TIMESTAMP NULL,
                    FOREIGN KEY (buyer_id) REFERENCES users(discord_id),
                    FOREIGN KEY (seller_id) REFERENCES users(discord_id)
                )
            """)
            
            # 创建租赁表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rentals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    transaction_id INT NOT NULL,
                    rental_period INT NOT NULL COMMENT '租赁期限',
                    rental_unit ENUM('hours', 'days') DEFAULT 'days' COMMENT '租赁期限单位',
                    deposit DECIMAL(18, 8) NOT NULL,
                    rental_fee DECIMAL(18, 8) NOT NULL,
                    start_date TIMESTAMP NULL,
                    end_date TIMESTAMP NULL,
                    returned BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                )
            """)
            
            # 创建邀请表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invitations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    inviter_id BIGINT NOT NULL,
                    invite_code VARCHAR(100) NOT NULL,  # 现在直接存储Discord邀请码
                    used BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    used_at TIMESTAMP NULL,
                    FOREIGN KEY (inviter_id) REFERENCES users(discord_id),
                    UNIQUE KEY (invite_code)  # 确保Discord邀请码唯一
                )
            """)
            
            # 注意：invite_links表已被移除，现在直接在invitations表中存储Discord邀请码
            
            # 创建服务器加速表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS server_boosts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    boost_count INT DEFAULT 1,
                    first_boost_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_boost_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(discord_id)
                )
            """)
            
            # 创建交易日志表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transaction_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    transaction_id INT NOT NULL,
                    action VARCHAR(100) NOT NULL,
                    actor_id BIGINT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
                    FOREIGN KEY (actor_id) REFERENCES users(discord_id)
                )
            """)
            
            # 创建持久化消息表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS persistent_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_type VARCHAR(50) NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY (message_type, channel_id)
                )
            """)
            
            # 创建邀请使用表，用于记录邀请的多次使用情况
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invitation_usage (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    invite_id INT NOT NULL,
                    invitee_id BIGINT NOT NULL,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    verified BOOLEAN DEFAULT FALSE,
                    verified_at TIMESTAMP NULL,
                    FOREIGN KEY (invite_id) REFERENCES invitations(id)
                )
            """)
            
            # 创建机器人设置表，用于存储各种机器人配置和数据
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    setting_key VARCHAR(100) NOT NULL,
                    setting_value TEXT,
                    user_id TEXT COMMENT '存储用户ID列表，通常为JSON格式',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY (setting_key)
                )
            """)
        
            # 创建抽奖表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    author_id BIGINT NOT NULL,
                    prize_name VARCHAR(100) NOT NULL,
                    prize_image VARCHAR(255),
                    winners_count INT NOT NULL DEFAULT 1,
                    winner_ids TEXT,
                    role_ids TEXT,
                    credit_requirement FLOAT NOT NULL DEFAULT 0,
                    message_id BIGINT,
                    channel_id BIGINT,
                    end_time DATETIME NOT NULL,
                    status ENUM('active', 'ended', 'cancelled') NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_status (status),
                    INDEX idx_author (author_id),
                    INDEX idx_end_time (end_time)
                )
            """)
            
            # 创建抽奖参与者表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    giveaway_id INT NOT NULL,
                    user_id BIGINT NOT NULL,
                    credits_used FLOAT NOT NULL DEFAULT 0,
                    joined_at DATETIME NOT NULL,
                    UNIQUE KEY unique_participation (giveaway_id, user_id),
                    INDEX idx_giveaway (giveaway_id),
                    INDEX idx_user (user_id),
                    FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
                )
            """)
        
            # 创建市场帖子表 (从main1.py迁移)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NULL,  # 去除过期自动删除
                    INDEX idx_user (user_id),
                    INDEX idx_channel (channel_id),
                    INDEX idx_message (message_id)
                )
            """)
            
            # 创建货币交易表 (从main1.py迁移)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS currency_trades (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    currency_type ENUM('NXPC', 'NESO') NOT NULL,
                    trade_type ENUM('sell', 'buy') NOT NULL,
                    quantity INT NOT NULL,
                    price DECIMAL(10, 2) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NULL,  # 去除过期自动删除
                    INDEX idx_user (user_id),
                    INDEX idx_currency (currency_type),
                    INDEX idx_trade_type (trade_type)
                )
            """)
            
            # 创建用户发帖限制表 (从main1.py迁移)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_post_limits (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    role_limit INT NOT NULL DEFAULT 0,
                    posts_today INT NOT NULL DEFAULT 0,
                    currency_posts_today INT NOT NULL DEFAULT 0,
                    last_post_date DATE NOT NULL,
                    UNIQUE KEY unique_user (user_id),
                    INDEX idx_user (user_id),
                    INDEX idx_date (last_post_date)
                )
            """)
            
            # 创建汇总消息表 (从main1.py迁移)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS summary_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_channel (channel_id)
                )
            """)
        
        connection.commit()
        print(f"数据库 '{config.MYSQL_DATABASE}' 和表创建成功。")
    
    except Exception as e:
        print(f"创建数据库时出错: {e}")
    
    finally:
        connection.close()

if __name__ == "__main__":
    create_database() 