import os
from dotenv import load_dotenv

# 从.env文件加载环境变量
load_dotenv()

# Discord机器人配置
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))

# 数据库配置
MYSQL_HOST = os.getenv('MYSQL_HOST', 'localhost')
MYSQL_USER = os.getenv('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'trade_bot')

# Redis配置
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# Binance API配置
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# 托管配置
ESCROW_FEE = 1.0  # 托管服务收取固定2 USDT费用
MIN_AMOUNT_FOR_FEE = 0  # 0 USDT以下的交易不收费
RENTAL_MIN_AMOUNT_FOR_FEE = 0
PAYMENT_TIMEOUT = 30 * 60  # 30分钟，以秒为单位

# 频道分类
ADMIN_LOGS_CHANNEL = int(os.getenv('ADMIN_LOGS_CHANNEL', '0'))
TRADE_CATEGORY_ID = int(os.getenv('TRADE_CATEGORY_ID', '0'))
RENTAL_CATEGORY_ID = int(os.getenv('RENTAL_CATEGORY_ID', '0'))

# 角色
FREE_ESCROW_ROLE_ID = int(os.getenv('FREE_ESCROW_ROLE_ID', 0))
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID', '0'))
MODERATOR_ROLE_ID = int(os.getenv('MODERATOR_ROLE_ID', '0'))
BOOSTER_ROLE_ID = int(os.getenv('BOOSTER_ROLE_ID', '0'))
GRANT_ELIGIBLE_ROLE_ID = int(os.getenv('GRANT_ELIGIBLE_ROLE_ID', '0'))
VERIFIED_ROLE_ID = int(os.getenv('VERIFIED_ROLE_ID', '0'))  # 已验证用户的身份组ID
VIP_ROLE_ID = int(os.getenv('VIP_ROLE_ID', '0'))

# 多租赁权限角色
MULTI_RENTAL_ROLE_IDS = [ADMIN_ROLE_ID, GRANT_ELIGIBLE_ROLE_ID, VIP_ROLE_ID]
MAX_RENTALS_DEFAULT = 2  # 默认最大租赁数量
MAX_RENTALS_BOOSTER = 3  # 助力用户最大租赁数量
MAX_RENTALS_SPECIAL = 6  # MULTI_RENTAL_ROLE_IDS中角色的最大租赁数量

# 频道ID
INVITATION_CHANNEL_ID = int(os.getenv('INVITATION_CHANNEL_ID', 0))  # 用于发送邀请按钮的频道ID
RANKING_CHANNEL_ID = int(os.getenv('RANKING_CHANNEL_ID', 0))  # 用于显示邀请排行榜的频道ID
INVITE_BUTTON_CHECK_INTERVAL = int(os.getenv('INVITE_BUTTON_CHECK_INTERVAL', 3600))  # 默认每小时检查一次

# 市场模块配置 (从main1.py迁移)
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID', '0'))  # 允许发送交易命令的频道
FORUM_CHANNEL_ID = int(os.getenv('FORUM_CHANNEL_ID', '0'))  # 论坛频道ID
CURRENCY_CHANNEL_ID = int(os.getenv('CURRENCY_CHANNEL_ID', '0'))  # 货币交易汇总频道

# 市场角色限制 (从main1.py迁移)
ROLE_LIMITS = {
    int(os.getenv('BOT_ADMIN_ROLE2', '0')): 4,
    int(os.getenv('BOT_ADMIN_ROLE', '0')): 4,  # 特殊角色
    int(os.getenv('LV5_ROLE', '0')): 3, 
    int(os.getenv('LV4_ROLE', '0')): 2, 
    int(os.getenv('LV3_ROLE', '0')): 2, 
    int(os.getenv('LV3_ROLE', '0')): 1,   # LV3角色
    int(os.getenv('LV1_ROLE', '0')): 1     # LV1角色
}

# 市场LV3角色ID列表 (从main1.py迁移)
LV3_ROLE_IDS = [int(id.strip()) for id in os.getenv('LV3_ROLE_IDS', '0').split(',') if id.strip()]

# 市场机器人管理角色 (从main1.py迁移)
BOT_ADMIN_ROLE = int(os.getenv('BOT_ADMIN_ROLE', '0'))

# 市场允许发送链接的频道白名单 (从main1.py迁移)
ALLOWED_CHANNELS = [int(id.strip()) for id in os.getenv('ALLOWED_LINK_CHANNELS', '').split(',') if id.strip()]

# 市场链接安全配置 (从main1.py迁移)
ALLOW_IMAGE_LINKS = os.getenv('ALLOW_IMAGE_LINKS', 'True').lower() in ('true', '1', 't', 'yes')
WHITELISTED_DOMAINS = os.getenv('WHITELISTED_DOMAINS', 'discord.com,discord.gg,msu.io,x.com,youtube.com').split(',')

# 排名身份组
TOP1_INVITER_ROLE_ID = int(os.getenv('TOP1_INVITER_ROLE_ID', '0'))
TOP2_INVITER_ROLE_ID = int(os.getenv('TOP2_INVITER_ROLE_ID', '0'))
TOP3_INVITER_ROLE_ID = int(os.getenv('TOP3_INVITER_ROLE_ID', '0'))

# 频道配置
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))  # 日志频道
INVITE_CHANNEL_ID = int(os.getenv('INVITE_CHANNEL_ID', '0'))  # 用于创建邀请链接的频道ID
DELETED_MESSAGES_LOG_CHANNEL_ID = int(os.getenv('DELETED_MESSAGES_LOG_CHANNEL_ID', '0'))  # 用于记录被删除消息的频道ID
GIVEAWAY_CHANNEL_IDS = [int(id) for id in os.getenv('GIVEAWAY_CHANNEL_IDS', '0').split(',')] if os.getenv('GIVEAWAY_CHANNEL_IDS') else []  # 允许创建抽奖的频道ID列表

# 积分配置
INVITE_FREE_ESCROW_THRESHOLD = int(os.getenv('INVITE_FREE_ESCROW_THRESHOLD', 3))  # 每邀请多少人获得积分
INVITE_FREE_ESCROW_AMOUNT = float(os.getenv('INVITE_FREE_ESCROW_AMOUNT', 2.0))  # 每次获得的积分
FREE_CREDITS_AMOUNT = float(os.getenv('FREE_CREDITS_AMOUNT', 6.0))  # 服务器助力奖励的积分

# 组队模块配置 (从xxz模块迁移)
VOICE_CATEGORY = os.getenv('VOICE_CATEGORY', '语音频道')  # 语音频道分类名称
NOTICE_CHANNEL_ID = int(os.getenv('NOTICE_CHANNEL_ID', '0'))  # 组队通知频道ID
PARTY_COMMAND_CHANNEL = int(os.getenv('PARTY_COMMAND_CHANNEL', '0'))  # 允许使用组队命令的频道ID
ALL_ROLE_ID = int(os.getenv('ALL_ROLE_ID', '0'))  # 所有用户角色ID
GUILD_ROLE_ID = int(os.getenv('GUILD_ROLE_ID', '0'))  # 公会用户角色ID
GUILD_NOTICE_CHANNEL_ID = int(os.getenv('GUILD_NOTICE_CHANNEL_ID', '0'))  # 公会组队通知频道ID

# 检查必需的配置
if not all([DISCORD_TOKEN, GUILD_ID, BINANCE_API_KEY, BINANCE_API_SECRET]):
    raise ValueError("缺少必需的环境变量。请检查您的.env文件。") 