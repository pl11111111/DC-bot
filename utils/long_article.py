"""Publish file-backed articles and verify every linked message before export."""
import asyncio
import io
import logging
import discord
from utils import new_store as db, notice_card
from utils.translation_editor import authorized

MAX_BODY=30000
log=logging.getLogger(__name__)

def split_body(body,limit=3400):
    chunks=[]
    while len(body)>limit:
        end=body.rfind('\n\n',0,limit)
        if end<limit//2: end=body.rfind('\n',0,limit)
        end=end+1 if end>=limit//2 else limit
        chunks.append(body[:end]);body=body[end:]
    if body: chunks.append(body)
    return chunks

def text_parts(raw):
    parts=[]
    def walk(items):
        for item in items:
            if item.get('type')==10: parts.append(item.get('content',''))
            walk(item.get('components',[]))
    walk(raw.get('components',[]))
    return parts

async def read(message,bot_id,raw):
    from discord.http import Route
    record=await db.setting('long_article:'+str(message.id))
    if not record or record.get('state')!='ready':
        raise ValueError('长文尚未完整发布，暂不能导出或翻译，请联系管理员。')
    if record['channel']!=message.channel.id: raise ValueError('长文频道记录不一致。')
    for part in record['parts']:
        current=raw if part['id']==message.id else await message._state.http.request(
            Route('GET','/channels/{channel_id}/messages/{message_id}',channel_id=record['channel'],message_id=part['id']))
        if int(current['author']['id'])!=bot_id or text_parts(current)!=part['text']:
            raise ValueError('长文分段已被修改，不能使用旧原文，请重新发布完整文件。')
    return record['source']

async def publish(cog,channel,title,body,tags):
    chunks=split_body(body)
    parts=[]
    root=None
    record=dict(state='publishing',channel=None,source={'title':title,'body':body},parts=parts)
    for index,chunk in enumerate(chunks):
        view=discord.ui.View(timeout=None)
        if index==0:
            view.add_item(discord.ui.Button(label='Translate',emoji='🌐',style=discord.ButtonStyle.primary,custom_id='new:long_translate'))
        layout=notice_card.layout(title if index==0 else '',chunk,view=view,buttons_below=True)
        if index==0 and isinstance(channel,discord.ForumChannel):
            msg=await notice_card.create_forum(channel,title,layout,tags)
            channel=msg.channel
        else:
            msg=await notice_card.send(channel,layout)
        if root is None:
            root=msg.id;record['channel']=channel.id
        parts.append({'id':msg.id,'text':text_parts({'components':layout})})
        await db.setting('long_article:'+str(root),record)
    record['state']='ready'
    await db.setting('long_article:'+str(root),record)
    return root,channel.id

async def editor(cog,ctx,channel,title,file,tags=''):
    await ctx.defer(ephemeral=True)
    try:
        if not isinstance(channel,(discord.TextChannel,discord.ForumChannel)) or channel.guild.id!=ctx.guild.id:
            raise ValueError('请选择本社群的文字或论坛频道。')
        if not title.strip() or len(title)>100: raise ValueError('标题必须填写，最多 100 字。')
        if file.size>128*1024: raise ValueError('Markdown 文件不能超过 128 KB。')
        data=await file.read()
        if len(data)>128*1024: raise ValueError('Markdown 文件不能超过 128 KB。')
        try: body=data.decode('utf-8-sig')
        except UnicodeError: raise ValueError('请上传 UTF-8 编码的 Markdown 或 TXT 文件。')
        if not body.strip() or len(body)>MAX_BODY: raise ValueError('正文必须填写，最多 30000 字符，请勿裁剪后重试。')
        selected=[]
        if isinstance(channel,discord.ForumChannel):
            ids=[int(x.strip()) for x in tags.split(',') if x.strip()]
            available={t.id:t for t in channel.available_tags}
            if len(ids)>5 or not set(ids)<=available.keys(): raise ValueError('论坛标签不正确。')
            if channel.requires_tag and not ids: raise ValueError('论坛要求标签，请填写 tags 参数。')
            selected=[available[x] for x in ids]
        view=discord.ui.View(timeout=300)
        button=discord.ui.Button(label='确认发布完整文章',style=discord.ButtonStyle.success,emoji='📣')
        lock=asyncio.Lock()
        attempted=False
        async def confirm(inter):
            nonlocal attempted
            await inter.response.defer(ephemeral=True)
            if not await authorized(inter,ctx.author.id): return await inter.followup.send('没有操作权限。',ephemeral=True)
            async with lock:
                if attempted: return await inter.followup.send('已提交发布，请查看上次结果；如有异常请先检查频道，避免重复发布。',ephemeral=True)
                attempted=True
                try:
                    async with cog.panel_lock:
                        root,channel_id=await publish(cog,channel,title,body,selected)
                    await db.audit(ctx.author.id,'long_article_publish',{'message':root,'channel':channel_id,'characters':len(body)})
                    await inter.followup.send(f'完整文章已发布，共 {len(body)} 字符。导出翻译请使用首条消息链接：\nhttps://discord.com/channels/{ctx.guild.id}/{channel_id}/{root}',ephemeral=True)
                except Exception:
                    log.exception('Long article publication interrupted')
                    await inter.followup.send('发布未能完整确认，请先检查目标频道，勿重复点击。未完成的长文不会开放翻译导出；确认后可删除不完整文章并重新发布。',ephemeral=True)
        button.callback=confirm;view.add_item(button)
        await ctx.followup.send(f'目标：{channel.mention}\n标题：{title}\n正文 {len(body)} 字符，将分为 {len(split_body(body))} 条消息。请核对完整附件后确认。',
            file=discord.File(io.BytesIO(body.encode('utf-8')),filename='article-preview.md'),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.exception('Long article preview failed')
        await ctx.followup.send(str(exc) if isinstance(exc,ValueError) else '文件读取失败，请检查文件和频道权限。',ephemeral=True)
