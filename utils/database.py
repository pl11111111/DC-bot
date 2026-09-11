import aiomysql
import pymysql
import logging
from typing import Dict, List, Optional, Any, Tuple, Union
import config
import asyncio
from datetime import datetime, timedelta
import re

# 自定义日志过滤器，过滤掉高频出现的错误信息
class DatabaseLogFilter(logging.Filter):
    def filter(self, record):
        # 检查是否是错误日志
        if record.levelno >= logging.ERROR:
            message = record.getMessage()
            # 过滤掉已知的表不存在错误，这些会在初始化时自动创建
            if "Table 'trade_bot.currency_trades' doesn't exist" in message:
                return False
            if "Table 'trade_bot.summary_messages' doesn't exist" in message:
                return False
        return True

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
# 添加过滤器
logger.addFilter(DatabaseLogFilter())

def validate_query(query: str, params: tuple = None) -> Tuple[bool, str]:
    """
    验证SQL查询语句的安全性。
    
    Args:
        query: SQL查询语句
        params: 查询参数
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误消息)
    """
    # 标准化查询（删除多余空格、换行等）
    query = query.strip()
    
    # 检查基本语法，确保是有效的SQL操作
    valid_operations = [
        "SELECT", "INSERT", "UPDATE", "DELETE", 
        "CREATE", "ALTER", "DROP", "SHOW"
    ]
    
    operation = query.split(" ", 1)[0].upper()
    if operation not in valid_operations:
        return False, f"不支持的SQL操作: {operation}"
    
    # 检查危险操作
    if operation in ["DROP", "ALTER"]:
        if not query.upper().startswith("ALTER TABLE") and not query.upper().startswith("DROP TABLE"):
            return False, f"危险的SQL操作: {query[:50]}..."
    
    # 检查参数数量是否匹配
    if params:
        # 计算查询中的占位符数量 (%s)
        placeholder_count = query.count("%s")
        param_count = len(params)
        
        if placeholder_count != param_count:
            return False, f"参数数量不匹配: 查询需要 {placeholder_count} 个参数，但提供了 {param_count} 个"
    
    # 检查分号使用 (防止多语句)
    if ";" in query[:-1]:  # 允许查询末尾有分号
        return False, "查询包含多个语句，不允许"
    
    # 检查注释 (可能用于SQL注入)
    if "--" in query or "#" in query or "/*" in query:
        return False, "查询包含注释，不允许"
    
    # 检查潜在的SQL注入尝试
    injection_patterns = [
        r"'\s*OR\s+'", 
        r"'\s*;\s*",
        r"'\s*--\s*",
        r"'\s*#\s*",
        r"'\s*OR\s*1\s*=\s*1",
        r"'\s*OR\s*'1'\s*=\s*'1",
        r"'\s*UNION\s*SELECT"
    ]
    
    for pattern in injection_patterns:
        if re.search(pattern, query, re.IGNORECASE):
            return False, f"检测到潜在的SQL注入: {query[:50]}..."
    
    return True, ""

_connection_pool = None
_connection_pool_lock = asyncio.Lock()

async def get_pool():
    """获取MySQL数据库的连接池。"""
    global _connection_pool
    if _connection_pool is not None:
        return _connection_pool
    try:
        async with _connection_pool_lock:
            if _connection_pool is None:
                _connection_pool = await aiomysql.create_pool(
                    host=config.MYSQL_HOST,
                    port=config.MYSQL_PORT,
                    user=config.MYSQL_USER,
                    password=config.MYSQL_PASSWORD,
                    db=config.MYSQL_DATABASE,
                    charset='utf8mb4',
                    cursorclass=aiomysql.DictCursor,
                    autocommit=True
                )
            return _connection_pool
    except Exception as e:
        logger.error(f"创建数据库池时出错: {e}")
        raise

async def execute_query(query: str, params: tuple = None) -> int:
    """执行不返回结果的查询（INSERT, UPDATE, DELETE）。"""
    # 验证查询
    is_valid, error_message = validate_query(query, params)
    if not is_valid:
        logger.error(f"SQL查询验证失败: {error_message}，查询: {query}")
        raise ValueError(f"SQL查询验证失败: {error_message}")
        
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            try:
                await cursor.execute(query, params)
                await conn.commit()
                # 对于UPDATE和DELETE查询，返回影响的行数
                if query.strip().upper().startswith(('UPDATE', 'DELETE')):
                    return cursor.rowcount
                # 对于INSERT查询，返回最后插入的ID
                return cursor.lastrowid
            except Exception as e:
                await conn.rollback()
                logger.error(f"执行 {query} 时数据库错误: {e}")
                raise

async def fetch_one(query: str, params: tuple = None) -> Optional[Dict]:
    """执行查询并获取一个结果。"""
    # 验证查询
    is_valid, error_message = validate_query(query, params)
    if not is_valid:
        logger.error(f"SQL查询验证失败: {error_message}，查询: {query}")
        raise ValueError(f"SQL查询验证失败: {error_message}")
        
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                result = await cursor.fetchone()
                if result:
                    # 记录原始结果类型和内容
                    logger.debug(f"fetch_one原始结果类型: {type(result)}, 内容: {result}")
                    
                    # 如果结果已经是字典，直接返回
                    if isinstance(result, dict):
                        return result
                    
                    # 获取列名
                    columns = [column[0] for column in cursor.description]
                    logger.debug(f"fetch_one列名: {columns}")
                    
                    # 确保结果是元组或列表
                    if isinstance(result, (tuple, list)) and len(result) == len(columns):
                        result_dict = dict(zip(columns, result))
                        
                        # 检查结果字典是否异常（值等于键名）
                        for key, value in result_dict.items():
                            if key == value:
                                logger.warning(f"fetch_one检测到键值相同: {key}={value}，可能是结果格式错误")
                                
                                # 尝试获取计数类型字段的实际值
                                if key.endswith('_count') and isinstance(result[0], (int, float)):
                                    result_dict[key] = result[0]
                                    logger.info(f"自动修正: 设置{key}={result[0]}")
                        
                        return result_dict
                    else:
                        logger.error(f"fetch_one结果格式无法处理: {result}")
                        if len(result) == 1 and isinstance(result[0], (int, float, str)):
                            # 如果只有一个值，创建一个包含该值的字典
                            field_name = columns[0] if columns else "value"
                            return {field_name: result[0]}
                        return None
                return None
    except asyncio.CancelledError:
        # 正确处理协程取消
        raise
    except Exception as e:
        logger.error(f"数据库查询失败: {e}", exc_info=True)
        return None

async def fetch_all(query: str, params: tuple = None) -> List[Dict]:
    """获取多行结果作为字典列表。"""
    # 验证查询
    is_valid, error_message = validate_query(query, params)
    if not is_valid:
        logger.error(f"SQL查询验证失败: {error_message}，查询: {query}")
        raise ValueError(f"SQL查询验证失败: {error_message}")
        
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                results = await cursor.fetchall()
                if results:
                    columns = [column[0] for column in cursor.description]
                    
                    # 检查结果格式
                    if isinstance(results[0], dict):
                        # 结果已经是字典格式，直接返回
                        return results
                    elif isinstance(results[0], (tuple, list)) and len(results[0]) == len(columns):
                        # 结果是元组或列表，需要转换为字典
                        return [dict(zip(columns, row)) for row in results]
                    else:
                        # 如果结果格式异常，返回空列表
                        return []
                return []
    except asyncio.CancelledError:
        # 正确处理协程取消
        raise
    except Exception as e:
        logger.error(f"数据库查询失败: {e}", exc_info=True)
        return []

# 用户操作
async def get_user(discord_id: int) -> Optional[Dict]:
    """通过Discord ID获取用户信息。"""
    query = "SELECT * FROM users WHERE discord_id = %s"
    return await fetch_one(query, (discord_id,))

async def create_user(discord_id: int, username: str) -> int:
    """创建新用户。"""
    query = """
        INSERT INTO users (discord_id, username) 
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE username = %s
    """
    return await execute_query(query, (discord_id, username, username))

async def update_user_free_escrow_count(discord_id: int, count: int) -> int:
    """更新用户的免费托管次数。"""
    query = "UPDATE users SET free_escrow_count = %s WHERE discord_id = %s"
    return await execute_query(query, (count, discord_id))

async def update_user_free_escrow_amount(discord_id: int, amount: float) -> int:
    """更新用户的免费托管额度。"""
    query = "UPDATE users SET free_escrow_amount = %s WHERE discord_id = %s"
    return await execute_query(query, (amount, discord_id))

async def set_user_active_transaction(discord_id: int, transaction_id: Optional[int], expected_transaction_id: Optional[int] = None) -> int:
    """设置用户的活跃交易ID。"""
    query = "UPDATE users SET active_transaction_id = %s WHERE discord_id = %s"
    if expected_transaction_id is not None:
        return await execute_query(query + ' AND active_transaction_id = %s', (transaction_id, discord_id, expected_transaction_id))
    return await execute_query(query, (transaction_id, discord_id))

async def set_user_active_rental(discord_id: int, rental_id: Optional[int]) -> int:
    """设置用户的活跃租赁ID。
    
    将租赁ID添加到active_rentals JSON数组中。
    
    Args:
        discord_id: 用户的Discord ID
        rental_id: 要设置的租赁ID
        
    Returns:
        影响的行数
    """
    # 获取用户数据
    user = await get_user(discord_id)
    if not user:
        return 0
    
    # 如果传入的rental_id为None，直接返回
    if rental_id is None:
        logger.warning(f"尝试设置空的租赁ID，操作被忽略")
        return 0
    
    # 对于所有用户，将租赁ID添加到active_rentals JSON数组中
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # 查询现有的active_rentals
                await cursor.execute(
                    "SELECT active_rentals FROM users WHERE discord_id = %s",
                    (discord_id,)
                )
                result = await cursor.fetchone()
                
                import json
                
                # 解析现有的active_rentals
                active_rentals = []
                if result and result.get('active_rentals'):
                    try:
                        rentals = json.loads(result['active_rentals'])
                        if isinstance(rentals, list):
                            active_rentals = rentals
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # 如果租赁ID已在列表中，不需要添加
                if rental_id in active_rentals:
                    return 0
                
                # 添加新的租赁ID
                active_rentals.append(rental_id)
                
                # 更新数据库，只设置active_rentals
                await cursor.execute(
                    """
                    UPDATE users 
                    SET active_rentals = %s 
                    WHERE discord_id = %s
                    """,
                    (json.dumps(active_rentals), discord_id)
                )
                await conn.commit()
                
                logger.info(f"已将租赁 {rental_id} 添加到用户 {discord_id} 的active_rentals: {active_rentals}")
                return 1
    except Exception as e:
        logger.error(f"设置多租赁时出错: {e}", exc_info=True)
        # 如果异常，尝试使用基本的方法设置
        query = "UPDATE users SET active_rentals = JSON_ARRAY(%s) WHERE discord_id = %s"
        return await execute_query(query, (rental_id, discord_id))

async def increment_rental_count(discord_id: int) -> int:
    """增加用户的租赁计数。"""
    query = "UPDATE users SET rental_count = rental_count + 1 WHERE discord_id = %s"
    return await execute_query(query, (discord_id,))

async def update_user_max_rentals(discord_id: int, max_rentals: int) -> int:
    """更新用户的最大租赁数量。"""
    query = "UPDATE users SET max_rentals = %s WHERE discord_id = %s"
    return await execute_query(query, (max_rentals, discord_id))

async def get_user_rental_count(discord_id: int) -> int:
    """获取用户的租赁计数。"""
    query = "SELECT rental_count FROM users WHERE discord_id = %s"
    result = await fetch_one(query, (discord_id,))
    return result.get('rental_count', 0) if result else 0

async def decrement_active_rental(discord_id: int, rental_id: int) -> bool:
    """从用户的活跃租赁中移除特定的租赁ID。
    
    Args:
        discord_id: 用户的Discord ID
        rental_id: 要移除的租赁ID
        
    Returns:
        bool: 是否成功移除
    """
    # 处理active_rentals JSON字段
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # 首先查询现有的active_rentals
                await cursor.execute(
                    "SELECT active_rentals FROM users WHERE discord_id = %s",
                    (discord_id,)
                )
                result = await cursor.fetchone()
                
                if not result or not result.get('active_rentals'):
                    # 如果没有active_rentals字段或为空，则不需要更新
                    return False
                
                import json
                
                # 解析JSON数组
                try:
                    active_rentals = json.loads(result['active_rentals'])
                    if not isinstance(active_rentals, list):
                        active_rentals = []
                except (json.JSONDecodeError, TypeError):
                    active_rentals = []
                
                # 如果租赁ID不在列表中，返回False
                if rental_id not in active_rentals:
                    return False
                
                # 从列表中移除租赁ID
                active_rentals.remove(rental_id)
                
                # 更新数据库
                await cursor.execute(
                    "UPDATE users SET active_rentals = %s WHERE discord_id = %s",
                    (json.dumps(active_rentals), discord_id)
                )
                await conn.commit()
                
                logger.info(f"已从用户 {discord_id} 的active_rentals中移除租赁 {rental_id}，剩余: {active_rentals}")
                return True
    except Exception as e:
        logger.error(f"移除活跃租赁时出错: {e}", exc_info=True)
    
    return False

async def has_active_rental(discord_id: int) -> bool:
    """检查用户是否有活跃租赁。"""
    user = await get_user(discord_id)
    if not user or not user.get('active_rentals'):
        return False
        
    # 解析active_rentals字段
    try:
        import json
        rentals = json.loads(user['active_rentals'])
        return isinstance(rentals, list) and len(rentals) > 0
    except (json.JSONDecodeError, TypeError):
        return False

async def get_user_active_rentals(discord_id: int) -> List[int]:
    """获取用户的所有活跃租赁ID。
    
    Args:
        discord_id: 用户的Discord ID
        
    Returns:
        List[int]: 活跃租赁ID列表
    """
    user = await get_user(discord_id)
    if not user:
        return []
    
    # 从active_rentals JSON字段获取
    active_rentals = []
    if user.get('active_rentals'):
        try:
            import json
            rentals = json.loads(user['active_rentals'])
            if isinstance(rentals, list):
                # 确保列表中的每个元素都是整数
                active_rentals = [int(rental_id) for rental_id in rentals if rental_id]
        except (json.JSONDecodeError, TypeError, ValueError):
            # 如果解析失败，记录错误
            logger.warning(f"解析用户 {discord_id} 的active_rentals时出错")
    
    return active_rentals

async def can_create_new_rental(discord_id: int, member_roles: List[int]) -> bool:
    """检查用户是否可以创建新的租赁。
    
    Args:
        discord_id: 用户的Discord ID
        member_roles: 用户拥有的角色ID列表
        
    Returns:
        bool: 是否可以创建新的租赁
    """
    # 获取用户信息
    user = await get_user(discord_id)
    if not user:
        return False
    
    # 检查用户是否有特殊角色
    has_booster_role = config.BOOSTER_ROLE_ID in member_roles
    has_special_role = any(role_id in config.MULTI_RENTAL_ROLE_IDS for role_id in member_roles)
    
    # 根据用户角色确定最大租赁数量
    max_rentals = config.MAX_RENTALS_DEFAULT
    
    # 设置最大租赁数量
    if has_special_role:
        # 特殊角色使用MAX_RENTALS_SPECIAL
        max_rentals = config.MAX_RENTALS_SPECIAL
    elif has_booster_role:
        # 助力用户使用MAX_RENTALS_BOOSTER
        max_rentals = config.MAX_RENTALS_BOOSTER
    
    # 更新用户的最大租赁数量
    if max_rentals != user.get('max_rentals'):
        await update_user_max_rentals(discord_id, max_rentals)
    
    # 获取用户的活跃租赁数量
    active_rentals = await get_user_active_rentals(discord_id)
    
    return len(active_rentals) < max_rentals

async def update_user_payment_address(user_id: int, payment_address: str) -> bool:
    """更新用户的收款地址。
    
    Args:
        user_id: 用户ID
        payment_address: 新的收款地址
        
    Returns:
        bool: 是否更新成功
    """
    try:
        # 防SQL注入验证: 检查地址格式是否合法
        if not payment_address:
            logger.warning(f"尝试使用空地址更新用户 {user_id} 的支付地址")
            return False
            
        # 检查是否包含可疑SQL注入关键字
        suspicious_patterns = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'UNION', '--', ';', "'"]
        for pattern in suspicious_patterns:
            if pattern.upper() in payment_address.upper():
                logger.warning(f"检测到可能的SQL注入尝试: 用户 {user_id} 的支付地址 '{payment_address}'")
                return False
                
        # 仅允许有效的加密货币地址格式 (可根据需要调整)
        # BEP20地址格式: 0x后跟40位十六进制字符 (与以太坊地址格式相同)
        bep20_pattern = r'^0x[0-9a-fA-F]{40}$'
        
        import re
        is_valid_bep20 = bool(re.match(bep20_pattern, payment_address))
        
        if not is_valid_bep20:
            logger.warning(f"用户 {user_id} 尝试使用无效格式的BEP20支付地址: '{payment_address}'")
            return False
        
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE users 
                    SET payment_address = %s
                    WHERE discord_id = %s
                    """,
                    (payment_address, user_id)
                )
                await conn.commit()
        return True
    except Exception as e:
        logger.error(f"更新用户收款地址时出错: {e}")
        return False

# 交易操作
async def create_transaction(
    transaction_type: str,
    buyer_id: int,
    seller_id: int,
    item_name: str,
    amount: float,
    escrow_fee: float,
    channel_id: int
) -> int:
    """创建新交易记录并返回交易ID。"""
    # 验证数值参数
    try:
        amount = float(amount)
        escrow_fee = float(escrow_fee)
    except (TypeError, ValueError):
        logger.error(f"交易参数类型无效: amount={type(amount)}, escrow_fee={type(escrow_fee)}")
        raise ValueError("无效的数字格式")
        
    # 检查数值范围
    if amount <= 0 or amount > 1000000000:
        logger.error(f"交易金额超出范围: {amount}")
        raise ValueError("金额超出有效范围")
        
    if escrow_fee < 0 or escrow_fee > 1000000:
        logger.error(f"托管费超出范围: {escrow_fee}")
        raise ValueError("托管费超出有效范围")
        
    if not item_name or len(item_name) > 200:
        logger.error(f"物品名称无效: {item_name}")
        raise ValueError("物品名称无效")

    # 创建交易记录
    created_at = datetime.now()
    
    try:
        query = """
        INSERT INTO transactions 
            (transaction_type, buyer_id, seller_id, item_name, amount, escrow_fee, status, channel_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        transaction_id = await execute_query(
            query, 
            (transaction_type, buyer_id, seller_id, item_name, amount, escrow_fee, "pending", channel_id, created_at)
        )
        
        return transaction_id
    except Exception as e:
        logger.error(f"创建交易记录时出错: {str(e)}")
        raise

async def get_transaction(transaction_id: int) -> Optional[Dict]:
    """通过ID获取交易。"""
    query = "SELECT * FROM transactions WHERE id = %s"
    return await fetch_one(query, (transaction_id,))

async def get_transaction_by_channel(channel_id: int) -> Optional[Dict]:
    """通过频道ID获取交易信息。"""
    try:
        query = "SELECT * FROM transactions WHERE channel_id = %s"
        return await fetch_one(query, (channel_id,))
    except asyncio.CancelledError:
        # 正确处理协程取消
        raise
    except Exception as e:
        logger.error(f"获取频道交易失败: {e}", exc_info=True)
        return None

async def update_transaction_status(transaction_id: int, status: str) -> int:
    """更新交易状态。"""
    valid_statuses = [
        "pending", "confirmed", "paid", "shipped", "completed", 
        "cancelled", "disputed", "returning", "paying","rental_started","confirm_return","confirmed_receipt"
    ]
    
    if status not in valid_statuses:
        raise ValueError(f"无效的交易状态: {status}")
    if status == 'cancelled' and config.NEW.PAYMENTS_ENABLED:
        row = await get_transaction(transaction_id)
        if row and row['transaction_type'] == 'trade':
            from utils.shared_payments import cancel_legacy_trade
            return await cancel_legacy_trade(transaction_id)
    
    trade_previous = {
        'confirmed': ('pending',), 'paying': ('confirmed',),
        'paid': ('paying',), 'shipped': ('paid',),
        'confirmed_receipt': ('shipped',), 'disputed': ('paid','shipped'),
        'completed': ('confirmed_receipt',), 'cancelled': ('pending','confirmed','paying')
    }
    if status in trade_previous:
        previous = trade_previous[status]
        placeholders = ','.join(['%s'] * len(previous))
        query = f"UPDATE transactions SET status=%s, updated_at=NOW() WHERE id=%s AND (transaction_type<>'trade' OR status IN ({placeholders}))"
        changed = await execute_query(query, (status, transaction_id, *previous))
        if changed != 1:
            raise ValueError('订单状态已变化，拒绝覆盖状态')
        return changed
    query = "UPDATE transactions SET status = %s, updated_at = NOW() WHERE id = %s"
    return await execute_query(query, (status, transaction_id))

async def update_transaction_payment(
    transaction_id: int, 
    payment_address: str, 
    txid: Optional[str] = None,
    unique_amount: Optional[float] = None
) -> int:
    """更新交易的支付信息。"""
    # 验证参数
    if not isinstance(transaction_id, int) or transaction_id <= 0:
        raise ValueError("无效的交易ID")
    
    if not payment_address or not isinstance(payment_address, str):
        raise ValueError("无效的支付地址")
    
    # 构建查询参数和SQL语句
    params = []
    set_clauses = []
    
    set_clauses.append("payment_address = %s")
    params.append(payment_address)
    
    if txid:
        set_clauses.append("txid = %s")
        params.append(txid)
        set_clauses.append("paid_at = CURRENT_TIMESTAMP")
    
    if unique_amount is not None:
        # 确保unique_amount的精度不超过数据库列定义DECIMAL(18,6)
        unique_amount_rounded = round(float(unique_amount), 6)
        set_clauses.append("unique_amount = %s")
        params.append(unique_amount_rounded)
    
    # 更新状态（如果有txid，标记为已支付）
    if txid:
        set_clauses.append("status = 'paid'")
        
    # 添加交易ID到参数列表
    params.append(transaction_id)
    
    # 构建并执行更新查询
    query = f"""
        UPDATE transactions 
        SET {', '.join(set_clauses)}
        WHERE id = %s
    """
    if txid:
        query += " AND (transaction_type <> 'trade' OR status = 'paying')"
    changed = await execute_query(query, tuple(params))
    if txid and changed != 1:
        raise ValueError('订单状态已改变；到账记录需核对，禁止覆盖订单')
    return changed

async def complete_transaction(transaction_id: int) -> int:
    """标记交易为已完成。"""
    query = """
        UPDATE transactions 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
        WHERE id = %s AND (transaction_type <> 'trade' OR status = 'confirmed_receipt')
    """
    return await execute_query(query, (transaction_id,))

# 租赁操作
async def create_rental(
    transaction_id: int,
    rental_period: int,
    deposit: float,
    rental_fee: float,
    rental_unit: str = "days"
) -> int:
    """创建新的租赁记录。"""
    query = """
        INSERT INTO rentals 
        (transaction_id, rental_period, deposit, rental_fee, rental_unit) 
        VALUES (%s, %s, %s, %s, %s)
    """
    return await execute_query(query, (transaction_id, rental_period, deposit, rental_fee, rental_unit))

async def get_rental(transaction_id: int) -> Optional[Dict]:
    """通过交易ID获取租赁信息。"""
    query = "SELECT * FROM rentals WHERE transaction_id = %s"
    return await fetch_one(query, (transaction_id,))

async def get_rental_by_transaction(transaction_id: int) -> Optional[Dict]:
    """通过交易ID获取租赁信息（与get_rental功能相同，提供兼容性）。"""
    return await get_rental(transaction_id)

async def update_rental_dates(
    transaction_id: int,
    start_date: str,
    end_date: str
) -> int:
    """更新租赁开始和结束日期。"""
    query = """
        UPDATE rentals 
        SET start_date = %s, end_date = %s 
        WHERE transaction_id = %s
    """
    return await execute_query(query, (start_date, end_date, transaction_id))

async def mark_rental_returned(transaction_id: int) -> int:
    """标记租赁物品为已归还。"""
    query = "UPDATE rentals SET returned = TRUE WHERE transaction_id = %s"
    return await execute_query(query, (transaction_id,))

# 邀请操作
async def create_invite(inviter_id: int, invite_code: str) -> int:
    """创建新的邀请码。
    
    先将用户的所有未使用邀请标记为已使用（无效化），然后创建新邀请。
    这确保每个用户只有一个有效的邀请链接。
    """
    try:
        # 先获取一个连接
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # 开始事务
                await cursor.execute("START TRANSACTION")
                
                try:
                    # 将该用户所有未使用的邀请标记为已使用
                    invalidate_query = """
                        UPDATE invitations 
                        SET used = TRUE, used_at = CURRENT_TIMESTAMP 
                        WHERE inviter_id = %s AND used = FALSE
                    """
                    await cursor.execute(invalidate_query, (inviter_id,))
                    
                    # 创建新邀请
                    insert_query = """
                        INSERT INTO invitations 
                        (inviter_id, invite_code) 
                        VALUES (%s, %s)
                    """
                    await cursor.execute(insert_query, (inviter_id, invite_code))
                    invite_id = cursor.lastrowid
                    
                    # 提交事务
                    await cursor.execute("COMMIT")
                    return invite_id
                except Exception as e:
                    # 如果出错，回滚事务
                    await cursor.execute("ROLLBACK")
                    logger.error(f"创建邀请过程中出错: {str(e)}", exc_info=True)
                    raise
    except Exception as e:
        logger.error(f"创建邀请时数据库操作失败: {str(e)}", exc_info=True)
        # 使用原始方法作为备份
    query = """
        INSERT INTO invitations 
            (inviter_id, invite_code) 
            VALUES (%s, %s)
    """
    return await execute_query(query, (inviter_id, invite_code))

async def use_invite(invite_code: str, invitee_id: int) -> int:
    """记录邀请码被使用，但不标记邀请为已使用。添加检查防止同一用户使用多个邀请码。"""
    try:
        # 首先检查该用户是否已经使用过任何邀请码
        general_check_query = """
            SELECT id FROM invitation_usage 
            WHERE invitee_id = %s
        """
        any_existing_usage = await fetch_one(general_check_query, (invitee_id,))
        
        if any_existing_usage:
            logger.warning(f"用户 {invitee_id} 已经使用过其他邀请码，不允许使用多个邀请码")
            return 0
        
        # 获取邀请信息
        invite_data = await get_invite_by_discord_code(invite_code)
        
        if not invite_data:
            logger.warning(f"无法找到邀请码 {invite_code}")
            return 0
        
        # 检查返回的数据格式，确保能够正确获取ID
        if isinstance(invite_data, dict):
            invite_id = invite_data.get('id')
        else:
            # 如果是元组或其他格式，假设第一个元素是ID
            invite_id = invite_data[0] if invite_data else None
            
        if not invite_id or invite_id == 'id':
            logger.warning(f"无法从邀请数据中获取有效的ID: {invite_data}")
            return 0
        
        # 检查该用户是否已经使用过这个邀请码 (双重检查)
        check_query = """
            SELECT id FROM invitation_usage 
            WHERE invite_id = %s AND invitee_id = %s
        """
        existing_usage = await fetch_one(check_query, (invite_id, invitee_id))
        
        if existing_usage:
            logger.warning(f"用户 {invitee_id} 已经使用过邀请码 {invite_code} (ID: {invite_id})，不再重复记录")
            return 0
        
        # 记录到邀请使用表
        usage_query = """
            INSERT INTO invitation_usage 
            (invite_id, invitee_id, used_at) 
            VALUES (%s, %s, CURRENT_TIMESTAMP)
        """
        result = await execute_query(usage_query, (invite_id, invitee_id))
        
        if result > 0:
            logger.info(f"已记录邀请码 {invite_code} (ID: {invite_id}) 被用户 {invitee_id} 使用")
        
        return result
    except Exception as e:
        logger.error(f"记录邀请使用情况时出错: {e}", exc_info=True)
        return 0

async def get_top_inviters(limit: int = 3) -> List[Dict]:
    """获取基于已验证成功邀请的顶级邀请者。"""
    try:
        query = """
            SELECT u.discord_id, u.username, COUNT(iu.id) as invite_count 
        FROM users u 
        JOIN invitations i ON u.discord_id = i.inviter_id 
            JOIN invitation_usage iu ON i.id = iu.invite_id
            WHERE iu.verified = TRUE
        GROUP BY u.discord_id 
        ORDER BY invite_count DESC 
        LIMIT %s
    """
        return await fetch_all(query, (limit,))
    except Exception as e:
        logger.error(f"获取顶级邀请者时出错: {e}", exc_info=True)
        return []

async def get_top_inviter_role_users() -> List[int]:
    """获取因为是前三名邀请者而自动分配了排名身份组的用户列表。"""
    try:
        query = """
            SELECT user_id FROM bot_settings 
            WHERE setting_key = 'top_inviter_role_users'
        """
        result = await fetch_one(query)
        if result and 'user_id' in result:
            # 用户ID列表可能被存储为逗号分隔的字符串或JSON数组
            value = result['user_id']
            if isinstance(value, str):
                if value.startswith('[') and value.endswith(']'):
                    # 尝试解析JSON数组
                    try:
                        import json
                        return json.loads(value)
                    except:
                        pass
                # 尝试解析逗号分隔的列表
                return [int(uid.strip()) for uid in value.split(',') if uid.strip().isdigit()]
            elif isinstance(value, list):
                return value
        return []
    except Exception as e:
        logger.error(f"获取自动分配排名身份组的用户列表时出错: {e}", exc_info=True)
        return []

async def update_top_inviter_role_users(user_ids: List[int]) -> bool:
    """更新因为是前三名邀请者而自动分配了排名身份组的用户列表。"""
    try:
        # 将用户ID列表转换为JSON字符串
        import json
        user_ids_json = json.dumps(user_ids)
        
        # 检查设置是否已存在
        exists_query = """
            SELECT COUNT(*) as count FROM bot_settings 
            WHERE setting_key = 'top_inviter_role_users'
        """
        result = await fetch_one(exists_query)
        exists = result and result.get('count', 0) > 0
        
        if exists:
            # 更新现有设置
            update_query = """
                UPDATE bot_settings 
                SET user_id = %s 
                WHERE setting_key = 'top_inviter_role_users'
            """
            await execute_query(update_query, (user_ids_json,))
        else:
            # 创建新设置
            insert_query = """
                INSERT INTO bot_settings (setting_key, user_id)
                VALUES ('top_inviter_role_users', %s)
            """
            await execute_query(insert_query, (user_ids_json,))
        
        logger.info(f"已更新自动分配排名身份组的用户列表: {user_ids}")
        return True
    except Exception as e:
        logger.error(f"更新自动分配排名身份组的用户列表时出错: {e}", exc_info=True)
        return False

async def get_user_invite_count(user_id: int) -> int:
    """获取特定用户成功邀请的数量。"""
    try:
        # 使用邀请使用表统计邀请次数
        query = """
            SELECT COUNT(u.id) as invite_count 
            FROM invitation_usage u
            JOIN invitations i ON u.invite_id = i.id
            WHERE i.inviter_id = %s
        """
        result = await fetch_one(query, (user_id,))
        
        # 增加日志和检查
        if not result:
            logger.warning(f"用户 {user_id} 没有邀请记录")
            return 0
            
        # 检查结果是否为字典且包含invite_count键
        if not isinstance(result, dict):
            logger.error(f"获取用户邀请计数返回了非字典结果: {result}")
            return 0
            
        if 'invite_count' not in result:
            logger.error(f"获取用户邀请计数返回的字典没有invite_count键: {result}")
            return 0
            
        # 检查invite_count是否为列名
        if result.get('invite_count') == 'invite_count':
            logger.error(f"获取用户邀请计数返回了列名而不是值: {result}")
            
            # 尝试直接执行原始SQL查询
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    try:
                        await cursor.execute(query, (user_id,))
                        raw_result = await cursor.fetchone()
                        logger.info(f"原始查询结果: {raw_result}")
                        
                        # 如果raw_result是元组或列表，直接取第一个元素
                        if raw_result and isinstance(raw_result, (tuple, list)) and len(raw_result) > 0:
                            count = raw_result[0]
                            logger.info(f"从原始结果中提取的计数: {count}")
                            
                            # 确保返回的是整数
                            try:
                                return int(count)
                            except (ValueError, TypeError):
                                logger.error(f"无法将计数转换为整数: {count}")
                                return 0
                    except Exception as e:
                        logger.error(f"执行原始SQL查询时出错: {e}")
            
            return 0
            
        # 尝试将结果转换为整数
        try:
            count = int(result.get('invite_count', 0))
            return count
        except (ValueError, TypeError):
            logger.error(f"无法将invite_count转换为整数: {result.get('invite_count')}")
            return 0
    except Exception as e:
        logger.error(f"获取用户邀请计数时出错: {e}", exc_info=True)
        return 0

# 日志操作
async def log_transaction_action(
    transaction_id: int,
    action: str,
    actor_id: int,
    details: Optional[str] = None
) -> int:
    """记录交易操作。"""
    query = """
        INSERT INTO transaction_logs 
        (transaction_id, action, actor_id, details) 
        VALUES (%s, %s, %s, %s)
    """
    return await execute_query(query, (transaction_id, action, actor_id, details))

async def get_transaction_logs(transaction_id: int) -> List[Dict]:
    """获取交易的日志记录列表。
    
    Args:
        transaction_id: 交易ID
        
    Returns:
        交易日志列表，按创建时间排序
    """
    query = """
        SELECT * FROM transaction_logs 
        WHERE transaction_id = %s 
        ORDER BY created_at ASC
    """
    return await fetch_all(query, (transaction_id,))

# 服务器boost操作
async def record_server_boost(user_id: int) -> int:
    """记录用户的服务器boost。"""
    query = """
        INSERT INTO server_boosts (user_id, boost_count) 
        VALUES (%s, 1) 
        ON DUPLICATE KEY UPDATE 
            boost_count = boost_count + 1, 
            last_boost_at = CURRENT_TIMESTAMP
    """
    return await execute_query(query, (user_id,))

async def get_server_boosters() -> List[Dict]:
    """获取所有boost过服务器的用户。"""
    query = "SELECT * FROM server_boosts ORDER BY boost_count DESC"
    return await fetch_all(query)

async def update_booster_last_credits(user_id: int) -> int:
    """更新服务器boost用户的最后获得免费额度时间。"""
    query = """
        UPDATE server_boosts
        SET last_credits_at = CURRENT_TIMESTAMP
        WHERE user_id = %s
    """
    return await execute_query(query, (user_id,))

async def get_boosters_due_for_credits() -> List[Dict]:
    """获取所有应该获得免费额度的boost用户（最后获得时间超过30天或从未获得）。"""
    query = """
        SELECT sb.*, u.username
        FROM server_boosts sb
        JOIN users u ON sb.user_id = u.discord_id
        WHERE 
            sb.is_active = 1 AND
            (sb.last_credits_at IS NULL OR 
            DATEDIFF(CURRENT_TIMESTAMP, sb.last_credits_at) >= 30)
    """
    result = await fetch_all(query)
    logger.info(f"查询到 {len(result)} 个符合条件的boost用户需要发放免费额度")
    
    # 记录详细信息以便调试
    for booster in result:
        user_id = booster['user_id']
        username = booster['username']
        last_credits_at = booster.get('last_credits_at')
        
        if last_credits_at is None:
            logger.info(f"用户 {user_id} ({username}) 从未获得过免费额度，符合发放条件")
        else:
            days_since_last = (datetime.now() - last_credits_at).days
            logger.info(f"用户 {user_id} ({username}) 上次获得免费额度是 {days_since_last} 天前，符合发放条件")
    
    return result

async def add_user_credits(user_id: int, amount: float) -> int:
    """为用户添加免费托管额度。"""
    query = """
        UPDATE users
        SET free_escrow_amount = COALESCE(free_escrow_amount, 0) + %s
        WHERE discord_id = %s
    """
    return await execute_query(query, (amount, user_id))

async def get_user_credits(user_id: int) -> float:
    """获取用户当前免费托管额度。"""
    query = """
        SELECT COALESCE(free_escrow_amount, 0) as free_escrow_amount
        FROM users
        WHERE discord_id = %s
    """
    result = await fetch_one(query, (user_id,))
    return float(result['free_escrow_amount']) if result else 0.0

async def get_invite_by_discord_code(discord_invite_code: str) -> Optional[Dict]:
    """通过Discord邀请代码获取邀请信息。"""
    try:
        # 先检查邀请码是否为空
        if not discord_invite_code:
            logger.warning("查询的邀请码为空")
            return None
        
        # 尝试直接使用原始SQL查询
        try:
            pool = await get_pool()
            query = "SELECT * FROM invitations WHERE invite_code = %s"
            
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:  # 使用DictCursor返回字典
                    await cursor.execute(query, (discord_invite_code,))
                    data = await cursor.fetchone()
                    if data:
                        return data
                    else:
                        # 尝试不区分大小写的查询
                        case_insensitive_query = "SELECT * FROM invitations WHERE LOWER(invite_code) = LOWER(%s)"
                        await cursor.execute(case_insensitive_query, (discord_invite_code,))
                        case_data = await cursor.fetchone()
                        
                        if case_data:
                            return case_data
        except Exception as e:
            logger.error(f"直接SQL查询邀请码时出错: {e}", exc_info=True)
            
            # 回退到使用fetch_one方法
            query = "SELECT * FROM invitations WHERE invite_code = %s"
            result = await fetch_one(query, (discord_invite_code,))
            
            # 检查结果是否有效
            if result and isinstance(result, dict):
                # 检查是否返回了列名而不是值
                if any(k == v for k, v in result.items()):
                    logger.error(f"fetch_one方法返回了列名而不是数据: {result}")
                    return None
                else:
                    return result
        
        return None
    except Exception as e:
        logger.error(f"查询邀请码时出错: {e}", exc_info=True)
        return None

async def check_invite_exists(invite_code: str) -> bool:
    """检查邀请码是否已存在于数据库中。"""
    try:
        # 直接查询邀请码，而不是使用COUNT
        query = "SELECT id FROM invitations WHERE invite_code = %s LIMIT 1"
        result = await fetch_one(query, (invite_code,))
        
        # 如果查询返回结果，说明邀请码存在
        return result is not None and bool(result)
    except Exception as e:
        logger.error(f"检查邀请码存在时出错: {e}", exc_info=True)
        return False

async def get_user_invites(user_id: int) -> List[Dict]:
    """获取用户的所有未使用邀请码"""
    try:
        query = """
            SELECT * FROM invitations 
            WHERE inviter_id = %s AND used = FALSE
            ORDER BY id DESC
        """
        return await fetch_all(query, (user_id,))
    except asyncio.CancelledError:
        # 正确处理协程取消
        raise
    except Exception as e:
        logger.error(f"获取用户邀请失败: {e}", exc_info=True)
        return []

# 持久化消息ID存储
async def save_persistent_message(message_type: str, channel_id: int, message_id: int) -> int:
    """保存持久化消息ID，用于机器人重启后恢复"""
    try:
        # 先删除该类型的旧消息记录
        query_delete = """
            DELETE FROM persistent_messages 
            WHERE message_type = %s AND channel_id = %s
        """
        await execute_query(query_delete, (message_type, channel_id))
        
        # 插入新记录
        query_insert = """
            INSERT INTO persistent_messages 
            (message_type, channel_id, message_id) 
            VALUES (%s, %s, %s)
        """
        return await execute_query(query_insert, (message_type, channel_id, message_id))
    except Exception as e:
        logger.error(f"保存持久化消息失败: {e}", exc_info=True)
        return 0

async def get_persistent_message(message_type: str, channel_id: int) -> Optional[int]:
    """获取持久化消息ID"""
    try:
        query = """
            SELECT message_id FROM persistent_messages 
            WHERE message_type = %s AND channel_id = %s
            LIMIT 1
        """
        result = await fetch_one(query, (message_type, channel_id))
        return result["message_id"] if result else None
    except Exception as e:
        logger.error(f"获取持久化消息失败: {e}", exc_info=True)
        return None

async def verify_invitation(invitee_id: int) -> int:
    """将用户的邀请标记为已验证。
    
    当被邀请用户获得验证身份组时，调用此函数来更新邀请记录。
    这将触发邀请计数的增加，并可能给邀请者提供免费额度奖励。
    
    Args:
        invitee_id: 被邀请用户的Discord ID
        
    Returns:
        int: 受影响的行数
    """
    try:
        # 找到这个用户最近的未验证邀请记录
        query = """
            SELECT iu.id, iu.invite_id, i.inviter_id 
            FROM invitation_usage iu 
            JOIN invitations i ON iu.invite_id = i.id 
            WHERE iu.invitee_id = %s AND iu.verified = FALSE 
            ORDER BY iu.used_at DESC LIMIT 1
        """
        invite_record = await fetch_one(query, (invitee_id,))
        
        if not invite_record:
            logger.warning(f"未找到用户 {invitee_id} 的未验证邀请记录")
            return 0
            
        # 更新邀请记录为已验证
        update_query = """
            UPDATE invitation_usage 
            SET verified = TRUE, verified_at = CURRENT_TIMESTAMP 
            WHERE id = %s
        """
        result = await execute_query(update_query, (invite_record['id'],))
        
        if result > 0:
            logger.info(f"已将用户 {invitee_id} 的邀请记录 {invite_record['id']} 标记为已验证")
            
            # 检查邀请者是否应该获得免费额度奖励
            inviter_id = invite_record['inviter_id']
            if inviter_id:
                await add_free_escrow_for_invites(inviter_id)
            
        return result
    except Exception as e:
        logger.error(f"验证邀请记录时出错: {e}", exc_info=True)
        return 0

async def add_free_escrow_for_invites(inviter_id: int) -> bool:
    """根据用户的已验证邀请数量，可能给用户添加免费额度。
    
    每当用户的已验证邀请数量达到配置的阈值 (INVITE_FREE_ESCROW_THRESHOLD) 的倍数时，
    给用户增加配置的免费额度 (INVITE_FREE_ESCROW_AMOUNT)。
    
    Args:
        inviter_id: 邀请者的Discord ID
        
    Returns:
        bool: 是否成功增加了免费额度
    """
    try:
        # 获取用户已验证的邀请数量
        verified_invites_query = """
            SELECT COUNT(*) as verified_count 
            FROM invitation_usage iu 
            JOIN invitations i ON iu.invite_id = i.id 
            WHERE i.inviter_id = %s AND iu.verified = TRUE
        """
        
        result = await fetch_one(verified_invites_query, (inviter_id,))
        
        if not result:
            logger.warning(f"用户 {inviter_id} 没有已验证邀请记录")
            return False
            
        # 确保我们得到的是整数结果
        try:
            if isinstance(result.get('verified_count'), str) and result.get('verified_count').isdigit():
                verified_count = int(result.get('verified_count'))
            else:
                verified_count = int(result.get('verified_count', 0))
        except (TypeError, ValueError):
            logger.error(f"无法解析已验证邀请数量: {result.get('verified_count')}")
            return False
            
        logger.info(f"用户 {inviter_id} 当前已验证邀请数量: {verified_count}")
        
        # 计算应该获得的奖励次数 (向下取整)
        should_reward_count = verified_count // config.INVITE_FREE_ESCROW_THRESHOLD
        
        # 获取用户信息以检查已获得的奖励次数
        user = await get_user(inviter_id)
        if not user:
            logger.warning(f"无法获取用户 {inviter_id} 的信息")
            return False
            
        # 获取用户已获得的奖励次数
        free_escrow_count = 0
        if 'free_escrow_count' in user:
            try:
                if isinstance(user['free_escrow_count'], str) and user['free_escrow_count'].isdigit():
                    free_escrow_count = int(user['free_escrow_count'])
                else:
                    free_escrow_count = int(user['free_escrow_count']) if user['free_escrow_count'] is not None else 0
            except (TypeError, ValueError):
                logger.error(f"无法解析已获得的奖励次数: {user['free_escrow_count']}")
                free_escrow_count = 0
                
        logger.info(f"用户 {inviter_id} 已获得的奖励次数: {free_escrow_count}")
        
        # 计算未发放的奖励次数 (正确的逻辑：应获得的奖励次数 - 已获得的奖励次数)
        new_rewards = should_reward_count - free_escrow_count
        
        if new_rewards > 0:
            # 获取用户当前的免费额度
            current_amount = await get_user_credits(inviter_id)
            
            # 计算新的免费额度
            reward_amount = new_rewards * config.INVITE_FREE_ESCROW_AMOUNT
            new_amount = current_amount + reward_amount
            
            # 更新用户的免费额度
            await add_user_credits(inviter_id, reward_amount)
            
            # 更新用户的奖励计数
            await update_user_free_escrow_count(inviter_id, should_reward_count)
            
            logger.info(f"用户 {inviter_id} 增加免费额度 {reward_amount}U ({new_rewards}次奖励)，总额度: {new_amount}U")
            return True
        else:
            logger.info(f"用户 {inviter_id} 已获得所有应得的邀请奖励")
            return False
    except Exception as e:
        logger.error(f"为邀请添加免费额度时出错: {e}", exc_info=True)
        return False

async def get_verified_invite_count(user_id: int) -> int:
    """获取特定用户成功邀请的已验证数量。"""
    try:
        query = """
            SELECT COUNT(iu.id) as invite_count 
            FROM invitation_usage iu
            JOIN invitations i ON iu.invite_id = i.id
            WHERE i.inviter_id = %s AND iu.verified = TRUE
        """
        result = await fetch_one(query, (user_id,))
        
        # 增加日志和检查
        if not result:
            logger.warning(f"用户 {user_id} 没有已验证邀请记录")
            return 0
            
        # 检查结果是否为字典且包含invite_count键
        if not isinstance(result, dict):
            logger.error(f"获取用户已验证邀请计数返回了非字典结果: {result}")
            return 0
            
        if 'invite_count' not in result:
            logger.error(f"获取用户已验证邀请计数返回的字典没有invite_count键: {result}")
            return 0
            
        # 检查invite_count是否为列名
        if result.get('invite_count') == 'invite_count':
            logger.error(f"获取用户已验证邀请计数返回了列名而不是值: {result}")
            
            # 尝试直接执行原始SQL查询
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    try:
                        await cursor.execute(query, (user_id,))
                        raw_result = await cursor.fetchone()
                        logger.info(f"原始已验证查询结果: {raw_result}")
                        
                        # 如果raw_result是元组或列表，直接取第一个元素
                        if raw_result and isinstance(raw_result, (tuple, list)) and len(raw_result) > 0:
                            count = raw_result[0]
                            logger.info(f"从原始结果中提取的已验证计数: {count}")
                            
                            # 确保返回的是整数
                            try:
                                return int(count)
                            except (ValueError, TypeError):
                                logger.error(f"无法将已验证计数转换为整数: {count}")
                                return 0
                    except Exception as e:
                        logger.error(f"执行原始已验证邀请SQL查询时出错: {e}")
            
            return 0
            
        # 尝试将结果转换为整数
        try:
            count = int(result.get('invite_count', 0))
            return count
        except (ValueError, TypeError):
            logger.error(f"无法将已验证invite_count转换为整数: {result.get('invite_count')}")
            return 0
    except Exception as e:
        logger.error(f"获取用户已验证邀请计数时出错: {e}", exc_info=True)
        return 0

async def get_inviter_ranking(user_id: int) -> Optional[int]:
    """获取特定用户在邀请排名中的位置。
    
    根据已验证的邀请数量计算用户的排名。如果用户没有邀请或未找到，返回None。
    
    Args:
        user_id: 用户的Discord ID
        
    Returns:
        Optional[int]: 用户的排名（从1开始），如果没有排名则返回None
    """
    try:
        # 首先获取所有邀请者及其已验证邀请数量的排序列表
        query = """
            SELECT 
                i.inviter_id, 
                COUNT(iu.id) as verified_count
            FROM 
                invitations i
            JOIN 
                invitation_usage iu ON i.id = iu.invite_id
            WHERE 
                iu.verified = TRUE
            GROUP BY 
                i.inviter_id
            ORDER BY 
                verified_count DESC
        """
        results = await fetch_all(query)
        
        # 如果没有结果，返回None
        if not results:
            logger.info(f"没有找到任何邀请记录，用户 {user_id} 无法获得排名")
            return None
        
        # 遍历结果找到用户的排名
        for i, row in enumerate(results, 1):
            if int(row['inviter_id']) == user_id:
                logger.info(f"用户 {user_id} 在邀请排名中位于第 {i} 位")
                return i
        
        # 如果未找到用户，返回None
        logger.info(f"用户 {user_id} 在邀请排名中未找到")
        return None
    except Exception as e:
        logger.error(f"获取用户 {user_id} 邀请排名时出错: {e}", exc_info=True)
        return None

async def get_total_inviters_count() -> int:
    """获取有效邀请者的总数量。
    
    只统计至少有一个已验证邀请的用户。
    
    Returns:
        int: 有效邀请者的总数
    """
    try:
        query = """
            SELECT 
                COUNT(DISTINCT i.inviter_id) as total_inviters
            FROM 
                invitations i
            JOIN 
                invitation_usage iu ON i.id = iu.invite_id
            WHERE 
                iu.verified = TRUE
        """
        result = await fetch_one(query)
        
        if not result or 'total_inviters' not in result:
            logger.warning("获取邀请者总数时未找到结果")
            return 0
        
        try:
            count = int(result['total_inviters'])
            logger.info(f"邀请者总数: {count}")
            return count
        except (ValueError, TypeError):
            logger.error(f"无法将邀请者总数转换为整数: {result['total_inviters']}")
            return 0
    except Exception as e:
        logger.error(f"获取邀请者总数时出错: {e}", exc_info=True)
        return 0

async def get_paying_transactions_with_unique_amounts() -> List[Dict]:
    """
    获取所有状态为'paying'的交易及其唯一金额。
    
    Returns:
        List[Dict]: 包含交易ID和唯一金额的字典列表
    """
    query = """
        SELECT id, unique_amount 
        FROM transactions 
        WHERE status = 'paying' AND unique_amount IS NOT NULL
    """
    return await fetch_all(query)

# 抽奖系统相关函数
async def create_giveaway(author_id: int, prize_name: str, winners_count: int, end_time: str, role_ids: str = "", credit_requirement: int = 0, prize_image: str = ""):
    """创建一个新的抽奖"""
    query = """
        INSERT INTO giveaways
        (author_id, prize_name, winners_count, end_time, role_ids, credit_requirement, prize_image, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """
    return await execute_query(
        query, 
        (author_id, prize_name, winners_count, end_time, role_ids, credit_requirement, prize_image, "active")
    )

async def update_giveaway_message(giveaway_id: int, message_id: int, channel_id: int):
    """更新抽奖消息ID和频道ID"""
    query = """
        UPDATE giveaways
        SET message_id = %s, channel_id = %s
        WHERE id = %s
    """
    return await execute_query(query, (message_id, channel_id, giveaway_id))

async def get_giveaway(giveaway_id: int):
    """获取抽奖信息"""
    query = """
        SELECT *
        FROM giveaways
        WHERE id = %s
    """
    return await fetch_one(query, (giveaway_id,))

async def update_giveaway_status(giveaway_id: int, status: str):
    """更新抽奖状态"""
    query = """
        UPDATE giveaways
        SET status = %s
        WHERE id = %s
    """
    return await execute_query(query, (status, giveaway_id))

async def update_giveaway_winners(giveaway_id: int, winner_ids: list):
    """更新抽奖获奖者"""
    winner_ids_str = ','.join(map(str, winner_ids)) if winner_ids else ""
    query = """
        UPDATE giveaways
        SET winner_ids = %s
        WHERE id = %s
    """
    return await execute_query(query, (winner_ids_str, giveaway_id))

async def add_giveaway_participant(giveaway_id: int, user_id: int, credits_used: int = 0):
    """添加抽奖参与者"""
    query = """
        INSERT INTO giveaway_participants
        (giveaway_id, user_id, credits_used, joined_at)
        VALUES (%s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE joined_at = NOW()
    """
    return await execute_query(query, (giveaway_id, user_id, credits_used))

async def has_user_joined_giveaway(giveaway_id: int, user_id: int):
    """检查用户是否已参与抽奖"""
    query = """
        SELECT id
        FROM giveaway_participants
        WHERE giveaway_id = %s AND user_id = %s
    """
    result = await fetch_one(query, (giveaway_id, user_id))
    return result is not None

async def get_giveaway_participants(giveaway_id: int, exclude_user_ids: list = None):
    """获取抽奖参与者"""
    if exclude_user_ids and exclude_user_ids[0]:  # 确保不是空字符串
        placeholders = ','.join(['%s'] * len(exclude_user_ids))
        query = f"""
            SELECT *
            FROM giveaway_participants
            WHERE giveaway_id = %s AND user_id NOT IN ({placeholders})
        """
        params = (giveaway_id,) + tuple(exclude_user_ids)
    else:
        query = """
            SELECT *
            FROM giveaway_participants
            WHERE giveaway_id = %s
        """
        params = (giveaway_id,)
    
    return await fetch_all(query, params)

async def get_active_giveaways():
    """获取所有活跃状态的抽奖"""
    query = """
        SELECT *
        FROM giveaways
        WHERE status = 'active'
    """
    return await fetch_all(query, ())

# 市场模块数据库函数 (从main1.py迁移)

async def get_max_post_limit(member_roles: List[int]) -> int:
    """获取用户的最大发帖限制数量。"""
    max_limit = 0
    for role_id in member_roles:
        if role_id in config.ROLE_LIMITS:
            max_limit = max(max_limit, config.ROLE_LIMITS[role_id])
    return max_limit

async def check_post_limit(user_id: int, max_limit: int, post_type: str = 'normal') -> bool:
    """检查用户是否超过了发帖限制。"""
    today = datetime.now().date()
    column = 'posts_today' if post_type == 'normal' else 'currency_posts_today'
    
    query = f"""
        SELECT {column}, last_post_date 
        FROM user_post_limits 
        WHERE user_id = %s
    """
    result = await fetch_one(query, (user_id,))
    
    if result:
        count = result[column]
        last_date = result['last_post_date']
        if last_date == today:
            return count < max_limit
        return True  # 新日期重置计数
    return True  # 新用户

async def update_post_count(user_id: int, max_limit: int, post_type: str = 'normal'):
    """更新用户发帖计数。"""
    today = datetime.now().date()
    update_field = 'posts_today' if post_type == 'normal' else 'currency_posts_today'
    
    # 先尝试更新现有记录
    query = f"""
        UPDATE user_post_limits
        SET 
            {update_field} = IF(last_post_date = %s, {update_field} + 1, 1),
            last_post_date = %s,
            role_limit = %s
        WHERE user_id = %s
    """
    rows_affected = await execute_query(query, (today, today, max_limit, user_id))
    
    # 如果没有记录被更新，则插入新记录
    if rows_affected == 0:
        query = f"""
            INSERT INTO user_post_limits 
            (user_id, role_limit, {update_field}, last_post_date)
            VALUES (%s, %s, 1, %s)
        """
        await execute_query(query, (user_id, max_limit, today))

async def create_post(user_id: int, channel_id: int, message_id: int) -> int:
    """创建新帖子记录。"""
    created_at = datetime.now()
    
    query = """
        INSERT INTO posts 
        (user_id, channel_id, message_id, created_at)
        VALUES (%s, %s, %s, %s)
    """
    return await execute_query(query, (user_id, channel_id, message_id, created_at))

async def create_currency_trade(
    user_id: int, 
    currency_type: str, 
    trade_type: str, 
    quantity: float, 
    price: float
) -> int:
    """创建新货币交易记录。"""
    try:
        # 防止无效数值 - 安全检查
        if not isinstance(quantity, (int, float)) or not isinstance(price, (int, float)):
            logger.error(f"货币交易参数类型无效: quantity={type(quantity)}, price={type(price)}")
            raise ValueError("无效的数字格式")
            
        # 防止溢出或极大数值
        if quantity <= 0 or quantity > 1000000000:
            logger.error(f"货币交易数量超出范围: {quantity}")
            raise ValueError("数量超出有效范围")
            
        if price <= 0 or price > 1000000000:
            logger.error(f"货币交易价格超出范围: {price}")
            raise ValueError("价格超出有效范围")
        
        created_at = datetime.now()
        expires_at = created_at + timedelta(hours=24)
        
        query = """
        INSERT INTO currency_trades (user_id, currency_type, trade_type, quantity, price, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        
        trade_id = await execute_query(
            query, 
            (user_id, currency_type, trade_type, quantity, price, created_at, expires_at)
        )
        return trade_id
    except Exception as e:
        logger.error(f"创建货币交易时出错: {str(e)}")
        raise

async def get_currency_trades_by_type(currency_type: str = None, trade_type: str = None) -> List[Dict]:
    """获取特定类型的货币交易记录。"""
    conditions = []
    params = []
    
    if currency_type:
        conditions.append("currency_type = %s")
        params.append(currency_type)
    
    if trade_type:
        conditions.append("trade_type = %s")
        params.append(trade_type)
    
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    
    query = f"""
        SELECT * FROM currency_trades
        {where_clause}
        ORDER BY price {'ASC' if trade_type == 'sell' else 'DESC'}
    """
    
    return await fetch_all(query, tuple(params) if params else None)

async def get_currency_trades_by_user(user_id: int) -> List[Dict]:
    """获取用户的货币交易记录。"""
    query = """
        SELECT * FROM currency_trades
        WHERE user_id = %s
    """
    return await fetch_all(query, (user_id,))

async def delete_currency_trade(trade_id: int, user_id: int = None) -> bool:
    """删除货币交易记录。如果指定了user_id，则仅当用户ID匹配时才删除。"""
    query = """
        DELETE FROM currency_trades
        WHERE id = %s
    """
    params = [trade_id]
    
    if user_id is not None:
        query += " AND user_id = %s"
        params.append(user_id)
    
    rows_affected = await execute_query(query, tuple(params))
    return rows_affected > 0

async def admin_delete_currency_trade(trade_id: int) -> bool:
    """管理员删除货币交易记录，不检查用户ID。"""
    return await delete_currency_trade(trade_id)

async def save_summary_message(channel_id: int, message_id: int) -> int:
    """保存或更新货币汇总消息ID。"""
    query = """
        INSERT INTO summary_messages (channel_id, message_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE message_id = %s
    """
    return await execute_query(query, (channel_id, message_id, message_id))

async def get_summary_message(channel_id: int) -> Optional[int]:
    """获取指定频道的汇总消息ID。"""
    query = """
        SELECT message_id FROM summary_messages
        WHERE channel_id = %s
    """
    result = await fetch_one(query, (channel_id,))
    if result:
        return result['message_id']
    return None
