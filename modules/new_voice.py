"""Join-to-create voice rooms, isolated to the new guild and persisted for restart."""
import asyncio
import logging
import time
import json
import discord
from discord.ext import commands, tasks
import config
from utils import new_store as db

cfg = config.NEW
log = logging.getLogger(__name__)
PREFIX = 'new_voice:'


class NewVoice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.rooms = {}
        self.created = {}
        self.cooldowns = {}
        self.loaded = False
        if cfg.VOICE_CREATE_CHANNEL_ID and cfg.VOICE_CATEGORY_ID:
            self.cleanup.start()

    def cog_unload(self):
        self.cleanup.cancel()

    async def load(self):
        if self.loaded:
            return
        rows = await db.query('SELECT setting_key,value FROM settings WHERE setting_key LIKE %s', (PREFIX+'%',))
        for row in rows:
            record = json.loads(row['value'])
            if record.get('guild') == cfg.GUILD_ID:
                self.rooms[int(row['setting_key'][len(PREFIX):])] = record
        self.loaded = True

    async def forget(self, cid):
        await db.query('DELETE FROM settings WHERE setting_key=%s', (PREFIX+str(cid),))
        self.rooms.pop(cid, None)
        self.created.pop(cid, None)

    async def remove_empty(self, cid):
        record = self.rooms.get(cid)
        if not record or cid == cfg.VOICE_CREATE_CHANNEL_ID:
            return
        if time.monotonic()-self.created.get(cid, float('-inf')) < 10:
            return  # Allow Discord's voice-state update to arrive after moving a member.
        channel = self.bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(cid)
            except discord.NotFound:
                await self.forget(cid)
                return
        # Never delete unrelated rooms, even if someone renames or moves a channel.
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != cfg.GUILD_ID:
            return
        if channel.category_id != record['category'] or channel.members:
            return
        try:
            await channel.delete(reason='Temporary voice room is empty')
        except discord.NotFound:
            pass
        log.info('Deleted empty new-guild voice room: %s', cid)
        await self.forget(cid)

    async def create_for(self, member):
        guild = member.guild
        if not member.voice or not member.voice.channel or member.voice.channel.id != cfg.VOICE_CREATE_CHANNEL_ID:
            return
        category = guild.get_channel(cfg.VOICE_CATEGORY_ID)
        entry = guild.get_channel(cfg.VOICE_CREATE_CHANNEL_ID)
        if not isinstance(category, discord.CategoryChannel) or not isinstance(entry, discord.VoiceChannel):
            raise ValueError('NEW_VOICE_CATEGORY_ID / NEW_VOICE_CREATE_CHANNEL_ID must identify a category and voice channel in the new guild')
        # A creator returning to the entry rejoins their still-existing room.
        for cid, record in self.rooms.items():
            room = guild.get_channel(cid)
            if record['owner'] == member.id and isinstance(room, discord.VoiceChannel) and room.category_id == cfg.VOICE_CATEGORY_ID:
                await member.move_to(room, reason='Return to existing temporary voice room')
                return
        now = time.monotonic()
        if now-self.cooldowns.get(member.id, float('-inf')) < 30:
            try:
                await member.send('Please wait 30 seconds before creating another voice room, then rejoin the creation channel.')
            except discord.HTTPException:
                pass
            return
        self.cooldowns[member.id] = now
        # Inherit category access; creating a room does not grant moderation powers.
        room = await guild.create_voice_channel(
            name=(member.display_name+' • Voice')[:100], category=category,
            reason=f'Temporary voice room requested by {member.id}')
        record = {'guild': guild.id, 'category': category.id, 'owner': member.id}
        self.rooms[room.id] = record
        self.created[room.id] = now
        try:
            await db.setting(PREFIX+str(room.id), record)
            # The user may have left while Discord/MySQL was processing the request.
            if member.voice and member.voice.channel and member.voice.channel.id == entry.id:
                await member.move_to(room, reason='Move into requested temporary voice room')
            log.info('Created new-guild voice room: channel=%s owner=%s', room.id, member.id)
        except Exception:
            # Keep the ID in memory if deletion fails; periodic cleanup can retry.
            self.created.pop(room.id, None)
            await self.remove_empty(room.id)
            raise

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.guild.id != cfg.GUILD_ID or member.bot:
            return
        if not cfg.VOICE_CREATE_CHANNEL_ID or not cfg.VOICE_CATEGORY_ID or before.channel == after.channel:
            return
        try:
            async with self.lock:
                await self.load()
                if before.channel:
                    await self.remove_empty(before.channel.id)
                if after.channel and after.channel.id == cfg.VOICE_CREATE_CHANNEL_ID:
                    await self.create_for(member)
        except Exception:
            log.exception('New-guild dynamic voice operation failed for member %s', member.id)

    @tasks.loop(seconds=30)
    async def cleanup(self):
        guild = self.bot.get_guild(cfg.GUILD_ID)
        if not self.bot.is_ready() or not guild or guild.unavailable:
            return
        try:
            async with self.lock:
                await self.load()
                for cid in list(self.rooms):
                    try:
                        await self.remove_empty(cid)
                    except Exception:
                        log.exception('Temporary voice cleanup failed: %s', cid)
                now = time.monotonic()
                self.cooldowns = {uid:stamp for uid,stamp in self.cooldowns.items() if now-stamp < 30}
        except Exception:
            log.exception('Temporary voice room recovery failed')

    @cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID != config.GUILD_ID:
        bot.add_cog(NewVoice(bot))
