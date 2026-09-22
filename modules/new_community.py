"""New-guild verification, language roles, invite counts and editable notices."""
import asyncio
import logging
import json
from pathlib import Path
import discord
from discord.ext import commands, tasks
import config
from utils import new_store as db, notice_card

cfg = config.NEW
log = logging.getLogger(__name__)

def texts():
    return json.loads((Path(__file__).resolve().parent.parent/'config'/'new_content.json').read_text(encoding='utf-8'))

def buttons(items):
    view = discord.ui.View(timeout=None)
    for label, custom in items:
        kind=custom.split(':',1)[0]
        if kind=='trade':
            action=custom.split(':',2)[1]
            emoji={'confirm':'✅','cancel':'❌','pay':'💳','ship':'📦',
                   'receipt':'✅','collect':'💰','dispute':'⚠️','keep':'📌','payment_info':'💡','payment_help':'🆘'}.get(action)
        else:
            emoji={'verify':'✅','lang':'🌐','invites':'👥','invite_link':'📨','forum':'🛒'}.get(kind)
            if custom=='lang:clear': emoji='🔄'
            if kind=='forum': emoji='💵' if label=='出售' else '🛒'
        view.add_item(discord.ui.Button(label=label,emoji=emoji,custom_id='new:'+custom,style=discord.ButtonStyle.secondary))
    return view

def admin(member, roles):
    return member.guild_permissions.administrator or any(r.id in roles for r in member.roles)

def safe_self_role(role,guild):
    return bool(role and not role.managed and not role.is_default() and role<guild.me.top_role
                and not role.permissions.administrator and not role.permissions.manage_roles
                and role.id not in set(cfg.TRADE_ADMIN_ROLES+cfg.NOTICE_ADMIN_ROLES))

def embeds(title, body, banner=None):
    # Separate image embed keeps the banner above the text without a SDK upgrade.
    result = []
    if banner:
        if not banner.startswith('https://'):
            raise ValueError('横幅请使用 HTTPS 图片链接')
        result.append(discord.Embed(color=0x9854DE).set_image(url=banner))
    if len(title)+len(body)>5800:
        raise ValueError('标题和正文合计请控制在 5800 字符以内')
    for offset in range(0,max(len(body),1),3500):
        result.append(discord.Embed(title=title if offset==0 else None,description=body[offset:offset+3500] or ' ',color=0x9854DE))
    return result

class NewCommunity(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self.invites=None
        self.invite_lock=asyncio.Lock()
        self.role_locks={}
        self.panel_lock=asyncio.Lock()
        self.ready=False
        self.maintenance.start()

    def cog_unload(self):
        self.maintenance.cancel()

    async def channel(self, ident):
        channel=self.bot.get_channel(ident)
        if channel is None: channel=await self.bot.fetch_channel(ident)
        if not channel or getattr(channel.guild,'id',None)!=cfg.GUILD_ID:
            raise ValueError('频道未配置或不属于新社群')
        return channel

    async def save_panel(self, channel_id, key, title, body, view=None, banner=None):
        if not channel_id:
            return
        async with self.panel_lock:
            if key in ('rules','verify') and await db.setting('community_content:channel:'+str(channel_id)):
                return
            # An admin edit may race the maintenance snapshot. Use persisted content.
            if key in ('rules','verify') or key.startswith('channel:'):
                current=await db.setting('community_content:'+key)
                if current:
                    title,body,banner=current['title'],current['body'],current.get('banner')
            await self._save_panel(channel_id,key,title,body,view,banner)

    async def _save_panel(self, channel_id, key, title, body, view=None, banner=None):
        channel=await self.channel(channel_id)
        content=texts()
        if banner is None: banner=content.get('banners',{}).get(str(channel_id))
        file=None
        if key=='language':
            file=discord.File(Path(__file__).resolve().parent.parent/'png'/'Language.png',filename='Language.png')
            banner='attachment://Language.png'
        try:
            rendered=notice_card.layout(title,body,banner,view,attachment_banner=file is not None)
            await self._publish_panel_message(channel,key,rendered,file)
        finally:
            if file is not None: file.close()

    async def _publish_panel_message(self,channel,key,rendered,file=None):
        channel_id=channel.id
        previous=await db.setting('panel:'+key)
        if not previous and key.startswith('channel:'):
            legacy='verify' if channel_id==cfg.VERIFY_CHANNEL_ID else 'rules' if channel_id==cfg.RULES_CHANNEL_ID else None
            if legacy: previous=await db.setting('panel:'+legacy)
        if previous and previous['channel']==channel.id:
            try:
                msg=await channel.fetch_message(previous['message'])
                await notice_card.edit(msg,rendered,file=file)
                if key.startswith('channel:'):
                    await db.setting('panel:'+key,{'channel':channel.id,'message':msg.id})
                return
            except discord.NotFound:
                pass
        msg=await notice_card.send(channel,rendered,file=file)
        await db.setting('panel:'+key,{'channel':channel.id,'message':msg.id})

    async def snapshot_invites(self,guild):
        invites=await guild.invites()
        self.invites={i.code:i.uses for i in invites}
        for inv in invites:
            # Bot-created links have an explicit owner; do not overwrite it with bot ID.
            if inv.inviter and not inv.inviter.bot:
                await db.query('INSERT INTO invite_links(code,owner_id) VALUES(%s,%s) ON DUPLICATE KEY UPDATE code=code',(inv.code,inv.inviter.id))
        return invites

    @tasks.loop(minutes=10)
    async def maintenance(self):
        try:
            guild=self.bot.get_guild(cfg.GUILD_ID)
            if not guild:
                return
            if not self.ready:
                try:
                    await self.snapshot_invites(guild)
                    self.ready=True
                except discord.Forbidden:
                    log.error('Cannot read new guild invites; verification/languages remain available')
            content=texts()
            for kind,channel_id in (('rules',cfg.RULES_CHANNEL_ID),('verify',cfg.VERIFY_CHANNEL_ID)):
                if not channel_id: continue
                if await db.setting('community_content:channel:'+str(channel_id)): continue
                if cfg.RULES_CHANNEL_ID==cfg.VERIFY_CHANNEL_ID:
                    log.error('Rules and verify must use different channels')
                    break
                saved=await db.setting('community_content:'+kind)
                panel=saved or {'title':content.get(kind+'_title',kind.title()),'body':content.get(kind+'_body',''),
                                'banner':content.get(kind+'_banner',content.get('banners',{}).get(str(channel_id)))}
                if panel['body']:
                    await self.save_panel(channel_id,kind,panel['title'],panel['body'],
                        buttons([('我已阅读并同意，完成验证','verify')]) if kind=='verify' else None,banner=panel.get('banner'))
            await self.save_panel(cfg.LANGUAGE_CHANNEL_ID,'language','Choose your language',
                'English is the default. You may select one additional language, or none.\nEnglish 为默认语言，可额外选择一种语言，也可以不选。',
                buttons([(name,'lang:'+str(role)) for name,role in cfg.LANGUAGES.items() if role]+[('清除额外语言','lang:clear')]))
            # Reconcile missed role events using recorded invitations, not new attribution.
            if cfg.VERIFIED_ROLE_ID:
                rows=await db.query('SELECT user_id FROM invitations WHERE verified=FALSE AND inviter_id IS NOT NULL')
                for row in rows:
                    member=guild.get_member(row['user_id'])
                    if member and any(r.id==cfg.VERIFIED_ROLE_ID for r in member.roles):
                        await self.verify_count(member.id)
            rows=await db.query('SELECT inviter_id,COUNT(*) AS n FROM invitations WHERE verified=TRUE AND inviter_id IS NOT NULL GROUP BY inviter_id ORDER BY n DESC,inviter_id LIMIT 10')
            body='\n'.join(f"{i}. <@{r['inviter_id']}> — **{r['n']}**" for i,r in enumerate(rows,1)) or '暂无有效邀请。'
            await self.save_panel(cfg.RANKING_CHANNEL_ID,'ranking','累计邀请排行榜',body,
                                  buttons([('查看我的邀请','invites'),('创建邀请链接','invite_link')]))
        except Exception:
            log.exception('New community maintenance failed; legacy guild unaffected')

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    async def verify_count(self,user_id):
        await db.query('UPDATE invitations SET verified=TRUE,verified_at=UTC_TIMESTAMP() WHERE user_id=%s AND verified=FALSE AND inviter_id IS NOT NULL',(user_id,))

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.invites=None
        self.ready=False

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.guild.id!=cfg.GUILD_ID or member.bot:
            return
        async with self.invite_lock:
            inviter=None
            code=None
            try:
                before=self.invites
                current=await self.snapshot_invites(member.guild)
                changed=[i for i in current if before is not None and i.code in before and (i.uses or 0)-before[i.code]==1]
                # Ambiguous joins are intentionally unassigned, never guessed.
                if len(changed)==1 and sum(max(0,(i.uses or 0)-before.get(i.code,i.uses or 0)) for i in current)==1:
                    code=changed[0].code
                    owner=await db.query('SELECT owner_id FROM invite_links WHERE code=%s',(code,),one=True)
                    if owner and owner['owner_id']!=member.id:
                        inviter=owner['owner_id']
            except Exception:
                self.invites=None
                log.exception('Invite attribution unavailable')
            await db.query('INSERT IGNORE INTO invitations(user_id,inviter_id,code) VALUES(%s,%s,%s)',(member.id,inviter,code))
            if any(r.id==cfg.VERIFIED_ROLE_ID for r in member.roles):
                await self.verify_count(member.id)

    @commands.Cog.listener()
    async def on_invite_create(self,invite):
        if invite.guild.id==cfg.GUILD_ID:
            async with self.invite_lock:
                if self.invites is not None:
                    self.invites[invite.code]=invite.uses or 0
                if invite.inviter and not invite.inviter.bot:
                    await db.query('INSERT INTO invite_links(code,owner_id) VALUES(%s,%s) ON DUPLICATE KEY UPDATE code=code',(invite.code,invite.inviter.id))

    @commands.Cog.listener()
    async def on_member_update(self,before,after):
        if after.guild.id==cfg.GUILD_ID and any(r.id==cfg.VERIFIED_ROLE_ID for r in after.roles):
            await self.verify_count(after.id)

    @commands.Cog.listener()
    async def on_interaction(self,interaction):
        if not interaction.guild or interaction.guild.id!=cfg.GUILD_ID:
            return
        custom=(interaction.data or {}).get('custom_id','')
        if custom not in ('new:verify','new:invites','new:invite_link') and not custom.startswith('new:lang:'):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if custom=='new:verify':
                configured=await db.setting('community_content:channel:'+str(interaction.channel_id))
                if configured:
                    if not configured.get('verification'): raise ValueError('此频道面板未启用验证。')
                    panel=await db.setting('panel:channel:'+str(interaction.channel_id))
                else:
                    if interaction.channel_id!=cfg.VERIFY_CHANNEL_ID: raise ValueError('请在验证频道操作')
                    panel=await db.setting('panel:verify')
                if not panel or panel['message']!=interaction.message.id:
                    raise ValueError('此验证面板已更新，请使用最新面板')
                role=interaction.guild.get_role(cfg.VERIFIED_ROLE_ID)
                if not safe_self_role(role,interaction.guild):
                    raise ValueError('验证身份组配置或 bot 身份组层级不正确')
                await interaction.user.add_roles(role,reason='Accepted community rules')
                await self.verify_count(interaction.user.id)
                await db.audit(interaction.user.id,'verified',{'role':role.id})
                reply='验证完成。'
            elif custom.startswith('new:lang:'):
                if interaction.channel_id!=cfg.LANGUAGE_CHANNEL_ID:
                    raise ValueError('请在语言频道操作')
                requested=custom.rsplit(':',1)[1]
                allowed={r for r in cfg.LANGUAGES.values() if r}
                if requested!='clear' and int(requested) not in allowed:
                    raise ValueError('未知语言身份组')
                async with self.role_locks.setdefault(interaction.user.id,asyncio.Lock()):
                    member=await interaction.guild.fetch_member(interaction.user.id)
                    old=[r for r in member.roles if r.id in allowed]
                    target=interaction.guild.get_role(int(requested)) if requested!='clear' else None
                    if requested!='clear' and (not safe_self_role(target,interaction.guild) or target.id==cfg.VERIFIED_ROLE_ID):
                        raise ValueError('语言身份组配置或层级不正确')
                    if old:
                        await member.remove_roles(*old,reason='Change optional language')
                    try:
                        if target:
                            await member.add_roles(target,reason='Selected optional language')
                    except Exception:
                        if old:
                            await member.add_roles(*old,reason='Restore language after failed change')
                        raise
                    await db.audit(member.id,'language',{'role':target.id if target else None})
                reply='语言身份组已更新。'
            elif custom=='new:invites':
                row=await db.query('SELECT COUNT(*) AS total,COALESCE(SUM(verified),0) AS verified FROM invitations WHERE inviter_id=%s',(interaction.user.id,),one=True)
                reply=f"累计邀请：{row['total']}；有效邀请：{row['verified']}。"
            else:
                channel=await self.channel(cfg.INVITE_CHANNEL_ID)
                async with self.invite_lock:
                    invites=await channel.guild.invites()
                    owners=await db.query('SELECT code FROM invite_links WHERE owner_id=%s',(interaction.user.id,))
                    codes={r['code'] for r in owners}
                    invite=next((i for i in invites if i.code in codes),None)
                    if not invite:
                        invite=await channel.create_invite(max_age=0,max_uses=0,unique=True,reason=f'Invite owner {interaction.user.id}')
                        await db.query('INSERT INTO invite_links(code,owner_id) VALUES(%s,%s)',(invite.code,interaction.user.id))
                        if self.invites is not None:
                            self.invites[invite.code]=0
                reply=invite.url
            await interaction.followup.send(reply,ephemeral=True)
        except Exception as exc:
            log.exception('New community interaction failed')
            await interaction.followup.send(str(exc) if isinstance(exc,ValueError) else '操作失败，请联系管理员检查权限或服务状态。',ephemeral=True)

    @discord.slash_command(name='new_panel',description='选择频道发布或修改内容、横幅及验证按钮')
    async def panel(self,ctx,channel:discord.TextChannel,verification:bool=None):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.NOTICE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        if channel.guild.id!=cfg.GUILD_ID or not isinstance(channel,discord.TextChannel):
            return await ctx.respond('请选择新社群的文字或公告频道，论坛规则请使用 /new_forum_rules。',ephemeral=True)
        channel_id=channel.id
        kind='channel:'+str(channel_id)
        content=texts()
        saved=await db.setting('community_content:'+kind)
        legacy='verify' if channel_id==cfg.VERIFY_CHANNEL_ID else 'rules' if channel_id==cfg.RULES_CHANNEL_ID else ''
        if not saved and legacy: saved=await db.setting('community_content:'+legacy)
        current=saved or dict(title=content.get(legacy+'_title','频道通知'),body=content.get(legacy+'_body',''),
                             banner=content.get(legacy+'_banner',content.get('banners',{}).get(str(channel_id),'')))
        verified=bool(current.get('verification',legacy=='verify')) if verification is None else verification
        modal=discord.ui.Modal(title='频道内容与横幅')
        title=discord.ui.InputText(label='标题',value=current['title'],max_length=200)
        body=discord.ui.InputText(label='正文',style=discord.InputTextStyle.long,value=current['body'],max_length=3500)
        banner=discord.ui.InputText(label='横幅 HTTPS 图片链接（留空移除）',required=False,value=current.get('banner') or '',max_length=1000)
        for field in (title,body,banner): modal.add_item(field)
        async def submitted(inter):
            if inter.user.id!=ctx.author.id or not admin(inter.user,cfg.NOTICE_ADMIN_ROLES):
                return await inter.response.send_message('没有操作权限。',ephemeral=True)
            try: notice_card.layout(title.value,body.value,banner.value or '')
            except ValueError as exc: return await inter.response.send_message(str(exc),ephemeral=True)
            view=discord.ui.View(timeout=300)
            button=discord.ui.Button(label='确认发布 / 保存修改',emoji='📣',style=discord.ButtonStyle.success)
            used=False
            async def accepted(click):
                nonlocal used
                if click.user.id!=ctx.author.id or not admin(click.user,cfg.NOTICE_ADMIN_ROLES):
                    return await click.response.send_message('没有操作权限。',ephemeral=True)
                if used: return await click.response.send_message('此预览正在处理、已完成或结果待核对，请查看第一次点击后的回复。',ephemeral=True)
                used=True
                try:
                    await click.response.defer(ephemeral=True)
                    values=dict(title=title.value,body=body.value,banner=banner.value or '',verification=verified)
                    await db.setting('community_content:'+kind,values)
                    await db.audit(click.user.id,'community_panel_edit',{'kind':kind,'channel':channel_id,**values})
                    await self.save_panel(channel_id,kind,title.value,body.value,
                        buttons([('我已阅读并同意，完成验证','verify')]) if verified else None,banner=banner.value or '')
                    await click.followup.send(f'已发布到 {channel.mention}，后续编辑使用 /new_panel。',ephemeral=True)
                except (discord.Forbidden,discord.NotFound,ValueError) as exc:
                    used=False
                    await click.followup.send(f'发布未完成：{exc}。修正权限或内容后可以再次点击本预览。',ephemeral=True)
                except Exception:
                    log.exception('Community panel publish failed: %s',kind)
                    await click.followup.send('发布结果待核对，请检查目标频道和服务器日志；不要重复新建。可重新使用 /new_panel 检查已保存的面板。',ephemeral=True)
            button.callback=accepted
            view.add_item(button)
            await notice_card.preview(inter,notice_card.layout(title.value,body.value,banner.value or '',view,
                f'目标频道：{channel.mention}\n验证按钮：'+('开启' if verified else '关闭')),view)
        modal.callback=submitted
        await ctx.send_modal(modal)

    @discord.slash_command(name='new_notice',description='发布、编辑或重新发布通知，可置顶论坛帖')
    async def notice(self,ctx,channel: discord.abc.GuildChannel, message_id: str = '', copy_from: str = '',pin:bool=False):
        await self.notice_editor(ctx,channel,message_id,copy_from,pin)

    @discord.slash_command(name='new_forum_rules',description='发布或修改论坛置顶规则帖（仅管理员）')
    async def forum_rules(self,ctx,forum:discord.ForumChannel,message_id:str=''):
        await self.notice_editor(ctx,forum,message_id,'',True)

    async def notice_editor(self,ctx,channel,message_id='',copy_from='',pin=False):
        if not ctx.guild or ctx.guild.id!=cfg.GUILD_ID or not admin(ctx.author,cfg.NOTICE_ADMIN_ROLES):
            return await ctx.respond('没有操作权限。',ephemeral=True)
        if channel.guild.id!=cfg.GUILD_ID or not isinstance(channel,(discord.TextChannel,discord.ForumChannel)):
            return await ctx.respond('请选择本社群的文字、公告或论坛频道。',ephemeral=True)
        if message_id and copy_from:
            return await ctx.respond('编辑与复制重发请选择一种。',ephemeral=True)
        previous=await db.setting('notice:'+(message_id or copy_from)) if message_id or copy_from else None
        if (message_id or copy_from) and not previous:
            return await ctx.respond('找不到该通知记录。',ephemeral=True)
        modal=discord.ui.Modal(title='通知内容')
        title=discord.ui.InputText(label='标题',value=previous['title'] if previous else '',max_length=200)
        body=discord.ui.InputText(label='正文',style=discord.InputTextStyle.long,value=previous['body'] if previous else '',max_length=3500)
        banner=discord.ui.InputText(label='横幅 HTTPS 链接（可留空）',required=False,value=previous.get('banner','') if previous else '')
        tags=discord.ui.InputText(label='论坛标签 ID（英文逗号分隔，可留空）',required=False)
        for field in (title,body,banner,tags): modal.add_item(field)
        async def submitted(inter):
            try:
                chosen_banner=banner.value or texts().get('banners',{}).get(str(channel.id))
                render=notice_card.layout(title.value,body.value,chosen_banner)
                view=discord.ui.View(timeout=300)
                publish=discord.ui.Button(label='确认发布 / 保存修改',emoji='📣',style=discord.ButtonStyle.success)
                state='ready'
                published=None
                async def confirmed(click):
                    nonlocal state,published
                    if click.user.id!=ctx.author.id or not admin(click.user,cfg.NOTICE_ADMIN_ROLES):
                        return await click.response.send_message('没有操作权限。',ephemeral=True)
                    if state!='ready':
                        messages={'processing':'正在发布，请稍候。','done':f'已发布成功。消息 ID：{getattr(published,"id","")}。',
                                  'unknown':'发布结果待核对，请先检查目标频道和第一次点击后的回复，避免重复发帖。'}
                        return await click.response.send_message(messages[state],ephemeral=True)
                    state='processing'
                    try:
                        await click.response.defer(ephemeral=True)
                        if published is not None:
                            msg=published
                        elif previous and message_id:
                            target=await self.channel(previous['channel'])
                            if getattr(target,'parent_id',target.id)!=channel.id and target.id!=channel.id:
                                raise ValueError('编辑必须选择原通知频道；重新发布请留空消息 ID')
                            msg=await target.fetch_message(int(message_id))
                            if msg.author.id!=self.bot.user.id: raise ValueError('仅能编辑 bot 发布的通知')
                            if isinstance(target,discord.Thread):
                                await target.edit(name=title.value,archived=False)
                            await notice_card.edit(msg,render)
                        elif isinstance(channel,discord.ForumChannel):
                            selected=[int(x.strip()) for x in tags.value.split(',') if x.strip()]
                            available={t.id for t in channel.available_tags}
                            if len(selected)>5 or not set(selected)<=available: raise ValueError('论坛标签不正确')
                            if channel.requires_tag and not selected: raise ValueError('这个论坛要求标签，请填写一个有效的论坛标签 ID。')
                            msg=await notice_card.create_forum(channel,title.value,render,[t for t in channel.available_tags if t.id in selected])
                        else:
                            msg=await notice_card.send(channel,render,nonce=inter.id)
                        published=msg
                        await db.setting('notice:'+str(msg.id),{'channel':msg.channel.id,'title':title.value,'body':body.value,'banner':chosen_banner or ''})
                        await db.audit(click.user.id,'notice_publish',{'message':msg.id,'channel':msg.channel.id,'title':title.value,'body':body.value,'banner':banner.value})
                        if pin:
                            try:
                                if isinstance(msg.channel,discord.Thread):
                                    await msg.channel.edit(pinned=True,reason='Administrator published forum rules')
                                else:
                                    await msg.pin(reason='Administrator pinned notice')
                                await db.audit(click.user.id,'notice_pin',{'message':msg.id,'channel':msg.channel.id})
                            except discord.HTTPException:
                                state='ready'
                                log.exception('Notice saved but pin failed: %s',msg.id)
                                return await click.followup.send(f'内容已保存，但置顶失败。请检查 bot 的管理帖子/消息权限。消息 ID：{msg.id}，请用此 ID 编辑重试，勿重复新建。',ephemeral=True)
                        state='done'
                        await click.followup.send(f'已发布成功。频道：{channel.mention}，消息 ID：{msg.id}',ephemeral=True)
                    except (ValueError,discord.Forbidden,discord.NotFound) as exc:
                        state='ready'
                        await click.followup.send(f'发布未完成：{exc}。修正后可再次点击此预览。',ephemeral=True)
                    except discord.HTTPException as exc:
                        state='ready' if exc.status==400 or published is not None else 'unknown'
                        log.exception('Notice publish HTTP failure')
                        await click.followup.send(f'发布未完成（HTTP {exc.status}）。'+('请检查内容和权限后重试本预览。' if state=='ready' else '结果需核对，请检查目标频道，勿重复新建。'),ephemeral=True)
                    except Exception:
                        state='ready' if published is not None else 'unknown'
                        log.exception('Notice publish failed')
                        await click.followup.send(f'内容已发出，消息 ID：{published.id}；记录保存或后续操作失败，可再次点击本预览继续，不会新建消息。' if published else '发布结果待核对，请检查目标频道及服务器日志，勿重复新建。',ephemeral=True)
                publish.callback=confirmed
                view.add_item(publish)
                await notice_card.preview(inter,notice_card.layout(title.value,body.value,chosen_banner,view),view)
            except ValueError as exc:
                await inter.response.send_message(str(exc),ephemeral=True)
        modal.callback=submitted
        await ctx.send_modal(modal)

def setup(bot):
    if cfg.GUILD_ID and cfg.GUILD_ID!=config.GUILD_ID:
        bot.add_cog(NewCommunity(bot))
