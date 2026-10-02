from utils.ui_language import localizations as ui_localizations, command_names as ui_command_names, choice as ui_choice
from utils.ui_language import text as ui_text, error as ui_error
import discord
from discord import SlashCommandGroup, Option, ApplicationContext, SlashCommandOptionType
from discord.ext import commands
import logging
import config
from utils import database
from utils.security import has_configured_role
import random
import secrets
from datetime import datetime
import traceback

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def market_admin(member, guild):
    return bool(guild and guild.id==config.GUILD_ID and
                (member.guild_permissions.administrator or
                 has_configured_role(member,(config.BOT_ADMIN_ROLE,),guild.id)))

class Admin(commands.Cog):
    """管理员专用命令"""
    
    def __init__(self, bot):
        self.bot = bot
        
    admin_group = SlashCommandGroup(
        name="管理",
        description=ui_text('English','cmd_admin'),
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True),
        description_localizations=ui_localizations('cmd_admin'),
        name_localizations=ui_command_names('cmd_admin'),
    )
    
    # 添加市场管理命令子组
    market_group = admin_group.create_subgroup(
        name="市场",
        description=ui_text('English','cmd_market'),
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True),
        description_localizations=ui_localizations('cmd_market'),
        name_localizations=ui_command_names('cmd_market'),
    )
    
    async def is_owner(ctx):
        """检查用户是否为频道拥有者"""
        if not ctx.guild:
            return False
        return ctx.guild.owner_id == ctx.author.id
    
    @admin_group.command(
        name="发放积分",
        description=ui_text('English','cmd_grant'),
        description_localizations=ui_localizations('cmd_grant'),
        name_localizations=ui_command_names('cmd_grant'),
    )
    async def grant_free_credits(
        self, 
        ctx: ApplicationContext,
        target_type= Option(
            SlashCommandOptionType.string,
            ui_text('English','opt_target_type'),
            choices=[ui_choice('用户','choice_user'),ui_choice('身份组','choice_role')],
            required=True,
            description_localizations=ui_localizations('opt_target_type'),
        ),
        target_id= Option(
            SlashCommandOptionType.string,
            ui_text('English','opt_target_id'),
            required=True,
            description_localizations=ui_localizations('opt_target_id'),
        ),
        amount= Option(
            SlashCommandOptionType.number,
            ui_text('English','opt_amount'),
            min_value=0,
            required=True,
            description_localizations=ui_localizations('opt_amount'),
        )
    ):
        # 权限检查，只允许频道拥有者操作
        if ctx.guild.owner_id != ctx.author.id:
            await ctx.respond(ui_text(ctx,'legacy_005'), ephemeral=True)
            return
        
        try:
            # 转换目标ID为整数
            try:
                target_id_int = int(target_id)
            except ValueError:
                await ctx.respond(ui_text(ctx,'legacy_036'), ephemeral=True)
                return
            
            # 根据目标类型处理
            if target_type == "用户":
                # 检查用户是否存在
                user = await self.bot.fetch_user(target_id_int)
                if not user:
                    await ctx.respond(ui_text(ctx,'legacy_037',v0=f'{target_id_int}'), ephemeral=True)
                    return
                
                # 确保用户在数据库中存在
                db_user = await database.get_user(target_id_int)
                if not db_user:
                    await database.create_user(target_id_int, user.name)
                
                # 添加积分
                await database.add_user_credits(target_id_int, amount)
                
                # 发送成功消息
                await ctx.respond(ui_text(ctx,'legacy_019',v0=f'{user.name}',v1=f'{target_id_int}',v2=f'{amount}'), ephemeral=True)
                logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 向用户 {target_id_int} ({user.name}) 发放了 {amount} 积分")
                
            elif target_type == "身份组":
                # 获取身份组
                role = ctx.guild.get_role(target_id_int)
                if not role:
                    await ctx.respond(ui_text(ctx,'legacy_040',v0=f'{target_id_int}'), ephemeral=True)
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
                    ui_text(ctx,'legacy_038',v0=f'{role.name}',v1=f'{target_id_int}',v2=f'{processed_count}',v3=f'{amount}',v4=f'{failed_count}'),
                    ephemeral=True
                )
                logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 向身份组 {target_id_int} ({role.name}) 的 {processed_count} 名成员发放了 {amount} 积分")
            
        except Exception as e:
            logger.error(f"发放积分时出错: {str(e)}", exc_info=True)
            await ctx.respond(ui_text(ctx,'legacy_020'), ephemeral=True)

    @admin_group.command(
        name="更新邀请排名",
        description=ui_text('English','cmd_rank'),
        description_localizations=ui_localizations('cmd_rank'),
        name_localizations=ui_command_names('cmd_rank'),
    )
    async def update_ranks_slash(self, ctx: ApplicationContext):
        """手动更新邀请排名（斜杠命令版本）。"""
        # 检查权限
        if not has_configured_role(ctx.author,(config.ADMIN_ROLE_ID,config.MODERATOR_ROLE_ID),config.GUILD_ID):
            await ctx.respond(ui_text(ctx,'legacy_006'), ephemeral=True)
            return
            
        # 发送初始响应
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取Social Cog以调用更新方法
            social_cog = self.bot.get_cog('Social')
            if not social_cog:
                await ctx.followup.send(ui_text(ctx,'legacy_021'), ephemeral=True)
                return
                
            # 更新排名
            await social_cog.update_invite_leaderboard()
            
            # 提示成功
            await ctx.followup.send(ui_text(ctx,'legacy_007'), ephemeral=True)
            
        except Exception as e:
            logger.error(f"手动更新排名时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(ui_text(ctx,'legacy_022',v0=f'{str(e)}'), ephemeral=True)
            
    @admin_group.command(
        name="重新抽取",
        description=ui_text('English','cmd_reroll'),
        description_localizations=ui_localizations('cmd_reroll'),
        name_localizations=ui_command_names('cmd_reroll'),
    )
    async def reroll(
        self,
        ctx: ApplicationContext,
        giveaway_id: str = Option(description=ui_text('English','opt_giveaway'), required=True,description_localizations=ui_localizations('opt_giveaway')),
        winners_count: int = Option(int,description=ui_text('English','opt_winners'), default=1,min_value=1,description_localizations=ui_localizations('opt_winners'))
    ):
        """重新抽取获奖者"""
        # 检查权限
        if (not ctx.guild or ctx.guild.id!=config.GUILD_ID or
                (ctx.guild.owner_id!=ctx.author.id and not has_configured_role(ctx.author,(config.ADMIN_ROLE_ID,),ctx.guild.id))):
            await ctx.respond(ui_text(ctx,'legacy_008'), ephemeral=True)
            return
            
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(giveaway_id)
            if not giveaway:
                await ctx.followup.send(ui_text(ctx,'legacy_023'), ephemeral=True)
                return
                
            if giveaway["status"] != "ended":
                await ctx.followup.send(ui_text(ctx,'legacy_024'), ephemeral=True)
                return
                
            # 获取参与者，排除已获奖者
            existing_winners = giveaway["winner_ids"].split(",") if giveaway["winner_ids"] else []
            participants = await database.get_giveaway_participants(giveaway_id, exclude_user_ids=existing_winners)
            
            if not participants:
                await ctx.followup.send(ui_text(ctx,'legacy_025'), ephemeral=True)
                return
                
            # 抽取新获奖者
            new_winner_count = min(winners_count, len(participants))
            new_winners = secrets.SystemRandom().sample(participants, new_winner_count)
            new_winner_ids = [winner["user_id"] for winner in new_winners]
            
            # 更新获奖者
            all_winner_ids = existing_winners + new_winner_ids
            await database.update_giveaway_winners(giveaway_id, all_winner_ids)
            
            # 获取频道和消息
            channel = self.bot.get_channel(giveaway["channel_id"])
            if not channel:
                await ctx.followup.send(ui_text(ctx,'legacy_026'), ephemeral=True)
                return
                
            # 发送新获奖者通知
            new_winner_mentions = [f"<@{winner_id}>" for winner_id in new_winner_ids]
            await channel.send(
                ui_text(ctx,'legacy_009',v0=f"{giveaway['prize_name']}",v1=f"{', '.join(new_winner_mentions)}",v2=f"{giveaway['author_id']}")
            )
            
            await ctx.followup.send(ui_text(ctx,'legacy_010',v0=f'{new_winner_count}'), ephemeral=True)
            
        except Exception as e:
            logger.error(f"重新抽取获奖者时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(ui_text(ctx,'legacy_027'), ephemeral=True)
    
    @admin_group.command(
        name="关闭抽奖",
        description=ui_text('English','cmd_close'),
        description_localizations=ui_localizations('cmd_close'),
        name_localizations=ui_command_names('cmd_close'),
    )
    async def close_giveaway(
        self,
        ctx: ApplicationContext,
        giveaway_id: str = Option(description=ui_text('English','opt_giveaway'), required=True,description_localizations=ui_localizations('opt_giveaway')),
        cancel: bool = Option(bool,description=ui_text('English','opt_cancel'), default=False,description_localizations=ui_localizations('opt_cancel'))
    ):
        """提前结束抽奖"""
        # 检查权限
        if (not ctx.guild or ctx.guild.id!=config.GUILD_ID or
                (ctx.guild.owner_id!=ctx.author.id and not has_configured_role(ctx.author,(config.ADMIN_ROLE_ID,),ctx.guild.id))):
            await ctx.respond(ui_text(ctx,'legacy_008'), ephemeral=True)
            return
            
        await ctx.defer(ephemeral=True)
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(giveaway_id)
            if not giveaway:
                await ctx.followup.send(ui_text(ctx,'legacy_023'), ephemeral=True)
                return
                
            if giveaway["status"] != "active":
                await ctx.followup.send(ui_text(ctx,'legacy_028'), ephemeral=True)
                return
                
            if cancel:
                from utils import giveaway_credits
                await giveaway_credits.settle(giveaway_id, ctx.author.id, 'refunded', cancel=True)

                # 获取频道和消息
                channel = self.bot.get_channel(giveaway["channel_id"])
                if channel:
                    try:
                        message = await channel.fetch_message(giveaway["message_id"])
                        
                        if message:
                            embed = message.embeds[0]
                            embed.description = ui_text(ctx,'legacy_029')
                            embed.color = discord.Color.red()
                            
                            # 停用所有按钮
                            view = discord.ui.View()
                            join_button = discord.ui.Button(emoji="🎉", style=discord.ButtonStyle.primary, disabled=True)
                            view.add_item(join_button)
                            
                            await message.edit(content=ui_text(ctx,'legacy_041'), embed=embed, view=view)
                            
                            # 发送取消通知
                            await channel.send(
                                ui_text(ctx,'legacy_042',v0=f"{giveaway['prize_name']}")
                            )
                    except Exception as e:
                        logger.warning(f"找不到抽奖 {giveaway_id} 的消息或无法更新: {str(e)}")
                
                await ctx.followup.send(ui_text(ctx,'legacy_029'), ephemeral=True)
            else:
                # 立即结束抽奖并选择获奖者
                giveaway_cog = self.bot.get_cog('Giveaway')
                if not giveaway_cog:
                    await ctx.followup.send(ui_text(ctx,'legacy_039'), ephemeral=True)
                    return
                    
                # 获取end_giveaway函数
                from modules.giveaway import end_giveaway
                await end_giveaway(self.bot, giveaway_id, giveaway["message_id"], giveaway["channel_id"])
                await ctx.followup.send(ui_text(ctx,'legacy_030'), ephemeral=True)
                
        except Exception as e:
            logger.error(f"关闭抽奖时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(ui_text(ctx,'legacy_031'), ephemeral=True)

    @admin_group.command(
        name="返还抽奖额度",
        description=ui_text('English','cmd_refundcredits'),
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True),
        description_localizations=ui_localizations('cmd_refundcredits'),
        name_localizations=ui_command_names('cmd_refundcredits'),
    )
    async def refund_giveaway_credits_standalone(
        self,
        ctx,
        giveaway_id: str = Option(description=ui_text('English','opt_giveaway'), required=True,description_localizations=ui_localizations('opt_giveaway'))
    ):
        if (not ctx.guild or ctx.guild.id != config.GUILD_ID or
                (ctx.guild.owner_id != ctx.author.id and
                 not has_configured_role(ctx.author,(config.ADMIN_ROLE_ID,),ctx.guild.id))):
            return await ctx.respond(ui_text(ctx,'legacy_011'), ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            from utils import giveaway_credits
            result = await giveaway_credits.settle(giveaway_id, ctx.author.id, 'refunded')
            await ctx.followup.send(ui_text(ctx,'legacy_012',v0=f"{result['count']}",v1=f"{result['total']}"), ephemeral=True)
        except ValueError as exc:
            logger.warning('Giveaway refund rejected: %s',exc)
            await ctx.followup.send(ui_error(ctx,exc), ephemeral=True)
        except Exception:
            logger.exception('Giveaway refund failed')
            await ctx.followup.send(ui_text(ctx,'legacy_032'), ephemeral=True)

    @market_group.command(name="删除货币交易", description=ui_text('English','cmd_delete'),description_localizations=ui_localizations('cmd_delete'),name_localizations=ui_command_names('cmd_delete'))
    async def delete_currency_trade_cmd(
        self, 
        ctx: ApplicationContext,
        currency_type: str = Option(description=ui_text('English','opt_currency'), choices=["NXPC", "NESO"],description_localizations=ui_localizations('opt_currency')),
        trade_type: str = Option(description=ui_text('English','opt_trade_type'), choices=[ui_choice('buy','choice_buy'),ui_choice('sell','choice_sell')],description_localizations=ui_localizations('opt_trade_type'))
    ):
        """管理员命令：删除指定的货币交易"""
        # 检查权限
        if not market_admin(ctx.author,ctx.guild):
            await ctx.respond(ui_text(ctx,'legacy_013'), ephemeral=True)
            return
            
        # 查询符合条件的交易
        trades = await database.get_currency_trades_by_type(currency_type, trade_type)
        
        if not trades:
            await ctx.respond(ui_text(ctx,'legacy_014',v0=f'{currency_type}',v1=f'{trade_type}'), ephemeral=True)
            return
            
        # 创建选择菜单
        options = []
        for trade in trades[:25]:  # Discord选择菜单最多25个选项
            user = await self.bot.fetch_user(trade["user_id"])
            user_name = user.display_name if user else f"ID: {trade['user_id']}"
            options.append(
                discord.SelectOption(
                    label=f"{currency_type} {trade_type} ×{trade['quantity']}",
                    description=ui_text(ctx,'legacy_015',v0=f"{trade['price']:.2f}",v1=f'{user_name}'),
                    value=str(trade["id"])
                )
            )
            
        # 创建视图和选择菜单
        view = discord.ui.View(timeout=120)
        select = discord.ui.Select(
            placeholder=ui_text(ctx,'legacy_000'),
            options=options
        )
        
        async def callback(interaction):
            if interaction.user.id != ctx.author.id or not market_admin(interaction.user,interaction.guild):
                await interaction.response.send_message(ui_text(interaction,'legacy_033'), ephemeral=True)
                return
                
            trade_id = int(select.values[0])
            if trade_id not in {trade['id'] for trade in trades[:25]}:
                return await interaction.response.send_message(ui_text(interaction,'legacy_033'),ephemeral=True)
            success = await database.admin_delete_currency_trade(trade_id)
            
            if success:
                await interaction.response.send_message(ui_text(interaction,'legacy_034',v0=f'{trade_id}'), ephemeral=True)
                # 需要更新货币交易汇总
                # 获取Market cog并调用更新方法
                market_cog = self.bot.get_cog('Market')
                if market_cog:
                    await market_cog.update_currency_summary()
            else:
                await interaction.response.send_message(ui_text(interaction,'legacy_035'), ephemeral=True)
                
        select.callback = callback
        view.add_item(select)
        
        await ctx.respond(ui_text(ctx,'legacy_003'), view=view, ephemeral=True)
        
    @market_group.command(name="删除用户货币交易", description=ui_text('English','cmd_delete_user'),description_localizations=ui_localizations('cmd_delete_user'),name_localizations=ui_command_names('cmd_delete_user'))
    async def delete_user_currency_trades(
        self,
        ctx: ApplicationContext,
        user: discord.Member = Option(discord.Member,description=ui_text('English','opt_user'), required=True,description_localizations=ui_localizations('opt_user'))
    ):
        """管理员命令：删除指定用户的所有货币交易"""
        # 检查权限
        if not market_admin(ctx.author,ctx.guild):
            await ctx.respond(ui_text(ctx,'legacy_013'), ephemeral=True)
            return
            
        # 查询用户的交易
        trades = await database.get_currency_trades_by_user(user.id)
        
        if not trades:
            await ctx.respond(ui_text(ctx,'legacy_016',v0=f'{user.display_name}'), ephemeral=True)
            return
            
        # 询问确认
        confirm_view = discord.ui.View(timeout=60)
        confirm_button = discord.ui.Button(style=discord.ButtonStyle.danger, label=ui_text(ctx,'legacy_001',v0=f'{len(trades)}'))
        cancel_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label=ui_text(ctx,'legacy_002'))
        
        async def confirm_callback(interaction):
            if interaction.user.id != ctx.author.id or not market_admin(interaction.user,interaction.guild):
                await interaction.response.send_message(ui_text(interaction,'legacy_033'), ephemeral=True)
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
                
            await interaction.followup.send(ui_text(interaction,'legacy_017',v0=f'{deleted_count}',v1=f'{len(trades)}'), ephemeral=True)
        
        async def cancel_callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message(ui_text(interaction,'legacy_033'), ephemeral=True)
                return
                
            await interaction.response.send_message(ui_text(interaction,'legacy_018'), ephemeral=True)
            
        confirm_button.callback = confirm_callback
        cancel_button.callback = cancel_callback
        
        confirm_view.add_item(confirm_button)
        confirm_view.add_item(cancel_button)
        
        await ctx.respond(
            ui_text(ctx,'legacy_004',v0=f'{user.display_name}',v1=f'{len(trades)}'),
            view=confirm_view,
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(Admin(bot)) 
