"""Offline checks for temporary-channel ownership and deletion boundaries."""
import os
os.environ.update(DISCORD_TOKEN='offline-test', GUILD_ID='1', NEW_GUILD_ID='2',
                  BINANCE_API_KEY='offline-test', BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from modules.new_voice import NewVoice
from utils.abuse_guard import TokenBucket


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def cog(self, room):
        cog = object.__new__(NewVoice)
        cog.bot = NS(get_channel=lambda cid:room)
        cog.rooms = {9:{'guild':2,'owner':42,'category':8}}
        cog.created = {}
        cog.cooldowns = {}
        cog.events = TokenBucket(4,10)
        cog.event_budget = TokenBucket(40,10,max_keys=1)
        cog.creations = TokenBucket(5,60,max_keys=1)
        cog.loaded = True
        cog.lock = asyncio.Lock()
        return cog

    def room(self, members=()):
        room = MagicMock(spec=discord.VoiceChannel)
        room.id=9; room.guild=NS(id=2); room.category_id=8; room.members=list(members)
        room.delete=AsyncMock()
        return room

    async def test_only_tracked_empty_room_is_deleted(self):
        room=self.room()
        cog=self.cog(room)
        with patch('modules.new_voice.db.query',AsyncMock()):
            await cog.remove_empty(10)
            room.delete.assert_not_awaited()
            await cog.remove_empty(9)
        room.delete.assert_awaited_once()
        self.assertNotIn(9,cog.rooms)

    async def test_other_members_keep_room_after_creator_leaves(self):
        room=self.room([NS(id=43)])
        cog=self.cog(room)
        await cog.remove_empty(9)
        room.delete.assert_not_awaited()

    async def test_entry_moved_and_old_guild_rooms_not_deleted(self):
        for category,guild,entry in ((7,2,5),(8,1,5),(8,2,9)):
            room=self.room(); room.category_id=category; room.guild=NS(id=guild)
            with patch('modules.new_voice.cfg.VOICE_CREATE_CHANNEL_ID',entry):
                await self.cog(room).remove_empty(9)
            room.delete.assert_not_awaited()

    async def test_restart_loads_and_cleans_recorded_room(self):
        room=self.room(); cog=self.cog(room)
        cog.loaded=False; cog.rooms={}
        query=AsyncMock(side_effect=[[{'setting_key':'new_voice:9','value':'{"guild":2,"category":8,"owner":42}'}],1])
        with patch('modules.new_voice.db.query',query):
            await cog.load()
            await cog.remove_empty(9)
        room.delete.assert_awaited_once()

    async def test_creation_is_saved_before_move_and_inherits_category(self):
        room=self.room()
        category=MagicMock(spec=discord.CategoryChannel); category.id=8
        entry=MagicMock(spec=discord.VoiceChannel); entry.id=5
        guild=NS(id=2,get_channel=lambda cid:category if cid==8 else entry,
                 create_voice_channel=AsyncMock(return_value=room))
        member=NS(id=42,bot=False,display_name='Member',guild=guild,voice=NS(channel=entry),move_to=AsyncMock())
        cog=self.cog(room); cog.rooms={}
        sequence=[]
        member.move_to.side_effect=lambda *a,**k:sequence.append('move')
        with patch('modules.new_voice.cfg.VOICE_CREATE_CHANNEL_ID',5),patch('modules.new_voice.cfg.VOICE_CATEGORY_ID',8),patch('modules.new_voice.db.setting',AsyncMock(side_effect=lambda *a:sequence.append('save'))):
            await cog.create_for(member)
        self.assertEqual(sequence,['save','move'])
        self.assertIs(guild.create_voice_channel.call_args.kwargs['category'],category)
        self.assertNotIn('overwrites',guild.create_voice_channel.call_args.kwargs)

    async def test_old_guild_voice_event_does_nothing(self):
        cog=self.cog(self.room()); cog.load=AsyncMock()
        await cog.on_voice_state_update(NS(guild=NS(id=1)),NS(channel=None),NS(channel=NS(id=5)))
        cog.load.assert_not_awaited()

    async def test_creation_capacity_does_not_stop_rejoining_existing_room(self):
        room=self.room()
        category=MagicMock(spec=discord.CategoryChannel); category.id=8
        entry=MagicMock(spec=discord.VoiceChannel); entry.id=5
        guild=NS(id=2,get_channel=lambda cid:{8:category,5:entry,9:room}.get(cid),create_voice_channel=AsyncMock())
        member=NS(id=43,bot=False,guild=guild,voice=NS(channel=entry),move_to=AsyncMock())
        cog=self.cog(room)
        with patch('modules.new_voice.cfg.VOICE_CREATE_CHANNEL_ID',5),patch('modules.new_voice.cfg.VOICE_CATEGORY_ID',8),patch('modules.new_voice.cfg.VOICE_MAX_ROOMS',1):
            await cog.create_for(member)
            guild.create_voice_channel.assert_not_awaited()
            member.move_to.assert_not_awaited()
            member.id=42
            await cog.create_for(member)
            member.move_to.assert_awaited_once_with(room,reason='Return to existing temporary voice room')

    async def test_many_users_share_creation_rate_limit(self):
        room=self.room()
        category=MagicMock(spec=discord.CategoryChannel); category.id=8
        entry=MagicMock(spec=discord.VoiceChannel); entry.id=5
        guild=NS(id=2,get_channel=lambda cid:category if cid==8 else entry,create_voice_channel=AsyncMock(return_value=room))
        member=NS(id=42,bot=False,display_name='Member',guild=guild,voice=NS(channel=entry),move_to=AsyncMock())
        cog=self.cog(room); cog.rooms={}
        cog.creations=TokenBucket(5,60,max_keys=1,clock=lambda:0)
        with patch('modules.new_voice.cfg.VOICE_CREATE_CHANNEL_ID',5),patch('modules.new_voice.cfg.VOICE_CATEGORY_ID',8),patch('modules.new_voice.db.setting',AsyncMock()):
            for uid in range(100):
                member.id=uid
                await cog.create_for(member)
        self.assertEqual(guild.create_voice_channel.await_count,5)

    async def test_rapid_voice_hopping_cannot_queue_unlimited_work(self):
        cog=self.cog(self.room()); cog.load=AsyncMock(); cog.create_for=AsyncMock()
        member=NS(id=42,bot=False,guild=NS(id=2))
        cog.events=TokenBucket(4,10,clock=lambda:0)
        with patch('modules.new_voice.cfg.VOICE_CREATE_CHANNEL_ID',5),patch('modules.new_voice.cfg.VOICE_CATEGORY_ID',8):
            await asyncio.gather(*(cog.on_voice_state_update(member,NS(channel=None),NS(channel=NS(id=5))) for _ in range(100)))
        self.assertEqual(cog.create_for.await_count,4)
