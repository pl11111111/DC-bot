"""Components V2 transport isolated from the legacy py-cord UI models."""
from copy import deepcopy
from types import SimpleNamespace
from discord.http import Route

V2=1 << 15


def components(embed,view,has_banner,has_qr=False):
    children=[]
    if has_banner:
        children.append({'type':12,'items':[{'media':{'url':'attachment://trade-step.png'}}]})
    if embed is not None:
        body=[]
        if embed.title: body.append('## '+embed.title)
        if embed.description: body.append(embed.description)
        compact=[]
        def flush():
            if compact:
                body.append('　｜　'.join(compact))
                compact.clear()
        for field in embed.fields:
            if field.inline:
                compact.append(f'**{field.name}** {field.value}')
                if len(compact)==2: flush()
            else:
                flush()
                body.append(f'**{field.name}**\n{field.value}')
        flush()
        if embed.footer.text: body.append('-# '+embed.footer.text)
        text='\n\n'.join(body)
        if len(text)>4000:
            raise ValueError('交易卡片文字超过 Discord 限制，请缩短内容')
        if text: children.append({'type':10,'content':text})
    if has_qr:
        children.append({'type':12,'items':[{'media':{'url':'attachment://payment-qr.png'},'description':'USDT-BEP20 收款地址二维码；请自行核对币种、网络和实际到账金额。'}]})
    if view and view.children:
        children.append({'type':14,'divider':True,'spacing':1})
        children.extend(view.to_components())
    return [{'type':17,'accent_color':0x9854DE,'components':children}] if children else []


async def send(channel,embed,view,file,qr_file=None):
    layout=components(embed,view,file is not None,qr_file is not None)
    if not layout: return
    # Use py-cord's authenticated, rate-limited HTTP client and multipart uploader.
    # No dependency upgrade or global change to legacy message handling is needed.
    data=await channel._state.http.send_files(channel.id,files=[f for f in (file,qr_file) if f is not None],
        components=layout,flags=V2,allowed_mentions={'parse':[]})
    return SimpleNamespace(id=int(data['id']))


def disable_buttons(layout):
    result=deepcopy(layout)
    def visit(items):
        for item in items:
            if item.get('type')==2: item['disabled']=True
            if item.get('type')==12:
                for media in item.get('items',[]):
                    media['media']={'url':media['media']['url']}
            visit(item.get('components',[]))
    visit(result)
    return result


async def retire(message):
    flags=getattr(getattr(message,'flags',None),'value',0)
    if not flags & V2:
        await message.edit(view=None)
        return
    # Old SDKs cannot serialize nested V2 models. Preserve the raw card and images.
    http=message._state.http
    route=Route('GET','/channels/{channel_id}/messages/{message_id}',
                channel_id=message.channel.id,message_id=message.id)
    raw=await http.request(route)
    await http.request(Route('PATCH','/channels/{channel_id}/messages/{message_id}',
        channel_id=message.channel.id,message_id=message.id),
        json={'components':disable_buttons(raw['components']),'allowed_mentions':{'parse':[]}})
