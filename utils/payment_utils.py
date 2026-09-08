import random
import os
import qrcode
from io import BytesIO
import discord
from typing import Tuple, Optional, List, Dict
from decimal import Decimal, getcontext
import logging
from utils import database

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def generate_unique_amount(base_amount: float, transaction_id: int) -> Tuple[float, str]:
    """
    基于交易ID为基础金额生成唯一的金额，确保不与当前正在支付状态的交易金额重复。
    
    Args:
        base_amount: 基础金额（正常显示两位小数）
        transaction_id: 交易ID
        
    Returns:
        Tuple[float, str]: 包含唯一金额的浮点数和格式化字符串
    """
    # 设置精度为8位，确保能处理小数
    getcontext().prec = 8
    
    # 格式化为2位小数的字符串 (正常价格表示)
    base_amount_rounded = round(base_amount, 2)
    
    # 获取所有正在支付状态的交易及其唯一金额
    paying_transactions = await database.get_paying_transactions_with_unique_amounts()
    existing_amounts = [float(tx['unique_amount']) for tx in paying_transactions if tx['id'] != transaction_id]
    
    # 使用集合来快速检查金额是否已存在
    existing_amounts_set = set(existing_amounts)
    
    # 设置尝试次数上限，避免无限循环
    max_attempts = 100
    attempts = 0
    
    while attempts < max_attempts:
        # 使用交易ID和尝试次数作为随机种子，确保每次生成不同的随机数
        random.seed(transaction_id + attempts)
        
        # 生成4位随机数作为唯一验证码
        unique_digits = random.randint(1000, 9999)
        
        # 构建唯一金额：基础金额 + 验证码/1000000
        # 这将使验证码成为小数点后第3-6位数字
        # 注意：数据库列为DECIMAL(18,6)，需确保精度匹配
        unique_amount = base_amount_rounded + (unique_digits / 1000000)
        
        # 检查是否与已存在的金额重复
        if unique_amount not in existing_amounts_set:
            # 格式化为字符串，确保显示6位小数，符合数据库DECIMAL(18,6)精度
            formatted_amount = f"{unique_amount:.6f}"
            
            # 转回浮点数，确保精确到6位小数，避免数据库截断问题
            unique_amount = round(float(formatted_amount), 6)
            
            logger.info(f"生成唯一金额: 基础={base_amount}, 验证码={unique_digits}, 唯一={unique_amount}, 格式化={formatted_amount}, 尝试次数={attempts+1}")
            
            return unique_amount, formatted_amount
        
        attempts += 1
    
    # 如果达到最大尝试次数仍未找到唯一金额，使用最后一次生成的金额
    # 确保精确到6位小数，避免数据库截断问题
    unique_amount = round(unique_amount, 6)
    formatted_amount = f"{unique_amount:.6f}"
    logger.warning(f"未能生成唯一金额，使用最后一次生成的金额: {formatted_amount}")
    
    return unique_amount, formatted_amount

