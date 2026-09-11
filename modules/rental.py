import discord
from discord.ext import commands
from discord import SlashCommandGroup, ApplicationContext
from discord.commands import user_command
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

import config
from utils import database, redis_client, helpers, binance_api, payment_utils

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Rental(commands.Cog):
    """处理租赁托管功能。"""
    
    def __init__(self, bot):
        self.bot = bot
        self.payment_check_tasks = {}  # 存储支付检查任务
        self.rental_reminder_tasks = {}  # 存储租赁提醒任务
    
    # 管理员命令组
    rental_admin = SlashCommandGroup("rental_admin", "租赁管理员命令", default_member_permissions=discord.Permissions(administrator=True))

    @rental_admin.command(name="close_channel", description="移除当前频道的活跃租赁并关闭")
    async def close_rental_channel(self, ctx: ApplicationContext):
        """移除当前频道的活跃租赁ID，并将租赁状态标记为已关闭。"""
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
                await ctx.respond("此频道没有关联的活跃租赁。", ephemeral=True)
                return
                
        # 获取交易和租赁信息
        transaction = await database.get_transaction(transaction_id)
        if not transaction:
            await ctx.respond("找不到关联的租赁信息。", ephemeral=True)
            return
            
        # 检查是否是租赁类型（而非交易）
        if transaction["transaction_type"] != "rental":
            await ctx.respond("此频道是交易频道，请使用 `/trade_admin close_channel` 命令关闭。", ephemeral=True)
            return
            
        # 获取租赁详情
        rental = await database.get_rental_by_transaction(transaction_id)
        if not rental and transaction["transaction_type"] == "rental":
            await ctx.respond("找不到关联的租赁详情信息。", ephemeral=True)
            return
            
        # 清除用户的活跃租赁
        await database.decrement_active_rental(transaction["buyer_id"], transaction_id)
        await database.decrement_active_rental(transaction["seller_id"], transaction_id)
        
        # 更新交易状态为已取消
        await database.update_transaction_status(transaction_id, "cancelled")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction_id,
            action="admin_close",
            actor_id=ctx.author.id,
            details="管理员关闭了租赁"
        )
        
        # 响应
        await ctx.respond(f"已成功关闭租赁ID {transaction_id}，并清除用户的活跃租赁状态。")
        
        # 发送租赁关闭通知
        embed = discord.Embed(
            title="租赁已被管理员关闭",
            description=f"租赁ID：{transaction_id}\n物品：{transaction['item_name']}\n租金：{transaction['amount']} USDT",
            color=discord.Color.red()
        )
        
        if rental:
            embed.add_field(
                name="租赁详情",
                value=f"保证金：{rental['deposit']} USDT\n租期：{rental['rental_period']} {'小时' if rental['rental_unit'] == 'hours' else '天'}",
                inline=False
            )
        
        embed.add_field(
            name="租赁参与者",
            value=f"租户：<@{transaction['buyer_id']}>\n出租方：<@{transaction['seller_id']}>",
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
    @user_command(name="开始租赁")
    async def rental_callback(self, ctx: ApplicationContext, user: discord.User):
        """发起与其他用户的租赁。"""
        # 检查是否为同一用户
        if ctx.author.id == user.id:
            await ctx.respond("您不能与自己发起租赁。", ephemeral=True)
            return
        
        # 获取双方用户角色
        renter_member = ctx.guild.get_member(ctx.author.id)
        owner_member = ctx.guild.get_member(user.id)
        
        if not renter_member or not owner_member:
            await ctx.respond("无法获取用户信息，请稍后再试。", ephemeral=True)
            return
        
        renter_roles = [role.id for role in renter_member.roles]
        owner_roles = [role.id for role in owner_member.roles]
        
        # 检查租户是否可以创建新的租赁
        renter = await database.get_user(ctx.author.id)
        owner = await database.get_user(user.id)
        
        if not renter:
            await database.create_user(ctx.author.id, str(ctx.author))
            renter = await database.get_user(ctx.author.id)
        
        if not owner:
            await database.create_user(user.id, str(user))
            owner = await database.get_user(user.id)
        
        # 检查租户是否可以创建新的租赁
        can_create_rental_renter = await database.can_create_new_rental(ctx.author.id, renter_roles)
        if not can_create_rental_renter:
            await ctx.respond("您已达到最大租赁数量限制。请先完成现有租赁。", ephemeral=True)
            return
        
        # 检查出租方是否可以创建新的租赁
        can_create_rental_owner = await database.can_create_new_rental(user.id, owner_roles)
        if not can_create_rental_owner:
            await ctx.respond("对方已达到最大租赁数量限制。请稍后再试。", ephemeral=True)
            return
        
        # 创建租赁表单
        modal = discord.ui.Modal(title="创建租赁请求")
        
        item_name_input = discord.ui.InputText(
            label="物品名称",
            placeholder="请输入要租赁的物品名称",
            required=True,
            min_length=3,
            max_length=100
        )
        
        rental_fee_input = discord.ui.InputText(
            label="租金 (USDT)",
            placeholder="请输入租金金额",
            required=True
        )
        
        deposit_input = discord.ui.InputText(
            label="保证金 (USDT)",
            placeholder="请输入保证金金额",
            required=True
        )
        
        rental_period_input = discord.ui.InputText(
            label="租赁期限",
            placeholder="请输入租赁期限（例如：24h 或 3d）",
            required=True
        )
        
        image_url_input = discord.ui.InputText(
            label="物品图片URL",
            placeholder="请输入物品图片的Discord URL地址",
            required=True,
            min_length=10,
            max_length=500
        )
        
        modal.add_item(item_name_input)
        modal.add_item(rental_fee_input)
        modal.add_item(deposit_input)
        modal.add_item(rental_period_input)
        modal.add_item(image_url_input)
        
        async def modal_callback(interaction: discord.Interaction):
            """处理租赁表单提交。"""
            # 首先延迟响应以防止交互超时
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception as e:
                logger.warning(f"延迟响应时出错: {e}")
                # 如果已经响应过，继续处理
            
            # 获取表单数据并进行安全处理
            item_name = helpers.sanitize_input(item_name_input.value)
            
            try:
                # 检查科学计数法
                rental_fee_str = helpers.sanitize_input(rental_fee_input.value)
                deposit_str = helpers.sanitize_input(deposit_input.value)
                period_input = helpers.sanitize_input(rental_period_input.value)
                image_url = image_url_input.value.strip()
                
                if ('e' in rental_fee_str.lower() or 'E' in rental_fee_str or 
                    'e' in deposit_str.lower() or 'E' in deposit_str):
                    await interaction.followup.send("金额不允许使用科学计数法，请输入普通数字格式。", ephemeral=True)
                    return
                    
                # 检查数字长度以防止溢出
                if (len(rental_fee_str.replace(',', '').replace('.', '')) > 15 or
                    len(deposit_str.replace(',', '').replace('.', '')) > 15):
                    await interaction.followup.send("金额数值过大，请输入合理的金额。", ephemeral=True)
                    return
                
                rental_fee = float(rental_fee_str)
                deposit = float(deposit_str)
                
                # 验证金额
                if rental_fee <= 0 or deposit <= 0:
                    await interaction.followup.send("租金和保证金必须大于0。", ephemeral=True)
                    return
                
                # 解析租赁期限
                rental_period = 0
                rental_unit = "days"  # 默认单位为天
                
                if 'h' in period_input:
                    # 小时为单位
                    rental_unit = "hours"
                    rental_period = int(period_input.replace('h', '').strip())
                    if rental_period <= 0:
                        await interaction.followup.send("租赁期限必须大于0小时。", ephemeral=True)
                        return
                elif 'd' in period_input:
                    # 天为单位
                    rental_unit = "days"
                    rental_period = int(period_input.replace('d', '').strip())
                    if rental_period <= 0:
                        await interaction.followup.send("租赁期限必须大于0天。", ephemeral=True)
                        return
                else:
                    try:
                        # 尝试直接解析为天数
                        rental_period = int(period_input)
                        if rental_period <= 0:
                            await interaction.followup.send("租赁期限必须大于0天。", ephemeral=True)
                            return
                    except ValueError:
                        await interaction.followup.send("无效的租赁期限格式。请使用如 '24h' 或 '3d' 的格式。", ephemeral=True)
                        return
                
                # 验证图片URL
                if not image_url.startswith(('https://cdn.discordapp.com/','https://media.discordapp.net/')):
                    await interaction.followup.send("请输入有效的Discord图片URL地址。", ephemeral=True)
                    return
                
                # 计算托管费用
                escrow_fee = 0.0
                
                # 检查租金是否低于最小收费金额
                if rental_fee >= config.RENTAL_MIN_AMOUNT_FOR_FEE:
                    if rental_unit == "hours":
                        # 小时租赁，按天计算，不足1天按1天计算
                        days = max(1, (rental_period + 23) // 24)  # 向上取整到天
                        escrow_fee = days * 2.0  # 每天2U
                    else:
                       escrow_fee = rental_period * 2.0 # 大于等于3天，每天1U
                
                # 创建私人频道
                guild = interaction.guild
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
                
                # 获取租赁分类
                rental_category = guild.get_channel(config.RENTAL_CATEGORY_ID)
                if not rental_category:
                    await interaction.response.send_message("找不到租赁分类，请联系管理员。", ephemeral=True)
                    return
                
                # 创建频道
                channel_name = f"租赁-{item_name[:20]}-{interaction.user.name[:10]}-{user.name[:10]}"
                channel = await guild.create_text_channel(
                    name=channel_name,
                    category=rental_category,
                    overwrites=overwrites
                )
                
                # 创建交易记录
                transaction_id = await database.create_transaction(
                    "rental",
                    interaction.user.id,
                    user.id,
                    item_name,
                    rental_fee,
                    escrow_fee,
                    channel.id
                )
                
                # 创建租赁记录
                await database.create_rental(
                    transaction_id,
                    rental_period,
                    deposit,
                    rental_fee,
                    rental_unit
                )
                
                # 设置用户活跃租赁
                await database.set_user_active_rental(interaction.user.id, transaction_id)
                await database.set_user_active_rental(user.id, transaction_id)
                
                # 更新双方的租赁计数
                await database.increment_rental_count(interaction.user.id)
                await database.increment_rental_count(user.id)
                
                # 记录操作
                await database.log_transaction_action(
                    transaction_id,
                    "rental_created",
                    interaction.user.id,
                    f"租赁物品: {item_name}, 租金: {rental_fee} USDT, 保证金: {deposit} USDT, 租赁期限: {rental_period} {rental_unit}"
                )
                
                # 检查用户是否有免费托管积分
                user_data = await database.get_user(interaction.user.id)
                has_free_escrow = False
                free_escrow_amount = 0.0
                actual_escrow_fee = escrow_fee
                
                if user_data and "free_escrow_amount" in user_data and user_data["free_escrow_amount"] > 0:
                    free_escrow_amount = user_data["free_escrow_amount"]
                    
                    if free_escrow_amount >= escrow_fee:
                        # 用户有足够的免费托管积分
                        has_free_escrow = True
                        actual_escrow_fee = 0.0
                    elif free_escrow_amount > 0:
                        # 部分减免
                        actual_escrow_fee = escrow_fee - free_escrow_amount
                
                # 更新总金额，加上实际托管费用
                total_amount = rental_fee + deposit + actual_escrow_fee
                
                # 创建租赁信息嵌入消息
                embed = discord.Embed(
                    title=f"🔄 租赁请求: {item_name}",
                    description=f"请出租方 {user.mention} 确认此租赁请求。",
                    color=discord.Color.blue()
                )
                
                # 添加物品图片
                embed.set_image(url=image_url)
                
                embed.add_field(name="👤 租户", value=interaction.user.mention, inline=True)
                embed.add_field(name="👑 出租方", value=user.mention, inline=True)
                embed.add_field(name="📦 物品", value=item_name, inline=True)
                
                embed.add_field(name="💰 租金", value=f"{rental_fee} USDT", inline=True)
                embed.add_field(name="💵 保证金", value=f"{deposit} USDT", inline=True)
                
                period_display = f"{rental_period} 小时" if rental_unit == "hours" else f"{rental_period} 天"
                embed.add_field(name="⏱️ 租赁期限", value=period_display, inline=True)
                
                # 显示托管费用，考虑积分
                if has_free_escrow:
                    embed.add_field(name="🏦 托管费用", value=f"{escrow_fee} USDT (使用积分)", inline=True)
                elif actual_escrow_fee < escrow_fee:
                    embed.add_field(name="🏦 托管费用", value=f"{escrow_fee} USDT (使用 {free_escrow_amount} 积分后实付 {actual_escrow_fee:.2f})", inline=True)
                else:
                    embed.add_field(name="🏦 托管费用", value=f"{escrow_fee} USDT (每天2U)", inline=True)
                
                # 计算总支付金额，考虑积分
                embed.add_field(name="💰 总支付金额", value=f"{total_amount} USDT", inline=True)
                embed.add_field(name="🖼️物品图片", value="🔍请核对**物品和金额详细信息**后再确认租赁", inline=False)
                
                # 创建确认/拒绝按钮
                confirm_button = discord.ui.Button(
                    style=discord.ButtonStyle.success,
                    label="确认租赁",
                    custom_id=f"confirm_rental:{transaction_id}"
                )
                
                reject_button = discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="取消租赁",
                    custom_id=f"reject_rental:{transaction_id}"
                )
                
                view = discord.ui.View()
                view.add_item(confirm_button)
                view.add_item(reject_button)
                
                # 发送租赁信息
                await channel.send(f"{interaction.user.mention} 和 {user.mention}", embed=embed, view=view)
                
                # 通知用户
                try:
                    await interaction.followup.send(f"租赁请求已创建，请前往 {channel.mention} 继续。", ephemeral=True)
                except Exception as e:
                    logger.error(f"发送确认消息失败: {e}")
                    # 如果followup失败，尝试在创建的频道中通知
                    await channel.send(f"{interaction.user.mention} 租赁请求已创建，可在本频道继续处理。")
            except ValueError:
                try:
                    await interaction.followup.send("无效的金额格式。请输入有效的数字。", ephemeral=True)
                except Exception as e:
                    logger.error(f"发送错误消息失败: {e}")
            except Exception as e:
                logger.error(f"创建租赁时出错: {e}", exc_info=True)
                try:
                    await interaction.followup.send("创建租赁时出错，请稍后再试。", ephemeral=True)
                except Exception as follow_error:
                    logger.error(f"发送错误消息失败: {follow_error}")
                    # 如果用户已经离开交互界面，无法发送消息，这里处理
        
        # 设置模态框回调
        modal.callback = modal_callback
        
        # 发送模态框
        await ctx.response.send_modal(modal)
    
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """处理按钮交互"""
        # 处理自定义ID按钮点击
        if "custom_id" in interaction.data:
            custom_id = interaction.data["custom_id"]
            base_custom_id = custom_id.split(":")[0] if ":" in custom_id else custom_id
            
            try:
                # 如果是交易相关的自定义ID
                if ":" in custom_id and custom_id.split(":")[0] in [
                    "confirm_rental", "reject_rental", "confirm_payment", 
                    "pay_rental", "start_rental", "return_item", 
                    "confirm_rental_return", "dispute_rental_return",
                    "ship_rental_item", "confirm_receipt", "remind_return",
                    "dispute_rental", "confirm_return", "use_existing_address",
                    "use_existing_addresses", "update_addresses", "update_renter_address",
                    "cancel_rental", "paying", "confirm_return_ask",  # 添加 confirm_return_ask
                    "confirm_return_yes", "confirm_return_no",  # 添加确认和拒绝二次确认按钮
                    "dispute_rental_confirm_yes", "dispute_rental_confirm_no"  # 添加争议确认和取消按钮
                ]:
                    # 获取交易ID
                    transaction_id = int(custom_id.split(":")[1])
                    
                    # 获取交易信息
                    transaction = await database.get_transaction(transaction_id)
                    if not transaction:
                        await interaction.response.send_message("找不到交易信息。", ephemeral=True)
                        return
                    
                    # 获取租赁信息
                    rental = await database.get_rental(transaction_id)
                    if not rental:
                        await interaction.response.send_message("找不到租赁信息。", ephemeral=True)
                        return
                    
                    # 根据自定义ID的基础部分执行相应操作
                    if base_custom_id == "confirm_rental" and interaction.user.id == transaction["seller_id"]:
                        await self.confirm_rental(interaction, transaction, rental)
                    
                    elif base_custom_id == "reject_rental":
                        # 移除了seller_id检查，允许买卖双方都能拒绝租赁
                        await self.reject_rental(interaction, transaction)
                    
                    elif base_custom_id == "pay_rental" and interaction.user.id == transaction["buyer_id"]:
                        try:
                            # 延迟响应以防止交互超时
                            await interaction.response.defer(ephemeral=True)
                            
                            # 更新交易状态为支付中
                            await database.update_transaction_status(transaction["id"], "paying")
                            
                            # 创建新的视图，禁用取消按钮
                            disabled_view = discord.ui.View()
                            disabled_view.add_item(discord.ui.Button(
                                style=discord.ButtonStyle.danger,
                                label="取消租赁",
                                custom_id=f"cancel_rental:{transaction['id']}",
                                disabled=True
                            ))
                            
                            # 更新原始消息，禁用取消按钮
                            try:
                                await interaction.message.edit(view=disabled_view)
                            except Exception as e:
                                logger.error(f"更新消息视图时出错: {e}")
                            
                            # 继续处理支付流程
                            await self.process_rental_payment(interaction, transaction, rental)
                            
                        except Exception as e:
                            logger.error(f"处理支付按钮点击时出错: {e}", exc_info=True)
                            try:
                                await interaction.followup.send("处理支付请求时出错，请重试。", ephemeral=True)
                            except:
                                pass
                    
                    elif base_custom_id == "start_rental" and interaction.user.id == transaction["seller_id"]:
                        await self.start_rental(interaction, transaction, rental)
                    
                    elif base_custom_id == "return_item" and interaction.user.id == transaction["buyer_id"]:
                        await self.return_item(interaction, transaction, rental)
                    
                    elif base_custom_id == "confirm_rental_return" and interaction.user.id == transaction["seller_id"]:
                        await self.confirm_return(interaction, transaction, rental)
                    
                    elif base_custom_id == "dispute_rental_return":
                        await self.open_rental_dispute(interaction, transaction, rental)
                    
                    elif base_custom_id == "ship_rental_item" and interaction.user.id == transaction["seller_id"]:
                        await self.ship_rental_item(interaction, transaction, rental)
                    
                    elif base_custom_id == "confirm_receipt" and interaction.user.id == transaction["buyer_id"]:
                        await self.confirm_receipt(interaction, transaction, rental)
                    
                    elif base_custom_id == "remind_return" and interaction.user.id == transaction["seller_id"]:
                        await self.remind_return(interaction, transaction, rental)
                    
                    elif base_custom_id == "dispute_rental":
                        # 不是直接开启争议，而是显示二次确认对话框
                        yes_button = discord.ui.Button(
                            style=discord.ButtonStyle.danger,
                            label="确认发起争议",
                            custom_id=f"dispute_rental_confirm_yes:{transaction_id}"
                        )
                        
                        no_button = discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label="取消",
                            custom_id=f"dispute_rental_confirm_no:{transaction_id}"
                        )
                        
                        view = discord.ui.View()
                        view.add_item(yes_button)
                        view.add_item(no_button)
                        
                        # 创建二次确认嵌入消息
                        embed = discord.Embed(
                            title="⚠️ 确认发起争议",
                            description=f"您确定要为租赁 '{transaction['item_name']}' 发起争议吗？",
                            color=discord.Color.gold()
                        )
                        
                        embed.add_field(
                            name="重要提示",
                            value="发起争议后，正常的租赁流程将会终止，管理员将介入处理。请仅在确实存在问题并且双方无法自行解决时使用此功能。",
                            inline=False
                        )
                        
                        await interaction.response.send_message(
                            "请确认是否要发起争议：",
                            embed=embed,
                            view=view,
                            ephemeral=True
                        )
                    
                    # 处理争议确认
                    elif base_custom_id == "dispute_rental_confirm_yes":
                        await self.open_rental_dispute(interaction, transaction, rental)
                    
                    # 处理争议取消
                    elif base_custom_id == "dispute_rental_confirm_no":
                        # 编辑原始的二次确认消息，告知用户操作已取消，并移除按钮和嵌入
                        try:
                            await interaction.response.edit_message(
                                content="您已取消发起争议。如果您仍希望发起争议，请再次点击租赁频道中的【发起争议】按钮。",
                                embed=None,  # 清除嵌入内容
                                view=None    # 移除所有组件（按钮）
                            )
                            logger.info(f"用户 {interaction.user.id} 取消了租赁 {transaction_id} 的争议发起，并编辑了提示消息。")
                        except Exception as e:
                            logger.error(f"编辑租赁争议取消消息时出错: {e}")
                            # 如果编辑消息失败（可能因为交互已响应），尝试发送追踪消息
                            try:
                                # 检查交互是否已响应，避免重复响应错误
                                if not interaction.response.is_done():
                                    await interaction.response.send_message("处理取消发起争议时出错，请稍后重试或联系管理员。", ephemeral=True)
                                else:
                                    await interaction.followup.send("处理取消发起争议时出错，请稍后重试或联系管理员。", ephemeral=True)
                            except discord.errors.HTTPException as http_err:
                                logger.error(f"发送租赁争议取消追踪消息时HTTP错误: {http_err}")
                            except Exception as e2:
                                logger.error(f"发送租赁争议取消追踪消息时进一步出错: {e2}")
                    
                    elif base_custom_id == "confirm_return" and interaction.user.id == transaction["seller_id"]:
                        # 调用确认归还方法
                        await self.confirm_return(interaction, transaction, rental)
                    
                    elif base_custom_id == "confirm_return_ask" and interaction.user.id == transaction["seller_id"]:
                        # 显示二次确认对话框
                        # 创建二次确认的按钮
                        yes_button = discord.ui.Button(
                            style=discord.ButtonStyle.success,
                            label="是，我确认已收到归还物品",
                            custom_id=f"confirm_return_yes:{transaction['id']}"
                        )
                        
                        no_button = discord.ui.Button(
                            style=discord.ButtonStyle.danger,
                            label="否，我还没收到物品",
                            custom_id=f"confirm_return_no:{transaction['id']}"
                        )
                        
                        view = discord.ui.View()
                        view.add_item(yes_button)
                        view.add_item(no_button)
                        
                        # 创建二次确认嵌入消息
                        embed = discord.Embed(
                            title="⚠️ 确认归还二次确认",
                            description=f"您确认已收到租户归还的物品 '{transaction['item_name']}' 吗？",
                            color=discord.Color.gold()
                        )
                        
                        embed.add_field(
                            name="重要提示",
                            value="点击确认后，系统将开始释放保证金给租户并处理交易结算流程。请确保您已实际收到物品。",
                            inline=False
                        )
                        
                        await interaction.response.send_message(
                            "请再次确认您是否已收到租户归还的物品：",
                            embed=embed,
                            view=view,
                            ephemeral=True
                        )
                    
                    elif base_custom_id == "confirm_return_yes" and interaction.user.id == transaction["seller_id"]:
                        # 用户确认已收到物品，继续处理确认归还流程
                        await self.confirm_return(interaction, transaction, rental)
                    
                    elif base_custom_id == "confirm_return_no" and interaction.user.id == transaction["seller_id"]:
                        # 用户表示尚未收到物品
                        # 编辑原始的二次确认消息，告知用户操作已取消，并移除按钮和嵌入
                        try:
                            await interaction.response.edit_message(
                                content="您已选择尚未收到归还的物品。请在实际收到物品后再点击【确认收到归还】按钮。",
                                embed=None,  # 清除嵌入内容
                                view=None    # 移除所有组件（按钮）
                            )
                            logger.info(f"出租方 {interaction.user.id} 在租赁 {transaction_id} 中选择尚未收到归还物品，并编辑了提示消息。")
                        except Exception as e:
                            logger.error(f"编辑确认归还取消消息时出错: {e}")
                            # 如果编辑消息失败（可能因为交互已响应），尝试发送追踪消息
                            try:
                                # 检查交互是否已响应，避免重复响应错误
                                if not interaction.response.is_done():
                                    await interaction.response.send_message("处理确认归还取消时出错，请稍后重试或联系管理员。", ephemeral=True)
                                else:
                                    await interaction.followup.send("处理确认归还取消时出错，请稍后重试或联系管理员。", ephemeral=True)
                            except discord.errors.HTTPException as http_err:
                                logger.error(f"发送确认归还取消追踪消息时HTTP错误: {http_err}")
                            except Exception as e2:
                                logger.error(f"发送确认归还取消追踪消息时进一步出错: {e2}")
                    
                    elif base_custom_id == "cancel_rental":  # 添加对 cancel_rental 的处理
                        await self.cancel_rental(interaction, transaction)
                    
                    elif base_custom_id == "use_existing_address" and interaction.user.id == transaction["buyer_id"]:
                        # 使用现有地址确认收货
                        try:
                            parts = custom_id.split(":")
                            if len(parts) < 3:
                                await interaction.response.send_message("无效的请求。", ephemeral=True)
                                return
                            
                            transaction_id = int(parts[1])
                            renter_address = parts[2]
                            
                            # 获取交易和租赁信息
                            transaction = await database.get_transaction(transaction_id)
                            rental = await database.get_rental(transaction_id)
                            
                            if not transaction or not rental:
                                await interaction.response.send_message("找不到交易或租赁信息。", ephemeral=True)
                                return
                            
                            # 更新租户地址
                            await database.update_user_payment_address(
                                transaction["buyer_id"],
                                renter_address
                            )
                            
                            # 开始租赁计时
                            current_time = datetime.now(timezone.utc)
                            rental_period = rental["rental_period"]
                            rental_unit = rental["rental_unit"]
                            
                            # 计算结束时间
                            if rental_unit == "hours":
                                end_time = current_time + timedelta(hours=rental_period)
                            else:  # days
                                end_time = current_time + timedelta(days=rental_period)
                            
                            # 更新租赁开始和结束时间
                            await database.update_rental_dates(
                                transaction_id,
                                current_time.strftime("%Y-%m-%d %H:%M:%S"),
                                end_time.strftime("%Y-%m-%d %H:%M:%S")
                            )
                            
                            # 记录操作
                            await database.log_transaction_action(
                                transaction_id=transaction_id,
                                action="rental_started",
                                actor_id=interaction.user.id,
                                details=f"租户确认收货，租赁开始。租赁期: {rental_period} {rental_unit}"
                            )
                            
                            # 创建归还和争议按钮
                            return_button = discord.ui.Button(
                                style=discord.ButtonStyle.primary,
                                label="归还物品",
                                custom_id=f"return_item:{transaction_id}"
                            )
                            
                            dispute_button = discord.ui.Button(
                                style=discord.ButtonStyle.danger,
                                label="发起争议",
                                custom_id=f"dispute_rental:{transaction_id}"
                            )
                            
                            remind_button = discord.ui.Button(
                                style=discord.ButtonStyle.secondary,
                                label="提醒归还",
                                custom_id=f"remind_return:{transaction_id}"
                            )
                            
                            view = discord.ui.View()
                            view.add_item(return_button)
                            view.add_item(dispute_button)
                            
                            # 只给出租方添加提醒归还按钮
                            owner_view = discord.ui.View()
                            owner_view.add_item(remind_button)
                            
                            # 创建嵌入消息
                            embed = discord.Embed(
                                title="租赁已开始",
                                description=f"租赁 {transaction['item_name']} 的租赁已开始。",
                                color=discord.Color.green()
                            )
                            
                            embed.add_field(
                                name="租赁期",
                                value=f"{rental_period} {'小时' if rental_unit == 'hours' else '天'}",
                                inline=True
                            )
                            
                            embed.add_field(
                                name="开始时间",
                                value=f"<t:{int(current_time.timestamp())}:f>",
                                inline=True
                            )
                            
                            embed.add_field(
                                name="结束时间",
                                value=f"<t:{int(end_time.timestamp())}:f>",
                                inline=True
                            )
                            
                            # 为租户发送消息
                            await interaction.channel.send(
                                f"<@{transaction['buyer_id']}> 您已确认收到物品，租赁开始。请在租赁期结束后点击归还物品按钮。",
                                embed=embed,
                                view=view
                            )
                            
                            # 为出租方发送消息
                            await interaction.channel.send(
                                f"<@{transaction['seller_id']}> 租户已确认收到物品，租赁开始。若需提醒租户归还，请点击下方按钮。",
                                view=owner_view
                            )
                            
                            await interaction.response.send_message("您已确认收货，租赁开始。", ephemeral=True)
                            
                        except Exception as e:
                            logger.error(f"处理租户地址设置时出错: {e}", exc_info=True)
                            await interaction.response.send_message(
                                "处理请求时出错，请联系管理员。",
                                ephemeral=True
                            )
                    
                    elif base_custom_id == "use_existing_addresses":
                        try:
                            # 获取出租方和租户的现有地址
                            owner = await database.get_user(transaction["seller_id"])
                            renter = await database.get_user(transaction["buyer_id"])
                            
                            owner_existing_address = owner.get("payment_address") if owner else None
                            renter_existing_address = renter.get("payment_address") if renter else None
                            
                            if not owner_existing_address or not renter_existing_address:
                                await interaction.response.send_message("缺少必要的收款地址。", ephemeral=True)
                                return
                            
                            # 继续处理确认归还流程
                            await self.process_confirm_return(interaction, transaction, rental, owner_existing_address)
                            
                        except Exception as e:
                            logger.error(f"处理地址确认时出错: {e}", exc_info=True)
                            await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
                    
                    elif base_custom_id == "update_addresses" and interaction.user.id == transaction["seller_id"]:
                        # 创建并发送模态框
                        try:
                            # 创建模态框
                            modal = discord.ui.Modal(title="更新收款地址")
                            
                            # 创建地址输入字段
                            address_input = discord.ui.InputText(
                                label="出租方收款地址",
                                placeholder="请输入您的USDT-BEP20收款地址",
                                required=True,
                                min_length=42,
                                max_length=42
                            )
                            
                            # 添加输入字段到模态框
                            modal.add_item(address_input)
                            
                            # 定义提交回调
                            async def modal_callback(modal_interaction):
                                try:
                                    # 先延迟响应，防止超时
                                    try:
                                        if not modal_interaction.response.is_done():
                                            await modal_interaction.response.defer(ephemeral=True)
                                    except Exception as e:
                                        logger.warning(f"延迟响应时出错: {e}")
                                        # 如果已经响应过，使用followup
                                        pass
                                    
                                    # 提取地址值并验证
                                    address_value = helpers.sanitize_input(address_input.value.strip())
                                    
                                    # 基本格式验证
                                    if len(address_value) != 42 or not address_value.startswith("0x"):
                                        try:
                                            await modal_interaction.followup.send("❌ 地址格式无效，请确保是42位的BEP20地址，以0x开头", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址验证失败消息时出错: {e}")
                                        return
                                    
                                    # SQL注入防护
                                    suspicious_patterns = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'UNION', '--', ';', "'"]
                                    for pattern in suspicious_patterns:
                                        if pattern.upper() in address_value.upper():
                                            logger.warning(f"检测到可能的SQL注入尝试: 用户 {transaction['seller_id']} 的支付地址 '{address_value}'")
                                            try:
                                                await modal_interaction.followup.send("❌ 检测到无效字符，请提供有效的加密货币地址", ephemeral=True)
                                            except Exception as e:
                                                logger.warning(f"发送安全警告消息时出错: {e}")
                                            return
                                    
                                    # 确保地址只包含有效的十六进制字符（0x开头后面是40位十六进制）
                                    import re
                                    if not re.match(r'^0x[0-9a-fA-F]{40}$', address_value):
                                        logger.warning(f"用户 {transaction['seller_id']} 提供了格式正确但包含无效字符的地址: '{address_value}'")
                                        try:
                                            await modal_interaction.followup.send("❌ 地址包含无效字符，请提供有效的BEP20地址", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址格式警告消息时出错: {e}")
                                        return
                                    
                                    # 更新出租方的收款地址前记录日志
                                    logger.info(f"正在为用户 {transaction['seller_id']} 更新支付地址: {address_value}")
                                    
                                    # 更新出租方的收款地址
                                    update_success = await database.update_user_payment_address(
                                        transaction["seller_id"], 
                                        address_value
                                    )
                                    
                                    if not update_success:
                                        try:
                                            await modal_interaction.followup.send("❌ 更新地址失败，请稍后重试", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址更新失败消息时出错: {e}")
                                        return
                                    
                                    # 发送成功消息
                                    try:
                                        await modal_interaction.followup.send("✅ 地址设置成功！正在处理确认归还...", ephemeral=True)
                                    except Exception as e:
                                        logger.warning(f"发送跟进消息失败: {e}")
                                    
                                    # 重新获取最新的交易数据
                                    updated_transaction = await database.get_transaction(transaction["id"])
                                    if not updated_transaction:
                                        logger.error(f"无法获取更新后的交易 {transaction['id']}")
                                        updated_transaction = transaction
                                        
                                    # 重新获取最新的租赁数据    
                                    updated_rental = await database.get_rental(transaction["id"])
                                    if not updated_rental:
                                        logger.error(f"无法获取更新后的租赁 {transaction['id']}")
                                        updated_rental = rental
                                    
                                    # 继续处理确认归还流程
                                    await self.process_confirm_return(
                                        modal_interaction,
                                        updated_transaction,
                                        updated_rental,
                                        address_value
                                    )
                                except Exception as e:
                                    logger.error(f"处理地址更新表单提交时出错: {e}", exc_info=True)
                                    try:
                                        await modal_interaction.followup.send("❌ 处理租赁归还时出错，请联系管理员", ephemeral=True)
                                    except:
                                        logger.warning("无法发送错误消息")
                            
                            # 设置回调
                            modal.callback = modal_callback
                            
                            # 发送模态框
                            await interaction.response.send_modal(modal)
                        except Exception as e:
                            logger.error(f"创建地址更新模态框时出错: {e}", exc_info=True)
                            await interaction.response.send_message(
                                "创建地址更新表单时出错，请重试。",
                                ephemeral=True
                            )
                    
                    elif base_custom_id == "update_renter_address" and interaction.user.id == transaction["buyer_id"]:
                        # 创建并发送模态框
                        try:
                            # 创建模态框
                            modal = discord.ui.Modal(title="更新收款地址")
                            
                            # 创建地址输入字段
                            address_input = discord.ui.InputText(
                                label="租户收款地址",
                                placeholder="请输入您的USDT-BEP20收款地址",
                                required=True,
                                min_length=42,
                                max_length=42
                            )
                            
                            # 添加输入字段到模态框
                            modal.add_item(address_input)
                            
                            # 定义提交回调
                            async def modal_callback(modal_interaction):
                                try:
                                    # 先延迟响应，防止超时
                                    try:
                                        if not modal_interaction.response.is_done():
                                            await modal_interaction.response.defer(ephemeral=True)
                                    except Exception as e:
                                        logger.warning(f"延迟响应时出错: {e}")
                                        # 如果已经响应过，使用followup
                                        pass
                                    
                                    # 提取地址值并验证
                                    address_value = helpers.sanitize_input(address_input.value.strip())
                                    
                                    # 基本格式验证
                                    if len(address_value) != 42 or not address_value.startswith("0x"):
                                        try:
                                            await modal_interaction.followup.send("❌ 地址格式无效，请确保是42位的BEP20地址，以0x开头", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址验证失败消息时出错: {e}")
                                        return
                                    
                                    # SQL注入防护
                                    suspicious_patterns = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'UNION', '--', ';', "'"]
                                    for pattern in suspicious_patterns:
                                        if pattern.upper() in address_value.upper():
                                            logger.warning(f"检测到可能的SQL注入尝试: 用户 {transaction['buyer_id']} 的支付地址 '{address_value}'")
                                            try:
                                                await modal_interaction.followup.send("❌ 检测到无效字符，请提供有效的加密货币地址", ephemeral=True)
                                            except Exception as e:
                                                logger.warning(f"发送安全警告消息时出错: {e}")
                                            return
                                    
                                    # 确保地址只包含有效的十六进制字符（0x开头后面是40位十六进制）
                                    import re
                                    if not re.match(r'^0x[0-9a-fA-F]{40}$', address_value):
                                        logger.warning(f"用户 {transaction['buyer_id']} 提供了格式正确但包含无效字符的地址: '{address_value}'")
                                        try:
                                            await modal_interaction.followup.send("❌ 地址包含无效字符，请提供有效的BEP20地址", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址格式警告消息时出错: {e}")
                                        return
                                    
                                    # 更新租户的收款地址前记录日志
                                    logger.info(f"正在为用户 {transaction['buyer_id']} 更新支付地址: {address_value}")
                                    
                                    # 更新租户的收款地址
                                    update_success = await database.update_user_payment_address(
                                        transaction["buyer_id"], 
                                        address_value
                                    )
                                    
                                    if not update_success:
                                        try:
                                            await modal_interaction.followup.send("❌ 更新地址失败，请稍后重试", ephemeral=True)
                                        except Exception as e:
                                            logger.warning(f"发送地址更新失败消息时出错: {e}")
                                        return
                                    
                                    # 发送成功消息
                                    try:
                                        await modal_interaction.followup.send("✅ 收款地址设置成功！", ephemeral=True)
                                    except Exception as e:
                                        logger.warning(f"发送跟进消息失败: {e}")
                                
                                except Exception as e:
                                    logger.error(f"处理租户地址更新表单提交时出错: {e}", exc_info=True)
                                    try:
                                        await modal_interaction.followup.send("❌ 处理地址更新时出错，请联系管理员", ephemeral=True)
                                    except:
                                        logger.warning("无法发送错误消息")
                            
                            # 设置回调
                            modal.callback = modal_callback
                            
                            # 发送模态框
                            await interaction.response.send_modal(modal)
                        except Exception as e:
                            logger.error(f"创建地址更新模态框时出错: {e}", exc_info=True)
                            await interaction.response.send_message(
                                "创建地址更新表单时出错，请重试。",
                                ephemeral=True
                            )
                            
            except Exception as e:
                            logger.error(f"处理时出错: {e}")
                            await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
                            
            except Exception as e:
                logger.error(f"处理交互时出错: {e}", exc_info=True)
                try:
                    await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
                except:
                    pass
    
    async def confirm_rental(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """物主确认租赁请求。"""
        if transaction["status"] != "pending":
            await interaction.response.send_message("❌ 此租赁请求已不能确认。", ephemeral=True)
            return
        
        # 延迟响应以防交互超时
        await interaction.response.defer()
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "confirmed")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="confirm",
            actor_id=interaction.user.id,
            details="出租方确认了租赁请求"
        )
        
        # 创建支付按钮
        pay_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="支付",
            custom_id=f"pay_rental:{transaction['id']}"
        )
        
        cancel_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="取消租赁",
            custom_id=f"cancel_rental:{transaction['id']}"
        )
        
        view = discord.ui.View()
        view.add_item(pay_button)
        view.add_item(cancel_button)
        
        # 创建确认嵌入消息
        embed = discord.Embed(
            title="✅ 租赁请求已确认",
            description=f"出租方已确认租赁请求，请租户完成支付。",
            color=discord.Color.green()
        )
        
        # 检查用户是否有免费托管积分
        user_data = await database.get_user(transaction["buyer_id"])
        has_free_escrow = False
        free_escrow_amount = 0.0
        escrow_fee = transaction["escrow_fee"]
        actual_escrow_fee = escrow_fee
        
        if user_data and "free_escrow_amount" in user_data and user_data["free_escrow_amount"] > 0:
            free_escrow_amount = user_data["free_escrow_amount"]
            
            if free_escrow_amount >= escrow_fee:
                # 用户有足够的免费托管积分
                has_free_escrow = True
                actual_escrow_fee = 0.0
            elif free_escrow_amount > 0:
                # 部分减免
                actual_escrow_fee = escrow_fee - free_escrow_amount
        
        # 计算实际总支付金额，考虑积分
        total_amount = float(transaction['amount']) + float(rental['deposit']) + actual_escrow_fee
        
        embed.add_field(name="👤 租户", value=f"<@{transaction['buyer_id']}>", inline=True)
        embed.add_field(name="👑 出租方", value=interaction.user.mention, inline=True)
        embed.add_field(name="📦 物品", value=transaction["item_name"], inline=True)
        
        embed.add_field(name="💰 租金", value=f"{transaction['amount']} USDT", inline=True)
        embed.add_field(name="💵 保证金", value=f"{rental['deposit']} USDT", inline=True)
        
        period_display = f"{rental['rental_period']} 小时" if rental.get('rental_unit') == "hours" else f"{rental['rental_period']} 天"
        embed.add_field(name="⏱️ 租赁期限", value=period_display, inline=True)
        
        # 显示托管费用，考虑积分
        if has_free_escrow:
            embed.add_field(name="🏦 托管费用", value=f"{escrow_fee:.2f} USDT (使用积分)", inline=True)
        elif actual_escrow_fee < escrow_fee:
            embed.add_field(name="🏦 托管费用", value=f"{escrow_fee:.2f} USDT (使用 {free_escrow_amount:.2f} 积分后实付 {actual_escrow_fee:.2f})", inline=True)
        else:
            embed.add_field(name="🏦 托管费用", value=f"{escrow_fee:.2f} USDT (每天2U)", inline=True)
        
        embed.add_field(name="💰 总支付金额", value=f"{total_amount:.2f} USDT", inline=True)
        embed.add_field(name="📊 状态", value="已确认，等待支付", inline=True)
        
        # 添加重要提示
        embed.add_field(
            name="⚠️ 重要提示",
            value="• 点击支付按钮后将无法取消交易\n• 请确保支付金额准确无误\n• 支付完成后请等待系统确认",
            inline=False
        )
        
        await interaction.followup.send(
            f"<@{transaction['buyer_id']}> 您的租赁请求已被确认！请点击下方按钮完成支付。",
            embed=embed,
            view=view
        )
    
    async def reject_rental(self, interaction: discord.Interaction, transaction: Dict):
        """拒绝租赁请求。"""
        if transaction["status"] != "pending":
            await interaction.response.send_message("此租赁已不能拒绝。", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 记录操作
        is_seller = interaction.user.id == transaction["seller_id"]
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="reject",
            actor_id=interaction.user.id,
            details=f"{'物主' if is_seller else '租户'}拒绝了租赁"
        )
        
        # 清除双方的活跃租赁
        await database.decrement_active_rental(transaction["buyer_id"], transaction["id"])
        await database.decrement_active_rental(transaction["seller_id"], transaction["id"])
        
        # 获取用户对象
        renter = self.bot.get_user(transaction["buyer_id"])
        owner = self.bot.get_user(transaction["seller_id"])
        
        # 发送拒绝消息
        embed = discord.Embed(
            title="租赁已拒绝",
            description=f"**{transaction['item_name']}** 的租赁已被拒绝。",
            color=discord.Color.red()
        )
        
        rejecter = owner if is_seller else renter
        await interaction.followup.send(
            f"租赁已被 {rejecter.mention} 拒绝。此频道将在30秒后删除。",
            embed=embed
        )
        
        # 设置频道将在30秒后删除
        await asyncio.sleep(30)
        channel = interaction.channel
        if channel:
            try:
                await channel.delete(reason="租赁交易已完成，自动删除频道")
                logger.info(f"频道 {channel.name} (ID: {channel.id}) 已自动删除")
            except discord.errors.NotFound:
                logger.warning(f"尝试删除已不存在的频道: {channel.id}")
            except discord.errors.Forbidden:
                logger.warning(f"没有权限删除频道: {channel.id}")
            except Exception as e:
                logger.error(f"删除频道时出错: {e}")
    
    async def process_rental_payment(self, ctx, transaction, rental=None):
        """处理租赁支付。
        
        Args:
            ctx: 交互上下文或互动对象
            transaction: 交易对象或交易ID
            rental: 可选的租赁对象，如果未提供则会从数据库获取
        """
        # 如果传入的是交易ID，获取完整交易信息
        if isinstance(transaction, int):
            transaction_id = transaction
            transaction = await database.get_transaction(transaction_id)
            if not transaction:
                if isinstance(ctx, discord.Interaction):
                    # 只有在响应尚未完成时才延迟响应
                    if not ctx.response.is_done():
                        await ctx.response.defer(ephemeral=True)
                    await ctx.followup.send("找不到交易信息。", ephemeral=True)
                else:
                    await ctx.respond("找不到交易信息。", ephemeral=True)
                return
        else:
            transaction_id = transaction["id"]
        
        # 如果未提供租赁信息，从数据库获取
        if rental is None:
            rental = await database.get_rental(transaction_id)
            if not rental:
                if isinstance(ctx, discord.Interaction):
                    # 只有在响应尚未完成时才延迟响应
                    if not ctx.response.is_done():
                        await ctx.response.defer(ephemeral=True)
                    await ctx.followup.send("找不到租赁信息。", ephemeral=True)
                else:
                    await ctx.respond("找不到租赁信息。", ephemeral=True)
                return
        
        # 检查交易状态
        if transaction["status"] != "confirmed":
            if isinstance(ctx, discord.Interaction):
                 # 只有在响应尚未完成时才延迟响应
                if not ctx.response.is_done():
                    await ctx.response.defer(ephemeral=True)
                await ctx.followup.send("交易尚未确认，无法处理支付。", ephemeral=True)
            else:
                await ctx.respond("交易尚未确认，无法处理支付。", ephemeral=True)
            return
        
        # 检查用户是否为租赁方
        user_id = ctx.user.id if isinstance(ctx, discord.Interaction) else ctx.author.id
        if transaction["buyer_id"] != user_id:
            if isinstance(ctx, discord.Interaction):
                # 只有在响应尚未完成时才延迟响应
                if not ctx.response.is_done():
                    await ctx.response.defer(ephemeral=True)
                await ctx.followup.send("只有租赁方可以处理支付。", ephemeral=True)
            else:
                await ctx.respond("只有租赁方可以处理支付。", ephemeral=True)
            return
        
        # 如果是交互对象，延迟响应
        if isinstance(ctx, discord.Interaction):
            try:
                 # 只有在响应尚未完成时才延迟响应
                if not ctx.response.is_done():
                    await ctx.response.defer(ephemeral=True)
            except discord.errors.InteractionResponded:
                # 如果交互已经被响应，使用 followup
                pass
        
        # 计算需要支付的总金额
        # 总金额 = 租金 + 保证金 + 实际托管费用
        total_amount = float(rental["rental_fee"]) + float(rental["deposit"])
        
        # 获取出租方信息
        seller = self.bot.get_user(transaction["seller_id"])
        if not seller:
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send("无法获取出租方信息。", ephemeral=True)
            else:
                await ctx.respond("无法获取出租方信息。", ephemeral=True)
            return
        
        # 获取交易频道
        channel = self.bot.get_channel(transaction["channel_id"])
        if not channel:
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send("无法获取交易频道。", ephemeral=True)
            else:
                await ctx.respond("无法获取交易频道。", ephemeral=True)
            return
        
        # 检查用户是否有免费托管积分
        user = await database.get_user(user_id)
        has_free_escrow = False
        free_escrow_amount = 0.0
        escrow_fee = transaction["escrow_fee"]
        actual_escrow_fee = escrow_fee
        
        if user and "free_escrow_amount" in user and user["free_escrow_amount"] > 0:
            free_escrow_amount = user["free_escrow_amount"]
            
            if free_escrow_amount >= escrow_fee:
                # 用户有足够的免费托管积分
                has_free_escrow = True
                actual_escrow_fee = 0.0
            elif free_escrow_amount > 0:
                # 部分减免
                actual_escrow_fee = escrow_fee - free_escrow_amount
        
        # 更新总金额，加上实际托管费用
        total_amount += actual_escrow_fee
        
        # 生成支付地址
        try:
            # 获取存款地址
            payment_address = await binance_api.get_deposit_address("USDT")
            
            if not payment_address:
                if isinstance(ctx, discord.Interaction):
                    await ctx.followup.send("无法生成支付地址，请联系管理员。", ephemeral=True)
                else:
                    await ctx.respond("无法生成支付地址，请联系管理员。", ephemeral=True)
                return
            
            # 生成唯一金额
            unique_amount, formatted_amount = await payment_utils.generate_unique_amount(total_amount, transaction_id)
            
            # 更新交易支付地址和唯一金额
            await database.update_transaction_payment(transaction_id, payment_address, unique_amount=unique_amount)
            
            # 添加支付超时任务
            self.bot.loop.create_task(
                self.payment_timeout(transaction_id, 1800)  # 30分钟超时
            )
            
            # 计算超时时间戳
            timeout_timestamp = int((datetime.now(timezone.utc) + timedelta(seconds=1800)).timestamp())
            
            # 创建支付嵌入消息
            embed = discord.Embed(
                title="租赁支付",
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
            
            embed.add_field(
                name="⚠️ 支付超时",
                value=f"⚠️请勿在支付超时前1分钟内支付，否则可能将无法收到付款\n<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                inline=True
            )
            
            embed.add_field(
                name="⚠️ 重要提示",
                value="• 请支付**确切金额**以便系统自动识别您的付款\n• 支付后将无法取消交易\n• 请确保使用USDT-BEP20网络\n• 支付完成后请等待系统确认",
                inline=False
            )
            
            embed.add_field(
                name="租金",
                value=f"{rental['rental_fee']} USDT",
                inline=True
            )
            
            embed.add_field(
                name="保证金",
                value=f"{rental['deposit']} USDT",
                inline=True
            )
            
            if escrow_fee > 0:
                if has_free_escrow:
                    embed.add_field(
                        name="托管费用",
                        value=f"{escrow_fee:.2f} USDT (使用积分)",
                        inline=True
                    )
                elif actual_escrow_fee < escrow_fee:
                    embed.add_field(
                        name="托管费用",
                        value=f"{escrow_fee:.2f} USDT (使用 {free_escrow_amount:.2f} 积分后实付 {actual_escrow_fee:.2f})",
                        inline=True
                    )
                else:
                    embed.add_field(
                        name="托管费用",
                        value=f"{escrow_fee:.2f} USDT (每天2U)",
                        inline=True
                    )
            
            embed.add_field(
                name="租赁期限",
                value=f"{rental['rental_period']} {rental['rental_unit']}",
                inline=True
            )
            
            embed.add_field(
                name="注意事项",
                value="• 请确保使用USDT-BEP20网络\n"
                      "• 支付完成后，系统将自动检测并更新交易状态\n"
                      "• 必须支付确切金额，不要修改小数位\n"
                      "• 支付后将无法取消交易",
                inline=False
            )
            
            # 发送支付信息给租赁方(仅租赁方可见)
            user_mention = ctx.user.mention if isinstance(ctx, discord.Interaction) else ctx.author.mention
            
            # 在频道发送通知和支付信息（仅租赁方可见）
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send(f"请按照以下说明完成支付:", embed=embed, ephemeral=True)
                
                # 在频道发送公开通知，让其他人知道支付已开始
                notice_embed = discord.Embed(
                    title="租赁支付已启动",
                    description="支付信息已发送给租赁方（仅租赁方可见）。",
                    color=discord.Color.blue()
                )
                
                notice_embed.add_field(
                    name="⚠️ 支付超时",
                    value=f"<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)\n⚠️请勿在支付超时前1分钟内支付，否则可能将无法收到付款",
                    inline=True
                )
                
                await channel.send(f"{user_mention} 已收到支付详情，等待支付确认。", embed=notice_embed)
            else:
                # 如果不是交互上下文，尝试使用respond方法发送私密消息
                try:
                    await ctx.respond(f"请按照以下说明完成支付:", embed=embed, ephemeral=True)
                    
                    notice_embed = discord.Embed(
                        title="租赁支付已启动",
                        description="支付信息已发送给租赁方（仅租赁方可见）。",
                        color=discord.Color.blue()
                    )
                    
                    notice_embed.add_field(
                        name="⚠️ 支付超时",
                        value=f"<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                        inline=True
                    )
                    
                    await channel.send(f"{user_mention} 已收到支付详情，等待支付确认。", embed=notice_embed)
                except Exception as e:
                    # 如果ephemeral消息发送失败，尝试直接私信给用户
                    logger.warning(f"无法发送ephemeral消息: {e}，尝试直接发送私信")
                    user = ctx.author
                    await user.send(f"请按照以下说明完成支付:", embed=embed)
                    
                    notice_embed = discord.Embed(
                        title="租赁支付已启动",
                        description="支付信息已通过私信发送给租赁方。",
                        color=discord.Color.blue()
                    )
                    
                    notice_embed.add_field(
                        name="⚠️ 支付超时",
                        value=f"<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                        inline=True
                    )
                    
                    await channel.send(f"{user_mention} 请检查您的私信以获取支付详情。", embed=notice_embed)
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id,
                "payment_initiated",
                user_id,
                f"支付地址: {payment_address}, 金额: {formatted_amount} USDT"
            )
            
            # 通知出租方
            try:
                user_name = ctx.user.name if isinstance(ctx, discord.Interaction) else ctx.author.name
                seller_embed = discord.Embed(
                    title="租赁方已开始支付",
                    description=f"租赁方 {user_name} 已开始为物品 '{transaction['item_name']}' 支付。",
                    color=discord.Color.blue()
                )
                
                seller_embed.add_field(
                    name="交易金额",
                    value=f"{formatted_amount} USDT",
                    inline=True
                )
                
                seller_embed.add_field(
                    name="交易ID",
                    value=f"{transaction_id}",
                    inline=True
                )
                
                seller_embed.add_field(
                    name="⚠️ 支付超时",
                    value=f"<t:{timeout_timestamp}:R> (<t:{timeout_timestamp}:f>)",
                    inline=True
                )
                
                await seller.send(embed=seller_embed)
            except Exception as e:
                logger.error(f"无法向出租方发送通知: {e}")
            
            # 响应用户
            if isinstance(ctx, discord.Interaction):
                # 已经在上面发送了ephemeral消息，无需再次响应
                pass
            else:
                # 已经在上面响应了，无需再次响应
                pass
            
        except Exception as e:
            logger.error(f"处理支付时出错: {e}")
            if isinstance(ctx, discord.Interaction):
                await ctx.followup.send("处理支付时出错，请联系管理员。", ephemeral=True)
            else:
                await ctx.respond("处理支付时出错，请联系管理员。", ephemeral=True)
    
    async def start_rental(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """开始租赁并设置开始和结束日期。"""
        # 检查交易状态
        if transaction["status"] != "paid":
            await interaction.response.send_message("❌ 此租赁尚未支付，无法开始。", ephemeral=True)
            return
        
        # 检查用户是否为出租方
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("⚠️ 只有出租方可以开始租赁。", ephemeral=True)
            return
        
        # 检查是否已经开始了租赁
        if rental.get("start_date"):
            await interaction.response.send_message("❌ 此租赁已经开始，不能重复启动。", ephemeral=True)
            return
        
        # 获取当前时间
        now = datetime.now(timezone.utc)
        
        # 计算结束时间
        rental_period = rental["rental_period"]
        rental_unit = rental.get("rental_unit", "days")  # 默认为天
        
        if rental_unit == "hours":
            end_time = now + timedelta(hours=rental_period)
        else:
            end_time = now + timedelta(days=rental_period)
        
        # 更新租赁日期
        await database.update_rental_dates(
            transaction["id"],
            now.strftime("%Y-%m-%d %H:%M:%S"),
            end_time.strftime("%Y-%m-%d %H:%M:%S")
        )
        
        # 创建租赁开始嵌入消息
        embed = discord.Embed(
            title="🚀 租赁已开始",
            description=f"物品 '{transaction['item_name']}' 的租赁已开始。",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="🕒 开始时间",
            value=f"<t:{int(now.timestamp())}:F>",
            inline=True
        )
        
        embed.add_field(
            name="🔚 结束时间",
            value=f"<t:{int(end_time.timestamp())}:F>",
            inline=True
        )
        
        period_display = f"{rental_period} 小时" if rental_unit == "hours" else f"{rental_period} 天"
        embed.add_field(
            name="⏱️ 租赁期限",
            value=period_display,
            inline=True
        )
        
        # 添加提醒信息
        embed.add_field(
            name="⚠️ 提醒",
            value="在租赁期限剩余20%和10%时，系统将发送提醒。\n租赁结束时，请归还物品并通知出租方。",
            inline=False
        )
        
        # 创建归还按钮
        return_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="归还物品",
            custom_id=f"return_item:{transaction['id']}"
        )
        
        view = discord.ui.View()
        view.add_item(return_button)
        
        # 发送消息
        await interaction.response.send_message(embed=embed, view=view)
        
        # 记录操作
        await database.log_transaction_action(
            transaction["id"],
            "rental_started",
            interaction.user.id,
            f"租赁开始时间: {now.strftime('%Y-%m-%d %H:%M:%S')}, 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        # 设置租赁提醒
        total_seconds = (end_time - now).total_seconds()
        # 计算剩余20%和10%的时间点
        reminder_20_percent = total_seconds * 0.8  # 当剩余20%时提醒（即已过去80%时间）
        reminder_10_percent = total_seconds * 0.9  # 当剩余10%时提醒（即已过去90%时间）
        
        # 创建提醒任务
        self.rental_reminder_tasks[f"{transaction['id']}_20percent"] = self.bot.loop.create_task(
            self.send_rental_reminder(transaction["id"], interaction.channel, reminder_20_percent, 0.2)
        )
        
        self.rental_reminder_tasks[f"{transaction['id']}_10percent"] = self.bot.loop.create_task(
            self.send_rental_reminder(transaction["id"], interaction.channel, reminder_10_percent, 0.1)
        )
        
        # 通知租户
        renter = self.bot.get_user(transaction["buyer_id"])
        if renter:
            try:
                renter_embed = discord.Embed(
                    title="您的租赁已开始",
                    description=f"物品 '{transaction['item_name']}' 的租赁已开始。",
                    color=discord.Color.green()
                )
                
                renter_embed.add_field(
                    name="开始时间",
                    value=f"<t:{int(now.timestamp())}:F>",
                    inline=True
                )
                
                renter_embed.add_field(
                    name="结束时间",
                    value=f"<t:{int(end_time.timestamp())}:F>",
                    inline=True
                )
                
                renter_embed.add_field(
                    name="租赁期限",
                    value=period_display,
                    inline=True
                )
                
                await renter.send(embed=renter_embed)
            except Exception as e:
                logger.error(f"无法向租户发送通知: {e}")
    
    async def send_rental_reminder(self, transaction_id: int, channel: discord.TextChannel, delay: float, remaining_percent: float = None):
        """在租赁结束前发送提醒。"""
        try:
            # 等待指定的延迟时间
            await asyncio.sleep(delay)
            
            # 获取交易和租赁信息
            transaction = await database.get_transaction(transaction_id)
            rental = await database.get_rental(transaction_id)
            
            if not transaction or not rental:
                logger.error(f"无法获取交易或租赁信息，ID: {transaction_id}")
                return
            
            # 检查租赁是否已经结束或已归还
            if transaction["status"] == "completed" or rental["returned"]:
                return
            
            # 获取用户
            renter = self.bot.get_user(transaction["buyer_id"])
            owner = self.bot.get_user(transaction["seller_id"])
            
            if not renter or not owner:
                logger.error(f"无法获取租户或出租方信息，交易ID: {transaction_id}")
                return
            
            # 计算剩余时间
            end_date = datetime.strptime(str(rental["end_date"]), "%Y-%m-%d %H:%M:%S")
            end_date = end_date.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            
            # 如果已经过了结束时间，则发送过期通知
            if now > end_date:
                embed = discord.Embed(
                    title="⚠️ 租赁已过期",
                    description=f"物品 '{transaction['item_name']}' 的租赁已过期。请尽快归还物品。",
                    color=discord.Color.red()
                )
                
                embed.add_field(
                    name="👤 租户",
                    value=renter.mention,
                    inline=True
                )
                
                embed.add_field(
                    name="👑 出租方",
                    value=owner.mention,
                    inline=True
                )
                
                embed.add_field(
                    name="🔚 结束时间",
                    value=f"<t:{int(end_date.timestamp())}:F>",
                    inline=True
                )
                
                embed.add_field(
                    name="⏰ 过期时间",
                    value=f"{(now - end_date).total_seconds() / 3600:.1f} 小时",
                    inline=True
                )
                
                # 创建归还按钮
                return_button = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="归还物品",
                    custom_id=f"return_item:{transaction_id}"
                )
                
                dispute_button = discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="发起争议",
                    custom_id=f"dispute_rental:{transaction_id}"
                )
                
                view = discord.ui.View()
                view.add_item(return_button)
                view.add_item(dispute_button)
                
                await channel.send(f"{renter.mention} {owner.mention}", embed=embed, view=view)
            else:
                # 计算剩余时间
                remaining_time = end_date - now
                remaining_hours = remaining_time.total_seconds() / 3600
                
                # 根据剩余时间格式化显示
                if remaining_hours < 24:
                    time_display = f"{remaining_hours:.1f} 小时"
                else:
                    days = int(remaining_hours / 24)
                    hours = remaining_hours % 24
                    time_display = f"{days} 天 {hours:.1f} 小时"
                
                # 确定提示消息标题
                if remaining_percent:
                    percent_text = f"{int(remaining_percent * 100)}%"
                    title = f"⏳ 租赁剩余时间{percent_text}"
                else:
                    title = "⏳ 租赁即将到期"
                
                embed = discord.Embed(
                    title=title,
                    description=f"物品 '{transaction['item_name']}' 的租赁即将到期。",
                    color=discord.Color.gold()
                )
                
                embed.add_field(
                    name="👤 租户",
                    value=renter.mention,
                    inline=True
                )
                
                embed.add_field(
                    name="👑 出租方",
                    value=owner.mention,
                    inline=True
                )
                
                embed.add_field(
                    name="🔚 结束时间",
                    value=f"<t:{int(end_date.timestamp())}:F>",
                    inline=True
                )
                
                embed.add_field(
                    name="⏱️ 剩余时间",
                    value=time_display,
                    inline=True
                )
                
                # 创建归还按钮
                return_button = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="归还物品",
                    custom_id=f"return_item:{transaction_id}"
                )
                
                view = discord.ui.View()
                view.add_item(return_button)
                
                await channel.send(f"{renter.mention} {owner.mention}", embed=embed, view=view)
                
        except asyncio.CancelledError:
            # 任务被取消
            pass
        except Exception as e:
            logger.error(f"发送租赁提醒时出错: {e}")
    
    async def return_item(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """租户归还物品。"""
        # 检查交易状态
        if transaction["status"] not in ["paid", "shipped"]:
            await interaction.response.send_message("❌ 此租赁当前无法归还物品。", ephemeral=True)
            return
        
        # 检查用户是否为租户
        if interaction.user.id != transaction["buyer_id"]:
            await interaction.response.send_message("⚠️ 只有租户可以归还物品。", ephemeral=True)
            return
        
        # 更新交易状态为正在归还
        await database.update_transaction_status(transaction["id"], "returning")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="return_requested",
            actor_id=interaction.user.id,
            details="租户请求归还物品"
        )
        
        # 创建确认按钮
        confirm_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="确认收到归还",
            custom_id=f"confirm_return_ask:{transaction['id']}"
        )
        
        dispute_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="发起争议",
            custom_id=f"dispute_rental:{transaction['id']}"
        )
        
        view = discord.ui.View()
        view.add_item(confirm_button)
        view.add_item(dispute_button)
        
        # 创建归还嵌入消息
        embed = discord.Embed(
            title="📦 物品归还请求",
            description=f"租户 {interaction.user.mention} 请求归还物品 '{transaction['item_name']}'。",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="👤 租户",
            value=interaction.user.mention,
            inline=True
        )
        
        embed.add_field(
            name="👑 出租方",
            value=f"<@{transaction['seller_id']}>",
            inline=True
        )
        
        try:
            # 获取租赁开始和结束时间
            start_date = rental.get("start_date")
            end_date = rental.get("end_date")
            
            if start_date and end_date:
                # 确保日期是 datetime 对象并添加时区信息
                if isinstance(start_date, str):
                    start_date = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
                if isinstance(end_date, str):
                    end_date = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                
                # 添加时区信息
                start_date = start_date.replace(tzinfo=timezone.utc)
                end_date = end_date.replace(tzinfo=timezone.utc)
                
                embed.add_field(
                    name="🕒 开始时间",
                    value=f"<t:{int(start_date.timestamp())}:F>",
                    inline=True
                )
                
                embed.add_field(
                    name="🔚 结束时间",
                    value=f"<t:{int(end_date.timestamp())}:F>",
                    inline=True
                )
                
                # 计算是否提前归还
                now = datetime.now(timezone.utc)
                if now < end_date:
                    early_return = end_date - now
                    hours = early_return.total_seconds() / 3600
                    
                    if hours > 24:
                        days = int(hours / 24)
                        remaining_hours = hours % 24
                        early_return_text = f"提前 {days} 天 {remaining_hours:.1f} 小时"
                    else:
                        early_return_text = f"提前 {hours:.1f} 小时"
                    
                    embed.add_field(
                        name="⏱️ 提前归还",
                        value=early_return_text,
                        inline=True
                    )
            
            # 添加说明
            embed.add_field(
                name="📝 说明",
                value="出租方确认收到归还后，押金将被释放给租户。",
                inline=False
            )
            
            await interaction.response.send_message(
                f"<@{transaction['seller_id']}> 租户请求归还物品，请确认是否收到。",
                embed=embed,
                view=view
            )
            
            # 通知出租方
            try:
                owner = self.bot.get_user(transaction["seller_id"])
                if owner:
                    owner_embed = discord.Embed(
                        title="租户请求归还物品",
                        description=f"租户请求归还物品 '{transaction['item_name']}'。",
                        color=discord.Color.blue()
                    )
                    
                    owner_embed.add_field(
                        name="📦 物品",
                        value=transaction["item_name"],
                        inline=True
                    )
                    
                    owner_embed.add_field(
                        name="👤 租户",
                        value=f"<@{transaction['buyer_id']}>",
                        inline=True
                    )
                    
                    owner_embed.add_field(
                        name="📝 提示",
                        value="请前往交易频道确认是否收到归还物品。",
                        inline=False
                    )
                    
                    await owner.send(embed=owner_embed)
            except Exception as e:
                logger.error(f"无法向出租方发送通知: {e}")
                
        except ValueError as e:
            logger.error(f"处理租赁时间时出错: {e}")
            await interaction.response.send_message("处理租赁时间时出错，请联系管理员。", ephemeral=True)
        except Exception as e:
            logger.error(f"处理归还请求时出错: {e}")
            await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
    
    async def confirm_return(self, interaction: discord.Interaction, transaction: Dict, rental: Dict, owner_address: str = None):
        """确认租赁归还并释放保证金和租金。"""
        try:
            print(transaction["status"])
            # 检查交易状态
            if transaction["status"] not in ["returning", "confirm_return"]:
                await interaction.response.send_message("此交易当前无法确认归还。", ephemeral=True)
                return
            
                        # 更新交易状态为正在确认归还
            await database.update_transaction_status(transaction["id"], "confirm_return")
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="confirm_return_requested",
                actor_id=interaction.user.id,
                details="出租方确认归还物品"
            ) 
                
            
            # 如果没有提供出租方地址，检查用户是否有现有地址并询问是否使用
            if not owner_address:
                # 获取出租方的现有地址
                owner = await database.get_user(transaction["seller_id"])
                owner_existing_address = owner.get("payment_address") if owner else None
                
                # 如果出租方有现有地址，询问是否使用现有地址
                if owner_existing_address:
                    # 创建确认按钮
                    confirm_button = discord.ui.Button(
                        style=discord.ButtonStyle.success,
                        label="使用现有地址",
                        custom_id=f"use_existing_addresses:{transaction['id']}"
                    )
                    
                    update_button = discord.ui.Button(
                        style=discord.ButtonStyle.primary,
                        label="更新地址",
                        custom_id=f"update_addresses:{transaction['id']}"
                    )
                    
                    view = discord.ui.View()
                    view.add_item(confirm_button)
                    view.add_item(update_button)
                    
                    # 创建嵌入消息显示现有地址
                    embed = discord.Embed(
                        title="确认收款地址",
                        description="您已有收款地址，是否使用现有地址？",
                        color=discord.Color.blue()
                    )
                    
                    embed.add_field(
                        name="您的收款地址",
                        value=f"```{owner_existing_address}```",
                        inline=False
                    )
                    
                    embed.add_field(
                        name="当前步骤",
                        value="1️⃣ 确认收款地址",
                        inline=False
                    )
                    
                    embed.add_field(
                        name="下一步",
                        value="选择使用现有地址或更新地址后，系统将开始处理归还流程。",
                        inline=False
                    )
                    
                    await interaction.response.send_message(
                        "请确认是否使用现有收款地址，或者更新收款地址。",
                        embed=embed,
                        view=view,
                        ephemeral=True
                    )
                    return
                
                # 创建模态框
                modal = discord.ui.Modal(title="租赁归还")
                
                # 创建地址输入字段
                address_input = discord.ui.InputText(
                    label="出租方收款地址",
                    placeholder="请输入您的USDT-BEP20收款地址",
                    required=True,
                    min_length=42,
                    max_length=42
                )
                
                # 添加输入字段到模态框
                modal.add_item(address_input)
                
                # 定义提交回调
                async def modal_callback(modal_interaction):
                    try:
                        # 先延迟响应，防止超时
                        try:
                            if not modal_interaction.response.is_done():
                                await modal_interaction.response.defer(ephemeral=True)
                        except Exception as e:
                            logger.warning(f"延迟响应时出错: {e}")
                            # 如果已经响应过，使用followup
                            pass
                        
                        # 提取地址值并验证
                        address_value = helpers.sanitize_input(address_input.value.strip())
                        if len(address_value) != 42 or not address_value.startswith("0x"):
                            try:
                                await modal_interaction.followup.send("❌ 地址格式无效，请确保是42位的BEP20地址，以0x开头", ephemeral=True)
                            except Exception as e:
                                logger.warning(f"发送地址验证失败消息时出错: {e}")
                            return
                        
                        # 更新出租方的收款地址前记录日志
                        logger.info(f"正在为用户 {transaction['seller_id']} 更新支付地址: {address_value}")
                        
                        # 更新出租方的收款地址
                        update_success = await database.update_user_payment_address(
                            transaction["seller_id"], 
                            address_value
                        )
                        
                        if not update_success:
                            try:
                                await modal_interaction.followup.send("❌ 更新地址失败，请稍后重试", ephemeral=True)
                            except Exception as e:
                                logger.warning(f"发送地址更新失败消息时出错: {e}")
                            return
                        
                        # 发送成功消息
                        try:
                            await modal_interaction.followup.send("✅ 地址设置成功！正在处理确认归还...", ephemeral=True)
                        except Exception as e:
                            logger.warning(f"发送跟进消息失败: {e}")
                        
                        # 重新获取最新的交易数据
                        updated_transaction = await database.get_transaction(transaction["id"])
                        if not updated_transaction:
                            logger.error(f"无法获取更新后的交易 {transaction['id']}")
                            updated_transaction = transaction
                            
                        # 重新获取最新的租赁数据    
                        updated_rental = await database.get_rental(transaction["id"])
                        if not updated_rental:
                            logger.error(f"无法获取更新后的租赁 {transaction['id']}")
                            updated_rental = rental
              
                        # 继续处理确认归还流程
                        await self.process_confirm_return(
                            modal_interaction,
                            updated_transaction,
                            updated_rental,
                            address_value
                        )
                    except Exception as e:
                        logger.error(f"处理租赁归还表单提交时出错: {e}", exc_info=True)
                        try:
                            await modal_interaction.followup.send("❌ 处理租赁归还时出错，请联系管理员", ephemeral=True)
                        except:
                            logger.warning("无法发送错误消息")
                
                # 设置回调
                modal.callback = modal_callback
                
                # 显示模态框
                await interaction.response.send_modal(modal)
                return
           
            # 继续处理确认归还流程
            await self.process_confirm_return(interaction, transaction, rental, owner_address)
            
        except Exception as e:
            logger.error(f"处理租赁归还时出错: {e}")
            try:
                await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
            except:
                pass
    
    async def process_confirm_return(self, interaction: discord.Interaction, transaction: Dict, rental: Dict, owner_address: str):
        """处理确认归还后的流程"""
        try:
            # 延迟响应以防止交互超时
            try:
                # 检查交互状态
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception as e:
                logger.warning(f"延迟响应时出错: {e}")
                # 如果已经响应过，使用followup
                pass
            
            # 检查交易是否已经完成
            if transaction["status"] == "completed":
                await interaction.followup.send("此交易已经完成，无法重复释放资金。", ephemeral=True)
                return
            
            # 检查交易状态是否为确认归还状态
            if transaction["status"] != "confirm_return":
                await interaction.followup.send("交易状态不正确，无法处理确认归还流程。", ephemeral=True)
                return
            
            # 创建进度条嵌入消息
            progress_embed = discord.Embed(
                title="处理归还流程",
                description="正在处理归还流程...",
                color=discord.Color.blue()
            )
            
            # 添加进度条
            progress_embed.add_field(
                name="当前步骤",
                value="1️⃣ 检查租户地址",
                inline=False
            )
            
            # 发送初始进度消息
            progress_message = None
            try:
                progress_message = await interaction.followup.send(embed=progress_embed, ephemeral=True)
            except Exception as e:
                logger.warning(f"发送进度消息时出错: {e}")
            
            # 获取租户地址
            renter = await database.get_user(transaction["buyer_id"])
            renter_address = renter.get("payment_address") if renter else None
            
            if not renter_address:
                await progress_message.edit(embed=discord.Embed(
                    title="处理失败",
                    description="租户未设置收款地址，无法释放保证金。",
                    color=discord.Color.red()
                ))
                return
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="2️⃣ 准备释放保证金",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 获取保证金和租金金额
            deposit = float(rental.get("deposit", 0))
            rental_fee = float(rental.get("rental_fee", 0))
            
            # 记录开始释放资金
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="start_release_funds",
                actor_id=interaction.user.id,
                details=f"开始释放资金 - 保证金: {deposit} USDT, 租金: {rental_fee} USDT"
            )
            
            # 如果有保证金，则释放给租户
            if deposit > 0:
                # 更新进度
                if progress_message:
                    progress_embed.add_field(
                        name="当前步骤",
                        value="3️⃣ 释放保证金",
                        inline=False
                    )
                    try:
                        await progress_message.edit(embed=progress_embed)
                    except Exception as e:
                        logger.warning(f"更新进度消息时出错: {e}")
                
                # 释放保证金给租户，使用不同的交易ID
                deposit_success, deposit_withdrawal_id, error_deposit = await binance_api.release_escrow_payment(
                    f"{transaction['id']}_deposit",  # 使用不同的交易ID
                    renter_address,
                    deposit
                )
                
                if not deposit_success:
                    # 记录释放资金失败
                    await database.log_transaction_action(
                        transaction_id=transaction["id"],
                        action="release_deposit_failed",
                        actor_id=interaction.user.id,
                        details=f"释放保证金失败: {error_deposit}"
                    )
                    
                    # 恢复交易状态
                    await database.update_transaction_status(transaction["id"], "paid")
                    await progress_message.edit(embed=discord.Embed(
                        title="处理失败",
                        description=f"释放保证金时出错：{error_deposit}",
                        color=discord.Color.red()
                    ))
                    return
            else:
                # 如果没有保证金，标记为成功
                deposit_success = True
            
            # 如果有租金，则释放给出租方
            if rental_fee > 0:
                # 更新进度
                if progress_message:
                    progress_embed.add_field(
                        name="当前步骤",
                        value="4️⃣ 释放租金",
                        inline=False
                    )
                    try:
                        await progress_message.edit(embed=progress_embed)
                    except Exception as e:
                        logger.warning(f"更新进度消息时出错: {e}")
                
                # 释放租金给出租方，使用不同的交易ID
                rental_success, rental_withdrawal_id, error_rental = await binance_api.release_escrow_payment(
                    f"{transaction['id']}_rental",  # 使用不同的交易ID
                    owner_address,
                    rental_fee
                )
                
                if not rental_success:
                    # 记录释放租金失败
                    await database.log_transaction_action(
                        transaction_id=transaction["id"],
                        action="release_rental_failed",
                        actor_id=interaction.user.id,
                        details=f"释放租金失败: {error_rental}"
                    )
                    
                    await progress_message.edit(embed=discord.Embed(
                        title="处理失败",
                        description=f"释放租金时出错：{error_rental}",
                        color=discord.Color.red()
                    ))
                    # 即使租金释放失败，如果保证金已释放，我们继续完成交易
                else:
                    # 记录租金释放成功
                    await database.log_transaction_action(
                        transaction_id=transaction["id"],
                        action="release_rental_success",
                        actor_id=interaction.user.id,
                        details=f"租金已释放，金额: {rental_fee} USDT，提现ID: {rental_withdrawal_id}"
                    )
                    rental_success = True
            else:
                # 如果没有租金，标记为成功
                rental_success = True
            
            # 如果保证金和租金都处理成功（或不需处理），则完成交易
            if deposit_success and rental_success:
                # 更新进度
                if progress_message:
                    progress_embed.add_field(
                        name="当前步骤",
                        value="5️⃣ 完成交易",
                        inline=False
                    )
                    try:
                        await progress_message.edit(embed=progress_embed)
                    except Exception as e:
                        logger.warning(f"更新进度消息时出错: {e}")
                
                # 更新交易状态为已完成
                await database.update_transaction_status(transaction["id"], "completed")
                
                # 清除双方的活跃租赁
                await database.decrement_active_rental(transaction["buyer_id"], transaction["id"])
                await database.decrement_active_rental(transaction["seller_id"], transaction["id"])
                
                # 记录交易完成
                await database.log_transaction_action(
                    transaction_id=transaction["id"],
                    action="rental_completed",
                    actor_id=interaction.user.id,
                    details="租赁交易已完成"
                )
                
                # 创建完成消息
                embed = discord.Embed(
                    title="租赁交易已完成",
                    description=f"租赁 {transaction['item_name']} 的交易已完成。",
                    color=discord.Color.green()
                )
                
                # 添加保证金信息（如果有）
                if deposit > 0:
                    embed.add_field(
                        name="保证金",
                        value=f"{deposit} USDT (已退还)",
                        inline=True
                    )
                
                # 添加租金信息（如果有）
                if rental_fee > 0:
                    embed.add_field(
                        name="租金",
                        value=f"{rental_fee} USDT (已支付)",
                        inline=True
                    )
                
                # 发送完成消息
                await interaction.channel.send(
                    f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}> 租赁交易已完成！",
                    embed=embed
                )
                
                # 发送最终确认消息
                await progress_message.edit(embed=discord.Embed(
                    title="处理完成",
                    description="租赁交易已完成。",
                    color=discord.Color.green()
                ))
                
                # 安排30秒后删除频道
                await self.schedule_channel_deletion(interaction.channel, 300, transaction["id"])
                
        except Exception as e:
            logger.error(f"处理租赁归还时出错: {e}")
            try:
                await interaction.followup.send("处理请求时出错，请联系管理员。", ephemeral=True)
            except:
                pass
    
    async def confirm_receipt(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """确认收到物品并开始租赁。"""
        # 检查交易状态
        if transaction["status"] not in ["paid", "shipped"]:
            await interaction.response.send_message("此交易当前无法确认收货。", ephemeral=True)
            return
        
        # 检查用户是否为租户
        if interaction.user.id != transaction["buyer_id"]:
            await interaction.response.send_message("只有租户可以确认收货。", ephemeral=True)
            return
        
        # 检查租户是否已设置收款地址
        renter = await database.get_user(transaction["buyer_id"])
        renter_address = renter.get("payment_address") if renter else None
        
        # 如果租户还没有设置地址，需要先设置
        if not renter_address:
            try:
                # 创建简单的模态框
                modal = discord.ui.Modal(title="设置收款地址")
                
                # 创建地址输入字段
                address_input = discord.ui.InputText(
                    label="请输入您的收款地址",
                    placeholder="请输入您的USDT-BEP20收款地址",
                    required=True,
                    min_length=42,
                    max_length=42
                )
                
                # 添加输入字段到模态框
                modal.add_item(address_input)
                
                # 定义提交回调
                async def modal_callback(modal_interaction):
                    try:
                        # 先延迟响应，防止超时
                        try:
                            if not modal_interaction.response.is_done():
                                await modal_interaction.response.defer(ephemeral=True)
                        except Exception as e:
                            logger.warning(f"延迟响应时出错: {e}")
                            # 如果已经响应过，使用followup
                            pass
                        
                        # 提取地址值并验证
                        address_value = helpers.sanitize_input(address_input.value.strip())
                        if len(address_value) != 42 or not address_value.startswith("0x"):
                            try:
                                await modal_interaction.followup.send("❌ 地址格式无效，请确保是42位的BEP20地址，以0x开头", ephemeral=True)
                            except Exception as e:
                                logger.warning(f"发送地址验证失败消息时出错: {e}")
                            return
                        
                        # 更新地址到数据库前记录日志
                        logger.info(f"正在为用户 {transaction['buyer_id']} 更新支付地址: {address_value}")
                        
                        # 更新地址到数据库
                        update_success = await database.update_user_payment_address(
                            transaction["buyer_id"],
                            address_value
                        )
                        
                        if not update_success:
                            try:
                                await modal_interaction.followup.send("❌ 更新地址失败，请稍后重试", ephemeral=True)
                            except Exception as e:
                                logger.warning(f"发送地址更新失败消息时出错: {e}")
                            return
                        
                        # 发送成功消息
                        try:
                            await modal_interaction.followup.send("✅ 地址设置成功！正在处理确认收货...", ephemeral=True)
                        except Exception as e:
                            logger.warning(f"发送跟进消息失败: {e}")
                        
                        # 重新获取最新的交易数据
                        updated_transaction = await database.get_transaction(transaction["id"])
                        if not updated_transaction:
                            logger.error(f"无法获取更新后的交易 {transaction['id']}")
                            updated_transaction = transaction
                            
                        # 重新获取最新的租赁数据    
                        updated_rental = await database.get_rental(transaction["id"])
                        if not updated_rental:
                            logger.error(f"无法获取更新后的租赁 {transaction['id']}")
                            updated_rental = rental
                        
                        # 触发后续流程
                        await self.process_confirm_receipt(modal_interaction, updated_transaction, updated_rental)
                    except Exception as e:
                        logger.error(f"地址设置失败: {e}", exc_info=True)
                        try:
                            await modal_interaction.followup.send("❌ 系统错误，请联系管理员", ephemeral=True)
                        except:
                            logger.warning("无法发送错误消息")
                
                # 设置回调
                modal.callback = modal_callback
                
                # 发送模态框
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"发送地址设置模态框时出错: {e}", exc_info=True)
                await interaction.response.send_message("❌ 系统错误，请联系管理员", ephemeral=True)
        else:
            # 已有地址时显示确认按钮
            class ConfirmView(discord.ui.View):
                def __init__(self, cog, transaction, rental):
                    super().__init__(timeout=300)
                    self.cog = cog
                    self.transaction = transaction
                    self.rental = rental

                @discord.ui.button(label="使用现有地址", style=discord.ButtonStyle.green)
                async def confirm_callback(self, button, interaction):
                    await interaction.response.defer(ephemeral=True)
                    # 直接触发后续流程
                    await self.cog.process_confirm_receipt(interaction, self.transaction, self.rental)

                @discord.ui.button(label="修改地址", style=discord.ButtonStyle.gray)
                async def update_callback(self, button, interaction):
                    # 创建简单的模态框
                    modal = discord.ui.Modal(title="设置收款地址")
                    
                    # 创建地址输入字段
                    address_input = discord.ui.InputText(
                        label="请输入您的收款地址",
                        placeholder="请输入您的USDT-BEP20收款地址",
                        required=True,
                        min_length=42,
                        max_length=42
                    )
                    
                    # 添加输入字段到模态框
                    modal.add_item(address_input)
                    
                    # 定义提交回调
                    async def modal_callback(modal_interaction):
                        try:
                            # 先延迟响应，防止超时
                            try:
                                if not modal_interaction.response.is_done():
                                    await modal_interaction.response.defer(ephemeral=True)
                            except Exception as e:
                                logger.warning(f"延迟响应时出错: {e}")
                                # 如果已经响应过，使用followup
                                pass
                            
                            # 提取地址值并验证
                            address_value = helpers.sanitize_input(address_input.value.strip())
                            if len(address_value) != 42 or not address_value.startswith("0x"):
                                try:
                                    await modal_interaction.followup.send("❌ 地址格式无效，请确保是42位的BEP20地址，以0x开头", ephemeral=True)
                                except Exception as e:
                                    logger.warning(f"发送地址验证失败消息时出错: {e}")
                                return
                            
                            # 更新地址到数据库前记录日志
                            logger.info(f"正在为用户 {self.transaction['buyer_id']} 更新支付地址: {address_value}")
                            
                            # 更新地址到数据库
                            update_success = await database.update_user_payment_address(
                                self.transaction["buyer_id"],
                                address_value
                            )
                            
                            if not update_success:
                                try:
                                    await modal_interaction.followup.send("❌ 更新地址失败，请稍后重试", ephemeral=True)
                                except Exception as e:
                                    logger.warning(f"发送地址更新失败消息时出错: {e}")
                                return
                            
                            # 发送成功消息
                            try:
                                await modal_interaction.followup.send("✅ 地址设置成功！正在处理确认收货...", ephemeral=True)
                            except Exception as e:
                                logger.warning(f"发送跟进消息失败: {e}")
                            
                            # 重新获取最新的交易数据
                            updated_transaction = await database.get_transaction(self.transaction["id"])
                            if not updated_transaction:
                                logger.error(f"无法获取更新后的交易 {self.transaction['id']}")
                                updated_transaction = self.transaction
                                
                            # 重新获取最新的租赁数据    
                            updated_rental = await database.get_rental(self.transaction["id"])
                            if not updated_rental:
                                logger.error(f"无法获取更新后的租赁 {self.transaction['id']}")
                                updated_rental = self.rental
                            
                            # 触发后续流程
                            await self.cog.process_confirm_receipt(modal_interaction, updated_transaction, updated_rental)
                        except Exception as e:
                            logger.error(f"地址设置失败: {e}", exc_info=True)
                            try:
                                await modal_interaction.followup.send("❌ 系统错误，请联系管理员", ephemeral=True)
                            except:
                                logger.warning("无法发送错误消息")
                    
                    # 设置回调
                    modal.callback = modal_callback
                    
                    # 发送模态框
                    await interaction.response.send_modal(modal)
            
            # 构建确认消息
            embed = discord.Embed(
                title="确认收款地址",
                description=f"当前地址：```{renter_address}```",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(
                "请确认或修改您的收款地址：",
                embed=embed,
                view=ConfirmView(self, transaction, rental),
                ephemeral=True
            )

    async def process_confirm_receipt(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """处理确认收货后的流程"""
        try:
            # 创建进度嵌入消息
            progress_embed = discord.Embed(
                title="处理确认收货流程",
                description="正在处理确认收货流程...",
                color=discord.Color.blue()
            )
            
            # 添加进度条
            progress_embed.add_field(
                name="当前步骤",
                value="1️⃣ 开始处理确认收货",
                inline=False
            )
            
            # 发送初始进度消息
            progress_message = None
            try:
                # 检查交互状态
                if interaction.response.is_done():
                    # 如果已经响应过，使用followup
                    progress_message = await interaction.followup.send(embed=progress_embed, ephemeral=True)
                else:
                    # 如果尚未响应，先延迟响应
                    await interaction.response.defer(ephemeral=True)
                    progress_message = await interaction.followup.send(embed=progress_embed, ephemeral=True)
            except Exception as e:
                logger.warning(f"发送进度消息时出错: {e}")
                # 如果无法发送进度消息，继续处理但不更新进度
            
            # 更新交易状态为已开始，设置开始和结束时间
            current_time = datetime.now(timezone.utc)
            rental_period = rental["rental_period"]
            rental_unit = rental["rental_unit"]
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="2️⃣ 计算租赁时间",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 计算结束时间
            if rental_unit == "hours":
                end_time = current_time + timedelta(hours=rental_period)
            else:  # days
                end_time = current_time + timedelta(days=rental_period)
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="3️⃣ 更新租赁信息",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 更新租赁开始和结束时间
            await database.update_rental_dates(
                transaction["id"],
                current_time.strftime("%Y-%m-%d %H:%M:%S"),
                end_time.strftime("%Y-%m-%d %H:%M:%S")
            )
                
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="rental_started",
                actor_id=interaction.user.id,
                details=f"租户确认收货，租赁开始。租赁期: {rental_period} {rental_unit}"
            )
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="4️⃣ 创建操作按钮",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 创建归还和争议按钮
            return_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="归还物品",
                custom_id=f"return_item:{transaction['id']}"
            )
            
            dispute_button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="发起争议",
                custom_id=f"dispute_rental:{transaction['id']}"
            )
            
            remind_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="提醒归还",
                custom_id=f"remind_return:{transaction['id']}"
            )
            
            view = discord.ui.View()
            view.add_item(return_button)
            view.add_item(dispute_button)
            
            # 只给出租方添加提醒归还按钮
            owner_view = discord.ui.View()
            owner_view.add_item(remind_button)
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="5️⃣ 发送确认消息",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 创建嵌入消息
            embed = discord.Embed(
                title="租赁已开始",
                description=f"租赁 {transaction['item_name']} 已开始。",
                color=discord.Color.green()
            )
                
            embed.add_field(
                name="租赁期",
                value=f"{rental_period} {'小时' if rental_unit == 'hours' else '天'}",
                inline=True
            )
            
            embed.add_field(
                name="开始时间",
                value=f"<t:{int(current_time.timestamp())}:F>",
                inline=True
            )
            
            embed.add_field(
                name="结束时间",
                value=f"<t:{int(end_time.timestamp())}:F>",
                inline=True
            )
            
            # 为租户发送消息
            await interaction.channel.send(
                f"<@{transaction['buyer_id']}> 您已确认收到物品，租赁开始。请在租赁期结束后点击归还物品按钮。",
                embed=embed,
                view=view
            )
            
            # 为出租方发送消息
            await interaction.channel.send(
                f"<@{transaction['seller_id']}> 租户已确认收到物品，租赁开始。若需提醒租户归还，请点击下方按钮。",
                view=owner_view
            )
            
            # 更新进度
            if progress_message:
                progress_embed.add_field(
                    name="当前步骤",
                    value="✅ 处理完成",
                    inline=False
                )
                try:
                    await progress_message.edit(embed=progress_embed)
                except Exception as e:
                    logger.warning(f"更新进度消息时出错: {e}")
            
            # 发送确认消息
            try:
                await interaction.followup.send("您已确认收货，租赁开始。", ephemeral=True)
            except Exception as e:
                logger.warning(f"发送确认消息时出错: {e}")
        except Exception as e:
            logger.error(f"处理确认收货时出错: {e}", exc_info=True)
            try:
                await interaction.followup.send("处理请求时出错，请联系管理员。", ephemeral=True)
            except:
                pass
    
    async def schedule_channel_deletion(self, channel, delay_seconds, transaction_ids=None):
        """安排在指定延迟后删除频道。"""
        try:
            # 创建带有取消按钮的视图
            class CancelDeletionView(discord.ui.View):
                def __init__(self, cog):
                    super().__init__(timeout=delay_seconds)
                    self.cog = cog
                    self.cancelled = False
                
                @discord.ui.button(label="停止频道删除", style=discord.ButtonStyle.danger)
                async def cancel_deletion(self, button, interaction):
                    self.cancelled = True
                    # 停用所有按钮
                    for item in self.children:
                        item.disabled = True
                    
                    # 发送确认消息
                    await interaction.response.edit_message(content="⚠️ 频道删除已取消。频道将被保留。", view=self)
                    
                    # 通知管理员
                    try:
                        # 查找管理员角色
                        admin_role = discord.utils.get(interaction.guild.roles, name="管理员")
                        admin_mention = admin_role.mention if admin_role else "@管理员"
                        
                        # 在当前频道发送通知
                        await channel.send(
                            f"{admin_mention} 注意：此频道的自动删除已被 {interaction.user.mention} 取消。请检查是否存在需要处理的问题。"
                        )
                    except Exception as e:
                        logger.error(f"通知管理员时出错: {e}", exc_info=True)
                        await channel.send("无法通知管理员，但频道删除已取消。")
            
            # 创建视图实例
            view = CancelDeletionView(self)
            
            # 获取交易参与者提及信息
            mentions = ""
            if transaction_ids:
                transaction_id = transaction_ids
                if isinstance(transaction_ids, list) and len(transaction_ids) > 0:
                    transaction_id = transaction_ids[0]
                
                # 获取交易信息
                try:
                    transaction = await database.get_transaction(transaction_id)
                    if transaction and "buyer_id" in transaction and "seller_id" in transaction:
                        mentions = f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}> "
                except Exception as e:
                    logger.warning(f"获取交易参与者信息时出错: {e}")
            
            # 发送通知消息
            await channel.send(f"⚠️ 此频道将在 {delay_seconds} 秒后自动删除,如有异议请点击停止频道删除按钮通知管理员{mentions}", view=view)
            
            # 等待指定的时间
            await asyncio.sleep(delay_seconds)
            
            # 检查是否已取消删除
            if view.cancelled:
                logger.info(f"频道 {channel.name} (ID: {channel.id}) 的删除已被取消")
                return
            
            # 再次确认并删除频道
            try:
                await channel.delete(reason="租赁交易已完成，自动删除频道")
                logger.info(f"频道 {channel.name} (ID: {channel.id}) 已自动删除")
            except discord.errors.NotFound:
                logger.warning(f"尝试删除已不存在的频道: {channel.id}")
            except discord.errors.Forbidden:
                logger.warning(f"没有权限删除频道: {channel.id}")
            except Exception as e:
                logger.error(f"删除频道时出错: {e}", exc_info=True)
        except asyncio.CancelledError:
            # 正确处理协程取消
            logger.warning(f"删除频道 {channel.id} 的任务被取消")
            raise
        except Exception as e:
            logger.error(f"安排删除频道时出错: {e}", exc_info=True)
    
    async def open_rental_dispute(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """打开租赁争议。"""
        try:
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="dispute_opened",
                actor_id=interaction.user.id,
                details=f"{'租户' if interaction.user.id == transaction['buyer_id'] else '出租方'}发起租赁争议"
            )
            
            # 更新交易状态
            await database.update_transaction_status(transaction["id"], "disputed")
            
            # 获取管理员角色提及
            admin_role = interaction.guild.get_role(config.ADMIN_ROLE_ID)
            admin_mention = admin_role.mention if admin_role else f"<@&{config.ADMIN_ROLE_ID}>"
            
            # 创建嵌入消息
            embed = discord.Embed(
                title="⚠️ 租赁争议已开启",
                description=f"租赁交易 '{transaction['item_name']}' 已进入争议状态。",
                color=discord.Color.red()
            )
            
            embed.add_field(
                name="👤 争议方",
                value=interaction.user.mention,
                inline=True
            )
            
            embed.add_field(
                name="🕒 开启时间",
                value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:f>",
                inline=True
            )
            
            # 添加租赁信息
            if rental:
                embed.add_field(
                    name="租赁期限",
                    value=f"{rental.get('rental_period', 'N/A')} {rental.get('rental_unit', 'N/A')}",
                    inline=True
                )
                
                embed.add_field(
                    name="租金",
                    value=f"{rental.get('rental_fee', 'N/A')} USDT",
                    inline=True
                )
                
                embed.add_field(
                    name="保证金",
                    value=f"{rental.get('deposit', 'N/A')} USDT",
                    inline=True
                )
            
            embed.add_field(
                name="📝 说明",
                value="争议已开启，请等待管理员介入解决。管理员将在此频道处理争议。",
                inline=False
            )
            
            # 在当前频道直接通知管理员
            await interaction.response.send_message(
                f"{admin_mention} 租赁交易争议已开启！\n<@{transaction['buyer_id']}> <@{transaction['seller_id']}> 请在此等待管理员解决。",
                embed=embed
            )
            
        except Exception as e:
            logger.error(f"打开租赁争议时出错: {e}")
            await interaction.response.send_message("处理请求时出错，请联系管理员。", ephemeral=True)
    
    async def cancel_rental(self, interaction: discord.Interaction, transaction: Dict):
        """取消租赁。"""
        if transaction["status"] not in ["pending", "confirmed"]:
            await interaction.response.send_message("❌ 此租赁当前无法取消。", ephemeral=True)
            return
        
        # 检查用户是否为交易参与者
        if interaction.user.id not in [transaction["buyer_id"], transaction["seller_id"]]:
            await interaction.response.send_message("⚠️ 只有交易参与者可以取消租赁。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.decrement_active_rental(transaction["buyer_id"], transaction["id"])
        await database.decrement_active_rental(transaction["seller_id"], transaction["id"])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="cancel",
            actor_id=interaction.user.id,
            details="用户取消了租赁"
        )
        
        # 创建取消嵌入消息
        embed = discord.Embed(
            title="❌ 租赁已取消",
            description=f"物品 '{transaction['item_name']}' 的租赁已被取消。",
            color=discord.Color.red()
        )
        
        embed.add_field(
            name="👤 租户",
            value=f"<@{transaction['buyer_id']}>",
            inline=True
        )
        
        embed.add_field(
            name="👑 出租方",
            value=f"<@{transaction['seller_id']}>",
            inline=True
        )
        
        embed.add_field(
            name="📦 物品",
            value=transaction["item_name"],
            inline=True
        )
        
        embed.add_field(
            name="📊 状态",
            value="已取消",
            inline=True
        )
        
        embed.add_field(
            name="🔍 取消人",
            value=interaction.user.mention,
            inline=True
        )
        
        await interaction.response.send_message(
            f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}>",
            embed=embed
        )
        
        # 设置频道将在30秒后删除
        await interaction.channel.send("📢 此频道将在30秒后删除。")
        await asyncio.sleep(30)
        await interaction.channel.delete()
        # # 安排300秒后删除频道
        # await self.schedule_channel_deletion(interaction.channel, 30)
    
    async def payment_timeout(self, transaction_id: int, timeout_seconds: int):
        """设置支付超时并在Redis中记录"""
        try:
            # 设置支付超时
            await redis_client.set_payment_timeout(transaction_id, timeout_seconds)
            
            # 获取交易信息以获取频道ID
            transaction = await database.get_transaction(transaction_id)
            if transaction and transaction.get("channel_id"):
                channel = self.bot.get_channel(transaction["channel_id"])
                if channel:
                    # 如果已有任务，先取消旧任务
                    existing_task = self.payment_check_tasks.get(transaction_id)
                    if existing_task and not existing_task.done():
                        existing_task.cancel()
                        
                    # 创建新的检查任务
                    self.payment_check_tasks[transaction_id] = self.bot.loop.create_task(
                        self.check_payment_status(transaction_id, channel)
                    )
                    # 添加任务名称以便调试
                    self.payment_check_tasks[transaction_id].set_name(f"payment_check_{transaction_id}")
        except Exception as e:
            logger.error(f"设置支付超时时出错: {e}")
            
    async def check_payment_status(self, transaction_id: int, channel: discord.TextChannel):
        """检查交易的支付状态。"""
        try:
            # 最多检查30次（每分钟检查一次，总共30分钟）
            check_count = 0
            max_checks = 30
            
            while check_count < max_checks:
                # 获取交易信息
                transaction = await database.get_transaction(transaction_id)
                if not transaction:
                    logger.info(f"交易 {transaction_id} 不存在，停止检查")
                    return
                
                # 如果交易已不在等待支付状态，停止检查
                if transaction["status"] != "paying":
                    logger.info(f"交易 {transaction_id} 状态已变更为 {transaction['status']}，停止检查")
                    return
                
                # 检查是否超时
                if not await redis_client.check_payment_timeout(transaction_id):
                    logger.info(f"交易 {transaction_id} 支付已超时")
                    await self.handle_payment_timeout(transaction, channel)
                    return
                
                # 如果交易有唯一金额和支付地址，检查是否收到付款
                if transaction.get("unique_amount") and transaction.get("payment_address"):
                    txid = await binance_api.check_deposit_by_amount(
                        transaction["payment_address"], 
                        float(transaction["unique_amount"]),
                        transaction_id  # 传递transaction_id以防止重复认领
                    )
                    
                    if txid:
                        logger.info(f"交易 {transaction_id} 检测到支付，txid: {txid}")
                        # 更新交易状态和txid
                        await database.update_transaction_payment(
                            transaction_id, 
                            transaction["payment_address"], 
                            txid=txid
                        )
                        
                        # 获取最新的交易信息
                        transaction = await database.get_transaction(transaction_id)
                
                # 如果有txid，检查是否已确认
                if transaction.get("txid"):
                    if await binance_api.is_transaction_confirmed(transaction["txid"]):
                        logger.info(f"交易 {transaction_id} 支付已确认")
                        # 更新交易状态
                        await database.update_transaction_status(transaction_id, "paid")
                        
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
                        
                        if user and "free_escrow_amount" in user and user["free_escrow_amount"] > 0:
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
                        
                        # 创建物品发货按钮
                        ship_button = discord.ui.Button(
                            style=discord.ButtonStyle.primary,
                            label="已发货物品",
                            custom_id=f"ship_rental_item:{transaction_id}"  # 修改custom_id以包含transaction_id
                        )
                        
                        # 添加争议按钮
                        dispute_button = discord.ui.Button(
                            style=discord.ButtonStyle.danger,
                            label="发起争议",
                            custom_id=f"dispute_rental:{transaction_id}"
                        )
                        
                        view = discord.ui.View()
                        view.add_item(ship_button)
                        view.add_item(dispute_button)
                        
                        # 获取租赁信息
                        rental = await database.get_rental(transaction_id)
                        
                        # 创建嵌入消息
                        embed = discord.Embed(
                            title="租赁付款已确认",
                            description=f"租赁 {transaction['item_name']} 的付款已确认。",
                            color=discord.Color.green()
                        )
                        
                        embed.add_field(
                            name="租金",
                            value=f"{rental['rental_fee']} USDT",
                            inline=True
                        )
                        
                        embed.add_field(
                            name="保证金",
                            value=f"{rental['deposit']} USDT",
                            inline=True
                        )
                        
                        embed.add_field(
                            name="托管费用",
                            value=f"{transaction['escrow_fee']:.2f} USDT (每天2U)",
                            inline=True
                        )
                        
                        embed.add_field(
                            name="租赁期限",
                            value=f"{rental['rental_period']} {rental['rental_unit']}",
                            inline=True
                        )
                        
                        await channel.send(
                            f"<@{transaction['seller_id']}>，租赁方的付款已确认。请点击发货物品按钮，将物品发送给租赁方。",
                            embed=embed,
                            view=view
                        )
                        return
                
                # 每次检查后增加计数
                check_count += 1

                # 每60秒检查一次
                await asyncio.sleep(60)
                            
            # 如果达到最大检查次数但仍未完成，处理为超时
            transaction = await database.get_transaction(transaction_id)
            if transaction and transaction["status"] == "paying":
                logger.info(f"交易 {transaction_id} 检查达到最大次数，处理为超时")
                await self.handle_payment_timeout(transaction, channel)
                
        except asyncio.CancelledError:
            # 正确处理任务取消
            logger.info(f"交易 {transaction_id} 的支付检查任务被取消")
        except Exception as e:
            logger.error(f"检查交易 {transaction_id} 的支付状态时出错: {e}", exc_info=True)
        finally:
            # 清理任务引用
            self.payment_check_tasks.pop(transaction_id, None)
    
    async def handle_payment_timeout(self, transaction: Dict, channel: discord.TextChannel):
        """处理支付超时的情况。"""
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "cancelled")
        
        # 清除双方的活跃交易
        await database.decrement_active_rental(transaction["buyer_id"], transaction["id"])
        await database.decrement_active_rental(transaction["seller_id"], transaction["id"])
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="payment_timeout",
            actor_id=transaction["buyer_id"],
            details="支付超时"
        )
        
        # 更新嵌入消息
        embed = discord.Embed(
            title="支付超时",
            description=f"租赁 {transaction['item_name']} 的支付已超时。",
            color=discord.Color.red()
        )
        
        # 添加当前时间戳
        current_timestamp = int(datetime.now(timezone.utc).timestamp())
        embed.add_field(
            name="超时时间",
            value=f"<t:{current_timestamp}:F>",
            inline=True
        )
        
        await channel.send(
            f"<@{transaction['buyer_id']}> <@{transaction['seller_id']}>，支付已超时，交易已取消。",
            embed=embed
        )
        
        # 安排30秒后删除频道
        await self.schedule_channel_deletion(channel, 30, transaction["id"])

    async def ship_rental_item(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """出租方发货物品。"""
        if transaction["status"] != "paid":
            await interaction.response.send_message("此租赁当前无法发货。", ephemeral=True)
            return
        
        # 检查用户是否为出租方
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("只有出租方可以发货。", ephemeral=True)
            return
        
        # 更新交易状态
        await database.update_transaction_status(transaction["id"], "shipped")
        
        # 记录操作
        await database.log_transaction_action(
            transaction_id=transaction["id"],
            action="item_shipped",
            actor_id=interaction.user.id,
            details="出租方已发货物品"
        )
        
        # 创建确认收货和争议按钮
        confirm_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="确认收到物品",
            custom_id=f"confirm_receipt:{transaction['id']}"
        )
        
        dispute_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="发起争议",
            custom_id=f"dispute_rental:{transaction['id']}"
        )
        
        view = discord.ui.View()
        view.add_item(confirm_button)
        view.add_item(dispute_button)
        
        # 创建发货嵌入消息
        embed = discord.Embed(
            title="📦 租赁物品已发货",
            description=f"出租方 {interaction.user.mention} 已发送物品 '{transaction['item_name']}'。",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="👤 租户",
            value=f"<@{transaction['buyer_id']}>",
            inline=True
        )
        
        embed.add_field(
            name="👑 出租方",
            value=interaction.user.mention,
            inline=True
        )
        
        embed.add_field(
            name="⏱️ 后续步骤",
            value="租户收到物品后，请点击确认收到物品按钮以开始租赁计时。",
            inline=False
        )
        
        await interaction.response.send_message(
            f"<@{transaction['buyer_id']}> 出租方已发货物品，请收到后点击确认按钮。",
            embed=embed,
            view=view
        )
        
    async def remind_return(self, interaction: discord.Interaction, transaction: Dict, rental: Dict):
        """出租方提醒租户归还物品。"""
        # 检查交易状态
        if transaction["status"] not in ["paid", "shipped"]:
            await interaction.response.send_message("此租赁当前无法发送提醒。", ephemeral=True)
            return
        
        # 检查用户是否为出租方
        if interaction.user.id != transaction["seller_id"]:
            await interaction.response.send_message("只有出租方可以发送提醒。", ephemeral=True)
            return
        
        # 获取租赁结束时间
        end_date = rental.get("end_date")
        if not end_date:
            await interaction.response.send_message("无法获取租赁结束时间。", ephemeral=True)
            return
        
        try:
            # 确保 end_date 是 datetime 对象并添加时区信息
            if isinstance(end_date, str):
                end_date = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
            end_date = end_date.replace(tzinfo=timezone.utc)
            
            # 获取当前时间（UTC）
            now = datetime.now(timezone.utc)
            
            # 创建提醒消息
            embed = discord.Embed(
                title="⏰ 租赁归还提醒",
                description=f"出租方提醒您归还物品 '{transaction['item_name']}'。",
                color=discord.Color.gold()
            )

            embed.add_field(
            name="⚠️ 重要提示",
            value="请在规定时间归还物品,否则出租方将发起争议通过管理员进行保证金释放",
            inline=False
            )
            
            # 显示结束时间（使用Discord时间戳）
            embed.add_field(
                name="租赁结束时间",
                value=f"<t:{int(end_date.timestamp())}:F>",
                inline=True
            )
            
            
            
            # 计算剩余时间或逾期时间
            if now > end_date:
                # 已逾期
                time_overdue = now - end_date
                hours_overdue = time_overdue.total_seconds() / 3600
                if hours_overdue > 24:
                    days = int(hours_overdue / 24)
                    remaining_hours = hours_overdue % 24
                    overdue_text = f"已逾期 {days} 天 {remaining_hours:.1f} 小时"
                else:
                    overdue_text = f"已逾期 {hours_overdue:.1f} 小时"
                embed.add_field(name="逾期时间", value=overdue_text, inline=True)
            else:
                # 未逾期，计算剩余时间
                time_remaining = end_date - now
                hours_remaining = time_remaining.total_seconds() / 3600
                
                if hours_remaining > 24:
                    days = int(hours_remaining / 24)
                    hours = hours_remaining % 24
                    remaining_text = f"还剩 {days} 天 {hours:.1f} 小时"
                else:
                    remaining_text = f"还剩 {hours_remaining:.1f} 小时"
                
                embed.add_field(name="剩余时间", value=remaining_text, inline=True)
            
            # 创建归还按钮
            return_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="归还物品",
                custom_id=f"return_item:{transaction['id']}"
            )
            
            view = discord.ui.View()
            view.add_item(return_button)
            
            await interaction.response.send_message(
                f"<@{transaction['buyer_id']}> 出租方提醒您归还物品。",
                embed=embed,
                view=view
            )
            
            # 记录操作
            await database.log_transaction_action(
                transaction_id=transaction["id"],
                action="return_reminder",
                actor_id=interaction.user.id,
                details="出租方发送归还提醒"
            )
            
        except ValueError as e:
            logger.error(f"处理租赁结束时间时出错: {e}")
            await interaction.response.send_message("租赁结束时间格式错误。", ephemeral=True)
        except Exception as e:
            logger.error(f"发送租赁提醒时出错: {e}")
            await interaction.response.send_message("发送提醒时出错，请联系管理员。", ephemeral=True)

def setup(bot):
    if not config.LEGACY_RENTAL_ENABLED:
        return
    """加载租赁托管组件。"""
    bot.add_cog(Rental(bot)) 