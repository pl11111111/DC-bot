import discord
from discord import SlashCommandGroup, Option, ApplicationContext
from discord.ext import commands, tasks
import logging
import asyncio
import re
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import uuid
import config
from utils import database, redis_client, helpers

# 自定义日志过滤器，过滤掉高频且不重要的日志
class GiveawayLogFilter(logging.Filter):
    def filter(self, record):
        # 忽略一些高频出现的不重要日志
        if any(msg in record.getMessage() for msg in [
            "正在检查活跃抽奖以确保连接...",
            "完成检查，已处理 0 个活跃抽奖"
        ]):
            return False
        return True

# 配置日志
logger = logging.getLogger(__name__)
# 添加过滤器
logger.addFilter(GiveawayLogFilter())

# 时间格式正则表达式
TIME_PATTERN = re.compile(r'^(\d+)([HhDdMm])$')

class GiveawayView(discord.ui.View):
    """抽奖参与视图"""
    
    def __init__(self, bot, giveaway_id, end_time):
        super().__init__(timeout=None)  # 永久视图
        self.bot = bot
        self.giveaway_id = giveaway_id
        self.end_time = end_time
        self.participants_count = 0
        
        # 创建初始按钮
        self.join_button = discord.ui.Button(
            emoji="🎉", 
            style=discord.ButtonStyle.primary, 
            custom_id="giveaway_join"
        )
        self.join_button.callback = self.join_button_callback
        self.add_item(self.join_button)
        
        # 更新按钮显示
        self.update_button_label()
    
    def update_button_label(self):
        """更新按钮标签显示参与人数"""
        if self.participants_count > 0:
            self.join_button.label = str(self.participants_count)
        else:
            self.join_button.label = None
    
    async def join_button_callback(self, interaction):
        """用户参与抽奖的按钮"""
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(self.giveaway_id)
            if not giveaway:
                await interaction.response.send_message("此抽奖不存在或已结束", ephemeral=True)
                return
                
            # 检查抽奖是否已结束
            if giveaway["status"] != "active":
                await interaction.response.send_message("此抽奖已结束", ephemeral=True)
                return
                
            # 检查用户是否已参与
            if await database.has_user_joined_giveaway(self.giveaway_id, interaction.user.id):
                await interaction.response.send_message("您已经参与了此抽奖", ephemeral=True)
                return
                
            # 检查身份组限制
            if giveaway["role_ids"]:
                role_ids = giveaway["role_ids"].split(",")
                user_roles = [str(role.id) for role in interaction.user.roles]
                if not any(role_id in user_roles for role_id in role_ids):
                    role_mentions = ", ".join([f"<@&{role_id}>" for role_id in role_ids])
                    await interaction.response.send_message(f"您没有参与此抽奖所需的身份组: {role_mentions}", ephemeral=True)
                    return
            
            # 检查积分需求
            if giveaway["credit_requirement"] > 0:
                # 获取用户积分
                user_credits = await database.get_user_credits(interaction.user.id)
                
                if user_credits < giveaway["credit_requirement"]:
                    await interaction.response.send_message(
                        f"您的积分不足。需要 {giveaway['credit_requirement']} 积分，您当前有 {user_credits} 积分",
                        ephemeral=True
                    )
                    return
                
                # 创建确认视图
                confirm_view = discord.ui.View(timeout=60)  # 60秒超时
                
                # 创建确认按钮
                async def confirm_callback(confirm_interaction):
                    try:
                        # 再次检查用户是否已参与，防止重复点击
                        if await database.has_user_joined_giveaway(self.giveaway_id, interaction.user.id):
                            await confirm_interaction.response.send_message("您已经参与了此抽奖", ephemeral=True)
                            return
                            
                        # 禁用确认和取消按钮，防止重复点击
                        for child in confirm_view.children:
                            child.disabled = True
                        await confirm_interaction.response.edit_message(view=confirm_view)
                            
                        # 扣除积分
                        try:
                            await database.add_user_credits(interaction.user.id, -giveaway["credit_requirement"])
                        except Exception as e:
                            logger.error(f"扣除用户 {interaction.user.id} 的积分时出错: {e}")
                            await confirm_interaction.followup.send("扣除积分时出错，请稍后重试", ephemeral=True)
                            return
                        
                        # 记录用户参与
                        try:
                            await database.add_giveaway_participant(
                                self.giveaway_id, 
                                interaction.user.id, 
                                giveaway["credit_requirement"]
                            )
                        except Exception as e:
                            logger.error(f"记录用户 {interaction.user.id} 参与抽奖 {self.giveaway_id} 时出错: {e}")
                            # 尝试退还积分
                            try:
                                await database.add_user_credits(interaction.user.id, giveaway["credit_requirement"])
                                await confirm_interaction.followup.send("参与抽奖时出错，已退还扣除的积分", ephemeral=True)
                            except:
                                await confirm_interaction.followup.send("参与抽奖时出错，请联系管理员手动退还积分", ephemeral=True)
                            return
                        
                        # 更新参与人数
                        self.participants_count += 1
                        self.update_button_label()
                        
                        # 尝试获取并更新原始抽奖消息
                        try:
                            channel = confirm_interaction.guild.get_channel(giveaway["channel_id"])
                            if channel:
                                try:
                                    original_message = await channel.fetch_message(giveaway["message_id"])
                                    if original_message:
                                        await original_message.edit(view=self)
                                except discord.NotFound:
                                    logger.warning(f"无法找到抽奖消息 {giveaway['message_id']} 进行更新")
                                except Exception as e:
                                    logger.error(f"更新抽奖消息时出错: {str(e)}")
                        except Exception as e:
                            logger.error(f"获取抽奖频道出错: {str(e)}")
                        
                        # 使用followup而不是response.send_message，因为我们已经用了edit_message
                        await confirm_interaction.followup.send(
                            f"您已成功参与抽奖！已扣除 {giveaway['credit_requirement']} 积分。如果中奖，积分将返还给抽奖发起人。",
                            ephemeral=True
                        )
                    except discord.errors.InteractionResponded:
                        # 如果交互已经响应过，使用followup
                        await confirm_interaction.followup.send(
                            f"您已成功参与抽奖！已扣除 {giveaway['credit_requirement']} 积分。如果中奖，积分将返还给抽奖发起人。",
                            ephemeral=True
                        )
                    except Exception as e:
                        logger.error(f"确认参与抽奖时出错: {str(e)}", exc_info=True)
                        try:
                            await confirm_interaction.followup.send("参与抽奖时出错，请稍后再试", ephemeral=True)
                        except:
                            pass
                
                # 创建取消按钮
                async def cancel_callback(cancel_interaction):
                    try:
                        # 禁用确认和取消按钮，防止重复点击
                        for child in confirm_view.children:
                            child.disabled = True
                        await cancel_interaction.response.edit_message(content="您已取消参与此抽奖", view=confirm_view)
                    except Exception as e:
                        logger.error(f"取消参与抽奖时出错: {str(e)}", exc_info=True)
                        try:
                            await cancel_interaction.followup.send("取消操作时出错，请稍后再试", ephemeral=True)
                        except:
                            pass
                
                # 添加按钮到视图
                confirm_button = discord.ui.Button(label="确认参与", style=discord.ButtonStyle.primary)
                confirm_button.callback = confirm_callback
                
                cancel_button = discord.ui.Button(label="取消", style=discord.ButtonStyle.secondary)
                cancel_button.callback = cancel_callback
                
                confirm_view.add_item(confirm_button)
                confirm_view.add_item(cancel_button)
                
                await interaction.response.send_message(
                    f"参与此抽奖需要 {giveaway['credit_requirement']} 积分。确认参与吗？",
                    view=confirm_view,
                    ephemeral=True
                )
            else:
                # 无需积分直接参与
                try:
                    await database.add_giveaway_participant(self.giveaway_id, interaction.user.id, 0)
                    
                    # 更新参与人数
                    self.participants_count += 1
                    self.update_button_label()
                    
                    # 尝试获取并更新原始抽奖消息
                    try:
                        channel = interaction.guild.get_channel(giveaway["channel_id"])
                        if channel:
                            try:
                                original_message = await channel.fetch_message(giveaway["message_id"])
                                if original_message:
                                    await original_message.edit(view=self)
                            except discord.NotFound:
                                logger.warning(f"无法找到抽奖消息 {giveaway['message_id']} 进行更新")
                            except Exception as e:
                                logger.error(f"更新抽奖消息时出错: {str(e)}")
                    except Exception as e:
                        logger.error(f"获取抽奖频道出错: {str(e)}")
                    
                    await interaction.response.send_message("您已成功参与抽奖！", ephemeral=True)
                except Exception as e:
                    logger.error(f"用户参与抽奖时出错: {e}")
                    await interaction.response.send_message("参与抽奖时出错，请稍后再试", ephemeral=True)
                
        except discord.errors.InteractionResponded:
            # 如果交互已经响应过，忽略
            pass
        except Exception as e:
            logger.error(f"参与抽奖出错: {str(e)}", exc_info=True)
            try:
                await interaction.response.send_message("参与抽奖时出错，请稍后再试", ephemeral=True)
            except:
                # 如果已经响应，使用followup
                try:
                    await interaction.followup.send("参与抽奖时出错，请稍后再试", ephemeral=True)
                except:
                    pass


class GiveawayPreviewView(discord.ui.View):
    """抽奖预览视图，用于修改和发布抽奖"""
    
    def __init__(self, bot, author_id, giveaway_data):
        super().__init__(timeout=600)  # 10分钟超时
        self.bot = bot
        self.author_id = author_id
        self.giveaway_data = giveaway_data
    
    @discord.ui.button(label="修改", style=discord.ButtonStyle.primary)
    async def edit_button(self, button, interaction):
        """修改抽奖信息"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有抽奖发起人可以修改", ephemeral=True)
            return
            
        # 创建选择菜单
        select_menu = discord.ui.Select(
            placeholder="请选择要修改的内容",
            options=[
                discord.SelectOption(label="奖品名称", value="prize", description="修改奖品名称", emoji="🎁"),
                discord.SelectOption(label="抽奖时间", value="time", description="修改抽奖持续时间", emoji="⏱️"),
                discord.SelectOption(label="获奖人数", value="winners", description="修改获奖者数量", emoji="👥"),
                discord.SelectOption(label="奖品图片", value="image", description="修改奖品图片", emoji="🖼️"),
                discord.SelectOption(label="身份组限制", value="role", description="修改参与所需身份组", emoji="🔒"),
                discord.SelectOption(label="积分", value="credits", description="修改参与所需积分", emoji="💰"),
            ]
        )
        
        # 创建修改菜单视图
        edit_view = discord.ui.View(timeout=300)  # 5分钟超时
        
        # 添加"返回"按钮
        back_button = discord.ui.Button(label="返回预览", style=discord.ButtonStyle.secondary)
        
        async def back_button_callback(back_interaction):
            # 重新创建预览嵌入消息
            embed = create_giveaway_embed(
                prize_name=self.giveaway_data["prize_name"],
                winners_count=self.giveaway_data["winners_count"],
                end_time=datetime.now() + self.giveaway_data["duration"],
                host=interaction.user,
                giveaway_id="PREVIEW",
                participants_count=0,
                role_ids=self.giveaway_data.get("role_ids", []),
                credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                prize_image=self.giveaway_data.get("prize_image", "")
            )
            
            # 编辑回原始预览消息
            await back_interaction.response.edit_message(
                content="📝 **抽奖预览**\n请检查以下信息，确认无误后点击「发布」按钮:",
                embed=embed,
                view=self
            )
            
        back_button.callback = back_button_callback
        edit_view.add_item(back_button)
        
        # 编辑菜单回调
        async def select_callback(select_interaction):
            selected_option = select_interaction.data["values"][0]
            
            # 根据选择的选项创建不同的修改界面
            if selected_option == "prize":
                # 创建奖品名称输入模态框
                modal = discord.ui.Modal(title="修改奖品名称")
                
                prize_input = discord.ui.InputText(
                    label="奖品名称",
                    style=discord.InputTextStyle.short,
                    placeholder="输入奖品名称",
                    value=self.giveaway_data["prize_name"],
                    required=True
                )
                
                modal.add_item(prize_input)
                
                async def modal_submit(modal_interaction):
                    # 验证和清理奖品名称
                    raw_prize_name = prize_input.value
                    if not raw_prize_name or len(raw_prize_name.strip()) == 0:
                        await modal_interaction.response.send_message("奖品名称不能为空", ephemeral=True)
                        return
                        
                    # 清理并验证奖品名称
                    sanitized_prize_name = helpers.sanitize_input(raw_prize_name)
                    if sanitized_prize_name != raw_prize_name:
                        logger.warning(f"奖品名称已被清理: {raw_prize_name} -> {sanitized_prize_name}")
                    
                    # 更新奖品名称
                    self.giveaway_data["prize_name"] = sanitized_prize_name
                    
                    # 更新预览
                    embed = create_giveaway_embed(
                        prize_name=self.giveaway_data["prize_name"],
                        winners_count=self.giveaway_data["winners_count"],
                        end_time=datetime.now() + self.giveaway_data["duration"],
                        host=interaction.user,
                        giveaway_id="PREVIEW",
                        participants_count=0,
                        role_ids=self.giveaway_data.get("role_ids", []),
                        credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                        prize_image=self.giveaway_data.get("prize_image", "")
                    )
                    
                    # 发送更新后的预览
                    await modal_interaction.response.edit_message(
                        content="📝 **抽奖预览 (已更新)**\n请继续修改或返回预览界面:",
                        embed=embed,
                        view=edit_view
                    )
                
                modal.callback = modal_submit
                await select_interaction.response.send_modal(modal)
                
            elif selected_option == "time":
                # 创建时间输入模态框
                modal = discord.ui.Modal(title="修改抽奖时间")
                
                # 显示当前时间
                current_duration = self.giveaway_data["duration"]
                current_time_str = ""
                if current_duration.days > 0:
                    current_time_str = f"{current_duration.days}D"
                elif current_duration.seconds // 3600 > 0:
                    current_time_str = f"{current_duration.seconds // 3600}H"
                else:
                    current_time_str = f"{current_duration.seconds // 60}M"
                
                time_input = discord.ui.InputText(
                    label="抽奖持续时间",
                    style=discord.InputTextStyle.short,
                    placeholder="例如: 1H (1小时), 1D (1天), 30M (30分钟)",
                    value=current_time_str,
                    required=True
                )
                
                modal.add_item(time_input)
                
                async def time_modal_submit(modal_interaction):
                    # 解析新时间
                    time_value = helpers.sanitize_input(time_input.value)
                    new_duration = parse_time(time_value)
                    if not new_duration:
                        await modal_interaction.response.send_message(
                            "无效的时间格式。请使用例如 1H (1小时), 1D (1天), 30M (30分钟) 的格式。",
                            ephemeral=True
                        )
                        return
                    
                    # 更新时间
                    self.giveaway_data["duration"] = new_duration
                    
                    # 更新预览
                    embed = create_giveaway_embed(
                        prize_name=self.giveaway_data["prize_name"],
                        winners_count=self.giveaway_data["winners_count"],
                        end_time=datetime.now() + self.giveaway_data["duration"],
                        host=interaction.user,
                        giveaway_id="PREVIEW",
                        participants_count=0,
                        role_ids=self.giveaway_data.get("role_ids", []),
                        credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                        prize_image=self.giveaway_data.get("prize_image", "")
                    )
                    
                    # 发送更新后的预览
                    await modal_interaction.response.edit_message(
                        content="📝 **抽奖预览 (已更新)**\n请继续修改或返回预览界面:",
                        embed=embed,
                        view=edit_view
                    )
                
                modal.callback = time_modal_submit
                await select_interaction.response.send_modal(modal)
                
            elif selected_option == "winners":
                # 创建获奖人数输入模态框
                modal = discord.ui.Modal(title="修改获奖人数")
                
                winners_input = discord.ui.InputText(
                    label="获奖者数量",
                    style=discord.InputTextStyle.short,
                    placeholder="输入正整数",
                    value=str(self.giveaway_data["winners_count"]),
                    required=True
                )
                
                modal.add_item(winners_input)
                
                async def winners_modal_submit(modal_interaction):
                    try:
                        raw_value = helpers.sanitize_input(winners_input.value)
                        
                        # 检查是否是有效的整数格式（不接受科学计数法）
                        if not is_valid_integer(raw_value):
                            await modal_interaction.response.send_message("请输入普通整数，不支持科学计数法 (如 1e3)", ephemeral=True)
                            return
                            
                        # 尝试将输入转换为整数
                        new_winners = int(raw_value)
                        
                        # 验证合理范围
                        if new_winners < 1:
                            await modal_interaction.response.send_message("获奖人数必须大于0", ephemeral=True)
                            return
                        
                        if new_winners > 100:
                            await modal_interaction.response.send_message("获奖人数不能超过100人", ephemeral=True)
                            return
                            
                        # 更新获奖人数
                        self.giveaway_data["winners_count"] = new_winners
                        
                        # 更新预览
                        embed = create_giveaway_embed(
                            prize_name=self.giveaway_data["prize_name"],
                            winners_count=self.giveaway_data["winners_count"],
                            end_time=datetime.now() + self.giveaway_data["duration"],
                            host=interaction.user,
                            giveaway_id="PREVIEW",
                            participants_count=0,
                            role_ids=self.giveaway_data.get("role_ids", []),
                            credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                            prize_image=self.giveaway_data.get("prize_image", "")
                        )
                        
                        # 发送更新后的预览
                        await modal_interaction.response.edit_message(
                            content="📝 **抽奖预览 (已更新)**\n请继续修改或返回预览界面:",
                            embed=embed,
                            view=edit_view
                        )
                    except ValueError:
                        await modal_interaction.response.send_message("请输入有效的正整数", ephemeral=True)
                
                modal.callback = winners_modal_submit
                await select_interaction.response.send_modal(modal)
                
            elif selected_option == "image":
                # 图片需要重新上传，通知用户
                await select_interaction.response.send_message(
                    "要修改图片，请重新创建抽奖并上传新图片。Discord不支持在此界面上传图片。",
                    ephemeral=True
                )
                
            elif selected_option == "credits":
                # 创建积分输入模态框
                modal = discord.ui.Modal(title="修改积分要求")
                
                credits_input = discord.ui.InputText(
                    label="所需积分",
                    style=discord.InputTextStyle.short,
                    placeholder="输入所需积分 (0表示不需要积分)",
                    value=str(self.giveaway_data.get("credit_requirement", 0)),
                    required=True
                )
                
                modal.add_item(credits_input)
                
                async def credits_modal_submit(modal_interaction):
                    try:
                        raw_value = helpers.sanitize_input(credits_input.value)
                        
                        # 检查是否是有效的整数格式（不接受科学计数法）
                        if not is_valid_integer(raw_value):
                            await modal_interaction.response.send_message("请输入普通整数，不支持科学计数法 (如 1e3)", ephemeral=True)
                            return
                            
                        # 尝试将输入转换为整数
                        new_credits = int(raw_value)
                        
                        # 验证合理范围
                        if new_credits < 0:
                            await modal_interaction.response.send_message("积分不能为负数", ephemeral=True)
                            return
                            
                        if new_credits > 10000:
                            await modal_interaction.response.send_message("积分不能超过10,000 U", ephemeral=True)
                            return
                            
                        # 更新积分
                        self.giveaway_data["credit_requirement"] = new_credits
                        
                        # 更新预览
                        embed = create_giveaway_embed(
                            prize_name=self.giveaway_data["prize_name"],
                            winners_count=self.giveaway_data["winners_count"],
                            end_time=datetime.now() + self.giveaway_data["duration"],
                            host=interaction.user,
                            giveaway_id="PREVIEW",
                            participants_count=0,
                            role_ids=self.giveaway_data.get("role_ids", []),
                            credit_requirement=self.giveaway_data["credit_requirement"],
                            prize_image=self.giveaway_data.get("prize_image", "")
                        )
                        
                        # 发送更新后的预览
                        await modal_interaction.response.edit_message(
                            content="📝 **抽奖预览 (已更新)**\n请继续修改或返回预览界面:",
                            embed=embed,
                            view=edit_view
                        )
                    except ValueError:
                        await modal_interaction.response.send_message("请输入有效的非负整数", ephemeral=True)
                
                modal.callback = credits_modal_submit
                await select_interaction.response.send_modal(modal)
                
            elif selected_option == "role":
                # 为角色创建一个新的视图
                role_view = discord.ui.View(timeout=180)
                
                # 获取可用角色
                guild_roles = [role for role in interaction.guild.roles 
                              if not role.is_default() 
                              and role.name != "@everyone"
                              and not role.managed]
                
                # 限制展示的角色数量，Discord选择菜单最多支持25个选项
                max_roles = min(25, len(guild_roles))
                
                # 创建角色选择器，不使用RoleSelect而是使用标准Select
                options = []
                
                # 获取当前已选的角色ID列表
                current_role_ids = self.giveaway_data.get("role_ids", [])
                
                # 为每个角色创建一个选项
                for role in guild_roles[:max_roles]:
                    # 检查是否已经选中
                    is_selected = role.id in current_role_ids
                    
                    options.append(
                        discord.SelectOption(
                            label=role.name,
                            value=str(role.id),
                            description=f"ID: {role.id}",
                            emoji="✅" if is_selected else None,
                            default=is_selected
                        )
                    )
                
                role_select = discord.ui.Select(
                    placeholder="选择参与所需的身份组",
                    min_values=0,
                    max_values=max_roles,
                    options=options
                )
                
                # 添加确认按钮
                confirm_button = discord.ui.Button(label="确认修改", style=discord.ButtonStyle.success)
                
                # 添加返回按钮
                role_back_button = discord.ui.Button(label="返回上一级", style=discord.ButtonStyle.secondary)
                
                # 先添加元素到视图
                role_view.add_item(role_select)
                role_view.add_item(confirm_button)
                role_view.add_item(role_back_button)
                
                # 先发送消息，然后再设置回调函数
                await select_interaction.response.edit_message(
                    content="📝 **修改身份组限制**\n请选择要添加/删除的身份组，完成后点击「确认」或「返回」按钮:",
                    embed=None,
                    view=role_view
                )
                
                # 角色选择回调
                async def role_select_callback(role_select_interaction):
                    # 更新选中的角色
                    selected_role_ids = [int(role_id) for role_id in role_select_interaction.data["values"]]
                    
                    # 更新角色ID列表
                    self.giveaway_data["role_ids"] = selected_role_ids
                    
                    # 重建选项列表以反映新的选择状态
                    new_options = []
                    for role in guild_roles[:max_roles]:
                        is_selected = role.id in selected_role_ids
                        new_options.append(
                            discord.SelectOption(
                                label=role.name,
                                value=str(role.id),
                                description=f"ID: {role.id}",
                                emoji="✅" if is_selected else None,
                                default=is_selected
                            )
                        )
                    
                    role_select.options = new_options
                    await role_select_interaction.response.defer()  # 使用defer而不是尝试编辑消息
                
                # 确认回调
                async def confirm_callback(confirm_interaction):
                    # 更新预览
                    embed = create_giveaway_embed(
                        prize_name=self.giveaway_data["prize_name"],
                        winners_count=self.giveaway_data["winners_count"],
                        end_time=datetime.now() + self.giveaway_data["duration"],
                        host=interaction.user,
                        giveaway_id="PREVIEW",
                        participants_count=0,
                        role_ids=self.giveaway_data["role_ids"],
                        credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                        prize_image=self.giveaway_data.get("prize_image", "")
                    )
                    
                    # 返回编辑菜单
                    await confirm_interaction.response.edit_message(
                        content="📝 **抽奖预览 (已更新)**\n请继续修改或返回预览界面:",
                        embed=embed,
                        view=edit_view
                    )
                
                # 返回回调
                async def role_back_callback(role_back_interaction):
                    # 不保存更改，返回到编辑菜单
                    embed = create_giveaway_embed(
                        prize_name=self.giveaway_data["prize_name"],
                        winners_count=self.giveaway_data["winners_count"],
                        end_time=datetime.now() + self.giveaway_data["duration"],
                        host=interaction.user,
                        giveaway_id="PREVIEW",
                        participants_count=0,
                        role_ids=self.giveaway_data.get("role_ids", []),
                        credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                        prize_image=self.giveaway_data.get("prize_image", "")
                    )
                    
                    await role_back_interaction.response.edit_message(
                        content="📝 **抽奖预览**\n请继续修改或返回预览界面:",
                        embed=embed,
                        view=edit_view
                    )
                
                # 设置回调函数
                role_select.callback = role_select_callback
                confirm_button.callback = confirm_callback
                role_back_button.callback = role_back_callback
        
        select_menu.callback = select_callback
        edit_view.add_item(select_menu)
        
        # 显示修改菜单
        await interaction.response.edit_message(
            content="📝 **抽奖编辑**\n请选择要修改的内容:",
            embed=None,
            view=edit_view
        )
    
    @discord.ui.button(label="发布", style=discord.ButtonStyle.success)
    async def publish_button(self, button, interaction):
        """发布抽奖"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有抽奖发起人可以发布", ephemeral=True)
            return
            
        # 停用所有按钮
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        
        try:
            # 计算结束时间
            now = datetime.now()
            end_time = now + self.giveaway_data["duration"]
            
            # 创建抽奖记录
            giveaway_id = await database.create_giveaway(
                author_id=self.author_id,
                prize_name=self.giveaway_data["prize_name"],
                winners_count=self.giveaway_data["winners_count"],
                end_time=end_time.isoformat(),
                role_ids=",".join(str(role_id) for role_id in self.giveaway_data.get("role_ids", [])) if self.giveaway_data.get("role_ids") else "",
                credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                prize_image=self.giveaway_data.get("prize_image", "")
            )
            
            # 创建抽奖视图
            giveaway_view = GiveawayView(self.bot, giveaway_id, end_time)
            
            # 获取参与人数（初始为0）
            participants = await database.get_giveaway_participants(giveaway_id)
            if participants:
                giveaway_view.participants_count = len(participants)
                giveaway_view.update_button_label()
            
            # 创建抽奖嵌入消息
            embed = create_giveaway_embed(
                prize_name=self.giveaway_data["prize_name"],
                winners_count=self.giveaway_data["winners_count"],
                end_time=end_time,
                host=interaction.user,
                giveaway_id=giveaway_id,
                participants_count=0,
                role_ids=self.giveaway_data.get("role_ids", []),
                credit_requirement=self.giveaway_data.get("credit_requirement", 0),
                prize_image=self.giveaway_data.get("prize_image", "")
            )
            
            # 发送抽奖消息
            giveaway_message = await interaction.channel.send(
                "🎉 **抽奖开始!** 🎉\n点击下方按钮参与!",
                embed=embed,
                view=giveaway_view
            )
            
            # 更新抽奖消息ID
            await database.update_giveaway_message(giveaway_id, giveaway_message.id, interaction.channel.id)
            
            # 创建定时任务
            self.bot.loop.create_task(
                schedule_giveaway_end(self.bot, giveaway_id, end_time, giveaway_message.id, interaction.channel.id)
            )
            
            await interaction.followup.send("抽奖已成功发布！", ephemeral=True)
            
        except Exception as e:
            logger.error(f"发布抽奖出错: {str(e)}", exc_info=True)
            await interaction.followup.send("发布抽奖时出错，请稍后再试", ephemeral=True)
    
    @discord.ui.button(label="取消", style=discord.ButtonStyle.danger)
    async def cancel_button(self, button, interaction):
        """取消抽奖创建"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有抽奖发起人可以取消", ephemeral=True)
            return
            
        # 停用所有按钮
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="抽奖已取消", embed=None, view=self)
        
        await interaction.followup.send("您已取消创建抽奖", ephemeral=True)


class GiveawayAdminView(discord.ui.View):
    """抽奖结束后的管理员专用视图"""
    
    def __init__(self, bot, giveaway_id):
        super().__init__(timeout=None)  # 永久视图
        self.bot = bot
        self.giveaway_id = giveaway_id
    
    @discord.ui.button(label="发放积分给发起者", style=discord.ButtonStyle.success)
    async def grant_to_host_button(self, button, interaction):
        """发放所有参与者的积分给抽奖发起者"""
        # 检查权限
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理员可以使用此功能", ephemeral=True)
            return
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(self.giveaway_id)
            if not giveaway:
                await interaction.response.send_message("找不到此抽奖信息", ephemeral=True)
                return
            
            # 获取参与者信息
            participants = await database.get_giveaway_participants(self.giveaway_id)
            if not participants:
                await interaction.response.send_message("没有参与者", ephemeral=True)
                return
            
            # 计算总积分
            credit_requirement = giveaway.get("credit_requirement", 0)
            if credit_requirement <= 0:
                await interaction.response.send_message("此抽奖不需要消耗积分", ephemeral=True)
                return
            
            total_credits = credit_requirement * len(participants)
            
            # 发放积分给发起者
            await database.add_user_credits(giveaway["author_id"], total_credits)
            
            # 禁用所有按钮
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            
            await interaction.followup.send(
                f"已将 {total_credits} 积分 ({credit_requirement} × {len(participants)}) 发放给抽奖发起者 <@{giveaway['author_id']}>。其他按钮已被禁用。",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"发放积分给发起者出错: {str(e)}", exc_info=True)
            await interaction.response.send_message("操作失败，请稍后再试", ephemeral=True)
    
    @discord.ui.button(label="返还积分给参与者", style=discord.ButtonStyle.primary)
    async def refund_to_participants_button(self, button, interaction):
        """返还积分给所有参与者"""
        # 检查权限
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理员可以使用此功能", ephemeral=True)
            return
        
        try:
            # 获取抽奖信息
            giveaway = await database.get_giveaway(self.giveaway_id)
            if not giveaway:
                await interaction.response.send_message("找不到此抽奖信息", ephemeral=True)
                return
            
            # 获取参与者信息
            participants = await database.get_giveaway_participants(self.giveaway_id)
            if not participants:
                await interaction.response.send_message("没有参与者", ephemeral=True)
                return
            
            # 计算总积分
            credit_requirement = giveaway.get("credit_requirement", 0)
            if credit_requirement <= 0:
                await interaction.response.send_message("此抽奖不需要消耗积分", ephemeral=True)
                return
            
            # 返还积分给参与者
            refund_count = 0
            for participant in participants:
                await database.add_user_credits(participant["user_id"], credit_requirement)
                refund_count += 1
            
            # 禁用所有按钮
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            
            await interaction.followup.send(
                f"已将 {credit_requirement} 积分返还给 {refund_count} 名参与者，总共 {credit_requirement * refund_count} 积分。其他按钮已被禁用。",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"返还积分给参与者出错: {str(e)}", exc_info=True)
            await interaction.response.send_message("操作失败，请稍后再试", ephemeral=True)


def create_giveaway_embed(prize_name, winners_count, end_time, host, giveaway_id, participants_count, role_ids=None, credit_requirement=0, prize_image=None):
    """创建抽奖嵌入消息"""
    embed = discord.Embed(
        title=f"🎁 {prize_name}",
        description=f"点击下方🎉按钮参与!\n将在 <t:{int(end_time.timestamp())}:R> 抽取 **{winners_count}** 名获奖者！",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="抽奖发起人", value=f"{host.mention}", inline=True)
    embed.add_field(name="结束时间", value=f"<t:{int(end_time.timestamp())}:F>", inline=True)
    embed.add_field(name="获奖人数", value=f"{winners_count} 人", inline=True)
    
    if role_ids:
        role_mentions = []
        for role_id in role_ids:
            role = host.guild.get_role(int(role_id))
            if role:
                role_mentions.append(role.mention)
        
        if role_mentions:
            embed.add_field(name="参与要求", value=f"需要以下身份组之一:\n{', '.join(role_mentions)}", inline=False)
    
    if credit_requirement > 0:
        embed.add_field(name="积分要求", value=f"需要 {credit_requirement} 积分", inline=True)
    

    
    # 只有在非预览模式下才显示抽奖ID
    if giveaway_id != "PREVIEW":

        embed.set_footer(text=f"抽奖ID: {giveaway_id} • 创建于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    else:
        embed.set_footer(text=f"预览模式 • {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    if prize_image:
        embed.set_image(url=prize_image)
    
    return embed


async def schedule_giveaway_end(bot, giveaway_id, end_time, message_id, channel_id):
    """计划抽奖结束任务"""
    try:
        # 计算等待时间
        now = datetime.now()
        wait_seconds = (end_time - now).total_seconds()
        
        if wait_seconds <= 0:
            logger.warning(f"抽奖 {giveaway_id} 的结束时间已过")
            await end_giveaway(bot, giveaway_id, message_id, channel_id)
            return
        
        # 尝试更新现有的消息视图
        try:
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(message_id)
                    if message:
                        # 创建视图
                        view = GiveawayView(bot, giveaway_id, end_time)
                        
                        # 获取参与人数
                        participants = await database.get_giveaway_participants(giveaway_id)
                        if participants:
                            view.participants_count = len(participants)
                            view.update_button_label()
                        
                        # 更新消息视图
                        await message.edit(view=view)
                except discord.NotFound:
                    logger.warning(f"抽奖 {giveaway_id} 的消息 {message_id} 已不存在")
                except Exception as e:
                    logger.error(f"更新抽奖 {giveaway_id} 的消息时出错: {str(e)}")
        except Exception as e:
            logger.error(f"更新抽奖 {giveaway_id} 的视图时出错: {str(e)}")
        
        # 等待到结束时间
        logger.info(f"抽奖 {giveaway_id} 将在 {wait_seconds} 秒后结束")
        
        # 检查任务是否被取消
        if asyncio.current_task().cancelled():
            logger.info(f"抽奖 {giveaway_id} 的结束任务被取消")
            return
            
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            logger.info(f"抽奖 {giveaway_id} 的结束任务在等待期间被取消")
            return
        
        # 结束抽奖
        await end_giveaway(bot, giveaway_id, message_id, channel_id)
        
    except asyncio.CancelledError:
        logger.info(f"抽奖 {giveaway_id} 的结束任务被取消")
    except Exception as e:
        logger.error(f"抽奖 {giveaway_id} 的结束任务出错: {str(e)}", exc_info=True)


async def end_giveaway(bot, giveaway_id, message_id, channel_id):
    """结束抽奖并选择获奖者"""
    try:
        # 检查任务是否被取消
        if asyncio.current_task().cancelled():
            logger.info(f"抽奖 {giveaway_id} 的结束任务被取消")
            return
            
        # 获取抽奖信息
        giveaway = await database.get_giveaway(giveaway_id)
        if not giveaway or giveaway["status"] != "active":
            logger.warning(f"抽奖 {giveaway_id} 不存在或已结束")
            return
        
        # 获取参与者
        participants = await database.get_giveaway_participants(giveaway_id)
        
        # 获取频道和消息
        channel = bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"找不到抽奖 {giveaway_id} 的频道 {channel_id}")
            return
        
        message = None
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            logger.warning(f"找不到抽奖 {giveaway_id} 的消息 {message_id}")
        except Exception as e:
            logger.error(f"获取抽奖 {giveaway_id} 的消息时出错: {str(e)}")
        
        # 如果没有参与者
        if not participants:
            # 更新抽奖状态
            try:
                await database.update_giveaway_status(giveaway_id, "ended")
            except Exception as e:
                logger.error(f"更新抽奖 {giveaway_id} 状态时出错: {str(e)}")
            
            # 更新消息
            if message:
                try:
                    embed = message.embeds[0]
                    embed.description = "抽奖已结束，没有人参与 😢"
                    embed.color = discord.Color.light_grey()
                    
                    # 停用所有按钮
                    view = discord.ui.View()
                    join_button = discord.ui.Button(emoji="🎉", style=discord.ButtonStyle.primary, disabled=True)
                    view.add_item(join_button)
                    
                    await message.edit(content="🎊 **抽奖已结束** 🎊\n没有人参与此次抽奖", embed=embed, view=view)
                except Exception as e:
                    logger.error(f"更新抽奖 {giveaway_id} 的消息时出错: {str(e)}")
            
            # 发送结束通知
            try:
                await channel.send(f"抽奖 **{giveaway['prize_name']}** 已结束，没有人参与 😢")
            except Exception as e:
                logger.error(f"发送抽奖 {giveaway_id} 的结束通知时出错: {str(e)}")
                
            return
        
        # 选择获奖者
        winner_count = min(giveaway["winners_count"], len(participants))
        winners = random.sample(participants, winner_count)
        
        # 更新抽奖状态和获奖者
        winner_ids = [winner["user_id"] for winner in winners]
        try:
            await database.update_giveaway_winners(giveaway_id, winner_ids)
            await database.update_giveaway_status(giveaway_id, "ended")
        except Exception as e:
            logger.error(f"更新抽奖 {giveaway_id} 的获奖者时出错: {str(e)}")
        
        # 更新消息
        if message:
            try:
                embed = message.embeds[0]
                
                # 更新描述，添加获奖者
                winner_mentions = [f"<@{winner_id}>" for winner_id in winner_ids]
                embed.description = f"抽奖已结束!\n\n**获奖者:**\n{', '.join(winner_mentions)}"
                embed.color = discord.Color.green()
                
                # 添加管理员视图（包含积分发放和返还按钮）
                admin_view = GiveawayAdminView(bot, giveaway_id)
                
                # 显示普通用户视图但禁用参与按钮
                user_view = discord.ui.View()
                join_button = discord.ui.Button(emoji="🎉", style=discord.ButtonStyle.primary, disabled=True)
                user_view.add_item(join_button)
                
                # 根据是否需要积分，决定是否添加管理员视图
                if giveaway["credit_requirement"] > 0:
                    await message.edit(content="🎊 **抽奖已结束** 🎊\n以下按钮仅管理员可用", embed=embed, view=admin_view)
                else:
                    await message.edit(content="🎊 **抽奖已结束** 🎊", embed=embed, view=user_view)
            except Exception as e:
                logger.error(f"更新抽奖 {giveaway_id} 的消息时出错: {str(e)}")
        
        # 发送获奖通知
        try:
            winner_mentions = [f"<@{winner_id}>" for winner_id in winner_ids]
            await channel.send(
                f"🎊 恭喜 {', '.join(winner_mentions)} 获得了 **{giveaway['prize_name']}**! 🎊\n"
                f"请联系 <@{giveaway['author_id']}> 领取奖品。"
            )
        except Exception as e:
            logger.error(f"发送抽奖 {giveaway_id} 的获奖通知时出错: {str(e)}")
        
    except asyncio.CancelledError:
        logger.info(f"抽奖 {giveaway_id} 的结束任务被取消")
        return
    except Exception as e:
        logger.error(f"结束抽奖 {giveaway_id} 时出错: {str(e)}", exc_info=True)
        # 尝试更新抽奖状态为已结束，避免悬挂状态
        try:
            await database.update_giveaway_status(giveaway_id, "ended")
        except:
            pass


def parse_time(time_str):
    """解析时间字符串，返回timedelta"""
    match = TIME_PATTERN.match(time_str)
    if not match:
        return None
    
    value, unit = match.groups()
    value = int(value)
    
    if unit.upper() == 'H':
        return timedelta(hours=value)
    elif unit.upper() == 'D':
        return timedelta(days=value)
    elif unit.upper() == 'M':
        return timedelta(minutes=value)
    
    return None


def is_valid_integer(value):
    """
    检查字符串是否是有效的整数，不接受科学计数法
    """
    try:
        # 使用helpers中的验证
        # 检查是否包含科学计数法的 'e' 或 'E'
        if 'e' in str(value).lower():
            return False
        
        # 对于整数，我们需要确保它是有效的数值
        if helpers.is_valid_amount(value):
            # 额外检查是否为整数
            # is_valid_amount允许小数，但此处我们需要整数
            if float(value).is_integer():
                return True
        return False
    except (ValueError, TypeError):
        return False

def sanitize_text(text):
    """
    清理文本内容，移除潜在的危险字符和注入内容
    """
    if text is None:
        return ""
    
    # 使用helpers中的sanitize_input进行处理
    return helpers.sanitize_input(text)


class Giveaway(commands.Cog):
    """抽奖系统"""
    
    def __init__(self, bot):
        self.bot = bot
        # 启动自动重连任务
        self.reconnect_giveaways.start()
    
    def cog_unload(self):
        # 停止自动重连任务
        self.reconnect_giveaways.cancel()
    
    @tasks.loop(minutes=10.0)
    async def reconnect_giveaways(self):
        """每10分钟重新检查活跃抽奖并确保它们有正确的结束任务"""
        try:
            logger.info("正在检查活跃抽奖以确保连接...")
            active_giveaways = await database.get_active_giveaways()
            
            processed_count = 0
            now = datetime.now()
            for giveaway in active_giveaways:
                try:
                    # 解析结束时间
                    end_time = datetime.fromisoformat(giveaway["end_time"])
                    
                    # 如果抽奖还没结束
                    if end_time > now:
                        # 为抽奖创建/更新结束任务
                        task = self.bot.loop.create_task(
                            schedule_giveaway_end(
                                self.bot, 
                                giveaway["id"], 
                                end_time, 
                                giveaway["message_id"], 
                                giveaway["channel_id"]
                            )
                        )
                        # 设置任务名称以便于调试
                        task.set_name(f"giveaway_end_{giveaway['id']}")
                        
                        processed_count += 1
                        logger.info(f"已重新连接抽奖 {giveaway['id']}")
                    # 如果抽奖已经过期但状态仍为活跃
                    elif giveaway["status"] == "active":
                        # 强制结束抽奖
                        end_task = self.bot.loop.create_task(
                            end_giveaway(
                                self.bot,
                                giveaway["id"],
                                giveaway["message_id"],
                                giveaway["channel_id"]
                            )
                        )
                        # 设置任务名称
                        end_task.set_name(f"giveaway_force_end_{giveaway['id']}")
                        
                        processed_count += 1
                        logger.info(f"已手动结束过期的抽奖 {giveaway['id']}")
                except Exception as e:
                    logger.error(f"重新连接抽奖 {giveaway['id']} 时出错: {str(e)}")
            
            if processed_count > 0:
                logger.info(f"完成检查，已处理 {processed_count} 个活跃抽奖")
            
        except asyncio.CancelledError:
            logger.info("抽奖重连任务被取消")
            raise
        except Exception as e:
            logger.error(f"重新连接抽奖任务出错: {str(e)}", exc_info=True)
    
    @reconnect_giveaways.before_loop
    async def before_reconnect_giveaways(self):
        """等待机器人准备好"""
        await self.bot.wait_until_ready()
    
    @commands.slash_command(
        name="创建抽奖",
        description="创建一个新的抽奖活动"
    )
    async def create_giveaway(
        self,
        ctx,
        time: str = Option(description="抽奖持续时间，例如: 1H(1小时), 1D(1天), 30M(30分钟)", required=False), 
        winners: int = Option(description="获奖者数量", required=False, default=1),
        prize: str = Option(description="奖品名称", required=False),
        image: discord.Attachment = Option(description="奖品图片", required=False),
        role: discord.Role = Option(description="参与抽奖所需的身份组", required=False, default=None),
        credits: int = Option(description="参与抽奖所需的积分", required=False, default=0)
    ):
        """创建一个新的抽奖"""
        # 检查频道权限
        if config.GIVEAWAY_CHANNEL_IDS and ctx.channel.id not in config.GIVEAWAY_CHANNEL_IDS:
            allowed_channels = [f"<#{channel_id}>" for channel_id in config.GIVEAWAY_CHANNEL_IDS]
            await ctx.respond(f"只能在以下频道使用此命令: {', '.join(allowed_channels)}", ephemeral=True)
            return
        
        # 清理并验证输入
        if time:
            time = helpers.sanitize_input(time)
        if prize:
            prize = helpers.sanitize_input(prize)
        
        # 检查必填信息
        missing_fields = []
        if not time or not isinstance(time, str):
            missing_fields.append("`time:抽奖时间`")
        if not prize or not isinstance(prize, str):
            missing_fields.append("`prize:奖品名称`")
        if not image or not isinstance(image, discord.Attachment):
            missing_fields.append("`image:奖品图片`")
        
        if missing_fields:
            await ctx.respond(f"请提供以下必填信息: {', '.join(missing_fields)}", ephemeral=True)
            return
        
        # 验证获奖者数量
        if not isinstance(winners, int) or winners < 1:
            await ctx.respond("获奖者数量必须是正整数", ephemeral=True)
            return
            
        # 检查获奖者数量是否超过限制
        if isinstance(winners, int) and winners > 100:
            await ctx.respond("获奖者数量不能超过100人", ephemeral=True)
            return
            
        # 验证积分
        if not isinstance(credits, int) or credits < 0:
            await ctx.respond("积分不能为负数", ephemeral=True)
            return
            
        # 检查积分是否超过限制
        if isinstance(credits, int) and credits > 10000:
            await ctx.respond("积分不能超过10,000 U", ephemeral=True)
            return
        
        # 检查奖品名称是否为空
        if not prize or len(prize.strip()) == 0:
            await ctx.respond("奖品名称不能为空", ephemeral=True)
            return
        
        # 解析时间
        duration = parse_time(time)
        if not duration:
            await ctx.respond("无效的时间格式。请使用例如 1H (1小时), 1D (1天), 30M (30分钟) 的格式。", ephemeral=True)
            return
        
        # 获取图片URL (如果提供了附件)
        image_url = None
        if image and isinstance(image, discord.Attachment):
            image_url = image.url
            content_type = getattr(image, 'content_type', None)
            if not (content_type and content_type.startswith('image/')):
                await ctx.respond("文件必须是图片格式 (PNG, JPG 或 JPEG)", ephemeral=True)
                return
        
        # 收集抽奖数据
        # 确保winners是有效的整数
        if not isinstance(winners, int) or winners < 1:
            winners = 1
        
        # 如果用户没有指定身份组，使用VERIFIED_ROLE_ID作为默认
        if role and isinstance(role, discord.Role):
            role_ids = [role.id]
        else:
            # 使用配置中的已验证身份组ID
            role_ids = [config.VERIFIED_ROLE_ID] if hasattr(config, 'VERIFIED_ROLE_ID') else []
            
        giveaway_data = {
            "duration": duration,
            "winners_count": winners,
            "prize_name": prize,
            "prize_image": image_url if image_url else "",
            "role_ids": role_ids,
            "credit_requirement": credits if isinstance(credits, int) else 0
        }
        
        # 创建预览
        embed = create_giveaway_embed(
            prize_name=prize,
            winners_count=winners,
            end_time=datetime.now() + duration,
            host=ctx.author,
            giveaway_id="PREVIEW",
            participants_count=0,
            role_ids=giveaway_data["role_ids"],
            credit_requirement=credits,
            prize_image=image_url if image_url else None
        )
        
        preview_view = GiveawayPreviewView(self.bot, ctx.author.id, giveaway_data)
        
        await ctx.respond(
            "📝 **抽奖预览**\n请检查以下信息，确认无误后点击「发布」按钮:",
            embed=embed,
            view=preview_view,
            ephemeral=True
        )


def setup(bot):
    """加载抽奖组件"""
    bot.add_cog(Giveaway(bot)) 