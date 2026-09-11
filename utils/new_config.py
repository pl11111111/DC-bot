"""Validated, opt-in settings for the second guild. Never fall back to legacy IDs."""
import os
import logging
from decimal import Decimal

log = logging.getLogger(__name__)

def number(name, default=0):
    value = os.getenv(name, str(default)).strip()
    try:
        result = int(value or default)
        if result < 0:
            raise ValueError()
        return result
    except ValueError:
        log.error("Invalid numeric setting: %s (feature disabled)", name)
        return 0

def ids(name):
    try:
        return tuple(int(x.strip()) for x in os.getenv(name, '').split(',') if x.strip() and int(x.strip()) > 0)
    except ValueError:
        log.error("Invalid ID list: %s", name)
        return ()

def mapping(name):
    try:
        pairs = [x.strip().split(':') for x in os.getenv(name, '').split(',') if x.strip()]
        return {int(k): int(v) for k, v in pairs if int(k) > 0 and int(v) > 0}
    except ValueError:
        log.error("Invalid forum/tag mapping: %s", name)
        return {}

GUILD_ID = number('NEW_GUILD_ID')
FORUM_IDS = ids('NEW_TRADE_FORUM_IDS')
BUY_TAGS = mapping('NEW_TRADE_BUY_TAG_MAP')
SELL_TAGS = mapping('NEW_TRADE_SELL_TAG_MAP')
TRADE_CATEGORY_ID = number('NEW_TRADE_CATEGORY_ID')
LOG_CHANNEL_ID = number('NEW_TRADE_LOG_CHANNEL_ID')
ALERT_CHANNEL_ID = number('NEW_ALERT_CHANNEL_ID')
VERIFY_CHANNEL_ID = number('NEW_VERIFY_CHANNEL_ID')
LANGUAGE_CHANNEL_ID = number('NEW_LANGUAGE_CHANNEL_ID')
VERIFIED_ROLE_ID = number('NEW_INVITE_VERIFIED_ROLE_ID')
RANKING_CHANNEL_ID = number('NEW_INVITE_RANKING_CHANNEL_ID')
TRADE_STATS_CHANNEL_ID = number('NEW_TRADE_STATS_CHANNEL_ID')
INVITE_CHANNEL_ID = number('NEW_INVITE_CHANNEL_ID')
TRADE_ADMIN_ROLES = ids('NEW_TRADE_ADMIN_ROLE_IDS')
NOTICE_ADMIN_ROLES = ids('NEW_NOTICE_ADMIN_ROLE_IDS')
LV3_ROLE_IDS = ids('NEW_LV3_ROLE_IDS')
VOICE_CREATE_CHANNEL_ID = number('NEW_VOICE_CREATE_CHANNEL_ID')
VOICE_CATEGORY_ID = number('NEW_VOICE_CATEGORY_ID')
LANGUAGES = {
    label: number('NEW_LANGUAGE_' + key + '_ROLE_ID')
    for key, label in [('CHINESE','中文'), ('TAGALOG','Tagalog'),
        ('INDONESIAN','Bahasa Indonesia'), ('JAPANESE','日本語'),
        ('KOREAN','한국어'), ('MALAY','Bahasa Melayu'), ('PORTUGUESE','Português'),
        ('SPANISH','Español'), ('THAI','ภาษาไทย'), ('VIETNAMESE','Tiếng Việt')]
}
MYSQL_DATABASE = os.getenv('NEW_MYSQL_DATABASE', 'trade_bot_new').strip()
PAYMENTS_DATABASE = os.getenv('PAYMENTS_MYSQL_DATABASE', 'trade_bot_payments').strip()
FEE = Decimal('2.00')
MAX_ACTIVE = number('NEW_MAX_ACTIVE_TRADES', 5)
LOG_SOFT_BYTES = 2_000_000_000
LOG_TARGET_BYTES = 1_500_000_000
LOG_WARN_BYTES = 2_700_000_000
LOG_HARD_BYTES = 3_000_000_000
PAYMENTS_ENABLED = os.getenv('SHARED_PAYMENTS_ENABLED', 'false').lower() == 'true'
