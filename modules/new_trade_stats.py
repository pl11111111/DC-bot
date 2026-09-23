"""Public aggregate statistics; private order details never leave the order channel."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import re
import discord
from discord.ext import commands, tasks
import config
from utils import new_store as db

cfg=config.NEW
log=logging.getLogger(__name__)
TZ=timezone(timedelta(hours=8))
ACTIVE={
    'Awaiting confirmation':('pending',),
    'Awaiting payment':('confirmed','invoicing','paying','payment_timeout'),
    'Awaiting shipment':('paid',),
    'Awaiting receipt':('shipped',),
    'Awaiting seller payout':('receipt_confirmed','releasing'),
}
REFUND=('refund_ready','releasing_refund')
REVIEW=('disputed','payment_review')
TERMINAL=('completed','cancelled','refunded','test_closed','manual_refunded','expired')


def render(rows,now):
    counts={r['status']:int(r['n']) for r in rows}
    count=lambda states:sum(counts.get(s,0) for s in states)
    active=sum(count(states) for states in ACTIVE.values())
    known=set(TERMINAL+REFUND+REVIEW).union(*(set(v) for v in ACTIVE.values()))
    review=count(REVIEW)+sum(n for state,n in counts.items() if state not in known)
    done=next((r for r in rows if r['status']=='completed'),{})
    total=Decimal(str(done.get('volume') or 0))
    today=Decimal(str(done.get('today_volume') or 0))
    lines=[f'**In progress: {active}**']
    lines.extend(f'{label}: {count(states)}' for label,states in ACTIVE.items())
    lines.extend([f'\nRefund pending: {count(REFUND)}',f'Dispute / exception review: {review}',
                  f"\nCompleted: {count(('completed',))}",f"Cancelled: {count(('cancelled',))}",
                  f"Refunded: {count(('refunded',))}",
                  f"Manual refunds: {count(('manual_refunded',))}",
                  f"Payment expired: {count(('expired',))}",
                  f"Test orders settled: {count(('test_closed',))}",
                  f'\nCompleted today: {int(done.get("today_n") or 0)}',
                  f'Today\'s completed volume: {today:,.2f} USDT',
                  f'Total completed volume: {total:,.2f} USDT',
                  '\nVolume excludes service and network fees. Today uses UTC+8.',
                  f'Updated: <t:{int(now.timestamp())}:F>'])
    return '\n'.join(lines)


class NewTradeStats(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        if cfg.TRADE_STATS_CHANNEL_ID: self.refresh.start()

    def cog_unload(self): self.refresh.cancel()

    @tasks.loop(seconds=60)
    async def refresh(self):
        try:
            channel=self.bot.get_channel(cfg.TRADE_STATS_CHANNEL_ID)
            if not isinstance(channel,discord.TextChannel) or channel.guild.id!=cfg.GUILD_ID:
                log.error('NEW_TRADE_STATS_CHANNEL_ID must be a text/announcement channel in the new guild')
                return
            community=self.bot.get_cog('NewCommunity')
            if not community: return
            now=datetime.now(TZ)
            start=now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
            shared=cfg.PAYMENTS_DATABASE
            if not re.fullmatch(r'[A-Za-z0-9_]+',shared): raise ValueError('Invalid payment database name')
            rows=await db.query(f'''SELECT
                CASE WHEN p.state IN ('review','unknown','failed') THEN 'payout_review' ELSE o.status END AS status,
                COUNT(*) AS n,SUM(o.amount) AS volume,
                SUM(CASE WHEN o.closed_at>=%s AND o.closed_at<%s THEN o.amount ELSE 0 END) AS today_volume,
                SUM(CASE WHEN o.closed_at>=%s AND o.closed_at<%s THEN 1 ELSE 0 END) AS today_n
                FROM orders o LEFT JOIN `{shared}`.payouts p ON p.order_key=CONCAT('new:',o.id)
                GROUP BY CASE WHEN p.state IN ('review','unknown','failed') THEN 'payout_review' ELSE o.status END''',
                (start,start+timedelta(days=1),start,start+timedelta(days=1)))
            await community.save_panel(channel.id,'trade_stats','Trade Statistics',render(rows,now))
        except Exception:
            log.exception('Trade statistics refresh failed')

    @refresh.before_loop
    async def before_refresh(self): await self.bot.wait_until_ready()


def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID!=config.GUILD_ID:
        bot.add_cog(NewTradeStats(bot))
