"""Independent new-guild orders; shared account ledger owns payment side effects."""
import asyncio
import logging
import uuid
import time
from datetime import timezone
from pathlib import Path
from decimal import Decimal
import discord
from discord.commands import user_command
from discord.ext import commands, tasks
import config
from utils import new_store as db, shared_payments as payments, binance_api, trade_card, trade_payment_ui
from modules.new_community import buttons, admin, texts

cfg=config.NEW
log=logging.getLogger(__name__)
TERMINAL=('completed','cancelled','refunded','test_closed')
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
            if new=='disputed':
                await cur.execute('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=(SELECT channel_id FROM orders WHERE id=%s) OR channel_id=(SELECT source_id FROM orders WHERE id=%s)',(ident,ident))

    def view(self,row):
        ident=row['id']
        options={
            'pending':[('确认交易','confirm'),('取消交易','cancel')],
            'confirmed':[('获取付款信息','pay'),('取消交易','cancel')],
            'paying':[('复制付款信息','payment_info')],
            'paid':[('标记为已发货','ship'),('发起争议','dispute')],
            'shipped':[('确认收货','receipt'),('发起争议','dispute')],
            'receipt_confirmed':[('领取货款','collect')],
            'refund_ready':[('领取退款','collect')],
            'payment_review':[('已付款／取消关闭，请管理员核实','keep')],
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
            'pending':('交易请求',f'{counterpart}，请查看物品及附加详情，确认无误后点击「确认交易」。'),
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
            'payment_review':('付款待核对','请勿继续付款或发货，请联系管理员核对账款。'),
            'refund_ready':('等待领取退款',f'{buyer}，退款已获准，请点击「领取退款」核对退款金额并提供收款地址。'),
            'releasing_refund':('正在处理退款','退款申请已提交处理，请等待系统核对结果，勿重复申请。'),
            'refunded':('退款已完成','系统已确认退款转出成功。'),
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
        channel=self.bot.get_channel(row['channel_id'])
        if not channel or channel.guild.id!=cfg.GUILD_ID:
            return
        if row['status'] in TERMINAL:
            extra+='\n频道将在约 5 分钟后清理。如需保留，请点击下方按钮。'
        qr_file=None
        embed=self.order_embed(row,extra)
        if row['status']=='paying':
            invoice=await db.query('SELECT * FROM invoices WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if not invoice: raise ValueError('付款账单缺失，请管理员核对，勿自行转账')
            fee=None
            try:
                network=await payments.withdrawal_network()
                fee=Decimal(str(network['withdrawFee']))
            except Exception:
                log.warning('Cannot quote incoming transfer fee hint for order %s',row['id'])
            embed=trade_payment_ui.payment_embed(row,invoice,fee)
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
            await db.setting(key,{'message':message.id,'channel':channel.id})
        if previous and previous['channel']==channel.id:
            try:
                old=await channel.fetch_message(previous['message'])
                await trade_card.retire(old)
            except discord.NotFound: pass
            except discord.HTTPException:
                log.warning('Could not retire prior order buttons: %s',row['id'])

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
        amount=discord.ui.InputText(label='商品价格 USDT（至少3，最多两位小数）',max_length=20)
        terms=discord.ui.InputText(label='交付方式、期限及特别约定',style=discord.InputTextStyle.long,max_length=1500)
        for field in (item,amount,terms): modal.add_item(field)
        async def submitted(inter):
            await inter.response.defer(ephemeral=True)
            channel=None
            try:
                if inter.user.id!=user.id: raise ValueError('仅发起者可提交此表单')
                value=payments.money(amount.value)
                if value!=value.quantize(Decimal('.01')): raise ValueError('商品金额最多两位小数')
                if value<Decimal('3'): raise ValueError('商品金额不能低于 3 USDT，托管费不计入商品金额。')
                await payments.payout_amount_quote(value)
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
                        await cur.execute('INSERT INTO balances(user_id) VALUES(%s) ON DUPLICATE KEY UPDATE user_id=user_id',(uid,))
                        await cur.execute('SELECT user_id FROM balances WHERE user_id=%s FOR UPDATE',(uid,))
                        await cur.fetchone()
                        await cur.execute("SELECT COUNT(*) AS n FROM orders WHERE (buyer_id=%s OR seller_id=%s) AND status NOT IN ('completed','cancelled','refunded','test_closed')",(uid,uid))
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
            return await inter.followup.send(trade_payment_ui.copy_text(invoice),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        if action=='collect':
            payee=row['buyer_id'] if row['status']=='refund_ready' else row['seller_id']
            if inter.user.id!=payee or row['status'] not in ('receipt_confirmed','refund_ready'):
                return await inter.response.send_message('当前不能领取货款。',ephemeral=True)
            return await self.address_modal(inter,row)
        await inter.response.defer(ephemeral=True)
        try:
            actor=inter.user.id
            if action=='keep':
                if row['status'] not in (*TERMINAL,'payment_review'): raise ValueError('当前步骤不支持保留频道')
                async with self.cleanup_lock:
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
        address=await binance_api.get_deposit_address('USDT','BSC')
        if not address: raise ValueError('无法获取收款地址')
        key='new:'+row['id']
        amount=await payments.invoice(key,address,row['amount']+row['fee']-row['credits'])
        await db.query('UPDATE orders SET address=%s WHERE id=%s',(address,row['id']))
        await self.transition(row['id'],'invoicing','paying',self.bot.user.id)
        await self.post(await self.order(row['id']))

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

    async def timeout_channel(self,row):
        """Close only the Discord channel; retain review state and payment evidence."""
        async with self.cleanup_lock:
            fresh=await self.order(row['id'])
            if not fresh or fresh['status']!='payment_review': return
            tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True)
            if not tracked or tracked['hold']: return
            deposit=await db.query('SELECT id FROM deposits WHERE order_key=%s',('new:'+row['id'],),shared=True,one=True)
            if deposit: return
            channel=self.bot.get_channel(row['channel_id'])
            if not channel or channel.guild.id!=cfg.GUILD_ID: return
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
                await db.audit(self.bot.user.id,'timeout_channel_cleanup',{'channel':channel.id},row['id'])
                await channel.delete(reason='Payment timeout; no request to retain channel')
                await db.query('UPDATE tracked_channels SET closed_at=UTC_TIMESTAMP() WHERE channel_id=%s',(channel.id,))

    @tasks.loop(seconds=60)
    async def worker(self):
        try:
            guild=self.bot.get_guild(cfg.GUILD_ID)
            if not guild: return
            await self.retry_forums(guild)
            if not cfg.PAYMENTS_ENABLED: return
            finished=await db.query("SELECT o.* FROM orders o JOIN tracked_channels c ON c.channel_id=o.channel_id WHERE o.status IN ('completed','cancelled','refunded','test_closed') AND c.hold=FALSE AND o.closed_at<UTC_TIMESTAMP()-INTERVAL 5 MINUTE")
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
                                tracked=await db.query('SELECT hold FROM tracked_channels WHERE channel_id=%s',(row['channel_id'],),one=True)
                                if tracked and not tracked['hold']:
                                    await db.query('UPDATE tracked_channels SET hold=TRUE WHERE channel_id=%s',(row['channel_id'],))
                                    await db.audit(self.bot.user.id,'late_payment_hold',{},row['id'])
                                    await self.alert('超时订单已找到到账记录，已取消频道自动关闭，请核实：'+row['id'])
                                    await self.post(row,'已找到到账记录，自动关闭已取消，请等待管理员核实。')
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
                                await self.alert('付款已超时，将通知双方倒计时关闭频道；账单保留供核对：'+row['id'])
                                await self.timeout_channel(await self.order(row['id']))
                            elif row['status']=='payment_review':
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
        button=discord.ui.Button(label='确认处理此订单',emoji='⚖️',style=discord.ButtonStyle.danger)
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
