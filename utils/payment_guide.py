"""One managed forum guide, with persisted identity and ambiguous-send recovery."""
import hashlib
import logging
from pathlib import Path
import discord
import config
from utils import new_store as db, notice_card

cfg=config.NEW
log=logging.getLogger(__name__)
TITLE='新手付款指南 · USDT / BSC'
SOURCE=Path(__file__).resolve().parents[1]/'config'/'payment_guide.md'


async def url():
    if not cfg.GUILD_ID or not cfg.GUIDES_CHANNEL_ID: return None
    saved=await db.setting('payment_guide:'+str(cfg.GUIDES_CHANNEL_ID))
    if saved and saved.get('phase')=='ready':
        return f"https://discord.com/channels/{cfg.GUILD_ID}/{saved['thread']}/{saved['message']}"
    return f'https://discord.com/channels/{cfg.GUILD_ID}/{cfg.GUIDES_CHANNEL_ID}'


async def find_existing(forum,bot):
    # A timeout may mean Discord created the thread but its ID was not saved.
    # Check both active and archived posts before making any creation decision.
    threads={t.id:t for t in await forum.guild.active_threads()
             if t.parent_id==forum.id and t.owner_id==bot.user.id and t.name==TITLE}
    async for thread in forum.archived_threads(limit=None):
        if thread.owner_id==bot.user.id and thread.name==TITLE: threads[thread.id]=thread
    if len(threads)>1: raise ValueError('存在多个 bot 新手指南帖，请管理员核对，未继续发布')
    if not threads: return None
    thread=next(iter(threads.values()))
    return await thread.fetch_message(thread.id)


async def sync(bot):
    if not cfg.GUILD_ID or not cfg.GUIDES_CHANNEL_ID: return
    forum=bot.get_channel(cfg.GUIDES_CHANNEL_ID) or await bot.fetch_channel(cfg.GUIDES_CHANNEL_ID)
    if not isinstance(forum,discord.ForumChannel) or forum.guild.id!=cfg.GUILD_ID:
        raise ValueError('NEW_GUIDES_CHANNE_ID 必须是新社群的论坛频道 ID')
    body=SOURCE.read_text(encoding='utf-8').strip()
    render=notice_card.layout(TITLE,body)
    digest=hashlib.sha256(body.encode()).hexdigest()
    key='payment_guide:'+str(forum.id)
    saved=await db.setting(key)
    message=None
    if saved and saved.get('phase')=='ready':
        try:
            thread=bot.get_channel(saved['thread']) or await bot.fetch_channel(saved['thread'])
            if thread.parent_id!=forum.id: raise ValueError('已保存的指南不属于配置论坛')
            message=await thread.fetch_message(saved['message'])
        except discord.NotFound:
            saved=None  # Explicitly deleted: safe to find/create a replacement.
    if message is None:
        message=await find_existing(forum,bot)
    if message is not None:
        if message.author.id!=bot.user.id: raise ValueError('指南不是本 bot 发布的消息')
        if not saved or saved.get('hash')!=digest:
            if message.channel.archived: await message.channel.edit(archived=False)
            await notice_card.edit(message,render)
    else:
        if saved and saved.get('phase')=='publishing':
            # Never create a second post just because an ambiguous response is missing.
            log.error('Guide publication outcome unknown; check forum and payment_guide setting: %s',forum.id)
            return
        tags=[t for t in forum.available_tags if t.id==cfg.GUIDES_TAG_ID] if cfg.GUIDES_TAG_ID else []
        if (cfg.GUIDES_TAG_ID and not tags) or (forum.requires_tag and not tags):
            raise ValueError('指南论坛要求标签，请配置有效的 NEW_GUIDES_TAG_ID')
        await db.setting(key,{'phase':'publishing'})
        try:
            message=await notice_card.create_forum(forum,TITLE,render,tags)
        except discord.HTTPException as exc:
            if exc.status in (400,401,403,404,429):
                await db.setting(key,{'phase':'retry'})
            raise
    await db.setting(key,{'phase':'ready','thread':message.channel.id,'message':message.id,'hash':digest})
