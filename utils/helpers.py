import discord
import logging
import string
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple, Any

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def generate_invite_code(length: int = 8) -> str:
    """生成一个随机的字母数字邀请码。"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def format_amount(amount: float, is_escrow_fee: bool = False) -> str:
    """格式化加密货币金额，使用适当的精度。
    
    Args:
        amount: 金额值
        is_escrow_fee: 是否是担保费用，如果是且金额为0则显示"免费"
    """
    if is_escrow_fee and amount == 0:
        return "免费"
    
    if amount >= 1:
        return f"{amount:.2f} USDT"
    else:
        return f"{amount:.8f} USDT"

def sanitize_input(input_text: str) -> str:
    """净化用户输入以防止SQL注入和其他攻击。"""
    # 移除任何潜在有害的字符
    sanitized = ''.join(char for char in input_text if char.isalnum() or char in ' .,')
    return sanitized.strip()

def is_valid_amount(amount_str: str) -> bool:
    """检查字符串是否为有效的数字金额。"""
    try:
        # 检查科学计数法
        if 'e' in amount_str.lower() or 'E' in amount_str:
            return False
            
        # 检查数字长度以防止溢出
        if len(amount_str.replace(',', '').replace('.', '')) > 15:
            return False
            
        amount = float(amount_str)
        return amount > 0 and amount <= 1000000  # 设置一个合理的上限
    except ValueError:
        return False

def create_embed(
    title: str, 
    description: str, 
    color: discord.Color = discord.Color.blue(),
    fields: List[Dict[str, str]] = None,
    footer_text: str = None,
    thumbnail_url: str = None
) -> discord.Embed:
    """创建具有一致样式的Discord嵌入消息。"""
    embed = discord.Embed(title=title, description=description, color=color)
    
    if fields:
        for field in fields:
            embed.add_field(
                name=field.get("name", ""),
                value=field.get("value", ""),
                inline=field.get("inline", False)
            )
    
    if footer_text:
        embed.set_footer(text=footer_text)
    
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    
    # 添加时间戳
    # embed.timestamp = datetime.now(timezone.utc)
    
    return embed

def create_transaction_embed(
    transaction_type: str,
    item_name: str,
    amount: float,
    escrow_fee: float,
    buyer_name: str,
    seller_name: str,
    status: str = "pending",
    buyer_id: Optional[int] = None,
    seller_id: Optional[int] = None
) -> discord.Embed:
    """为交易信息创建嵌入消息。"""
    title = "交易请求"
    
    if transaction_type == 'trade':
        description = f"**{item_name}** 的交易"
    else:
        description = f"**{item_name}** 的租赁协议"
    
    # 根据状态设置颜色
    colors = {
        'pending': discord.Color.orange(),
        'confirmed': discord.Color.yellow(),
        'paid': discord.Color.blue(),
        'shipped': discord.Color.purple(),
        'confirmed_receipt': discord.Color.teal(),
        'completed': discord.Color.green(),
        'cancelled': discord.Color.red()
    }
    color = colors.get(status, discord.Color.light_grey())
    
    # 格式化买家和卖家字段，如果有ID就使用提及格式
    buyer_value = f"<@{buyer_id}>" if buyer_id else buyer_name
    seller_value = f"<@{seller_id}>" if seller_id else seller_name
    
    # 获取当前时间戳，用于Discord的时间戳格式
    current_timestamp = int(datetime.now(timezone.utc).timestamp())
    
    fields = [
        {"name": "📦 物品", "value": item_name, "inline": True},
        {"name": "💰 价格", "value": format_amount(amount), "inline": True},
        {"name": "🔒 担保费", "value": format_amount(escrow_fee, is_escrow_fee=True), "inline": True},
        {"name": "💵 总计", "value": format_amount(amount + escrow_fee), "inline": True},
        {"name": "🛒 买家", "value": buyer_value, "inline": True},
        {"name": "🏪 卖家", "value": seller_value, "inline": True},
        {"name": "🕒 创建时间", "value": f"<t:{current_timestamp}:F>", "inline": False}
    ]
    
    # 创建嵌入消息
    embed = create_embed(title, description, color, fields)
    
    return embed

def create_rental_embed(
    item_name: str,
    rental_fee: float,
    deposit: float,
    escrow_fee: float,
    rental_period: int,
    renter_name: str,
    owner_name: str,
    status: str = "pending"
) -> discord.Embed:
    """为租赁信息创建嵌入消息。"""
    title = "租赁交易"
    description = f"**{item_name}** 的租赁协议"
    
    # 根据状态设置颜色
    colors = {
        'pending': discord.Color.orange(),
        'confirmed': discord.Color.yellow(),
        'paid': discord.Color.blue(),
        'active': discord.Color.purple(),
        'completed': discord.Color.green(),
        'cancelled': discord.Color.red()
    }
    color = colors.get(status, discord.Color.light_grey())
    
    total_amount = rental_fee + deposit + escrow_fee
    
    fields = [
        {"name": "物品", "value": item_name, "inline": True},
        {"name": "租赁期", "value": f"{rental_period} 天", "inline": True},
        {"name": "租金", "value": format_amount(rental_fee), "inline": True},
        {"name": "押金", "value": format_amount(deposit), "inline": True},
        {"name": "担保费", "value": format_amount(escrow_fee, is_escrow_fee=True), "inline": True},
        {"name": "总计", "value": format_amount(total_amount), "inline": True},
        {"name": "租户", "value": renter_name, "inline": True},
        {"name": "物主", "value": owner_name, "inline": True},
        {"name": "状态", "value": status.capitalize(), "inline": False}
    ]
    
    footer_text = "确认后将提供租赁ID"
    
    return create_embed(title, description, color, fields, footer_text)

def create_invite_embed(user_name: str, invite_code: str) -> discord.Embed:
    """为邀请创建嵌入消息。"""
    title = "Discord邀请"
    description = f"**{user_name}** 已创建邀请链接！"
    
    fields = [
        {"name": "邀请码", "value": invite_code, "inline": False},
        {"name": "说明", "value": "与朋友分享此邀请码以获得奖励。当他们加入并完成验证时，您将获得积分。", "inline": False}
    ]
    
    footer_text = "邀请奖励每月发放"
    
    return create_embed(title, description, discord.Color.gold(), fields, footer_text)

def calculate_rental_end_date(start_date: datetime, rental_period: int) -> datetime:
    """根据开始日期和租赁期限（天数）计算租赁结束日期。"""
    return start_date + timedelta(days=rental_period)

def calculate_time_remaining(end_date: datetime) -> str:
    """计算并格式化距离给定结束日期的剩余时间。"""
    now = datetime.now(timezone.utc)
    if now >= end_date:
        return "已过期"
    
    delta = end_date - now
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    if days > 0:
        return f"{days} 天, {hours} 小时"
    elif hours > 0:
        return f"{hours} 小时, {minutes} 分钟"
    else:
        return f"{minutes} 分钟"

async def auto_delete_message(message: discord.Message, delay: int = 60) -> None:
    """在延迟（秒）后自动删除消息。"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.NotFound:
        pass  # 消息已被删除
    except Exception as e:
        logger.error(f"删除消息时出错: {e}")

async def wait_for_confirmation(
    ctx: discord.ApplicationContext,
    user_id: int,
    timeout: int = 300
) -> Optional[bool]:
    """等待确认按钮按下。"""
    def check(interaction):
        return interaction.user.id == user_id and interaction.data["custom_id"] in ["confirm", "cancel"]
    
    try:
        interaction = await ctx.bot.wait_for("interaction", check=check, timeout=timeout)
        return interaction.data["custom_id"] == "confirm"
    except asyncio.TimeoutError:
        return None 