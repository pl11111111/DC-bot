"""Single-container notices using the existing authenticated Discord transport."""
import discord
from discord.http import Route
from discord.webhook.async_ import async_context
from utils.trade_card import V2


def layout(title,body,banner='',view=None,intro=''):
    text='\n\n'.join(x for x in (intro,'## '+title if title else '',body) if x)
    if len(text)>4000: raise ValueError('标题和正文合计过长，请缩短至 4000 字以内。')
    children=[]
    if banner:
        if not banner.startswith('https://'): raise ValueError('横幅请使用 HTTPS 图片链接')
        children.append({'type':12,'items':[{'media':{'url':banner}}]})
    if text: children.append({'type':10,'content':text})
    if view and view.children: children.extend(view.to_components())
    return [{'type':17,'accent_color':0x9854DE,'components':children}]


async def preview(inter,components,view):
    await inter.response.defer(ephemeral=True)
    hook=inter.followup
    data=await async_context.get().execute_webhook(hook.id,hook.token,session=hook.session,
        payload={'flags':V2|64,'components':components,'allowed_mentions':{'parse':[]}},wait=True)
    inter._state.store_view(view,int(data['id']))


async def send(channel,components,nonce=None):
    payload={'flags':V2,'components':components,'allowed_mentions':{'parse':[]}}
    if nonce is not None: payload.update(nonce=str(nonce),enforce_nonce=True)
    data=await channel._state.http.request(Route('POST','/channels/{channel_id}/messages',channel_id=channel.id),json=payload)
    return channel._state.create_message(channel=channel,data=data)


async def edit(message,components):
    await message._state.http.request(Route('PATCH','/channels/{channel_id}/messages/{message_id}',
        channel_id=message.channel.id,message_id=message.id),json={
            'flags':V2,'content':None,'embeds':[],'components':components,'allowed_mentions':{'parse':[]}})


async def create_forum(channel,title,components,tags):
    data=await channel._state.http.request(Route('POST','/channels/{channel_id}/threads',channel_id=channel.id),
        json={'name':title,'applied_tags':[str(tag.id) for tag in tags],
              'message':{'flags':V2,'components':components,'allowed_mentions':{'parse':[]}}})
    thread=discord.Thread(guild=channel.guild,state=channel._state,data=data)
    return thread.get_partial_message(int(data.get('message',{}).get('id',thread.id)))
