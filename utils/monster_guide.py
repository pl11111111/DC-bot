"""Offline monster catalog and bounded Discord cards. No remote scraping at runtime."""
import json
from pathlib import Path
import discord

ROOT=Path(__file__).resolve().parents[1]/'config'/'monster_guide'
BANDS=((1,10,'1–10'),(11,20,'11–20'),(21,30,'21–30'),(31,50,'31–50'),(51,70,'51–70'),(71,10000,'71+'))

def load():
    data=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
    data['monsters'].sort(key=lambda m:(m['level'],m['id']))
    return data

def image_path(path):
    resolved=(ROOT/path).resolve()
    if ROOT.resolve() not in resolved.parents or resolved.suffix!='.png' or not resolved.is_file():
        raise ValueError('Missing or invalid catalog image: '+path)
    return resolved

def card(title,body='',image=None,url=None):
    embed=discord.Embed(title=title,description=body,color=0x9854DE,url=url)
    if image: embed.set_thumbnail(url='attachment://'+Path(image).name)
    return embed,image

def payload(cards):
    embeds=[c[0] for c in cards]
    paths=list(dict.fromkeys(c[1] for c in cards if c[1]))
    if len(cards)>10 or sum(len(e) for e in embeds)>6000: raise ValueError('Guide card exceeds Discord limits')
    return {'embeds':embeds,'files':[discord.File(image_path(p),filename=Path(p).name) for p in paths],
            'allowed_mentions':discord.AllowedMentions.none()}

def groups(data):
    return [(label,[m for m in data['monsters'] if lo<=m['level']<=hi]) for lo,hi,label in BANDS]

def detail(data,monster,back):
    name=monster['name']; mid=str(monster['id'])
    maps=monster.get('maps') or []
    lines=[f"• {m['name']}" for m in maps] or ['No spawn maps listed by the source.']
    base=f"**Level:** {monster['level']} · **HP:** {monster['hp']:,} · **EXP:** {monster['exp']:,}\n\n[↩ Monster list]({back})"
    batches=[];text=''
    for line in lines:
        if len(text)+len(line)>2900: batches.append(text);text=''
        text+=line+'\n'
    if text: batches.append(text)
    pages=[[card('👾 '+name,base+'\n\n**🗺️ Spawn maps**\n'+batches[0],monster['thumbnail'],data['source'])]]
    for extra in batches[1:]: pages.append([card('🗺️ '+name+' — Spawn maps',extra)])
    items={str(i['id']):i for i in data['items']}
    drops=data['drops'].get(mid,[])
    if not drops:
        pages.append([card('🎁 '+name+' — Drops','The source has not provided drop information for this monster.')])
    else:
        for offset in range(0,len(drops),8):
            cards=[card('🎁 '+name+' — Drops',f"Source: MSCW Guidebook · Export: {data['exported_at'][:10]}\nTest-version records; current-game drops may differ.\n[↩ Monster list]({back})")]
            for drop in drops[offset:offset+8]:
                item=items[str(drop['itemId'])]
                source={'1测':'Test 1','2测':'Test 2'}.get(drop.get('source'),drop.get('source') or 'Unspecified')
                cards.append(card(item['name'],f"{item.get('category','Item')} · Source: {source}",item['thumbnail']))
            pages.append(cards)
    return pages
