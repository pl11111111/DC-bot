import discord
from discord.ext import commands
import logging
import os
import asyncio
from typing import List, Optional

import config
from utils.guild_isolation import IsolatedBot, command_allowed

# 自定义日志过滤器，过滤掉高频且不重要的日志
class LogFilter(logging.Filter):
    def filter(self, record):
        # 忽略一些高频出现的不重要日志
        if any(msg in record.getMessage() for msg in [
            "正在检查活跃抽奖以确保连接...", 
            "完成检查，已处理 0 个活跃抽奖",
            "已将 Top邀请者身份组分配给",
            "已更新自动分配排名身份组的用户列表:",
            "已更新自动分配Top邀请者身份组的用户列表:",
            "开始执行一次性服务器boost奖励发放",
            "查询到 0 个符合条件的boost用户需要发放免费额度",
            "发现 0 个需要发放免费额度的boost用户",
            "一次性服务器boost用户免费额度检查完成",
            "完成boost助力免费额度检查和发放",
            "开始获取邀请排名数据",
            "成功获取到",
            "开始更新邀请排名身份组",
            "获取到",
            "找到现有排行榜消息，正在更新",
            "已更新排行榜消息"
        ]):
            return False
        return True

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8', delay=True),
        logging.StreamHandler()
    ]
)

# 添加日志过滤器
for handler in logging.root.handlers:
    handler.addFilter(LogFilter())
    
logger = logging.getLogger(__name__)

# 定义机器人权限
intents = discord.Intents.default()
intents.members = True  # 需要成员权限来跟踪加入和用户信息
intents.message_content = True  # 需要消息内容权限来处理命令
intents.voice_states = True  # 需要语音状态权限来处理语音频道的操作

# 创建机器人实例 (使用py-cord)
bot = IsolatedBot(command_prefix="!", intents=intents)
bot.add_check(command_allowed)

# 加载组件
COGS_TO_LOAD = [
    "modules.social",
    "modules.admin",
    "modules.giveaway",
    "modules.market",
    "modules.party"
    ,"modules.new_community"
    ,"modules.new_trading"
    ,"modules.new_message_log"
    ,"modules.new_moderation"
    ,"modules.new_voice"
    ,"modules.new_trade_stats"
]

@bot.event
async def on_ready():
    """当机器人登录后调用。"""
    logger.info(f"已登录为 {bot.user.name}")
    logger.info(f"机器人 ID: {bot.user.id}")
    logger.info(f"Discord.py 版本: {discord.__version__}")
    
    # 同步应用命令
    try:
        logger.info("开始同步斜杠命令...")
        # 在py-cord中，使用sync_commands()同步所有斜杠命令
        await bot.sync_commands(check_guilds=[gid for gid in (config.GUILD_ID,config.NEW.GUILD_ID) if gid],delete_existing=True)
        logger.info("斜杠命令同步完成")
    except Exception as e:
        logger.error(f"同步斜杠命令时出错: {e}", exc_info=True)
    
    # 设置机器人状态
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="MapleStory N"
        )
    )
    
    # 显示服务器列表
    servers = len(bot.guilds)
    logger.info(f"机器人加入了 {servers} 个服务器")
    
    for guild in bot.guilds:
        logger.info(f"- {guild.name} (ID: {guild.id})")
        
    # 初始化需要在机器人就绪后执行的Cog功能
    # 现在各个Cog已经使用了自己的初始化方法，不需要在这里手动调用

@bot.event
async def on_guild_join(guild):
    """机器人加入新服务器时触发的事件。"""
    if guild.id != config.GUILD_ID:
        return  # New guild panels are managed by the dedicated modules.
    logger.info(f"加入新服务器: {guild.name} (ID: {guild.id})")
    
    # 查找或创建交易和租赁频道的分类
    trading_category = None
    rental_category = None
    voice_category = None
    
    for category in guild.categories:
        if category.name.lower() == "trading":
            trading_category = category
        elif category.name.lower() == "rentals":
            rental_category = category
        elif category.name.lower() == config.VOICE_CATEGORY.lower():
            voice_category = category
    
    # 如果分类不存在则创建
    if config.LEGACY_TRADING_ENABLED and not trading_category:
        logger.info(f"在 {guild.name} 中创建交易分类")
        try:
            trading_category = await guild.create_category("Trading")
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    "我已创建交易分类用于托管频道。"
                    "请根据需要配置权限。"
                )
        except Exception as e:
            logger.error(f"创建交易分类时出错: {e}")
    
    if config.LEGACY_RENTAL_ENABLED and not rental_category:
        logger.info(f"在 {guild.name} 中创建租赁分类")
        try:
            rental_category = await guild.create_category("Rentals")
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    "我已创建租赁分类用于托管频道。"
                    "请根据需要配置权限。"
                )
        except Exception as e:
            logger.error(f"创建租赁分类时出错: {e}")
    
    if not voice_category:
        logger.info(f"在 {guild.name} 中创建语音频道分类")
        try:
            voice_category = await guild.create_category(config.VOICE_CATEGORY)
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    f"我已创建{config.VOICE_CATEGORY}分类用于组队语音频道。"
                    "请根据需要配置权限。"
                )
        except Exception as e:
            logger.error(f"创建语音频道分类时出错: {e}")
    
    # 如果邀请频道不存在则创建
    invitation_channel = discord.utils.get(guild.text_channels, name="invitations")
    if config.LEGACY_RANKING_ENABLED and not invitation_channel:
        logger.info(f"在 {guild.name} 中创建 #invitations 频道")
        try:
            invitation_channel = await guild.create_text_channel("invitations")
            await invitation_channel.send(
                "此频道用于创建和管理邀请。"
                "使用 `/invitation create` 生成邀请链接。"
            )
            
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    "我已创建 #invitations 频道用于生成邀请链接。"
                    "用户可以在那里使用 `/invitation create`。"
                )
        except Exception as e:
            logger.error(f"创建 #invitations 频道时出错: {e}")
    
    # 如果排名频道不存在则创建
    ranking_channel = discord.utils.get(guild.text_channels, name="top-inviters")
    if config.LEGACY_RANKING_ENABLED and not ranking_channel:
        logger.info(f"在 {guild.name} 中创建 #top-inviters 频道")
        try:
            ranking_channel = await guild.create_text_channel("top-inviters")
            await ranking_channel.send(
                "此频道将显示服务器中的顶级邀请者。"
                "排名每小时更新一次。"
            )
            
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    "我已创建 #top-inviters 频道以展示顶级邀请者。"
                )
        except Exception as e:
            logger.error(f"创建 #top-inviters 频道时出错: {e}")
    
    # 如果组队频道不存在则创建
    party_channel = discord.utils.get(guild.text_channels, name="party-finder")
    if not party_channel:
        logger.info(f"在 {guild.name} 中创建 #party-finder 频道")
        try:
            party_channel = await guild.create_text_channel("party-finder")
            await party_channel.send(
                "此频道用于创建和管理组队。\n"
                "使用 `/party boss` 创建BOSS组队。\n"
                "使用 `/party task` 创建任务组队。"
            )
            
            # 向系统频道发送设置消息
            if guild.system_channel:
                await guild.system_channel.send(
                    "我已创建 #party-finder 频道用于创建组队。"
                    "用户可以在那里使用 `/party boss` 和 `/party task` 命令。"
                )
        except Exception as e:
            logger.error(f"创建 #party-finder 频道时出错: {e}")
    


@bot.event
async def on_application_command_error(ctx, error):
    """处理斜杠命令中的错误。"""
    error = getattr(error, 'original', error)  # 获取原始错误
    
    try:
        if isinstance(error, commands.CommandOnCooldown):
            if not ctx.response.is_done():
                await ctx.respond(
                    f"此命令正在冷却中。请在 {error.retry_after:.2f} 秒后重试。",
                    ephemeral=True
                )
            else:
                await ctx.followup.send(
                    f"此命令正在冷却中。请在 {error.retry_after:.2f} 秒后重试。",
                    ephemeral=True
                )
        elif isinstance(error, commands.MissingPermissions):
            if not ctx.response.is_done():
                await ctx.respond(
                    "您没有使用此命令的权限。",
                    ephemeral=True
                )
            else:
                await ctx.followup.send(
                    "您没有使用此命令的权限。",
                    ephemeral=True
                )
        elif isinstance(error,(commands.CheckFailure,discord.CheckFailure)):
            if not ctx.response.is_done():
                await ctx.respond('此指令不适用于当前社群，或你没有使用权限。',ephemeral=True)
            else:
                await ctx.followup.send('此指令不适用于当前社群，或你没有使用权限。',ephemeral=True)
        else:
            logger.error(f"命令错误: {error}", exc_info=(type(error),error,error.__traceback__))
            if not ctx.response.is_done():
                await ctx.respond(
                    "处理此命令时发生错误。请稍后重试。",
                    ephemeral=True
                )
            else:
                await ctx.followup.send(
                    "处理此命令时发生错误。请稍后重试。",
                    ephemeral=True
                )
    except Exception as e:
        logger.error(f"在处理错误时发生异常: {e}", exc_info=True)

@bot.event
async def on_error(event, *args, **kwargs):
    """处理一般错误。"""
    if event == "on_interaction":
        # 交互事件的错误处理
        interaction = args[0] if args else None
        if interaction and hasattr(interaction, 'response') and not interaction.response.is_done():
            try:
                # 尝试响应交互
                await interaction.response.defer(ephemeral=True)
                await interaction.followup.send("处理您的请求时出错。请稍后再试。", ephemeral=True)
            except Exception as e:
                logger.error(f"无法响应交互: {e}")
    
    # 记录错误信息
    logger.error(f"事件 {event} 中的错误: {args}")
    
    # 打印更详细的错误信息，以便调试
    import traceback
    logger.error(traceback.format_exc())



def run_bot():
    """运行机器人的主函数。"""
    # 加载组件
    for cog in COGS_TO_LOAD:
        try:
            bot.load_extension(cog)
            logger.info(f"已加载组件: {cog}")
        except Exception as e:
            logger.error(f"加载组件 {cog} 失败: {e}", exc_info=True)
            # 显示更详细的错误信息
            import traceback
            logger.error(f"加载组件 {cog} 的详细错误:\n{traceback.format_exc()}")
    
    # 添加任务状态检查
    async def check_background_tasks():
        await bot.wait_until_ready()
        logger.info("开始检查后台任务状态...")
        
        # 等待一些时间让任务启动
        await asyncio.sleep(10)
        
        # 检查每个已加载的Cog的任务
        task_issues_found = False
        for cog_name, cog in bot.cogs.items():
            logger.info(f"检查组件 {cog_name} 的任务状态")
            
            # 检查social模块的任务
            if cog_name == "Social" and config.LEGACY_RANKING_ENABLED:
                if hasattr(cog, 'ranking_task'):
                    status = "运行中" if cog.ranking_task and not cog.ranking_task.done() else "未运行"
                    logger.info(f"- 邀请排名任务: {status}")
                    if status == "未运行" or (cog.ranking_task and cog.ranking_task.done()):
                        task_issues_found = True
                        logger.warning("邀请排名任务未运行，尝试重启...")
                        try:
                            if cog.ranking_task and cog.ranking_task.done():
                                # 检查任务是否有异常
                                try:
                                    cog.ranking_task.result()
                                    logger.info("任务已完成但没有异常")
                                except Exception as e:
                                    logger.error(f"任务异常: {e}", exc_info=True)
                            
                            # 重新创建任务
                            cog.ranking_task = bot.loop.create_task(cog.update_invitation_rankings())
                            cog.ranking_task.set_name("ranking_task_autofix")
                            if hasattr(cog, '_handle_task_result'):
                                cog.ranking_task.add_done_callback(cog._handle_task_result)
                            logger.info("邀请排名任务已重启")
                        except Exception as e:
                            logger.error(f"重启邀请排名任务时出错: {e}", exc_info=True)
                else:
                    logger.warning("Social组件没有ranking_task属性")
                
                if hasattr(cog, 'rank_roles_task'):
                    status = "运行中" if cog.rank_roles_task and not cog.rank_roles_task.done() else "未运行"
                    logger.info(f"- 排名身份组任务: {status}")
                    if status == "未运行" or (cog.rank_roles_task and cog.rank_roles_task.done()):
                        task_issues_found = True
                        logger.warning("排名身份组任务未运行，尝试重启...")
                        try:
                            if cog.rank_roles_task and cog.rank_roles_task.done():
                                # 检查任务是否有异常
                                try:
                                    cog.rank_roles_task.result()
                                    logger.info("任务已完成但没有异常")
                                except Exception as e:
                                    logger.error(f"任务异常: {e}", exc_info=True)
                            
                            # 重新创建任务
                            cog.rank_roles_task = bot.loop.create_task(cog.update_rank_roles())
                            cog.rank_roles_task.set_name("rank_roles_task_autofix")
                            if hasattr(cog, '_handle_task_result'):
                                cog.rank_roles_task.add_done_callback(cog._handle_task_result)
                            logger.info("排名身份组任务已重启")
                        except Exception as e:
                            logger.error(f"重启排名身份组任务时出错: {e}", exc_info=True)
                else:
                    logger.warning("Social组件没有rank_roles_task属性")
            
            # 检查party模块的按钮设置任务
            if hasattr(cog, 'button_setup_task'):
                status = "运行中" if cog.button_setup_task and not cog.button_setup_task.done() else "未运行"
                logger.info(f"- 按钮设置任务: {status}")
                if status == "未运行" and hasattr(cog, 'setup_party_buttons'):
                    task_issues_found = True
                    logger.warning("按钮设置任务未运行，尝试重启...")
                    try:
                        # 重新创建任务
                        cog.button_setup_task = bot.loop.create_task(cog.setup_party_buttons())
                        cog.button_setup_task.set_name("party_button_task_autofix")
                        if hasattr(cog, '_handle_task_result'):
                            cog.button_setup_task.add_done_callback(cog._handle_task_result)
                        logger.info("按钮设置任务已重启")
                    except Exception as e:
                        logger.error(f"重启按钮设置任务时出错: {e}", exc_info=True)
        
        if task_issues_found:
            logger.warning("检测到任务问题并尝试修复，30秒后将再次检查")
            # 30秒后重新检查
            bot.loop.create_task(recheck_tasks())
        else:
            logger.info("所有任务状态正常")
        
        logger.info("后台任务状态检查完成")
    
    async def recheck_tasks():
        """再次检查任务状态"""
        await asyncio.sleep(30)
        logger.info("开始再次检查后台任务状态...")
        
        all_good = True
        for cog_name, cog in bot.cogs.items():
            if cog_name == "Social" and config.LEGACY_RANKING_ENABLED:
                if hasattr(cog, 'ranking_task'):
                    status = "运行中" if cog.ranking_task and not cog.ranking_task.done() else "未运行"
                    logger.info(f"- 邀请排名任务: {status}")
                    if status == "未运行":
                        all_good = False
                
                if hasattr(cog, 'rank_roles_task'):
                    status = "运行中" if cog.rank_roles_task and not cog.rank_roles_task.done() else "未运行"
                    logger.info(f"- 排名身份组任务: {status}")
                    if status == "未运行":
                        all_good = False
        
        if not all_good:
            logger.error("在再次检查时仍有任务未运行，可能存在更严重的问题")
        else:
            logger.info("所有任务现在正常运行")
            
        logger.info("再次检查完成")
    
    # 创建后台任务状态检查
    bot.loop.create_task(check_background_tasks())
    
    # 启动机器人
    bot.run(config.DISCORD_TOKEN)

if __name__ == "__main__":
    # 运行机器人
    run_bot() 
