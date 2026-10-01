"""Bounded, local admission control before Discord dispatches any interaction.

This complements durable order/credit checks; it is not an authorization check.
Pycord's state hook is pinned and covered by gateway dispatch regression tests.
"""
import asyncio
import time
from collections import OrderedDict
import discord
from discord.state import ConnectionState
import config
from utils.security_language import text as security_text


class TokenBucket:
    def __init__(self, capacity, period, max_keys=8192, clock=time.monotonic):
        self.capacity, self.period, self.max_keys = capacity, period, max_keys
        self.clock = clock
        self.entries = OrderedDict()

    def allow(self, key):
        now = self.clock()
        previous = self.entries.get(key)
        if previous is None:
            # Never evict a live bucket: rotating accounts must not reset limits.
            while self.entries and next(iter(self.entries.values()))[1] <= now-self.period:
                self.entries.popitem(last=False)
            if len(self.entries) >= self.max_keys:
                return False
            tokens = self.capacity
        else:
            tokens = min(self.capacity, previous[0]+max(0,now-previous[1])*self.capacity/self.period)
        accepted = tokens >= 1
        self.entries[key] = (tokens-1 if accepted else tokens, now)
        self.entries.move_to_end(key)
        return accepted


class InteractionGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.users = TokenBucket(8,10,clock=clock)
        # Separate budgets keep ordinary panel spam away from existing trade actions.
        self.guilds = TokenBucket(40,10,max_keys=8,clock=clock)
        self.notice_users = TokenBucket(1,15,clock=clock)
        self.notices = TokenBucket(4,10,max_keys=1,clock=clock)
        self.seen = OrderedDict()

    def admit(self, data):
        try:
            guild = int(data.get('guild_id',0))
            user_data = (data.get('member') or {}).get('user') or data.get('user') or {}
            user = int(user_data['id'])
            ident = int(data['id'])
            if not guild or guild not in (config.GUILD_ID, config.NEW.GUILD_ID) or user_data.get('bot'):
                return False, False
        except (TypeError, ValueError, KeyError, AttributeError):
            return False, False
        now = self.clock()
        while self.seen and next(iter(self.seen.values())) <= now-300:
            self.seen.popitem(last=False)
        if ident in self.seen:
            return False, False
        payload = data.get('data') or {}
        if not isinstance(payload,dict):
            return False, False
        custom = str(payload.get('custom_id',''))
        lane = 'trade' if custom.startswith(('new:trade:','new:refund_close:')) else 'ordinary'
        accepted = (self.users.allow((guild,user)) and len(self.seen)<16384
                    and self.guilds.allow((guild,lane)))
        if accepted:
            self.seen[ident] = now
            return True, False
        notify = self.notice_users.allow((guild,user)) and self.notices.allow('notice')
        return False, notify


class GuardedConnectionState(ConnectionState):
    def __init__(self, **options):
        self.abuse_gate = InteractionGate()
        self.rejection_tasks = set()
        super().__init__(**options)

    def parse_interaction_create(self, data):
        allowed, notify = self.abuse_gate.admit(data)
        if allowed:
            return super().parse_interaction_create(data)
        # Do not create unbounded response tasks when Discord is slow or unreachable.
        if notify and len(self.rejection_tasks)<8:
            task = self.loop.create_task(self.reject_interaction(data))
            self.rejection_tasks.add(task)
            task.add_done_callback(self.rejection_tasks.discard)

    async def reject_interaction(self, data):
        try:
            interaction = discord.Interaction(data=data, state=self)
            async def reply():
                if interaction.type == discord.InteractionType.auto_complete:
                    await interaction.response.send_autocomplete_result([])
                else:
                    await interaction.response.send_message(
                        security_text(interaction.user,'rate'),
                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await asyncio.wait_for(reply(), timeout=5)
        except (Exception, asyncio.CancelledError):
            # A rejected request never triggers business work or a retry.
            pass
