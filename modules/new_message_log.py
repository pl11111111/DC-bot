"""Latest-message cache and durable edit/delete events, with bounded retention."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands, tasks
import config
from utils import new_store as db
from modules.new_community import admin

cfg=config.NEW
log=logging.getLogger(__name__)

def attachments(items):
    return [{'name':a.filename,'url':a.url} for a in items]

def naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)

class NewMessageLog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.lock=asyncio.Lock()
        self.bytes=0
        self.warn_level=0
        self.cleanup.start()

    def cog_unload(self): self.cleanup.cancel()

    async def tracked(self,channel_id):
        return await db.query('SELECT * FROM tracked_channels WHERE channel_id=%s',(channel_id,),one=True)

    async def warn(self,text):
        log.warning(text)
        channel=self.bot.get_channel(cfg.ALERT_CHANNEL_ID)
        if channel and channel.guild.id==cfg.GUILD_ID:
            await channel.send(text,allowed_mentions=discord.AllowedMentions.none())

    async def event(self,mid,cid,author,kind,payload):
        encoded=db.encode(payload)
        estimate=len(encoded.encode('utf-8'))+512
        if self.bytes+estimate>cfg.LOG_HARD_BYTES:
            if self.warn_level<3:
                self.warn_level=3
                await self.warn('消息记录达到 3 GB 上限，新记录无法完整保存。请处理容量；争议证据没有自动删除。')
            return False
        await db.query('INSERT INTO message_events(message_id,channel_id,author_id,kind,payload) VALUES(%s,%s,%s,%s,%s)',(mid,cid,author,kind,encoded))
        self.bytes+=estimate
        return True

    @commands.Cog.listener()
    async def on_message(self,message):
        if not message.guild or message.guild.id!=cfg.GUILD_ID or message.author.bot: return
        async with self.lock:
            if not await self.tracked(message.channel.id):
                # Catch forum starter messages even before thread-create listener finishes.
                channel=message.channel
                if not isinstance(channel,discord.Thread) or channel.parent_id not in cfg.FORUM_IDS: return
                tags={t.id for t in channel.applied_tags}
                if cfg.BUY_TAGS.get(channel.parent_id) not in tags and cfg.SELL_TAGS.get(channel.parent_id) not in tags: return
                await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(%s,'forum') ON DUPLICATE KEY UPDATE channel_id=channel_id",(channel.id,))
            if self.bytes>=cfg.LOG_WARN_BYTES:
                # Leave room for edit/delete evidence; do not create new snapshots.
                return
            now=datetime.utcnow()
            await db.query('INSERT IGNORE INTO messages(message_id,channel_id,author_id,body,attachments,sent_at,updated_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                (message.id,message.channel.id,message.author.id,message.content,db.encode(attachments(message.attachments)),naive(message.created_at),now,now+timedelta(days=30)))
            self.bytes+=len(message.content.encode('utf-8'))+len(db.encode(attachments(message.attachments)).encode('utf-8'))+512

    @commands.Cog.listener()
    async def on_raw_message_edit(self,payload):
        # One ordered pipeline: preserve the edit before moderation can delete it.
        try:
            await self.record_edit(payload)
        finally:
            bot = getattr(self, 'bot', None)
            moderation = bot.get_cog('NewModeration') if bot else None
            if moderation:
                await moderation.check_edit(payload)

    async def record_edit(self,payload):
        if payload.guild_id!=cfg.GUILD_ID: return
        # Embed refreshes do not represent user edits.
        if not any(k in payload.data for k in ('content','attachments')): return
        async with self.lock:
            if not await self.tracked(payload.channel_id): return
            old=await db.query('SELECT * FROM messages WHERE message_id=%s',(payload.message_id,),one=True)
            if payload.data.get('author',{}).get('bot'): return
            old_body=old['body'] if old else None
            new_body=payload.data.get('content',old_body)
            new_files=db.encode([{'name':a.get('filename'),'url':a.get('url')} for a in payload.data['attachments']]) if 'attachments' in payload.data else (old['attachments'] if old else '[]')
            if old and old_body==new_body and old['attachments']==new_files: return
            author=old['author_id'] if old else payload.data.get('author',{}).get('id')
            ok=await self.event(payload.message_id,payload.channel_id,author,'edit',{'before':old_body,'after':new_body,'attachments_before':old['attachments'] if old else None,'attachments_after':new_files,'original_unavailable':old_body is None})
            if ok and old:
                await db.query('UPDATE messages SET body=%s,attachments=%s,updated_at=UTC_TIMESTAMP(),expires_at=%s WHERE message_id=%s',
                    (new_body,new_files,datetime.utcnow()+timedelta(days=30),payload.message_id))

    async def deleted(self,mid,cid):
        async with self.lock:
            if not await self.tracked(cid): return
            old=await db.query('SELECT * FROM messages WHERE message_id=%s',(mid,),one=True)
            if await self.event(mid,cid,old['author_id'] if old else None,'delete',
                {'body':old['body'] if old else None,'attachments':old['attachments'] if old else None,'original_unavailable':not old or old['body'] is None,'deleted_by':None}):
                await db.query('DELETE FROM messages WHERE message_id=%s',(mid,))

    @commands.Cog.listener()
    async def on_raw_message_delete(self,payload):
        if payload.guild_id==cfg.GUILD_ID: await self.deleted(payload.message_id,payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self,payload):
        if payload.guild_id==cfg.GUILD_ID:
            for mid in payload.message_ids: await self.deleted(mid,payload.channel_id)

    async def channel_deleted(self,channel_id):
        record=await self.tracked(channel_id)
        if not record: return
        rows=await db.query("SELECT id FROM orders WHERE (channel_id=%s OR source_id=%s) AND status NOT IN ('completed','cancelled','refunded','test_closed')",(channel_id,channel_id))
        if rows:
            await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s',(channel_id,))
            await self.warn(f'交易相关频道被删除，临时证据已暂停清理：{channel_id}')
        else:
            await db.query('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP() WHERE channel_id=%s',(channel_id,))
        await db.audit(0,'channel_deleted',{'channel':channel_id,'active_orders':[r['id'] for r in rows],'operator':'unknown'})

    @commands.Cog.listener()
    async def on_guild_channel_delete(self,channel):
        if channel.guild.id==cfg.GUILD_ID: await self.channel_deleted(channel.id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self,payload):
        if payload.guild_id==cfg.GUILD_ID: await self.channel_deleted(payload.thread_id)

    async def measure(self):
        # Logical live data, not InnoDB file high-water size. File allocation is reported separately.
        row=await db.query("SELECT COALESCE(SUM(COALESCE(OCTET_LENGTH(body),0)+OCTET_LENGTH(attachments)+512),0) AS n FROM messages",one=True)
        events=await db.query('SELECT COALESCE(SUM(OCTET_LENGTH(payload)+512),0) AS n FROM message_events',one=True)
        self.bytes=int(row['n'])+int(events['n'])
        return self.bytes

    async def remove_expired(self,ident,event=False,clear_body=False):
        table,key=('message_events','id') if event else ('messages','message_id')
        condition=(f"{table}.{key}=%s AND EXISTS (SELECT 1 FROM tracked_channels c "
                   f"WHERE c.channel_id={table}.channel_id AND c.hold=FALSE AND NOT EXISTS "
                   "(SELECT 1 FROM orders o WHERE (o.channel_id=c.channel_id OR o.source_id=c.channel_id) "
                   "AND o.status NOT IN ('completed','cancelled','refunded','test_closed')))")
        # Re-evaluate protection when deleting, not only when selecting the batch.
        if clear_body:
            return await db.query("UPDATE messages SET body=NULL,attachments='[]' WHERE "+condition,(ident,))
        return await db.query(f'DELETE FROM {table} WHERE '+condition,(ident,))

    @tasks.loop(minutes=1)
    async def cleanup(self):
        try:
            async with self.lock:
                await self.measure()
                # Hold/protect both private channels and source forums of active orders.
                safe="c.hold=FALSE AND NOT EXISTS (SELECT 1 FROM orders o WHERE (o.channel_id=c.channel_id OR o.source_id=c.channel_id) AND o.status NOT IN ('completed','cancelled','refunded','test_closed'))"
                expired=f"SELECT m.message_id FROM messages m JOIN tracked_channels c ON c.channel_id=m.channel_id WHERE {safe} AND m.body IS NOT NULL AND ((c.kind='forum' AND m.expires_at<UTC_TIMESTAMP()) OR (c.closed_at IS NOT NULL AND c.closed_at<UTC_TIMESTAMP()-INTERVAL 7 DAY)) ORDER BY m.updated_at LIMIT 500"
                rows=await db.query(expired)
                for row in rows:
                    await self.remove_expired(row['message_id'],clear_body=True)
                events=await db.query(f'SELECT e.id FROM message_events e JOIN tracked_channels c ON c.channel_id=e.channel_id WHERE {safe} AND e.created_at<UTC_TIMESTAMP()-INTERVAL 180 DAY ORDER BY e.id LIMIT 500')
                for row in events: await self.remove_expired(row['id'],event=True)
                indexes=await db.query(f'SELECT m.message_id FROM messages m JOIN tracked_channels c ON c.channel_id=m.channel_id WHERE {safe} AND m.body IS NULL AND m.updated_at<UTC_TIMESTAMP()-INTERVAL 180 DAY ORDER BY m.updated_at LIMIT 500')
                for row in indexes: await self.remove_expired(row['message_id'])
                if self.bytes>=cfg.LOG_SOFT_BYTES:
                    # Bounded batches; progressively recover to target across subsequent runs.
                    await db.setting('log_pressure',True)
                pressure=await db.setting('log_pressure')
                cleaned=0
                if pressure:
                    rows=await db.query(f'SELECT m.message_id FROM messages m JOIN tracked_channels c ON c.channel_id=m.channel_id WHERE {safe} ORDER BY (c.kind=\'trade\') DESC,m.updated_at LIMIT 500')
                    for row in rows:
                        await self.remove_expired(row['message_id'])
                    cleaned+=len(rows)
                    if not rows:
                        events=await db.query(f'SELECT e.id FROM message_events e JOIN tracked_channels c ON c.channel_id=e.channel_id WHERE {safe} AND e.created_at<UTC_TIMESTAMP()-INTERVAL 30 DAY ORDER BY e.id LIMIT 500')
                        for row in events: await self.remove_expired(row['id'],event=True)
                        cleaned+=len(events)
                    await self.measure()
                    if self.bytes<=cfg.LOG_TARGET_BYTES: await db.setting('log_pressure',False)
                    if cleaned: await db.audit(0,'capacity_cleanup',{'rows':cleaned,'logical_bytes_after':self.bytes})
                if self.bytes>=cfg.LOG_WARN_BYTES and self.warn_level<2:
                    self.warn_level=2
                    await self.warn(f'消息记录占用约 {self.bytes/1e9:.2f} GB，已停止新增普通正文以预留证据空间。')
                elif self.bytes<cfg.LOG_SOFT_BYTES: self.warn_level=0
            # Durable outbox: Discord delivery is independent from evidence persistence.
            channel=self.bot.get_channel(cfg.LOG_CHANNEL_ID)
            if channel and channel.guild.id==cfg.GUILD_ID:
                for event in await db.query('SELECT * FROM message_events WHERE delivered=FALSE ORDER BY id LIMIT 10'):
                    import io
                    payload=event['payload']
                    await channel.send(f"{event['kind']} | 频道 {event['channel_id']} | 消息 {event['message_id']} | 作者 {event['author_id'] or '未知'}",
                        file=discord.File(io.BytesIO(payload.encode('utf-8')),filename=f"event-{event['id']}.txt"),allowed_mentions=discord.AllowedMentions.none())
                    await db.query('UPDATE message_events SET delivered=TRUE WHERE id=%s',(event['id'],))
        except Exception: log.exception('New message log maintenance failed')

    @cleanup.before_loop
    async def before_cleanup(self): await self.bot.wait_until_ready()

    @discord.slash_command(name='new_log_release',description='解除已结案频道的证据保留标记')
    async def release_hold(self,ctx,channel_id:str,reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        if not channel_id.isdigit() or not reason.strip():
            return await ctx.respond('请填写频道 ID 和结案原因。',ephemeral=True)
        cid=int(channel_id)
        active=await db.query("SELECT id FROM orders WHERE (channel_id=%s OR source_id=%s) AND status NOT IN ('completed','cancelled','refunded','test_closed')",(cid,cid))
        if active: return await ctx.respond('仍有未结束订单，不能解除证据保留。',ephemeral=True)
        view=discord.ui.View(timeout=180)
        button=discord.ui.Button(label='确认结案并恢复到期清理',emoji='🧹',style=discord.ButtonStyle.danger)
        async def confirm(inter):
            if inter.user.id!=ctx.author.id or not admin(inter.user,cfg.TRADE_ADMIN_ROLES): return
            await inter.response.defer(ephemeral=True)
            await db.query("UPDATE tracked_channels c SET hold=FALSE,closed_at=UTC_TIMESTAMP() WHERE channel_id=%s AND NOT EXISTS (SELECT 1 FROM orders o WHERE (o.channel_id=c.channel_id OR o.source_id=c.channel_id) AND o.status NOT IN ('completed','cancelled','refunded','test_closed'))",(cid,))
            await db.audit(inter.user.id,'release_evidence_hold',{'channel':cid,'reason':reason})
            await inter.followup.send('已核对并恢复符合条件记录的到期清理。',ephemeral=True)
        button.callback=confirm
        view.add_item(button)
        await ctx.respond('解除后将按保留期限和容量规则清理；请确认已结案。',view=view,ephemeral=True)

def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID!=config.GUILD_ID: bot.add_cog(NewMessageLog(bot))
