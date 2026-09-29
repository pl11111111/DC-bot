"""Translate the current bot article, never a stale copy from settings."""
import discord
from discord.http import Route
from utils import verification_flow as vf
from utils.verification_text import text
from utils import translation_store

async def sync_existing(cog):
    rows=await vf.db.query("SELECT s.setting_key,s.value FROM settings s WHERE s.setting_key LIKE %s AND NOT EXISTS (SELECT 1 FROM settings done WHERE done.setting_key=CONCAT('translation_button:',SUBSTRING(s.setting_key,8))) LIMIT 10",('notice:%',))
    import json
    for row in rows:
        ident=int(row['setting_key'].split(':',1)[1])
        record=json.loads(row['value'])
        channel=await cog.channel(record['channel'])
        route=dict(channel_id=channel.id,message_id=ident)
        try:
            raw=await channel._state.http.request(Route('GET','/channels/{channel_id}/messages/{message_id}',**route))
        except discord.NotFound:
            await vf.db.setting('translation_button:'+str(ident),True)
            continue
        if int(raw['author']['id'])!=cog.bot.user.id: continue
        components=raw.get('components',[])
        if 'new:article_translate' not in json.dumps(components):
            if len(components)>=5: continue
            components.append({'type':1,'components':[{'type':2,'style':2,'label':'Translate','custom_id':'new:article_translate','emoji':{'name':'🌐'}}]})
            await channel._state.http.request(Route('PATCH','/channels/{channel_id}/messages/{message_id}',**route),
                json={'components':components,'allowed_mentions':{'parse':[]}})
        await vf.db.setting('translation_button:'+str(ident),True)

async def read(message,bot_id):
    raw=await message._state.http.request(Route('GET','/channels/{channel_id}/messages/{message_id}',
        channel_id=message.channel.id,message_id=message.id))
    if int(raw['author']['id'])!=bot_id: raise ValueError('Only bot articles can be translated here.')
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
    async def render(article,lang):
        translated,missing=await translation_store.resolve(inter.message.id,article,lang)
        body=(text(lang)['fallback']+'\n\n' if missing else '')+translated['body']
        # Discord supports at most 6000 embed characters in one message.
        if len(body)+len(translated['title'])>5700: raise ValueError('This article is too long; please split it into shorter posts.')
        return [discord.Embed(title=translated['title'] or None,description=body[i:i+3500],color=0x9854DE) for i in range(0,len(body),3500)]
    async def choose(click):
        if not click.guild or click.guild.id!=vf.cfg.GUILD_ID or click.user.id!=inter.user.id:
            return await click.response.send_message('This translation menu belongs to another user.',ephemeral=True)
        await click.response.defer()
        try:
            current=await read(inter.message,cog.bot.user.id)
            await click.edit_original_response(embeds=await render(current,menu.values[0]),view=view,allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            await click.followup.send('Unable to load the current article. Please reopen Translate.',ephemeral=True)
    menu.callback=choose
    view.add_item(menu)
    await inter.followup.send(embeds=await render(original,language),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
