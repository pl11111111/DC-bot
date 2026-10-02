"""Local text catalog for repaired management and social interfaces.

Only bot-authored templates are translated; supplied names, amounts, addresses,
reasons and other values are inserted afterwards and never treated as templates.
"""
import json
from pathlib import Path
import config
from utils.payout_language import language as member_language

CATALOG = json.loads((Path(__file__).resolve().parents[1]/'config'/'repaired_ui.json').read_text(encoding='utf-8'))
LOCALES = {
    'en-US':'English','en-GB':'English','zh-CN':'中文','zh-TW':'中文','ja':'日本語','ko':'한국어',
    'id':'Bahasa Indonesia','ms':'Bahasa Melayu','fil':'Tagalog','tl':'Tagalog',
    'pt-BR':'Português','pt-PT':'Português','es-ES':'Español','es-419':'Español',
    'th':'ภาษาไทย','vi':'Tiếng Việt',
}
# Only locales supported by Discord are sent in command metadata.
COMMAND_LOCALES={code:lang for code,lang in LOCALES.items() if code not in ('ms','fil','tl','pt-PT')}
DECISIONS=dict(zip(('记录意见','继续履约','允许卖家收款','退还买家','无到账关闭','关闭未付款已取消频道','登记手动退款','暂停关闭','重新通知关闭'),
                   ('note','resume','release','refund','unpaid','cancelled','manual','hold','renotify')))


def decision(subject,value):
    return text(subject,'decision_'+DECISIONS[value])


def localizations(key):
    return {locale:CATALOG[lang][key] for locale,lang in COMMAND_LOCALES.items()}


def command_names(key):
    import unicodedata
    def name(value):
        value=value.lower().replace(' ','-')
        return ''.join(c for c in value if c in '_-' or unicodedata.category(c)[0] in 'LMN')[:32]
    return {locale:name(value) for locale,value in localizations(key).items()}


def choice(value,key):
    import discord
    return discord.OptionChoice(CATALOG['English'][key],value=value,name_localizations=localizations(key))


def decision_choices():
    return [choice(value,'decision_'+key) for value,key in DECISIONS.items()]


def language(subject):
    if isinstance(subject,str) and subject in CATALOG:
        return subject
    # A guild has every language role; those roles are not a member's selection.
    if getattr(subject,'preferred_locale',None) is not None and not getattr(subject,'guild',None):
        return LOCALES.get(str(subject.preferred_locale),'English')
    member=getattr(subject,'user',None) or getattr(subject,'author',None) or subject
    guild=getattr(subject,'guild',None) or getattr(member,'guild',None)
    # Preserve the new community's role selection and English default.
    if guild and getattr(guild,'id',None)==config.NEW.GUILD_ID:
        return member_language(member)
    selected=member_language(member)
    if selected!='English':
        return selected
    locale=getattr(subject,'locale',None) or getattr(getattr(subject,'interaction',None),'locale',None)
    if not locale and guild:
        locale=getattr(guild,'preferred_locale',None)
    if not locale:
        locale=getattr(subject,'preferred_locale',None)
    return LOCALES.get(str(locale),'English')


def text(subject,key,**values):
    lang=language(subject)
    return CATALOG.get(lang,CATALOG['English']).get(key,CATALOG['English'][key]).format(**values)


def error(subject,exc):
    # Internal errors remain in logs. Never put raw DB/API errors into messages.
    import logging
    logging.getLogger(__name__).warning('Operation rejected: %s',exc)
    message=str(exc)
    if message=='原因必须为 1–500 字':
        return text(subject,'reason_required')
    if message in ('退款地址必须为有效 BSC 地址','请输入有效的 BSC 地址'):
        from utils.payout_language import ROWS,KEYS
        return dict(zip(KEYS,ROWS[language(subject)]))['invalid']
    if any(part in message for part in ('确认码','重新预览','最新确认码')):
        return text(subject,'preview_stale')
    return text(subject,'operation_review')


async def participants(row,guild):
    """Use the same participant roles as the existing trade cards."""
    from utils.trade_language import assign
    if guild and callable(getattr(guild,'get_member',None)):
        return await assign(row,guild)
    return dict(row,_languages=['English'],_user_languages={'buyer_id':'English','seller_id':'English'})


def participant_text(row,buyer_key,seller_key,**values):
    import discord
    from utils.trade_language import PACKS
    lines=[]
    for field,key,index,emoji in (('buyer_id',buyer_key,7,'🛒'),('seller_id',seller_key,8,'📦')):
        lang=row['_user_languages'][field]
        lines.append(f"{emoji} {PACKS[lang]['labels'][index]} <@{row[field]}>: {text(lang,key,**values)}")
    mentions=discord.AllowedMentions(users=[discord.Object(id=row[k]) for k in ('buyer_id','seller_id')],roles=False,everyone=False)
    return '\n\n'.join(lines),mentions
