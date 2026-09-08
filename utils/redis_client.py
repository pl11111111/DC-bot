import redis.asyncio as redis
import json
import logging
from typing import Any, Optional, Dict, List
import config
import asyncio
import uuid

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Redis客户端
redis_client = None

async def get_redis():
    """获取Redis客户端连接。"""
    global redis_client
    if redis_client is None:
        try:
            redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                password=config.REDIS_PASSWORD,
                db=config.REDIS_DB,
                decode_responses=True
            )
            # 测试连接
            await redis_client.ping()
            logger.info("Redis连接成功")
        except Exception as e:
            logger.error(f"Redis连接错误: {e}")
            raise
    return redis_client

async def set_value(key: str, value: Any, expiry: int = None) -> bool:
    """在Redis中设置值，可选过期时间（秒）。"""
    try:
        client = await get_redis()
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        if expiry:
            await client.setex(key, expiry, value)
        else:
            await client.set(key, value)
        return True
    except Exception as e:
        logger.error(f"设置Redis键 {key} 时出错: {e}")
        return False

async def get_value(key: str, as_json: bool = False) -> Optional[Any]:
    """从Redis获取值，可选解析为JSON。"""
    try:
        client = await get_redis()
        value = await client.get(key)
        if value and as_json:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                logger.error(f"解析JSON时出错，键 {key}，值: {value}")
                return None
        return value
    except Exception as e:
        logger.error(f"获取Redis键 {key} 时出错: {e}")
        return None

async def delete_value(key: str) -> bool:
    """从Redis删除值。"""
    try:
        client = await get_redis()
        await client.delete(key)
        return True
    except Exception as e:
        logger.error(f"删除Redis键 {key} 时出错: {e}")
        return False

async def increment(key: str, amount: int = 1) -> int:
    """在Redis中增加数值。"""
    try:
        client = await get_redis()
        return await client.incrby(key, amount)
    except Exception as e:
        logger.error(f"增加Redis键 {key} 时出错: {e}")
        return 0

async def set_hash(key: str, values: Dict[str, Any]) -> bool:
    """在Redis中设置哈希。"""
    try:
        client = await get_redis()
        for field, value in values.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            await client.hset(key, field, value)
        return True
    except Exception as e:
        logger.error(f"设置Redis哈希 {key} 时出错: {e}")
        return False

async def get_hash(key: str) -> Dict[str, Any]:
    """从Redis获取哈希的所有字段。"""
    try:
        client = await get_redis()
        return await client.hgetall(key)
    except Exception as e:
        logger.error(f"获取Redis哈希 {key} 时出错: {e}")
        return {}

async def get_hash_field(key: str, field: str, as_json: bool = False) -> Optional[Any]:
    """从Redis哈希获取特定字段。"""
    try:
        client = await get_redis()
        value = await client.hget(key, field)
        if value and as_json:
            return json.loads(value)
        return value
    except Exception as e:
        logger.error(f"获取Redis哈希字段 {key}.{field} 时出错: {e}")
        return None

async def set_expiry(key: str, seconds: int) -> bool:
    """设置键的过期时间。"""
    try:
        client = await get_redis()
        await client.expire(key, seconds)
        return True
    except Exception as e:
        logger.error(f"设置Redis键 {key} 的过期时间时出错: {e}")
        return False

# 不同缓存类型的键前缀
USER_PREFIX = "user:"
TRANSACTION_PREFIX = "transaction:"
PAYMENT_PREFIX = "payment:"
CHANNEL_PREFIX = "channel:"
INVITE_PREFIX = "invite:"

# 存款认领相关键的前缀
CLAIMED_DEPOSIT_PREFIX = "claimed_deposit:"
LOCK_PREFIX = "lock:"

# 特定缓存需求的辅助函数
async def cache_user(discord_id: int, user_data: Dict, expiry: int = 3600) -> bool:
    """缓存用户数据，默认1小时过期。"""
    key = f"{USER_PREFIX}{discord_id}"
    return await set_value(key, user_data, expiry)

async def get_cached_user(discord_id: int) -> Optional[Dict]:
    """获取缓存的用户数据。"""
    key = f"{USER_PREFIX}{discord_id}"
    return await get_value(key, as_json=True)

async def cache_transaction(transaction_id: int, data: Dict) -> bool:
    """缓存交易数据。"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}"
        await client.set(key, json.dumps(data), ex=3600)  # 1小时过期
        return True
    except Exception as e:
        logger.error(f"缓存交易 {transaction_id} 时出错: {e}")
        return False

async def get_cached_transaction(transaction_id: int) -> Optional[Dict]:
    """获取缓存的交易数据。"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}"
        data = await client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.error(f"获取缓存交易 {transaction_id} 时出错: {e}")
        return None

async def delete_cached_transaction(transaction_id: int) -> bool:
    """删除缓存的交易数据。"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}"
        await client.delete(key)
        return True
    except Exception as e:
        logger.error(f"删除缓存交易 {transaction_id} 时出错: {e}")
        return False

async def cache_channel_transaction(channel_id: int, transaction_id: int, expiry: int = 86400) -> bool:
    """缓存频道到交易的映射，默认24小时过期。"""
    key = f"{CHANNEL_PREFIX}{channel_id}"
    return await set_value(key, transaction_id, expiry)

async def get_channel_transaction(channel_id: int) -> Optional[int]:
    """获取频道的交易ID。"""
    try:
        key = f"{CHANNEL_PREFIX}{channel_id}"
        value = await get_value(key)
        return int(value) if value else None
    except asyncio.CancelledError:
        # 正确处理协程取消
        raise
    except Exception as e:
        logger.error(f"获取频道 {channel_id} 的交易ID时出错: {e}")
        return None

async def set_payment_timeout(transaction_id: int, expiry: int = config.PAYMENT_TIMEOUT) -> bool:
    """为交易设置支付超时。"""
    key = f"{PAYMENT_PREFIX}{transaction_id}"
    return await set_value(key, "timeout", expiry)

async def check_payment_timeout(transaction_id: int) -> bool:
    """检查支付超时是否仍然存在（如果未超时则返回True）。"""
    key = f"{PAYMENT_PREFIX}{transaction_id}"
    return await get_value(key) is not None

async def set_payment_timestamp(transaction_id: int, timestamp: int) -> bool:
    """记录交易支付请求的时间戳。"""
    key = f"{PAYMENT_PREFIX}timestamp:{transaction_id}"
    return await set_value(key, timestamp, 86400)  # 24小时过期

async def get_payment_timestamp(transaction_id: int) -> Optional[int]:
    """获取交易支付请求的时间戳。"""
    key = f"{PAYMENT_PREFIX}timestamp:{transaction_id}"
    value = await get_value(key)
    return int(value) if value else None

async def cache_invite_code(discord_id: int, invite_code: str) -> bool:
    """缓存用户的邀请码。"""
    key = f"{INVITE_PREFIX}{discord_id}"
    return await set_value(key, invite_code)

async def get_cached_invite_code(discord_id: int) -> Optional[str]:
    """获取用户缓存的邀请码。"""
    key = f"{INVITE_PREFIX}{discord_id}"
    return await get_value(key)

async def cache_guild_invites(guild_id: int, invites: Dict[str, int]) -> bool:
    """缓存服务器邀请。"""
    try:
        client = await get_redis()
        key = f"guild_invites:{guild_id}"
        await client.set(key, json.dumps(invites), ex=86400)  # 24小时过期
        return True
    except Exception as e:
        logger.error(f"缓存服务器 {guild_id} 邀请时出错: {e}")
        return False

async def get_guild_invites(guild_id: int) -> Optional[Dict[str, int]]:
    """获取缓存的服务器邀请。"""
    try:
        client = await get_redis()
        key = f"guild_invites:{guild_id}"
        data = await client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.error(f"获取服务器 {guild_id} 缓存邀请时出错: {e}")
        return None

# 卖家收款地址等待状态管理
async def set_waiting_for_seller_address(transaction_id, seller_id):
    """设置交易正在等待卖家提供收款地址"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:waiting_address"
        await client.set(key, str(seller_id), ex=3600)  # 1小时过期
        return True
    except Exception as e:
        logger.error(f"设置等待卖家地址状态时出错: {e}")
        return False

async def get_waiting_for_seller_address(transaction_id):
    """获取交易是否正在等待卖家提供收款地址"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:waiting_address"
        return await client.get(key)
    except Exception as e:
        logger.error(f"获取等待卖家地址状态时出错: {e}")
        return None

async def clear_waiting_for_seller_address(transaction_id):
    """清除交易等待卖家提供收款地址的状态"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:waiting_address"
        await client.delete(key)
        return True
    except Exception as e:
        logger.error(f"清除等待卖家地址状态时出错: {e}")
        return False

async def cache_pending_address(transaction_id, address):
    """缓存待确认的收款地址"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:pending_address"
        await client.set(key, address, ex=1800)  # 30分钟过期
        return True
    except Exception as e:
        logger.error(f"缓存待确认收款地址时出错: {e}")
        return False

async def get_pending_address(transaction_id):
    """获取待确认的收款地址"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:pending_address"
        return await client.get(key)
    except Exception as e:
        logger.error(f"获取待确认收款地址时出错: {e}")
        return None

async def clear_pending_address(transaction_id):
    """清除待确认的收款地址"""
    try:
        client = await get_redis()
        key = f"transaction:{transaction_id}:pending_address"
        await client.delete(key)
        return True
    except Exception as e:
        logger.error(f"清除待确认收款地址时出错: {e}")
        return False

async def set_claimed_deposit(deposit_txid: str, transaction_id: int, expire_seconds: int = 86400 * 7) -> bool:
    """将存款标记为已被特定交易认领。
    
    Args:
        deposit_txid: 存款交易ID
        transaction_id: 认领的交易ID
        expire_seconds: 过期时间（秒），默认7天
        
    Returns:
        是否成功设置
    """
    key = f"{CLAIMED_DEPOSIT_PREFIX}{deposit_txid}"
    try:
        client = await get_redis()
        await client.set(key, str(transaction_id), ex=expire_seconds)
        return True
    except Exception as e:
        logger.error(f"将存款 {deposit_txid} 标记为已被交易 {transaction_id} 认领时出错: {e}")
        return False

async def get_claimed_deposit(deposit_txid: str) -> Optional[int]:
    """检查存款是否已被认领，如果是，返回认领的交易ID。
    
    Args:
        deposit_txid: 存款交易ID
        
    Returns:
        认领的交易ID，如果未被认领则返回None
    """
    key = f"{CLAIMED_DEPOSIT_PREFIX}{deposit_txid}"
    try:
        client = await get_redis()
        claimed_by = await client.get(key)
        if claimed_by:
            return int(claimed_by)
        return None
    except Exception as e:
        logger.error(f"检查存款 {deposit_txid} 是否被认领时出错: {e}")
        return None

async def acquire_lock(lock_key: str, expire_seconds: int = 30) -> Optional[str]:
    """获取分布式锁。
    
    Args:
        lock_key: 锁的键
        expire_seconds: 锁的过期时间（秒）
        
    Returns:
        锁的值（用于释放锁），如果获取失败则返回None
    """
    full_key = f"{LOCK_PREFIX}{lock_key}"
    lock_value = str(uuid.uuid4())
    try:
        client = await get_redis()
        # 使用NX选项确保原子性，只有当键不存在时才设置
        result = await client.set(full_key, lock_value, ex=expire_seconds, nx=True)
        if result:
            return lock_value
        return None
    except Exception as e:
        logger.error(f"获取锁 {lock_key} 时出错: {e}")
        return None

async def release_lock(lock_key: str, lock_value: str) -> bool:
    """释放分布式锁。
    
    Args:
        lock_key: 锁的键
        lock_value: 锁的值（确保只有持有者能释放锁）
        
    Returns:
        是否成功释放锁
    """
    full_key = f"{LOCK_PREFIX}{lock_key}"
    try:
        client = await get_redis()
        # 使用Lua脚本确保原子性，只有当值匹配时才删除键
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        result = await client.eval(script, 1, full_key, lock_value)
        return bool(result)
    except Exception as e:
        logger.error(f"释放锁 {lock_key} 时出错: {e}")
        return False 