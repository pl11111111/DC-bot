"""Restore legacy trading payment watchers; never resets invoice expiry on restart."""
import asyncio
import logging
from datetime import datetime
from discord.ext import commands,tasks
import config
from utils import database,shared_payments as payments,new_store as db

log=logging.getLogger(__name__)

class PaymentRecovery(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.run.start()
    def cog_unload(self): self.run.cancel()
    @tasks.loop(seconds=60)
    async def run(self):
        if not config.NEW.PAYMENTS_ENABLED: return
        try:
            await payments.require_ready()
            trading=self.bot.get_cog('Trading')
            if not trading: return
            rows=await database.fetch_all("SELECT * FROM transactions WHERE transaction_type='trade' AND status IN ('paying','confirmed_receipt')")
            for row in rows:
                key=payments.legacy_key(row['id'])
                channel=self.bot.get_channel(row['channel_id'])
                if not channel or channel.guild.id!=config.GUILD_ID: continue
                if row['status']=='paying':
                    inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
                    if not inv: continue
                    remaining=int((inv['expires_at']-datetime.utcnow()).total_seconds())
                    if remaining<=0: continue
                    task=trading.payment_check_tasks.get(row['id'])
                    if not task or task.done():
                        await trading.payment_timeout(row['id'],remaining)
                else:
                    payout=await payments.reconcile(key)
                    if payout and payout['state']=='completed':
                        changed=await database.complete_transaction(row['id'])
                        if changed:
                            await database.set_user_active_transaction(row['buyer_id'],None,row['id'])
                            await database.set_user_active_transaction(row['seller_id'],None,row['id'])
                            await database.log_transaction_action(row['id'],'funds_released',row['seller_id'],f"提现已确认完成：{payout['provider_id']}")
                            await channel.send('提现已确认完成，交易已完成。')
        except Exception: log.exception('Legacy payment recovery failed')
    @run.before_loop
    async def before(self): await self.bot.wait_until_ready()

def setup(bot):
    if config.LEGACY_TRADING_ENABLED: bot.add_cog(PaymentRecovery(bot))
