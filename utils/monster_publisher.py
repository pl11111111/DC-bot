"""Resumable publication used only by the standalone server script."""
import hashlib
import logging
import discord
import config
from utils import new_store as db, monster_guide as guide

log=logging.getLogger(__name__)
cfg=config.NEW

class MonsterGuidePublisher:
    def __init__(self,bot):
        self.bot=bot

    async def thread(self,forum,slot,title,tags):
        key=f'monster_thread:{forum.id}:{slot}'
        saved=await db.setting(key)
        if saved:
            if not saved.get('id'): raise ValueError('上次建帖结果未确认，请管理员核对论坛，勿重复创建。')
            thread=self.bot.get_channel(saved['id']) or await self.bot.fetch_channel(saved['id'])
            if thread.parent_id!=forum.id or thread.owner_id!=self.bot.user.id: raise ValueError('图鉴帖子归属不符')
            if thread.archived: await thread.edit(archived=False)
            return thread
        await db.setting(key,{'phase':'creating'})
        result=await forum.create_thread(name=title,content='📖 Guide publication in progress. Please check back shortly.',applied_tags=tags,
                                         allowed_mentions=discord.AllowedMentions.none())
        thread=result.thread if hasattr(result,'thread') else result
        await db.setting(key,{'phase':'ready','id':thread.id})
        return thread

    async def message(self,thread,slot,cards,starter=False):
        key=f'monster_msg:{thread.id}:{slot}'
        saved=await db.setting(key)
        kwargs=guide.payload(cards)
        try:
            if starter or saved and saved.get('id'):
                message=await thread.fetch_message(thread.id if starter else saved['id'])
                if message.author.id!=self.bot.user.id: raise ValueError('图鉴消息不属于 bot')
                await message.edit(content=None,attachments=[],**kwargs)
            else:
                if saved: raise ValueError('上次发消息结果未确认，请管理员核对，勿重复发布。')
                await db.setting(key,{'phase':'sending'})
                nonce=str(int(hashlib.sha256(key.encode()).hexdigest()[:15],16))
                message=await thread.send(nonce=nonce,**kwargs)
            await db.setting(key,{'id':message.id,'phase':'ready'})
            return message
        finally:
            for file in kwargs['files']: file.close()

    async def publish(self,forum,tags,data,grouped):
        index=await self.thread(forum,'index','📖 Monster Guide',tags)
        directory=[]
        for number,(label,monsters) in enumerate(grouped):
            log.info("Publishing level band %s (%s monsters)",label,len(monsters))
            thread=await self.thread(forum,str(number),'👾 Monsters · Lv. '+label,tags)
            back=f'https://discord.com/channels/{forum.guild.id}/{thread.id}/{thread.id}'
            links={}
            for monster in monsters:
                for page,cards in enumerate(guide.detail(data,monster,back)):
                    message=await self.message(thread,f"detail:{monster['id']}:{page}",cards)
                    if page==0: links[monster['id']]=message.jump_url
            list_pages=[]
            for start in range(0,len(monsters),8):
                cards=[]
                for monster in monsters[start:start+8]:
                    body=f"**Lv. {monster['level']}** · ❤️ HP {monster['hp']:,} · ✨ EXP {monster['exp']:,}"
                    cards.append(guide.card(monster['name'],body,monster['thumbnail'],links[monster['id']]))
                list_pages.append(cards)
            page_links=[]
            for page,cards in enumerate(list_pages):
                message=await self.message(thread,f'list:{page}',cards)
                page_links.append(f'[Page {page+1}]({message.jump_url})')
            body='Click a monster name to open its details.\n\n'+' · '.join(page_links)+f'\n\n[↩ Level directory](https://discord.com/channels/{forum.guild.id}/{index.id}/{index.id})'
            await self.message(thread,'header',[guide.card('👾 Monsters · Lv. '+label,body,monsters[0]['thumbnail'])],starter=True)
            directory.append(guide.card('Lv. '+label,f'[Browse {len(monsters)} monsters]({back})',monsters[0]['thumbnail'],back))
        directory.insert(0,guide.card('📖 Monster Guide',f"Choose a level range below.\nSource: [MSCW Guidebook]({data['source']}) · Data exported {data['exported_at'][:10]}\nGame images belong to their respective owners."))
        await self.message(index,'header',directory,starter=True)
