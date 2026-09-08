import os
import discord
import json
import asyncio
from discord.commands import SlashCommandGroup
from discord.ext import commands
import datetime

# 加载本地化模块
from utils.l10n import get_lang_code, get_text

active_channels = {}  # {channel_id: owner_id}
channel_timers = {}   # {channel_id: timer_task}
active_team_posts = {}  # {message_id: {data about team}}

class BaseCreationView(discord.ui.View):
    async def cleanup(self, interaction):
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass

    async def start_deletion_timer(self, channel):
        try:
            await asyncio.sleep(180)  # 等待3分钟
            if len(channel.members) == 0 and channel.id in active_channels:
                await self.safe_delete_channel(channel)
        except asyncio.CancelledError:
            print(f"频道 {channel.id} 删除计时器被取消")
        except Exception as e:
            print(f"Timer error: {str(e)}")

    async def safe_delete_channel(self, channel):
        try:
            if channel.id in active_channels:
                data = active_channels.pop(channel.id)
                notice_channel = channel.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
                try:
                    if notice_channel:
                        notice_msg = await notice_channel.fetch_message(data["notice_msg_id"])
                        await notice_msg.delete()
                except discord.NotFound:
                    print(f"通知消息已不存在: {data.get('notice_msg_id')}")
                except Exception as e:
                    print(f"Failed to delete notice message: {e}")
            if channel.id in channel_timers:
                del channel_timers[channel.id]
            await channel.delete()
            print(f"Channel {channel.name} deleted successfully")
        except discord.NotFound:
            print(f"频道已不存在: {channel.id}")
            # 清理相关数据
            if channel.id in active_channels:
                active_channels.pop(channel.id)
            if channel.id in channel_timers:
                channel_timers.pop(channel.id)
        except Exception as e:
            print(f"Delete error: {str(e)}")

class InitialPartyView(discord.ui.View):
    def __init__(self, lang):
        super().__init__(timeout=None)
        self.lang = lang
        
        # Remove existing buttons
        self.clear_items()
        
        # Add localized buttons with persistent custom_ids
        self.add_item(discord.ui.Button(
            label=get_text("create_boss_party", lang),
            style=discord.ButtonStyle.primary,
            custom_id="create_boss_party"  # Persistent custom_id
        ))
        self.add_item(discord.ui.Button(
            label=get_text("create_task_party", lang),
            style=discord.ButtonStyle.success,
            custom_id="create_task_party"  # Persistent custom_id 
        ))
        
        # Set up callbacks
        self.children[0].callback = self.boss_button_callback
        self.children[1].callback = self.task_button_callback

    async def boss_button_callback(self, interaction):
        lang = self.lang
        view = PartyTypeSelectionView(lang, "boss")
        await interaction.response.send_message(get_text("select_boss_team_info", lang), view=view, ephemeral=True)

    async def task_button_callback(self, interaction):
        lang = self.lang
        view = PartyTypeSelectionView(lang, "task")
        await interaction.response.send_message(get_text("select_mission_team_info", lang), view=view, ephemeral=True)

class PartyTypeSelectionView(discord.ui.View):
    def __init__(self, lang, party_type):
        super().__init__()
        self.lang = lang
        self.party_type = party_type  # "boss" or "task"
        
        # Remove existing buttons
        self.clear_items()
        
        # Add localized buttons with persistent custom_ids
        self.add_item(discord.ui.Button(
            label=get_text("create_voice_channel", lang),
            style=discord.ButtonStyle.primary,
            custom_id=f"{party_type}_voice_channel_button"
        ))
        self.add_item(discord.ui.Button(
            label=get_text("publish_team_info", lang),
            style=discord.ButtonStyle.success,
            custom_id=f"{party_type}_team_post_button"
        ))
        
        # Set up callbacks
        self.children[0].callback = self.voice_channel_button_callback
        self.children[1].callback = self.team_post_button_callback

    async def voice_channel_button_callback(self, interaction):
        if self.party_type == "boss":
            view = PartyCreationView(self.lang)
            await interaction.response.send_message(get_text("select_boss_team_info", self.lang), view=view, ephemeral=True)
        else:  # task
            view = TaskCreationView(self.lang)
            await interaction.response.send_message(get_text("select_mission_team_info", self.lang), view=view, ephemeral=True)

    async def team_post_button_callback(self, interaction):
        if self.party_type == "boss":
            view = BossTeamPostView(self.lang)
            await interaction.response.send_message(get_text("select_boss_team_info", self.lang), view=view, ephemeral=True)
        else:  # task
            view = TaskTeamPostView(self.lang)
            await interaction.response.send_message(get_text("select_mission_team_info", self.lang), view=view, ephemeral=True)

class TaskCreationView(BaseCreationView):
    def __init__(self, lang):
        super().__init__()
        self.lang = lang
        self.server = None
        self.task = None
        self.size = None

        # 动态添加组件
        self.add_item(ServerSelect(lang))
        self.add_item(TaskSelect(lang))
        self.add_item(SizeSelect(lang))
        self.add_item(PublicButton(lang))
        self.add_item(GuildOnlyButton(lang))

    async def handle_channel_creation(self, interaction, public):
        if interaction.user.id in active_channels.values():
            return await interaction.response.send_message(get_text("error_active_channel", self.lang), ephemeral=True)
            
        if None in (self.server, self.task, self.size):
            return await interaction.response.send_message(get_text("error_incomplete_selection", self.lang), ephemeral=True)

        category = discord.utils.get(interaction.guild.categories, name=os.getenv("VOICE_CATEGORY"))
        if not category:
            return await interaction.response.send_message(get_text("error_category_not_found", self.lang), ephemeral=True)

        # 构建任务频道名称
        channel_name = f"【{self.server}】{get_text(self.task,self.lang)}"

        # 设置权限覆盖
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }
        if public:
            all_role = interaction.guild.get_role(int(os.getenv("ALL_ROLE_ID")))
            if all_role:
                overwrites[all_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        else:
            guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID")))
            if guild_role:
                overwrites[guild_role] = discord.PermissionOverwrite(view_channel=True, connect=True)

        try:
            voice_channel = await interaction.guild.create_voice_channel(
                name=channel_name,
                category=category,
                user_limit=self.size,
                overwrites=overwrites
            )
        except Exception as e:
            return await interaction.response.send_message(get_text("error_create_failed", self.lang) + f"{str(e)}", ephemeral=True)

        notice_channel = interaction.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
        notice_msg = await notice_channel.send(f"📢 {interaction.user.mention} 創建了組隊頻道：{voice_channel.mention}")

        # 将通知消息ID与语音频道ID关联保存
        active_channels[voice_channel.id] = {
            "owner": interaction.user.id,
            "notice_msg_id": notice_msg.id
        }

        # 启动3分钟计时器（创建后无人加入时删除频道）
        channel_timers[voice_channel.id] = asyncio.create_task(
            self.start_deletion_timer(voice_channel)
        )
        await interaction.response.send_message(get_text("success_channel_created", self.lang) + f"{voice_channel.mention}", ephemeral=True)
        await asyncio.sleep(10)
        await self.cleanup(interaction)

# 任务组队信息发布视图
class TaskTeamPostView(discord.ui.View):
    def __init__(self, lang):
        super().__init__()
        self.lang = lang
        self.server = None
        self.task = None
        self.size = None
        self.guild_only = False  # 默认所有人可加入
        
        # 动态添加组件
        self.add_item(ServerSelect(lang))
        self.add_item(TaskSelect(lang))
        self.add_item(SizeSelect(lang))
        
        # 添加权限选择按钮
        self.add_item(PublicPostButton(lang))
        self.add_item(GuildOnlyPostButton(lang))
    
    async def create_team_post(self, interaction, guild_only=False):
        if None in (self.server, self.task, self.size):
            return await interaction.response.send_message(get_text("error_incomplete_selection", self.lang), ephemeral=True)
        
        # 检查是否有权限发布公会限制信息
        if guild_only:
            guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID", 0)))
            if not guild_role or guild_role not in interaction.user.roles:
                return await interaction.response.send_message(get_text("error_no_permission", self.lang), ephemeral=True)
        
        self.guild_only = guild_only
        
        # 创建组队信息嵌入
        task_name = get_text(self.task, self.lang)
        embed = discord.Embed(
            title=f"📢 {get_text('create_task_party', self.lang)}",
            description=f"{get_text('select_server', self.lang)}: **{self.server}**\n{get_text('select_task', self.lang)}: **{task_name}**\n{get_text('select_size', self.lang)}: **{self.size}**",
            color=discord.Color.blue()
        )
        
        # 设置截止时间（6小时后）
        expiration_time = datetime.datetime.now() + datetime.timedelta(hours=6)
        creator_field = get_text("creator_field", self.lang)
        members_field = get_text("members_field", self.lang)
        deadline_field = get_text("deadline_field", self.lang)
        
        embed.add_field(name=creator_field, value=interaction.user.mention, inline=True)
        embed.add_field(name=members_field, value=f"1/{self.size}", inline=True)
        embed.add_field(name=deadline_field, value=f"<t:{int(expiration_time.timestamp())}:R>", inline=True)
        
        # 修复页脚文本设置，添加错误处理
        try:
            embed.set_footer(text=get_text("valid_until", self.lang).format(time=expiration_time.strftime('%Y-%m-%d %H:%M:%S')))
        except KeyError:
            # 如果翻译中没有{time}占位符，使用默认文本
            embed.set_footer(text=f"有效期至 {expiration_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 创建加入按钮
        team_view = JoinTeamView(self.size, interaction.user.id, expiration_time, self.lang, guild_only)
        
        # 根据选择发送到不同频道
        if guild_only:
            # 发送到公会频道
            guild_channel_id = int(os.getenv("GUILD_NOTICE_CHANNEL_ID", os.getenv("NOTICE_CHANNEL_ID")))
            notice_channel = interaction.guild.get_channel(guild_channel_id)
            # 在标题中添加公会限制标记
            embed.title = f"📢 【仅公会】{get_text('create_task_party', self.lang)}"
        else:
            # 发送到公共频道
            notice_channel = interaction.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
        
        if not notice_channel:
            return await interaction.response.send_message("找不到指定的通知频道，请联系管理员设置正确的频道ID", ephemeral=True)
        
        team_post = await notice_channel.send(content=f"{interaction.user.mention} {get_text('create_task_party', self.lang)}:", embed=embed, view=team_view)
        
        # 存储组队信息
        active_team_posts[team_post.id] = {
            "type": "task",
            "server": self.server,
            "task": self.task,
            "size": self.size,
            "creator": interaction.user.id,
            "members": [interaction.user.id],
            "expiration": expiration_time,
            "message_id": team_post.id,
            "members_field_name": members_field,
            "guild_only": guild_only  # 添加公会限制标记
        }
        
        # 设置6小时后自动删除
        team_view.message_id = team_post.id
        asyncio.create_task(self.auto_expire_post(team_post, expiration_time))
        
        await interaction.response.send_message(get_text("team_info_published", self.lang), ephemeral=True)
    
    async def auto_expire_post(self, message, expiration_time):
        # 计算等待时间
        now = datetime.datetime.now()
        wait_seconds = (expiration_time - now).total_seconds()
        
        if wait_seconds > 0:
            try:
                # 检查任务是否被取消
                if asyncio.current_task().cancelled():
                    print(f"组队过期任务被取消: {message.id}")
                    return
                    
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                print(f"组队过期任务在等待期间被取消: {message.id}")
                return
            except Exception as e:
                print(f"组队过期任务等待时发生错误: {e}")
                return
            
        try:
            # 检查消息是否仍然存在
            if message.id in active_team_posts:
                await message.edit(content=f"~~{message.content}~~ ({get_text('team_expired', self.lang)})", view=None)
                await message.reply(get_text("team_expired", self.lang))
                active_team_posts.pop(message.id)
        except discord.NotFound:
            print(f"组队消息已不存在: {message.id}")
            if message.id in active_team_posts:
                active_team_posts.pop(message.id)
        except Exception as e:
            print(f"过期处理错误: {e}")
            # 仍然尝试移除字典中的条目
            if message.id in active_team_posts:
                active_team_posts.pop(message.id)

class ServerSelect(discord.ui.Select):
    def __init__(self, lang):
        options = [
            discord.SelectOption(label=get_text(f'server_{server.lower()}', lang), value=server)
            for server in ["Ain", "Errai", "Fang", "Polaris", "Nunki"]
        ]
        super().__init__(
            placeholder=get_text('select_server', lang),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.server = self.values[0]
        await interaction.response.defer()

class TaskSelect(discord.ui.Select):
    def __init__(self, lang):
        options = [
            discord.SelectOption(label=get_text(task,lang), value=task)
            for task in ["Regular Party Quests","Cross World Party Quests","Erda Spectrum","Hungry Muto"]
        ]
        super().__init__(
            placeholder=get_text('select_task', lang),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.task = self.values[0]
        await interaction.response.defer()

class SizeSelect(discord.ui.Select):
    def __init__(self, lang):
        options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(2, 7)]
        super().__init__(
            placeholder=get_text('select_size', lang),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.size = int(self.values[0])
        await interaction.response.defer()

class PublicButton(discord.ui.Button):
    def __init__(self, lang):
        super().__init__(
            label=get_text('create_public', lang),
            style=discord.ButtonStyle.green
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_channel_creation(interaction, public=True)

class GuildOnlyButton(discord.ui.Button):
    def __init__(self, lang):
        super().__init__(
            label=get_text('create_guild_only', lang),
            style=discord.ButtonStyle.blurple
        )

    async def callback(self, interaction: discord.Interaction):
        guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID")))
        if not guild_role or guild_role not in interaction.user.roles:
            return await interaction.response.send_message(get_text('error_no_permission', self.view.lang), ephemeral=True)
        await self.view.handle_channel_creation(interaction, public=False)

class PostTeamButton(discord.ui.Button):
    def __init__(self, lang):
        super().__init__(
            label=get_text("publish_team_info", lang),
            style=discord.ButtonStyle.success
        )
    
    async def callback(self, interaction: discord.Interaction):
        await self.view.create_team_post(interaction)

class PartyCreationView(BaseCreationView):
    def __init__(self, lang):
        super().__init__()
        self.lang = lang
        self.server = None
        self.boss = None
        self.difficulty = None
        self.size = None

        # 动态添加组件
        self.add_item(ServerSelect(lang))
        self.add_item(BossSelect(lang))
        self.add_item(DifficultySelect(lang))
        self.add_item(SizeSelect(lang))
        self.add_item(PublicButton(lang))
        self.add_item(GuildOnlyButton(lang))

    async def handle_channel_creation(self, interaction, public):
        if interaction.user.id in active_channels.values():
            return await interaction.response.send_message(get_text("error_active_channel",self.lang), ephemeral=True)
            
        if None in (self.server, self.boss, self.difficulty, self.size):
            return await interaction.response.send_message(get_text("error_incomplete_selection",self.lang), ephemeral=True)

        category = discord.utils.get(interaction.guild.categories, name=os.getenv("VOICE_CATEGORY"))
        if not category:
            return await interaction.response.send_message(get_text("error_category_not_found",self.lang), ephemeral=True)

        # 构建频道名称
        channel_name = f"【{self.server}】" + get_text("boss_" + self.boss, self.lang) + f"（{self.difficulty}）"

        # 设置权限覆盖
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }

        if public:
            all_role = interaction.guild.get_role(int(os.getenv("ALL_ROLE_ID")))
            if all_role:
                overwrites[all_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True
                )
        else:
            guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID")))
            if guild_role:
                overwrites[guild_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True
                )

        try:
            voice_channel = await interaction.guild.create_voice_channel(
                name=channel_name,
                category=category,
                user_limit=self.size,
                overwrites=overwrites
            )
        except Exception as e:
            return await interaction.response.send_message(get_text("error_create_failed", self.lang) + f"{str(e)}", ephemeral=True)

        notice_channel = interaction.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
        notice_msg = await notice_channel.send(f"📢 {interaction.user.mention} 創建了組隊頻道：{voice_channel.mention}")

        # 将通知消息ID与语音频道ID关联保存
        active_channels[voice_channel.id] = {
            "owner": interaction.user.id,
            "notice_msg_id": notice_msg.id
        }

        # 启动3分钟计时器（创建后无人加入时删除频道）
        channel_timers[voice_channel.id] = asyncio.create_task(
            self.start_deletion_timer(voice_channel)
        )

        await interaction.response.send_message(get_text("success_channel_created", self.lang) + f"{voice_channel.mention}", ephemeral=True)
        await asyncio.sleep(10)
        await self.cleanup(interaction)

# BOSS组队信息发布视图
class BossTeamPostView(discord.ui.View):
    def __init__(self, lang):
        super().__init__()
        self.lang = lang
        self.server = None
        self.boss = None
        self.difficulty = None
        self.size = None
        self.guild_only = False  # 默认所有人可加入
        
        # 动态添加组件
        self.add_item(ServerSelect(lang))
        self.add_item(BossSelect(lang))
        self.add_item(DifficultySelect(lang))
        self.add_item(SizeSelect(lang))
        
        # 添加权限选择按钮
        self.add_item(PublicPostButton(lang))
        self.add_item(GuildOnlyPostButton(lang))
    
    async def create_team_post(self, interaction, guild_only=False):
        if None in (self.server, self.boss, self.difficulty, self.size):
            return await interaction.response.send_message(get_text("error_incomplete_selection", self.lang), ephemeral=True)
        
        # 检查是否有权限发布公会限制信息
        if guild_only:
            guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID", 0)))
            if not guild_role or guild_role not in interaction.user.roles:
                return await interaction.response.send_message(get_text("error_no_permission", self.lang), ephemeral=True)
        
        self.guild_only = guild_only
        
        # 创建组队信息嵌入
        boss_name = get_text("boss_" + self.boss, self.lang)
        embed = discord.Embed(
            title=f"📢 {get_text('create_boss_party', self.lang)}",
            description=f"{get_text('select_server', self.lang)}: **{self.server}**\n{get_text('select_boss', self.lang)}: **{boss_name}**\n{get_text('select_difficulty', self.lang)}: **{self.difficulty}**\n{get_text('select_size', self.lang)}: **{self.size}**",
            color=discord.Color.red()
        )
        
        # 设置截止时间（6小时后）
        expiration_time = datetime.datetime.now() + datetime.timedelta(hours=6)
        creator_field = get_text("creator_field", self.lang)
        members_field = get_text("members_field", self.lang)
        deadline_field = get_text("deadline_field", self.lang)
        
        embed.add_field(name=creator_field, value=interaction.user.mention, inline=True)
        embed.add_field(name=members_field, value=f"1/{self.size}", inline=True)
        embed.add_field(name=deadline_field, value=f"<t:{int(expiration_time.timestamp())}:R>", inline=True)
        
        # 修复页脚文本设置，添加错误处理
        try:
            embed.set_footer(text=get_text("valid_until", self.lang).format(time=expiration_time.strftime('%Y-%m-%d %H:%M:%S')))
        except KeyError:
            # 如果翻译中没有{time}占位符，使用默认文本
            embed.set_footer(text=f"有效期至 {expiration_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 创建加入按钮
        team_view = JoinTeamView(self.size, interaction.user.id, expiration_time, self.lang, guild_only)
        
        # 根据选择发送到不同频道
        if guild_only:
            # 发送到公会频道
            guild_channel_id = int(os.getenv("GUILD_NOTICE_CHANNEL_ID", os.getenv("NOTICE_CHANNEL_ID")))
            notice_channel = interaction.guild.get_channel(guild_channel_id)
            # 在标题中添加公会限制标记
            embed.title = f"📢 【仅公会】{get_text('create_boss_party', self.lang)}"
        else:
            # 发送到公共频道
            notice_channel = interaction.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
        
        if not notice_channel:
            return await interaction.response.send_message("找不到指定的通知频道，请联系管理员设置正确的频道ID", ephemeral=True)
        
        team_post = await notice_channel.send(content=f"{interaction.user.mention} {get_text('create_boss_party', self.lang)}:", embed=embed, view=team_view)
        
        # 存储组队信息
        active_team_posts[team_post.id] = {
            "type": "boss",
            "server": self.server,
            "boss": self.boss,
            "difficulty": self.difficulty,
            "size": self.size,
            "creator": interaction.user.id,
            "members": [interaction.user.id],
            "expiration": expiration_time,
            "message_id": team_post.id,
            "members_field_name": members_field,
            "guild_only": guild_only  # 添加公会限制标记
        }
        
        # 设置6小时后自动删除
        team_view.message_id = team_post.id
        asyncio.create_task(self.auto_expire_post(team_post, expiration_time))
        
        await interaction.response.send_message(get_text("team_info_published", self.lang), ephemeral=True)
    
    async def auto_expire_post(self, message, expiration_time):
        # 计算等待时间
        now = datetime.datetime.now()
        wait_seconds = (expiration_time - now).total_seconds()
        
        if wait_seconds > 0:
            try:
                # 检查任务是否被取消
                if asyncio.current_task().cancelled():
                    print(f"组队过期任务被取消: {message.id}")
                    return
                    
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                print(f"组队过期任务在等待期间被取消: {message.id}")
                return
            except Exception as e:
                print(f"组队过期任务等待时发生错误: {e}")
                return
            
        try:
            # 检查消息是否仍然存在
            if message.id in active_team_posts:
                await message.edit(content=f"~~{message.content}~~ ({get_text('team_expired', self.lang)})", view=None)
                await message.reply(get_text("team_expired", self.lang))
                active_team_posts.pop(message.id)
        except discord.NotFound:
            print(f"组队消息已不存在: {message.id}")
            if message.id in active_team_posts:
                active_team_posts.pop(message.id)
        except Exception as e:
            print(f"过期处理错误: {e}")
            # 仍然尝试移除字典中的条目
            if message.id in active_team_posts:
                active_team_posts.pop(message.id)

class BossSelect(discord.ui.Select):
    def __init__(self, lang):
        options = [
            discord.SelectOption(label=get_text(f'boss_{boss}', lang), value=boss)
            for boss in ["Zakum", "Horntail", "Pink Bean", "Root Abyss", "Cygnus", 
                         "Von Leon", "Hilla", "Arkarium", "Magnus", "Lotus", 
                         "Damien", "Lucid", "Will", "Guardian Angel Slime", 
                         "Giant Monster Gloom", "Verus Hilla", "Guard Captain Darknell",
                         "Black Mage", "Chosen Seren", "Kalos the Guardian", "Kaling"]
        ]
        super().__init__(
            placeholder=get_text('select_boss', lang),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.boss = self.values[0]
        await interaction.response.defer()

class DifficultySelect(discord.ui.Select):
    def __init__(self, lang):
        options = [
            discord.SelectOption(label=get_text(f'difficulty_{diff.lower()}', lang), value=get_text(f'difficulty_{diff.lower()}', lang))
            for diff in ["Normal", "Hard", "Extreme"]
        ]
        super().__init__(
            placeholder=get_text('select_difficulty', lang),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.difficulty = self.values[0]
        await interaction.response.defer()

# 创建公开团队按钮
class PublicPostButton(discord.ui.Button):
    def __init__(self, lang):
        super().__init__(
            label=get_text("create_public", lang),
            style=discord.ButtonStyle.green
        )
    
    async def callback(self, interaction: discord.Interaction):
        await self.view.create_team_post(interaction, guild_only=False)

# 创建仅公会可见团队按钮
class GuildOnlyPostButton(discord.ui.Button):
    def __init__(self, lang):
        super().__init__(
            label=get_text("create_guild_only", lang),
            style=discord.ButtonStyle.blurple
        )
    
    async def callback(self, interaction: discord.Interaction):
        # 检查是否是公会成员
        guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID", 0)))
        if not guild_role or guild_role not in interaction.user.roles:
            return await interaction.response.send_message(get_text("error_no_permission", self.view.lang), ephemeral=True)
        
        await self.view.create_team_post(interaction, guild_only=True)

# 加入队伍按钮视图
class JoinTeamView(discord.ui.View):
    def __init__(self, size, creator_id, expiration_time, lang, guild_only=False):
        super().__init__(timeout=None)
        self.message_id = None
        self.size = size
        self.creator_id = creator_id
        self.expiration_time = expiration_time
        self.lang = lang
        self.guild_only = guild_only  # 是否仅公会成员可加入
        
        # Remove existing buttons
        self.clear_items()
        
        # Add localized button with custom_id
        join_button = discord.ui.Button(
            label=get_text("join_team", lang),
            style=discord.ButtonStyle.primary,
            custom_id="join_team"  # Persistent custom_id
        )
        join_button.callback = self.join_button_callback
        self.add_item(join_button)
        
    async def join_button_callback(self, interaction):
        # 获取消息ID
        message_id = interaction.message.id
        
        if message_id not in active_team_posts:
            return await interaction.response.send_message(get_text("team_expired", self.lang), ephemeral=True)
        
        team_data = active_team_posts[message_id]
        
        # 检查是否限制仅公会成员
        if team_data.get("guild_only", False):
            guild_role = interaction.guild.get_role(int(os.getenv("GUILD_ROLE_ID", 0)))
            if not guild_role or guild_role not in interaction.user.roles:
                return await interaction.response.send_message(get_text("error_no_permission", self.lang), ephemeral=True)
        
        # 检查是否已加入
        if interaction.user.id in team_data["members"]:
            return await interaction.response.send_message(get_text("already_joined", self.lang), ephemeral=True)
        
        # 检查队伍是否已满
        if len(team_data["members"]) >= team_data["size"]:
            return await interaction.response.send_message(get_text("team_full", self.lang), ephemeral=True)
        
        # 添加用户到队伍
        team_data["members"].append(interaction.user.id)
        current_size = len(team_data["members"])
        
        # 更新嵌入消息
        message = interaction.message
        embed = message.embeds[0]
        
        # 更新人数字段
        for i, field in enumerate(embed.fields):
            if field.name == team_data["members_field_name"]:
                embed.set_field_at(i, name=team_data["members_field_name"], value=f"{current_size}/{team_data['size']}", inline=True)
        
        await message.edit(embed=embed)
        
        # 通知用户已加入
        try:
            # 尝试使用翻译字符串
            join_message = get_text("join_success", self.lang).format(current=current_size, total=team_data['size'])
        except KeyError:
            # 如果翻译字符串中没有必要的占位符，使用默认文本
            join_message = f"您已成功加入队伍！当前人数: {current_size}/{team_data['size']}"
            
        await interaction.response.send_message(join_message, ephemeral=True)
        
        # 检查队伍是否已满
        if current_size >= team_data["size"]:
            # 通知所有队员
            mention_str = " ".join([f"<@{uid}>" for uid in team_data["members"]])
            
            try:
                if team_data["type"] == "boss":
                    boss_name = get_text("boss_" + team_data["boss"], self.lang)
                    boss_info = f"BOSS: {boss_name}\n难度: {team_data['difficulty']}"
                    notification = get_text("team_ready", self.lang).format(
                        mentions=mention_str,
                        type="BOSS",
                        server=team_data['server'],
                        info=boss_info
                    )
                else:
                    task_name = get_text(team_data["task"], self.lang)
                    task_info = f"任务: {task_name}"
                    notification = get_text("team_ready", self.lang).format(
                        mentions=mention_str,
                        type="任务",
                        server=team_data['server'],
                        info=task_info
                    )
            except KeyError:
                # 默认通知文本
                if team_data["type"] == "boss":
                    boss_name = get_text("boss_" + team_data["boss"], self.lang)
                    notification = f"队伍已满！\n\n{mention_str}\n\n您的BOSS组队已准备就绪！\n服务器: {team_data['server']}\nBOSS: {boss_name}\n难度: {team_data['difficulty']}"
                else:
                    task_name = get_text(team_data["task"], self.lang)
                    notification = f"队伍已满！\n\n{mention_str}\n\n您的任务组队已准备就绪！\n服务器: {team_data['server']}\n任务: {task_name}"
            
            await message.reply(notification)
            
            # 禁用加入按钮
            self.children[0].disabled = True
            await message.edit(view=self)

# 此函数是PartyModule外部的全局函数
async def safe_delete_channel(channel):
    try:
        if channel.id in active_channels:
            data = active_channels.pop(channel.id)
            notice_channel = channel.guild.get_channel(int(os.getenv("NOTICE_CHANNEL_ID")))
            try:
                if notice_channel:
                    notice_msg = await notice_channel.fetch_message(data["notice_msg_id"])
                    await notice_msg.delete()
            except discord.NotFound:
                print(f"通知消息已不存在: {data.get('notice_msg_id')}")
            except Exception as e:
                print(f"Failed to delete notice message: {e}")
        if channel.id in channel_timers:
            del channel_timers[channel.id]
        await channel.delete()
        print(f"Channel {channel.name} deleted successfully")
    except discord.NotFound:
        print(f"频道已不存在: {channel.id}")
        # 清理相关数据
        if channel.id in active_channels:
            active_channels.pop(channel.id)
        if channel.id in channel_timers:
            channel_timers.pop(channel.id)
    except Exception as e:
        print(f"Delete error: {str(e)}")

class PartyModule:
    def __init__(self, bot):
        self.bot = bot
        self.party = SlashCommandGroup("party", "组队相关指令")
        self.setup()
        # 启动一个任务来设置派对按钮
        print("初始化PartyModule，创建setup_party_buttons任务")
        # 确保使用bot.loop.create_task而不是直接使用asyncio.create_task
        self.button_setup_task = self.bot.loop.create_task(self.setup_party_buttons())
        self.button_setup_task.set_name("party_button_setup")
        self.button_setup_task.add_done_callback(self._handle_task_result)
        print(f"已创建Party按钮设置任务: {self.button_setup_task}")
        
    def _handle_task_result(self, task):
        """处理任务完成的回调，记录任何未捕获的异常"""
        try:
            # 获取任务结果，如果有异常，这会引发异常
            task.result()
        except asyncio.CancelledError:
            print(f"Party按钮设置任务被取消")
        except Exception as e:
            print(f"Party按钮设置任务出错: {e}")
            import traceback
            print(traceback.format_exc())
            
            # 如果是关键任务失败，尝试重新启动
            print("尝试重新启动Party按钮设置任务")
            self.button_setup_task = self.bot.loop.create_task(self.setup_party_buttons())
            self.button_setup_task.set_name("party_button_setup_restarted")
            self.button_setup_task.add_done_callback(self._handle_task_result)
        
    async def setup_party_buttons(self):
        """初始化派对按钮。在bot准备好后被调用。"""
        try:
            # 等待机器人准备就绪，设置超时
            print("等待机器人准备就绪以设置Party按钮...")
            ready_event = asyncio.Event()
            
            # 创建一个检查函数
            async def wait_for_ready():
                await self.bot.wait_until_ready()
                ready_event.set()
            
            # 创建等待任务并设置超时
            wait_task = asyncio.create_task(wait_for_ready())
            try:
                # 最多等待60秒
                await asyncio.wait_for(ready_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                print("WARNING: 等待机器人准备就绪超时，将继续尝试设置按钮")
            finally:
                if not wait_task.done():
                    wait_task.cancel()
            
            print("Party module ready! 机器人已准备就绪")
            
            # 在指定频道创建组队按钮
            party_channel_id = int(os.getenv("PARTY_COMMAND_CHANNEL", 0))
            if not party_channel_id:
                print("WARNING: PARTY_COMMAND_CHANNEL is not set or is 0. Buttons will not be sent automatically.")
                return
            
            print(f"Looking for channel with ID {party_channel_id} to send party buttons...")
            
            # 获取指定频道
            channel = self.bot.get_channel(party_channel_id)
            if not channel:
                try:
                    # 尝试通过fetch获取频道，以防get_channel失败
                    print(f"通过get_channel无法找到频道 {party_channel_id}，尝试使用fetch_channel")
                    channel = await self.bot.fetch_channel(party_channel_id)
                except Exception as e:
                    print(f"ERROR: Could not find channel with ID {party_channel_id}. Error: {e}")
                    return
            
            if not channel:
                print(f"ERROR: Channel with ID {party_channel_id} not found.")
                return
            
            print(f"Channel found: {channel.name} (ID: {channel.id})")
            
            # 创建组队按钮消息
            try:
                # 首先检查是否已经有组队按钮
                old_button_message = None
                async for message in channel.history(limit=20):
                    if message.author.id == self.bot.user.id and len(message.embeds) > 0:
                        for embed in message.embeds:
                            if embed.title == "组队系统" or "组队系统" in (embed.title or ""):
                                print(f"找到现有组队按钮消息，ID: {message.id}")
                                old_button_message = message
                                break
                        if old_button_message:
                            break
                
                # 创建新的按钮视图
                embed = discord.Embed(
                    title="组队系统",
                    description="点击下方按钮创建队伍",
                    color=discord.Color.blue()
                )
                
                view = InitialPartyView("zh-cn")  # 默认使用中文
                
                # 如果找到旧按钮消息，更新它
                if old_button_message:
                    try:
                        await old_button_message.edit(embed=embed, view=view)
                        print(f"SUCCESS: Updated party buttons in message {old_button_message.id}")
                    except discord.HTTPException as e:
                        print(f"ERROR: Failed to update existing buttons (HTTP error), deleting old message: {e}")
                        try:
                            await old_button_message.delete()
                        except Exception as del_err:
                            print(f"Failed to delete old message: {del_err}")
                        
                        try:
                            await channel.send(embed=embed, view=view)
                            print(f"SUCCESS: Deleted old message and sent new party buttons")
                        except Exception as send_err:
                            print(f"ERROR: Failed to send new message after deleting old one: {send_err}")
                    except Exception as e:
                        print(f"ERROR: Failed to update existing buttons, deleting old message: {e}")
                        try:
                            await old_button_message.delete()
                            await channel.send(embed=embed, view=view)
                            print(f"SUCCESS: Deleted old message and sent new party buttons")
                        except Exception as nested_e:
                            print(f"ERROR: Failed during error recovery: {nested_e}")
                else:
                    # 未找到现有按钮，发送新的按钮消息
                    await channel.send(embed=embed, view=view)
                    print(f"SUCCESS: Party buttons sent to channel {channel.name} (ID: {channel.id})")
            except Exception as e:
                print(f"ERROR: Failed to send party buttons. Error: {e}")
                import traceback
                print(traceback.format_exc())
        except Exception as e:
            print(f"ERROR during Party button setup: {e}")
            import traceback
            print(traceback.format_exc())
    
    def setup(self):
        # 监听语音通道状态变更
        @self.bot.event
        async def on_voice_state_update(member, before, after):
            # 处理加入频道的情况
            if after and after.channel and after.channel.id in channel_timers:
                # 如果有人加入频道，取消自动删除计时器
                channel_timers[after.channel.id].cancel()
                channel_timers.pop(after.channel.id)
                
            # 处理离开频道的情况
            if before and before.channel and before.channel.id in active_channels:
                if len(before.channel.members) == 0:
                    # 如果频道已经空了，创建新的计时器5秒钟后删除频道
                    async def delete_after_timeout(channel):
                        try:
                            await asyncio.sleep(5)  # 5秒钟
                            # 再次检查频道是否仍然为空
                            if channel.id in active_channels and len(channel.members) == 0:
                                await safe_delete_channel(channel)
                            else:
                                print(f"频道 {channel.id} 不再为空或已不在活跃频道列表中，取消删除")
                        except asyncio.CancelledError:
                            print(f"删除频道 {channel.id} 的任务被取消")
                        except Exception as e:
                            print(f"Delete after timeout error: {e}")
                    
                    channel_timers[before.channel.id] = asyncio.create_task(
                        delete_after_timeout(before.channel)
                    )
        
        @self.party.command(name="boss", description="创建BOSS组队频道")
        async def create_boss_party(ctx):
            # 检查是否在指定频道使用指令
            allowed_channel_id = int(os.getenv("NOTICE_CHANNEL_ID", 0))
            if allowed_channel_id and ctx.channel.id != allowed_channel_id:
                return await ctx.respond(get_text("error_wrong_channel", get_lang_code(ctx.locale)), ephemeral=True)
                
            lang = get_lang_code(ctx.locale)
            view = PartyCreationView(lang)
            await ctx.respond(get_text("select_boss_team_info", lang), view=view, ephemeral=True)

        @self.party.command(name="task", description="创建任务组队频道")
        async def create_task_party(ctx):
            # 检查是否在指定频道使用指令
            allowed_channel_id = int(os.getenv("NOTICE_CHANNEL_ID", 0))
            if allowed_channel_id and ctx.channel.id != allowed_channel_id:
                return await ctx.respond(get_text("error_wrong_channel", get_lang_code(ctx.locale)), ephemeral=True)
                
            lang = get_lang_code(ctx.locale)
            view = TaskCreationView(lang)
            await ctx.respond(get_text("select_mission_team_info", lang), view=view, ephemeral=True)
        
        # 添加刷新组队按钮命令
        @self.party.command(name="refresh", description="刷新组队按钮")
        @commands.has_permissions(administrator=True)  # 只允许管理员使用
        async def refresh_party_buttons(ctx):
            await ctx.defer(ephemeral=True)
            try:
                # 获取指定频道
                party_channel_id = int(os.getenv("PARTY_COMMAND_CHANNEL", 0))
                if not party_channel_id:
                    return await ctx.followup.send("未设置PARTY_COMMAND_CHANNEL环境变量，无法刷新按钮。", ephemeral=True)
                
                channel = self.bot.get_channel(party_channel_id)
                if not channel:
                    return await ctx.followup.send(f"找不到ID为{party_channel_id}的频道。", ephemeral=True)
                
                # 查找并删除所有旧的组队按钮消息
                deleted_count = 0
                async for message in channel.history(limit=20):
                    if message.author.id == self.bot.user.id and len(message.embeds) > 0:
                        for embed in message.embeds:
                            if embed.title == "组队系统" or "组队系统" in (embed.title or ""):
                                await message.delete()
                                deleted_count += 1
                
                # 创建新的按钮消息
                embed = discord.Embed(
                    title="组队系统",
                    description="点击下方按钮创建队伍",
                    color=discord.Color.blue()
                )
                
                view = InitialPartyView("zh-cn")  # 默认使用中文
                await channel.send(embed=embed, view=view)
                
                return await ctx.followup.send(f"已成功删除{deleted_count}条旧按钮消息并创建新的组队按钮。", ephemeral=True)
            except Exception as e:
                return await ctx.followup.send(f"刷新按钮时出错: {e}", ephemeral=True)
        
        self.bot.add_application_command(self.party)

def setup(bot):
    party_module = PartyModule(bot)
    return party_module 