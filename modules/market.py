import discord
from discord.ext import commands, tasks
from discord import ApplicationContext, Option, SlashCommandGroup
import datetime
import os
import re
import logging
import asyncio
from typing import Optional, List
from datetime import datetime, timedelta

import config
from utils import database, helpers

# 自定义日志过滤器，过滤掉高频重复日志
class MarketLogFilter(logging.Filter):
    def filter(self, record):
        # 忽略一些高频出现的错误日志
        if record.levelno >= logging.ERROR:
            message = record.getMessage()
            if "Option does not take min_value or max_value" in message:
                return False
        return True

# 配置日志
logger = logging.getLogger(__name__)
# 添加过滤器
logger.addFilter(MarketLogFilter())

# 货币交易输入模态框
class CurrencyTradeModal(discord.ui.Modal):
    def __init__(self, currency_type: str, trade_type: str, bot, callback_cog):
        self.currency_type = currency_type
        self.trade_type = trade_type
        self.bot = bot
        self.callback_cog = callback_cog
        
        title = f"{'出售' if trade_type == 'sell' else '求购'} {currency_type}"
        super().__init__(title=title, timeout=300)
        
        self.quantity = discord.ui.InputText(
            label="数量",
            placeholder="输入整数数量",
            style=discord.InputTextStyle.short,
            required=True
        )
        self.add_item(self.quantity)
        
        self.price = discord.ui.InputText(
            label="单价",
            placeholder="输入数字价格",
            style=discord.InputTextStyle.short,
            required=True
        )
        self.add_item(self.price)

    async def callback(self, interaction: discord.Interaction):
        try:
            # 立即延迟响应，防止交互超时
            await interaction.response.defer(ephemeral=True)
            
            # 获取输入数据
            quantity = self.quantity.value
            price = self.price.value
            
            # 验证输入的数字格式
            try:
                # 检查是否包含SQL注入特征或其他非法字符
                valid_number_pattern = r'^[0-9]+(\.[0-9]+)?$'
                
                # 检测SQL注入攻击模式
                sql_injection_patterns = [
                    r"'\s*OR",
                    r"'\s*--",
                    r"'\s*;",
                    r"SELECT",
                    r"INSERT",
                    r"DELETE",
                    r"UPDATE",
                    r"DROP",
                    r"UNION",
                    r"WHERE",
                    r"1\s*=\s*1"
                ]
                
                # 检查数量输入
                for pattern in sql_injection_patterns:
                    if re.search(pattern, quantity, re.IGNORECASE):
                        logger.warning(f"检测到可能的SQL注入尝试: {quantity}")
                        await interaction.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                        return
                        
                # 检查价格输入
                for pattern in sql_injection_patterns:
                    if re.search(pattern, price, re.IGNORECASE):
                        logger.warning(f"检测到可能的SQL注入尝试: {price}")
                        await interaction.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                        return
                
                # 验证是否为有效数字格式
                if not re.match(valid_number_pattern, quantity):
                    await interaction.followup.send("❌ 数量必须是正数，仅允许数字和小数点", ephemeral=True)
                    return
                    
                if not re.match(valid_number_pattern, price):
                    await interaction.followup.send("❌ 价格必须是正数，仅允许数字和小数点", ephemeral=True)
                    return
                
                # 检查其他格式或字符和极大数值
                if 'e' in quantity.lower() or 'e' in price.lower():
                    await interaction.followup.send("❌ 不允许使用科学计数法 (e)", ephemeral=True)
                    return
                
                # 检查数字长度以防止溢出
                if len(quantity.replace('.', '')) > 15 or len(price.replace('.', '')) > 15:
                    await interaction.followup.send("❌ 数字过大，请输入合理的数值", ephemeral=True)
                    return
                
                # 转换为浮点数进行验证
                quantity = float(quantity)
                if quantity <= 0:
                    await interaction.followup.send("❌ 数量必须大于0", ephemeral=True)
                    return
                    
                # 检查数量是否合理 (防止极大值)
                if quantity > 1000000000:
                    await interaction.followup.send("❌ 数量超出合理范围", ephemeral=True)
                    return
                
                price = float(price)
                if price <= 0:
                    await interaction.followup.send("❌ 价格必须大于0", ephemeral=True)
                    return
                
                # 检查价格是否合理 (防止极大值)
                if price > 1000000000:
                    await interaction.followup.send("❌ 价格超出合理范围", ephemeral=True)
                    return
            except ValueError as e:
                await interaction.followup.send(f"输入无效: {str(e)}", ephemeral=True)
                return
            
            # 获取用户角色ID列表
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
                if not member:
                    await interaction.followup.send("❌ 无法获取用户信息", ephemeral=True)
                    return
                    
                member_roles = [role.id for role in member.roles if hasattr(role, 'id')]
                
                # 获取用户的最大发帖限制
                max_limit = await database.get_max_post_limit(member_roles)
                
                if max_limit == 0:
                    await interaction.followup.send("❌ 你没有发帖权限", ephemeral=True)
                    return
                    
                # 检查是否超过发帖限制
                if not await database.check_post_limit(interaction.user.id, max_limit, 'currency'):
                    await interaction.followup.send(f"❌ 已达到每日限额（{max_limit}次/天）", ephemeral=True)
                    return
                
                # 创建货币交易记录
                try:
                    await database.create_currency_trade(
                        interaction.user.id,
                        self.currency_type,
                        self.trade_type,
                        quantity,
                        price
                    )
                except Exception as db_error:
                    logger.error(f"数据库创建货币交易失败: {db_error}")
                    await interaction.followup.send(f"❌ 创建交易失败: {str(db_error)}", ephemeral=True)
                    return
                
                # 更新发帖计数
                await database.update_post_count(interaction.user.id, max_limit, 'currency')
                
                # 更新货币交易汇总
                await self.callback_cog.update_currency_summary()
                
                # 发送成功消息
                await interaction.followup.send("✅ 交易信息已添加到市场！", ephemeral=True)
            except Exception as member_error:
                logger.error(f"处理用户和交易数据时出错: {member_error}")
                await interaction.followup.send(f"❌ 处理请求时出错: {str(member_error)}", ephemeral=True)
        except Exception as e:
            logger.error(f"创建货币交易失败: {e}")
            # 尝试使用followup发送错误消息
            try:
                await interaction.followup.send(f"❌ 创建交易失败: {str(e)}", ephemeral=True)
            except Exception as follow_error:
                logger.error(f"无法发送交易错误消息: {follow_error}")

# 货币交易按钮
class CurrencyTradeButton(discord.ui.Button):
    """货币交易按钮"""
    
    def __init__(self, currency_type, trade_type, bot, market_cog):
        # 设置按钮标签和样式
        if trade_type == "sell":
            label = f"出售 {currency_type}"
            style = discord.ButtonStyle.success  # 绿色
            emoji = "💰"
        else:  # buy
            label = f"求购 {currency_type}"
            style = discord.ButtonStyle.primary  # 蓝色
            emoji = "🛒"
            
        super().__init__(style=style, label=label, emoji=emoji)
        self.currency_type = currency_type
        self.trade_type = trade_type
        self.bot = bot
        self.market_cog = market_cog
        
    async def callback(self, interaction: discord.Interaction):
        """按钮点击回调"""
        # 创建模态框
        modal = discord.ui.Modal(title=f"{self.label} 信息")
        
        # 数量输入
        quantity_input = discord.ui.InputText(
            label=f"数量",
            placeholder="输入您想要交易的数量",
            required=True,
            custom_id="quantity",
            style=discord.InputTextStyle.short
        )
        
        # 价格输入
        price_input = discord.ui.InputText(
            label=f"单价 (USDT)",
            placeholder="输入单价",
            required=True,
            custom_id="price",
            style=discord.InputTextStyle.short
        )
        
        # 添加输入框到模态框
        modal.add_item(quantity_input)
        modal.add_item(price_input)
        
        # 定义模态框提交回调
        async def modal_callback(modal_interaction):
            try:
                # 立即延迟响应，防止交互超时
                await modal_interaction.response.defer(ephemeral=True)
                
                # 获取输入数据
                quantity = quantity_input.value
                price = price_input.value
                
                # 验证输入的数字格式
                try:
                    # 检查是否包含SQL注入特征或其他非法字符
                    valid_number_pattern = r'^[0-9]+(\.[0-9]+)?$'
                    
                    # 检测SQL注入攻击模式
                    sql_injection_patterns = [
                        r"'\s*OR",
                        r"'\s*--",
                        r"'\s*;",
                        r"SELECT",
                        r"INSERT",
                        r"DELETE",
                        r"UPDATE",
                        r"DROP",
                        r"UNION",
                        r"WHERE",
                        r"1\s*=\s*1"
                    ]
                    
                    # 检查数量输入
                    for pattern in sql_injection_patterns:
                        if re.search(pattern, quantity, re.IGNORECASE):
                            logger.warning(f"检测到可能的SQL注入尝试: {quantity}")
                            await modal_interaction.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                            return
                            
                    # 检查价格输入
                    for pattern in sql_injection_patterns:
                        if re.search(pattern, price, re.IGNORECASE):
                            logger.warning(f"检测到可能的SQL注入尝试: {price}")
                            await modal_interaction.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                            return
                    
                    # 验证是否为有效数字格式
                    if not re.match(valid_number_pattern, quantity):
                        await modal_interaction.followup.send("❌ 数量必须是正数，仅允许数字和小数点", ephemeral=True)
                        return
                        
                    if not re.match(valid_number_pattern, price):
                        await modal_interaction.followup.send("❌ 价格必须是正数，仅允许数字和小数点", ephemeral=True)
                        return
                    
                    # 检查其他格式或字符和极大数值
                    if 'e' in quantity.lower() or 'e' in price.lower():
                        await modal_interaction.followup.send("❌ 不允许使用科学计数法 (e)", ephemeral=True)
                        return
                    
                    # 检查数字长度以防止溢出
                    if len(quantity.replace('.', '')) > 15 or len(price.replace('.', '')) > 15:
                        await modal_interaction.followup.send("❌ 数字过大，请输入合理的数值", ephemeral=True)
                        return
                    
                    # 转换为浮点数进行验证
                    quantity = float(quantity)
                    if quantity <= 0:
                        await modal_interaction.followup.send("❌ 数量必须大于0", ephemeral=True)
                        return
                        
                    # 检查数量是否合理 (防止极大值)
                    if quantity > 1000000000:
                        await modal_interaction.followup.send("❌ 数量超出合理范围", ephemeral=True)
                        return
                    
                    price = float(price)
                    if price <= 0:
                        await modal_interaction.followup.send("❌ 价格必须大于0", ephemeral=True)
                        return
                    
                    # 检查价格是否合理 (防止极大值)
                    if price > 1000000000:
                        await modal_interaction.followup.send("❌ 价格超出合理范围", ephemeral=True)
                        return
                except ValueError as e:
                    await modal_interaction.followup.send(f"输入无效: {str(e)}", ephemeral=True)
                    return
                    
                # 获取用户角色ID列表
                try:
                    member = await interaction.guild.fetch_member(modal_interaction.user.id)
                    if not member:
                        await modal_interaction.followup.send("❌ 无法获取用户信息", ephemeral=True)
                        return
                        
                    member_roles = [role.id for role in member.roles if hasattr(role, 'id')]
                    
                    # 获取用户的最大发帖限制
                    max_limit = await database.get_max_post_limit(member_roles)
                    
                    if max_limit == 0:
                        await modal_interaction.followup.send("❌ 你没有发帖权限", ephemeral=True)
                        return
                        
                    # 检查是否超过发帖限制
                    if not await database.check_post_limit(modal_interaction.user.id, max_limit, 'currency'):
                        await modal_interaction.followup.send(f"❌ 已达到每日限额（{max_limit}次/天）", ephemeral=True)
                        return
                
                    # 记录交易到数据库 - 移除未使用的变量
                    try:
                        await database.create_currency_trade(
                            modal_interaction.user.id, 
                            self.currency_type, 
                            self.trade_type, 
                            quantity, 
                            price
                        )
                    except Exception as db_error:
                        logger.error(f"数据库创建货币交易失败: {db_error}")
                        await modal_interaction.followup.send(f"❌ 创建交易失败: {str(db_error)}", ephemeral=True)
                        return
                    
                    # 更新用户发帖计数
                    await database.update_post_count(modal_interaction.user.id, max_limit, 'currency')
                    
                    # 更新货币交易汇总
                    await self.market_cog.update_currency_summary()
                    
                    # 发送成功信息
                    await modal_interaction.followup.send("✅ 交易信息已添加到市场！", ephemeral=True)
                except Exception as member_error:
                    logger.error(f"处理用户和交易数据时出错: {member_error}")
                    await modal_interaction.followup.send(f"❌ 处理请求时出错: {str(member_error)}", ephemeral=True)
            except Exception as e:
                logger.error(f"处理货币交易按钮回调时出错: {e}")
                # 尝试使用followup发送错误消息
                try:
                    await modal_interaction.followup.send(f"❌ 处理交易请求时出错: {str(e)}", ephemeral=True)
                except Exception as follow_error:
                    logger.error(f"无法发送交易错误消息: {follow_error}")
        
        # 设置回调函数
        modal.callback = modal_callback
        
        # 显示模态框
        await interaction.response.send_modal(modal)

class Market(commands.Cog):
    """市场模块 - 从main1.py迁移而来的功能集合"""
    
    def __init__(self, bot):
        self.bot = bot
        # 启动货币交易市场汇总更新任务
        self.update_currency_summary.start()
        
    def cog_unload(self):
        # 停止任务
        self.update_currency_summary.cancel()
    
    # -------------------------
    # 链接安全模块
    # -------------------------
    @commands.Cog.listener()
    async def on_message(self, message):
        """监听消息以实现链接安全功能"""
        # 忽略机器人消息
        if message.author.bot:
            return
        
        # 如果在允许链接的频道中，直接返回
        if message.channel.id in config.ALLOWED_CHANNELS:
            return
            
        # 链接检测正则表达式
        url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        )
        
        # 如果消息中包含白名单域名，允许通过
        if any(domain in message.content for domain in config.WHITELISTED_DOMAINS):
            return
            
        # 确保消息内容是字符串
        if not message.content or not isinstance(message.content, str):
            return
            
        # 如果配置允许图片链接且消息以图片扩展名结尾，允许通过
        if config.ALLOW_IMAGE_LINKS and isinstance(message.content, str) and message.content.endswith(('.png', '.jpg', '.jpeg')):
            return
            
        # 检查消息中是否包含链接
        if url_pattern.search(message.content):
            # 检查是否拥有LV3角色
            if isinstance(config.LV3_ROLE_IDS, list):
                has_lv3_role = any(role.id in config.LV3_ROLE_IDS for role in message.author.roles)
            else:
                has_lv3_role = any(role.id == config.LV3_ROLE_IDS for role in message.author.roles)
                
            # 如果没有LV3角色，删除消息并发送警告
            if not has_lv3_role:
                try:
                    # 删除消息
                    await message.delete()
                    
                    # 获取用户的locale属性，默认使用"en"
                    user_locale = getattr(message.author, "locale", "en")
                    
                    # 定义本地化提示信息
                    localized_messages = {
                        "zh-TW": f":yeti_basic:{message.author.mention} 您需要擁有LV3角色才能傳送連結！",
                        "zh-CN": f":yeti_basic:{message.author.mention} 您需要拥有LV3角色才能发送链接！",
                        "en": f":yeti_basic:{message.author.mention} You need LV3 role to post links!",
                        "ja": f":yeti_basic:{message.author.mention} リンクを投稿するにはLV3ロールが必要です！",
                        "fr": f":yeti_basic:{message.author.mention} 링크를 게시하려면 LV3 역할이 필요합니다!"
                    }
                    warning_message = localized_messages.get(user_locale, localized_messages["zh-TW"])
                    
                    # 发送警告消息，10秒后自动删除
                    warning = await message.channel.send(warning_message, delete_after=10)
                except Exception as e:
                    logger.error(f"删除消息失败: {str(e)}")
    
    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        """监听帖子创建事件，根据帖子标签添加相应按钮"""
        try:
            # 检查是否是指定的论坛频道
            if thread.parent_id != config.FORUM_CHANNEL_ID:
                return
                
            # 检查是否有标签
            if not thread.applied_tags:
                return
                
            # 获取标签名称（转为小写）
            tag_names = []
            for tag in thread.applied_tags:
                if hasattr(tag, 'name') and isinstance(tag.name, str):
                    tag_names.append(tag.name.lower())
                    
            if not tag_names:
                logger.warning(f"帖子 {thread.id} 没有有效的标签名称")
                return
                
            # 根据标签创建不同的按钮
            buttons = []
            view = discord.ui.View(timeout=None)
            
            # 获取第一条消息（通常包含帖子内容）
            async for message in thread.history(limit=1):
                first_message = message
                break
            else:
                return  # 没有找到消息
                
            # 检查发布者信息
            author_id = None
            if first_message.embeds:
                embed = first_message.embeds[0]
                for field in embed.fields:
                    if field.name == "\u200b":  # 匹配发布者字段
                        # 提取用户ID
                        user_mention_match = re.search(r'<@(\d+)>', field.value)
                        if user_mention_match:
                            author_id = int(user_mention_match.group(1))
                            break
            
            if author_id is None:
                logger.warning(f"无法确定帖子 {thread.id} 的作者")
                return
                
            # 根据标签添加按钮
            if "sell" in tag_names:
                # 添加购买按钮
                button = discord.ui.Button(
                    style=discord.ButtonStyle.success,
                    label="购买",
                    emoji="🛒",
                    custom_id=f"post_buy:{thread.id}:{author_id}"
                )
                view.add_item(button)
                
            elif "buy" in tag_names:
                # 添加出售按钮
                button = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="出售",
                    emoji="💰",
                    custom_id=f"post_sell:{thread.id}:{author_id}"
                )
                view.add_item(button)
                
            elif "rent_out" in tag_names:
                # 添加租赁按钮
                button = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="租赁",
                    emoji="🔑",
                    custom_id=f"post_rent:{thread.id}:{author_id}"
                )
                view.add_item(button)
                
            # 如果有按钮，则发送
            if len(view.children) > 0:
                await first_message.edit(view=view)
                
        except Exception as e:
            logger.error(f"处理帖子按钮时出错: {e}", exc_info=True)
    
    # 添加按钮交互的处理
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """处理按钮交互"""
        if not interaction.data or not interaction.data.get("custom_id"):
            return
            
        custom_id = interaction.data["custom_id"]
        
        # 处理帖子交易按钮
        if custom_id.startswith("post_"):
            parts = custom_id.split(":")
            if len(parts) < 3:
                return
                
            action_type = parts[0]  # post_buy, post_sell, post_rent
            thread_id = int(parts[1])
            author_id = int(parts[2])
            
            # 避免与自己交易
            if interaction.user.id == author_id:
                await interaction.response.send_message("您不能与自己进行交易。", ephemeral=True)
                return
                
            # 根据按钮类型执行不同操作
            if action_type == "post_buy":
                # 使用trading.py的开始交易(buy)功能
                author = await interaction.guild.fetch_member(author_id)
                if not author:
                    await interaction.response.send_message("无法找到帖子作者。", ephemeral=True)
                    return
                    
                # 获取trading.py的Cog实例
                trading_cog = self.bot.get_cog("Trading")
                if not trading_cog:
                    await interaction.response.send_message("交易系统暂时不可用。", ephemeral=True)
                    return
                    
                # 调用开始交易方法
                ctx = await self.create_context_from_interaction(interaction)
                await trading_cog.trade_callback(ctx, author)
                
            elif action_type == "post_sell":
                # 使用trading.py的开始交易(sell)功能
                author = await interaction.guild.fetch_member(author_id)
                if not author:
                    await interaction.response.send_message("无法找到帖子作者。", ephemeral=True)
                    return
                    
                # 获取trading.py的Cog实例
                trading_cog = self.bot.get_cog("Trading")
                if not trading_cog:
                    await interaction.response.send_message("交易系统暂时不可用。", ephemeral=True)
                    return
                    
                # 调用开始交易方法
                ctx = await self.create_context_from_interaction(interaction)
                await trading_cog.trade_sell_callback(ctx, author)
                
            elif action_type == "post_rent":
                # 使用rental.py的租赁功能
                author = await interaction.guild.fetch_member(author_id)
                if not author:
                    await interaction.response.send_message("无法找到帖子作者。", ephemeral=True)
                    return
                    
                # 获取rental.py的Cog实例
                rental_cog = self.bot.get_cog("Rental")
                if not rental_cog:
                    await interaction.response.send_message("租赁系统暂时不可用。", ephemeral=True)
                    return
                    
                # 调用租赁方法
                ctx = await self.create_context_from_interaction(interaction)
                await rental_cog.rental_callback(ctx, author)
                
        # 处理其他原有的自定义ID...
            
    # 辅助方法：从Interaction创建ApplicationContext
    async def create_context_from_interaction(self, interaction: discord.Interaction) -> ApplicationContext:
        """
        从交互创建应用程序上下文
        
        Args:
            interaction: Discord交互对象
            
        Returns:
            ApplicationContext: 创建的应用程序上下文对象
        """
        ctx = ApplicationContext(self.bot, interaction)
        return ctx
    
    # -------------------------
    # 市场管理命令
    # -------------------------
    # (已移至admin.py)
    
    # -------------------------
    # 功能命令
    # -------------------------
    def allowed_channel():
        """检查命令是否在允许的频道中使用"""
        async def predicate(ctx: ApplicationContext):
            if ctx.channel.id != config.ALLOWED_CHANNEL_ID:
                error_msgs = {
                    "en-US": f"⛔ Command only available in <#{config.ALLOWED_CHANNEL_ID}>",
                    "zh-CN": f"⛔ 此命令仅在 <#{config.ALLOWED_CHANNEL_ID}> 可用",
                    "zh-TW": f"⛔ 此指令僅在 <#{config.ALLOWED_CHANNEL_ID}> 可用",
                    "ja": f"⛔ このコマンドは <#{config.ALLOWED_CHANNEL_ID}> でのみ使用可能",
                    "ko": f"⛔ 이 명령어는 <#{config.ALLOWED_CHANNEL_ID}> 에서만 사용 가능"
                }
                msg = error_msgs.get(str(ctx.locale), error_msgs["en-US"])
                await ctx.respond(msg, ephemeral=True)
                return False
            return True
        return commands.check(predicate)
    
    @commands.slash_command(
        name="trade",
        description="Create trading post",
        name_localizations={
            "zh-CN": "交易",
            "zh-TW": "交易",
            "ja": "取引",
            "ko": "거래"
        },
        description_localizations={
            "zh-CN": "创建交易帖子",
            "zh-TW": "建立交易貼文",
            "ja": "トレード投稿作成",
            "ko": "거래 게시물 생성"
        }
    )
    @allowed_channel()
    async def trade(
        self,
        ctx: ApplicationContext,
        post_type: str = Option(
            name="post_type",
            description="Type: Sell/Buy/Rent out",
            name_localizations={
                "zh-CN": "交易类型",
                "zh-TW": "交易類型",
                "ja": "取引タイプ",
                "ko": "거래_유형"
            },
            description_localizations={
                "zh-CN": "类型：出售/求购/出租",
                "zh-TW": "類型：出售/徵求/出租",
                "ja": "タイプ：売却/購入/レンタル",
                "ko": "유형: 판매/구매/렌트"
            },
            choices=[
                discord.OptionChoice(
                    name="Sell",
                    value="sell",
                    name_localizations={
                        "zh-CN": "出售",
                        "zh-TW": "出售",
                        "ja": "売却",
                        "ko": "판매"
                    }
                ),
                discord.OptionChoice(
                    name="Buy",
                    value="buy",
                    name_localizations={
                        "zh-CN": "求购",
                        "zh-TW": "徵求",
                        "ja": "購入",
                        "ko": "구매"
                    }
                ),
                discord.OptionChoice(
                    name="Rent out",
                    value="rent_out",
                    name_localizations={
                        "zh-CN": "出租",
                        "zh-TW": "出租",
                        "ja": "レンタル",
                        "ko": "렌트"
                    }
                )
            ]
        ),
        item: str = Option(
            name="item",
            description="Item name (required)",
            name_localizations={
                "zh-CN": "物品列表",
                "zh-TW": "物品清單",
                "ja": "アイテム一覧",
                "ko": "아이템_목록"
            },
            description_localizations={
                "zh-CN": "物品名称（必填，多个物品用逗号分隔，例：物品1, 物品2）",
                "zh-TW": "物品名稱（必填，多個物品用逗號分隔，例：物品1, 物品2）",
                "ja": "アイテム名（必須、カンマ区切りのアイテム一覧，例：アイテム1, アイテム2）",
                "ko": "아이템 이름（필수, 쉼표로 구분된 아이템 목록，예: 아이템1, 아이템2）"
            },
            required=True
        ),
        price: str = Option(
            name="price",
            description="Price (required)",
            name_localizations={
                "zh-CN": "价格",
                "zh-TW": "價格",
                "ja": "価格",
                "ko": "가격"
            },
            description_localizations={
                "zh-CN": "价格（必填）",
                "zh-TW": "價格（必填）",
                "ja": "価格（必須）",
                "ko": "가격（필수）"
            },
            required=True
        ),
        quantity: str = Option(
            name="quantity",
            description="Item quantity (optional)",
            name_localizations={
                "zh-CN": "数量",
                "zh-TW": "數量",
                "ja": "数量",
                "ko": "수량"
            },
            description_localizations={
                "zh-CN": "物品数量（可选）",
                "zh-TW": "物品數量（選填）",
                "ja": "アイテムの数量（任意）",
                "ko": "아이템 수량 (선택)"
            },
            required=False,
            default=""
        ),
        image: Optional[discord.Attachment] = None
    ):
        """主交易命令处理"""
        # 链接检测函数
        def contains_url(text: str) -> bool:
            # 确保输入是字符串类型并且非空
            if text is None or not isinstance(text, str) or not text.strip():
                return False
            url_pattern = re.compile(r'https?://\S+')
            return bool(url_pattern.search(text))
            
        try:
            # 立即延迟响应，防止交互超时
            await ctx.defer(ephemeral=True)
                
            # 确保所有参数都是字符串
            item_str = str(item) if item is not None else ""
            price_str = str(price) if price is not None else ""
            
            # 特别处理quantity，添加更多检查以处理各种可能的空值情况
            if quantity is None or str(quantity).strip() == "" or str(quantity).lower() in ["none", "undefined", "null"]:
                quantity_str = ""
            else:
                quantity_str = str(quantity).strip()
                
            # 检查是否包含URL
            if any(contains_url(field) for field in [item_str, price_str, quantity_str] if field):
                l10n_msg = {
                    "en-US": "❌ URLs not allowed in posts",
                    "zh-CN": "❌ 帖子中禁止包含链接",
                    "zh-TW": "❌ 貼文中禁止包含連結",
                    "ja": "❌ 投稿にURLを含めることはできません",
                    "ko": "❌ 게시물에 URL을 포함할 수 없습니다"
                }
                await ctx.followup.send(l10n_msg.get(str(ctx.locale), l10n_msg["en-US"]), ephemeral=True)
                return
            
            # 验证必填字段不为空
            if not item_str or item_str.strip() == "":
                l10n_msg = {
                    "en-US": "❌ Item name cannot be empty",
                    "zh-CN": "❌ 物品名称不能为空",
                    "zh-TW": "❌ 物品名稱不能為空",
                    "ja": "❌ アイテム名を入力してください",
                    "ko": "❌ 아이템 이름을 입력해야 합니다"
                }
                await ctx.followup.send(l10n_msg.get(str(ctx.locale), l10n_msg["en-US"]), ephemeral=True)
                return
            
            # 检查物品名称列表中是否有内容
            item_list = [i.strip() for i in re.split(r'[,，]', item_str)]
            if not any(i for i in item_list if i):
                l10n_msg = {
                    "en-US": "❌ Item name cannot be empty",
                    "zh-CN": "❌ 物品名称不能为空",
                    "zh-TW": "❌ 物品名稱不能為空",
                    "ja": "❌ アイテム名を入力してください",
                    "ko": "❌ 아이템 이름을 입력해야 합니다"
                }
                await ctx.followup.send(l10n_msg.get(str(ctx.locale), l10n_msg["en-US"]), ephemeral=True)
                return
            
            if not price_str or price_str.strip() == "":
                await ctx.followup.send({
                    "en-US": "❌ Price cannot be empty",
                    "zh-CN": "❌ 价格不能为空",
                    "zh-TW": "❌ 價格不能為空",
                    "ja": "❌ 価格を入力してください",
                    "ko": "❌ 가격을 입력해야 합니다"
                }.get(str(ctx.locale), "❌ Price cannot be empty"), ephemeral=True)
                return
            
            # 验证价格是有效数字
            try:
                # 检查是否包含SQL注入特征或其他非法字符
                valid_number_pattern = r'^[0-9]+(\.[0-9]+)?$'
                
                # 检测SQL注入攻击模式
                sql_injection_patterns = [
                    r"'\s*OR",
                    r"'\s*--",
                    r"'\s*;",
                    r"SELECT",
                    r"INSERT",
                    r"DELETE",
                    r"UPDATE",
                    r"DROP",
                    r"UNION",
                    r"WHERE",
                    r"1\s*=\s*1"
                ]
                
                # 检查价格输入
                for pattern in sql_injection_patterns:
                    if re.search(pattern, price_str, re.IGNORECASE):
                        logger.warning(f"检测到可能的SQL注入尝试: {price_str}")
                        await ctx.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                        return
                        
                # 检查数量输入 (如果提供)
                if quantity_str and quantity_str.strip():
                    for pattern in sql_injection_patterns:
                        if re.search(pattern, quantity_str, re.IGNORECASE):
                            logger.warning(f"检测到可能的SQL注入尝试: {quantity_str}")
                            await ctx.followup.send("❌ 检测到非法输入，请只输入有效数字", ephemeral=True)
                            return
                
                # 验证价格是否为有效数字格式
                if not re.match(valid_number_pattern, price_str.replace(',', '.')):
                    await ctx.followup.send({
                        "en-US": "❌ Price must be a positive number, only digits and decimal point allowed",
                        "zh-CN": "❌ 价格必须是正数，仅允许数字和小数点",
                        "zh-TW": "❌ 價格必須是正數，僅允許數字和小數點",
                        "ja": "❌ 価格は正の数でなければならず、数字と小数点のみ許可されます",
                        "ko": "❌ 가격은 양수여야 하며, 숫자와 소수점만 허용됩니다"
                    }.get(str(ctx.locale), "❌ Price must be a positive number"), ephemeral=True)
                    return
                
                # 检查其他格式或字符
                if 'e' in price_str.lower():
                    await ctx.followup.send({
                        "en-US": "❌ Scientific notation is not allowed",
                        "zh-CN": "❌ 不允许使用科学计数法",
                        "zh-TW": "❌ 不允許使用科學計數法",
                        "ja": "❌ 科学的表記法は許可されていません",
                        "ko": "❌ 과학적 표기법은, 허용되지 않습니다"
                    }.get(str(ctx.locale), "❌ Scientific notation is not allowed"), ephemeral=True)
                    return
                
                # 检查数字长度以防止溢出
                if len(price_str.replace(',', '').replace('.', '')) > 15:
                    await ctx.followup.send({
                        "en-US": "❌ Price value is too large",
                        "zh-CN": "❌ 价格数值过大",
                        "zh-TW": "❌ 價格數值過大",
                        "ja": "❌ 価格が大きすぎます",
                        "ko": "❌ 가격이 너무 큽니다"
                    }.get(str(ctx.locale), "❌ Price value is too large"), ephemeral=True)
                    return
                
                price_value = float(price_str.replace(',', '.'))
                if price_value <= 0:
                    await ctx.followup.send({
                        "en-US": "❌ Price must be a positive number",
                        "zh-CN": "❌ 价格必须是正数",
                        "zh-TW": "❌ 價格必須是正數",
                        "ja": "❌ 価格は正の数でなければなりません",
                        "ko": "❌ 가격은 양수여야 합니다"
                    }.get(str(ctx.locale), "❌ Price must be a positive number"), ephemeral=True)
                    return
                
                # 检查价格是否合理 (防止极大值)
                if price_value > 1000000000:
                    await ctx.followup.send({
                        "en-US": "❌ Price is out of reasonable range",
                        "zh-CN": "❌ 价格超出合理范围",
                        "zh-TW": "❌ 價格超出合理範圍",
                        "ja": "❌ 価格が妥当な範囲を超えています",
                        "ko": "❌ 가격이 합리적인 범위를 벗어났습니다"
                    }.get(str(ctx.locale), "❌ Price is out of reasonable range"), ephemeral=True)
                    return
            except ValueError:
                await ctx.followup.send({
                    "en-US": "❌ Invalid price format",
                    "zh-CN": "❌ 价格格式无效",
                    "zh-TW": "❌ 價格格式無效",
                    "ja": "❌ 価格の形式が無効です",
                    "ko": "❌ 가격 형식이 잘못되었습니다"
                }.get(str(ctx.locale), "❌ Invalid price format"), ephemeral=True)
                return
            
            # 如果提供了数量，验证它是有效数字
            if quantity_str and quantity_str.strip():
                try:
                    # 检查其他格式或字符
                    if 'e' in quantity_str.lower():
                        await ctx.followup.send({
                            "en-US": "❌ Scientific notation is not allowed",
                            "zh-CN": "❌ 不允许使用其他格式或字符",
                            "zh-TW": "❌ 不允許使用科學計數法",
                            "ja": "❌ 科学的表記法は許可されていません",
                            "ko": "❌ 과학적 표기법은, 허용되지 않습니다"
                        }.get(str(ctx.locale), "❌ Scientific notation is not allowed"), ephemeral=True)
                        return
                    
                    # 检查数字长度以防止溢出
                    if len(quantity_str.replace(',', '').replace('.', '')) > 15:
                        await ctx.followup.send({
                            "en-US": "❌ Quantity value is too large",
                            "zh-CN": "❌ 数量数值过大",
                            "zh-TW": "❌ 數量數值過大",
                            "ja": "❌ 数量が大きすぎます",
                            "ko": "❌ 수량이 너무 큽니다"
                        }.get(str(ctx.locale), "❌ Quantity value is too large"), ephemeral=True)
                        return
                    
                    qty_value = float(quantity_str.replace(',', '.'))
                    if qty_value <= 0:
                        await ctx.followup.send({
                            "en-US": "❌ Quantity must be a positive number",
                            "zh-CN": "❌ 数量必须是正数",
                            "zh-TW": "❌ 數量必須是正數",
                            "ja": "❌ 数量は正の数でなければなりません",
                            "ko": "❌ 수량은 양수여야 합니다"
                        }.get(str(ctx.locale), "❌ Quantity must be a positive number"), ephemeral=True)
                        return
                    
                    # 检查数量是否合理 (防止极大值)
                    if qty_value > 1000000000:
                        await ctx.followup.send({
                            "en-US": "❌ Quantity is out of reasonable range",
                            "zh-CN": "❌ 数量超出合理范围",
                            "zh-TW": "❌ 數量超出合理範圍",
                            "ja": "❌ 数量が妥当な範囲を超えています",
                            "ko": "❌ 수량이 합리적인 범위를 벗어났습니다"
                        }.get(str(ctx.locale), "❌ Quantity is out of reasonable range"), ephemeral=True)
                        return
                except ValueError:
                    await ctx.followup.send({
                        "en-US": "❌ Invalid quantity format",
                        "zh-CN": "❌ 数量格式无效",
                        "zh-TW": "❌ 數量格式無效",
                        "ja": "❌ 数量の形式が無効です",
                        "ko": "❌ 수량 형식이 잘못되었습니다"
                    }.get(str(ctx.locale), "❌ Invalid quantity format"), ephemeral=True)
                    return
            
            # 获取用户角色ID列表
            member = await ctx.guild.fetch_member(ctx.user.id)
            if not member:
                await ctx.followup.send("❌ 无法获取用户信息", ephemeral=True)
                return
            
            member_roles = [role.id for role in member.roles if hasattr(role, 'id')]
            
            # 获取用户的最大发帖限制
            max_limit = await database.get_max_post_limit(member_roles)
            
            if max_limit == 0:
                await ctx.followup.send("❌ 你没有发帖权限", ephemeral=True)
                return
            
            # 检查是否超过发帖限制
            if not await database.check_post_limit(ctx.user.id, max_limit, 'normal'):
                l10n_msg = {
                    "en-US": f"❌ Daily limit reached ({max_limit}/day)",
                    "zh-CN": f"❌ 已达到每日限额（{max_limit}次/天）",
                    "zh-TW": f"❌ 已達每日限額（{max_limit}次/天）",
                    "ja": f"❌ 1日の制限に達しました（{max_limit}回/日）",
                    "ko": f"❌ 일일 한도 도달 ({max_limit}회/일)"
                }
                await ctx.followup.send(l10n_msg.get(str(ctx.locale), l10n_msg["en-US"]), ephemeral=True)
                return
            
            # 检查图片格式
            l10n = {
                "invalid_image": {
                    "en-US": "❌ Only PNG/JPG images are supported",
                    "zh-CN": "❌ 仅支持PNG/JPG格式图片",
                    "zh-TW": "❌ 僅支援PNG/JPG格式圖片",
                    "ja": "❌ PNG/JPG形式のみ対応",
                    "ko": "❌ PNG/JPG 형식만 지원"
                },
                "success": {
                    "en-US": "✅ Post created: {url}",
                    "zh-CN": "✅ 帖子创建成功: {url}",
                    "zh-TW": "✅ 貼文建立成功: {url}",
                    "ja": "✅ 投稿が作成されました: {url}",
                    "ko": "✅ 게시물 생성 완료: {url}"
                }
            }
            
            if image is not None:
                if image.content_type not in ["image/png", "image/jpeg"]:
                    await ctx.followup.send(
                        l10n["invalid_image"].get(str(ctx.locale), l10n["invalid_image"]["en-US"]),
                        ephemeral=True
                    )
                    return
            
            # 处理物品列表
            if not isinstance(item_str, str):
                item_str = str(item_str)
            item_list = [items.strip() for items in re.split(r'[,，]', item_str)]
            item_formatted = '\n'.join(f" {items}" for items in item_list)
            
            # 创建Embed
            embed = discord.Embed(color=0x00ff00, timestamp=datetime.now())
            if image:
                embed.set_image(url=image.url)
            
            locale = str(ctx.locale)
            
            embed.add_field(
                name="\u200b",  # 使用零宽度空格占位
                value=f" 👶 發佈者 ：{ctx.user.mention}\n\u200b",
                inline=False
            )
            
            embed.add_field(
                name="💍 物品",
                value=f"```\n{item_formatted}\n```",
                inline=False
            )
            
            embed.add_field(
                name="💰 價格",
                value=f"```\n {price}\n```",
                inline=False
            )
            
            if quantity:
                embed.add_field(
                    name="📦 數量",
                    value=f"```\n {quantity}\n```",
                    inline=False
                )
            
            embed.set_author(
                name=f"{ctx.user.display_name} 的交易信息",
                icon_url=ctx.user.display_avatar.url
            )
            
            # 无过期时间，不删除帖子
            embed.set_footer(text=f"📝 发布时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
            
            try:
                # 获取论坛频道
                forum = self.bot.get_channel(config.FORUM_CHANNEL_ID)
                if not isinstance(forum, discord.ForumChannel):
                    raise ValueError("论坛频道配置错误")
                
                # 获取标签
                try:
                    tag = next(t for t in forum.available_tags if t.name.lower() == post_type.lower())
                except StopIteration:
                    error_msg = {
                        "en-US": f"❌ Missing '{post_type}' tag",
                        "zh-CN": f"❌ 缺少'{post_type}'标签",
                        "ja": f"❌ '{post_type}'タグなし",
                        "ko": f"❌ '{post_type}' 태그 없음"
                    }.get(locale, f"❌ Missing tag: {post_type}")
                    await ctx.followup.send(error_msg, ephemeral=True)
                    return
                
                # 创建论坛帖子
                thread = await forum.create_thread(
                    name=item_str[:100],
                    content=" ",  # 占位起始消息
                    embed=embed,
                    applied_tags=[tag]
                )
                
                # 更新发帖计数
                await database.update_post_count(ctx.user.id, max_limit, 'normal')
                
                # 记录帖子信息到数据库
                await database.create_post(ctx.user.id, thread.id, thread.id)
                
                # 发送成功消息
                success_msg = l10n["success"].get(locale, l10n["success"]["en-US"])
                await ctx.followup.send(
                    success_msg.format(url=thread.jump_url),
                    ephemeral=True
                )
                
            except Exception as e:
                logger.error(f"创建交易帖子失败: {e}")
                await ctx.followup.send(f"🔥 Error: {str(e)}", ephemeral=True)
        except Exception as e:
            logger.error(f"处理交易命令时出错: {e}")
            await ctx.followup.send(f"🔥 Error: {str(e)}", ephemeral=True)
    
    # Remove the slash command but keep the functionality
    # as an internal method that can be called by the delete button
    async def _user_delete_currency_trade(self, ctx: ApplicationContext, already_deferred: bool = False):
        """删除用户自己的货币交易（内部方法）
        
        Args:
            ctx: 应用程序上下文
            already_deferred: 交互是否已被defer处理
        """
        try:
            # 如果交互尚未被处理，则进行延迟响应
            if not already_deferred:
                await ctx.defer(ephemeral=True)
            
            # 获取用户的交易记录
            trades = await database.get_currency_trades_by_user(ctx.user.id)
            
            if not trades:
                await ctx.followup.send({
                    "en-US": "❌ No active trades found",
                    "zh-CN": "❌ 没有找到有效交易",
                    "zh-TW": "❌ 沒有找到有效交易",
                    "ja": "❌ 有効な取引が見つかりません",
                    "ko": "❌ 유효한 거래가 없습니다"
                }.get(str(ctx.locale), "❌ No trades"), ephemeral=True)
                return
                
            # 创建选择菜单
            options = [
                discord.SelectOption(
                    label=f"{trade['currency_type']} {trade['trade_type']} ×{trade['quantity']} @{trade['price']}",
                    value=str(trade['id'])
                ) for trade in trades
            ]
            
            select = discord.ui.Select(placeholder="选择要删除的交易", options=options)
            
            async def delete_callback(interaction):
                try:
                    # 立即延迟响应，防止交互超时
                    await interaction.response.defer(ephemeral=True)
                    
                    if interaction.user.id != ctx.user.id:
                        await interaction.followup.send("⛔ 您不能使用此菜单", ephemeral=True)
                        return
                        
                    trade_id = int(select.values[0])
                    success = await database.delete_currency_trade(trade_id, interaction.user.id)
                    
                    if success:
                        await interaction.followup.send({
                            "en-US": "✅ Trade deleted",
                            "zh-CN": "✅ 交易已删除",
                            "zh-TW": "✅ 交易已刪除",
                            "ja": "✅ 取引を削除しました",
                            "ko": "✅ 거래 삭제 완료"
                        }.get(str(interaction.locale), "✅ Deleted"), ephemeral=True)
                        
                        # 更新货币交易汇总
                        await self.update_currency_summary()
                    else:
                        await interaction.followup.send("❌ 删除交易失败", ephemeral=True)
                except Exception as e:
                    logger.error(f"删除交易回调处理出错: {e}")
                    await interaction.followup.send(f"❌ 处理删除请求时出错: {str(e)}", ephemeral=True)
                    
            select.callback = delete_callback
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            
            await ctx.followup.send({
                "en-US": "Select a trade to delete:",
                "zh-CN": "请选择要删除的交易：",
                "zh-TW": "請選擇要刪除的交易：",
                "ja": "削除する取引を選択：",
                "ko": "삭제할 거래 선택:"
            }.get(str(ctx.locale), "Select:"), view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"处理删除交易请求时出错: {e}")
            await ctx.followup.send(f"❌ 处理请求时出错: {str(e)}", ephemeral=True)
    
    # -------------------------
    # 后台任务
    # -------------------------
    @tasks.loop(minutes=30)
    async def update_currency_summary(self):
        """更新货币交易汇总"""
        try:
            # 保存无效用户ID列表，用于稍后清理
            invalid_user_ids = set()
            
            # 获取NXPC交易数据
            nxpc_sell = await database.get_currency_trades_by_type("NXPC", "sell")
            nxpc_buy = await database.get_currency_trades_by_type("NXPC", "buy")
            
            # 获取NESO交易数据
            neso_sell = await database.get_currency_trades_by_type("NESO", "sell")
            neso_buy = await database.get_currency_trades_by_type("NESO", "buy")
            
            # 排序交易数据
            nxpc_sell = sorted(nxpc_sell, key=lambda x: x['price'])
            nxpc_buy = sorted(nxpc_buy, key=lambda x: -x['price'])
            neso_sell = sorted(neso_sell, key=lambda x: x['price'])
            neso_buy = sorted(neso_buy, key=lambda x: -x['price'])
            
            # 构建Embed
            embed = discord.Embed(title="💰 實時貨幣交易市場", color=0x00ff00)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1372068180582993940/1372068181161541673/zxc3.png?ex=68256d92&is=68241c12&hm=d74b665d4634a1f0028f6ea21b1b6e727030c4c0c5a726380bd466f5eae3c7fb&")
            
            # NXPC出售列表
            nxpc_sell_text = []
            for idx, trade in enumerate(nxpc_sell[:5], 1):  # 每个货币类型最多显示5条
                try:
                    user = await self.bot.fetch_user(trade['user_id'])
                    nxpc_sell_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 賣家：{user.mention}"
                    )
                except Exception as e:
                    logger.warning(f"无法获取用户 {trade['user_id']} 信息: {e}")
                    nxpc_sell_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 賣家：(未知用户)"
                    )
                    # 将无效用户ID添加到集合中
                    invalid_user_ids.add(trade['user_id'])
            embed.add_field(
                name="🔵 NXPC 出售（價格從低到高）",
                value="\n".join(nxpc_sell_text) or "暫無NXPC出售信息",
                inline=False
            )
            
            # NXPC求購列表
            nxpc_buy_text = []
            for idx, trade in enumerate(nxpc_buy[:5], 1):
                try:
                    user = await self.bot.fetch_user(trade['user_id'])
                    nxpc_buy_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 買家：{user.mention}"
                    )
                except Exception as e:
                    logger.warning(f"无法获取用户 {trade['user_id']} 信息: {e}")
                    nxpc_buy_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 買家：(未知用户)"
                    )
                    # 将无效用户ID添加到集合中
                    invalid_user_ids.add(trade['user_id'])
            embed.add_field(
                name="🔵 NXPC 求購（價格從高到低）",
                value="\n".join(nxpc_buy_text) or "暫無NXPC求購信息",
                inline=False
            )
            
            # NESO出售列表
            neso_sell_text = []
            for idx, trade in enumerate(neso_sell[:5], 1):
                try:
                    user = await self.bot.fetch_user(trade['user_id'])
                    neso_sell_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 賣家：{user.mention}"
                    )
                except Exception as e:
                    logger.warning(f"无法获取用户 {trade['user_id']} 信息: {e}")
                    neso_sell_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 賣家：(未知用户)"
                    )
                    # 将无效用户ID添加到集合中
                    invalid_user_ids.add(trade['user_id'])
            embed.add_field(
                name="🟠 NESO 出售（價格從低到高）",
                value="\n".join(neso_sell_text) or "暫無NESO出售信息",
                inline=False
            )
            
            # NESO求購列表
            neso_buy_text = []
            for idx, trade in enumerate(neso_buy[:5], 1):
                try:
                    user = await self.bot.fetch_user(trade['user_id'])
                    neso_buy_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 買家：{user.mention}"
                    )
                except Exception as e:
                    logger.warning(f"无法获取用户 {trade['user_id']} 信息: {e}")
                    neso_buy_text.append(
                        f"`{idx:02d}` 📦數量×{trade['quantity']} "
                        f"| 單價 ${trade['price']:.2f} | 買家：(未知用户)"
                    )
                    # 将无效用户ID添加到集合中
                    invalid_user_ids.add(trade['user_id'])
            embed.add_field(
                name="🟠 NESO 求購（價格從高到低）",
                value="\n".join(neso_buy_text) or "暫無NESO求購信息",
                inline=False
            )
            
            embed.set_footer(text="🕒 每30分鐘自動更新 | 點擊下方按鈕創建交易")
            
            # 创建交易按钮
            view = discord.ui.View(timeout=None)
            # NXPC 出售按钮
            view.add_item(CurrencyTradeButton("NXPC", "sell", self.bot, self))
            # NXPC 求购按钮
            view.add_item(CurrencyTradeButton("NXPC", "buy", self.bot, self))
            # NESO 出售按钮
            view.add_item(CurrencyTradeButton("NESO", "sell", self.bot, self))
            # NESO 求购按钮
            view.add_item(CurrencyTradeButton("NESO", "buy", self.bot, self))
            
            # 添加删除交易按钮
            delete_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary, 
                label="删除我的交易", 
                emoji="🗑️", 
                custom_id="delete_currency_trade"
            )
            
            async def delete_callback(interaction: discord.Interaction):
                try:
                    # 立即延迟响应，防止交互超时
                    await interaction.response.defer(ephemeral=True)
                    
                    # 创建应用程序上下文对象
                    ctx = await self.create_context_from_interaction(interaction)
                    
                    # 调用内部删除方法，标记交互已被响应
                    await self._user_delete_currency_trade(ctx, already_deferred=True)
                except Exception as e:
                    logger.error(f"处理删除按钮回调时出错: {e}")
                    try:
                        await interaction.followup.send(f"❌ 处理删除请求时出错: {str(e)}", ephemeral=True)
                    except Exception as follow_error:
                        logger.error(f"无法发送删除错误消息: {follow_error}")
            
            delete_button.callback = delete_callback
            view.add_item(delete_button)
            
            # 更新频道消息
            channel = self.bot.get_channel(config.CURRENCY_CHANNEL_ID)
            if channel:
                # 获取现有汇总消息ID
                message_id = await database.get_summary_message(config.CURRENCY_CHANNEL_ID)
                try:
                    if message_id:
                        # 尝试编辑现有消息
                        try:
                            msg = await channel.fetch_message(message_id)
                            await msg.edit(embed=embed, view=view)
                        except:
                            # 如果无法编辑，创建新消息
                            msg = await channel.send(embed=embed, view=view)
                            await database.save_summary_message(config.CURRENCY_CHANNEL_ID, msg.id)
                    else:
                        # 创建新的汇总消息
                        msg = await channel.send(embed=embed, view=view)
                        await database.save_summary_message(config.CURRENCY_CHANNEL_ID, msg.id)
                except Exception as e:
                    logger.error(f"更新汇总消息失败: {e}")
            else:
                logger.error(f"找不到汇总频道ID: {config.CURRENCY_CHANNEL_ID}")
                
            # 清理无效用户的交易
            if invalid_user_ids:
                logger.warning(f"发现 {len(invalid_user_ids)} 个无效用户ID，准备清理其交易记录")
                for user_id in invalid_user_ids:
                    try:
                        trades = await database.get_currency_trades_by_user(user_id)
                        for trade in trades:
                            await database.admin_delete_currency_trade(trade['id'])
                            logger.info(f"已删除无效用户 {user_id} 的交易 {trade['id']}")
                    except Exception as e:
                        logger.error(f"清理无效用户 {user_id} 的交易时出错: {e}")
                        
        except Exception as e:
            logger.error(f"更新货币交易汇总失败: {e}")
            
    @update_currency_summary.before_loop
    async def before_update_currency_summary(self):
        """等待机器人准备好后再开始任务"""
        await self.bot.wait_until_ready()

# 设置Cog
def setup(bot):
    bot.add_cog(Market(bot)) 