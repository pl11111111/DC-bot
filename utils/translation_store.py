"""Message-scoped translations bound to exact source text and an edit revision."""
import hashlib
import json
import re
import uuid
from utils import new_store as db

def parse_link(link,guild_id):
    match=re.fullmatch(r'https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?',link.strip())
    if not match or int(match[1])!=guild_id:
        raise ValueError('请填写本社群的完整消息链接，包括服务器、频道和消息 ID。')
    return int(match[2]),int(match[3])

def fingerprint(source):
    return hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def key(message_id,language):
    return f'article_translation:{message_id}:'+hashlib.sha256(language.encode()).hexdigest()[:16]

async def lookup(message_id,language):
    return await db.setting(key(message_id,language))

async def resolve(message_id,source,language):
    if language=='English': return source,False
    from utils.verification_flow import translated
    record=await lookup(message_id,language)
    if record:
        if record['source_hash']==fingerprint(source):
            return {'title':record['title'],'body':record['body']},False
        # A stale DB translation must not silently revert to an older file version.
        return source,True
    return translated(source,language)

async def save(message_id,channel_id,source,language,title,body,actor,expected_revision):
    if not title.strip() or not body.strip() or len(title)>200 or len(body)>3500:
        raise ValueError('请填写翻译标题与正文，标题最多 200 字，正文最多 3500 字。')
    record=dict(channel_id=channel_id,message_id=message_id,language=language,
        source_hash=fingerprint(source),source=source,title=title,body=body,
        actor=actor,revision=uuid.uuid4().hex)
    setting_key=key(message_id,language)
    async with db.transaction() as cur:
        # Establish a lockable row even on the first concurrent translation save.
        await cur.execute("INSERT INTO settings(setting_key,value) VALUES(%s,'null') ON DUPLICATE KEY UPDATE setting_key=setting_key",(setting_key,))
        await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE',(setting_key,))
        old=json.loads((await cur.fetchone())['value'])
        if (old or {}).get('revision')!=expected_revision:
            raise ValueError('该语言译文已被其他操作更新，请重新打开指令编辑。')
        await cur.execute('UPDATE settings SET value=%s WHERE setting_key=%s',(db.encode(record),setting_key))
        await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,NULL)',
            (actor,'article_translation_saved',db.encode({'before':old,'after':record})))
    return record

async def save_batch(message_id,channel_id,source,translations,actor,revisions):
    """All languages and their audit records commit together or roll back together."""
    records={}
    for language,content in translations.items():
        records[language]=dict(channel_id=channel_id,message_id=message_id,language=language,
            source_hash=fingerprint(source),source=source,title=content['title'],body=content['body'],
            actor=actor,revision=uuid.uuid4().hex)
    async with db.transaction() as cur:
        for language in sorted(records):
            setting_key=key(message_id,language)
            await cur.execute("INSERT INTO settings(setting_key,value) VALUES(%s,'null') ON DUPLICATE KEY UPDATE setting_key=setting_key",(setting_key,))
            await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE',(setting_key,))
            old=json.loads((await cur.fetchone())['value'])
            if (old or {}).get('revision')!=revisions[language]:
                raise ValueError(f'{language} 译文已被其他操作更新，本次全部取消，请重新预览。')
            await cur.execute('UPDATE settings SET value=%s WHERE setting_key=%s',(db.encode(records[language]),setting_key))
            await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,NULL)',
                (actor,'article_translation_saved',db.encode({'before':old,'after':records[language],'batch':True})))
    return records
