import discord
from discord import SlashCommandGroup, Option, ApplicationContext, SlashCommandOptionType
from discord.ext import commands
import logging
import config
from utils import database
import random
from datetime import datetime
import traceback

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Admin(commands.Cog):
    """管理员专用命令"""
    
    def __init__(self, bot):
        self.bot = bot
        
    admin_group = SlashCommandGroup(
        name="管理",
        description="管理员指令",
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    
    # 添加市场管理命令子组
    market_group = admin_group.create_subgroup(
        name="市场",
        description="市场管理命令",
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    
    async def is_owner(ctx):
        """检查用户是否为频道拥有者"""
        if not ctx.guild:
            return False
        return ctx.guild.owner_id == ctx.author.id
    
    @admin_group.command(
        name="发放积分",
        description="向指定用户或身份组发放积分",
    )
    async def grant_free_credits(
        self, 
        ctx: ApplicationContext,
        target_type= Option(
            SlashCommandOptionType.string,
            "选择发放目标类型",
            choices=["用户", "身份组"],
            required=True
        ),
        target_id= Option(
            SlashCommandOptionType.string,
            "目标ID (用户ID或身份组ID)",
            required=True
        ),
        amount= Option(
            SlashCommandOptionType.number,
            "发放的积分数量",
            min_value=0,
            required=True
        )
    ):
        # 权限检查，只允许频道拥有者操作
        if ctx.guild.owner_id != ctx.author.id:
            await ctx.respond("只有频道拥有者可以使用此命令！", ephemeral=True)
            return
        
        try:
            # 转换目标ID为整数
            try:
                target_id_int = int(target_id)
            except ValueError:
                await ctx.respond("目标ID必须是有效的数字ID", ephemeral=True)
                return
            
            # 根据目标类型处理
            if target_type == "用户":
                # 检查用户是否存在
                user = await self.bot.fetch_user(target_id_int)
                if not user:
                    await ctx.respond(f"找不到ID为 {target_id_int} 的用户", ephemeral=True)
                    return
                
                # 确保用户在数据库中存在
                db_user = await database.get_user(target_id_int)
                if not db_user:
                    await database.create_user(target_id_int, user.name)
                
                # 添加积分
                await database.add_user_credits(target_id_int, amount)
                
                # 发送成功消息
                await ctx.respond(f"已成功向用户 {user.name} (ID: {target_id_int}) 发放 {amount} 积分", ephemeral=True)
                logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 向用户 {target_id_int} ({user.name}) 发放了 {amount} 积分")
                
            elif target_type == "身份组":
                # 获取身份组
                role = ctx.guild.get_role(target_id_int)
                if not role:
                    await ctx.respond(f"找不到ID为 {target_id_int} 的身份组", ephemeral=True)
                    return
                
                # 统计处理的成员数
                processed_count = 0
                failed_count = 0
                
                # 向所有拥有该身份组的成员发放积分
                for member in role.members:
                    try:
                        # 确保用户在数据库中存在
                        db_user = await database.get_user(member.id)
                        if not db_user:
                            await database.create_user(member.id, member.name)
                        
                        # 添加积分
                        await database.add_user_credits(member.id, amount)
                        processed_count += 1
                    except Exception as e:
                        logger.error(f"为用户 {member.id} 添加积分时出错: {str(e)}")
                        failed_count += 1
                
                # 发送成功消息
                await ctx.respond(
                    f"已成功向身份组 {role.name} (ID: {target_id_int}) 的 {processed_count} 名成员发放 {amount} 积分\n"
                    f"失败: {failed_count} 名成员",
                    ephemeral=True
                )
                logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 向身份组 {target_id_int} ({role.name}) 的 {processed_count} 名成员发放了 {amount} 积分")
            
        except Exception as e:
            logger.error(f"发放积分时出错: {str(e)}", exc_info=True)
            await ctx.respond("处理请求时发生错误，请稍后再试", ephemeral=True)

    @admin_group.command(
        name="更新邀请排名",
        description="手动更新邀请排名和排名角色"
    )
    async def update_ranks_slash(self, ctx: ApplicationContext):
        """手动更新邀请排名（斜杠命令版本）。"""
        # 检查权限
        if not any(role.id in [config.ADMIN_ROLE_ID, config.MODERATOR_ROLE_ID] for role in ctx.author.roles):
            await ctx.respond("您没有权限执行此命令。", ephemeral=True)
            return
            
        # 发送初始响应
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取Social Cog以调用更新方法
            social_cog = self.bot.get_cog('Social')
            if not social_cog:
                await ctx.followup.send("无法找到Social模块，更新失败", ephemeral=True)
                return
                
            # 更新排名
            await social_cog.update_invite_leaderboard()
            
            # 提示成功
            await ctx.followup.send("✅ 邀请排名和排名角色已成功更新！", ephemeral=True)
            
        except Exception as e:
            logger.error(f"手动更新排名时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(f"❌ 更新排名时出错: {str(e)}", ephemeral=True)
            
    @admin_group.command(
        name="重新抽取",
        description="为指定抽奖重新抽取获奖者"
    )
    async def reroll(
        self,
        ctx: ApplicationContext,
        giveaway_id: str = Option(description="抽奖ID", required=True),
        winners_count: int = Option(description="重新抽取的获奖者数量", default=1)
    ):
        """重新抽取获奖者"""
        # 检查权限
        if ctx.guild.owner_id != ctx.author.id and not any(role.id in [config.ADMIN_ROLE_ID] for role in ctx.author.roles):
            await ctx.respond("只有频道拥有者或管理员可以使用此命令！", ephemeral=True)
            return
            
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(giveaway_id)
            if not giveaway:
                await ctx.followup.send("找不到指定的抽奖", ephemeral=True)
                return
                
            if giveaway["status"] != "ended":
                await ctx.followup.send("此抽奖尚未结束，无法重新抽取", ephemeral=True)
                return
                
            # 获取参与者，排除已获奖者
            existing_winners = giveaway["winner_ids"].split(",") if giveaway["winner_ids"] else []
            participants = await database.get_giveaway_participants(giveaway_id, exclude_user_ids=existing_winners)
            
            if not participants:
                await ctx.followup.send("没有更多参与者可以抽取", ephemeral=True)
                return
                
            # 抽取新获奖者
            new_winner_count = min(winners_count, len(participants))
            new_winners = random.sample(participants, new_winner_count)
            new_winner_ids = [winner["user_id"] for winner in new_winners]
            
            # 更新获奖者
            all_winner_ids = existing_winners + new_winner_ids
            await database.update_giveaway_winners(giveaway_id, all_winner_ids)
            
            # 获取频道和消息
            channel = self.bot.get_channel(giveaway["channel_id"])
            if not channel:
                await ctx.followup.send("找不到抽奖所在的频道，但已成功重新抽取获奖者", ephemeral=True)
                return
                
            # 发送新获奖者通知
            new_winner_mentions = [f"<@{winner_id}>" for winner_id in new_winner_ids]
            await channel.send(
                f"🎊 **重新抽取结果** 🎊\n"
                f"抽奖 **{giveaway['prize_name']}** 的新获奖者: {', '.join(new_winner_mentions)}!\n"
                f"请联系 <@{giveaway['author_id']}> 领取奖品。"
            )
            
            await ctx.followup.send(f"已成功重新抽取 {new_winner_count} 名获奖者", ephemeral=True)
            
        except Exception as e:
            logger.error(f"重新抽取获奖者时出错: {str(e)}", exc_info=True)
            await ctx.followup.send("重新抽取获奖者时出错，请稍后再试", ephemeral=True)
    
    @admin_group.command(
        name="关闭抽奖",
        description="提前结束一个进行中的抽奖"
    )
    async def close_giveaway(
        self,
        ctx: ApplicationContext,
        giveaway_id: str = Option(description="抽奖ID", required=True),
        cancel: bool = Option(description="是否取消抽奖而不是结束抽奖", default=False)
    ):
        """提前结束抽奖"""
        # 检查权限
        if ctx.guild.owner_id != ctx.author.id and not any(role.id in [config.ADMIN_ROLE_ID] for role in ctx.author.roles):
            await ctx.respond("只有频道拥有者或管理员可以使用此命令！", ephemeral=True)
            return
            
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(giveaway_id)
            if not giveaway:
                await ctx.followup.send("找不到指定的抽奖", ephemeral=True)
                return
                
            if giveaway["status"] != "active":
                await ctx.followup.send("此抽奖已经结束", ephemeral=True)
                return
                
            if cancel:
                # 取消抽奖
                await database.update_giveaway_status(giveaway_id, "cancelled")
                
                # 获取参与者并退还积分
                if giveaway["credit_requirement"] > 0:
                    participants = await database.get_giveaway_participants(giveaway_id)
                    for participant in participants:
                        await database.add_user_credits(participant["user_id"], giveaway["credit_requirement"])
                
                # 获取频道和消息
                channel = self.bot.get_channel(giveaway["channel_id"])
                if channel:
                    try:
                        message = await channel.fetch_message(giveaway["message_id"])
                        
                        if message:
                            embed = message.embeds[0]
                            embed.description = "抽奖已被管理员取消"
                            embed.color = discord.Color.red()
                            
                            # 停用所有按钮
                            view = discord.ui.View()
                            join_button = discord.ui.Button(emoji="🎉", style=discord.ButtonStyle.primary, disabled=True)
                            view.add_item(join_button)
                            
                            await message.edit(content="🛑 **抽奖已取消** 🛑", embed=embed, view=view)
                            
                            # 发送取消通知
                            await channel.send(
                                f"🛑 抽奖 **{giveaway['prize_name']}** 已被管理员取消\n"
                                f"所有已扣除的积分已退还给参与者。"
                            )
                    except Exception as e:
                        logger.warning(f"找不到抽奖 {giveaway_id} 的消息或无法更新: {str(e)}")
                
                await ctx.followup.send("已成功取消抽奖", ephemeral=True)
            else:
                # 立即结束抽奖并选择获奖者
                giveaway_cog = self.bot.get_cog('Giveaway')
                if not giveaway_cog:
                    await ctx.followup.send("无法找到抽奖模块，无法结束抽奖", ephemeral=True)
                    return
                    
                # 获取end_giveaway函数
                from modules.giveaway import end_giveaway
                await end_giveaway(self.bot, giveaway_id, giveaway["message_id"], giveaway["channel_id"])
                await ctx.followup.send("已成功结束抽奖并选择获奖者", ephemeral=True)
                
        except Exception as e:
            logger.error(f"关闭抽奖时出错: {str(e)}", exc_info=True)
            await ctx.followup.send("关闭抽奖时出错，请稍后再试", ephemeral=True)

    @admin_group.command(
        name="返还抽奖额度",
        description="返还指定抽奖的所有参与者的积分",
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    async def refund_giveaway_credits_standalone(
        self,
        ctx,
        giveaway_id: str = Option(description="抽奖ID", required=True)
    ):
        """返还指定抽奖的所有参与者的积分（独立命令版本）"""
        try:
            # 记录命令开始执行
            logger.info(f"执行返还抽奖额度命令，抽奖ID: {giveaway_id}, 执行者: {ctx.author.id}")
            
            # 检查权限
            is_owner = ctx.guild.owner_id == ctx.author.id
            is_admin = False
            for role in ctx.author.roles:
                if role.id == config.ADMIN_ROLE_ID:
                    is_admin = True
                    break
                    
            if not (is_owner or is_admin):
                logger.warning(f"用户 {ctx.author.id} 尝试无权限执行返还抽奖额度命令")
                await ctx.respond("只有频道拥有者或管理员可以使用此命令！", ephemeral=True)
                return
                
            # 立即响应以避免超时
            await ctx.respond("正在处理，请稍候...", ephemeral=True)
                
            # 获取抽奖信息
            logger.info(f"查询抽奖 {giveaway_id} 信息")
            giveaway = await database.get_giveaway(giveaway_id)
            
            if not giveaway:
                logger.warning(f"找不到抽奖ID: {giveaway_id}")
                await ctx.send_followup("找不到指定的抽奖", ephemeral=True)
                return
                
            # 获取参与者信息
            logger.info(f"获取抽奖 {giveaway_id} 的参与者信息")
            participants = await database.get_giveaway_participants(giveaway_id)
            
            if not participants:
                logger.warning(f"抽奖 {giveaway_id} 没有参与者")
                await ctx.send_followup("没有参与者", ephemeral=True)
                return
                
            # 检查抽奖是否需要积分
            try:
                credit_requirement = int(giveaway.get("credit_requirement", 0))
            except (ValueError, TypeError):
                credit_requirement = 0
                
            logger.info(f"抽奖 {giveaway_id} 需要的积分: {credit_requirement}")
            
            if credit_requirement <= 0:
                logger.warning(f"抽奖 {giveaway_id} 不需要消耗积分")
                await ctx.send_followup("此抽奖不需要消耗积分", ephemeral=True)
                return
                
            # 返还额度给参与者
            refund_count = 0
            failed_count = 0
            logger.info(f"开始返还额度，参与者数量: {len(participants)}")
            
            for participant in participants:
                try:
                    user_id = participant["user_id"]
                    if not user_id:
                        logger.warning(f"参与者记录中没有有效的user_id: {participant}")
                        failed_count += 1
                        continue
                        
                    logger.info(f"返还额度给用户 {user_id}, 金额: {credit_requirement}")
                    await database.add_user_credits(user_id, credit_requirement)
                    refund_count += 1
                except Exception as e:
                    failed_count += 1
                    logger.error(f"返还用户额度时出错: {str(e)}")
                    
            # 生成结果消息
            total_credits = credit_requirement * refund_count
            
            result_message = (
                f"返还结果:\n"
                f"- 成功: {refund_count} 名参与者\n"
                f"- 失败: {failed_count} 名参与者\n"
                f"- 每人返还: {credit_requirement} 积分\n"
                f"- 总计返还: {total_credits} 积分"
            )
            
            logger.info(f"返还抽奖 {giveaway_id} 额度完成: {result_message}")
            
            # 发送结果
            await ctx.send_followup(result_message, ephemeral=True)
            
            # 记录日志
            logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 为抽奖 {giveaway_id} 返还了 {total_credits} 积分给 {refund_count} 名参与者")
            
        except Exception as e:
            error_traceback = traceback.format_exc()
            logger.error(f"返还抽奖额度命令执行出错: {str(e)}\n{error_traceback}")
            
            try:
                await ctx.send_followup(f"执行命令时出错: {str(e)}", ephemeral=True)
            except:
                try:
                    await ctx.respond(f"执行命令时出错: {str(e)}", ephemeral=True)
                except:
                    logger.error("无法发送错误消息给用户")

    @market_group.command(name="删除货币交易", description="删除指定的货币交易")
    async def delete_currency_trade_cmd(
        self, 
        ctx: ApplicationContext,
        currency_type: str = Option(description="货币类型", choices=["NXPC", "NESO"]),
        trade_type: str = Option(description="交易类型", choices=["buy", "sell"])
    ):
        """管理员命令：删除指定的货币交易"""
        # 检查权限
        if not ctx.author.guild_permissions.administrator and not any(role.id == config.BOT_ADMIN_ROLE for role in ctx.author.roles):
            await ctx.respond("⛔ 您没有权限执行此命令", ephemeral=True)
            return
            
        # 查询符合条件的交易
        trades = await database.get_currency_trades_by_type(currency_type, trade_type)
        
        if not trades:
            await ctx.respond(f"❌ 没有找到符合条件的{currency_type}_{trade_type}交易", ephemeral=True)
            return
            
        # 创建选择菜单
        options = []
        for trade in trades[:25]:  # Discord选择菜单最多25个选项
            user = await self.bot.fetch_user(trade["user_id"])
            user_name = user.display_name if user else f"用户ID:{trade['user_id']}"
            options.append(
                discord.SelectOption(
                    label=f"{currency_type} {trade_type} ×{trade['quantity']}",
                    description=f"单价: ${trade['price']:.2f} | 用户: {user_name}",
                    value=str(trade["id"])
                )
            )
            
        # 创建视图和选择菜单
        view = discord.ui.View(timeout=120)
        select = discord.ui.Select(
            placeholder="选择要删除的交易",
            options=options
        )
        
        async def callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("⛔ 您不能使用此菜单", ephemeral=True)
                return
                
            trade_id = int(select.values[0])
            success = await database.admin_delete_currency_trade(trade_id)
            
            if success:
                await interaction.response.send_message(f"✅ 已成功删除交易ID: {trade_id}", ephemeral=True)
                # 需要更新货币交易汇总
                # 获取Market cog并调用更新方法
                market_cog = self.bot.get_cog('Market')
                if market_cog:
                    await market_cog.update_currency_summary()
            else:
                await interaction.response.send_message(f"❌ 删除交易失败", ephemeral=True)
                
        select.callback = callback
        view.add_item(select)
        
        await ctx.respond("请选择要删除的交易:", view=view, ephemeral=True)
        
    @market_group.command(name="删除用户货币交易", description="删除指定用户的所有货币交易")
    async def delete_user_currency_trades(
        self,
        ctx: ApplicationContext,
        user: discord.Member = Option(description="要删除交易的用户", required=True)
    ):
        """管理员命令：删除指定用户的所有货币交易"""
        # 检查权限
        if not ctx.author.guild_permissions.administrator and not any(role.id == config.BOT_ADMIN_ROLE for role in ctx.author.roles):
            await ctx.respond("⛔ 您没有权限执行此命令", ephemeral=True)
            return
            
        # 查询用户的交易
        trades = await database.get_currency_trades_by_user(user.id)
        
        if not trades:
            await ctx.respond(f"❌ 用户 {user.display_name} 没有任何货币交易", ephemeral=True)
            return
            
        # 询问确认
        confirm_view = discord.ui.View(timeout=60)
        confirm_button = discord.ui.Button(style=discord.ButtonStyle.danger, label=f"确认删除 {len(trades)} 条交易")
        cancel_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="取消")
        
        async def confirm_callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("⛔ 您不能使用此菜单", ephemeral=True)
                return
                
            await interaction.response.defer(ephemeral=True)
            
            # 删除所有交易
            deleted_count = 0
            for trade in trades:
                success = await database.admin_delete_currency_trade(trade["id"])
                if success:
                    deleted_count += 1
            
            # 更新货币交易汇总
            market_cog = self.bot.get_cog('Market')
            if market_cog:
                await market_cog.update_currency_summary()
                
            await interaction.followup.send(f"✅ 已成功删除 {deleted_count}/{len(trades)} 条交易", ephemeral=True)
        
        async def cancel_callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("⛔ 您不能使用此菜单", ephemeral=True)
                return
                
            await interaction.response.send_message("❌ 操作已取消", ephemeral=True)
            
        confirm_button.callback = confirm_callback
        cancel_button.callback = cancel_callback
        
        confirm_view.add_item(confirm_button)
        confirm_view.add_item(cancel_button)
        
        await ctx.respond(
            f"⚠️ 确认要删除用户 {user.display_name} 的所有 {len(trades)} 条货币交易吗？此操作不可撤销。",
            view=confirm_view,
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(Admin(bot)) 