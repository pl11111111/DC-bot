import discord
from discord.ext import commands
from discord import SlashCommandGroup, ApplicationContext
from discord.commands import user_command
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import time

import config
from utils import database, redis_client, helpers, binance_api, payment_utils
from utils.legacy_safety import serialized

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Trading(commands.Cog):
    """处理交易托管功能。"""
    
    def __init__(self, bot):
        self.bot = bot
        self.payment_check_tasks = {}  # 存储支付检查任务
    
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        """监听消息删除事件，记录交易分类中被删除的消息"""
        try:
            # 确保消息有频道信息
            if not message.channel or not message.guild:
                return
                
            # 获取消息所在频道的分类ID
            category_id = message.channel.category_id if hasattr(message.channel, 'category_id') else None
            
            # 检查是否在交易分类中
            if category_id != config.TRADE_CATEGORY_ID:
                # 检查频道名称是否为交易频道格式
                if not message.channel.name.startswith('trade-'):
                    return
            
            # 忽略机器人自己的消息
            if message.author.id == self.bot.user.id:
                return
                
            # 获取记录频道
            log_channel_id = config.DELETED_MESSAGES_LOG_CHANNEL_ID
            if not log_channel_id:
                return
                
            log_channel = self.bot.get_channel(log_channel_id)
            if not log_channel:
                logger.warning(f"找不到删除消息日志频道 ID: {log_channel_id}")
                return
                
            # 创建嵌入消息
            embed = discord.Embed(
                title="❌ 检测到交易频道中的消息被删除",
                description=f"频道: {message.channel.mention} (`{message.channel.name}`)",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            
            # 添加消息内容
            if message.content:
                # 截断过长的消息内容
                content = message.content
                if len(content) > 1024:
                    content = content[:1021] + "..."
                embed.add_field(name="消息内容", value=content, inline=False)
            
            # 添加附件信息
            if message.attachments:
                attachments_info = "\n".join([f"[{a.filename}]({a.url})" for a in message.attachments])
                if attachments_info:
                    embed.add_field(name="附件", value=attachments_info, inline=False)
            
            # 添加用户和时间信息
            embed.add_field(
                name="发送者", 
                value=f"{message.author.mention} (`{message.author.name}` - ID: `{message.author.id}`)", 
                inline=True
            )
            
            # 添加消息创建时间
            if message.created_at:
                embed.add_field(
                    name="发送时间",
                    value=f"<t:{int(message.created_at.timestamp())}:F>",
                    inline=True
                )
            
            # 添加消息ID
            embed.add_field(name="消息ID", value=f"`{message.id}`", inline=True)
            
            # 设置消息作者头像
            if message.author.avatar:
                embed.set_author(name=message.author.name, icon_url=message.author.avatar.url)
            else:
                embed.set_author(name=message.author.name)
                
            # 发送日志消息
            await log_channel.send(embed=embed)
            logger.info(f"记录了被删除的消息: 频道={message.channel.name}, 用户={message.author.name}, 内容长度={len(message.content) if message.content else 0}")
        
        except Exception as e:
            logger.error(f"监控删除消息时出错: {str(e)}", exc_info=True)
    
    # 管理员命令组
    trade_admin = SlashCommandGroup("trade_admin", "交易管理员命令", default_member_permissions=discord.Permissions(administrator=True))

    @trade_admin.command(name="set_delete_log_channel", description="设置删除消息监控日志频道")
    async def set_delete_log_channel(self, ctx: ApplicationContext, channel: discord.TextChannel = discord.Option(discord.TextChannel, "选择要用作删除消息监控日志的频道", required=True)):
        """设置用于记录交易频道中被删除消息的日志频道"""
        # 验证用户是否有管理员权限
        if not ctx.author.guild_permissions.administrator:
            await ctx.respond("您没有执行此命令的权限。", ephemeral=True)
            return
            
        try:
            # 更新环境变量
            import os
            os.environ['DELETED_MESSAGES_LOG_CHANNEL_ID'] = str(channel.id)
            
            # 更新配置
            config.DELETED_MESSAGES_LOG_CHANNEL_ID = channel.id
            
            # 响应
            await ctx.respond(f"已成功设置 {channel.mention} 为删除消息监控日志频道。", ephemeral=True)
            logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 设置了删除消息监控日志频道: {channel.id}")
            
        except Exception as e:
            logger.error(f"设置删除消息监控日志频道时出错: {str(e)}", exc_info=True)
            await ctx.respond("设置删除消息监控日志频道时出错，请稍后再试。", ephemeral=True)

    @trade_admin.command(name="close_channel", description="移除当前频道的活跃交易并关闭")
    async def close_channel(self, ctx: ApplicationContext):
        """移除当前频道的活跃交易或租赁ID，并将交易状态标记为已关闭。"""
        # 验证用户是否有管理员权限
        if not ctx.author.guild_permissions.administrator:
            await ctx.respond("您没有执行此命令的权限。", ephemeral=True)
            return
            
        # 获取当前频道的交易ID
        channel_id = ctx.channel.id
        transaction_id = await redis_client.get_channel_transaction(channel_id)
        
        if not transaction_id:
            # 尝试从数据库直接获取
            transaction = await database.get_transaction_by_channel(channel_id)
            if transaction:
                transaction_id = transaction["id"]
            else:
                await ctx.respond("此频道没有关联的活跃交易。", ephemeral=True)
                return
                
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await ctx.respond("找不到关联的交易信息。", ephemeral=True)
            return
            
        # 检查是否是交易类型（而非租赁）
        if transaction['status'] not in ('pending', 'confirmed'):
            await database.log_transaction_action(transaction_id, 'admin_close_blocked', ctx.author.id, '订单可能涉及资金，禁止直接标记取消')
            await ctx.respond('此订单已进入资金流程，不能直接关闭。请先核对入款、退款或放款结果。', ephemeral=True)
            return
        if transaction["transaction_type"] != "trade":
            await ctx.respond("此频道是租赁频道，请使用 `/rental_admin close_channel` 命令关闭。", ephemeral=True)
            return
            
        # 更新交易状态为已取消
        await database.update_transaction_status(transaction_id, "cancelled")
        await database.set_user_active_transaction(transaction["buyer_id"], None, transaction_id)
        await database.set_user_active_transaction(transaction["seller_id"], None, transaction_id)
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction_id,
            action="admin_close",
            actor_id=ctx.author.id,
            details="管理员关闭了交易"
        )
        
        # 响应
        await ctx.respond(f"已成功关闭交易ID {transaction_id}，并清除用户的活跃交易状态。")
        
        # 发送交易关闭通知
        embed = discord.Embed(
            title="交易已被管理员关闭",
            description=f"交易ID：{transaction_id}\n物品：{transaction['item_name']}\n价格：{transaction['amount']} USDT",
            color=discord.Color.red()
        )
        
        embed.add_field(
            name="交易参与者",
            value=f"买家：<@{transaction['buyer_id']}>\n卖家：<@{transaction['seller_id']}>",
            inline=False
        )
        
        embed.add_field(
            name="操作人",
            value=f"{ctx.author.mention} ({ctx.author.name})",
            inline=False
        )
        
        embed.set_footer(text=f"关闭时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        await ctx.channel.send(embed=embed)
    
    # 用户命令（右键点击用户）
    @user_command(name="开始交易(buy)")
    async def trade_callback(self, ctx: ApplicationContext, user: discord.User):
        if ctx.guild and ctx.guild.id == config.NEW.GUILD_ID:
            cog = self.bot.get_cog('NewTrading')
            if not cog:
                return await ctx.respond('新社群交易模块未启用。', ephemeral=True)
            return await cog.start(ctx, user, buy=True)
        """发起与其他用户的交易（作为买家）。"""
        # 检查是否为同一用户
        if ctx.author.id == user.id:
            await ctx.respond("您不能与自己发起交易。", ephemeral=True)
            return
        
        # 检查任一用户是否已有活跃交易
        buyer = await database.get_user(ctx.author.id)
        seller = await database.get_user(user.id)
        
        # 如果用户不存在则创建
        if not buyer:
            await database.create_user(ctx.author.id, str(ctx.author))
            buyer = {"discord_id": ctx.author.id, "active_transaction_id": None, "free_escrow_count": 0}
        
        if not seller:
            await database.create_user(user.id, str(user))
            seller = {"discord_id": user.id, "active_transaction_id": None, "free_escrow_count": 0}
        
        # 检查任一用户是否有活跃交易
        if buyer.get("active_transaction_id"):
            await ctx.respond("您已有一个活跃交易。请在开始新交易前完成或取消它。", ephemeral=True)
            return
        
        if seller.get("active_transaction_id"):
            await ctx.respond(f"{user.mention} 已有一个活跃交易。请稍后再试。", ephemeral=True)
            return
        
        # 创建模态框收集交易信息
        modal = discord.ui.Modal(title="交易信息")
        
        item_name_input = discord.ui.InputText(
            label="物品名称",
            placeholder="输入物品名称",
            custom_id="item_name",
            style=discord.InputTextStyle.short,
            min_length=1,
            max_length=100
        )
        
        price_input = discord.ui.InputText(
            label="价格 (USDT)",
            placeholder="输入USDT价格（小数点后最多一位）",
            custom_id="price",
            style=discord.InputTextStyle.short,
            min_length=1,
            max_length=10
        )
        
        details_input = discord.ui.InputText(
            label="附加详情（可选）",
            placeholder="输入任何附加详情",
            custom_id="details",
            style=discord.InputTextStyle.paragraph,
            required=False,
            max_length=500
        )
        
        modal.add_item(item_name_input)
        modal.add_item(price_input)
        modal.add_item(details_input)
        
        async def modal_callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            
            # 从模态框获取值
            item_name = helpers.sanitize_input(interaction.data["components"][0]["components"][0]["value"])
            price_str = interaction.data["components"][1]["components"][0]["value"]
            details = helpers.sanitize_input(interaction.data["components"][2]["components"][0]["value"])
            
            # 检查科学计数法和极大数值
            if 'e' in price_str.lower() or 'E' in price_str:
                await interaction.followup.send("价格不允许使用科学计数法，请输入普通数字格式。", ephemeral=True)
                return
                
            # 检查数字长度以防止溢出
            if len(price_str.replace(',', '').replace('.', '')) > 15:
                await interaction.followup.send("价格数值过大，请输入合理的金额。", ephemeral=True)
                return
            
            # 验证价格
            if not helpers.is_valid_amount(price_str):
                await interaction.followup.send("无效的价格。请输入有效的正数。", ephemeral=True)
                return
                
            # 验证价格小数位数不超过1位
            if '.' in price_str and len(price_str.split('.')[1]) > 1:
                await interaction.followup.send("价格小数点后最多只能有1位。", ephemeral=True)
                return
            
            price = float(price_str)
            
            # 为交易创建私人频道
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                self.bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            # 在适当的分类中创建频道
            category = self.bot.get_channel(config.TRADE_CATEGORY_ID)
            if not category:
                categories = [c for c in ctx.guild.categories if c.name.lower() == "trading"]
                category = categories[0] if categories else None
            
            channel_name = f"trade-{ctx.author.name}-{user.name}"
            channel = await ctx.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"{ctx.author.name} 和 {user.name} 关于 {item_name} 的交易"
            )
            
            # 计算担保费用
            escrow_fee = 0.0  # 默认为0
            if price >= config.MIN_AMOUNT_FOR_FEE:
                escrow_fee = config.ESCROW_FEE
            
            # 创建交易记录
            transaction_id = await database.create_transaction(
                "trade",
                ctx.author.id,
                user.id,
                item_name,
                price,
                escrow_fee,
                channel.id
            )
            
            # 为双方设置活跃交易
            await database.set_user_active_transaction(ctx.author.id, transaction_id)
            await database.set_user_active_transaction(user.id, transaction_id)
            
            # 缓存频道到交易的映射
            await redis_client.cache_channel_transaction(channel.id, transaction_id)
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction_id,
                action="create",
                actor_id=ctx.author.id,
                details=f"创建了 {item_name} 的交易，价格为 {price} USDT"
            )
            
            # 创建并发送交易信息嵌入消息
            embed = helpers.create_transaction_embed(
                transaction_type="trade",
                item_name=item_name,
                amount=price,
                escrow_fee=escrow_fee,
                buyer_name=ctx.author.name,
                seller_name=user.name,
                buyer_id=ctx.author.id,
                seller_id=user.id
            )
            
            # 如果提供了附加详情则添加
            if details:
                embed.add_field(name="附加详情", value=details, inline=False)
            
            
            # 创建确认/拒绝按钮
            confirm_button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="接受交易",
                custom_id="confirm"
            )
            
            reject_button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="拒绝交易",
                custom_id="reject"
            )
            
            view = discord.ui.View()
            view.add_item(confirm_button)
            view.add_item(reject_button)
            
            # 发送消息并提及卖家
            await channel.send(f"{user.mention}，请查看并确认此交易：", embed=embed, view=view)
            
            # 通知用户交易已发起
            await interaction.followup.send(f"交易已发起！请查看 {channel.mention}", ephemeral=True)
        
        # 设置模态框回调
        modal.callback = modal_callback
        
        # 发送模态框
        await ctx.response.send_modal(modal)
    
    @user_command(name="开始交易(sell)")
    async def trade_sell_callback(self, ctx: ApplicationContext, user: discord.User):
        if ctx.guild and ctx.guild.id == config.NEW.GUILD_ID:
            cog = self.bot.get_cog('NewTrading')
            if not cog:
                return await ctx.respond('新社群交易模块未启用。', ephemeral=True)
            return await cog.start(ctx, user, buy=False)
        """发起与其他用户的交易（作为卖家）。"""
        # 检查是否为同一用户
        if ctx.author.id == user.id:
            await ctx.respond("您不能与自己发起交易。", ephemeral=True)
            return
        
        # 检查任一用户是否已有活跃交易
        seller = await database.get_user(ctx.author.id)
        buyer = await database.get_user(user.id)
        
        # 如果用户不存在则创建
        if not seller:
            await database.create_user(ctx.author.id, str(ctx.author))
            seller = {"discord_id": ctx.author.id, "active_transaction_id": None, "free_escrow_count": 0}
        
        if not buyer:
            await database.create_user(user.id, str(user))
            buyer = {"discord_id": user.id, "active_transaction_id": None, "free_escrow_count": 0}
        
        # 检查任一用户是否有活跃交易
        if seller.get("active_transaction_id"):
            await ctx.respond("您已有一个活跃交易。请在开始新交易前完成或取消它。", ephemeral=True)
            return
        
        if buyer.get("active_transaction_id"):
            await ctx.respond(f"{user.mention} 已有一个活跃交易。请稍后再试。", ephemeral=True)
            return
        
        # 创建模态框收集交易信息
        modal = discord.ui.Modal(title="交易信息")
        
        item_name_input = discord.ui.InputText(
            label="物品名称",
            placeholder="输入物品名称",
            custom_id="item_name",
            style=discord.InputTextStyle.short,
            min_length=1,
            max_length=100
        )
        
        price_input = discord.ui.InputText(
            label="价格 (USDT)",
            placeholder="输入USDT价格（小数点后最多一位）",
            custom_id="price",
            style=discord.InputTextStyle.short,
            min_length=1,
            max_length=10
        )
        
        details_input = discord.ui.InputText(
            label="附加详情（可选）",
            placeholder="输入任何附加详情",
            custom_id="details",
            style=discord.InputTextStyle.paragraph,
            required=False,
            max_length=500
        )
        
        modal.add_item(item_name_input)
        modal.add_item(price_input)
        modal.add_item(details_input)
        
        async def modal_callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            
            # 从模态框获取值
            item_name = helpers.sanitize_input(interaction.data["components"][0]["components"][0]["value"])
            price_str = interaction.data["components"][1]["components"][0]["value"]
            details = helpers.sanitize_input(interaction.data["components"][2]["components"][0]["value"])
            
            # 检查科学计数法和极大数值
            if 'e' in price_str.lower() or 'E' in price_str:
                await interaction.followup.send("价格不允许使用科学计数法，请输入普通数字格式。", ephemeral=True)
                return
                
            # 检查数字长度以防止溢出
            if len(price_str.replace(',', '').replace('.', '')) > 15:
                await interaction.followup.send("价格数值过大，请输入合理的金额。", ephemeral=True)
                return
            
            # 验证价格
            if not helpers.is_valid_amount(price_str):
                await interaction.followup.send("无效的价格。请输入有效的正数。", ephemeral=True)
                return
                
            # 验证价格小数位数不超过1位
            if '.' in price_str and len(price_str.split('.')[1]) > 1:
                await interaction.followup.send("价格小数点后最多只能有1位。", ephemeral=True)
                return
            
            price = float(price_str)
            
            # 为交易创建私人频道
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                self.bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            # 在适当的分类中创建频道
            category = self.bot.get_channel(config.TRADE_CATEGORY_ID)
            if not category:
                categories = [c for c in ctx.guild.categories if c.name.lower() == "trading"]
                category = categories[0] if categories else None
            
            channel_name = f"trade-{ctx.author.name}-{user.name}"
            channel = await ctx.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"{ctx.author.name} 和 {user.name} 关于 {item_name} 的交易"
            )
            
            # 计算担保费用
            escrow_fee = 0.0  # 默认为0
            if price >= config.MIN_AMOUNT_FOR_FEE:
                escrow_fee = config.ESCROW_FEE
            
            # 创建交易记录 - 注意这里与买家发起不同，买卖双方角色互换
            transaction_id = await database.create_transaction(
                "trade",
                user.id,  # 买家是被邀请的用户
                ctx.author.id,  # 卖家是发起交易的用户
                item_name,
                price,
                escrow_fee,
                channel.id
            )
            
            # 为双方设置活跃交易
            await database.set_user_active_transaction(ctx.author.id, transaction_id)
            await database.set_user_active_transaction(user.id, transaction_id)
            
            # 缓存频道到交易的映射
            await redis_client.cache_channel_transaction(channel.id, transaction_id)
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction_id,
                action="create",
                actor_id=ctx.author.id,
                details=f"创建了 {item_name} 的交易，价格为 {price} USDT"
            )
            
            # 创建并发送交易信息嵌入消息
            embed = helpers.create_transaction_embed(
                transaction_type="trade",
                item_name=item_name,
                amount=price,
                escrow_fee=escrow_fee,
                buyer_name=user.name,  # 买家是被邀请的用户
                seller_name=ctx.author.name,  # 卖家是发起交易的用户
                buyer_id=user.id,
                seller_id=ctx.author.id
            )
            
            # 如果提供了附加详情则添加
            if details:
                embed.add_field(name="附加详情", value=details, inline=False)
            
            
            # 创建确认/拒绝按钮
            confirm_button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="接受交易",
                custom_id="confirm"
            )
            
            reject_button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="拒绝交易",
                custom_id="reject"
            )
            
            view = discord.ui.View()
            view.add_item(confirm_button)
            view.add_item(reject_button)
            
            # 发送消息并提及买家
            await channel.send(f"{user.mention}，请查看并确认此交易：", embed=embed, view=view)
            
            # 通知用户交易已发起
            await interaction.followup.send(f"交易已发起！请查看 {channel.mention}", ephemeral=True)
        
        # 设置模态框回调
        modal.callback = modal_callback
        
        # 发送模态框
        await ctx.response.send_modal(modal)
    
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """处理按钮交互。"""
        if not interaction.data or not interaction.data.get("custom_id"):
            return
        
        custom_id = interaction.data["custom_id"]
        # 分离自定义ID和任何附加参数
        parts = custom_id.split(":")
        base_custom_id = parts[0]
        
        # 处理地址确认相关的按钮 - 这些现在可能来自私信
        if base_custom_id == "confirm_address":
            if len(parts) >= 3:
                trans_id = int(parts[1])
                address = parts[2]
                for i in range(3, len(parts)):
                    address += ":" + parts[i]  # 重建可能包含冒号的地址
                await self.handle_address_confirmation(interaction, trans_id, address)
                return
        
        elif base_custom_id == "reenter_address":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                await self.handle_address_reenter(interaction, trans_id)
                return
        
        # 处理收货确认相关的按钮
        elif base_custom_id == "confirm_receipt_final":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                await self.handle_confirm_receipt_final(interaction, trans_id)
                return
        
        elif base_custom_id == "cancel_receipt":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                await self.handle_cancel_receipt(interaction, trans_id)
                return

        # 处理收款按钮 - 来自私信
        elif base_custom_id == "collect_payment":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                await self.handle_collect_payment(interaction, trans_id)
                return
                
        # 处理使用已保存地址的按钮
        elif base_custom_id == "use_saved_address":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                # 获取交易信息
                transaction = await database.get_transaction(trans_id)
                if not transaction:
                    await interaction.response.send_message("找不到交易信息。", ephemeral=True)
                    return
                
                # 确保用户是卖家
                if interaction.user.id != transaction["seller_id"]:
                    await interaction.response.send_message("只有卖家可以使用此功能。", ephemeral=True)
                    return
                
                # 获取用户保存的地址
                seller = await database.get_user(transaction["seller_id"])
                seller_address = seller.get("payment_address") if seller else None
                
                if not seller_address:
                    await interaction.response.send_message("您没有保存的地址，请使用输入地址功能。", ephemeral=True)
                    return
                
                # 使用保存的地址处理资金释放
                await self.handle_address_confirmation(interaction, trans_id, seller_address)
                return

        # 处理输入新地址的按钮
        elif base_custom_id == "input_address":
            if len(parts) >= 2:
                trans_id = int(parts[1])
                # 获取交易信息
                transaction = await database.get_transaction(trans_id)
                if not transaction:
                    await interaction.response.send_message("找不到交易信息。", ephemeral=True)
                    return
                
                # 确保用户是卖家
                if interaction.user.id != transaction["seller_id"]:
                    await interaction.response.send_message("只有卖家可以使用此功能。", ephemeral=True)
                    return
                
                # 创建地址输入模态框
                modal = discord.ui.Modal(title="输入收款地址")
                address_input = discord.ui.InputText(
                    label="USDT-BEP20 收款地址",
                    placeholder="请输入您的USDT-BEP20收款地址",
                    required=True,
                    min_length=42,
                    max_length=42
                )
                modal.add_item(address_input)
                
                # 捕获transaction_id作为闭包变量
                transaction_id_for_callback = trans_id
                
                async def modal_callback(modal_interaction):
                    try:
                        new_address = address_input.value.strip()
                        if len(new_address) != 42 or not new_address.startswith("0x"):
                            await modal_interaction.response.send_message("地址格式不正确，请确保是42位的BEP20地址，以0x开头。", ephemeral=True)
                            return
                        
                        # 更新用户的默认地址
                        update_success = await database.update_user_payment_address(
                            transaction["seller_id"], 
                            new_address
                        )
                        
                        if not update_success:
                            await modal_interaction.response.send_message("更新地址失败，请稍后重试。", ephemeral=True)
                            return
                        
                        # 处理地址确认
                        await self.handle_address_confirmation(modal_interaction, transaction_id_for_callback, new_address)
                    except Exception as e:
                        logger.error(f"处理地址输入时出错: {e}")
                        await modal_interaction.response.send_message(f"处理地址时出错: {e}", ephemeral=True)
                
                modal.callback = modal_callback
                await interaction.response.send_modal(modal)
                return

        # 从频道获取交易ID
        channel_id = interaction.channel_id
        transaction_id = await redis_client.get_channel_transaction(channel_id)
        
        if not transaction_id:
            # 尝试从数据库获取
            transaction = await database.get_transaction_by_channel(channel_id)
            if transaction:
                transaction_id = transaction["id"]
                # 缓存以供将来使用
                await redis_client.cache_channel_transaction(channel_id, transaction_id)
            else:
                return  # 不是交易频道
        
        # 获取交易详情
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            return
        
        # 获取交易记录，判断交易发起者
        transaction_logs = await database.get_transaction_logs(transaction_id)
        initiator_id = None
        if transaction_logs:
            # 找到创建交易的记录
            for log in transaction_logs:
                if log["action"] == "create":
                    initiator_id = log["actor_id"]
                    break
        
        if base_custom_id == "confirm":
            # 确定当前用户是否为交易发起者
            is_initiator = initiator_id and interaction.user.id == initiator_id
            
            if is_initiator:
                # 交易发起者不能确认自己的交易
                await interaction.response.send_message("您不能确认自己发起的交易，必须等待对方确认。", ephemeral=True)
            elif interaction.user.id == transaction["seller_id"]:
                # 被邀请的卖家确认交易
                await self.confirm_trade(interaction, transaction)
            elif interaction.user.id == transaction["buyer_id"]:
                # 被邀请的买家确认交易
                await self.confirm_trade(interaction, transaction)
            else:
                # 其他用户试图确认交易
                await interaction.response.send_message("只有交易参与者才能确认交易。", ephemeral=True)
        
        elif base_custom_id == "reject" and interaction.user.id == transaction["seller_id"]:
            # 卖家拒绝交易
            await self.reject_trade(interaction, transaction)
        
        elif base_custom_id == "reject" and interaction.user.id == transaction["buyer_id"]:
            # 买家取消自己的交易请求
            await self.buyer_cancel_trade(interaction, transaction)
        
        elif base_custom_id == "pay":
            if interaction.user.id == transaction["buyer_id"]:
                # 买家付款
                await self.process_trade_payment(interaction, transaction)
            elif interaction.user.id == transaction["seller_id"]:
                # 卖家试图付款（不允许）
                await interaction.response.send_message("只有买家可以进行支付。", ephemeral=True)
        
        elif base_custom_id == "shipped" and interaction.user.id == transaction["seller_id"]:
            # 卖家标记为已发货
            await self.mark_shipped(interaction, transaction)
        
        elif base_custom_id == "received" and interaction.user.id == transaction["buyer_id"]:
            # 买家确认收货
            await self.confirm_receipt(interaction, transaction)
        
        elif base_custom_id == "dispute" and interaction.user.id in [transaction["buyer_id"], transaction["seller_id"]]:
            # 不是直接开启争议，而是显示二次确认对话框
            yes_button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="确认发起争议",
                custom_id=f"dispute_confirm_yes:{transaction_id}"
            )
            
            no_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="取消",
                custom_id=f"dispute_confirm_no:{transaction_id}"
            )
            
            view = discord.ui.View()
            view.add_item(yes_button)
            view.add_item(no_button)
            
            # 创建二次确认嵌入消息
            embed = discord.Embed(
                title="⚠️ 确认发起争议",
                description=f"您确定要为交易 '{transaction['item_name']}' 发起争议吗？",
                color=discord.Color.gold()
            )
            
            embed.add_field(
                name="重要提示",
                value="发起争议后，正常的交易流程将会终止，管理员将介入处理。请仅在确实存在问题并且双方无法自行解决时使用此功能。",
                inline=False
            )
            
            await interaction.response.send_message(
                "请确认是否要发起争议：",
                embed=embed,
                view=view,
                ephemeral=True
            )
        
        # 处理争议确认
        elif base_custom_id == "dispute_confirm_yes" and interaction.user.id in [transaction["buyer_id"], transaction["seller_id"]]:
            await self.open_dispute(interaction, transaction)
        
        # 处理争议取消
        elif base_custom_id == "dispute_confirm_no" and interaction.user.id in [transaction["buyer_id"], transaction["seller_id"]]:
            # 编辑原始的二次确认消息，告知用户操作已取消，并移除按钮和嵌入
            try:
                await interaction.response.edit_message(
                    content="您已取消发起争议。如果您仍希望发起争议，请再次点击交易频道中的【发起争议】按钮。",
                    embed=None,  # 清除嵌入内容
                    view=None    # 移除所有组件（按钮）
                )
                logger.info(f"用户 {interaction.user.id} 取消了交易 {transaction_id} 的争议发起，并编辑了提示消息。")
            except Exception as e:
                logger.error(f"编辑争议取消消息时出错: {e}")
                # 如果编辑消息失败（可能因为交互已响应），尝试发送追踪消息
                try:
                    # 检查交互是否已响应，避免重复响应错误
                    if not interaction.response.is_done():
                        await interaction.response.send_message("处理取消发起争议时出错，请稍后重试或联系管理员。", ephemeral=True)
                    else:
                        await interaction.followup.send("处理取消发起争议时出错，请稍后重试或联系管理员。", ephemeral=True)
                except discord.errors.HTTPException as http_err:
                    logger.error(f"发送争议取消追踪消息时HTTP错误: {http_err}")
                except Exception as e2:
                    logger.error(f"发送争议取消追踪消息时进一步出错: {e2}")
        
        elif base_custom_id == "cancel" and interaction.user.id in [transaction["buyer_id"], transaction["seller_id"]]:
            # 任一方取消交易
            await self.cancel_trade(interaction, transaction)
        
        elif base_custom_id == "copy_address":
            # 处理复制地址按钮
            transaction = await database.get_transaction(transaction_id)
            if transaction and transaction.get("payment_address"):
                # 创建嵌入消息，方便复制
                embed = discord.Embed(
                    title="支付信息",
                    description="请复制以下信息完成支付",
                    color=discord.Color.blue()
                )
                
                embed.add_field(
                    name="支付地址",
                    value=f"```{transaction['payment_address']}```",
                    inline=False
                )
                
                embed.add_field(
                    name="支付金额",
                    value=f"```{transaction.get('unique_amount', transaction['amount'])}```",
                    inline=False
                )
                
                embed.add_field(
                    name="注意事项",
                    value="• 请确保使用USDT-BEP20网络\n"
                          "• 支付完成后，系统将自动检测并更新交易状态\n"
                          "• 必须支付确切金额，不要修改小数位",
                    inline=False
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @serialized
    async def confirm_trade(self, interaction: discord.Interaction, transaction: Dict):
        """卖家确认交易。"""
        if transaction["status"] != "pending":
            await interaction.response.send_message("此交易已不能确认。", ephemeral=True)
            return
        
        # 首先延迟响应以防止交互超时
        await interaction.response.defer()
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "confirmed")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="confirm",
            actor_id=interaction.user.id,
            details="卖家确认了交易"
        )
        
        # 创建支付按钮
        pay_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="支付",
            custom_id="pay"
        )
        
        cancel_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="取消交易",
            custom_id="cancel"
        )
        
        view = discord.ui.View()
        view.add_item(pay_button)
        view.add_item(cancel_button)
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=interaction.user.name,
            status="confirmed",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        # 使用followup而不是response，因为我们已经调用了defer
        await interaction.followup.send(
            f"<@{transaction['buyer_id']}>，交易已被确认。请点击支付按钮继续。",
            embed=embed,
            view=view
        )
    
    @serialized
    async def reject_trade(self, interaction: discord.Interaction, transaction: Dict):
        """卖家拒绝交易。"""
        if transaction["status"] != "pending":
            await interaction.response.send_message("此交易已不能拒绝。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.set_user_active_transaction(transaction["buyer_id"], None, transaction['id'])
        await database.set_user_active_transaction(transaction["seller_id"], None, transaction['id'])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="reject",
            actor_id=interaction.user.id,
            details="卖家拒绝了交易"
        )
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=interaction.user.name,
            status="cancelled",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        await interaction.response.send_message(
            f"<@{transaction['buyer_id']}>，交易已被拒绝。",
            embed=embed
        )
        
        # 设置频道将在30秒后删除
        await interaction.channel.send("此频道将在30秒后删除。")
        await asyncio.sleep(30)
        await interaction.channel.delete()
    
    @serialized
    async def process_trade_payment(self, ctx, transaction: Dict):
        """处理交易支付。"""
        transaction = await database.get_transaction(transaction['id'])
        if not transaction or transaction['status'] != 'confirmed':
            if isinstance(ctx, discord.Interaction):
                await ctx.response.send_message('订单状态已变化，不能再次生成账单。', ephemeral=True)
            else:
                await ctx.respond('订单状态已变化，不能再次生成账单。', ephemeral=True)
            return
        # 如果是交互对象，延迟响应
        if isinstance(ctx, discord.Interaction):
            try:
                await ctx.response.defer(ephemeral=True)
            except discord.errors.InteractionResponded:
                pass

        # 创建一个新的视图，禁用所有按钮
        disabled_view = discord.ui.View()
        disabled_view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="取消交易",
            custom_id=f"cancel_trade:{transaction['id']}",
            disabled=True
        ))
        disabled_view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="支付",
            custom_id=f"pay_trade:{transaction['id']}",
            disabled=True
        ))

        # 更新原始消息，禁用所有按钮
        try:
            if isinstance(ctx, discord.Interaction):
                await ctx.message.edit(view=disabled_view)
        except Exception as e:
            logger.error(f"更新消息视图时出错: {e}")

        try:
            # 获取买家信息，检查免费托管积分
            user = await database.get_user(transaction["buyer_id"])
            has_free_escrow = False
            free_escrow_amount = 0.0
            escrow_fee = float(transaction["escrow_fee"])
            actual_escrow_fee = escrow_fee
            
            if user and "free_escrow_amount" in user and user["free_escrow_amount"] > 0:
                free_escrow_amount = float(user["free_escrow_amount"])
                
                if free_escrow_amount >= escrow_fee:
                    # 用户有足够的免费托管积分
                    has_free_escrow = True
                    actual_escrow_fee = 0.0
                elif free_escrow_amount > 0:
                    # 部分减免
                    actual_escrow_fee = escrow_fee - free_escrow_amount
            
            # 计算实际需要支付的总金额（考虑免费托管积分）
            if config.NEW.PAYMENTS_ENABLED:
                from utils import shared_payments
                reserved = await shared_payments.reserve_legacy_credits(transaction['id'], transaction['buyer_id'], escrow_fee)
                free_escrow_amount = float(reserved)
                actual_escrow_fee = escrow_fee - free_escrow_amount
                has_free_escrow = actual_escrow_fee == 0
            total_amount = float(transaction["amount"]) + actual_escrow_fee
            
            # 生成支付地址
            payment_address = await binance_api.get_deposit_address("USDT")
            
            if not payment_address:
                if isinstance(ctx, discord.Interaction):
                    await ctx.followup.send("无法生成支付地址，请联系管理员。", ephemeral=True)
                else:
                    await ctx.respond("无法生成支付地址，请联系管理员。", ephemeral=True)
                return
            
            # 生成唯一金额
            unique_amount, formatted_amount = await payment_utils.generate_unique_amount(total_amount, transaction["id"])
            
            # 更新交易支付信息
            await database.update_transaction_payment(transaction["id"], payment_address, unique_amount=unique_amount)

            # 更新交易状态为支付中
            await database.update_transaction_status(transaction["id"], "paying")
            
            # 添加支付超时任务
            self.bot.loop.create_task(
                self.payment_timeout(transaction["id"], 1800)  # 30分钟超时
            )
            
            # 计算超时时间戳
            timeout_timestamp = int((datetime.now(timezone.utc) + timedelta(seconds=1800)).timestamp())
            
            # 创建支付嵌入消息
            embed = discord.Embed(
                title="交易支付",
                description=f"请向以下地址支付 **{formatted_amount} USDT**",
                color=discord.Color.blue()
            )
            
            embed.add_field(
                name="支付地址",
                value=f"```{payment_address}```",
                inline=False
            )
            
            embed.add_field(
                name="支付金额",
                value=f"```{formatted_amount}``` USDT",
                inline=True
            )
            
            # 如果使用了免费托管积分，显示说明
            if has_free_escrow:
                embed.add_field(
                    name="担保费用",
                    value=f"{escrow_fee:.2f} USDT (已使用积分)",
                    inline=True
                )
            elif actual_escrow_fee < escrow_fee:
                embed.add_field(
                    name="担保费用",
                    value=f"{escrow_fee:.2f} USDT (已使用 {free_escrow_amount:.2f} 积分后实付 {actual_escrow_fee:.2f})",
                    inline=True
                )
            
            embed.add_field(
                name="⚠️ 支付超时",
                value=f"⚠️请勿在支付超时前1分钟内支付，否则可能将无法收到付款\n<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                inline=True
            )
            
            embed.add_field(
                name="⚠️ 重要提示",
                value="• 请支付**确切金额**以便系统自动识别您的付款\n• 支付后将无法取消交易\n• 请确保使用USDT-BEP20网络\n• 支付完成后请等待系统确认\n\n"
                      "**Binance用户:**\n"
                      "• 选择提现→USDT→BEP20网络\n"
                      "• 复制上方地址和金额\n\n"
                      "**其他钱包用户:**\n"
                      "• 使用「发送」功能\n"
                      "• 确保使用BEP20网络\n"
                      "• 选择USDT代币",
                inline=False
            )
            
            # 发送支付信息给买家（仅买家可见）
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)
            
            # 在交易频道中发送支付超时提示
            channel = self.bot.get_channel(transaction["channel_id"])
            if channel:
                timeout_embed = discord.Embed(
                    title="⚠️ 支付超时提醒",
                    description=f"支付信息已发送给买家（仅买家可见）。",
                    color=discord.Color.gold()
                )
                timeout_embed.add_field(
                    name="超时时间",
                    value=f"<t:{timeout_timestamp}:F> (<t:{timeout_timestamp}:R>)\n⚠️请勿在支付超时前1分钟内支付，否则可能将无法收到付款",
                    inline=True
                )
                await channel.send(embed=timeout_embed)
                    

            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="payment_initiated",
                actor_id=ctx.user.id if isinstance(ctx, discord.Interaction) else ctx.author.id,
                details=f"支付地址: {payment_address}, 金额: {formatted_amount} USDT"
            )
            
            # 通知卖家
            try:
                seller = self.bot.get_user(transaction["seller_id"])
                if seller:
                    seller_embed = discord.Embed(
                        title="买家已开始支付",
                        description=f"买家已开始为物品 '{transaction['item_name']}' 支付。",
                        color=discord.Color.blue()
                    )
                    
                    
                    
                    seller_embed.add_field(
                        name="⚠️ 支付超时",
                        value=f"<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                        inline=True
                    )
                    
                    await seller.send(embed=seller_embed)
            except Exception as e:
                logger.error(f"无法向卖家发送通知: {e}")
            
        except Exception as e:
            logger.error(f"处理支付时出错: {e}")
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send("处理支付时出错，请联系管理员。", ephemeral=True)
            else:
                await ctx.respond("处理支付时出错，请联系管理员。", ephemeral=True)
    
    async def check_payment_status(self, transaction_id: int, channel: discord.TextChannel):
        """检查交易的支付状态。"""
        try:
            logger.info(f"开始检查交易 {transaction_id} 的支付状态")
            # 最多检查30次（每分钟检查一次，总共30分钟）
            check_count = 0
            max_checks = 30
            
            while check_count < max_checks:
                # 检查任务是否被取消
                if asyncio.current_task().cancelled():
                    logger.info(f"交易 {transaction_id} 的支付检查任务被取消")
                    return
                # 获取交易信息
                transaction = await database.get_transaction(transaction_id)
                if not transaction:
                    logger.warning(f"交易 {transaction_id} 不存在，停止检查支付状态")
                    return
                
                # 打印当前交易状态用于调试
                logger.info(f"交易 {transaction_id} 当前状态: {transaction.get('status')}, txid: {transaction.get('txid')}")
                
                # 如果交易已不在等待支付状态，停止检查
                if transaction["status"] != "paying":
                    logger.info(f"交易 {transaction_id} 状态不是paying,而是 {transaction['status']}，停止检查")
                    return
                
                # 检查是否超时
                timeout_status = await redis_client.check_payment_timeout(transaction_id)
                logger.info(f"交易 {transaction_id} 支付超时检查: {'未超时' if timeout_status else '已超时'}")
                
                if not timeout_status:
                    await self.handle_payment_timeout(transaction, channel)
                    return
                
                # 如果交易有唯一金额和支付地址，检查是否收到付款
                if transaction.get("unique_amount") and transaction.get("payment_address"):
                    logger.info(f"检查交易 {transaction_id} 的存款，地址: {transaction['payment_address']}, 金额: {transaction['unique_amount']}")
                    txid = await binance_api.check_deposit_by_amount(
                        transaction["payment_address"], 
                        float(transaction["unique_amount"]),
                        transaction_id
                    )
                    
                    if txid:
                        logger.info(f"交易 {transaction_id} 找到匹配存款，txid: {txid}")
                        # 更新交易状态和txid
                        await database.update_transaction_payment(
                            transaction_id, 
                            transaction["payment_address"], 
                            txid=txid
                        )
                        
                        # 获取最新的交易信息
                        transaction = await database.get_transaction(transaction_id)
                        logger.info(f"更新后的交易信息: {transaction}")
                
                # 如果有txid，检查是否已确认
                if transaction.get("txid"):
                    is_confirmed = config.NEW.PAYMENTS_ENABLED or await binance_api.is_transaction_confirmed(transaction["txid"])
                    logger.info(f"交易 {transaction_id} txid: {transaction['txid']} 确认状态: {'已确认' if is_confirmed else '未确认'}")
                    
                    if is_confirmed:
                        # 更新交易状态
                        prev_status = transaction["status"]
                        if prev_status != 'paid':
                            await database.update_transaction_status(transaction_id, "paid")
                        logger.info(f"已将交易 {transaction_id} 状态从 {prev_status} 更新为 paid")
                        
                        # 记录操作
                        await database.log_transaction_action(
                            transaction_id=transaction_id,
                            action="payment_confirmed",
                            actor_id=transaction["buyer_id"],
                            details="支付已确认"
                        )
                        
                        # 检查用户是否有免费托管积分，支付确认后扣除
                        user = await database.get_user(transaction["buyer_id"])
                        escrow_fee = transaction["escrow_fee"]
                        
                        if not config.NEW.PAYMENTS_ENABLED and user and "free_escrow_amount" in user and user["free_escrow_amount"] > 0:
                            free_escrow_amount = user["free_escrow_amount"]
                            
                            if free_escrow_amount >= escrow_fee:
                                # 用户有足够的免费托管积分
                                new_amount = free_escrow_amount - escrow_fee
                                await database.update_user_free_escrow_amount(transaction["buyer_id"], new_amount)
                                
                                # 记录使用免费托管积分
                                await database.log_transaction_action(
                                    transaction_id,
                                    "used_free_escrow",
                                    transaction["buyer_id"],
                                    f"使用了 {escrow_fee}U 免费托管积分，剩余 {new_amount}U"
                                )
                            elif free_escrow_amount > 0:
                                # 部分减免
                                await database.update_user_free_escrow_amount(transaction["buyer_id"], 0)
                                
                                # 记录使用免费托管积分
                                await database.log_transaction_action(
                                    transaction_id,
                                    "used_free_escrow",
                                    transaction["buyer_id"],
                                    f"使用了 {free_escrow_amount}U 免费托管积分，剩余 0U"
                                )
                        
                        # 创建发货按钮
                        ship_button = discord.ui.Button(
                            style=discord.ButtonStyle.primary,
                            label="标记为已发货",
                            custom_id="shipped"
                        )
                        
                        # 创建争议按钮
                        dispute_button = discord.ui.Button(
                            style=discord.ButtonStyle.danger,
                            label="发起争议",
                            custom_id="dispute"
                        )
                        
                        view = discord.ui.View()
                        view.add_item(ship_button)
                        view.add_item(dispute_button)
                        
                        # 更新嵌入消息
                        embed = helpers.create_transaction_embed(
                            transaction_type="trade",
                            item_name=transaction["item_name"],
                            amount=transaction["amount"],
                            escrow_fee=transaction["escrow_fee"],
                            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
                            seller_name=self.bot.get_user(transaction["seller_id"]).name,
                            status="paid",
                            buyer_id=transaction["buyer_id"],
                            seller_id=transaction["seller_id"]
                        )
                        
                        logger.info(f"准备向交易 {transaction_id} 的卖家 {transaction['seller_id']} 发送发货按钮")
                        try:
                            message = await channel.send(
                                f"<@{transaction['seller_id']}>，买家的付款已确认。请发货并点击按钮更新状态。\n<@{transaction['buyer_id']}>，如果遇到问题，可以点击发起争议按钮。",
                                embed=embed,
                                view=view
                            )
                            logger.info(f"成功发送发货按钮，消息ID: {message.id}")
                            return
                        except Exception as e:
                            logger.error(f"发送发货按钮时出错: {e}")
                
                # 每60秒检查一次
                logger.info(f"交易 {transaction_id} 的支付尚未确认，60秒后再次检查")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    logger.info(f"交易 {transaction_id} 的支付检查任务在睡眠期间被取消")
                    return
                check_count += 1

                        
            # 如果达到最大检查次数但仍未完成，处理为超时
            transaction = await database.get_transaction(transaction_id)
            if transaction and transaction["status"] == "paying":
                logger.info(f"交易 {transaction_id} 检查达到最大次数，处理为超时")
                await self.handle_payment_timeout(transaction, channel)
                
        except asyncio.CancelledError:
            logger.info(f"交易 {transaction_id} 的支付检查任务被取消")
            return
        except Exception as e:
            logger.error(f"检查交易 {transaction_id} 的支付状态时出错: {e}")
        finally:
            # 清理任务引用
            logger.info(f"清理交易 {transaction_id} 的支付检查任务")
            self.payment_check_tasks.pop(transaction_id, None)
    
    @serialized
    async def handle_payment_timeout(self, transaction: Dict, channel: discord.TextChannel):
        """处理支付超时的情况。"""
        if transaction['status'] != 'paying':
            return
        if config.NEW.PAYMENTS_ENABLED:
            await database.log_transaction_action(transaction['id'], 'payment_review', transaction['buyer_id'], '支付超时，保留频道和订单供迟到账核对')
            await channel.send('付款时间已到，请勿继续付款。订单和频道已保留，请联系管理员核对迟到账款。')
            return
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.set_user_active_transaction(transaction["buyer_id"], None, transaction['id'])
        await database.set_user_active_transaction(transaction["seller_id"], None, transaction['id'])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="payment_timeout",
            actor_id=transaction["buyer_id"],
            details="支付超时"
        )
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=self.bot.get_user(transaction["seller_id"]).name,
            status="cancelled",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        await channel.send(
            f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}>，由于支付超时，交易已自动取消。",
            embed=embed
        )
        
        # 设置频道将在30秒后删除
        await channel.send("此频道将在30秒后删除。")
        await asyncio.sleep(30)
        await channel.delete()
    
    @serialized
    async def mark_shipped(self, interaction: discord.Interaction, transaction: Dict):
        """卖家标记物品为已发货。"""
        if transaction["status"] != "paid":
            await interaction.response.send_message("此交易当前无法标记为已发货。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "shipped")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="mark_shipped",
            actor_id=interaction.user.id,
            details="卖家标记物品为已发货"
        )
        
        # 创建确认收货按钮
        received_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="确认收货",
            custom_id="received"
        )
        
        dispute_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="发起争议",
            custom_id="dispute"
        )
        
        view = discord.ui.View()
        view.add_item(received_button)
        view.add_item(dispute_button)
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=interaction.user.name,
            status="shipped",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        # 使用channel.send而不是interaction.response.send_message确保消息对所有人可见
        try:
            # 先发送一个临时确认给卖家
            await interaction.response.send_message("已将物品标记为已发货，正在通知买家...", ephemeral=True)
            
            # 然后发送公开消息到频道
            await interaction.channel.send(
                f"<@{transaction['buyer_id']}>，卖家已发货。收到物品后请确认。",
                embed=embed,
                view=view
            )
            logger.info(f"交易 {transaction['id']} 已被标记为已发货")
        except Exception as e:
            logger.error(f"发送发货确认消息时出错: {e}")
            await interaction.followup.send("发送通知时出错，但物品已标记为已发货。请联系管理员。", ephemeral=True)
    
    async def confirm_receipt(self, interaction: discord.Interaction, transaction: Dict):
        """买家确认收货。"""
        if transaction["status"] != "shipped":
            await interaction.response.send_message("此交易当前无法确认收货。", ephemeral=True)
            return
        
        # 创建二次确认按钮
        confirm_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="确认，我已收到货物",
            custom_id=f"confirm_receipt_final:{transaction['id']}"
        )
        
        cancel_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="取消，我尚未收到",
            custom_id=f"cancel_receipt:{transaction['id']}"
        )
        
        view = discord.ui.View()
        view.add_item(confirm_button)
        view.add_item(cancel_button)
        
        # 创建确认嵌入消息
        embed = discord.Embed(
            title="确认收货确认",
            description="请确认您是否已经收到所购买的物品？\n\n**重要提示：**\n一旦确认收货，资金将被释放给卖家，此操作无法撤销。",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="物品名称", 
            value=f"{transaction['item_name']}",
            inline=True
        )
        
        embed.add_field(
            name="交易金额", 
            value=f"{transaction['amount']} USDT",
            inline=True
        )
        
        # 发送确认消息
        await interaction.response.send_message(
            f"{interaction.user.mention}，请再次确认您是否已收到物品：",
            embed=embed,
            view=view,
            ephemeral=True
        )
    
    @serialized
    async def open_dispute(self, interaction: discord.Interaction, transaction: Dict):
        """开启交易争议。"""
        if transaction["status"] not in ["paid", "shipped"]:
            await interaction.response.send_message("此交易当前无法开启争议。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "disputed")
        
        # 记录操作
        dispute_initiator = "买家" if interaction.user.id == transaction["buyer_id"] else "卖家"
        dispute_status = "支付后" if transaction["status"] == "paid" else "发货后"
        
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="dispute",
            actor_id=interaction.user.id,
            details=f"{dispute_initiator}在{dispute_status}开启了争议"
        )
        
        # 通知管理员
        admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID)
        # 确保始终有管理员标记，即使角色不存在也使用ID直接提及
        admin_mention = admin_role.mention if admin_role else f"<@&{config.ADMIN_ROLE_ID}>"
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=self.bot.get_user(transaction["seller_id"]).name,
            status="disputed",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        # 添加争议详情
        embed.add_field(
            name="争议信息",
            value=f"• 发起方: {interaction.user.mention} ({dispute_initiator})\n• 交易状态: {dispute_status}\n• 发起时间: <t:{int(datetime.now(timezone.utc).timestamp())}:F>",
            inline=False
        )
        
        await interaction.response.send_message(
            f"{admin_mention}，此交易已开启争议。请协助解决。\n"
            f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}> 请在此等待管理员处理。",
            embed=embed
        )
    
    @serialized
    async def cancel_trade(self, interaction: discord.Interaction, transaction: Dict):
        """取消交易。"""
        # 卖家不能在买家点击支付后取消交易
        if interaction.user.id == transaction["seller_id"] and (
            transaction["status"] == "confirmed" and transaction.get("payment_address") or
            transaction["status"] == "paying"
        ):
            await interaction.response.send_message("买家已经开始支付流程，您不能取消交易。请联系管理员处理。", ephemeral=True)
            return
        
        if transaction["status"] not in ["pending", "confirmed"]:
            await interaction.response.send_message("此交易当前无法取消。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.set_user_active_transaction(transaction["buyer_id"], None, transaction['id'])
        await database.set_user_active_transaction(transaction["seller_id"], None, transaction['id'])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="cancel",
            actor_id=interaction.user.id,
            details="用户取消了交易"
        )
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
            seller_name=self.bot.get_user(transaction["seller_id"]).name,
            status="cancelled",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        other_user_id = transaction["seller_id"] if interaction.user.id == transaction["buyer_id"] else transaction["buyer_id"]
        
        # 创建一个新的视图，所有按钮都禁用
        disabled_view = discord.ui.View()
        disabled_view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="取消交易",
            custom_id=f"cancel_trade:{transaction['id']}",
            disabled=True
        ))
        disabled_view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="支付",
            custom_id=f"pay_trade:{transaction['id']}",
            disabled=True
        ))
        
        await interaction.response.send_message(
            f"<@{other_user_id}>，{interaction.user.mention} 已取消交易。",
            embed=embed,
            view=disabled_view
        )
        
        # 设置频道将在30秒后删除
        await interaction.channel.send("此频道将在30秒后删除。")
        await asyncio.sleep(30)
        await interaction.channel.delete()

    async def payment_timeout(self, transaction_id: int, timeout_seconds: int):
        """设置交易支付超时和检查支付状态。"""
        try:
            logger.info(f"交易 {transaction_id} 设置支付超时 {timeout_seconds} 秒")
            
            # 设置Redis超时
            await redis_client.set_payment_timeout(transaction_id, timeout_seconds)
            
            # 获取交易信息
            transaction = await database.get_transaction(transaction_id)
            if not transaction:
                logger.warning(f"交易 {transaction_id} 不存在，无法设置支付超时")
                return
            
            # 获取频道
            channel_id = transaction.get("channel_id")
            if not channel_id:
                logger.warning(f"交易 {transaction_id} 没有关联的频道ID，无法检查支付状态")
                return
            
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"找不到交易 {transaction_id} 的频道 {channel_id}，无法检查支付状态")
                return
            
            # 记录支付请求时间戳，用于后续查询交易记录
            await redis_client.set_payment_timestamp(transaction_id, int(time.time()))
            
            # 创建任务检查支付状态
            logger.info(f"开始为交易 {transaction_id} 创建支付检查任务")
            
            # 如果已经有任务，先取消它
            existing_task = self.payment_check_tasks.get(transaction_id)
            if existing_task:
                if not existing_task.done() and not existing_task.cancelled():
                    logger.info(f"取消交易 {transaction_id} 的已有支付检查任务")
                    existing_task.cancel()

                    # 等待任务正确取消
                    try:
                        # 给任务一点时间进行清理操作
                        await asyncio.wait_for(asyncio.shield(asyncio.gather(existing_task, return_exceptions=True)), timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        # 忽略超时和取消异常
                        pass
                    except Exception as e:
                        # 记录任何其他异常
                        logger.error(f"等待任务取消时出错: {str(e)}")
                
                # 从字典中移除旧任务引用
                self.payment_check_tasks.pop(transaction_id, None)
            
            # 创建新的支付检查任务
            task = self.bot.loop.create_task(
                self.check_payment_status(transaction_id, channel)
            )

            # 添加任务完成回调函数
            def task_done_callback(future):
                try:
                    # 尝试获取任务结果，捕获异常
                    future.result()
                except asyncio.CancelledError:
                    logger.info(f"交易 {transaction_id} 的支付检查任务已被正常取消")
                except Exception as e:
                    logger.error(f"交易 {transaction_id} 的支付检查任务出错: {str(e)}")
                finally:
                    # 确保任务引用被清理
                    self.payment_check_tasks.pop(transaction_id, None)
            
            # 添加回调
            task.add_done_callback(task_done_callback)
            
            # 保存任务引用
            self.payment_check_tasks[transaction_id] = task

            logger.info(f"已为交易 {transaction_id} 创建支付检查任务")
            
        except Exception as e:
            logger.error(f"为交易 {transaction_id} 设置支付超时时出错: {e}")
            # 确保任务引用被清理
            self.payment_check_tasks.pop(transaction_id, None)

    @serialized
    async def handle_address_confirmation(self, interaction: discord.Interaction, transaction_id: int, address: str):
        """处理卖家确认收款地址。"""
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await interaction.response.send_message("找不到交易信息。", ephemeral=True)
            return
        
        # 确保用户是卖家
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("只有卖家可以确认收款地址。", ephemeral=True)
            return
        
        # 确保交易状态正确
        if transaction["status"] != "confirmed_receipt":
            await interaction.response.send_message("此交易当前无法释放资金。", ephemeral=True)
            return
        
        # 先发送确认消息
        try:
            await interaction.response.send_message("地址已确认，正在处理资金释放...", ephemeral=True)
        except:
            # 如果已经响应了交互，使用followup
            await interaction.followup.send("地址已确认，正在处理资金释放...", ephemeral=True)
        
        # 获取交易频道
        channel = self.bot.get_channel(transaction["channel_id"])
        if not channel:
            await interaction.followup.send("无法获取交易频道，处理失败。请联系管理员。", ephemeral=True)
            return
        
        # 向频道发送公开处理消息，不显示具体地址
        await channel.send(
            f"<@{interaction.user.id}>，您的收款地址已确认，正在处理资金释放..."
        )
        
        try:
            # 释放托管资金到提供的地址
            logger.info(f"开始为交易 {transaction_id} 释放资金，地址: {address[:10]}...")
            
            # 记录释放资金前的日志
            await database.log_transaction_action(
                transaction_id=transaction_id,
                action="release_funds_started",
                actor_id=interaction.user.id,
                details=f"开始释放资金到地址: {address[:10]}..."
            )
            
            success, withdrawal_id, error = await binance_api.release_escrow_payment(
                transaction_id,
                address.strip(),
                transaction["amount"]
            )
            
            if not success:
                # 记录释放资金失败的日志
                await database.log_transaction_action(
                    transaction_id=transaction_id,
                    action="release_funds_failed",
                    actor_id=interaction.user.id,
                    details=f"释放资金失败: {error}"
                )
                
                # 检查是否是因为并发操作导致的重复释放
                if "已经触发了资金释放" in error:
                    await channel.send(
                        f"<@{interaction.user.id}>，系统已在处理此交易的资金释放，请勿重复操作。"
                    )
                    return
                
                await channel.send(f"<@{interaction.user.id}>，释放资金时出错：{error}")
                return
                
            # 更新交易状态
            await database.complete_transaction(transaction_id)
            
            # 清除双方的活跃交易
            await database.set_user_active_transaction(transaction["buyer_id"], None, transaction['id'])
            await database.set_user_active_transaction(transaction["seller_id"], None, transaction['id'])
            
            # 清除等待收款地址的缓存
            await redis_client.clear_waiting_for_seller_address(transaction_id)
            await redis_client.clear_pending_address(transaction_id)
            
            # 记录操作 - 更新详细的资金释放信息
            await database.log_transaction_action(
                transaction_id=transaction_id,
                action="funds_released",
                actor_id=interaction.user.id,
                details=f"资金已释放到地址：{address[:10]}...，金额：{transaction['amount']} USDT，提现ID：{withdrawal_id}"
            )
            
            # 更新嵌入消息
            embed = helpers.create_transaction_embed(
                transaction_type="trade",
                item_name=transaction["item_name"],
                amount=transaction["amount"],
                escrow_fee=transaction["escrow_fee"],
                buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
                seller_name=self.bot.get_user(transaction["seller_id"]).name,
                status="completed",
                buyer_id=transaction["buyer_id"],
                seller_id=transaction["seller_id"]
            )
            
            # 发送公开消息到频道，让所有人都能看到
            await channel.send(
                f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}>，交易已完成！\n买家已确认收货，资金已释放到卖家提供的地址。",
                embed=embed
            )
            
            
            # 买家和卖家提及
            buyer_mention = f"<@{transaction['buyer_id']}>"
            seller_mention = f"<@{transaction['seller_id']}>"
            
            # 计划删除频道
            self.bot.loop.create_task(
                self.schedule_channel_deletion(
                    channel, 
                    transaction_id, 
                    minutes=5,  # 修改为5分钟 (300秒)
                    mentions=f" {buyer_mention} {seller_mention} "
                )
            )
            
        except Exception as e:
            logger.error(f"处理地址确认时出错: {e}")
            await channel.send(f"<@{interaction.user.id}>，处理请求时出错: {e}")

    async def handle_address_reenter(self, interaction: discord.Interaction, transaction_id: int):
        """处理卖家重新输入收款地址。"""
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await interaction.response.send_message("找不到交易信息。", ephemeral=True)
            return
        
        # 确保用户是卖家
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("只有卖家可以重新输入收款地址。", ephemeral=True)
            return
        
        # 确保交易状态正确
        if transaction["status"] != "shipped":
            await interaction.response.send_message("此交易当前无法重新输入地址。", ephemeral=True)
            return
        
        # 清除缓存的待确认地址
        await redis_client.clear_pending_address(transaction_id)
        
        # 发送消息提示重新输入
        await interaction.response.send_message("请在交易频道中重新输入您的收款地址。", ephemeral=True)
        
        # 获取交易频道
        channel = self.bot.get_channel(transaction["channel_id"])
        if not channel:
            await interaction.followup.send("无法获取交易频道，处理失败。请联系管理员。", ephemeral=True)
            return
        
        # 向频道发送公开消息
        await channel.send(
            f"<@{interaction.user.id}>，请重新输入您的USDT-BEP20收款地址。"
        )

    @serialized
    async def handle_confirm_receipt_final(self, interaction: discord.Interaction, transaction_id: int):
        """处理买家最终确认收货。"""
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await interaction.response.send_message("找不到交易信息。", ephemeral=True)
            return
        
        # 确保用户是买家
        if interaction.user.id != transaction["buyer_id"]:
            await interaction.response.send_message("只有买家可以确认收货。", ephemeral=True)
            return
        
        # 确保交易状态正确
        if transaction["status"] != "shipped":
            await interaction.response.send_message("此交易当前无法确认收货。", ephemeral=True)
            return
        
        try:
            # 更新交易状态为confirmed_receipt
            await database.update_transaction_status(transaction["id"], "confirmed_receipt")
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="receipt_confirmed",
                actor_id=interaction.user.id,
                details="买家确认收货，等待卖家提供收款地址"
            )
            
            # 向买家发送确认消息
            await interaction.response.send_message("您已确认收货，请等待卖家提供收款地址进行资金释放。", ephemeral=True)
            
            # 更新交易状态显示
            status_embed = helpers.create_transaction_embed(
                transaction_type="trade",
                item_name=transaction["item_name"],
                amount=transaction["amount"],
                escrow_fee=transaction["escrow_fee"],
                buyer_name=self.bot.get_user(transaction["buyer_id"]).name,
                seller_name=self.bot.get_user(transaction["seller_id"]).name,
                status="confirmed_receipt",
                buyer_id=transaction["buyer_id"],
                seller_id=transaction["seller_id"]
            )
            
            # 获取交易频道
            channel = self.bot.get_channel(transaction["channel_id"])
            if not channel:
                await interaction.followup.send("无法获取交易频道，处理失败。请联系管理员。", ephemeral=True)
                return
            
            # 发送状态更新
            await channel.send(
                f"<@{transaction['buyer_id']}> 已确认收货！",
                embed=status_embed
            )
            
            # 仍然缓存交易等待付款状态 - 用于额外的标记
            await redis_client.set_waiting_for_seller_address(transaction["id"], transaction["seller_id"])
            
            # 创建收款按钮
            collect_button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="收款",
                custom_id=f"collect_payment:{transaction['id']}"
            )
            
            seller_view = discord.ui.View()
            seller_view.add_item(collect_button)
            
            seller_embed = discord.Embed(
                title="收款操作",
                description=f"买家已确认收到物品 '{transaction['item_name']}'。\n\n卖家请点击下方按钮提供收款地址，以便系统释放资金。",
                color=discord.Color.green()
            )
            
            seller_embed.add_field(
                name="交易金额",
                value=f"{transaction['amount']} USDT",
                inline=True
            )
            
            seller_embed.add_field(
                name="交易ID",
                value=f"{transaction['id']}",
                inline=True
            )
            
            # 发送收款按钮到交易频道
            await channel.send(
                f"<@{transaction['seller_id']}>，请点击下方按钮收款。",
                embed=seller_embed,
                view=seller_view
            )
            
            logger.info(f"交易 {transaction['id']} 已确认收货，等待卖家提供收款地址")
        except Exception as e:
            logger.error(f"处理最终确认收货请求时出错: {e}")
            await interaction.followup.send("处理请求时出错，请稍后重试或联系管理员。", ephemeral=True)

    async def handle_cancel_receipt(self, interaction: discord.Interaction, transaction_id: int):
        """处理买家取消确认收货。"""
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await interaction.response.send_message("找不到交易信息。", ephemeral=True)
            return
        
        # 确保用户是买家
        if interaction.user.id != transaction["buyer_id"]:
            await interaction.response.send_message("只有买家可以取消确认收货。", ephemeral=True)
            return
        
        # 确保交易状态正确
        if transaction["status"] != "shipped":
            await interaction.response.send_message("此交易当前无法取消确认收货。", ephemeral=True)
            return
        
        try:
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="receipt_confirmation_cancelled",
                actor_id=interaction.user.id,
                details="买家取消了确认收货"
            )
            
            # 编辑原始的二次确认消息，告知用户操作已取消，并移除按钮
            await interaction.response.edit_message(
                content="您已取消确认收货。如果您仍需确认收货，请再次点击交易频道中的【确认收货】按钮。",
                view=None,  # 移除所有组件（按钮）
                embed=None  # 清除任何嵌入内容
            )
            
            logger.info(f"交易 {transaction['id']} 的买家取消了确认收货，并编辑了提示消息。")
        except Exception as e:
            logger.error(f"处理取消确认收货请求时出错: {e}")
            # 如果编辑消息失败（可能因为交互已响应），尝试发送追踪消息
            try:
                # 检查交互是否已响应，避免重复响应错误
                if not interaction.response.is_done():
                    await interaction.response.send_message("处理取消确认收货时出错，请稍后重试或联系管理员。", ephemeral=True)
                else:
                    await interaction.followup.send("处理取消确认收货时出错，请稍后重试或联系管理员。", ephemeral=True)
            except discord.errors.HTTPException as http_err:
                logger.error(f"发送追踪消息时HTTP错误: {http_err}")
            except Exception as e2:
                logger.error(f"发送追踪消息时进一步出错: {e2}")

    async def handle_collect_payment(self, interaction: discord.Interaction, transaction_id: int):
        """处理卖家收集付款。"""
        # 获取交易信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await interaction.response.send_message("找不到交易信息。", ephemeral=True)
            return
        
        # 确保用户是卖家
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("只有卖家可以收集付款。", ephemeral=True)
            return
        
        # 确保交易状态正确
        if transaction["status"] != "confirmed_receipt":
            await interaction.response.send_message("此交易当前无法收集付款。", ephemeral=True)
            return
        
        try:
            # 获取卖家信息
            seller = await database.get_user(transaction["seller_id"])
            seller_address = seller.get("payment_address") if seller else None
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="collect_payment_initiated",
                actor_id=interaction.user.id,
                details="卖家开始收集付款流程"
            )
            
            # 如果有保存的地址
            if seller_address:
                # 创建确认视图
                view = discord.ui.View()
                
                # 使用已保存地址按钮
                use_address_button = discord.ui.Button(
                    style=discord.ButtonStyle.success,
                    label="使用现有地址",
                    custom_id=f"use_saved_address:{transaction_id}"
                )
                
                # 输入新地址按钮
                input_address_button = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="更新地址",
                    custom_id=f"input_address:{transaction_id}"
                )
                
                view.add_item(use_address_button)
                view.add_item(input_address_button)
                
                # 创建嵌入消息
                embed = discord.Embed(
                    title="确认收款地址",
                    description="您已有收款地址，是否使用现有地址？",
                    color=discord.Color.blue()
                )
                
                embed.add_field(
                    name="您的收款地址",
                    value=f"```{seller_address}```",
                    inline=False
                )
                
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            else:
                # 没有保存的地址，直接显示地址输入模态框
                modal = discord.ui.Modal(title="输入收款地址")
                address_input = discord.ui.InputText(
                    label="USDT-BEP20 收款地址",
                    placeholder="请输入您的USDT-BEP20收款地址",
                    required=True,
                    min_length=42,
                    max_length=42
                )
                modal.add_item(address_input)
                
                # 捕获transaction_id作为闭包变量
                transaction_id_for_callback = transaction_id
                
                async def modal_callback(modal_interaction):
                    try:
                        new_address = address_input.value.strip()
                        if len(new_address) != 42 or not new_address.startswith("0x"):
                            await modal_interaction.response.send_message("地址格式不正确，请确保是42位的BEP20地址，以0x开头。", ephemeral=True)
                            return
                        
                        # 更新用户的默认地址
                        update_success = await database.update_user_payment_address(
                            transaction["seller_id"], 
                            new_address
                        )
                        
                        if not update_success:
                            await modal_interaction.response.send_message("更新地址失败，请稍后重试。", ephemeral=True)
                            return
                        
                        # 处理地址确认
                        await self.handle_address_confirmation(modal_interaction, transaction_id_for_callback, new_address)
                    except Exception as e:
                        logger.error(f"处理地址输入时出错: {e}")
                        await modal_interaction.response.send_message(f"处理地址时出错: {e}", ephemeral=True)
                
                modal.callback = modal_callback
                await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"处理收集付款请求时出错: {e}")
            await interaction.followup.send("处理请求时出错，请稍后重试或联系管理员。", ephemeral=True)

    async def schedule_channel_deletion(self, channel, transaction_id=None, minutes=5, mentions=""):
        """计划删除频道，并提供取消按钮。
        
        Args:
            channel: 要删除的频道
            transaction_id: 交易ID（可选）
            minutes: 删除前等待的分钟数
            mentions: 要在消息中提及的用户或角色（例如 <@123456789>）
        """
        if not channel:
            logger.error("无法获取频道，无法计划删除。")
            return

        try:
            # 创建带有取消按钮的视图
            class CancelDeletionView(discord.ui.View):
                def __init__(self, cog):
                    super().__init__(timeout=minutes*60)
                    self.cog = cog
                    self.cancelled = False
                
                @discord.ui.button(label="取消删除频道", style=discord.ButtonStyle.danger)
                async def cancel_deletion(self, button, interaction):
                    self.cancelled = True
                    # 停用所有按钮
                    for item in self.children:
                        item.disabled = True
                    
                    # 发送确认消息
                    await interaction.response.edit_message(content="⚠️ 频道删除已取消。频道将被保留。", view=self)
                    
                    # 获取交易信息（可选，只是用于日志）
                    transaction = await database.get_transaction_by_channel(channel.id)
                    
                    # 获取管理员角色提及
                    admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID)
                    admin_mention = admin_role.mention if admin_role else f"<@&{config.ADMIN_ROLE_ID}>"
                    
                    if transaction:
                        # 记录操作
                        await database.log_transaction_action(
                            transaction_id=transaction["id"],
                            action="stop_deletion",
                            actor_id=interaction.user.id,
                            details="用户请求停止删除频道"
                        )
                        
                        # 创建嵌入消息，显示交易详情
                        embed = helpers.create_transaction_embed(
                            transaction_type="trade",
                            item_name=transaction["item_name"],
                            amount=transaction["amount"],
                            escrow_fee=transaction["escrow_fee"],
                            buyer_name=self.cog.bot.get_user(transaction["buyer_id"]).name if self.cog.bot.get_user(transaction["buyer_id"]) else "未知买家",
                            seller_name=self.cog.bot.get_user(transaction["seller_id"]).name if self.cog.bot.get_user(transaction["seller_id"]) else "未知卖家",
                            status=transaction["status"],
                            buyer_id=transaction["buyer_id"],
                            seller_id=transaction["seller_id"]
                        )
                        
                        # 在频道中发送带有交易信息的通知
                        await channel.send(
                            f"{admin_mention} {interaction.user.mention} 已取消删除此频道，请管理员关注此交易。\n"
                            f"交易ID: {transaction['id']}",
                            embed=embed
                        )
                    else:
                        # 在频道中发送简单通知
                        await channel.send(
                            f"{admin_mention} {interaction.user.mention} 已取消删除此频道，请管理员关注。"
                        )
            
            # 创建视图实例
            view = CancelDeletionView(self)
            
            # 计算删除时间（秒）
            delay_seconds = minutes * 60
            
            # 计算删除时间
            deletion_time = int((datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp())
            
            # 创建删除提醒嵌入消息
            embed = discord.Embed(
                title="⚠️ 频道删除提醒",
                description=f"此交易已完成，频道将在稍后自动删除。",
                color=discord.Color.gold()
            )
            
            embed.add_field(
                name="删除时间",
                value=f"<t:{deletion_time}:F> (<t:{deletion_time}:R>)",
                inline=False
            )
            
            embed.add_field(
                name="保留频道",
                value=f"⚠️ 此频道将在 {minutes * 60} 秒后自动删除，如有异议请点击取消删除频道按钮通知管理员{mentions}",
                inline=False
            )
            
            # 发送删除通知
            await channel.send(embed=embed, view=view)
            
            # 记录操作
            if transaction_id:
                await database.log_transaction_action(
                    transaction_id=transaction_id,
                    action="channel_deletion_scheduled",
                    actor_id=self.bot.user.id,
                    details=f"频道将在 {minutes * 60} 秒后删除"
                )
            
            # 等待指定的时间
            await asyncio.sleep(delay_seconds)
            
            # 检查是否已取消删除
            if view.cancelled:
                logger.info(f"频道 {channel.name} (ID: {channel.id}) 的删除已被取消")
                return
            
            # 再次确认并删除频道
            try:
                await channel.delete(reason="交易已完成，自动删除频道")
                logger.info(f"频道 {channel.name} (ID: {channel.id}) 已自动删除")
            except discord.errors.NotFound:
                logger.warning(f"尝试删除已不存在的频道: {channel.id}")
            except discord.errors.Forbidden:
                logger.warning(f"没有权限删除频道: {channel.id}")
            except Exception as e:
                logger.error(f"删除频道时出错: {e}")
        
        except asyncio.CancelledError:
            # 正确处理协程取消
            logger.warning(f"删除频道 {channel.id} 的任务被取消")
            raise
        except Exception as e:
            logger.error(f"计划删除频道 {channel.id} 时出错: {e}", exc_info=True)

    @serialized
    async def buyer_cancel_trade(self, interaction: discord.Interaction, transaction: Dict):
        """买家取消自己的交易请求。"""
        if transaction["status"] != "pending":
            await interaction.response.send_message("此交易已不能取消。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.set_user_active_transaction(transaction["buyer_id"], None, transaction['id'])
        await database.set_user_active_transaction(transaction["seller_id"], None, transaction['id'])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="buyer_cancel",
            actor_id=interaction.user.id,
            details="买家取消了交易请求"
        )
        
        # 更新嵌入消息
        embed = helpers.create_transaction_embed(
            transaction_type="trade",
            item_name=transaction["item_name"],
            amount=transaction["amount"],
            escrow_fee=transaction["escrow_fee"],
            buyer_name=interaction.user.name,
            seller_name=self.bot.get_user(transaction["seller_id"]).name,
            status="cancelled",
            buyer_id=transaction["buyer_id"],
            seller_id=transaction["seller_id"]
        )
        
        await interaction.response.send_message(
            f"<@{transaction['seller_id']}>，买家已取消此交易请求。",
            embed=embed
        )
        
        # 设置频道将在30秒后删除
        await interaction.channel.send("此频道将在30秒后删除。")
        await asyncio.sleep(30)
        await interaction.channel.delete()

def setup(bot):
    if not config.LEGACY_TRADING_ENABLED:
        return
    """加载交易托管组件。"""
    bot.add_cog(Trading(bot)) 
