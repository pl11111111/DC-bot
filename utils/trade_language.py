"""Shared trade instructions use participant role languages, never client locale."""
import json
from pathlib import Path
import discord
import config

PACKS=json.loads((Path(__file__).resolve().parent.parent/'config'/'trade_translations.json').read_text(encoding='utf-8'))
from utils.trade_buttons import localize_references
PACKS={language:localize_references(pack,language) for language,pack in PACKS.items()}
ALIASES={'payment_timeout':'review','payment_review':'review','disputed':'review',
         'releasing_refund':'releasing','expired':'cancelled','manual_refunded':'refunded','test_closed':'review'}
BUTTONS={'confirm':'Confirm trade','cancel':'Cancel trade','pay':'Get payment information',
         'payment_info':'Payment instructions','payment_help':'Contact admin','ship':'Mark as shipped',
         'receipt':'Confirm receipt','collect':'Claim funds','dispute':'Open dispute','keep':'Keep channel / Contact admin'}

async def assign(row,guild):
    result=dict(row)
    users={}
    for key in ('buyer_id','seller_id'):
        member=guild.get_member(row[key])
        if member is None:
            try: member=await guild.fetch_member(row[key])
            except discord.NotFound: member=None
        roles={r.id for r in member.roles} if member else set()
        users[key]=next((name for name,rid in config.NEW.LANGUAGES.items() if rid and rid in roles and name in PACKS),'English')
    result['_user_languages']=users
    result['_languages']=list(dict.fromkeys(users.values()))
    return result

def instruction(status,language):
    return PACKS.get(language,PACKS['English'])[ALIASES.get(status,status) if ALIASES.get(status,status) in PACKS['English'] else 'review']

def description(row,status=None):
    if (status or row['status'])=='pending': return pending_notice(row)
    if row.get('_user_languages'): return participant_notice(row,status)
    return '\n\n'.join(instruction(status or row['status'],language) for language in dict.fromkeys(row['_languages']))

def participant_notice(row,status=None):
    status=status or row['status']
    sections=[]
    for language in row['_languages']:
        pack=PACKS[language]
        specific=pack.get('participants',{}).get(status,{})
        lines=[]
        for key,lang in row['_user_languages'].items():
            if lang!=language: continue
            buyer=key=='buyer_id'
            role=pack['labels'][7 if buyer else 8]
            message=specific.get('buyer' if buyer else 'seller',instruction(status,language))
            lines.append(f'{"🛒" if buyer else "📦"} {role} <@{row[key]}>：{message}')
        sections.append('\n\n'.join(lines))
    return '\n\n'.join(sections)

def pending_notice(row):
    sections=[]
    for language in row['_languages']:
        lines=[]
        for key,lang in row['_user_languages'].items():
            if lang!=language: continue
            sender=row[key]==row.get('initiator_id',row['buyer_id'])
            role=PACKS[language]['labels'][7 if key=='buyer_id' else 8]
            instruction=PACKS[language]['pending_sender' if sender else 'pending_recipient']
            lines.append(f'{"🛒" if key=="buyer_id" else "📦"} {role} <@{row[key]}>：{instruction}')
        sections.append('\n\n'.join(lines))
    return '\n\n'.join(sections)

def notify(row,status=None):
    mentions=discord.AllowedMentions(users=[discord.Object(id=row[k]) for k in ('buyer_id','seller_id')],roles=False,everyone=False)
    if (status or row['status'])=='pending': return pending_notice(row),mentions
    return participant_notice(row,status),mentions

def localize_embed(embed,row):
    result=embed.copy()
    def label(index): return ' / '.join(PACKS[lang]['labels'][index] for lang in row['_languages'])
    result.title='🤝 '+label(0)
    result.description=description(row)
    labels=dict(zip(('📦 物品','💰 价格','🔒 担保费','🎟️ 积分抵扣','💵 订单合计','💰 卖家货款',
        '🛒 买家','🏪 卖家','附加详情','🕒 创建时间','交易提示','链上必须实际到账','收款地址','付款截止'),range(1,15)))
    for index,field in enumerate(result.fields):
        value=field.value.replace('（转出网络费从中扣除）','')
        if field.name=='付款截止': value=value.split('\n\n')[0]
        emojis={1:'📦',2:'💰',3:'🔒',4:'🎟️',5:'💵',6:'💰',7:'🛒',8:'📦',9:'📝',10:'🕒',11:'📌',12:'💰',13:'📍',14:'⏳'}
        field_id=labels.get(field.name)
        if field_id==11:
            from utils.trade_extra import render
            value=render(value,row['_languages'])
        result.set_field_at(index,name=emojis[field_id]+' '+label(field_id) if field_id else field.name,value=value,inline=field.inline)
    result.set_footer(text=label(15)+' '+row['id'])
    return result
