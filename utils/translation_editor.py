"""Private admin editor with source revalidation before saving a translation."""
import asyncio
import logging
import discord
from discord.http import Route
from utils import translation_store as store, article_translation as articles
from utils.verification_text import ROWS
import config

log=logging.getLogger(__name__)

async def authorized(inter,owner):
    from modules.new_community import admin
    if not inter.guild or inter.guild.id!=config.NEW.GUILD_ID or inter.user.id!=owner: return False
    try: member=await inter.guild.fetch_member(owner)
    except discord.HTTPException: return False
    return admin(member,config.NEW.NOTICE_ADMIN_ROLES)

async def ensure_button(message,bot_id):
    route=dict(channel_id=message.channel.id,message_id=message.id)
    raw=await message._state.http.request(Route('GET','/channels/{channel_id}/messages/{message_id}',**route))
    if int(raw['author']['id'])!=bot_id: raise ValueError('Only bot messages may be edited.')
    import json
    components=raw.get('components',[])
    serialized=json.dumps(components)
    changed=articles.normalize_translation_buttons(components)
    if 'new:translate' in serialized or 'new:article_translate' in serialized or 'new:long_translate' in serialized:
        if not changed: return
    else:
        if len(components)>=5: raise ValueError('消息按钮已满，请管理员调整原消息布局。')
        components.append({'type':1,'components':[{'type':2,'style':1,'label':'Translate','custom_id':'new:article_translate','emoji':{'name':'🌐'}}]})
    await message._state.http.request(Route('PATCH','/channels/{channel_id}/messages/{message_id}',**route),
        json={'components':components,'allowed_mentions':{'parse':[]}})

async def open_editor(cog,ctx,message_link,language):
    if language not in ROWS or language=='English':
        return await ctx.respond('请选择非 English 的译文语言；英文原文请通过原发布指令修改。',ephemeral=True)
    await ctx.defer(ephemeral=True)
    try:
        channel_id,message_id=store.parse_link(message_link,config.NEW.GUILD_ID)
        channel=await cog.channel(channel_id)
        message=channel.get_partial_message(message_id)
        source=await articles.read(message,cog.bot.user.id)
        previous=await store.lookup(message_id,language)
        if len(source['body'])>3500 or (previous and len(previous['body'])>3500):
            return await ctx.followup.send('长篇文章请使用 /new_translation_batch 导出和导入完整译文，编辑窗口无法容纳全文。',ephemeral=True)
        if previous:
            initial={'title':previous['title'],'body':previous['body']}
        else:
            initial,missing=await store.resolve(message_id,source,language)
            if missing: initial={'title':'','body':''}
        view=discord.ui.View(timeout=600)
        button=discord.ui.Button(label='编辑译文',emoji='📝',style=discord.ButtonStyle.primary)
        async def edit(inter):
            if not await authorized(inter,ctx.author.id):
                return await inter.response.send_message('没有操作权限。',ephemeral=True)
            modal=discord.ui.Modal(title=f'译文 · {language}')
            title=discord.ui.InputText(label='翻译标题',value=initial['title'],max_length=200)
            body=discord.ui.InputText(label='翻译正文',value=initial['body'],style=discord.InputTextStyle.long,max_length=3500)
            modal.add_item(title);modal.add_item(body)
            async def submitted(click):
                if not await authorized(click,ctx.author.id):
                    return await click.response.send_message('没有操作权限。',ephemeral=True)
                await click.response.defer(ephemeral=True)
                try:
                    if await articles.read(message,cog.bot.user.id)!=source:
                        return await click.followup.send('原文已修改，请重新打开 /new_translation。',ephemeral=True)
                except Exception:
                    log.exception('Translation preview source read failed')
                    return await click.followup.send('无法核对原消息，请检查权限后重新打开指令。',ephemeral=True)
                preview=discord.ui.View(timeout=300)
                save=discord.ui.Button(label='确认保存译文',emoji='✅',style=discord.ButtonStyle.success)
                lock=asyncio.Lock()
                done=False
                async def confirmed(confirm):
                    nonlocal done
                    if not await authorized(confirm,ctx.author.id):
                        return await confirm.response.send_message('没有操作权限。',ephemeral=True)
                    await confirm.response.defer(ephemeral=True)
                    async with lock:
                        if done: return await confirm.followup.send('此译文已保存，请勿重复提交。',ephemeral=True)
                        try:
                            async with cog.panel_lock:
                                if await articles.read(message,cog.bot.user.id)!=source:
                                    raise ValueError('原文已修改，请重新打开 /new_translation，按最新原文翻译。')
                                await store.save(message_id,channel_id,source,language,title.value,body.value,
                                    ctx.author.id,(previous or {}).get('revision'))
                                done=True
                                try: await ensure_button(message,cog.bot.user.id)
                                except Exception:
                                    log.exception('Translation saved but button update failed: %s',message_id)
                                    return await confirm.followup.send('译文已保存，但翻译按钮添加失败。请检查消息权限或重新发布原面板。',ephemeral=True)
                            await confirm.followup.send('译文已保存，Translate 立即生效。原文变化后本译文将停止使用。',ephemeral=True)
                        except Exception as exc:
                            log.exception('Translation save failed: %s',message_id)
                            await confirm.followup.send(str(exc) if isinstance(exc,ValueError) else '保存结果需核对，请重新打开指令查看，勿重复提交。',ephemeral=True)
                save.callback=confirmed
                preview.add_item(save)
                await click.followup.send(content=f'语言：{language}\n原消息：{message_link}',
                    embed=discord.Embed(title=title.value,description=body.value,color=0x9854DE),view=preview,
                    ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            modal.callback=submitted
            await inter.response.send_modal(modal)
        button.callback=edit
        view.add_item(button)
        stale=bool(previous and previous['source_hash']!=store.fingerprint(source))
        await ctx.followup.send(f'原文：{source["title"] or "无标题"}\n语言：{language}\n'+
            ('旧译文已失效，请按最新原文重新核对。\n' if stale else '')+'点击下方按钮粘贴或修改译文。',view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.exception('Translation editor failed')
        await ctx.followup.send(str(exc) if isinstance(exc,ValueError) else '无法读取消息，请检查链接及 bot 的频道访问权限。',ephemeral=True)
