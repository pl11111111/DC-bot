import discord
from discord.ext import commands
from discord import SlashCommandGroup, ApplicationContext
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import json
import decimal
import re

import config
from utils import database, redis_client, helpers

# 自定义日志过滤器，过滤掉高频且不重要的日志
class SocialLogFilter(logging.Filter):
    def filter(self, record):
        # 忽略一些高频出现的不重要日志
        if any(msg in record.getMessage() for msg in [
            "已从用户 rp1__ 移除自动分配的Top邀请者身份组",
            "已从用户 louxuezhang 移除自动分配的Top邀请者身份组",
            "已将 Top邀请者身份组分配给",
            "已更新自动分配排名身份组的用户列表:",
            "已更新自动分配Top邀请者身份组的用户列表:",
            "开始执行一次性服务器boost奖励发放",
            "查询到 0 个符合条件的boost用户需要发放积分",
            "发现 0 个需要发放积分的boost用户",
            "一次性服务器boost用户积分检查完成",
            "完成boost助力积分检查和发放",
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
logger = logging.getLogger(__name__)
# 添加过滤器
logger.addFilter(SocialLogFilter())

class InviteButtonView(discord.ui.View):
    """处理邀请按钮的视图类"""
    
    def __init__(self, bot):
        # 将超时设置为None使视图持久化
        super().__init__(timeout=None)
        self.bot = bot
    
    @discord.ui.button(style=discord.ButtonStyle.primary, label="创建邀请链接", custom_id="create_invite_button")
    async def invite_button_callback(self, button, interaction):
        """当邀请按钮被点击时的回调"""
        try:
            user_id = interaction.user.id
            logger.info(f"用户 {user_id} ({interaction.user.name}) 点击了邀请按钮")
            
            # 立即发送初始响应
            try:
                await interaction.response.send_message("正在处理邀请链接请求...", ephemeral=True)
                logger.info(f"发送了初始响应给用户 {user_id}")
            except Exception as e:
                logger.error(f"发送初始响应时出错: {str(e)}")
                return
            
            # 先检查用户是否已有邀请链接
            try:
                # 从数据库查询用户现有未使用邀请
                existing_invites = await database.get_user_invites(user_id)
                
                logger.info(f"用户 {user_id} 的现有邀请: {existing_invites}")
                
                if existing_invites:
                    # 用户已有未使用的邀请链接，直接返回第一个邀请，不管是否有效
                    invite = existing_invites[0]
                    invite_code = invite.get("invite_code")
                    
                    # 检查邀请码是否为None或空字符串
                    if not invite_code:
                        logger.warning(f"从数据库获取的邀请码为空: {invite}")
                        # 创建新邀请
                        logger.info(f"用户 {user_id} 邀请码无效，将创建新邀请")
                        # 跳过后续逻辑，直接创建新邀请
                    else:
                        # 检查邀请码是否看起来像列名 (数据库错误)
                        if invite_code == "invite_code":
                            logger.warning(f"邀请码可能是列名而不是实际值: {invite_code}")
                            # 创建新邀请
                            logger.info(f"用户 {user_id} 邀请码无效 (可能是列名)，将创建新邀请")
                            # 跳过后续逻辑，直接创建新邀请
                        else:
                            # 尝试验证，但不影响返回逻辑
                            try:
                                invites = await interaction.guild.invites()
                                valid_invite = next((inv for inv in invites if inv.code == invite_code), None)
                                
                                if valid_invite:
                                    await interaction.followup.send(
                                        f"您已有一个有效的邀请链接: {valid_invite.url}\n"
                                        f"每用户只能创建一个永久邀请链接。每邀请一个新用户加入，您将获得奖励！", 
                                        ephemeral=True
                                    )
                                    logger.info(f"向用户 {user_id} 返回了现有邀请 {valid_invite.url}，拒绝创建新邀请")
                                    return
                            except Exception as e:
                                logger.error(f"验证邀请 {invite_code} 时出错: {str(e)}")
                            
                            # 如果验证失败或邀请无效，仍然返回该邀请链接
                            invite_url = f"https://discord.gg/{invite_code}"
                            await interaction.followup.send(
                                f"您已有一个邀请链接: {invite_url}\n"
                                f"每用户只能创建一个永久邀请链接。如果此链接已失效，请联系管理员。", 
                                ephemeral=True
                            )
                            logger.info(f"向用户 {user_id} 返回了现有邀请 {invite_url}，拒绝创建新邀请")
                            return
                
                # 如果没有现有邀请，继续创建新邀请
                logger.info(f"用户 {user_id} 没有现有邀请，将创建新邀请")
            except Exception as e:
                logger.error(f"查询用户邀请时出错: {str(e)}", exc_info=True)
                # 出错时，保险起见，阻止创建新邀请
                try:
                    await interaction.followup.send(
                        "验证您的邀请状态时出错，请稍后再试。如需帮助，请联系管理员。", 
                        ephemeral=True
                    )
                    return
                except Exception:
                    pass
            
            # 创建邀请
            try:
                channel_id = config.INVITE_CHANNEL_ID
                # 如果没有配置邀请频道，使用当前频道
                if channel_id == 0:
                    channel = interaction.channel
                else:
                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        logger.warning(f"找不到配置的邀请创建频道 {channel_id}，使用当前频道")
                        channel = interaction.channel
                
                logger.info(f"为用户 {user_id} 在频道 {channel.id} 创建邀请")
                
                # 创建邀请
                invite = await channel.create_invite(
                    max_age=0,  # 永久邀请
                    max_uses=0,  # 无使用次数限制
                    unique=True  # 创建新的唯一邀请
                )
                
                logger.info(f"成功创建邀请链接: {invite.url}")
                
                # 发送邀请链接给用户
                try:
                    await interaction.followup.send(
                        f"您的专属邀请链接已创建: {invite.url}\n"
                        f"每用户只能创建一个永久邀请链接。每邀请一个新用户加入，您将获得奖励！", 
                        ephemeral=True
                    )
                    logger.info(f"已发送邀请链接给用户 {user_id}")
                except Exception as e:
                    logger.error(f"发送邀请链接时出错: {str(e)}")
                    return
                
                # 在后台存储邀请信息
                try:
                    logger.info(f"准备在后台为用户 {user_id} 存储邀请信息")
                    invite_code = str(invite.code)
                    # 直接存储邀请信息，不使用异步任务
                    try:
                        # 检查邀请码是否已存在
                        invite_exists = await database.check_invite_exists(invite_code)
                        if invite_exists:
                            logger.info(f"邀请码 {invite_code} 已存在于数据库中，跳过保存")
                        else:
                            # 确保用户存在于users表中
                            db_user = await database.get_user(user_id)
                            if not db_user:
                                logger.info(f"用户 {user_id} ({interaction.user.name}) 不在users表中，正在创建...")
                                await database.create_user(user_id, interaction.user.name)
                                logger.info(f"用户 {user_id} ({interaction.user.name}) 已创建。")
                            
                            logger.info(f"尝试保存Discord邀请码 {invite_code} 到数据库")
                            invite_id = await database.create_invite(user_id, invite_code)
                            logger.info(f"已保存Discord邀请码 {invite_code} 到数据库，ID: {invite_id}")
                            
                        # 尝试更新邀请排名
                        await self.update_invite_leaderboard()
                    except Exception as e:
                        logger.error(f"存储邀请信息时出错: {str(e)}", exc_info=True)
                except Exception as e:
                    logger.error(f"创建后台存储任务时出错: {str(e)}", exc_info=True)
            
            except Exception as e:
                logger.error(f"创建邀请链接时出错: {str(e)}", exc_info=True)
                try:
                    await interaction.followup.send("创建邀请链接时出错，请稍后再试。", ephemeral=True)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"邀请按钮处理过程中出错: {str(e)}", exc_info=True)
            try:
                # 尝试发送错误消息
                if not interaction.response.is_done():
                    await interaction.response.send_message("处理您的请求时出错，请稍后再试。", ephemeral=True)
                else:
                    await interaction.followup.send("处理您的请求时出错，请稍后再试。", ephemeral=True)
            except Exception as inner_e:
                logger.error(f"在处理错误时发生额外错误: {str(inner_e)}")
                
    async def store_invite_in_background(self, user_id, invite_code):
        """后台尝试存储邀请信息，不阻塞主流程"""
        try:
            logger.info(f"开始后台存储用户 {user_id} 的邀请信息，Discord邀请码: {invite_code}")
            
            # 记录当前时间
            start_time = datetime.now()
            
            # 延迟一点时间执行，避免连接拥塞
            await asyncio.sleep(1)
            
            # 获取或创建用户
            try:
                logger.info(f"尝试获取用户 {user_id}")
                db_user = await database.get_user(user_id)
                if not db_user:
                    logger.info(f"用户 {user_id} 不存在，正在创建")
                    user_name = str(self.bot.get_user(user_id) or f"用户 {user_id}")
                    await database.create_user(user_id, user_name)
                    logger.info(f"已创建用户 {user_id} 名称: {user_name}")
                else:
                    logger.info(f"已找到用户 {user_id}")
            except Exception as e:
                logger.error(f"获取或创建用户时出错: {str(e)}", exc_info=True)
                # 尝试重新连接数据库
                try:
                    logger.info("尝试重新连接数据库...")
                    pool = await database.get_pool()
                    logger.info("数据库重新连接成功")
                    
                    # 再次尝试获取或创建用户
                    db_user = await database.get_user(user_id)
                    if not db_user:
                        user_name = str(self.bot.get_user(user_id) or f"用户 {user_id}")
                        await database.create_user(user_id, user_name)
                        logger.info(f"重试后已创建用户 {user_id}")
                except Exception as retry_e:
                    logger.error(f"重试获取或创建用户时出错: {str(retry_e)}", exc_info=True)
                    return
            
            # 直接保存Discord邀请码到数据库
            try:
                # 首先检查邀请码是否已存在
                invite_exists = await database.check_invite_exists(invite_code)
                if invite_exists:
                    logger.info(f"邀请码 {invite_code} 已存在于数据库中，跳过保存")
                    # 尝试找到邀请ID
                    invite_info = await database.get_invite_by_discord_code(invite_code)
                    invite_id = invite_info.get('id') if invite_info else None
                else:
                    logger.info(f"尝试保存Discord邀请码 {invite_code} 到数据库")
                    invite_id = await database.create_invite(user_id, invite_code)
                    logger.info(f"已保存Discord邀请码 {invite_code} 到数据库，ID: {invite_id}")
            except Exception as e:
                logger.error(f"保存邀请到数据库时出错: {str(e)}", exc_info=True)
                # 尝试重新连接并再次保存
                try:
                    logger.info("尝试重新连接数据库并再次保存邀请...")
                    pool = await database.get_pool()
                    # 首先检查邀请码是否已存在
                    invite_exists = await database.check_invite_exists(invite_code)
                    if invite_exists:
                        logger.info(f"重试后发现邀请码 {invite_code} 已存在于数据库中，跳过保存")
                        # 尝试找到邀请ID
                        invite_info = await database.get_invite_by_discord_code(invite_code)
                        invite_id = invite_info.get('id') if invite_info else None
                    else:
                        invite_id = await database.create_invite(user_id, invite_code)
                        logger.info(f"重试后已保存Discord邀请码 {invite_code} 到数据库，ID: {invite_id}")
                except Exception as retry_e:
                    logger.error(f"重试保存邀请时出错: {str(retry_e)}", exc_info=True)
                    return
            
            # 缓存邀请码
            try:
                logger.info(f"尝试缓存邀请码 {invite_code} 到Redis")
                await redis_client.cache_invite_code(user_id, invite_code)
                logger.info(f"已缓存邀请码 {invite_code} 到Redis")
            except Exception as e:
                logger.error(f"缓存邀请码到Redis时出错: {str(e)}", exc_info=True)
                # Redis缓存不是关键操作，继续处理
            
            # 记录总用时
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.info(f"已在后台成功存储用户 {user_id} 的邀请信息，总用时: {duration}秒")
            
            # 尝试更新邀请排名
            try:
                if hasattr(self, 'post_invitation_rankings'):
                    logger.info("尝试更新邀请排名...")
                    await self.post_invitation_rankings()
                    logger.info("已更新邀请排名")
            except Exception as e:
                logger.error(f"更新邀请排名时出错: {str(e)}", exc_info=True)
        except Exception as e:
            logger.error(f"后台存储邀请信息时出错: {str(e)}", exc_info=True)
            
            # 尝试记录更详细的错误信息
            try:
                import traceback
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                logger.error(f"详细错误跟踪:\n{tb_str}")
            except Exception as tb_e:
                logger.error(f"获取错误跟踪时出错: {str(tb_e)}")
                pass

class Social(commands.Cog):
    """处理邀请和服务器加成等社交功能。"""
    
    def __init__(self, bot):
        self.bot = bot
        self.ranking_task = None
        self.rank_roles_task = None
        self.booster_rewards_task = None  # 新增: 定期检查并发放boost奖励的任务
        self.invites_cache = {}  # 用于存储每个服务器的邀请缓存
        
        # 创建斜杠命令组 (使用py-cord的SlashCommandGroup)
        self.invite_group = SlashCommandGroup("invite","邀请相关命令")
        
        # 把斜杠命令方法注册到命令组
        self.invite_group.command(name="查询邀请", description="查看你的邀请次数和排名")(self.my_invites_slash)
        # 注：更新排名命令已移动到admin.py
        
        # 在__init__中直接创建和启动后台任务，确保它们在Cog加载后立即开始运行
        logger.info("Social模块初始化开始")
        
    def cog_load(self):
        """当cog被加载时启动后台任务。"""
        # 启动一个后台任务来完成初始化，以避免阻塞bot启动
        logger.info("开始加载Social模块")
        self.init_task = self.bot.loop.create_task(self.initialize_tasks())
        self.init_task.add_done_callback(self._handle_task_result)
        logger.info("已创建Social模块初始化任务")
        
    async def initialize_tasks(self):
        """异步初始化所有后台任务。"""
        try:
            # 等待机器人准备就绪
            logger.info("等待机器人准备就绪...")
            await self.bot.wait_until_ready()
            logger.info("机器人已准备就绪，开始初始化Social模块任务")
            
            # 缓存所有服务器的邀请
            try:
                await self.cache_invites()
                logger.info("成功缓存服务器邀请")
            except Exception as e:
                logger.error(f"缓存服务器邀请时出错: {e}", exc_info=True)
            
            # 同步服务器boost用户
            try:
                await self.sync_server_boosters()
                logger.info("成功同步服务器boost用户")
            except Exception as e:
                logger.error(f"同步服务器boost用户时出错: {e}", exc_info=True)
            
            # 使用异步的方式启动后台任务
            # 启动定期更新邀请排名的后台任务
            if self.ranking_task and not self.ranking_task.done():
                logger.info("邀请排名更新任务已经在运行")
            else:
                logger.info("创建并启动邀请排名更新任务")
                # 重要: 使用bot.loop创建任务，而不是直接使用asyncio.create_task
                self.ranking_task = self.bot.loop.create_task(self.update_invitation_rankings())
                self.ranking_task.set_name("ranking_task")
                self.ranking_task.add_done_callback(self._handle_task_result)
                logger.info(f"邀请排名更新任务已创建: {self.ranking_task}")

            # 启动定期更新邀请排名身份组的任务
            if self.rank_roles_task and not self.rank_roles_task.done():
                logger.info("邀请排名身份组更新任务已经在运行")
            else:
                logger.info("创建并启动邀请排名身份组更新任务")
                # 重要: 使用bot.loop创建任务，而不是直接使用asyncio.create_task
                self.rank_roles_task = self.bot.loop.create_task(self.update_rank_roles())
                self.rank_roles_task.set_name("rank_roles_task")
                self.rank_roles_task.add_done_callback(self._handle_task_result)
                logger.info(f"邀请排名身份组更新任务已创建: {self.rank_roles_task}")
            
            logger.info("已成功启动所有社交功能后台任务")
            
            # 立即执行一次排名更新，确保数据初始化
            try:
                logger.info("执行初始邀请排名更新...")
                await self.update_invite_leaderboard()
                logger.info("初始邀请排名更新完成")
            except Exception as e:
                logger.error(f"初始邀请排名更新出错: {e}", exc_info=True)
                
        except Exception as e:
            logger.error(f"初始化Social模块任务时出错: {e}", exc_info=True)
            
    def _handle_task_result(self, task):
        """处理任务完成的回调，记录任何未捕获的异常"""
        try:
            # 获取任务结果，如果有异常，这会引发异常
            task.result()
        except asyncio.CancelledError:
            logger.info(f"任务 {task.get_name() if hasattr(task, 'get_name') else 'unknown'} 被取消")
        except Exception as e:
            logger.error(f"任务执行出错: {e}", exc_info=True)
            # 如果是关键任务，尝试重新启动
            task_name = task.get_name() if hasattr(task, 'get_name') else str(task)
            logger.info(f"尝试重新启动任务: {task_name}")
            
            if "ranking_task" in task_name:
                logger.info("重新启动邀请排名任务")
                self.ranking_task = self.bot.loop.create_task(self.update_invitation_rankings())
                self.ranking_task.set_name("ranking_task_restarted")
                self.ranking_task.add_done_callback(self._handle_task_result)
            elif "rank_roles_task" in task_name:
                logger.info("重新启动排名身份组任务")
                self.rank_roles_task = self.bot.loop.create_task(self.update_rank_roles())
                self.rank_roles_task.set_name("rank_roles_task_restarted")
                self.rank_roles_task.add_done_callback(self._handle_task_result)
    
    def cog_unload(self):
        """当cog被卸载时进行清理。"""
        logger.info("开始卸载Social模块")
        if self.ranking_task:
            logger.info("取消排名更新任务")
            self.ranking_task.cancel()
        if self.rank_roles_task:
            logger.info("取消排名身份组更新任务")
            self.rank_roles_task.cancel()
        logger.info("Social模块任务已全部取消")

    async def update_invitation_rankings(self):
        """后台任务：定期更新邀请排名。"""
        # 设置任务名称，便于调试
        if hasattr(asyncio.current_task(), 'set_name'):
            asyncio.current_task().set_name('invitation_rankings_task')
            
        logger.info("邀请排名更新任务启动")
        try:
            while not self.bot.is_closed():
                try:
                    logger.info("开始执行邀请排名更新")
                    # 更新邀请排名
                    await self.update_invite_leaderboard()
                    logger.info("成功更新邀请排名")
                    
                    # 同时检查并发放boost助力奖励
                    try:
                        logger.info("开始检查并发放boost助力积分...")
                        await self.update_booster_rewards_once()
                        logger.info("完成boost助力积分检查和发放")
                    except Exception as boost_error:
                        logger.error(f"检查boost助力奖励时出错: {str(boost_error)}", exc_info=True)
                except Exception as e:
                    logger.error(f"更新邀请排名过程中出错: {str(e)}", exc_info=True)
                
                # 等待一小时后再次更新
                logger.info("邀请排名更新完成，将在1小时后再次更新")
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # 任务被取消
            logger.info("邀请排名任务被取消")
            raise
        except Exception as e:
            logger.error(f"邀请排名任务出错: {str(e)}", exc_info=True)
            raise
    
    async def update_rank_roles(self):
        """定期更新用户的邀请排名身份组。"""
        # 设置任务名称，便于调试
        if hasattr(asyncio.current_task(), 'set_name'):
            asyncio.current_task().set_name('rank_roles_task')
            
        logger.info("邀请排名身份组更新任务启动")
        try:
            while not self.bot.is_closed():
                try:
                    logger.info("开始执行邀请排名身份组更新")
                    # 调用一次性更新方法
                    await self.update_rank_roles_once()
                    
                    # 每天更新一次
                    logger.info("邀请排名身份组更新完成，24小时后再次更新")
                    await asyncio.sleep(24 * 3600)
                except Exception as e:
                    logger.error(f"邀请排名身份组更新周期中出错: {e}", exc_info=True)
                    # 出错后等待一小时再尝试
                    await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("邀请排名身份组任务被取消")
            raise
        except Exception as e:
            logger.error(f"邀请排名身份组任务出错: {e}", exc_info=True)
            raise
    
    async def update_rank_roles_once(self):
        """一次性更新用户的邀请排名身份组（不包含循环和等待）。"""
        logger.info("开始更新邀请排名身份组")
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            logger.error("找不到服务器，无法更新邀请排名身份组")
            return
            
        # 获取排名角色
        top_inviter_role = guild.get_role(config.TOP1_INVITER_ROLE_ID)
        
        if not top_inviter_role:
            logger.error("找不到排名身份组，无法更新")
            return
            
        # 获取上一轮的前三名邀请者
        previous_top_inviters = await database.get_top_inviter_role_users()
        logger.info(f"获取到 {len(previous_top_inviters) if previous_top_inviters else 0} 个上一轮自动分配身份组的用户")
        
        # 获取当前的前三名邀请者
        top_inviters = await database.get_top_inviters(3)
        
        # 只从之前因排名而获得身份组的用户中移除身份组
        if previous_top_inviters:
            for user_id in previous_top_inviters:
                member = guild.get_member(user_id)
                if member and top_inviter_role in member.roles:
                    await member.remove_roles(top_inviter_role)
                    logger.info(f"已从用户 {member.name} 移除自动分配的Top邀请者身份组")
        
        # 分配新的排名身份组（所有前三名都分配到同一个身份组）
        if top_inviters:
            # 创建新的前三名邀请者ID列表
            new_top_inviter_ids = []
            
            for i, inviter in enumerate(top_inviters, 1):
                user_id = inviter["discord_id"]
                new_top_inviter_ids.append(user_id)
                member = guild.get_member(user_id)
                
                if member:
                    await member.add_roles(top_inviter_role)
                    logger.info(f"已将 Top邀请者身份组分配给 {member.name} (排名第{i})")
            
            # 保存新的自动分配身份组的用户列表
            await database.update_top_inviter_role_users(new_top_inviter_ids)
            logger.info(f"已更新自动分配Top邀请者身份组的用户列表: {new_top_inviter_ids}")

    async def post_invitation_rankings(self):
        """发布邀请排名到指定频道"""
        try:
            # 检查是否配置了排名频道
            if config.RANKING_CHANNEL_ID == 0:
                logger.warning("未配置排名频道ID，跳过发布邀请排名")
                return
            
            # 获取排名频道
            channel = self.bot.get_channel(config.RANKING_CHANNEL_ID)
            if not channel:
                logger.warning(f"找不到排名频道 {config.RANKING_CHANNEL_ID}，跳过发布邀请排名")
                return
            
            logger.info(f"开始获取邀请排名数据")
            
            # 获取顶级邀请者
            top_inviters = await database.get_top_inviters(10)  # 获取前10名
            
            if not top_inviters:
                logger.info("没有找到邀请数据，跳过发布排名")
                return
            
            logger.info(f"成功获取到 {len(top_inviters)} 个顶级邀请者")
            
            # 创建排名嵌入消息
            embed = discord.Embed(
                title="🏆 邀请排行榜",
                description="邀请新用户加入服务器并获得验证的排名",
                color=discord.Color.gold()
            )
            
            # 获取服务器对象
            guild = self.bot.get_guild(config.GUILD_ID)
            if not guild:
                logger.warning("无法获取服务器对象，用户将不会被@提及")
            
            # 添加排名信息
            for i, inviter in enumerate(top_inviters):
                # 获取用户对象以显示正确的名称
                user_id = inviter['discord_id']
                member = guild.get_member(user_id) if guild else None
                user = self.bot.get_user(user_id)
                
                # 确定如何显示用户
                if member:
                    # 使用@提及格式
                    user_display = f"<@{user_id}>"
                else:
                    # 找不到成员，使用普通文本
                    username = user.name if user else inviter['username']
                    user_display = f"{username} (ID: {user_id})"
                
                # 为前三名添加特殊标记
                rank_emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
                
                embed.add_field(
                    name=f"{rank_emoji} 排名",
                    value=f"{user_display}\n已验证邀请: **{inviter['invite_count']}**",
                    inline=False
                )
            
            # 添加页脚
            embed.set_footer(text=f"上次更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 创建查看我的邀请按钮
            view = discord.ui.View(timeout=None)
            my_invites_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="查看我的邀请人数", 
                emoji="👥",
                custom_id="my_invites_button"
            )
            view.add_item(my_invites_button)
            
            # 添加刷新奖励按钮
            refresh_rewards_button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="刷新我的奖励", 
                emoji="🔄",
                custom_id="refresh_rewards_button"
            )
            view.add_item(refresh_rewards_button)
            
            # 添加创建邀请链接按钮
            create_invite_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="创建邀请链接",
                emoji="📨",
                custom_id="create_invite_button"
            )
            view.add_item(create_invite_button)
            
            # 尝试编辑现有消息或发送新消息
            try:
                # 获取频道中的最后50条消息
                messages = await channel.history(limit=50).flatten()
                
                # 查找机器人发送的排行榜消息
                ranking_message = None
                for msg in messages:
                    if msg.author.id == self.bot.user.id and len(msg.embeds) > 0:
                        if "邀请排行榜" in msg.embeds[0].title:
                            ranking_message = msg
                            break
                
                if ranking_message:
                    logger.info(f"找到现有排行榜消息，正在更新")
                    await ranking_message.edit(embed=embed, view=view)
                    logger.info(f"已更新排行榜消息")
                else:
                    logger.info(f"未找到现有排行榜消息，发送新消息")
                    await channel.send(embed=embed, view=view)
                    logger.info(f"已发送新的排行榜消息")
            except Exception as e:
                logger.error(f"发布排行榜消息时出错: {str(e)}", exc_info=True)
                # 尝试发送新消息
                try:
                    await channel.send(embed=embed, view=view)
                    logger.info(f"已发送新的排行榜消息（在错误后重试）")
                except Exception as retry_e:
                    logger.error(f"重试发送排行榜消息时出错: {str(retry_e)}", exc_info=True)
            
            # 更新排名身份组
            try:
                await self.update_rank_roles_once()
            except Exception as e:
                logger.error(f"更新排名身份组时出错: {str(e)}", exc_info=True)
        
        except Exception as e:
            logger.error(f"发布邀请排名时出错: {str(e)}", exc_info=True)
            import traceback
            tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"详细错误跟踪:\n{tb_str}")

    @commands.command(name="update_ranks", description="手动更新邀请排名（仅限管理员）")
    @commands.has_any_role(config.ADMIN_ROLE_ID, config.MODERATOR_ROLE_ID)
    async def update_ranks(self, ctx):
        """手动更新邀请排名和排名角色（仅限管理员使用）"""
        await ctx.send("正在更新邀请排名和排名角色，请稍候...")
        
        try:
            # 更新排名
            await self.update_invite_leaderboard()
            
            # 提示成功
            await ctx.send("✅ 邀请排名和排名角色已成功更新！")
            
        except Exception as e:
            logger.error(f"手动更新排名时出错: {str(e)}", exc_info=True)
            await ctx.send(f"❌ 更新排名时出错: {str(e)}")
    
    async def update_ranks_slash(self, ctx: ApplicationContext):
        """手动更新邀请排名（斜杠命令版本）。"""
        # 检查权限
        if not any(role.id in [config.ADMIN_ROLE_ID, config.MODERATOR_ROLE_ID] for role in ctx.author.roles):
            await ctx.respond("您没有权限执行此命令。", ephemeral=True)
            return
            
        # 发送初始响应
        await ctx.defer(ephemeral=True)
        
        try:
            # 更新排名
            await self.update_invite_leaderboard()
            
            # 提示成功
            await ctx.followup.send("✅ 邀请排名和排名角色已成功更新！", ephemeral=True)
            
        except Exception as e:
            logger.error(f"手动更新排名时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(f"❌ 更新排名时出错: {str(e)}", ephemeral=True)

    @commands.command(name="myinvites", description="查看你的邀请次数")
    async def my_invites(self, ctx):
        """查看自己的邀请次数和邀请排名"""
        try:
            user_id = ctx.author.id
            
            # 获取用户的邀请次数
            invite_count = await database.get_user_invite_count(user_id)
            verified_invite_count = await database.get_verified_invite_count(user_id)
            
            # 确保变量是整数类型
            try:
                if isinstance(invite_count, str):
                    if invite_count.isdigit():
                        invite_count = int(invite_count)
                    else:
                        logger.warning(f"获取到的invite_count格式异常: {invite_count}")
                        invite_count = 0
                elif invite_count is None:
                    invite_count = 0
                else:
                    invite_count = int(invite_count)
            except (ValueError, TypeError):
                logger.warning(f"无法将invite_count转换为整数: {invite_count}")
                invite_count = 0
            
            try:
                if isinstance(verified_invite_count, str):
                    if verified_invite_count.isdigit():
                        verified_invite_count = int(verified_invite_count)
                    else:
                        logger.warning(f"获取到的verified_invite_count格式异常: {verified_invite_count}")
                        verified_invite_count = 0
                elif verified_invite_count is None:
                    verified_invite_count = 0
                else:
                    verified_invite_count = int(verified_invite_count)
            except (ValueError, TypeError):
                logger.warning(f"无法将verified_invite_count转换为整数: {verified_invite_count}")
                verified_invite_count = 0
            
            # 获取用户当前的积分
            user = await database.get_user(user_id)
            
            # 确保积分是浮点数
            try:
                if user and 'free_escrow_amount' in user:
                    if isinstance(user['free_escrow_amount'], str):
                        try:
                            free_escrow_amount = float(user['free_escrow_amount'])
                        except (ValueError, TypeError):
                            logger.warning(f"无法将free_escrow_amount转换为浮点数: {user['free_escrow_amount']}")
                            free_escrow_amount = 0.0
                    else:
                        free_escrow_amount = float(user['free_escrow_amount']) if user['free_escrow_amount'] is not None else 0.0
                else:
                    free_escrow_amount = 0.0
            except Exception as e:
                logger.warning(f"处理free_escrow_amount时出错: {str(e)}")
                free_escrow_amount = 0.0
            
            # 计算距离下一次获得积分还需要多少邀请
            threshold = int(config.INVITE_FREE_ESCROW_THRESHOLD)
            next_reward = threshold - (verified_invite_count % threshold) if verified_invite_count % threshold != 0 else threshold
            
            # 获取所有邀请者排名
            all_inviters = await database.get_top_inviters(100)  # 获取前100名
            
            # 计算用户的排名
            user_rank = None
            for i, inviter in enumerate(all_inviters, 1):
                if inviter['discord_id'] == user_id:
                    user_rank = i
                    break
            
            # 创建一个embed来显示用户邀请信息
            embed = discord.Embed(
                title="📊 你的邀请统计",
                description="感谢您邀请新用户加入我们的服务器！",
                color=discord.Color.blue()
            )
            
            embed.add_field(name="总邀请人数", value=f"**{invite_count}** 人", inline=True)
            embed.add_field(name="已验证邀请人数", value=f"**{verified_invite_count}** 人", inline=True)
            embed.add_field(name="当前积分", value=f"**{free_escrow_amount:.2f}** ", inline=True)
            
            if user_rank:
                embed.add_field(name="当前排名", value=f"**第 {user_rank} 名**", inline=True)
                
            else:
                embed.add_field(name="当前排名", value="暂无排名", inline=True)
            
            embed.add_field(
                name="下一次奖励",
                value=f"再邀请 **{next_reward}** 人获得验证可获得 **{config.INVITE_FREE_ESCROW_AMOUNT}**  积分",
                inline=False
            )
            
            # 添加如何邀请的提示
            embed.add_field(
                name="💡 如何邀请更多用户",
                value="点击邀请排行榜中的\"创建邀请链接\"按钮获取你的专属邀请链接，分享给好友即可。",
                inline=False
            )
            
            embed.set_footer(text="注：只有邀请的用户获得验证身份组后才会计入已验证邀请")
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"获取邀请信息时出错: {str(e)}", exc_info=True)
            await ctx.send(f"❌ 获取邀请信息时出错，请稍后再试。")

    async def my_invites_slash(self, ctx: ApplicationContext):
        """查看你的邀请次数（斜杠命令版本）。"""
        try:
            # 发送初始响应
            await ctx.defer(ephemeral=True)
            
            user_id = ctx.author.id
            username = str(ctx.author)
            
            logger.info(f"用户 {username} (ID: {user_id}) 请求查看其邀请信息")
            
            # 获取邀请信息
            total_invites = await database.get_user_invite_count(user_id)
            verified_invites = await database.get_verified_invite_count(user_id)
            
            # 获取排名信息
            inviter_ranking = await database.get_inviter_ranking(user_id)
            total_inviters = await database.get_total_inviters_count()
            
            rank_text = f"#{inviter_ranking}" if inviter_ranking else "未排名"
            
            # 获取用户的积分
            user = await database.get_user(user_id)
            free_escrow_amount = 0
            if user:
                try:
                    free_escrow_amount = float(user.get('free_escrow_amount', 0))
                except (ValueError, TypeError):
                    logger.warning(f"用户 {user_id} 的积分格式不正确: {user.get('free_escrow_amount')}")
            
            # 创建嵌入消息
            embed = discord.Embed(
                title="📊 您的邀请统计",
                description=f"用户: {ctx.author.mention}",
                color=discord.Color.blue()
            )
            
            embed.add_field(name="总邀请数", value=str(total_invites), inline=True)
            embed.add_field(name="已验证邀请", value=str(verified_invites), inline=True)
            embed.add_field(name="排名", value=f"{rank_text} / {total_inviters}", inline=True)
            
            # 添加积分信息
            if free_escrow_amount > 0:
                embed.add_field(
                    name="积分", 
                    value=f"{free_escrow_amount}", 
                    inline=False
                )
            
            # 添加邀请奖励信息
            rewards_info = (
                f"每成功邀请 {config.INVITE_FREE_ESCROW_THRESHOLD} 名用户加入并验证，"
                f"您将获得 {config.INVITE_FREE_ESCROW_AMOUNT} 的积分！"
            )
            embed.add_field(name="邀请奖励", value=rewards_info, inline=False)
            
            # 添加排名身份组信息
            rank_roles_info = ""
            if config.TOP1_INVITER_ROLE_ID:
                rank_roles_info += f"• 邀请第一名: <@&{config.TOP1_INVITER_ROLE_ID}>\n"
            if config.TOP2_INVITER_ROLE_ID:
                rank_roles_info += f"• 邀请第二名: <@&{config.TOP2_INVITER_ROLE_ID}>\n"
            if config.TOP3_INVITER_ROLE_ID:
                rank_roles_info += f"• 邀请第三名: <@&{config.TOP3_INVITER_ROLE_ID}>\n"
                
            if rank_roles_info:
                embed.add_field(name="排名奖励身份组", value=rank_roles_info, inline=False)
            
            # 添加脚注
            embed.set_footer(text="邀请排名每小时更新一次")
            
            # 发送消息
            await ctx.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"处理my_invites_slash命令时出错: {str(e)}", exc_info=True)
            await ctx.followup.send("获取邀请信息时出错，请稍后再试。", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """当新成员加入服务器时，尝试跟踪并记录他们使用的邀请。"""
        try:
            guild = member.guild
            logger.info(f"用户 {member.name} (ID: {member.id}) 加入了服务器 {guild.name}")
            
            # 如果是机器人，则忽略
            if member.bot:
                logger.info(f"忽略机器人 {member.name} 的加入")
                return
            
            # 获取服务器所有邀请
            try:
                # 获取加入前的邀请缓存
                invites_before = self.invites_cache.get(guild.id, {})
                
                if not invites_before:
                    logger.warning(f"加入前的邀请缓存为空，可能是首次使用或缓存失效")
                else:
                    logger.info(f"加入前邀请缓存: {invites_before}")
                
                # 获取当前所有邀请
                current_invites = await guild.invites()
                current_invites_dict = {invite.code: invite.uses for invite in current_invites}
                
                logger.info(f"当前邀请列表: {current_invites_dict}")
                
                # 更新缓存前先找出被使用的邀请
                used_invite_code = None
                likely_invites = []
                
                for invite in current_invites:
                    # 如果这个邀请在加入前不存在
                    if invite.code not in invites_before:
                        logger.info(f"发现新邀请: {invite.code}, 使用次数: {invite.uses}")
                        if invite.uses > 0:  # 只有使用次数>0才可能是被用来加入的
                            likely_invites.append((invite.code, invite.uses, "新邀请"))
                    # 或者使用次数增加了
                    elif invite.uses > invites_before[invite.code]:
                        logger.info(f"邀请使用次数增加: {invite.code}, 从 {invites_before[invite.code]} 增加到 {invite.uses}")
                        likely_invites.append((invite.code, invite.uses - invites_before[invite.code], "次数增加"))
                
                # 根据使用次数差异排序，优先选择使用次数变化最大的
                likely_invites.sort(key=lambda x: x[1], reverse=True)
                
                if likely_invites:
                    used_invite_code = likely_invites[0][0]
                    logger.info(f"最可能的邀请码: {used_invite_code}, 原因: {likely_invites[0][2]}, 变化量: {likely_invites[0][1]}")
                    
                    # 如果有多个可能的邀请码，记录所有可能性
                    if len(likely_invites) > 1:
                        logger.info(f"其他可能的邀请码: {likely_invites[1:]}")
                
                # 更新缓存
                self.invites_cache[guild.id] = current_invites_dict
                
                if used_invite_code:
                    logger.info(f"检测到用户 {member.id} 使用邀请码 {used_invite_code} 加入服务器")
                    
                    # 检查数据库中是否有这个邀请码
                    db_invite = await database.get_invite_by_discord_code(used_invite_code)
                    
                    if db_invite:
                        logger.info(f"在数据库中找到邀请码 {used_invite_code}, 邀请者ID: {db_invite.get('inviter_id')}")
                        # 记录邀请使用情况，但不标记为已使用
                        try:
                            result = await database.use_invite(used_invite_code, member.id)
                            if result > 0:
                                logger.info(f"已记录邀请码 {used_invite_code} 被用户 {member.id} 使用")
                            else:
                                logger.warning(f"记录邀请使用情况失败: {used_invite_code}, 可能用户已使用过其他邀请码")
                        except Exception as e:
                            logger.error(f"记录邀请使用情况时出错: {e}", exc_info=True)
                    else:
                        logger.warning(f"数据库中未找到邀请码 {used_invite_code}, 尝试查询所有邀请码")
                        try:
                            # 获取所有邀请记录，查看是否有相似的邀请码
                            query = "SELECT invite_code FROM invitations"
                            all_invites = await database.fetch_all(query)
                            if all_invites:
                                all_invite_codes = [inv.get('invite_code') for inv in all_invites if inv.get('invite_code')]
                                logger.info(f"数据库中所有邀请码: {all_invite_codes}")
                        except Exception as e:
                            logger.error(f"查询所有邀请码时出错: {e}")
                else:
                    logger.info(f"无法确定用户 {member.id} 使用的邀请码")
                    try:
                        # 如果找不到使用的邀请码，检查是否使用了系统生成的邀请链接
                        vanity_invite = await guild.vanity_invite()
                        if vanity_invite:
                            logger.info(f"服务器有自定义邀请链接: {vanity_invite.code}, 用户可能使用了此链接")
                    except (discord.HTTPException, discord.NotFound, AttributeError):
                        # 服务器没有自定义邀请链接或无权限查看
                        pass
            except Exception as e:
                logger.error(f"处理用户加入事件时出错: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"on_member_join事件处理出错: {e}", exc_info=True)

    async def cache_invites(self):
        """缓存所有服务器的邀请。"""
        for guild in self.bot.guilds:
            try:
                # 获取服务器的所有邀请
                invites = await guild.invites()
                # 以邀请码为键，使用次数为值，缓存起来
                self.invites_cache[guild.id] = {invite.code: invite.uses for invite in invites}
                logger.info(f"已缓存服务器 {guild.name} (ID: {guild.id}) 的 {len(invites)} 个邀请")
                
                # 输出详细的邀请信息用于调试
                for invite in invites:
                    logger.info(f"邀请码: {invite.code}, 使用次数: {invite.uses}, 创建者: {invite.inviter.name if invite.inviter else 'Unknown'}")
            except discord.Forbidden:
                logger.error(f"没有权限获取服务器 {guild.name} 的邀请")
            except Exception as e:
                logger.error(f"缓存服务器 {guild.name} 的邀请时出错: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """当机器人加入新服务器时，缓存该服务器的邀请。"""
        try:
            # 获取并缓存新服务器的邀请
            invites = await guild.invites()
            self.invites_cache[guild.id] = {invite.code: invite.uses for invite in invites}
            logger.info(f"已缓存新加入的服务器 {guild.name} 的 {len(invites)} 个邀请")
        except discord.Forbidden:
            logger.error(f"没有权限获取服务器 {guild.name} 的邀请")
        except Exception as e:
            logger.error(f"缓存新服务器 {guild.name} 的邀请时出错: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        """当创建新邀请时，更新缓存。"""
        try:
            # 确保服务器ID存在于缓存中
            if invite.guild.id not in self.invites_cache:
                self.invites_cache[invite.guild.id] = {}
            
            # 更新缓存
            self.invites_cache[invite.guild.id][invite.code] = invite.uses
            logger.info(f"已更新服务器 {invite.guild.name} 的邀请缓存，添加新邀请 {invite.code}")
        except Exception as e:
            logger.error(f"更新邀请缓存时出错: {e}", exc_info=True)
    
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        """当成员更新时，检查身份组变化（已验证状态和boost状态）。"""
        try:
            # 检查是否是同一个服务器
            if after.guild.id != config.GUILD_ID:
                return
                
            # 检查1：验证状态变化
            before_has_verified = discord.utils.get(before.roles, id=config.VERIFIED_ROLE_ID) is not None
            after_has_verified = discord.utils.get(after.roles, id=config.VERIFIED_ROLE_ID) is not None
            
            if not before_has_verified and after_has_verified:
                logger.info(f"用户 {after.name} (ID: {after.id}) 获得了已验证身份组")
                
                # 更新邀请记录
                result = await database.verify_invitation(after.id)
                if result > 0:
                    logger.info(f"已验证用户 {after.id} 的邀请记录")
                else:
                    logger.warning(f"未找到用户 {after.id} 的有效邀请记录或验证失败")
            
            # 检查2：boost状态变化
            had_booster_role = any(role.id == config.BOOSTER_ROLE_ID for role in before.roles)
            has_booster_role = any(role.id == config.BOOSTER_ROLE_ID for role in after.roles)
            
            # 如果用户新获得了boost者身份组
            if not had_booster_role and has_booster_role:
                logger.info(f"用户 {after.id} ({after.name}) 开始boost服务器")
                
                # 检查用户是否已经在数据库中有boost记录
                query = "SELECT * FROM server_boosts WHERE user_id = %s"
                boost_info = await database.fetch_one(query, (after.id,))
                
                is_new_booster = boost_info is None
                
                # 记录用户的boost信息
                await database.record_server_boost(after.id)
                
                # 更新活跃状态
                await database.execute_query(
                    "UPDATE server_boosts SET is_active = 1 WHERE user_id = %s", 
                    (after.id,)
                )
                
                # 计算用户当前的积分（用于DM消息）
                user = await database.get_user(after.id)
                current_free_escrow = user.get('free_escrow_amount', 0) if user else 0
                try:
                    # 确保current_free_escrow是浮点数
                    if isinstance(current_free_escrow, decimal.Decimal):
                        current_free_escrow = float(current_free_escrow)
                    elif isinstance(current_free_escrow, str):
                        current_free_escrow = float(current_free_escrow)
                except (ValueError, TypeError):
                    current_free_escrow = 0.0
                
                # 只有对新boost用户或者从未获得过积分的用户发放积分
                if is_new_booster or (boost_info and boost_info.get('last_credits_at') is None):
                    # 计算新的积分（确保都是浮点数）
                    new_free_escrow = current_free_escrow + float(config.FREE_CREDITS_AMOUNT)
                    
                    # 更新积分
                    await database.update_user_free_escrow_amount(after.id, new_free_escrow)
                    await database.update_booster_last_credits(after.id)
                    
                    logger.info(f"向新boost用户 {after.id} ({after.name}) 发放了 {config.FREE_CREDITS_AMOUNT} 积分，总积分: {new_free_escrow}")
                    
                    # 更新当前积分（用于DM显示）
                    current_free_escrow = new_free_escrow
                
                # 给用户发送DM通知（无论是否获得积分）
                try:
                    # 根据是否获得积分选择不同的消息内容
                    if is_new_booster or (boost_info and boost_info.get('last_credits_at') is None):
                        embed = discord.Embed(
                            title="🎁 感谢boost服务器！",
                            description=f"感谢您boost我们的服务器！\n\n作为感谢，您已收到 **{config.FREE_CREDITS_AMOUNT}** 的积分。",
                            color=discord.Color.nitro_pink()
                        )
                        embed.add_field(
                            name="持续奖励", 
                            value=f"只要您继续boost服务器，每30天都将获得 **{config.FREE_CREDITS_AMOUNT}** 的积分！", 
                            inline=False
                        )
                    else:
                        embed = discord.Embed(
                            title="🎁 感谢boost服务器！",
                            description=f"感谢您boost我们的服务器！",
                            color=discord.Color.nitro_pink()
                        )
                        
                        # 如果有上次获得积分的记录，计算下次获得积分的时间
                        if boost_info and boost_info.get('last_credits_at'):
                            last_credits_at = boost_info.get('last_credits_at')
                            next_credits_date = last_credits_at + timedelta(days=30)
                            days_until_next = max(0, (next_credits_date - datetime.now()).days)
                            
                            embed.add_field(
                                name="下次积分", 
                                value=f"您将在 **{days_until_next}** 天后获得下一次积分奖励。", 
                                inline=False
                            )
                    
                    embed.set_footer(text=f"当前总积分: {current_free_escrow}")
                    
                    # 记录发送DM前的日志
                    logger.info(f"尝试向用户 {after.id} ({after.name}) 发送boost开始DM通知...")
                    
                    # 检查是否可以发送DM
                    try:
                        can_dm = after.dm_channel is not None or await after.create_dm() is not None
                        if not can_dm:
                            logger.warning(f"无法创建与用户 {after.id} ({after.name}) 的DM频道")
                    except Exception as e:
                        logger.warning(f"检查DM权限时出错: {str(e)}")
                    
                    # 发送DM
                    await after.send(embed=embed)
                    logger.info(f"成功向用户 {after.id} ({after.name}) 发送了boost开始通知DM")
                except discord.Forbidden:
                    logger.warning(f"无法向用户 {after.id} ({after.name}) 发送boost开始DM通知: 权限不足(可能是用户关闭了DM)")
                except discord.HTTPException as e:
                    logger.warning(f"向用户 {after.id} ({after.name}) 发送boost开始DM通知时发生HTTP错误: {str(e)}")
                except Exception as e:
                    logger.warning(f"无法向新boost用户 {after.id} ({after.name}) 发送boost开始DM通知: {str(e)}", exc_info=True)
            
            # 如果用户失去了boost者身份组
            elif had_booster_role and not has_booster_role:
                logger.info(f"用户 {after.id} ({after.name}) 停止boost服务器")
                
                # 标记此用户不再是活跃boost用户
                await database.execute_query(
                    "UPDATE server_boosts SET is_active = 0 WHERE user_id = %s", 
                    (after.id,)
                )
                
                # 可以选择给用户发送DM提醒
                try:
                    embed = discord.Embed(
                        title="💔 server boost结束通知",
                        description="我们注意到您不再boost我们的服务器。\n\n感谢您之前的支持！如果您再次boost服务器，将继续获得积分。",
                        color=discord.Color.dark_grey()
                    )
                    
                    # 记录发送DM前的日志
                    logger.info(f"尝试向停止boost的用户 {after.id} ({after.name}) 发送DM通知...")
                    
                    # 检查是否可以发送DM
                    try:
                        can_dm = after.dm_channel is not None or await after.create_dm() is not None
                        if not can_dm:
                            logger.warning(f"无法创建与用户 {after.id} ({after.name}) 的DM频道")
                    except Exception as e:
                        logger.warning(f"检查DM权限时出错: {str(e)}")
                    
                    # 发送DM
                    await after.send(embed=embed)
                    logger.info(f"成功向用户 {after.id} ({after.name}) 发送了停止boost通知DM")
                except discord.Forbidden:
                    logger.warning(f"无法向用户 {after.id} ({after.name}) 发送停止boost DM通知: 权限不足(可能是用户关闭了DM)")
                except discord.HTTPException as e:
                    logger.warning(f"向用户 {after.id} ({after.name}) 发送停止boost DM通知时发生HTTP错误: {str(e)}")
                except Exception as e:
                    logger.warning(f"无法向停止boost用户 {after.id} ({after.name}) 发送DM通知: {str(e)}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"处理成员更新事件时出错: {e}", exc_info=True)

    async def update_invite_leaderboard(self):
        """更新邀请排行榜。"""
        # 获取配置的频道ID
        channel_id = config.RANKING_CHANNEL_ID
        if not channel_id:
            logger.warning("未配置邀请排行榜频道ID，跳过更新")
            return
        
        # 获取频道对象
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"无法找到邀请排行榜频道 {channel_id}，跳过更新")
            return
            
        logger.info(f"开始获取邀请排名数据")
        
        # 获取顶级邀请者
        top_inviters = await database.get_top_inviters(10)  # 获取前10名
        
        if not top_inviters:
            logger.info("没有找到邀请数据，跳过发布排名")
            return
        
        logger.info(f"成功获取到 {len(top_inviters)} 个顶级邀请者")
        
        # 创建排名嵌入消息
        embed = discord.Embed(
            title="🏆 邀请排行榜",
            description="邀请新用户加入服务器并获得验证的排名",
            color=discord.Color.gold()
        )
        
        # 获取服务器对象
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            logger.warning("无法获取服务器对象，用户将不会被@提及")
        
        # 添加排名信息
        for i, inviter in enumerate(top_inviters):
            # 获取用户对象以显示正确的名称
            user_id = inviter['discord_id']
            member = guild.get_member(user_id) if guild else None
            user = self.bot.get_user(user_id)
            
            # 确定如何显示用户
            if member:
                # 使用@提及格式
                user_display = f"<@{user_id}>"
            else:
                # 找不到成员，使用普通文本
                username = user.name if user else inviter['username']
                user_display = f"{username} (ID: {user_id})"
            
            # 为前三名添加特殊标记
            rank_emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
            
            embed.add_field(
                name=f"{rank_emoji} 排名",
                value=f"{user_display}\n已验证邀请: **{inviter['invite_count']}**",
                inline=False
            )
        
        # 添加页脚
        embed.set_footer(text=f"上次更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 创建查看我的邀请按钮
        view = discord.ui.View(timeout=None)
        my_invites_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="查看我的邀请人数", 
            emoji="👥",
            custom_id="my_invites_button"
        )
        view.add_item(my_invites_button)
        
        # 添加刷新奖励按钮
        refresh_rewards_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="刷新我的奖励", 
            emoji="🔄",
            custom_id="refresh_rewards_button"
        )
        view.add_item(refresh_rewards_button)
        
        # 添加创建邀请链接按钮
        create_invite_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="创建邀请链接",
            emoji="📨",
            custom_id="create_invite_button"
        )
        view.add_item(create_invite_button)
        
        # 尝试编辑现有消息或发送新消息
        try:
            # 获取频道中的最后50条消息
            messages = await channel.history(limit=50).flatten()
            
            # 查找机器人发送的排行榜消息
            ranking_message = None
            for msg in messages:
                if msg.author.id == self.bot.user.id and len(msg.embeds) > 0:
                    if "邀请排行榜" in msg.embeds[0].title:
                        ranking_message = msg
                        break
            
            if ranking_message:
                logger.info(f"找到现有排行榜消息，正在更新")
                await ranking_message.edit(embed=embed, view=view)
                logger.info(f"已更新排行榜消息")
            else:
                logger.info(f"未找到现有排行榜消息，发送新消息")
                await channel.send(embed=embed, view=view)
                logger.info(f"已发送新的排行榜消息")
        except Exception as e:
            logger.error(f"发布排行榜消息时出错: {str(e)}", exc_info=True)
            # 尝试发送新消息
            try:
                await channel.send(embed=embed, view=view)
                logger.info(f"已发送新的排行榜消息（在错误后重试）")
            except Exception as retry_e:
                logger.error(f"重试发送排行榜消息时出错: {str(retry_e)}", exc_info=True)
        
        # 更新排名身份组
        try:
            await self.update_rank_roles_once()
        except Exception as e:
            logger.error(f"更新排名身份组时出错: {str(e)}", exc_info=True)
            
    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        """处理按钮交互。"""
        # 检查是否是我们的自定义按钮
        if interaction.type != discord.InteractionType.component:
            return
            
        # 判断是哪个按钮
        custom_id = interaction.data.get('custom_id', '')
        
        try:
            if custom_id == "my_invites_button":
                # 回复交互以避免超时
                await interaction.response.defer(ephemeral=True)
                
                # 获取用户ID
                user_id = interaction.user.id
                
                # 查询用户的邀请统计
                invite_count = await database.get_user_invite_count(user_id)
                verified_invite_count = await database.get_verified_invite_count(user_id)
                
                # 确保变量是整数类型
                try:
                    if isinstance(invite_count, str):
                        if invite_count.isdigit():
                            invite_count = int(invite_count)
                        else:
                            logger.warning(f"获取到的invite_count格式异常: {invite_count}")
                            invite_count = 0
                    elif invite_count is None:
                        invite_count = 0
                    else:
                        invite_count = int(invite_count)
                except (ValueError, TypeError):
                    logger.warning(f"无法将invite_count转换为整数: {invite_count}")
                    invite_count = 0
                
                try:
                    if isinstance(verified_invite_count, str):
                        if verified_invite_count.isdigit():
                            verified_invite_count = int(verified_invite_count)
                        else:
                            logger.warning(f"获取到的verified_invite_count格式异常: {verified_invite_count}")
                            verified_invite_count = 0
                    elif verified_invite_count is None:
                        verified_invite_count = 0
                    else:
                        verified_invite_count = int(verified_invite_count)
                except (ValueError, TypeError):
                    logger.warning(f"无法将verified_invite_count转换为整数: {verified_invite_count}")
                    verified_invite_count = 0
                
                # 获取用户当前的积分
                user = await database.get_user(user_id)
                
                # 确保积分是浮点数
                try:
                    if user and 'free_escrow_amount' in user:
                        if isinstance(user['free_escrow_amount'], str):
                            try:
                                free_escrow_amount = float(user['free_escrow_amount'])
                            except (ValueError, TypeError):
                                logger.warning(f"无法将free_escrow_amount转换为浮点数: {user['free_escrow_amount']}")
                                free_escrow_amount = 0.0
                        else:
                            free_escrow_amount = float(user['free_escrow_amount']) if user['free_escrow_amount'] is not None else 0.0
                    else:
                        free_escrow_amount = 0.0
                except Exception as e:
                    logger.warning(f"处理free_escrow_amount时出错: {str(e)}")
                    free_escrow_amount = 0.0
                
                # 计算距离下一次获得积分还需要多少邀请
                threshold = int(config.INVITE_FREE_ESCROW_THRESHOLD)
                next_reward = threshold - (verified_invite_count % threshold) if verified_invite_count % threshold != 0 else threshold
                
                # 创建回复嵌入消息
                embed = discord.Embed(
                    title="📊 您的邀请统计",
                    description=f"感谢您邀请新用户加入我们的服务器！",
                    color=discord.Color.blue()
                )
                
                embed.add_field(
                    name="总邀请人数",
                    value=f"**{invite_count}** 人",
                    inline=True
                )
                
                embed.add_field(
                    name="已验证邀请人数",
                    value=f"**{verified_invite_count}** 人",
                    inline=True
                )
                
                embed.add_field(
                    name="当前积分",
                    value=f"**{free_escrow_amount:.2f}** ",
                    inline=True
                )
                
                embed.add_field(
                    name="下一次奖励",
                    value=f"再邀请 **{next_reward}** 人获得验证可获得 **{config.INVITE_FREE_ESCROW_AMOUNT}**  积分",
                    inline=False
                )
                
                embed.set_footer(text="注：只有邀请的用户获得验证身份组后才会计入已验证邀请")
                
                # 发送私密回复
                await interaction.followup.send(embed=embed, ephemeral=True)
                logger.info(f"用户 {interaction.user.name} (ID: {user_id}) 查看了自己的邀请统计")
                
            elif custom_id == "refresh_rewards_button":
                # 回复交互以避免超时
                await interaction.response.defer(ephemeral=True)
                
                user_id = interaction.user.id
                
                # 获取用户当前状态
                old_user = await database.get_user(user_id)
                if not old_user:
                    await interaction.followup.send("❌ 你的用户信息不存在，请先使用邀请功能。", ephemeral=True)
                    return
                    
                old_amount = old_user.get('free_escrow_amount', 0.0)
                old_count = old_user.get('free_escrow_count', 0)
                
                # 确保数据类型正确
                try:
                    if isinstance(old_amount, str):
                        old_amount = float(old_amount)
                    else:
                        old_amount = float(old_amount) if old_amount is not None else 0.0
                        
                    if isinstance(old_count, str) and old_count.isdigit():
                        old_count = int(old_count)
                    else:
                        old_count = int(old_count) if old_count is not None else 0
                except (ValueError, TypeError):
                    logger.warning(f"无法转换用户 {user_id} 的原有值")
                    old_amount = 0.0
                    old_count = 0
                
                # 调用积分计算函数
                reward_added = await database.add_free_escrow_for_invites(user_id)
                
                # 获取更新后的状态
                new_user = await database.get_user(user_id)
                if not new_user:
                    await interaction.followup.send("❌ 刷新后无法获取你的用户信息，请联系管理员。", ephemeral=True)
                    return
                    
                new_amount = new_user.get('free_escrow_amount', 0.0)
                new_count = new_user.get('free_escrow_count', 0)
                
                # 确保数据类型正确
                try:
                    if isinstance(new_amount, str):
                        new_amount = float(new_amount)
                    else:
                        new_amount = float(new_amount) if new_amount is not None else 0.0
                        
                    if isinstance(new_count, str) and new_count.isdigit():
                        new_count = int(new_count)
                    else:
                        new_count = int(new_count) if new_count is not None else 0
                except (ValueError, TypeError):
                    logger.warning(f"无法转换用户 {user_id} 的新值")
                    new_amount = old_amount
                    new_count = old_count
                
                # 创建嵌入消息
                embed = discord.Embed(
                    title="🎁 邀请奖励刷新结果",
                    color=discord.Color.green() if new_amount > old_amount else discord.Color.blue()
                )
                
                # 获取邀请统计
                invite_count = await database.get_user_invite_count(user_id)
                verified_invite_count = await database.get_verified_invite_count(user_id)
                
                # 计算下一次奖励需要的邀请数
                threshold = int(config.INVITE_FREE_ESCROW_THRESHOLD)
                next_reward = threshold - (verified_invite_count % threshold) if verified_invite_count % threshold != 0 else threshold
                
                embed.add_field(
                    name="邀请统计",
                    value=f"总邀请: **{invite_count}** 人\n已验证邀请: **{verified_invite_count}** 人",
                    inline=False
                )
                
                embed.add_field(
                    name="奖励更新",
                    value=f"原积分: **{old_amount:.2f}** \n现积分: **{new_amount:.2f}** \n增加积分: **{new_amount - old_amount:.2f}** ",
                    inline=False
                )
                
                if new_amount > old_amount:
                    embed.add_field(
                        name="✅ 奖励发放成功",
                        value=f"你获得了额外的积分！",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="ℹ️ 奖励已是最新",
                        value=f"你已经获得了所有应得的奖励。再邀请 **{next_reward}** 人获得验证可获得 **{config.INVITE_FREE_ESCROW_AMOUNT}**  积分。",
                        inline=False
                    )
                
                await interaction.followup.send(embed=embed, ephemeral=True)
                logger.info(f"用户 {interaction.user.name} (ID: {user_id}) 刷新了邀请奖励，新增积分: {new_amount - old_amount:.2f} ")
                
            elif custom_id == "create_invite_button":
                # 调用创建邀请链接的逻辑
                # 这里复用InviteButtonView中的逻辑，但直接在Social类中实现
                try:
                    user_id = interaction.user.id
                    logger.info(f"用户 {user_id} ({interaction.user.name}) 在排行榜中点击了邀请按钮")
                    
                    # 立即发送初始响应
                    try:
                        await interaction.response.defer(ephemeral=True)
                        logger.info(f"发送了初始响应给用户 {user_id}")
                    except Exception as e:
                        logger.error(f"发送初始响应时出错: {str(e)}")
                        return
                    
                    # 先检查用户是否已有邀请链接
                    try:
                        # 从数据库查询用户现有未使用邀请
                        existing_invites = await database.get_user_invites(user_id)
                        
                        logger.info(f"用户 {user_id} 的现有邀请: {existing_invites}")
                        
                        if existing_invites:
                            # 用户已有未使用的邀请链接，直接返回第一个邀请，不管是否有效
                            invite = existing_invites[0]
                            invite_code = invite.get("invite_code")
                            
                            # 检查邀请码是否为None或空字符串
                            if not invite_code:
                                logger.warning(f"从数据库获取的邀请码为空: {invite}")
                                # 创建新邀请
                                logger.info(f"用户 {user_id} 邀请码无效，将创建新邀请")
                                # 跳过后续逻辑，直接创建新邀请
                            else:
                                # 检查邀请码是否看起来像列名 (数据库错误)
                                if invite_code == "invite_code":
                                    logger.warning(f"邀请码可能是列名而不是实际值: {invite_code}")
                                    # 创建新邀请
                                    logger.info(f"用户 {user_id} 邀请码无效 (可能是列名)，将创建新邀请")
                                    # 跳过后续逻辑，直接创建新邀请
                                else:
                                    # 尝试验证，但不影响返回逻辑
                                    try:
                                        invites = await interaction.guild.invites()
                                        valid_invite = next((inv for inv in invites if inv.code == invite_code), None)
                                        
                                        if valid_invite:
                                            await interaction.followup.send(
                                                f"您已有一个有效的邀请链接: {valid_invite.url}\n"
                                                f"每用户只能创建一个永久邀请链接。每邀请一个新用户加入，您将获得奖励！", 
                                                ephemeral=True
                                            )
                                            logger.info(f"向用户 {user_id} 返回了现有邀请 {valid_invite.url}，拒绝创建新邀请")
                                            return
                                    except Exception as e:
                                        logger.error(f"验证邀请 {invite_code} 时出错: {str(e)}")
                                    
                                    # 如果验证失败或邀请无效，仍然返回该邀请链接
                                    invite_url = f"https://discord.gg/{invite_code}"
                                    await interaction.followup.send(
                                        f"您已有一个邀请链接: {invite_url}\n"
                                        f"每用户只能创建一个永久邀请链接。如果此链接已失效，请联系管理员。", 
                                        ephemeral=True
                                    )
                                    logger.info(f"向用户 {user_id} 返回了现有邀请 {invite_url}，拒绝创建新邀请")
                                    return
                        
                        # 如果没有现有邀请，继续创建新邀请
                        logger.info(f"用户 {user_id} 没有现有邀请，将创建新邀请")
                    except Exception as e:
                        logger.error(f"查询用户邀请时出错: {str(e)}", exc_info=True)
                        # 出错时，保险起见，阻止创建新邀请
                        try:
                            await interaction.followup.send(
                                "验证您的邀请状态时出错，请稍后再试。如需帮助，请联系管理员。", 
                                ephemeral=True
                            )
                            return
                        except Exception:
                            pass
                    
                    # 创建邀请
                    try:
                        channel_id = config.INVITE_CHANNEL_ID
                        # 如果没有配置邀请频道，使用当前频道
                        if channel_id == 0:
                            channel = interaction.channel
                        else:
                            channel = self.bot.get_channel(channel_id)
                            if not channel:
                                logger.warning(f"找不到配置的邀请创建频道 {channel_id}，使用当前频道")
                                channel = interaction.channel
                        
                        logger.info(f"为用户 {user_id} 在频道 {channel.id} 创建邀请")
                        
                        # 创建邀请
                        invite = await channel.create_invite(
                            max_age=0,  # 永久邀请
                            max_uses=0,  # 无使用次数限制
                            unique=True  # 创建新的唯一邀请
                        )
                        
                        logger.info(f"成功创建邀请链接: {invite.url}")
                        
                        # 发送邀请链接给用户
                        try:
                            await interaction.followup.send(
                                f"您的专属邀请链接已创建: {invite.url}\n"
                                f"每用户只能创建一个永久邀请链接。每邀请一个新用户加入，您将获得奖励！", 
                                ephemeral=True
                            )
                            logger.info(f"已发送邀请链接给用户 {user_id}")
                        except Exception as e:
                            logger.error(f"发送邀请链接时出错: {str(e)}")
                            return
                        
                        # 在后台存储邀请信息
                        try:
                            logger.info(f"准备在后台为用户 {user_id} 存储邀请信息")
                            invite_code = str(invite.code)
                            # 直接存储邀请信息，不使用异步任务
                            try:
                                # 检查邀请码是否已存在
                                invite_exists = await database.check_invite_exists(invite_code)
                                if invite_exists:
                                    logger.info(f"邀请码 {invite_code} 已存在于数据库中，跳过保存")
                                else:
                                    # 确保用户存在于users表中
                                    db_user = await database.get_user(user_id)
                                    if not db_user:
                                        logger.info(f"用户 {user_id} ({interaction.user.name}) 不在users表中，正在创建...")
                                        await database.create_user(user_id, interaction.user.name)
                                        logger.info(f"用户 {user_id} ({interaction.user.name}) 已创建。")
                                    
                                    logger.info(f"尝试保存Discord邀请码 {invite_code} 到数据库")
                                    invite_id = await database.create_invite(user_id, invite_code)
                                    logger.info(f"已保存Discord邀请码 {invite_code} 到数据库，ID: {invite_id}")
                                    
                                # 尝试更新邀请排名
                                await self.update_invite_leaderboard()
                            except Exception as e:
                                logger.error(f"存储邀请信息时出错: {str(e)}", exc_info=True)
                        except Exception as e:
                            logger.error(f"创建后台存储任务时出错: {str(e)}", exc_info=True)
                    
                    except Exception as e:
                        logger.error(f"创建邀请链接时出错: {str(e)}", exc_info=True)
                        try:
                            await interaction.followup.send("创建邀请链接时出错，请稍后再试。", ephemeral=True)
                        except:
                            pass
                            
                except Exception as e:
                    logger.error(f"邀请按钮处理过程中出错: {str(e)}", exc_info=True)
                    try:
                        # 尝试发送错误消息
                        if not interaction.response.is_done():
                            await interaction.response.send_message("处理您的请求时出错，请稍后再试。", ephemeral=True)
                        else:
                            await interaction.followup.send("处理您的请求时出错，请稍后再试。", ephemeral=True)
                    except Exception as inner_e:
                        logger.error(f"在处理错误时发生额外错误: {str(inner_e)}")
                        
        except Exception as e:
            logger.error(f"处理交互时出错: {str(e)}", exc_info=True)
            # 尝试发送错误消息
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("查询邀请统计时出错，请稍后再试。", ephemeral=True)
                else:
                    await interaction.followup.send("查询邀请统计时出错，请稍后再试。", ephemeral=True)
            except Exception as send_error:
                logger.error(f"尝试发送错误消息时出错: {str(send_error)}")

    async def update_booster_rewards(self):
        """定期检查并给予服务器boost用户积分的后台任务。"""
        try:
            while not self.bot.is_closed():
                try:
                    logger.info("开始每日检查服务器boost用户积分发放...")
                    
                    # 获取主服务器对象
                    guild = self.bot.get_guild(config.GUILD_ID)
                    if not guild:
                        logger.error(f"无法获取服务器 ID: {config.GUILD_ID}")
                        await asyncio.sleep(3600)  # 1小时后重试
                        continue
                    
                    # 先同步服务器boost用户，确保数据库与实际状态一致
                    await self.sync_server_boosters()
                    
                    # 获取所有应该获得积分的boost用户
                    boosters = await database.get_boosters_due_for_credits()
                    logger.info(f"发现 {len(boosters)} 个需要发放积分的boost用户")
                    
                    # 给每个符合条件的用户发放积分
                    for booster in boosters:
                        user_id = booster['user_id']
                        username = booster['username']
                        
                        # 检查用户是否仍然是boost用户
                        member = guild.get_member(user_id)
                        if not member:
                            logger.warning(f"用户 {user_id} ({username}) 不在服务器中，跳过积分发放")
                            continue
                            
                        # 检查用户是否有boost者身份组
                        if not any(role.id == config.BOOSTER_ROLE_ID for role in member.roles):
                            logger.warning(f"用户 {user_id} ({username}) 不再是boost用户，跳过积分发放")
                            # 标记此用户不再是活跃boost用户
                            await database.execute_query(
                                "UPDATE server_boosts SET is_active = 0 WHERE user_id = %s", 
                                (user_id,)
                            )
                            continue
                        
                        # 再次检查是否已经发放过积分（防止重复发放）
                        last_credit_check = await database.fetch_one(
                            "SELECT last_credits_at FROM server_boosts WHERE user_id = %s",
                            (user_id,)
                        )
                        
                        if last_credit_check and last_credit_check.get('last_credits_at'):
                            last_credit_time = last_credit_check['last_credits_at']
                            days_since_last_credit = (datetime.now() - last_credit_time).days
                            
                            if days_since_last_credit < 30:
                                logger.warning(f"用户 {user_id} ({username}) 最近已获得过积分 ({days_since_last_credit} 天前)，跳过发放")
                                continue
                        
                        # 发放积分 - 修改为更新积分
                        # 先获取当前用户的积分
                        user = await database.get_user(user_id)
                        current_free_escrow = user.get('free_escrow_amount', 0) if user else 0
                        try:
                            # 确保current_free_escrow是浮点数
                            if isinstance(current_free_escrow, decimal.Decimal):
                                current_free_escrow = float(current_free_escrow)
                            elif isinstance(current_free_escrow, str):
                                current_free_escrow = float(current_free_escrow)
                        except (ValueError, TypeError):
                            current_free_escrow = 0.0
                            
                        # 计算新的积分（确保都是浮点数）
                        new_free_escrow = current_free_escrow + float(config.FREE_CREDITS_AMOUNT)
                        
                        # 更新积分
                        await database.update_user_free_escrow_amount(user_id, new_free_escrow)
                        # 更新最后发放时间
                        await database.update_booster_last_credits(user_id)
                        
                        logger.info(f"向用户 {user_id} ({username}) 发放了 {config.FREE_CREDITS_AMOUNT} 积分，总积分: {new_free_escrow}")
                        
                        # 尝试给用户发送DM通知
                        try:
                            embed = discord.Embed(
                                title="🎁 收到积分！",
                                description=f"感谢您对服务器的boost支持！\n\n您已收到 **{config.FREE_CREDITS_AMOUNT}** 的积分作为感谢。",
                                color=discord.Color.nitro_pink()
                            )
                            embed.add_field(name="下次发放", value="下一次积分将在30天后发放。", inline=False)
                            embed.set_footer(text=f"当前总积分: {new_free_escrow}")
                            
                            # 记录发送DM前的日志
                            logger.info(f"尝试向用户 {user_id} ({username}) 发送定期boost奖励DM通知...")
                            
                            # 检查是否可以发送DM
                            try:
                                can_dm = member.dm_channel is not None or await member.create_dm() is not None
                                if not can_dm:
                                    logger.warning(f"无法创建与用户 {user_id} ({username}) 的DM频道")
                            except Exception as e:
                                logger.warning(f"检查DM权限时出错: {str(e)}")
                            
                            # 发送DM
                            await member.send(embed=embed)
                            logger.info(f"成功向用户 {user_id} ({username}) 发送了定期boost奖励通知DM")
                        except discord.Forbidden:
                            logger.warning(f"无法向用户 {user_id} ({username}) 发送定期boost奖励DM通知: 权限不足(可能是用户关闭了DM)")
                        except discord.HTTPException as e:
                            logger.warning(f"向用户 {user_id} ({username}) 发送定期boost奖励DM通知时发生HTTP错误: {str(e)}")
                        except Exception as e:
                            logger.warning(f"无法向用户 {user_id} ({username}) 发送DM通知: {str(e)}", exc_info=True)
                    
                    logger.info("服务器boost用户积分检查完成")
                    await asyncio.sleep(24 * 3600)  # 每24小时检查一次
                    
                except Exception as e:
                    logger.error(f"更新服务器boost奖励时出错: {str(e)}", exc_info=True)
                    await asyncio.sleep(1800)  # 30分钟后重试
        
        except Exception as e:
            logger.error(f"更新服务器boost奖励时出错: {str(e)}", exc_info=True)
            await asyncio.sleep(1800)  # 30分钟后重试
    
    @discord.slash_command(
        name="查询积分",
        description="查看你的Server BOOST状态和积分"
    )
    async def check_boost_credits_slash(self, ctx: ApplicationContext):
        """查看当前服务器boost状态和积分。"""
        user_id = ctx.author.id
        
        # 获取用户的boost信息
        query = "SELECT * FROM server_boosts WHERE user_id = %s"
        boost_info = await database.fetch_one(query, (user_id,))
        
        # 获取用户的积分
        user = await database.get_user(user_id)
        free_escrow_amount = 0.0
        if user:
            try:
                free_escrow_amount = float(user.get('free_escrow_amount', 0))
            except (ValueError, TypeError):
                free_escrow_amount = 0.0
        
        embed = discord.Embed(
            title="🚀 服务器boost状态",
            color=discord.Color.nitro_pink()
        )
        
        # 检查用户是否是boost用户
        is_booster = False
        if ctx.guild:  # 添加对ctx.guild的检查
            member = ctx.guild.get_member(user_id)
            if member:
                is_booster = any(role.id == config.BOOSTER_ROLE_ID for role in member.roles)
        else:
            logger.warning(f"无法检查用户 {user_id} 的boost状态: guild对象为None")
        
        if is_booster:
            embed.description = "✅ 您当前是服务器的boost用户！"
            
            if boost_info:
                # 计算下次获得积分的时间
                last_credits_at = boost_info.get('last_credits_at')
                if last_credits_at:
                    next_credits_date = last_credits_at + timedelta(days=30)
                    days_left = (next_credits_date - datetime.now()).days
                    
                    embed.add_field(
                        name="boost状态", 
                        value=f"首次boost: {boost_info['first_boost_at'].strftime('%Y-%m-%d')}\n"
                              f"boost次数: {boost_info['boost_count']}",
                        inline=False
                    )
                    
                    embed.add_field(
                        name="积分状态", 
                        value=f"上次获得: {last_credits_at.strftime('%Y-%m-%d')}\n"
                              f"下次获得: {next_credits_date.strftime('%Y-%m-%d')} (还有 {days_left} 天)",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="积分状态", 
                        value="您还未获得过积分，系统将很快发放！",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="积分状态", 
                    value="系统将很快为您注册并发放积分！",
                    inline=False
                )
        else:
            embed.description = "❌ 您当前不是服务器的boost用户"
            embed.add_field(
                name="如何获得积分", 
                value=f"成为服务器boost用户后，您每30天可获得 **{config.FREE_CREDITS_AMOUNT}** 的积分！",
                inline=False
            )
        
        embed.add_field(
            name="当前积分", 
            value=f"**{free_escrow_amount}**",
            inline=False
        )
        
        await ctx.respond(embed=embed, ephemeral=True)

    async def sync_server_boosters(self):
        """同步服务器的boost用户到数据库。"""
        try:
            logger.info("开始同步服务器boost用户...")
            
            # 获取主服务器对象
            guild = self.bot.get_guild(config.GUILD_ID)
            if not guild:
                logger.error(f"无法获取服务器 ID: {config.GUILD_ID}")
                return
                
            # 获取boost用户身份组
            booster_role = guild.get_role(config.BOOSTER_ROLE_ID)
            if not booster_role:
                logger.error(f"无法获取boost者身份组 ID: {config.BOOSTER_ROLE_ID}")
                return
                
            # 获取所有具有boost者身份组的成员
            boosters = [member for member in guild.members if booster_role in member.roles]
            logger.info(f"服务器中有 {len(boosters)} 个boost用户")
            
            # 获取数据库中的所有boost记录
            query = "SELECT * FROM server_boosts"
            db_boosters = await database.fetch_all(query)
            db_booster_ids = {booster['user_id'] for booster in db_boosters} if db_boosters else set()
            
            # 记录当前在线的boost用户
            for booster in boosters:
                # 确保用户在users表中存在
                await database.get_user(booster.id) or await database.create_user(booster.id, booster.name)
                
                # 如果用户不在boost记录中，添加记录
                if booster.id not in db_booster_ids:
                    logger.info(f"添加新的boost用户记录: {booster.id} ({booster.name})")
                    await database.record_server_boost(booster.id)
                else:
                    # 确保用户标记为活跃
                    await database.execute_query(
                        "UPDATE server_boosts SET is_active = 1 WHERE user_id = %s",
                        (booster.id,)
                    )
            
            # 更新数据库中但不再是boost用户的记录
            current_booster_ids = {booster.id for booster in boosters}
            for db_id in db_booster_ids:
                if db_id not in current_booster_ids:
                    logger.info(f"用户 {db_id} 不再是boost用户，更新状态")
                    await database.execute_query(
                        "UPDATE server_boosts SET is_active = 0 WHERE user_id = %s",
                        (db_id,)
                    )
            
            logger.info("服务器boost用户同步完成")
            
            # 立即运行一次奖励发放
            await self.update_booster_rewards_once()
            
        except Exception as e:
            logger.error(f"同步服务器boost用户时出错: {str(e)}", exc_info=True)

    async def update_booster_rewards_once(self):
        """执行一次服务器boost奖励更新。"""
        FREE_CREDITS_AMOUNT = float(config.FREE_CREDITS_AMOUNT)  # 确保是浮点数
        
        try:
            logger.info("开始执行一次性服务器boost奖励发放")
            
            # 获取所有应该获得积分的boost用户
            boosters = await database.get_boosters_due_for_credits()
            logger.info(f"发现 {len(boosters)} 个需要发放积分的boost用户")
            
            # 获取主服务器对象
            guild = self.bot.get_guild(config.GUILD_ID)
            if not guild:
                logger.error(f"无法获取服务器 ID: {config.GUILD_ID}")
                return
                
            # 获取boost者身份组
            booster_role = guild.get_role(config.BOOSTER_ROLE_ID)
            if not booster_role:
                logger.error(f"无法获取boost者身份组 ID: {config.BOOSTER_ROLE_ID}")
                return
                
            # 获取所有当前的boost用户但还没有在数据库中记录的
            current_boosters = {member.id for member in guild.members if booster_role in member.roles}
            
            # 获取数据库中的所有活跃boost用户
            query = "SELECT user_id FROM server_boosts WHERE is_active = 1"
            db_active_boosters = await database.fetch_all(query)
            db_booster_ids = {booster['user_id'] for booster in db_active_boosters} if db_active_boosters else set()
            
            # 找出那些是当前boost用户但不在活跃记录中的用户
            missing_boosters = current_boosters - db_booster_ids
            if missing_boosters:
                logger.info(f"发现 {len(missing_boosters)} 个当前boost用户不在活跃记录中")
                for user_id in missing_boosters:
                    member = guild.get_member(user_id)
                    if member:
                        logger.info(f"同步boost用户 {user_id} ({member.name}) 的记录")
                        # 确保用户在users表中
                        await database.get_user(user_id) or await database.create_user(user_id, member.name)
                        # 记录boost信息
                        await database.record_server_boost(user_id)
                        # 更新活跃状态
                        await database.execute_query(
                            "UPDATE server_boosts SET is_active = 1 WHERE user_id = %s", 
                            (user_id,)
                        )
                
                # 重新获取应该获得积分的boost用户
                boosters = await database.get_boosters_due_for_credits()
                logger.info(f"同步后发现 {len(boosters)} 个需要发放积分的boost用户")
            
            # 给每个符合条件的用户发放积分
            for booster in boosters:
                user_id = booster['user_id']
                username = booster['username']
                
                # 检查用户是否仍然是boost用户
                member = guild.get_member(user_id)
                if not member:
                    logger.warning(f"用户 {user_id} ({username}) 不在服务器中，跳过积分发放")
                    continue
                    
                # 检查用户是否有boost者身份组
                if not any(role.id == config.BOOSTER_ROLE_ID for role in member.roles):
                    logger.warning(f"用户 {user_id} ({username}) 不再是boost用户，跳过积分发放")
                    # 标记此用户不再是活跃boost用户
                    await database.execute_query(
                        "UPDATE server_boosts SET is_active = 0 WHERE user_id = %s", 
                        (user_id,)
                    )
                    continue
                
                # 再次检查是否已经发放过积分（防止重复发放）
                last_credit_check = await database.fetch_one(
                    "SELECT last_credits_at FROM server_boosts WHERE user_id = %s",
                    (user_id,)
                )
                
                if last_credit_check and last_credit_check.get('last_credits_at'):
                    last_credit_time = last_credit_check['last_credits_at']
                    days_since_last_credit = (datetime.now() - last_credit_time).days
                    
                    if days_since_last_credit < 30:
                        logger.warning(f"用户 {user_id} ({username}) 最近已获得过积分 ({days_since_last_credit} 天前)，跳过发放")
                        continue
                
                # 发放积分
                # 先获取当前用户的积分
                user = await database.get_user(user_id)
                current_free_escrow = user.get('free_escrow_amount', 0) if user else 0
                try:
                    # 确保current_free_escrow是浮点数
                    if isinstance(current_free_escrow, decimal.Decimal):
                        current_free_escrow = float(current_free_escrow)
                    elif isinstance(current_free_escrow, str):
                        current_free_escrow = float(current_free_escrow)
                except (ValueError, TypeError):
                    current_free_escrow = 0.0
                    
                # 计算新的积分（确保都是浮点数）
                new_free_escrow = current_free_escrow + FREE_CREDITS_AMOUNT
                
                # 更新积分
                await database.update_user_free_escrow_amount(user_id, new_free_escrow)
                # 更新最后发放时间
                await database.update_booster_last_credits(user_id)
                
                logger.info(f"向用户 {user_id} ({username}) 发放了 {FREE_CREDITS_AMOUNT} 积分，总积分: {new_free_escrow}")
                
                # 尝试给用户发送DM通知
                try:
                    embed = discord.Embed(
                        title="🎁 收到积分！",
                        description=f"感谢您对服务器的boost支持！\n\n您已收到 **{FREE_CREDITS_AMOUNT}** 的积分作为感谢。",
                        color=discord.Color.nitro_pink()
                    )
                    embed.add_field(name="下次发放", value="下一次积分将在30天后发放。", inline=False)
                    embed.set_footer(text=f"当前总积分: {new_free_escrow}")
                    
                    # 记录发送DM前的日志
                    logger.info(f"尝试向用户 {user_id} ({username}) 发送定期boost奖励DM通知...")
                    
                    # 检查是否可以发送DM
                    try:
                        can_dm = member.dm_channel is not None or await member.create_dm() is not None
                        if not can_dm:
                            logger.warning(f"无法创建与用户 {user_id} ({username}) 的DM频道")
                    except Exception as e:
                        logger.warning(f"检查DM权限时出错: {str(e)}")
                    
                    # 发送DM
                    await member.send(embed=embed)
                    logger.info(f"成功向用户 {user_id} ({username}) 发送了定期boost奖励通知DM")
                except discord.Forbidden:
                    logger.warning(f"无法向用户 {user_id} ({username}) 发送定期boost奖励DM通知: 权限不足(可能是用户关闭了DM)")
                except discord.HTTPException as e:
                    logger.warning(f"向用户 {user_id} ({username}) 发送定期boost奖励DM通知时发生HTTP错误: {str(e)}")
                except Exception as e:
                    logger.warning(f"无法向用户 {user_id} ({username}) 发送DM通知: {str(e)}", exc_info=True)
            
            logger.info("一次性服务器boost用户积分检查完成")
                
        except Exception as e:
            logger.error(f"一次性更新服务器boost奖励时出错: {str(e)}", exc_info=True)

    @discord.slash_command(
        name="检查发放助力积分",
        description="立即检查并向符合条件的boost用户发放积分（仅限管理员）",
        guild_ids=[config.GUILD_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    async def check_and_grant_boost_credits(self, ctx: ApplicationContext):
        """立即检查并向符合条件的boost用户发放积分。"""
        # 权限检查
        if not ctx.author.guild_permissions.administrator and not any(role.id in [config.ADMIN_ROLE_ID, config.MODERATOR_ROLE_ID] for role in ctx.author.roles):
            await ctx.respond("您没有权限执行此命令", ephemeral=True)
            return
        
        await ctx.defer(ephemeral=True)
        
        try:
            logger.info(f"管理员 {ctx.author.id} ({ctx.author.name}) 手动触发了boost用户积分检查")
            
            # 同步服务器boost用户
            await self.sync_server_boosters()
            
            # 执行一次性的boost奖励发放
            await self.update_booster_rewards_once()
            
            await ctx.followup.send("✅ 已成功检查并发放符合条件的boost用户积分！", ephemeral=True)
        except Exception as e:
            logger.error(f"手动检查boost用户积分时出错: {str(e)}", exc_info=True)
            await ctx.followup.send(f"❌ 检查过程中出错: {str(e)}", ephemeral=True)

def setup(bot):
    """加载社交功能组件。"""
    try:
        # 创建cog实例
        cog = Social(bot)
        
        # 添加cog到机器人
        bot.add_cog(cog)
        
        # 注册斜杠命令组
        bot.add_application_command(cog.invite_group)
        
        logger.info("Social组件已加载")
    except Exception as e:
        logger.error(f"加载Social组件时出错: {e}")
    return
