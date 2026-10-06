"""Run on the server: python publish_monster_guide.py --publish.

Without --publish, validates local data and prints a preview without connecting.
Does not load bot extensions, register commands or run payment workers.
"""
import argparse
import asyncio
import logging
import discord
import config
from utils import monster_guide as guide, new_store as db
from utils.monster_publisher import MonsterGuidePublisher
from utils.security import RedactingFormatter

async def publish(data):
    intents=discord.Intents.none()
    intents.guilds=True
    client=discord.Client(intents=intents,allowed_mentions=discord.AllowedMentions.none())
    started=False
    failures=[]

    @client.event
    async def on_ready():
        nonlocal started
        if started: return
        started=True
        try:
            forum=client.get_channel(config.NEW.GUIDES_CHANNEL_ID) or await client.fetch_channel(config.NEW.GUIDES_CHANNEL_ID)
            if not isinstance(forum,discord.ForumChannel) or forum.guild.id!=config.NEW.GUILD_ID:
                raise ValueError('指南频道必须是新社群的论坛频道')
            tags=[t for t in forum.available_tags if t.id==config.NEW.GUIDES_TAG_ID]
            if (config.NEW.GUIDES_TAG_ID or forum.requires_tag) and not tags:
                raise ValueError('请配置有效的 NEW_GUIDES_TAG_ID')
            pool=await db.pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    key=f'monster_guide:{forum.id}'
                    await cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(key,))
                    if (await cur.fetchone())['acquired']!=1:
                        raise ValueError('另一个图鉴发布进程正在运行')
                    try:
                        await MonsterGuidePublisher(client).publish(forum,tags,data,guide.groups(data))
                        await db.audit(client.user.id,'monster_guide_publish',{'forum':forum.id,'via':'server_script','monsters':len(data['monsters'])})
                    finally:
                        await cur.execute('SELECT RELEASE_LOCK(%s)',(key,))
            print('图鉴发布完成。脚本即将退出。',flush=True)
        except Exception as exc:
            failures.append(exc)
            logging.exception('图鉴发布未完成；已保存进度，不会自动重发结果不明确的消息')
        finally:
            await client.close()

    try:
        await client.start(config.DISCORD_TOKEN)
    finally:
        if not client.is_closed(): await client.close()
        for pool in db._pools.values():
            pool.close()
            await pool.wait_closed()
    if failures: raise SystemExit(1)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish',action='store_true',help='发布或更新图鉴；省略则只校验本地素材')
    args=parser.parse_args()
    handler=logging.StreamHandler()
    handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.INFO,handlers=[handler])
    data=guide.load()
    for item in data['monsters']+data['items']: guide.image_path(item['thumbnail'])
    for monster in data['monsters']:
        for cards in guide.detail(data,monster,'https://discord.com/channels/1/2/3'):
            payload=guide.payload(cards)
            for file in payload['files']: file.close()
    print(f'目标指南论坛：{config.NEW.GUIDES_CHANNEL_ID}',flush=True)
    for label,monsters in guide.groups(data): print(f'Lv. {label}: {len(monsters)} monsters',flush=True)
    if not args.publish:
        print('本地校验通过。实际发布请添加 --publish；本次未连接 Discord。')
        return
    asyncio.run(publish(data))

if __name__=='__main__': main()
