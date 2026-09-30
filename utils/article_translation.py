"""Translate the current bot article, never a stale copy from settings."""
import discord
from discord.http import Route
from utils import verification_flow as vf
from utils.verification_text import text
from utils import translation_store

def style_translation_buttons(components):
    changed=False
    for item in components:
        if item.get('custom_id')=='new:article_translate' and item.get('style')!=1:
            item['style']=1
            changed=True
        changed=style_translation_buttons(item.get('components',[])) or changed
    return changed

def normalize_translation_buttons(components):
    changed=style_translation_buttons(components)
    moved=[]
    for container in components:
        if container.get('type')!=17: continue
        children=[]
        for child in container.get('components',[]):
            if child.get('type')==1:
                buttons=child.get('components',[])
                translated=[b for b in buttons if b.get('custom_id') in ('new:article_translate','new:long_translate','new:translate')]
                if translated:
                    moved.extend(translated)
                    others=[b for b in buttons if b not in translated]
                    if others: children.append(dict(child,components=others))
                    continue
            children.append(child)
        container['components']=children
    if moved:
        for offset in range(0,len(moved),5):
            components.append({'type':1,'components':moved[offset:offset+5]})
        changed=True
    return changed

async def sync_existing(cog):
    rows=await vf.db.query("SELECT s.setting_key,s.value FROM settings s WHERE s.setting_key LIKE %s AND NOT EXISTS (SELECT 1 FROM settings done WHERE done.setting_key=CONCAT('translation_button_v3:',SUBSTRING(s.setting_key,8))) LIMIT 10",('notice:%',))
    import json
    for row in rows:
        ident=int(row['setting_key'].split(':',1)[1])
        record=json.loads(row['value'])
        channel=await cog.channel(record['channel'])
        route=dict(channel_id=channel.id,message_id=ident)
        try:
            raw=await channel._state.http.request(Route('GET','/channels/{channel_id}/messages/{message_id}',**route))
        except discord.NotFound:
            await vf.db.setting('translation_button_v3:'+str(ident),True)
            continue
        if int(raw['author']['id'])!=cog.bot.user.id: continue
        components=raw.get('components',[])
        changed=normalize_translation_buttons(components)
        if 'new:article_translate' not in json.dumps(components):
            if len(components)>=5: continue
            components.append({'type':1,'components':[{'type':2,'style':1,'label':'Translate','custom_id':'new:article_translate','emoji':{'name':'🌐'}}]})
            changed=True
        if changed:
            await channel._state.http.request(Route('PATCH','/channels/{channel_id}/messages/{message_id}',**route),
                json={'components':components,'allowed_mentions':{'parse':[]}})
        await vf.db.setting('translation_button_v3:'+str(ident),True)

async def read(message,bot_id):
    raw=await message._state.http.request(Route('GET','/channels/{channel_id}/messages/{message_id}',
        channel_id=message.channel.id,message_id=message.id))
    if int(raw['author']['id'])!=bot_id: raise ValueError('Only bot articles can be translated here.')
    import json
    if 'new:long_translate' in json.dumps(raw.get('components',[])):
        from utils.long_article import read as read_long
        return await read_long(message,bot_id,raw)
    parts=[]
    def walk(items):
        for item in items:
            if item.get('type')==10: parts.append(item.get('content',''))
            walk(item.get('components',[]))
    walk(raw.get('components',[]))
    if not parts:
        if raw.get('content'): parts.append(raw['content'])
        for embed in raw.get('embeds',[]):
            parts.append('\n\n'.join(x for x in ('## '+embed['title'] if embed.get('title') else '',embed.get('description','')) if x))
    content='\n\n'.join(parts)
    title=''
    if content.startswith('## '):
        title,_,content=content[3:].partition('\n\n')
    if not content: raise ValueError('No article text was found.')
    return {'title':title,'body':content}

async def start(cog,inter):
    member=await inter.guild.fetch_member(inter.user.id)
    language=vf.preferred(member,inter.locale,await vf.db.setting('verify_language:'+str(member.id)))
    original=await read(inter.message,cog.bot.user.id)
    view=discord.ui.View(timeout=600)
    menu=discord.ui.Select(placeholder=text(language)['choose'],options=[discord.SelectOption(label=x,value=x,default=x==language) for x in vf.languages()])
    page=0
    pages=[]
    selected=language
    previous=discord.ui.Button(label='Previous',emoji='⬅️')
    following=discord.ui.Button(label='Next',emoji='➡️')
    import asyncio
    lock=asyncio.Lock()
    async def render(article,lang):
        nonlocal pages,page
        translated,missing=await translation_store.resolve(inter.message.id,article,lang)
        body=(text(lang)['fallback']+'\n\n' if missing else '')+translated['body']
        from utils.long_article import split_body
        pages=split_body(body)
        page=min(page,max(0,len(pages)-1))
        previous.disabled=page==0
        following.disabled=page>=len(pages)-1
        return [discord.Embed(title=translated['title'] or None,description=pages[page],color=0x9854DE).set_footer(text=f'{lang} · {page+1}/{len(pages)}')]
    async def update(click,delta=None):
        nonlocal page,selected
        if not click.guild or click.guild.id!=vf.cfg.GUILD_ID or click.user.id!=inter.user.id:
            return await click.response.send_message('This translation menu belongs to another user.',ephemeral=True)
        await click.response.defer()
        async with lock:
            try:
                if delta is None:
                    selected=menu.values[0];page=0
                else: page=max(0,page+delta)
                current=await read(inter.message,cog.bot.user.id)
                await click.edit_original_response(embeds=await render(current,selected),view=view,allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                await click.followup.send('Unable to load the current article. Please reopen Translate.',ephemeral=True)
    async def choose(click): await update(click)
    async def back(click): await update(click,-1)
    async def forward(click): await update(click,1)
    menu.callback=choose
    previous.callback=back
    following.callback=forward
    view.add_item(menu)
    view.add_item(previous);view.add_item(following)
    await inter.followup.send(embeds=await render(original,language),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
