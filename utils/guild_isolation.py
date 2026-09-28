"""Keep legacy listeners and commands out of the new guild, including button IDs."""
import functools
import config
import discord
from discord.ext import commands

LEGACY={'modules.trading','modules.rental','modules.social','modules.admin','modules.market','modules.giveaway','modules.party'}

class IsolatedBot(commands.Bot):
    async def process_application_commands(self,interaction,auto_sync=None):
        if interaction.guild is None:
            if not interaction.response.is_done():
                if interaction.type==discord.InteractionType.auto_complete:
                    await interaction.response.send_autocomplete_result([])
                else:
                    await interaction.response.send_message('请在社群内使用机器人指令，私聊不提供命令功能。',ephemeral=True)
            return
        return await super().process_application_commands(interaction,auto_sync=auto_sync)

    def add_application_command(self,command):
        command.guild_only=True
        module=getattr(getattr(command,'callback',None),'__module__','')
        if not module:
            module=getattr(getattr(command,'cog',None),'__module__','')
        if not module and getattr(command,'subcommands',None):
            module=getattr(getattr(command.subcommands[0],'callback',None),'__module__','')
        if module=='modules.party' or command.name=='party':
            return
        if module in LEGACY:
            if not config.GUILD_ID: return
            command.guild_ids=[config.GUILD_ID]
        elif module.startswith('modules.new_'):
            if not config.NEW.GUILD_ID: return
            command.guild_ids=[config.NEW.GUILD_ID]
            if isinstance(command,(discord.SlashCommand,discord.SlashCommandGroup)):
                command.default_member_permissions=discord.Permissions(administrator=True)
                command.guild_only=True
        return super().add_application_command(command)

    def add_listener(self,func,name=None):
        if func.__module__ in LEGACY:
            original=func
            @functools.wraps(original)
            async def isolated(*args,**kwargs):
                for arg in args:
                    data=getattr(arg,'data',None)
                    if isinstance(data,dict) and str(data.get('custom_id','')).startswith('new:'):
                        return
                    guild=getattr(arg,'guild',None)
                    if guild and guild.id!=config.GUILD_ID:
                        return
                    if getattr(arg,'id',None)==config.NEW.GUILD_ID and config.NEW.GUILD_ID:
                        return
                return await original(*args,**kwargs)
            func=isolated
        return super().add_listener(func,name)

def command_allowed(ctx):
    guild=getattr(ctx,'guild',None)
    if guild is None:
        return False
    callback=getattr(getattr(ctx,'command',None),'callback',None)
    module=getattr(callback,'__module__','')
    if module=='modules.party':
        return False
    if module.startswith('modules.new_'):
        if not guild or guild.id!=config.NEW.GUILD_ID:
            return False
        if isinstance(ctx.command,(discord.SlashCommand,discord.SlashCommandGroup)):
            from modules.new_community import admin
            roles=config.NEW.NOTICE_ADMIN_ROLES if module=='modules.new_community' else config.NEW.TRADE_ADMIN_ROLES
            return admin(ctx.author,roles)
    if getattr(callback,'__name__','') in ('create_boss_party','close_test'):
        return False
    if module in ('modules.trading','modules.rental'):
        return False
    if not config.LEGACY_RANKING_ENABLED and module in ('modules.social','modules.admin'):
        if getattr(callback,'__name__','') in ('update_ranks','update_ranks_slash','my_invites','my_invites_slash'):
            return False
    if module in LEGACY and guild and guild.id!=config.GUILD_ID:
        return module=='modules.trading' and guild.id==config.NEW.GUILD_ID and getattr(callback,'__name__','') in ('trade_callback','trade_sell_callback')
    return True
