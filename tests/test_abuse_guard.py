"""Offline adversarial checks at the actual Pycord dispatch boundary."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import discord
from discord.ext import commands
from utils.abuse_guard import TokenBucket, InteractionGate, GuardedConnectionState
from utils.guild_isolation import IsolatedBot


def payload(ident=1,user=42,kind=3,custom='new:language:English',guild=2):
    return dict(id=str(ident),guild_id=str(guild),type=kind,
                member={'user':{'id':str(user),'bot':False}},
                data={'custom_id':custom,'component_type':2})


class BucketTests(unittest.TestCase):
    def test_burst_limit_refills_without_extending_penalty(self):
        now=[0]
        bucket=TokenBucket(2,10,clock=lambda:now[0])
        self.assertTrue(bucket.allow('a')); self.assertTrue(bucket.allow('a'))
        self.assertFalse(bucket.allow('a'))
        now[0]=4
        self.assertFalse(bucket.allow('a'))
        now[0]=5
        self.assertTrue(bucket.allow('a'))
        self.assertFalse(bucket.allow('a'))
        now[0]=15
        self.assertTrue(bucket.allow('a')); self.assertTrue(bucket.allow('a'))

    def test_account_rotation_cannot_evict_live_limits_or_grow_memory(self):
        now=[0]
        bucket=TokenBucket(1,10,max_keys=2,clock=lambda:now[0])
        self.assertTrue(bucket.allow(1)); self.assertTrue(bucket.allow(2))
        for uid in range(3,10000): self.assertFalse(bucket.allow(uid))
        self.assertEqual(len(bucket.entries),2)
        self.assertFalse(bucket.allow(1))
        now[0]=10
        self.assertTrue(bucket.allow(3))
        self.assertLessEqual(len(bucket.entries),2)

    def test_user_limit_shared_across_custom_ids_and_trade_lane(self):
        gate=InteractionGate(clock=lambda:0)
        for i in range(8): self.assertTrue(gate.admit(payload(i,custom='different:'+str(i)))[0])
        self.assertFalse(gate.admit(payload(10,custom='new:trade:collect:order'))[0])
        self.assertTrue(gate.admit(payload(11,user=43))[0])

    def test_replayed_interaction_never_dispatches_twice(self):
        gate=InteractionGate(clock=lambda:0)
        self.assertEqual(gate.admit(payload()),(True,False))
        self.assertEqual(gate.admit(payload()),(False,False))

    def test_ordinary_flood_does_not_use_existing_trade_budget(self):
        gate=InteractionGate(clock=lambda:0)
        for i in range(40): self.assertTrue(gate.admit(payload(i,user=100+i))[0])
        self.assertFalse(gate.admit(payload(41,user=200))[0])
        self.assertTrue(gate.admit(payload(42,user=201,custom='new:trade:receipt:order'))[0])
        self.assertTrue(gate.admit(payload(43,user=202,guild=1))[0])

    def test_notifications_are_bounded_for_multi_account_flood(self):
        gate=InteractionGate(clock=lambda:0)
        for i in range(40): gate.admit(payload(i,user=100+i))
        notices=sum(gate.admit(payload(i,user=100+i))[1] for i in range(40,400))
        self.assertEqual(notices,4)

    def test_replay_cache_is_bounded_and_expires(self):
        now=[0]
        gate=InteractionGate(clock=lambda:now[0])
        gate.seen.update((i,0) for i in range(16384))
        self.assertFalse(gate.admit(payload(20000))[0])
        self.assertEqual(len(gate.seen),16384)
        now[0]=301
        self.assertTrue(gate.admit(payload(20001))[0])
        self.assertEqual(len(gate.seen),1)

    def test_unexpected_guild_bot_and_malformed_payloads_rejected(self):
        gate=InteractionGate(clock=lambda:0)
        cases=[payload(guild=0),payload(guild=3),{},None,{'member':[]},payload()]
        cases[-1]['member']['user']['bot']=True
        for case in cases: self.assertEqual(gate.admit(case),(False,False))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot=IsolatedBot(command_prefix='!',intents=discord.Intents.none())
        self.state=self.bot._connection
        self.state.abuse_gate=InteractionGate(clock=lambda:0)

    async def asyncTearDown(self):
        await asyncio.gather(*self.state.rejection_tasks,return_exceptions=True)
        await self.bot.close()

    async def test_real_gateway_parser_guards_views_modals_and_commands(self):
        self.assertIsInstance(self.state,GuardedConnectionState)
        parse=self.state.parsers['INTERACTION_CREATE']
        for kind in (2,3,5):
            with self.subTest(kind=kind):
                self.state.abuse_gate=InteractionGate(clock=lambda:0)
                packet=payload(kind=kind)
                interaction=NS(data=packet['data'],type=discord.InteractionType(kind),user=NS(id=42))
                with patch('discord.state.Interaction',return_value=interaction) as constructor, \
                     patch.object(self.state._view_store,'dispatch') as view, \
                     patch.object(self.state._modal_store,'dispatch',AsyncMock()) as modal, \
                     patch.object(self.state,'dispatch') as dispatch, \
                     patch.object(self.state,'reject_interaction',AsyncMock()):
                    parse(packet)
                    parse(packet)  # Duplicate ID, even if Pycord received it twice.
                    for i in range(2,14): parse(payload(i,kind=kind))
                    await asyncio.sleep(0)
                    self.assertEqual(constructor.call_count,8)
                    self.assertEqual(dispatch.call_count,8)
                    self.assertEqual(view.call_count,8 if kind==3 else 0)
                    self.assertEqual(modal.await_count,8 if kind==5 else 0)

    async def test_blocked_reply_tasks_cannot_grow_without_bound(self):
        blocker=asyncio.Event()
        async def slow(_): await blocker.wait()
        self.state.abuse_gate.admit=Mock(return_value=(False,True))
        with patch.object(self.state,'reject_interaction',slow):
            for i in range(100): self.state.parse_interaction_create(payload(i))
            self.assertEqual(len(self.state.rejection_tasks),8)
            blocker.set()
            await asyncio.gather(*self.state.rejection_tasks)

    async def test_prefix_commands_share_interaction_budget(self):
        message=NS(guild=NS(id=2),author=NS(id=42,bot=False),content='!command')
        for i in range(8): self.state.abuse_gate.admit(payload(i))
        with patch.object(commands.Bot,'process_commands',AsyncMock()) as process:
            await self.bot.process_commands(message)
            process.assert_not_awaited()
            message.author.id=43
            await self.bot.process_commands(message)
            process.assert_awaited_once()

    async def test_other_messages_do_not_consume_command_quota(self):
        message=NS(guild=NS(id=2),author=NS(id=42,bot=False),content='ordinary chat')
        with patch.object(commands.Bot,'process_commands',AsyncMock()) as process:
            for _ in range(50): await self.bot.process_commands(message)
            self.assertEqual(len(self.state.abuse_gate.users.entries),0)
            process.assert_not_awaited()
