"""Independent new-guild orders; shared account ledger owns payment side effects."""
import asyncio
import logging
import uuid
from pathlib import Path
from decimal import Decimal
import discord
from discord.commands import user_command
from discord.ext import commands, tasks
import config
from utils import new_store as db, shared_payments as payments, binance_api
from modules.new_community import buttons, admin, texts

cfg=config.NEW
log=logging.getLogger(__name__)
TERMINAL=('completed','cancelled','refunded')
BANNER_DIR=Path(__file__).resolve().parents[1] / 'png'
# Highlight the next action, rather than the action that just finished.
STEP_BANNERS={
    'created':'initiate trade.png',
    'pending':'confirm order.png',
    'confirmed':'verify payment.png',
    'invoicing':'verify payment.png',
    'paying':'verify payment.png',
    'paid':'ship item.png',
    'shipped':'confirm receipt.png',
    'receipt_confirmed':'receive payment.png',
    'releasing':'receive payment.png',
    'completed':'receive payment.png',
}

class NewTrading(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.scan_done=False
        self.forum_lock=asyncio.Lock()
        self.pending_forums=set()
        self.worker.start()

    def cog_unload(self): self.worker.cancel()

    @user_command(name="开始交易(buy)", guild_ids=[cfg.GUILD_ID] if cfg.GUILD_ID else None)
    async def trade_callback(self, ctx, user: discord.User):
        await self.start(ctx, user, buy=True)

    @user_command(name="开始交易(sell)", guild_ids=[cfg.GUILD_ID] if cfg.GUILD_ID else None)
    async def trade_sell_callback(self, ctx, user: discord.User):
        await self.start(ctx, user, buy=False)

    async def order(self,ident):
        return await db.query('SELECT * FROM orders WHERE id=%s',(ident,),one=True)

    async def transition(self,ident,old,new,actor,details=None):
        async with db.transaction() as cur:
            await cur.execute('UPDATE orders SET status=%s WHERE id=%s AND status=%s',(new,ident,old))
            if cur.rowcount!=1: raise ValueError('订单状态已改变，请使用最新交易消息')
            await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                              (actor,new,db.encode(details or {'from':old}),ident))
            if new in TERMINAL:
                await cur.execute('UPDATE orders SET closed_at=UTC_TIMESTAMP() WHERE id=%s',(ident,))
                await cur.execute('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=FALSE WHERE channel_id=(SELECT channel_id FROM orders WHERE id=%s)',(ident,))
            if new=='disputed':
                await cur.execute('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=(SELECT channel_id FROM orders WHERE id=%s) OR channel_id=(SELECT source_id FROM orders WHERE id=%s)',(ident,ident))

    def view(self,row):
        ident=row['id']
        options={
            'pending':[('确认交易','confirm'),('取消','cancel')],
            'confirmed':[('获取付款信息','pay'),('取消','cancel')],
            'paid':[('已发货','ship'),('发起争议','dispute')],
            'shipped':[('确认收货','receipt'),('发起争议','dispute')],
            'receipt_confirmed':[('领取货款','collect')],
            'refund_ready':[('领取退款','collect')],
            'completed':[('保留频道并通知管理员','keep')],
            'cancelled':[('保留频道并通知管理员','keep')],
            'refunded':[('保留频道并通知管理员','keep')],
        }.get(row['status'],[])
        return buttons([(label,'trade:'+action+':'+ident) for label,action in options])

    async def send_step(self,channel,status,embed=None,view=None):
        """Attach a local banner above the body, only inside an order channel."""
        if channel.guild.id!=cfg.GUILD_ID:
            return
        file=None
        filename=STEP_BANNERS.get(status)
        if filename:
            try:
                file=discord.File(BANNER_DIR / filename,filename='trade-step.png')
            except OSError:
                log.exception('Trade banner unavailable: %s',filename)
        embeds=[]
        if file:
            banner=discord.Embed(color=0x9854DE)
            banner.set_image(url='attachment://trade-step.png')
            embeds.append(banner)
        if embed is not None:
            embeds.append(embed)
        if not embeds:
            return
        try:
            kwargs={'embeds':embeds,'allowed_mentions':discord.AllowedMentions.none()}
            if view is not None: kwargs['view']=view
            if file: kwargs['file']=file
            await channel.send(**kwargs)
        finally:
            if file: file.close()

    async def post(self,row,extra=''):
        channel=self.bot.get_channel(row['channel_id'])
        if not channel or channel.guild.id!=cfg.GUILD_ID:
            return
        if row['status'] in TERMINAL:
            extra+='\n频道将在约 5 分钟后清理。如需保留，请点击下方按钮。'
        embed=discord.Embed(title='担保交易',description=f"商品：{row['item']}\n约定：{row['terms']}\n\n状态：{row['status']}\n{extra}",color=0x9854DE)
        embed.add_field(name='买家 / 卖家',value=f"<@{row['buyer_id']}> / <@{row['seller_id']}>")
        embed.add_field(name='商品价款 / 服务费',value=f"{row['amount']} / {row['fee']} USDT")
        embed.set_footer(text='订单 '+row['id'])
        await self.send_step(channel,row['status'],embed,self.view(row))

    async def start(self,ctx,other,buy=True,source=None):
        guild=ctx.guild
        user=getattr(ctx,'user',None) or ctx.author
        if not guild or guild.id!=cfg.GUILD_ID or other.id==user.id or other.bot:
            return await ctx.respond('请选择本社群的其他成员。',ephemeral=True) if hasattr(ctx,'respond') else await ctx.response.send_message('请选择本社群的其他成员。',ephemeral=True)
        if not cfg.PAYMENTS_ENABLED:
            msg='共享支付账本尚未启用，请管理员完成迁移与检查后开放交易。'
            return await ctx.respond(msg,ephemeral=True) if hasattr(ctx,'respond') else await ctx.response.send_message(msg,ephemeral=True)
        modal=discord.ui.Modal(title='担保交易条件')
        item=discord.ui.InputText(label='商品名称及数量',max_length=150)
        amount=discord.ui.InputText(label='商品价格 USDT（最多两位小数）',max_length=20)
        terms=discord.ui.InputText(label='交付方式、期限及特别约定',style=discord.InputTextStyle.long,max_length=1500)
        for field in (item,amount,terms): modal.add_item(field)
        async def submitted(inter):
            await inter.response.defer(ephemeral=True)
            channel=None
            try:
                if inter.user.id!=user.id: raise ValueError('仅发起者可提交此表单')
                value=payments.money(amount.value)
                if value!=value.quantize(Decimal('.01')): raise ValueError('商品金额最多两位小数')
                member=await guild.fetch_member(other.id)
                if member.bot: raise ValueError('不支持与 bot 交易')
                category=self.bot.get_channel(cfg.TRADE_CATEGORY_ID)
                if not isinstance(category,discord.CategoryChannel) or category.guild.id!=guild.id:
                    raise ValueError('交易分类配置不正确')
                buyer,seller=(user.id,other.id) if buy else (other.id,user.id)
                ident=uuid.uuid4().hex
                # Lock balance rows in sorted order to serialize concurrent admission.
                async with db.transaction() as cur:
                    for uid in sorted((buyer,seller)):
                        await cur.execute('INSERT IGNORE INTO balances(user_id) VALUES(%s)',(uid,))
                        await cur.execute('SELECT user_id FROM balances WHERE user_id=%s FOR UPDATE',(uid,))
                        await cur.fetchone()
                        await cur.execute("SELECT COUNT(*) AS n FROM orders WHERE (buyer_id=%s OR seller_id=%s) AND status NOT IN ('completed','cancelled','refunded')",(uid,uid))
                        if (await cur.fetchone())['n']>=cfg.MAX_ACTIVE: raise ValueError('交易参与者已达到同时进行的订单上限')
                    await cur.execute('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,source_id,item,terms,amount,fee) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                      (ident,buyer,seller,user.id,source,item.value,terms.value,value,cfg.FEE))
                overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False),guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),user:discord.PermissionOverwrite(view_channel=True,send_messages=True),member:discord.PermissionOverwrite(view_channel=True,send_messages=True)}
                for rid in cfg.TRADE_ADMIN_ROLES:
                    role=guild.get_role(rid)
                    if role: overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
                try:
                    channel=await guild.create_text_channel('trade-'+ident[:8],category=category,overwrites=overwrites,reason='New guild escrow order '+ident)
                    await db.query('UPDATE orders SET channel_id=%s WHERE id=%s',(channel.id,ident))
                    await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(%s,'trade')",(channel.id,))
                except Exception:
                    await self.transition(ident,'pending','cancelled',user.id,{'reason':'channel creation/setup failed'})
                    raise
                await db.audit(user.id,'create',{'terms':terms.value,'source':source},ident)
                await self.send_step(channel,'created')
                await self.post(await self.order(ident),texts()['payment_notice'])
                await inter.followup.send(f'交易已创建：{channel.mention}',ephemeral=True)
            except Exception as exc:
                log.exception('Create new order failed')
                await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '建单失败，请联系管理员核对。',ephemeral=True)
        modal.callback=submitted
        if hasattr(ctx,'send_modal'): await ctx.send_modal(modal)
        else: await ctx.response.send_modal(modal)

    async def forum(self,thread):
        if thread.guild.id!=cfg.GUILD_ID or thread.parent_id not in cfg.FORUM_IDS:
            return
        # Thread-create and starter-message events may arrive concurrently.
        async with self.forum_lock:
            await self._forum(thread)

    async def _forum(self,thread):
        self.pending_forums.discard(thread.id)
        tags={t.id for t in thread.applied_tags}
        buy=cfg.BUY_TAGS.get(thread.parent_id) in tags
        sell=cfg.SELL_TAGS.get(thread.parent_id) in tags
        stored=await db.setting('forum:'+str(thread.id))
        if not buy and not sell and not stored: return
        await db.query("INSERT IGNORE INTO tracked_channels(channel_id,kind) VALUES(%s,'forum')",(thread.id,))
        text=texts()['forum_prompt'] if buy!=sell else '请在 buy / sell 中仅保留一个标签后发起担保交易。'
        view=buttons([('购买' if sell else '出售','forum:'+str(thread.id))]) if buy!=sell else buttons([])
        if stored:
            try:
                msg=await thread.fetch_message(stored)
                await msg.edit(content=text,view=view)
                return
            except discord.NotFound: pass
        if not thread.archived and not thread.locked:
            try:
                # Discord can emit THREAD_CREATE before the starter is available.
                await thread.fetch_message(thread.id)
            except discord.NotFound as exc:
                if exc.code!=10008: raise
                self.pending_forums.add(thread.id)
                return
            try:
                msg=await thread.send(text,view=view)
            except discord.Forbidden as exc:
                if exc.code!=40058: raise  # Real permission failures must remain visible.
                self.pending_forums.add(thread.id)
                log.info('Waiting for forum starter message: %s',thread.id)
                return
            await db.setting('forum:'+str(thread.id),msg.id)

    @commands.Cog.listener()
    async def on_thread_create(self,thread):
        try: await self.forum(thread)
        except Exception: log.exception('Forum setup failed')

    @commands.Cog.listener()
    async def on_thread_update(self,before,after):
        if before.applied_tags!=after.applied_tags:
            await self.on_thread_create(after)

    @commands.Cog.listener()
    async def on_message(self,message):
        if (message.guild and message.guild.id==cfg.GUILD_ID
                and isinstance(message.channel,discord.Thread)
                and message.channel.parent_id in cfg.FORUM_IDS
                and message.id==message.channel.id):
            await self.on_thread_create(message.channel)

    async def retry_forums(self,guild):
        threads={t.id:t for t in guild.threads}
        targets=list(threads) if not self.scan_done else list(self.pending_forums)
        self.scan_done=True
        for ident in targets:
            thread=threads.get(ident)
            if thread is None:
                self.pending_forums.discard(ident)
                continue
            try:
                await self.forum(thread)
            except Exception:
                # A forum permissions error must not stop payment reconciliation.
                log.exception('Forum setup failed: %s',ident)

    @commands.Cog.listener()
    async def on_interaction(self,inter):
        custom=(inter.data or {}).get('custom_id','')
        if not inter.guild or inter.guild.id!=cfg.GUILD_ID: return
        if custom.startswith('new:forum:'):
            thread=inter.channel
            if not isinstance(thread,discord.Thread) or str(thread.id)!=custom.split(':')[-1] or thread.parent_id not in cfg.FORUM_IDS: return
            tags={t.id for t in thread.applied_tags}
            buy=cfg.BUY_TAGS.get(thread.parent_id) in tags
            sell=cfg.SELL_TAGS.get(thread.parent_id) in tags
            if buy==sell or thread.locked or thread.archived:
                return await inter.response.send_message('此贴文当前不支持发起交易。',ephemeral=True)
            owner=await inter.guild.fetch_member(thread.owner_id)
            return await self.start(inter,owner,buy=sell,source=thread.id)
        if not custom.startswith('new:trade:'): return
        _,_,action,ident=custom.split(':',3)
        row=await self.order(ident)
        if not row or inter.channel_id!=row['channel_id'] or inter.user.id not in (row['buyer_id'],row['seller_id']):
            return await inter.response.send_message('无权操作此订单。',ephemeral=True)
        if action=='collect':
            payee=row['buyer_id'] if row['status']=='refund_ready' else row['seller_id']
            if inter.user.id!=payee or row['status'] not in ('receipt_confirmed','refund_ready'):
                return await inter.response.send_message('当前不能领取货款。',ephemeral=True)
            return await self.address_modal(inter,row)
        await inter.response.defer(ephemeral=True)
        try:
            actor=inter.user.id
            if action=='keep':
                if row['status'] not in TERMINAL: raise ValueError('仅用于保留已结束订单频道')
                await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s',(row['channel_id'],))
                await db.audit(actor,'retain_channel',{},ident)
                await self.alert('用户请求保留交易频道：'+ident)
                return await inter.followup.send('已请求保留，管理员将核对。',ephemeral=True)
            elif action=='confirm':
                if actor==row['initiator_id']: raise ValueError('请等待交易对方确认')
                await self.transition(ident,'pending','confirmed',actor)
            elif action=='pay':
                if actor!=row['buyer_id']: raise ValueError('只有买家可以付款')
                async with db.transaction() as cur:
                    await cur.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(ident,))
                    locked=await cur.fetchone()
                    if locked['status']!='confirmed': raise ValueError('此订单已生成账单或状态已变化')
                    await cur.execute('SELECT * FROM balances WHERE user_id=%s FOR UPDATE',(actor,))
                    balance=await cur.fetchone()
                    credit=min(balance['available'],locked['fee'])
                    await cur.execute('UPDATE balances SET available=available-%s,reserved=reserved+%s WHERE user_id=%s',(credit,credit,actor))
                    await cur.execute("UPDATE orders SET credits=%s,status='invoicing' WHERE id=%s",(credit,ident))
                # A durable intermediate state lets the worker recover failures.
                await self.prepare_invoice(await self.order(ident))
            elif action=='cancel':
                if row['status'] not in ('pending','confirmed'): raise ValueError('已生成账单，不能直接取消；请联系管理员')
                await self.transition(ident,row['status'],'cancelled',actor)
            elif action=='ship':
                if actor!=row['seller_id']: raise ValueError('只有卖家可以标记发货')
                await self.transition(ident,'paid','shipped',actor)
            elif action=='receipt':
                if actor!=row['buyer_id']: raise ValueError('只有买家可以确认收货')
                # Require a second explicit confirmation, bound to the buyer and order.
                view=discord.ui.View(timeout=180)
                button=discord.ui.Button(label='确认已收到商品，允许卖家收款',style=discord.ButtonStyle.danger)
                async def confirm(click):
                    if click.user.id!=row['buyer_id']: return
                    await click.response.defer(ephemeral=True)
                    try:
                        await self.transition(ident,'shipped','receipt_confirmed',click.user.id)
                        await self.post(await self.order(ident))
                        await click.followup.send('已确认收货。',ephemeral=True)
                    except ValueError as exc: await click.followup.send(str(exc),ephemeral=True)
                button.callback=confirm
                view.add_item(button)
                return await inter.followup.send('请核对商品；确认后卖家可领取货款。',view=view,ephemeral=True)
            elif action=='dispute':
                if row['status'] not in ('paid','shipped'): raise ValueError('当前无法发起争议，请联系管理员')
                await self.transition(ident,row['status'],'disputed',actor)
                await self.alert('新社群订单发生争议：'+ident)
            else: return
            await self.post(await self.order(ident))
            await inter.followup.send('操作完成。',ephemeral=True)
        except Exception as exc:
            log.exception('New order operation failed')
            await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '操作未完成，请勿重复付款，联系管理员核对。',ephemeral=True)

    async def prepare_invoice(self,row):
        address=await binance_api.get_deposit_address('USDT','BSC')
        if not address: raise ValueError('无法获取收款地址')
        key='new:'+row['id']
        amount=await payments.invoice(key,address,row['amount']+row['fee']-row['credits'])
        await db.query('UPDATE orders SET address=%s WHERE id=%s',(address,row['id']))
        await self.transition(row['id'],'invoicing','paying',self.bot.user.id)
        await self.post(await self.order(row['id']),f'实际到账金额：{amount:.6f} USDT\n网络：BSC / BEP20\n地址：{address}\n30 分钟内付款。'+texts()['payment_notice'])

    async def address_modal(self,inter,row):
        refund=row['status']=='refund_ready'
        payee=row['buyer_id'] if refund else row['seller_id']
        gross=row['amount']
        if refund:
            deposit=await db.query('SELECT amount FROM deposits WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if not deposit:
                return await inter.response.send_message('没有已核对的到账流水，不能退款。',ephemeral=True)
            gross=deposit['amount']
        modal=discord.ui.Modal(title='退款地址' if refund else '卖家收款地址')
        address=discord.ui.InputText(label='USDT-BEP20 地址',min_length=42,max_length=42)
        modal.add_item(address)
        async def submitted(click):
            await click.response.defer(ephemeral=True)
            try:
                fee,net=await payments.payout_quote(address.value.strip(),gross)
                view=discord.ui.View(timeout=180)
                button=discord.ui.Button(label='确认地址与预计费用，申请放款',style=discord.ButtonStyle.danger)
                async def accepted(confirm):
                    if confirm.user.id!=payee: return
                    await confirm.response.defer(ephemeral=True)
                    try:
                        fresh_fee,fresh_net=await payments.payout_quote(address.value.strip(),gross)
                        if (fee,net)!=(fresh_fee,fresh_net): raise ValueError('费用已变化，请重新领取查看报价')
                        await self.transition(row['id'],row['status'],'releasing_refund' if refund else 'releasing',confirm.user.id,{'address':address.value,'gross':gross,'fee':fee,'net':net})
                        await db.query('UPDATE orders SET address=%s WHERE id=%s',(address.value.strip(),row['id']))
                        await payments.release('new:'+row['id'],address.value.strip(),gross,fee,net)
                        await confirm.followup.send('提现已提交或结果待核对。请勿重复操作，确认最终结果后会更新订单。',ephemeral=True)
                    except Exception as exc:
                        log.exception('Payout request failed')
                        await confirm.followup.send(str(exc) if isinstance(exc,ValueError) else '放款结果待核对，请联系管理员，勿重复提现。',ephemeral=True)
                button.callback=accepted
                view.add_item(button)
                summary=(f'退款总额（含已付服务费）：{gross} U\n网络费从退款中扣除，由退款领取方承担。'
                         if refund else f'放款总额：{gross} U\n网络费从卖家货款中扣除。')
                await click.followup.send(f'地址：{address.value}\n{summary}\n预计网络费：{fee} U\n预计到账：{net} U\n金额精度舍入差额：{gross-fee-net} U\n内部转账是否免手续费以实际渠道结果为准。',view=view,ephemeral=True)
            except Exception as exc:
                await click.followup.send(str(exc) if isinstance(exc,ValueError) else '暂时无法获取提现报价。',ephemeral=True)
        modal.callback=submitted
        await inter.response.send_modal(modal)

    async def alert(self,text):
        channel=self.bot.get_channel(cfg.ALERT_CHANNEL_ID)
        if channel and channel.guild.id==cfg.GUILD_ID:
            await channel.send(text,allowed_mentions=discord.AllowedMentions.none())
        log.warning(text)

    @tasks.loop(seconds=60)
    async def worker(self):
        try:
            guild=self.bot.get_guild(cfg.GUILD_ID)
            if not guild: return
            await self.retry_forums(guild)
            if not cfg.PAYMENTS_ENABLED: return
            finished=await db.query("SELECT o.* FROM orders o JOIN tracked_channels c ON c.channel_id=o.channel_id WHERE o.status IN ('completed','cancelled','refunded') AND c.hold=FALSE AND o.closed_at<UTC_TIMESTAMP()-INTERVAL 5 MINUTE")
            for done in finished:
                channel=self.bot.get_channel(done['channel_id'])
                if channel and channel.guild.id==cfg.GUILD_ID:
                    # Re-check immediately before destructive Discord operation.
                    tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(channel.id,),one=True)
                    if tracked and not tracked['hold']:
                        await db.audit(self.bot.user.id,'channel_cleanup',{'channel':channel.id},done['id'])
                        await channel.delete(reason='Completed escrow channel cleanup')
            rows=await db.query("SELECT * FROM orders WHERE status IN ('invoicing','paying','payment_review','releasing','releasing_refund')")
            for row in rows:
                try:
                    key='new:'+row['id']
                    if row['status']=='invoicing':
                        await self.prepare_invoice(row)
                    elif row['status'] in ('paying','payment_review'):
                        inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
                        if not inv: continue
                        txid=await payments.find_deposit(key,inv['address'],inv['amount'])
                        if txid:
                            deposit=await db.query('SELECT payload FROM deposits WHERE order_key=%s',(key,),shared=True,one=True)
                            import json
                            from datetime import timezone
                            inserted=json.loads(deposit['payload']).get('insertTime',0)
                            deadline=int(inv['expires_at'].replace(tzinfo=timezone.utc).timestamp()*1000)
                            if inserted>deadline and row['status']=='paying':
                                await self.transition(row['id'],'paying','payment_review',self.bot.user.id,{'reason':'late deposit'})
                                await self.alert('收到迟到账款，请人工核对：'+row['id'])
                                continue
                            if row['status']=='payment_review':
                                # Stay review-only: no automatic allocation/refund after expiry.
                                continue
                            async with db.transaction() as cur:
                                await cur.execute("UPDATE orders SET status='paid' WHERE id=%s AND status='paying'",(row['id'],))
                                if cur.rowcount:
                                    await cur.execute('UPDATE balances SET reserved=reserved-%s WHERE user_id=%s AND reserved>=%s',(row['credits'],row['buyer_id'],row['credits']))
                                    await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',(self.bot.user.id,'paid',db.encode({'txid':txid,'credits':row['credits']}),row['id']))
                            await self.post(await self.order(row['id']))
                        else:
                            from datetime import datetime
                            if inv['expires_at']<=datetime.utcnow() and row['status']=='paying':
                                await self.transition(row['id'],'paying','payment_review',self.bot.user.id)
                                await self.alert('付款已超时，保留订单供迟到账核对：'+row['id'])
                    else:
                        payout=await payments.reconcile(key)
                        if not payout:
                            intent=await db.query('SELECT details FROM audit WHERE order_id=%s AND action=%s ORDER BY id DESC LIMIT 1',
                                (row['id'],row['status']),one=True)
                            if intent:
                                import json
                                data=json.loads(intent['details'])
                                gross=Decimal(data['gross'])
                                await payments.release(key,data['address'].strip(),gross,Decimal(data['fee']),Decimal(data['net']))
                                continue
                        if payout and payout['state']=='completed':
                            await self.transition(row['id'],row['status'],'refunded' if row['status']=='releasing_refund' else 'completed',self.bot.user.id,{'withdrawal':payout['provider_id'],'provider_result':payout['payload']})
                            await self.post(await self.order(row['id']))
                        elif not payout or payout['state'] in ('unknown','failed','review'):
                            notified=await db.setting('payout_alert:'+row['id'])
                            if not notified:
                                await self.alert('放款需人工核对，禁止重复提交：'+row['id']+('；资金安全校验异常，新的自动放款已暂停。' if payout and payout['state']=='review' else ''))
                                await db.setting('payout_alert:'+row['id'],True)
                except Exception: log.exception('Order recovery failed: %s',row['id'])
        except Exception: log.exception('New trading worker failed')

    @worker.before_loop
    async def before_worker(self): await self.bot.wait_until_ready()

    @discord.slash_command(name='new_trade_review',description='处理争议或异常订单（需二次确认）')
    async def review(self,ctx,order_id:str,decision:discord.Option(str,choices=['记录意见','继续履约','允许卖家收款','退还买家','无到账关闭']),reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        row=await self.order(order_id)
        if not row: return await ctx.respond('订单不存在。',ephemeral=True)
        if not reason.strip(): return await ctx.respond('必须填写原因。',ephemeral=True)
        if decision=='记录意见':
            await db.audit(ctx.author.id,'review_decision',{'decision':decision,'reason':reason,'status':row['status']},order_id)
            return await ctx.respond('意见已保存。',ephemeral=True)
        view=discord.ui.View(timeout=180)
        button=discord.ui.Button(label='确认处理此订单',style=discord.ButtonStyle.danger)
        async def accepted(inter):
            if inter.user.id!=ctx.author.id or not admin(inter.user,cfg.TRADE_ADMIN_ROLES): return
            await inter.response.defer(ephemeral=True)
            try:
                fresh=await self.order(order_id)
                if fresh['status'] not in ('disputed','payment_review'):
                    raise ValueError('仅能处理争议或待核对订单；正在放款的订单不能改判')
                inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+order_id,),shared=True,one=True)
                if inv:
                    await payments.find_deposit('new:'+order_id,inv['address'],inv['amount'])
                deposit=await db.query('SELECT amount FROM deposits WHERE order_key=%s',('new:'+order_id,),shared=True,one=True)
                if decision=='无到账关闭' and deposit: raise ValueError('已找到到账记录，不能作为无到账订单关闭')
                if decision!='无到账关闭' and not deposit: raise ValueError('没有已核对的到账记录，禁止放款或退款')
                target={'继续履约':'paid','允许卖家收款':'receipt_confirmed','退还买家':'refund_ready','无到账关闭':'cancelled'}[decision]
                async with db.transaction() as cur:
                    await cur.execute('UPDATE orders SET status=%s WHERE id=%s AND status=%s',(target,order_id,fresh['status']))
                    if cur.rowcount!=1: raise ValueError('订单状态已变化')
                    credit=fresh['credits']
                    if fresh['status']=='payment_review':
                        await cur.execute('UPDATE balances SET reserved=reserved-%s WHERE user_id=%s AND reserved>=%s',(credit,fresh['buyer_id'],credit))
                    if decision in ('退还买家','无到账关闭'):
                        await cur.execute('UPDATE balances SET available=available+%s WHERE user_id=%s',(credit,fresh['buyer_id']))
                    await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',(inter.user.id,'review_decision',db.encode({'decision':decision,'reason':reason,'from':fresh['status']}),order_id))
                    if target=='cancelled':
                        await cur.execute('UPDATE orders SET closed_at=UTC_TIMESTAMP() WHERE id=%s',(order_id,))
                        await cur.execute('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=FALSE WHERE channel_id=%s',(fresh['channel_id'],))
                await self.post(await self.order(order_id))
                await inter.followup.send('处理决定已保存。需要收款或退款时，由对应交易方确认地址后提交。',ephemeral=True)
            except Exception as exc:
                log.exception('Review settlement failed')
                await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '核对失败，请勿重复处理。',ephemeral=True)
        button.callback=accepted
        view.add_item(button)
        await ctx.respond(f'订单 {order_id}\n决定：{decision}\n原因：{reason}\n退款将退还已认领入款，网络费由退款领取方承担。',view=view,ephemeral=True)

def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID!=config.GUILD_ID: bot.add_cog(NewTrading(bot))
