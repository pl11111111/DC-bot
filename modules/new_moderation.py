"""New guild link restriction, including edits of uncached messages."""
import logging
import re
import discord
from discord.ext import commands
import config
from utils import new_store as db

cfg = config.NEW
log = logging.getLogger(__name__)
LINK = re.compile(r'(?i)(?:https?://|ftp://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)\S+')


def contains_link(text):
    return bool(LINK.search(text or ''))


class NewModeration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        guild = self.bot.get_guild(cfg.GUILD_ID)
        if guild and not any(guild.get_role(rid) for rid in cfg.LV3_ROLE_IDS):
            log.error('NEW_LV3_ROLE_IDS missing/invalid: links are blocked for all human members in the new guild')

    async def moderate(self, message, edit=None):
        if not message.guild or message.guild.id != cfg.GUILD_ID or message.author.bot:
            return
        if not contains_link(message.content):
            return
        member = message.author
        if not isinstance(member, discord.Member):
            member = await message.guild.fetch_member(member.id)
        if any(role.id in cfg.LV3_ROLE_IDS for role in member.roles):
            return
        # Save evidence before requesting deletion. Log failures must not disable moderation.
        evidence = self.bot.get_cog('NewMessageLog')
        if evidence and edit is None:
            try:
                await evidence.on_message(message)
            except Exception:
                log.exception('Could not preserve link moderation evidence: %s', message.id)
        details = {'channel': message.channel.id, 'message': message.id,
                   'author': member.id, 'reason': 'requires_new_lv3', 'edited': edit is not None}
        try:
            await message.delete(reason='New guild links require LV3')
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.error('Cannot delete non-LV3 link: channel=%s message=%s', message.channel.id, message.id)
            return
        log.info('Removed non-LV3 link: %s', details)
        try:
            await db.audit(self.bot.user.id, 'lv3_link_deleted', details)
        except Exception:
            log.exception('Could not persist link moderation audit: %s', details)
        try:
            await message.channel.send(
                f'<@{member.id}> Only members with the LV3 role can post links. / 需要 LV3 身份组才能发送链接。',
                delete_after=15, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning('Could not send LV3 warning in channel %s', message.channel.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.moderate(message)

    async def check_edit(self, payload):
        if payload.guild_id != cfg.GUILD_ID or not contains_link(payload.data.get('content')):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                log.exception('Cannot fetch edited-message channel %s', payload.channel_id)
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        await self.moderate(message, edit=payload)


def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID != config.GUILD_ID:
        bot.add_cog(NewModeration(bot))
