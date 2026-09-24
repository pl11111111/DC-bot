"""Independent new-guild orders; shared account ledger owns payment side effects."""
import asyncio
import logging
import uuid
import time
from datetime import timezone, datetime, timedelta
from pathlib import Path
from decimal import Decimal
import discord
from discord.commands import user_command
from discord.ext import commands, tasks
import config
from utils import new_store as db, shared_payments as payments, binance_api, trade_card, trade_payment_ui, trade_review, trade_timeout
from modules.new_community import buttons, admin, texts

cfg=config.NEW
log=logging.getLogger(__name__)
from utils.trade_states import TERMINAL, TERMINAL_SQL, AUTO_CLEANUP, IDLE_SECONDS
from utils import trade_idle
BANNER_DIR=Path(__file__).resolve().parents[1] / 'png'
# Match the named workflow stage; creating an order renders only its first banner.
STEP_BANNERS={
    'pending':'initiate trade.png',
    'confirmed':'confirm order.png',
    'invoicing':'verify payment.png',
    'paying':'verify payment.png',
    'paid':'ship item.png',
    'shipped':'confirm receipt.png',
    'receipt_confirmed':'receive payment.png',
    'releasing':'receive payment.png',
    'completed':'receive payment.png',
}

class OrderStateChanged(ValueError):
    pass

class NewTrading(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.scan_done=False
        self.forum_lock=asyncio.Lock()
        self.pending_forums=set()
        self.post_lock=asyncio.Lock()
        self.cleanup_lock=asyncio.Lock()
        self.access_ready=False
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
            if cur.rowcount!=1: raise OrderStateChanged('订单状态已改变，请使用最新交易消息')
            await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                              (actor,new,db.encode(details or {'from':old}),ident))
            if new in TERMINAL:
                await cur.execute('UPDATE orders SET closed_at=UTC_TIMESTAMP() WHERE id=%s',(ident,))
                await cur.execute('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=FALSE WHERE channel_id=(SELECT channel_id FROM orders WHERE id=%s)',(ident,))
            if new in ('disputed','payment_review'):
                await cur.execute('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=(SELECT channel_id FROM orders WHERE id=%s) OR channel_id=(SELECT source_id FROM orders WHERE id=%s)',(ident,ident))

    def view(self,row):
        ident=row['id']
        options={
            'pending':[('确认交易','confirm'),('取消交易','cancel')],
            'confirmed':[('获取付款信息','pay'),('取消交易','cancel')],
            'paying':[('付款说明','payment_info'),('呼叫管理员','payment_help')],
            'paid':[('标记为已发货','ship'),('发起争议','dispute')],
            'shipped':[('确认收货','receipt'),('发起争议','dispute')],
            'receipt_confirmed':[('领取货款','collect')],
            'refund_ready':[('领取退款','collect')],
            'payment_timeout':[('已付款／取消关闭，请管理员核实','keep'),('付款有问题／呼叫管理员','payment_help')],
            'expired':[('保留频道并通知管理员','keep')],
            'payment_review':[('已付款／取消关闭，请管理员核实','keep'),('付款有问题／呼叫管理员','payment_help')],
            'completed':[('保留频道并通知管理员','keep')],
            'cancelled':[('保留频道并通知管理员','keep')],
            'refunded':[('保留频道并通知管理员','keep')],
            'test_closed':[('保留频道并通知管理员','keep')],
        }.get(row['status'],[])
        return buttons([(label,'trade:'+action+':'+ident) for label,action in options])

    async def send_step(self,channel,status,embed=None,view=None,qr_file=None):
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
        try:
            if qr_file is not None:
                return await trade_card.send(channel,embed,view,file,qr_file)
            return await trade_card.send(channel,embed,view,file)
        finally:
            if file: file.close()

    async def post(self,row,extra=''):
        async with self.post_lock:
            fresh=await self.order(row['id'])
            if not fresh: return
            if fresh['status']!=row['status']: extra=''
            await self._post(fresh,extra)

    def order_embed(self,row,extra=''):
        """Legacy message fields with the new ledger's amounts and workflow."""
        buyer=f"<@{row['buyer_id']}>"
        seller=f"<@{row['seller_id']}>"
        counterpart=seller if row.get('initiator_id')==row['buyer_id'] else buyer
        stages={
            'pending':('交易请求',f'{counterpart}，请查看物品及附加详情，30 分钟内点击「确认交易」，超时将结束订单。'),
            'confirmed':('交易已确认',f'{buyer}，交易已被确认。请点击「获取付款信息」继续支付。'),
            'invoicing':('正在生成付款信息','请等待系统生成账单，请勿提前转账。'),
            'paying':('等待买家付款',f'{buyer}，请按本订单付款信息转账；已付款请等待系统确认，勿重复支付。'),
            'paid':('支付已确认',f'{seller}，系统已确认买家付款。请交付物品，完成后点击「标记为已发货」。'),
            'shipped':('卖家已发货',f'{buyer}，卖家已发货。收到物品并核对无误后，请点击「确认收货」。如有问题，请发起争议。'),
            'receipt_confirmed':('买家已确认收货',f'{seller}，买家已确认收到物品。请点击「领取货款」提供收款地址。'),
            'releasing':('正在释放资金','收款申请已提交处理，请等待系统核对结果，勿重复申请。'),
            'completed':('交易已完成','买家已确认收货，系统已确认货款转出成功。感谢使用担保交易！'),
            'cancelled':('交易已取消','本订单已取消，请勿继续付款或发货。'),
            'disputed':('交易争议处理中',f'{buyer} {seller}，请保留相关证据，在此等待管理员处理。'),
            'payment_timeout':('付款已超时','请勿继续转账。频道关闭前如已付款或需要核实，请点击取消关闭按钮。'),
            'expired':('订单已超时结束','本订单已超时结束，请勿继续付款。若已付款，请联系管理员核实。'),
            'payment_review':('付款待核对','请勿继续付款或发货，请联系管理员核对账款。'),
            'refund_ready':('等待领取退款',f'{buyer}，退款已获准，请点击「领取退款」核对退款金额并提供收款地址。'),
            'releasing_refund':('正在处理退款','退款申请已提交处理，请等待系统核对结果，勿重复申请。'),
            'refunded':('退款已完成','系统已确认退款转出成功。'),
            'manual_refunded':('手动退款已登记','管理员已核实外部退款完成。本记录不会再次转账，请查看频道关闭确认通知。'),
            'test_closed':('测试订单已结清','管理员已确认本订单全部为本人测试资金，资金留存在担保账户；未执行货款转出或退款。'),
        }
        title,notice=stages.get(row['status'],('交易待核对','请联系管理员确认当前进度。'))
        embed=discord.Embed(title=title,description=notice,color=0x9854DE)
        def amount(value):
            text=format(Decimal(str(value)), 'f')
            return text.rstrip('0').rstrip('.') if '.' in text else text
        price=Decimal(str(row['amount']))
        fee=Decimal(str(row['fee']))
        credits=Decimal(str(row.get('credits',0)))
        for name,value in [('📦 物品',row['item']),('💰 价格',amount(price)+' USDT'),
                           ('🔒 托管费',amount(fee)+' USDT')]:
            embed.add_field(name=name,value=value,inline=name!='📦 物品')
        if credits:
            embed.add_field(name='🎟️ 积分抵扣',value=amount(credits)+' USDT',inline=False)
        embed.add_field(name='💵 订单合计',value=amount(price+fee-credits)+' USDT',inline=True)
        embed.add_field(name='💰 卖家货款',value=amount(price)+' USDT（转出网络费从中扣除）',inline=True)
        embed.add_field(name='🛒 买家',value=buyer,inline=True)
        embed.add_field(name='🏪 卖家',value=seller,inline=True)
        if row.get('terms'):
            embed.add_field(name='附加详情',value=row['terms'],inline=False)
        created=row.get('created_at')
        if created:
            if created.tzinfo is None: created=created.replace(tzinfo=timezone.utc)
            embed.add_field(name='🕒 创建时间',value=f'<t:{int(created.timestamp())}:F>',inline=False)
        if extra:
            embed.add_field(name='交易提示',value=extra,inline=False)
        embed.set_footer(text='订单 '+row['id'])
        return embed

    async def _post(self,row,extra=''):
        channel=await self.resolve_trade_channel(row)
        if channel is None: return
        if row['status']=='expired':
            extra+='\n订单超时，订单名额已释放，频道即将清理。'
        elif row['status'] in TERMINAL and row['status']!='manual_refunded':
            extra+='\n频道将在约 5 分钟后清理。如需保留，请点击下方按钮。'
        qr_file=None
        embed=self.order_embed(row,extra)
        if row['status']=='paying':
            invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if not invoice: raise ValueError('付款账单缺失，请管理员核对，勿自行转账')
            embed=trade_payment_ui.payment_embed(row,invoice)
            qr_file=trade_payment_ui.qr_file(invoice['address'])
        key='trade_panel:'+row['id']
        previous=await db.setting(key)
        try:
            if qr_file is not None:
                message=await self.send_step(channel,row['status'],embed,self.view(row),qr_file)
            else:
                message=await self.send_step(channel,row['status'],embed,self.view(row))
        finally:
            if qr_file is not None: qr_file.close()
        if message:
            await db.setting(key,{'message':message.id,'channel':channel.id,'status':row['status']})
            await self.deliver_step_notification(channel,row)
        if previous and previous['channel']==channel.id:
            try:
                old=await channel.fetch_message(previous['message'])
                await trade_card.retire(old)
            except discord.NotFound: pass
            except discord.HTTPException:
                log.warning('Could not retire prior order buttons: %s',row['id'])

    async def notify_step(self,channel,row):
        buyer,seller=row['buyer_id'],row['seller_id']
        both=[buyer,seller]
        prompts={
            'pending':(both,'交易频道已创建，请交易对方在 30 分钟内确认交易。'),
            'expired':(both,'订单已超时结束，请勿继续转账；若已付款请联系管理员。'),
            'confirmed':([buyer],'交易已确认，请点击「获取付款信息」查看本订单应到账金额。'),
            'invoicing':(both,'正在生成付款信息，请买家等待账单，卖家暂勿发货。'),
            'paying':(both,f'付款信息已展示。买家 <@{buyer}> 请按卡片精确付款；卖家 <@{seller}> 请等待系统确认买家到账，暂勿发货，也不要替买家付款。'),
            'paid':(both,f'系统已确认买家付款。卖家 <@{seller}> 请交付商品，完成后点击「标记为已发货」；买家请等待收货。'),
            'shipped':([buyer],'卖家已标记发货，请核对商品，实际收到后再点击「确认收货」。'),
            'receipt_confirmed':([seller],'买家已确认收货，请点击「领取货款」核对收款地址和费用。'),
            'releasing':([seller],'收款申请正在处理，请等待系统核对提现结果。'),
            'completed':(both,'交易已完成，系统已确认货款转出。'),
            'cancelled':(both,'交易已取消，请勿继续付款或发货。'),
            'disputed':(both,'交易已进入争议处理，请保留证据，等待管理员核实。'),
            'payment_review':(both,'付款需要核对，请勿重复支付或自行补差额，卖家暂勿发货。可点击「付款有问题／呼叫管理员」。'),
            'refund_ready':([buyer],'退款已获准，请点击「领取退款」核对金额和退款地址。'),
            'releasing_refund':([buyer],'退款申请正在处理，请等待系统核对结果。'),
            'refunded':(both,'系统已确认退款转出。'),
            'test_closed':(both,'管理员已记录本人测试资金留存结清，订单不再提供领取入口。'),
        }
        selected=prompts.get(row['status'])
        if not selected: return
        users,text=selected
        await channel.send(' '.join(f'<@{uid}>' for uid in users)+'，'+text,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid) for uid in users],roles=False,everyone=False))

    async def current_step(self,inter,row):
        """Recover UI without repeating a state change, invoice, or withdrawal."""
        body=f"此按钮对应的步骤已结束。订单当前状态：{row['status']}。\n请使用下方当前步骤按钮。"
        if row['status']=='paying':
            inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if inv:
                body+=f"\n原账单到账金额：{inv['amount']} USDT\n网络：BSC / BEP20\n地址：{inv['address']}\n截止时间（UTC）：{inv['expires_at']}\n如已付款请勿重复转账；过期请联系管理员。"
        if inter.message:
            try: await trade_card.retire(inter.message)
            except discord.HTTPException: pass
        await inter.followup.send(body,view=self.view(row),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

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
        amount=discord.ui.InputText(label='商品价格 USDT（至少5.01，最多两位小数）',max_length=20)
        terms=discord.ui.InputText(label='交付方式、期限及特别约定（选填）',style=discord.InputTextStyle.long,max_length=1500,required=False)
        for field in (item,amount,terms): modal.add_item(field)
        async def submitted(inter):
            await inter.response.defer(ephemeral=True)
            channel=None
            try:
                if inter.user.id!=user.id: raise ValueError('仅发起者可提交此表单')
                value=payments.money(amount.value)
                if value!=value.quantize(Decimal('.01')): raise ValueError('商品金额最多两位小数')
                if value<Decimal('5.01'): raise ValueError('商品金额不能低于 5.01 USDT。')
                await payments.payout_amount_quote(value)
                member=await guild.fetch_member(other.id)
                initiator=await guild.fetch_member(user.id)
                if member.bot: raise ValueError('不支持与 bot 交易')
                category=self.bot.get_channel(cfg.TRADE_CATEGORY_ID)
                if not isinstance(category,discord.CategoryChannel) or category.guild.id!=guild.id:
                    raise ValueError('交易分类配置不正确')
                buyer,seller=(user.id,other.id) if buy else (other.id,user.id)
                ident=uuid.uuid4().hex
                # Lock balance rows in sorted order to serialize concurrent admission.
                async with db.transaction() as cur:
                    for uid in sorted((buyer,seller)):
                        await cur.execute('INSERT INTO balances(user_id) VALUES(%s) ON DUPLICATE KEY UPDATE user_id=user_id',(uid,))
                        await cur.execute('SELECT user_id FROM balances WHERE user_id=%s FOR UPDATE',(uid,))
                        await cur.fetchone()
                        await cur.execute(f"SELECT COUNT(*) AS n FROM orders WHERE (buyer_id=%s OR seller_id=%s) AND status NOT IN {TERMINAL_SQL}",(uid,uid))
                        if (await cur.fetchone())['n']>=cfg.MAX_ACTIVE: raise ValueError(f'每位用户最多同时进行 {cfg.MAX_ACTIVE} 笔交易，请先完成或取消已有订单。')
                    await cur.execute('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,source_id,item,terms,amount,fee) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                      (ident,buyer,seller,user.id,source,item.value,terms.value or '',value,cfg.FEE))
                overwrites={guild.default_role:discord.PermissionOverwrite(view_channel=False),guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),initiator:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),member:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
                for rid in cfg.TRADE_ADMIN_ROLES:
                    role=guild.get_role(rid)
                    if role: overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
                try:
                    channel=await guild.create_text_channel('trade-'+ident[:8],category=category,overwrites=overwrites,topic='Escrow order '+ident,reason='New guild escrow order '+ident)
                    await db.query('UPDATE orders SET channel_id=%s WHERE id=%s',(channel.id,ident))
                    await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(%s,'trade')",(channel.id,))
                except Exception:
                    await self.transition(ident,'pending','cancelled',user.id,{'reason':'channel creation/setup failed'})
                    raise
                await db.audit(user.id,'create',{'terms':terms.value,'source':source},ident)
                await self.post(await self.order(ident),texts()['payment_notice'])
                # The durable notification worker retries participant mentions after failures.
                await inter.followup.send(f'交易已创建：{channel.mention}',ephemeral=True)
            except ValueError as exc:
                await inter.followup.send(str(exc),ephemeral=True)
            except Exception:
                log.exception('Create new order failed')
                await inter.followup.send('建单失败，请联系管理员核对。',ephemeral=True)
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
        await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(%s,'forum') ON DUPLICATE KEY UPDATE channel_id=channel_id",(thread.id,))
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
        if custom.startswith('new:refund_close:'):
            return await self.refund_close_response(inter,custom)
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
        valid={b.custom_id.split(':')[2] for b in self.view(row).children}
        if action not in valid:
            await inter.response.defer(ephemeral=True)
            return await self.current_step(inter,row)
        if action=='payment_info':
            await inter.response.defer(ephemeral=True)
            invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+ident,),shared=True,one=True)
            if not invoice or invoice['state']!='waiting' or time.time()>=trade_payment_ui.deadline(invoice):
                return await inter.followup.send('账单已过期或付款已识别，请勿继续转账；已付款请联系管理员核对。',ephemeral=True)
            fee=None
            try: fee=Decimal(str((await payments.withdrawal_network())['withdrawFee']))
            except Exception: log.warning('Payment instruction fee query unavailable: %s',ident)
            return await inter.followup.send(trade_payment_ui.payment_instructions(invoice,fee),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        if action=='collect':
            payee=row['buyer_id'] if row['status']=='refund_ready' else row['seller_id']
            if inter.user.id!=payee or row['status'] not in ('receipt_confirmed','refund_ready'):
                return await inter.response.send_message('当前不能领取货款。',ephemeral=True)
            return await self.address_modal(inter,row)
        await inter.response.defer(ephemeral=True)
        try:
            actor=inter.user.id
            if action=='payment_help':
                async with self.cleanup_lock:
                    current=await self.order(ident)
                    if not current or current['status'] not in ('paying','payment_timeout','payment_review'):
                        raise OrderStateChanged('付款步骤已改变')
                    last=await db.setting('payment_help:'+ident)
                    if last and time.time()-last['time']<300:
                        return await inter.followup.send('管理员已收到请求，频道已保留。请补充转账金额、交易哈希及付款截图，不要重复付款或补差额。',ephemeral=True)
                    async with db.transaction() as cur:
                        await cur.execute('SELECT status FROM orders WHERE id=%s FOR UPDATE',(ident,))
                        fresh=await cur.fetchone()
                        if not fresh or fresh['status'] not in ('paying','payment_timeout','payment_review'):
                            raise OrderStateChanged('付款步骤已改变，请使用最新交易消息')
                        await cur.execute("UPDATE orders SET status='payment_review' WHERE id=%s",(ident,))
                        await cur.execute('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s OR channel_id=%s',(row['channel_id'],row.get('source_id')))
                        await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                            (actor,'payment_help',db.encode({'from':fresh['status'],'reason':'用户请求核对付款，暂停自动履约并保留频道'}),ident))
                    await self.alert(f'付款问题请求：订单 {ident}\n申请人：<@{actor}>\n交易频道：<#{row["channel_id"]}>\n请核实实际到账金额、币种、网络和交易哈希。若需手动退款，请先核对是否已处理并保留凭证；不得仅凭截图退款。',notify_admins=True,fallback=inter.channel)
                    await db.setting('payment_help:'+ident,{'time':time.time(),'actor':actor})
                await self.post(await self.order(ident),'已呼叫管理员，频道已保留。请提供实际转账金额、交易哈希和付款截图，等待核实；请勿自行补差额或重复支付。')
                return await inter.followup.send('已呼叫管理员并暂停自动履约，频道不会因付款超时自动关闭。管理员将核对到账及是否需要手动退款。',ephemeral=True)
            elif action=='keep':
                if row['status'] not in (*TERMINAL,'payment_timeout','payment_review'): raise ValueError('当前步骤不支持保留频道')
                async with self.cleanup_lock:
                    current=await self.order(ident)
                    if await db.setting('channel_deleted:'+ident):
                        raise ValueError('频道已关闭，无法保留，请联系管理员')
                    if not current or current['status'] not in (*TERMINAL,'payment_timeout','payment_review'):
                        raise OrderStateChanged('订单状态已改变')
                    if current['status']=='payment_timeout':
                        await self.transition(ident,'payment_timeout','payment_review',actor,{'reason':'用户取消关闭'})
                    await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s',(row['channel_id'],))
                await db.audit(actor,'retain_channel',{},ident)
                await self.alert('用户请求保留交易频道：'+ident)
                await self.post(await self.order(ident),'已取消自动关闭，频道已保留，等待管理员核实。')
                return await inter.followup.send('已取消自动关闭并通知管理员，请提供付款凭证。',ephemeral=True)
            elif action=='confirm':
                if actor==row['initiator_id']: raise ValueError('请等待交易对方确认')
                await self.transition(ident,'pending','confirmed',actor)
            elif action=='pay':
                if actor!=row['buyer_id']: raise ValueError('只有买家可以付款')
                if row['amount']<Decimal('5.01'): raise ValueError('商品金额不能低于 5.01 USDT，请取消后重新发起。')
                await payments.payout_amount_quote(row['amount'])
                async with db.transaction() as cur:
                    await cur.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(ident,))
                    locked=await cur.fetchone()
                    if locked['status']!='confirmed': raise OrderStateChanged('此订单已生成账单或状态已变化')
                    await cur.execute('SELECT * FROM balances WHERE user_id=%s FOR UPDATE',(actor,))
                    balance=await cur.fetchone()
                    credit=min(balance['available'],locked['fee'])
                    await cur.execute('UPDATE balances SET available=available-%s,reserved=reserved+%s WHERE user_id=%s',(credit,credit,actor))
                    await cur.execute("UPDATE orders SET credits=%s,status='invoicing' WHERE id=%s",(credit,ident))
                # A durable intermediate state lets the worker recover failures.
                await self.prepare_invoice(await self.order(ident))
                # prepare_invoice already posts the payment banner and full instructions.
                return await inter.followup.send('付款信息已生成，请查看交易频道。',ephemeral=True)
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
                button=discord.ui.Button(label='确认已收到商品，允许卖家收款',emoji='✅',style=discord.ButtonStyle.danger)
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
        except OrderStateChanged:
            fresh=await self.order(ident)
            if fresh: await self.current_step(inter,fresh)
        except ValueError as exc:
            await inter.followup.send(str(exc),ephemeral=True)
        except Exception as exc:
            log.exception('New order operation failed')
            await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '操作未完成，请勿重复付款，联系管理员核对。',ephemeral=True)

    async def prepare_invoice(self,row):
        # Recover the original invoice even after its deadline; never allocate a
        # replacement amount/address or extend the original payment window.
        key='new:'+row['id']
        inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
        if not inv:
            address=await binance_api.get_deposit_address('USDT','BSC')
            if not address: raise ValueError('无法获取收款地址')
            async with db.transaction() as cur:
                await cur.execute('SELECT status FROM orders WHERE id=%s FOR UPDATE',(row['id'],))
                current=await cur.fetchone()
                if not current or current['status']!='invoicing': return
                await payments.invoice(key,address,row['amount']+row['fee']-row['credits'])
            inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
        if not inv or inv['state'] not in ('waiting','received','received_late','expired'):
            raise ValueError('原账单状态异常，需管理员核实')
        await db.query('UPDATE orders SET address=%s WHERE id=%s',(inv['address'],row['id']))
        try:
            await self.transition(row['id'],'invoicing','paying',self.bot.user.id)
        except OrderStateChanged:
            return
        if inv['expires_at']>datetime.utcnow() and inv['state']=='waiting':
            await self.post(await self.order(row['id']))
        # The financial worker reconciles received/expired invoices before posting.

    async def retire_payout_confirmation(self,inter):
        if getattr(inter,'message',None):
            try: await trade_card.retire(inter.message)
            except discord.HTTPException:
                log.warning('Could not retire payout confirmation buttons')

    async def payout_progress(self,inter,ident):
        """Read-only recovery for a stale confirmation; never submit again."""
        fresh=await self.order(ident)
        payout=await db.query('SELECT state FROM payouts WHERE order_key=%s',('new:'+ident,),shared=True,one=True)
        state=payout['state'] if payout else None
        status=fresh['status'] if fresh else None
        if state in ('unknown','failed','review'):
            text='这笔提现已有处理记录，结果需要管理员核对，请勿重复提交。'
        elif status in ('completed','refunded'):
            text='本订单已完成结算，请查看最新交易消息，不需要再次领取。'
        elif status=='test_closed':
            text='本订单已记为测试结清，资金留存在担保账户，领取入口已关闭。'
        elif state or status in ('releasing','releasing_refund'):
            text='本订单的收款申请已受理，正在核对提现结果。请等待最新交易消息，不要重复提交。'
        else:
            text='订单状态已改变，这个收款确认已失效，请使用最新交易消息。'
        await self.retire_payout_confirmation(inter)
        await inter.followup.send(text,ephemeral=True)

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
                button=discord.ui.Button(label='确认地址与预计费用，申请放款',emoji='💰',style=discord.ButtonStyle.danger)
                async def accepted(confirm):
                    if confirm.user.id!=payee: return
                    await confirm.response.defer(ephemeral=True)
                    try:
                        fresh=await self.order(row['id'])
                        if not fresh or fresh['status']!=row['status']:
                            return await self.payout_progress(confirm,row['id'])
                        fresh_fee,fresh_net=await payments.payout_quote(address.value.strip(),gross)
                        if (fee,net)!=(fresh_fee,fresh_net): raise ValueError('费用已变化，请重新领取查看报价')
                        await self.transition(row['id'],row['status'],'releasing_refund' if refund else 'releasing',confirm.user.id,{'address':address.value,'gross':gross,'fee':fee,'net':net})
                        await self.retire_payout_confirmation(confirm)
                        await db.query('UPDATE orders SET address=%s WHERE id=%s',(address.value.strip(),row['id']))
                        await payments.release('new:'+row['id'],address.value.strip(),gross,fee,net)
                        await confirm.followup.send('提现已提交或结果待核对。请勿重复操作，确认最终结果后会更新订单。',ephemeral=True)
                        try: await self.post(await self.order(row['id']))
                        except Exception: log.exception('Could not refresh payout progress card: %s',row['id'])
                    except OrderStateChanged:
                        await self.payout_progress(confirm,row['id'])
                    except ValueError as exc:
                        await confirm.followup.send(str(exc),ephemeral=True)
                    except Exception:
                        log.exception('Payout request failed')
                        await confirm.followup.send('放款结果待核对，请联系管理员，勿重复提现。',ephemeral=True)
                button.callback=accepted
                view.add_item(button)
                summary=(f'退款总额（含已付服务费）：{gross} U\n网络费从退款中扣除，由退款领取方承担。'
                         if refund else f'放款总额：{gross} U\n网络费从卖家货款中扣除。')
                await click.followup.send(f'地址：{address.value}\n{summary}\n预计网络费：{fee} U\n预计到账：{net} U\n金额精度舍入差额：{gross-fee-net} U\n内部转账是否免手续费以实际渠道结果为准。',view=view,ephemeral=True)
            except Exception as exc:
                await click.followup.send(str(exc) if isinstance(exc,ValueError) else '暂时无法获取提现报价。',ephemeral=True)
        modal.callback=submitted
        await inter.response.send_modal(modal)

    async def alert(self,text,notify_admins=False,fallback=None):
        channel=self.bot.get_channel(cfg.ALERT_CHANNEL_ID)
        if not channel or channel.guild.id!=cfg.GUILD_ID: channel=fallback
        if channel and channel.guild.id==cfg.GUILD_ID:
            roles=[discord.Object(id=rid) for rid in cfg.TRADE_ADMIN_ROLES] if notify_admins else []
            prefix=' '.join(f'<@&{role.id}>' for role in roles)
            await channel.send((prefix+'\n' if prefix else '')+text,
                allowed_mentions=discord.AllowedMentions(users=False,roles=roles,everyone=False))
        log.warning(text)

    async def timeout_channel(self,row):
        """Finish an ordinary unpaid timeout before deleting its channel."""
        async with self.cleanup_lock:
            fresh=await self.order(row['id'])
            if not fresh or fresh['status']!='payment_timeout': return
            channel=await self.resolve_trade_channel(row)
            if channel is None:
                await self.transition(row['id'],'payment_timeout','payment_review',self.bot.user.id,{'reason':'payment channel missing'})
                await self.alert('付款频道已不存在，原账单已保留，请核实后使用管理员指令结束订单：'+row['id'],notify_admins=True)
                return
            tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True)
            if not tracked or tracked['hold']: return
            deposit=await db.query('SELECT id FROM deposits WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if deposit: return
            key='timeout_close:'+row['id']
            timer=await db.setting(key)
            if not timer:
                await self.post(fresh,'付款已超时，请勿继续转账。频道将在约 5 分钟后关闭。\n如已付款或需要核对，请点击下方「已付款／取消关闭，请管理员核实」按钮。')
                await channel.send(f"<@{row['buyer_id']}> <@{row['seller_id']}>，付款已超时，频道将在约 5 分钟后关闭。如已付款，请点击上方按钮取消关闭并请求管理员核实。",
                                   allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row['buyer_id']),discord.Object(id=row['seller_id'])],roles=False,everyone=False))
                # Start only after both notices succeed; persist across restarts.
                await db.setting(key,{'deadline':time.time()+300})
                await db.audit(self.bot.user.id,'timeout_close_scheduled',{'channel':channel.id,'seconds':300},row['id'])
            elif time.time()>=timer['deadline']:
                await trade_timeout.close_unpaid(row['id'],self.bot.user.id,'付款超时且倒计时内未申请保留',automatic=True)
                await db.audit(self.bot.user.id,'timeout_channel_cleanup',{'channel':channel.id},row['id'])
                try: await channel.delete(reason='Payment expired; order closed and credit reservation released')
                except discord.NotFound: pass
                await db.setting('channel_deleted:'+row['id'],{'time':time.time()})
                await db.query('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP() WHERE channel_id=%s',(channel.id,))

    async def check_closed_payments(self):
        # Retain invoices permanently. The automatic history lookup window is bounded.
        import re
        shared=cfg.PAYMENTS_DATABASE
        if not re.fullmatch(r'[A-Za-z0-9_]+',shared): raise ValueError('Invalid payment database name')
        rows=await db.query(f"SELECT o.id,i.address,i.amount FROM orders o JOIN settings s ON s.setting_key=CONCAT('closed_unpaid:',o.id) JOIN `{shared}`.invoices i ON i.order_key=CONCAT('new:',o.id) WHERE o.status IN ('expired','cancelled') AND i.created_at>UTC_TIMESTAMP()-INTERVAL 89 DAY")
        for row in rows:
            try:
                if await payments.find_deposit('new:'+row['id'],row['address'],row['amount']):
                    async with self.cleanup_lock:
                        reopened=await trade_timeout.reopen_late(row['id'],self.bot.user.id)
                    if reopened:
                        await self.alert('已结束订单发现迟到账款，已重新转入付款待核对，禁止自动放款：'+row['id'],notify_admins=True)
                        await self.post(await self.order(row['id']),'发现迟到账款，请等待管理员核实；已删除的频道不会自动重建。')
            except Exception: log.exception('Closed order deposit lookup failed: %s',row['id'])

    async def repair_trade_access(self,guild):
        rows=await db.query("SELECT o.* FROM orders o JOIN tracked_channels c ON c.channel_id=o.channel_id WHERE c.kind='trade' AND o.status NOT IN ('completed','cancelled','refunded','test_closed','manual_refunded','expired')")
        complete=True
        for row in rows:
            try:
                channel=self.bot.get_channel(row['channel_id'])
                if channel is None:
                    try: channel=await self.bot.fetch_channel(row['channel_id'])
                    except discord.NotFound: continue
                if not isinstance(channel,discord.TextChannel) or channel.guild.id!=guild.id: continue
                for uid in (row['buyer_id'],row['seller_id']):
                    try: member=guild.get_member(uid) or await guild.fetch_member(uid)
                    except discord.NotFound: continue
                    overwrite=channel.overwrites_for(member)
                    if all(getattr(overwrite,key) is True for key in ('view_channel','send_messages','read_message_history')): continue
                    overwrite.update(view_channel=True,send_messages=True,read_message_history=True)
                    await channel.set_permissions(member,overwrite=overwrite,reason='Restore escrow participant history access')
                    await db.audit(self.bot.user.id,'repair_trade_access',{'channel':channel.id,'member':uid},row['id'])
            except Exception:
                complete=False
                log.exception('Could not repair trade channel access: %s',row['id'])
        self.access_ready=complete

    async def mark_paid(self,row,txid):
        async with db.transaction() as cur:
            await cur.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(row['id'],))
            fresh=await cur.fetchone()
            if not fresh or fresh['status']!='paying': return False
            if fresh['credits']:
                await cur.execute('UPDATE balances SET reserved=reserved-%s WHERE user_id=%s AND reserved>=%s',(fresh['credits'],fresh['buyer_id'],fresh['credits']))
                if cur.rowcount!=1: raise ValueError('积分预留不一致，到账状态未提交，需管理员核对')
            await cur.execute("UPDATE orders SET status='paid' WHERE id=%s",(row['id'],))
            await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',(self.bot.user.id,'paid',db.encode({'txid':txid,'credits':fresh['credits']}),row['id']))
        return True

    async def recovery_alert(self,row):
        try:
            key='recovery_alert:'+row['id']
            previous=await db.setting(key)
            if not previous or time.time()-previous['time']>=3600:
                await self.alert('订单恢复异常，请管理员核实，禁止重复付款或提现：'+row['id'],notify_admins=True)
                await db.setting(key,{'time':time.time()})
        except Exception: log.exception('Recovery alert failed: %s',row['id'])

    async def resolve_trade_channel(self,row):
        if not row.get('channel_id'):
            guild=self.bot.get_guild(cfg.GUILD_ID)
            matches=[ch for ch in guild.text_channels if ch.topic=='Escrow order '+row['id']] if guild else []
            if len(matches)>1: raise ValueError('多个频道关联同一订单，需要管理员核实')
            if not matches: return None
            await db.query('UPDATE orders SET channel_id=%s WHERE id=%s AND channel_id IS NULL',(matches[0].id,row['id']))
            fresh=await self.order(row['id'])
            row['channel_id']=fresh['channel_id']
        channel=self.bot.get_channel(row['channel_id'])
        if channel is None:
            try: channel=await self.bot.fetch_channel(row['channel_id'])
            except discord.NotFound: return None
            # Forbidden/network errors are not evidence of a deleted channel.
        if channel.guild.id!=cfg.GUILD_ID: raise ValueError('交易频道不属于新社群')
        return channel

    async def deliver_step_notification(self,channel,row):
        key='trade_notification:'+row['id']
        previous=await db.setting(key)
        if previous and previous.get('status')==row['status']: return
        await self.notify_step(channel,row)
        await db.setting(key,{'status':row['status']})

    async def recover_panels(self):
        # The committed order is the durable notification task. Delivery receipts
        # are separate, so a failed Discord send cannot roll back financial state.
        rows=await db.query("SELECT o.* FROM orders o WHERE NOT EXISTS (SELECT 1 FROM settings s WHERE s.setting_key=CONCAT('channel_deleted:',o.id))")
        for row in rows:
            try:
                if row['status'] in ('payment_timeout','manual_refunded'): continue
                if await db.setting('channel_deleted:'+row['id']): continue
                channel=await self.resolve_trade_channel(row)
                if channel is None:
                    if row['status'] not in TERMINAL: await self.recovery_alert(row)
                    else: await db.setting('channel_deleted:'+row['id'],{'time':time.time()})
                    continue
                await db.query("INSERT INTO tracked_channels(channel_id,kind) VALUES(%s,'trade') ON DUPLICATE KEY UPDATE channel_id=channel_id",(channel.id,))
                if row['status']=='paying':
                    inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
                    if not inv or inv['state']!='waiting' or inv['expires_at']<=datetime.utcnow(): continue
                async with self.post_lock:
                    fresh=await self.order(row['id'])
                    if not fresh or fresh['status']!=row['status']: continue
                    panel=await db.setting('trade_panel:'+row['id'])
                    if not panel or panel.get('status')!=row['status']:
                        await self._post(row)
                    else:
                        await self.deliver_step_notification(channel,row)
            except Exception: log.exception('Trade notification recovery failed: %s',row['id'])

    async def idle_worker(self):
        rows=await db.query("SELECT o.*, (SELECT MAX(a.created_at) FROM audit a WHERE a.order_id=o.id AND a.action='confirmed') AS confirmed_at FROM orders o WHERE o.status IN ('pending','confirmed')")
        for row in rows:
            try:
                started=row.get('confirmed_at') if row['status']=='confirmed' else row['created_at']
                # Old confirmed orders missing an audit timestamp receive a fresh
                # persisted grace period rather than expiring from creation time.
                key='idle_timer:'+row['id']+':'+row['status']
                timer=await db.setting(key)
                if not timer:
                    timer={'deadline':(started or datetime.utcnow()).replace(tzinfo=timezone.utc).timestamp()+IDLE_SECONDS[row['status']]}
                    await db.setting(key,timer)
                channel=await self.resolve_trade_channel(row)
                if channel and time.time()>=timer['deadline']-300 and not timer.get('warned'):
                    # If the bot was offline at warning time, allow a full 5 minutes.
                    timer['deadline']=max(timer['deadline'],time.time()+300)
                    await channel.send(f"<@{row['buyer_id']}> <@{row['seller_id']}>，订单尚未{'确认' if row['status']=='pending' else '获取付款信息'}，将在 <t:{int(timer['deadline'])}:R> 自动结束并关闭频道。请及时操作。",
                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row['buyer_id']),discord.Object(id=row['seller_id'])],roles=False,everyone=False))
                    timer['warned']=True
                    await db.setting(key,timer)
                if time.time()<timer['deadline']: continue
                async with self.cleanup_lock:
                    ended=await trade_idle.expire(row['id'],self.bot.user.id,row['status'],datetime.utcfromtimestamp(timer['deadline']))
                if ended and channel: await self.post(await self.order(row['id']))
            except Exception:
                log.exception('Idle order recovery failed: %s',row['id'])
                await self.recovery_alert(row)

    async def cleanup_finished(self):
        rows=await db.query("SELECT o.* FROM orders o JOIN tracked_channels c ON c.channel_id=o.channel_id WHERE c.hold=FALSE AND (o.status='expired' OR (o.status IN ('completed','cancelled','refunded','test_closed') AND o.closed_at<UTC_TIMESTAMP()-INTERVAL 5 MINUTE))")
        for row in rows:
            try:
                async with self.cleanup_lock:
                    fresh=await self.order(row['id'])
                    if not fresh or fresh['status'] not in AUTO_CLEANUP: continue
                    if fresh['status']!='expired' and (not fresh.get('closed_at') or fresh['closed_at']>datetime.utcnow()-timedelta(minutes=5)): continue
                    if await db.setting('channel_deleted:'+row['id']): continue
                    tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(fresh['channel_id'],),one=True)
                    if not tracked or tracked['hold']: continue
                    channel=await self.resolve_trade_channel(fresh)
                    if channel:
                        panel=await db.setting('trade_panel:'+row['id'])
                        notice=await db.setting('trade_notification:'+row['id'])
                        if not panel or panel.get('status')!=fresh['status'] or not notice or notice.get('status')!=fresh['status']: continue
                        await db.audit(self.bot.user.id,'channel_cleanup',{'channel':channel.id},row['id'])
                        try: await channel.delete(reason='Closed escrow order cleanup')
                        except discord.NotFound: pass
                    await db.setting('channel_deleted:'+row['id'],{'time':time.time()})
            except Exception:
                log.exception('Channel cleanup failed: %s',row['id'])
                await self.recovery_alert(row)

    @tasks.loop(seconds=60)
    async def worker(self):
        try:
            guild=self.bot.get_guild(cfg.GUILD_ID)
            if not guild: return
            if not cfg.PAYMENTS_ENABLED:
                await self.retry_forums(guild)
                await self.manual_close_worker()
                return
            await self.check_closed_payments()
            rows=await db.query("SELECT * FROM orders WHERE status IN ('invoicing','paying','payment_timeout','payment_review','releasing','releasing_refund')")
            for row in rows:
                try:
                    key='new:'+row['id']
                    if row['status']=='invoicing':
                        await self.prepare_invoice(row)
                    elif row['status'] in ('paying','payment_timeout','payment_review'):
                        inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',(key,),shared=True,one=True)
                        if not inv:
                            if row['status']!='payment_review':
                                await self.transition(row['id'],row['status'],'payment_review',self.bot.user.id,{'reason':'missing invoice'})
                            await self.recovery_alert(row)
                            continue
                        if inv['state']=='manual_refunded': continue
                        txid=await payments.find_deposit(key,inv['address'],inv['amount'])
                        if txid:
                            deposit=await db.query('SELECT payload FROM deposits WHERE order_key=%s',(key,),shared=True,one=True)
                            import json
                            from datetime import timezone
                            inserted=json.loads(deposit['payload']).get('insertTime',0)
                            deadline=int(inv['expires_at'].replace(tzinfo=timezone.utc).timestamp()*1000)
                            if row['status']=='payment_timeout' or (inserted>deadline and row['status']=='paying'):
                                await self.transition(row['id'],row['status'],'payment_review',self.bot.user.id,{'reason':'late deposit'})
                                await self.alert('收到迟到账款，请人工核对：'+row['id'],notify_admins=True)
                                await self.post(await self.order(row['id']),'已找到到账记录，频道已保留，请等待管理员核实。')
                                continue
                            if row['status']=='payment_review':
                                tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True)
                                if tracked and not tracked['hold']:
                                    changed=await db.query("UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s AND EXISTS (SELECT 1 FROM orders WHERE id=%s AND status='payment_review')",(row['channel_id'],row['id']))
                                    if not changed: continue
                                    await db.audit(self.bot.user.id,'late_payment_hold',{},row['id'])
                                    await self.alert('超时订单已找到到账记录，已取消频道自动关闭，请核实：'+row['id'])
                                    await self.post(row,'已找到到账记录，自动关闭已取消，请等待管理员核实。')
                                # Stay review-only: no automatic allocation/refund after expiry.
                                continue
                            await self.mark_paid(row,txid)
                            await self.post(await self.order(row['id']))
                        else:
                            from datetime import datetime
                            if inv['expires_at']<=datetime.utcnow() and row['status']=='paying':
                                await self.transition(row['id'],'paying','payment_timeout',self.bot.user.id)
                                await self.alert('付款已超时，将通知双方倒计时关闭频道；账单保留供核对：'+row['id'])
                                await self.timeout_channel(await self.order(row['id']))
                            elif row['status']=='payment_timeout':
                                await self.timeout_channel(row)
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
                except Exception:
                    log.exception('Order recovery failed: %s',row['id'])
                    if row['status']=='invoicing':
                        try:
                            key='invoice_failure:'+row['id']
                            failure=await db.setting(key)
                            if not failure: await db.setting(key,{'time':time.time()})
                            elif time.time()-failure['time']>=900:
                                await self.transition(row['id'],'invoicing','payment_review',self.bot.user.id,{'reason':'invoice recovery repeatedly failed'})
                        except Exception: log.exception('Invoice failure escalation failed')
                    await self.recovery_alert(row)
            # Discord cleanup/notifications must not prevent financial reconciliation.
            for operation in (self.idle_worker,self.recover_panels,self.cleanup_finished,self.manual_close_worker):
                try: await operation()
                except Exception: log.exception('Order maintenance failed: %s',operation.__name__)
            await self.retry_forums(guild)
            if not self.access_ready: await self.repair_trade_access(guild)
        except Exception: log.exception('New trading worker failed')

    @worker.before_loop
    async def before_worker(self): await self.bot.wait_until_ready()

    @discord.slash_command(name='new_trade_review',description='核对异常订单，生成一次性指令确认码')
    async def review(self,ctx,order_id:str,decision:discord.Option(str,choices=['记录意见','继续履约','允许卖家收款','退还买家','无到账关闭']),reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            row=await self.order(order_id)
            if not row: raise ValueError('订单不存在')
            if not reason.strip() or len(reason)>500: raise ValueError('原因必须为 1–500 字')
            if decision=='记录意见':
                async with db.transaction() as cur:
                    await cur.execute('SELECT status FROM orders WHERE id=%s FOR UPDATE',(order_id,))
                    current=await cur.fetchone()
                    await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                        (ctx.author.id,'review_decision',db.encode({'decision':decision,'reason':reason,'status':current['status']}),order_id))
                log.warning('Administrator opinion: actor=%s order=%s reason=%s',ctx.author.id,order_id,reason)
                return await ctx.followup.send('意见已保存，未改变订单或频道关闭状态。',ephemeral=True)
            inv=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+order_id,),shared=True,one=True)
            if inv: await payments.find_deposit('new:'+order_id,inv['address'],inv['amount'])
            data=await trade_review.prepare(order_id,ctx.author.id,decision,reason)
            await ctx.followup.send(f"订单 {order_id}\n决定：{decision}\n原因：{reason}\n金额错误仅能核实后手动退款；无到账关闭表示你已核实确实未收到任何付款，不能只凭 bot 未匹配。\n确认码 5 分钟有效：\n`/new_trade_confirm order_id:{order_id} code:{data['code']}`",ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except ValueError as exc: await ctx.followup.send(str(exc),ephemeral=True)
        except Exception:
            log.exception('Review preview failed')
            await ctx.followup.send('无法生成预览，请稍后核对。',ephemeral=True)

    @discord.slash_command(name='new_trade_manual_refund',description='登记本账户已完成的外部 BSC 手动退款（不会转账）')
    async def manual_refund(self,ctx,order_id:str,deposit_txid:str,withdrawal_id:str,refund_address:str,reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            verified=await trade_review.verify_manual(order_id,deposit_txid.strip(),withdrawal_id.strip(),refund_address.strip())
            data=await trade_review.prepare(order_id,ctx.author.id,'登记手动退款',reason,
                {'deposit_txid':deposit_txid.strip(),'withdrawal_id':withdrawal_id.strip(),'address':refund_address.strip(),'evidence':verified})
            await ctx.followup.send(f"仅登记已发生的手动退款，不会转账。\n订单 {order_id}\n原入款 {verified['received']} USDT\n网络费 {verified['fee']} USDT\n退款到账 {verified['net']} USDT\n地址 `{verified['address']}`\n原因：{reason}\n确认表示你已核实该入款属于本订单买家，且退款地址已与买家核对。流水存在本身不证明归属。\n确认码 5 分钟有效：\n`/new_trade_confirm order_id:{order_id} code:{data['code']}`",ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except ValueError as exc: await ctx.followup.send(str(exc),ephemeral=True)
        except Exception:
            log.exception('Manual refund preview failed')
            await ctx.followup.send('流水核对失败，频道继续保留；未登记退款成功。',ephemeral=True)

    @discord.slash_command(name='new_trade_confirm',description='通过一次性确认码执行已预览的处理决定')
    async def confirm_review(self,ctx,order_id:str,code:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            target=await trade_review.confirm(order_id,ctx.author.id,code.strip())
        except ValueError as exc: return await ctx.followup.send(str(exc),ephemeral=True)
        except Exception:
            log.exception('Command settlement failed: %s',order_id)
            return await ctx.followup.send('处理结果需核对，请查看订单状态，勿重复退款。',ephemeral=True)
        log.warning('Administrator settlement: actor=%s order=%s target=%s',ctx.author.id,order_id,target)
        try:
            if target=='manual_refunded': await self.manual_close_tick(order_id)
            else: await self.post(await self.order(order_id))
            await self.alert(f'管理员 {ctx.author.id} 已处理订单 {order_id}：{target}')
        except Exception: log.exception('Settlement saved; notification failed: %s',order_id)
        await ctx.followup.send('处理已记录。手动退款只登记账本，不会再次转账；频道关闭通知会自动重试。' if target=='manual_refunded' else '处理决定已记录，请查看订单当前步骤。',ephemeral=True)

    @discord.slash_command(name='new_trade_channel',description='手动退款后暂停关闭或重新发起用户关闭确认')
    async def manage_refund_channel(self,ctx,order_id:str,action:discord.Option(str,choices=['暂停关闭','重新通知关闭']),reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            if not reason.strip() or len(reason)>500: raise ValueError('原因必须为 1–500 字')
            async with self.cleanup_lock:
                row=await self.order(order_id)
                state=await db.setting('manual_close:'+order_id)
                if not row or row['status']!='manual_refunded' or not state or state['phase']=='deleted':
                    raise ValueError('只支持尚未删除频道的已登记手动退款订单')
                if action=='暂停关闭':
                    state['phase']='held'
                else:
                    if state['phase']!='held': raise ValueError('已经通知或正在通知，不能重复重置倒计时')
                    state.update(phase='unnotified',generation=uuid.uuid4().hex[:8])
                    state.pop('deadline',None)
                await self.save_manual_close(row,state,True,ctx.author.id,'manual_channel_control',{'action':action,'reason':reason})
            if action=='重新通知关闭': await self.manual_close_tick(order_id)
            log.warning('Manual channel control: actor=%s order=%s action=%s reason=%s',ctx.author.id,order_id,action,reason)
            await ctx.followup.send('已保留频道。' if action=='暂停关闭' else '已重新安排关闭通知；成功发送后计时 30 分钟。',ephemeral=True)
        except ValueError as exc: await ctx.followup.send(str(exc),ephemeral=True)
        except Exception:
            log.exception('Manual channel control failed')
            await ctx.followup.send('操作结果需核对，频道状态以持久记录为准。',ephemeral=True)

    async def save_manual_close(self,row,state,hold,actor,action,details):
        # Commit the timer, retention flag and audit together, or none of them.
        async with db.transaction() as cur:
            await cur.execute('UPDATE settings SET value=%s WHERE setting_key=%s',(db.encode(state),'manual_close:'+row['id']))
            await cur.execute('UPDATE tracked_channels SET hold=%s WHERE channel_id=%s',(hold,row['channel_id']))
            await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',(actor,action,db.encode(details),row['id']))

    async def manual_close_worker(self):
        rows=await db.query("SELECT o.id FROM orders o JOIN settings s ON s.setting_key=CONCAT('manual_close:',o.id) WHERE o.status='manual_refunded' AND JSON_UNQUOTE(JSON_EXTRACT(s.value,'$.phase')) IN ('unnotified','waiting','accepted')")
        for row in rows:
            try: await self.manual_close_tick(row['id'])
            except Exception: log.exception('Manual refund channel recovery failed: %s',row['id'])

    async def manual_close_tick(self,ident):
        async with self.cleanup_lock:
            row=await self.order(ident)
            state=await db.setting('manual_close:'+ident)
            if not row or row['status']!='manual_refunded' or not state or state['phase'] in ('held','deleted'): return
            channel=self.bot.get_channel(row['channel_id'])
            if not channel or channel.guild.id!=cfg.GUILD_ID: return
            if state['phase']=='unnotified':
                details=state['details']; deadline=int(time.time()+1800)
                view=discord.ui.View(timeout=None)
                for label,action,emoji in [('确认关闭','accept','✅'),('尚有问题／暂不关闭','hold','🆘')]:
                    view.add_item(discord.ui.Button(label=label,emoji=emoji,custom_id=f"new:refund_close:{action}:{ident}:{state['generation']}"))
                message=await channel.send(f"<@{row['buyer_id']}> <@{row['seller_id']}>，管理员已核实手动退款完成。\n退款到账：**{details['net']} USDT**　网络费：**{details['fee']} USDT**\n退款地址：`{details['address']}`\n退款凭证：`{details['withdrawal']['txId']}`\n买家可确认关闭，或选择「尚有问题／暂不关闭」。30 分钟无回应则自动关闭（<t:{deadline}:f>，<t:{deadline}:R>）。未收到退款请申请保留。",
                    view=view,allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row['buyer_id']),discord.Object(id=row['seller_id'])],roles=False,everyone=False))
                state.update(phase='waiting',deadline=time.time()+1800,message=message.id)
                await self.save_manual_close(row,state,False,self.bot.user.id,'manual_close_notified',{'deadline':state['deadline'],'message':message.id})
                return
            if state['phase'] not in ('waiting','accepted'): return
            if state['phase']=='waiting' and time.time()<state['deadline']: return
            tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True)
            if not tracked or tracked['hold']: return
            await db.audit(self.bot.user.id,'manual_channel_cleanup',{'phase':state['phase'],'channel':channel.id},ident)
            try: await channel.delete(reason='Verified manual refund; user confirmed or 30 minute silence')
            except discord.NotFound: pass
            state['phase']='deleted'
            await db.setting('manual_close:'+ident,state)
            await db.query('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP(),hold=FALSE WHERE channel_id=%s',(row['channel_id'],))

    async def refund_close_response(self,inter,custom):
        await inter.response.defer(ephemeral=True)
        parts=custom.split(':')
        if len(parts)!=5: return
        _,_,action,ident,generation=parts
        if action not in ('accept','hold'): return
        async with self.cleanup_lock:
            row=await self.order(ident); state=await db.setting('manual_close:'+ident)
            if not row or row['status']!='manual_refunded' or inter.channel_id!=row['channel_id'] or inter.user.id!=row['buyer_id']:
                return await inter.followup.send('仅本订单退款接收人（买家）可确认。',ephemeral=True)
            if not state or state['generation']!=generation or state['phase']!='waiting':
                return await inter.followup.send('此关闭通知已处理或已失效，请查看最新通知。',ephemeral=True)
            state['phase']='accepted' if action=='accept' else 'held'
            await self.save_manual_close(row,state,action=='hold',inter.user.id,'manual_close_response',{'action':action,'generation':generation})
        await inter.followup.send('已确认，即将关闭频道。' if action=='accept' else '已取消倒计时并保留频道，管理员将继续核实。',ephemeral=True)
        if action=='accept': await self.manual_close_tick(ident)
        else: await self.alert('手动退款后用户仍有问题，已保留频道：'+ident,notify_admins=True,fallback=inter.channel)

    @discord.slash_command(name='new_trade_close_test',description='本人测试资金留存担保账户并结清订单（不转账）')
    async def close_test(self,ctx,order_id:str,reason:str):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.TRADE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        await ctx.defer(ephemeral=True)
        row=await self.order(order_id)
        deposit=await db.query('SELECT * FROM deposits WHERE order_key=%s',('new:'+order_id,),shared=True,one=True)
        if not reason.strip() or len(reason)>500:
            return await ctx.followup.send('请填写 1–500 字的测试结清原因。',ephemeral=True)
        if not row or row['status']!='receipt_confirmed' or not deposit:
            return await ctx.followup.send('仅支持有到账记录、已确认收货且未提交提现的测试订单。',ephemeral=True)
        view=discord.ui.View(timeout=180)
        button=discord.ui.Button(label='确认全部为本人测试资金，留存并结清',emoji='🧾',style=discord.ButtonStyle.danger)
        async def accepted(inter):
            if inter.user.id!=ctx.author.id or not admin(inter.user,cfg.TRADE_ADMIN_ROLES):
                return await inter.response.send_message('无权确认。',ephemeral=True)
            await inter.response.defer(ephemeral=True)
            from utils.test_order_settlement import settle
            try:
                details=await settle(order_id,inter.user.id,row['amount'],deposit['amount'],reason.strip())
            except ValueError as exc:
                return await inter.followup.send(str(exc),ephemeral=True)
            except Exception:
                log.exception('Test settlement failed: %s',order_id)
                return await inter.followup.send('结清结果需管理员核对，请勿重复操作。',ephemeral=True)
            log.warning('Test funds retained: order=%s actor=%s amount=%s',order_id,inter.user.id,details['retained_in_account'])
            try:
                await self.post(await self.order(order_id))
                await self.alert(f"测试订单已结清：{order_id}；管理员 {inter.user.id} 确认本人资金 {details['retained_in_account']} USDT 留存在担保账户，未执行转账。")
            except Exception: log.exception('Test settlement notification failed: %s',order_id)
            await inter.followup.send('测试结清已记录，未发起转账，领取入口已失效。频道约 5 分钟后清理。',ephemeral=True)
        button.callback=accepted
        view.add_item(button)
        await ctx.followup.send(f"订单：{order_id}\n商品：{row['item']}\n实际到账：{deposit['amount']} USDT\n原因：{reason}\n\n确认表示：买家和卖家资金全部属于你本人，全部到账款留在担保账户，订单记为测试结清。不会发起提现或退款，不计入正常成交额。",view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID!=config.GUILD_ID: bot.add_cog(NewTrading(bot))
