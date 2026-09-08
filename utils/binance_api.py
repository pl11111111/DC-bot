import logging
import aiohttp
import json
import hmac
import hashlib
import time
from typing import Dict, List, Optional, Any, Tuple
import config
from utils import redis_client

# 自定义日志过滤器，过滤掉高频的API调用日志
class BinanceAPILogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        # 过滤掉高频的API调用详情日志
        if any(msg in message for msg in [
            "API请求: GET",
            "API请求: POST",
            "参数: {",
            "查询字符串:",
            "使用默认时间窗口:",
            "检查地址",
            "找到",
            "未找到地址",
            "主要部分比较:",
            "验证部分比较:",
            "检查存款:"
        ]):
            return False
        return True

# 配置日志
logger = logging.getLogger(__name__)
# 添加过滤器
logger.addFilter(BinanceAPILogFilter())

# Binance API端点
BASE_URL = "https://api.binance.com"
DEPOSIT_ADDRESS_ENDPOINT = "/sapi/v1/capital/deposit/address"
WITHDRAW_ENDPOINT = "/sapi/v1/capital/withdraw/apply"
ASSET_DETAIL_ENDPOINT = "/sapi/v1/asset/assetDetail"
TRANSACTION_DETAIL_ENDPOINT = "/sapi/v1/capital/deposit/hisrec"
DEPOSIT_HISTORY_ENDPOINT = "/sapi/v1/capital/deposit/hisrec"

async def generate_signature(query_string: str) -> str:
    """为Binance API生成HMAC SHA256签名。"""
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

async def make_api_request(
    endpoint: str,
    method: str = "GET",
    params: Dict = None,
    needs_signature: bool = True
) -> Optional[Dict]:
    """向Binance API发出请求。"""
    headers = {
        "X-MBX-APIKEY": config.BINANCE_API_KEY
    }
    
    url = BASE_URL + endpoint
    
    if params is None:
        params = {}
    
    # 确保所有参数值都是字符串
    for key in list(params.keys()):
        if params[key] is not None:
            params[key] = str(params[key])
    
    # 为需要签名的端点添加时间戳
    if needs_signature:
        # 添加时间戳
        params["timestamp"] = str(int(time.time() * 1000))
        
        # 按照Binance要求处理参数（URL编码）
        import urllib.parse
        query_string = urllib.parse.urlencode(params)
        
        # 生成签名
        signature = hmac.new(
            config.BINANCE_API_SECRET.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        # 添加签名到查询参数（不参与再次签名）
        params["signature"] = signature
        
        # 记录完整的请求信息以便调试
        logger.info(f"API请求: {method} {endpoint}")
        logger.info(f"参数: {params}")
        logger.info(f"查询字符串: {query_string}")
    
    try:
        async with aiohttp.ClientSession() as session:
            if method == "GET":
                async with session.get(url, params=params, headers=headers) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            return result
                        except json.JSONDecodeError:
                            logger.error(f"无法解析JSON响应: {response_text}")
                            return None
                    else:
                        logger.error(f"Binance API错误 ({response.status}): {response_text}")
                        logger.error(f"请求URL: {url}")
                        logger.error(f"请求参数: {params}")
                        return None
            elif method == "POST":
                async with session.post(url, params=params, headers=headers) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            return result
                        except json.JSONDecodeError:
                            logger.error(f"无法解析JSON响应: {response_text}")
                            return None
                    else:
                        logger.error(f"Binance API错误 ({response.status}): {response_text}")
                        logger.error(f"请求URL: {url}")
                        logger.error(f"请求参数: {params}")
                        return None
    except Exception as e:
        logger.error(f"向Binance发出API请求时出错: {e}")
        logger.error(f"请求URL: {url}")
        logger.error(f"请求参数: {params}")
        return None

async def get_deposit_address(coin: str = "USDT", network: str = "BSC", transaction_id: Optional[int] = None) -> Optional[str]:
    """获取特定币种和网络的存款地址。
    
    Args:
        coin: 币种，默认为USDT
        network: 网络，默认为BSC
        transaction_id: 交易ID，用于生成唯一的存款地址
        
    Returns:
        存款地址，如果获取失败则返回None
    """
    params = {
        "coin": coin,
        "network": network
    }
    
    # 如果提供了交易ID，添加交易特定的标识
    address_label = None
    if transaction_id is not None:
        # 添加交易ID作为地址标签，使得每个交易都有唯一的地址标识
        address_label = f"tx_{transaction_id}"
        params["label"] = address_label
    
    response = await make_api_request(DEPOSIT_ADDRESS_ENDPOINT, "GET", params)
    
    if response and "address" in response:
        # 如果响应中包含tag或memo字段，返回地址和tag/memo
        address = response["address"]
        if transaction_id is not None:
            # 对于某些币种，可能需要使用memo或tag来区分不同用户的存款
            if "tag" in response:
                return f"{address}:{response['tag']}"
            elif "memo" in response:
                return f"{address}:{response['memo']}"
        return address
    return None

async def withdraw_funds(
    coin: str,
    address: str,
    amount: float,
    network: str = "BSC",
    name: str = "Transaction"
) -> Optional[str]:
    """提取资金到地址。
    
    Args:
        coin: 币种，如USDT
        address: 接收地址
        amount: 提取金额
        network: 网络，默认BSC (BEP20)
        name: 提现标签/描述
    
    Returns:
        成功时返回提现ID，失败时返回None
    """
    # 地址验证
    if not address or len(address) < 10:
        logger.error(f"提现地址无效: {address}")
        return None
        
    # 金额验证
    try:
        float_amount = float(amount)
        if float_amount <= 0:
            logger.error(f"提现金额必须大于0: {amount}")
            return None
    except ValueError:
        logger.error(f"提现金额格式无效: {amount}")
        return None
    
    # 清理描述字段，避免特殊字符
    clean_name = ''.join(c for c in name if c.isalnum() or c in '_-')
    if not clean_name:
        clean_name = f"Tx{int(time.time())}"
    
    # 准备参数 - 精确控制类型和格式
    params = {
        "coin": str(coin).upper(),  # 确保大写
        "address": str(address).strip(),  # 移除前后空格
        "amount": format(float_amount, '.6f'),  # 固定精度格式
        "network": str(network).upper(),  # 确保大写
        "name": clean_name  # 使用清理后的名称
    }
    
    logger.info(f"尝试提现: 币种={params['coin']}, 网络={params['network']}, 金额={params['amount']}, 地址={params['address'][:10]}...")
    
    # 发送API请求
    response = await make_api_request(WITHDRAW_ENDPOINT, "POST", params)
    
    if response and "id" in response:
        logger.info(f"提现请求成功，ID: {response['id']}")
        return response["id"]
    else:
        if response:
            logger.error(f"提现请求失败，返回: {response}")
        else:
            logger.error("提现请求失败，无响应")
        return None

async def check_transaction_status(txid: str, coin: str = "USDT") -> Optional[Dict]:
    """检查存款交易的状态。"""
    params = {
        "txId": txid,
        "coin": coin
    }
    
    response = await make_api_request(TRANSACTION_DETAIL_ENDPOINT, "GET", params)
    
    if response and isinstance(response, list) and len(response) > 0:
        return response[0]
    return None

async def is_transaction_confirmed(txid: str, coin: str = "USDT") -> bool:
    """检查交易是否已确认（完成并有足够的确认数）。"""
    # 对于Off-chain转账，可以直接认为已确认
    if txid and "Off-chain transfer" in txid:
        logger.info(f"检测到Off-chain转账 {txid}，自动视为已确认")
        return True
        
    tx_info = await check_transaction_status(txid, coin)
    
    if tx_info and "status" in tx_info:
        return tx_info["status"] == 1  # 1表示存款成功
    return False

async def get_network_fee(coin: str = "USDT", network: str = "BSC") -> Optional[float]:
    """获取特定币种和网络的提现费用。"""
    params = {
        "coin": coin
    }
    
    response = await make_api_request(ASSET_DETAIL_ENDPOINT, "GET", params)
    
    if response and coin in response:
        coin_details = response[coin]
        for network_info in coin_details.get("networkList", []):
            if network_info.get("network") == network:
                return float(network_info.get("withdrawFee", 0))
    return None

async def process_escrow_payment(
    transaction_id: int,
    buyer_address: str,
    amount: float,
    description: str
) -> Tuple[bool, Optional[str], Optional[str]]:
    """处理从买家到托管账户的托管付款。"""
    try:
        # 为交易生成新的存款地址
        deposit_address = await get_deposit_address()
        
        if not deposit_address:
            return False, None, "生成存款地址失败"
        
        # 返回地址给用户，用户将进行支付
        return True, deposit_address, None
    except Exception as e:
        logger.error(f"处理交易 {transaction_id} 的托管付款时出错: {e}")
        return False, None, str(e)

async def release_escrow_payment(
    transaction_id: int,
    recipient_address: str,
    amount: float
) -> Tuple[bool, Optional[str], Optional[str]]:
    """向收款方释放托管付款。
    
    Args:
        transaction_id: 交易ID
        recipient_address: 收款方的区块链地址（而非用户ID）
        amount: 释放金额
        
    Returns:
        成功标志, 提现ID, 错误信息
    """
    try:
        # 地址验证 - 基本检查
        if not recipient_address or len(recipient_address) < 10:
            logger.error(f"提供的接收地址无效: {recipient_address}")
            return False, None, "提供的接收地址无效，请检查地址格式"
        
        # 记录尝试
        logger.info(f"尝试为交易 {transaction_id} 释放资金到地址 {recipient_address}，金额 {amount}")

        # 使用安全的键名，避免Redis键问题
        safe_tx_id = str(transaction_id).replace(" ", "_").lower()

        # 检查该交易是否已经触发了资金释放
        withdrawal_key = f"withdrawal_lock:{safe_tx_id}"
        existing_withdrawal = await redis_client.get_value(withdrawal_key)
        if existing_withdrawal:
            logger.warning(f"交易 {transaction_id} 已经触发了资金释放，ID: {existing_withdrawal}")
            return False, None, f"此交易已经触发了资金释放，ID: {existing_withdrawal}"
        
        # 使用分布式锁确保原子操作
        lock_key = f"release_funds_lock:{safe_tx_id}"
        lock_value = await redis_client.acquire_lock(lock_key, 60)  # 60秒锁
        
        if not lock_value:
            logger.warning(f"无法获取交易 {transaction_id} 的资金释放锁，可能正在处理中")
            return False, None, "系统正在处理您的请求，请稍候再试"
        
        try:
            # 双重检查该交易是否已经触发了资金释放
            existing_withdrawal = await redis_client.get_value(withdrawal_key)
            if existing_withdrawal:
                logger.warning(f"二次检查: 交易 {transaction_id} 已经触发了资金释放，ID: {existing_withdrawal}")
                return False, None, f"此交易已经触发了资金释放，ID: {existing_withdrawal}"
            
            # 确保金额为浮点数并保留适当精度
            try:
                float_amount = float(amount)
                if float_amount <= 0:
                    return False, None, "释放金额必须大于0"
            except ValueError:
                return False, None, "金额格式无效"
            
            # 将资金提取到收款方地址
            withdrawal_id = await withdraw_funds(
                coin="USDT",
                address=recipient_address,
                amount=float_amount,
                network="BSC",  # BEP20网络
                name=f"tx{transaction_id}"  # 使用简洁的名称，避免空格或特殊字符
            )
            
            if not withdrawal_id:
                logger.error(f"提取资金失败，交易ID: {transaction_id}, 地址: {recipient_address}, 金额: {amount}")
                return False, None, "提取资金失败，可能是地址错误或API限制"
            
            # 使用安全的ID处理Off-chain transfers
            safe_withdrawal_id = withdrawal_id
            if withdrawal_id and "Off-chain" in withdrawal_id:
                safe_withdrawal_id = withdrawal_id.replace(" ", "_").lower()
            
            # 记录此交易已经触发了资金释放
            await redis_client.set_value(withdrawal_key, safe_withdrawal_id, 86400 * 7)  # 保存7天
            
            logger.info(f"成功释放交易 {transaction_id} 的资金，提现ID: {withdrawal_id}")
            return True, withdrawal_id, None
        finally:
            # 无论如何都释放锁
            await redis_client.release_lock(lock_key, lock_value)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"释放交易 {transaction_id} 的托管付款时出错: {error_msg}")
        return False, None, f"系统错误: {error_msg}"

async def check_deposit_by_amount(address: str, expected_amount: float, transaction_id: Optional[int] = None, time_window: int = 1800) -> Optional[str]:
    """根据金额检查存款。
    
    Args:
        address: 存款地址
        expected_amount: 预期金额
        transaction_id: 交易ID，用于获取确切的支付请求时间
        time_window: 搜索的时间窗口（秒），默认30分钟
        
    Returns:
        匹配金额的交易ID，如果不存在则返回None
    """
    # 获取最近的存款历史
    from datetime import datetime
    
    now = int(datetime.now().timestamp() * 1000)  # 当前时间戳（毫秒）
    
    # 如果提供了交易ID，尝试获取该交易的支付请求时间
    start_time = None
    if transaction_id:
        payment_timestamp = await redis_client.get_payment_timestamp(transaction_id)
        if payment_timestamp:
            # 使用支付请求时间作为开始时间
            start_time = int(payment_timestamp) * 1000  # 转换为毫秒
            logger.info(f"使用交易 {transaction_id} 的支付请求时间: {payment_timestamp} 作为开始时间")
    
    # 如果没有交易特定的时间戳，使用默认的时间窗口
    if not start_time:
        start_time = now - (time_window * 1000)  # 开始时间（毫秒）
        logger.info(f"使用默认时间窗口: {time_window}秒")
    
    logger.info(f"检查地址 {address} 的存款，预期金额: {expected_amount}，开始时间: {start_time}")
    
    params = {
        "coin": "USDT",
        "status": 1,  # 1表示已完成的存款
        "startTime": start_time,
        "endTime": now
    }
    
    deposits = await make_api_request(DEPOSIT_HISTORY_ENDPOINT, "GET", params)
    
    if not deposits or not isinstance(deposits, list):
        logger.info(f"未找到地址 {address} 的存款记录")
        return None
    
    logger.info(f"找到 {len(deposits)} 条存款记录，开始匹配")
    
    # 查找匹配地址和金额的存款
    for deposit in deposits:
        # 精确匹配金额（考虑小数精度问题）
        deposit_amount = float(deposit.get("amount", 0))
        amount_diff = abs(deposit_amount - expected_amount)
        deposit_address = deposit.get("address", "")
        deposit_time = deposit.get("insertTime", 0)
        txid = deposit.get("txId")
        
        # 特殊处理Off-chain交易ID，它们包含空格，可能导致Redis键问题
        # 将"Off-chain transfer"替换为"off_chain_transfer"
        safe_txid = txid
        if txid and "Off-chain" in txid:
            safe_txid = txid.replace(" ", "_").lower()
        
        # 记录每个存款的信息以便调试
        logger.info(f"检查存款: 地址={deposit_address}, 金额={deposit_amount}, 差额={amount_diff}, 时间={deposit_time}")
        
        # 将浮点数格式化为字符串，然后比较主要部分和验证部分
        expected_str = f"{expected_amount:.6f}"
        deposit_str = f"{deposit_amount:.6f}"
        
        # 获取预期金额的整数部分和前两位小数（主要部分）
        expected_main_part = expected_str.split('.')[0]
        if '.' in expected_str and len(expected_str.split('.')[1]) >= 2:
            expected_main_part += '.' + expected_str.split('.')[1][:2]
            
        # 获取存款金额的整数部分和前两位小数（主要部分）
        deposit_main_part = deposit_str.split('.')[0]
        if '.' in deposit_str and len(deposit_str.split('.')[1]) >= 2:
            deposit_main_part += '.' + deposit_str.split('.')[1][:2]
            
        # 获取预期金额的验证部分（后4位小数）
        expected_verify_part = ''
        if '.' in expected_str and len(expected_str.split('.')[1]) >= 6:
            expected_verify_part = expected_str.split('.')[1][2:6]
            
        # 获取存款金额的验证部分（后4位小数）
        deposit_verify_part = ''
        if '.' in deposit_str and len(deposit_str.split('.')[1]) >= 6:
            deposit_verify_part = deposit_str.split('.')[1][2:6]
        
        logger.info(f"主要部分比较: 预期={expected_main_part}, 存款={deposit_main_part}")
        logger.info(f"验证部分比较: 预期={expected_verify_part}, 存款={deposit_verify_part}")
        
        # 1. 地址必须匹配
        # 2. 主要部分（整数+前2位小数）必须匹配
        # 3. 验证部分（后4位小数）必须匹配
        if (deposit_address == address and 
            expected_main_part == deposit_main_part and 
            expected_verify_part == deposit_verify_part):
            
            # 检查此交易是否已被其他交易认领
            claimed_by = await redis_client.get_claimed_deposit(safe_txid)
            if claimed_by:
                logger.info(f"存款 {txid} 已被交易 {claimed_by} 认领，跳过")
                continue
            
            # 在Redis中记录此存款已被当前交易认领（使用锁机制确保原子操作）
            lock_key = f"deposit_claim_lock:{safe_txid}"
            claim_lock = await redis_client.acquire_lock(lock_key, 30)  # 30秒锁
            
            if not claim_lock:
                logger.info(f"无法获取存款 {txid} 的锁，可能正被其他进程处理，跳过")
                continue
            
            try:
                # 再次检查是否已被认领（双重检查）
                claimed_by = await redis_client.get_claimed_deposit(safe_txid)
                if claimed_by:
                    logger.info(f"二次检查: 存款 {txid} 已被交易 {claimed_by} 认领，跳过")
                    continue
                
                # 标记此存款已被当前交易认领
                if transaction_id:
                    await redis_client.set_claimed_deposit(safe_txid, transaction_id)
                    logger.info(f"标记存款 {txid} 已被交易 {transaction_id} 认领")
                
                logger.info(f"找到匹配的存款! TxID: {txid}")
                return txid
            finally:
                # 无论如何都释放锁
                await redis_client.release_lock(lock_key, claim_lock)
    
    logger.info(f"未找到匹配的存款记录")
    return None 