"""Export source text or atomically import a reviewed multilingual JSON attachment."""
import asyncio
import io
import json
import logging
import discord
import config
from utils import translation_store as store, article_translation as articles
from utils.translation_editor import authorized, ensure_button
from utils.verification_text import ROWS

log=logging.getLogger(__name__)
MAX_BYTES=2*1024*1024

def decode(data,source,message_id):
    if len(data)>MAX_BYTES: raise ValueError('翻译文件不能超过 2 MB。')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError(f'文件存在重复字段：{key}')
            result[key]=value
        return result
    try: bundle=json.loads(data.decode('utf-8-sig'),object_pairs_hook=unique)
    except (UnicodeError,json.JSONDecodeError): raise ValueError('请上传 UTF-8 格式的 JSON 翻译文件。')
    if not isinstance(bundle,dict) or bundle.get('version')!=1:
        raise ValueError('翻译文件格式不正确，需要 version: 1。')
    if bundle.get('source')!=source: raise ValueError('文件原文与当前消息不一致，请重新导出并翻译。')
    if bundle.get('message_id') is not None and str(bundle['message_id'])!=str(message_id):
        raise ValueError('此翻译文件属于另一条消息。')
    translations=bundle.get('translations')
    if not isinstance(translations,dict) or not translations or len(translations)>len(ROWS)-1:
        raise ValueError('文件需要包含至少一种非 English 语言的译文。')
    for language,content in translations.items():
        if language not in ROWS or language=='English': raise ValueError(f'不支持的译文语言：{language}')
        if not isinstance(content,dict): raise ValueError(f'{language} 译文格式不正确。')
        for field,limit in [('title',200),('body',30000 if len(source['body'])>3500 else 3500)]:
            value=content.get(field)
            if not isinstance(value,str) or not value.strip() or len(value)>limit:
                raise ValueError(f'{language} 的 {field} 必须填写，最多 {limit} 字。')
    return {language:{'title':value['title'],'body':value['body']} for language,value in translations.items()}

async def run(cog,ctx,message_link,file=None):
    await ctx.defer(ephemeral=True)
    try:
        channel_id,message_id=store.parse_link(message_link,config.NEW.GUILD_ID)
        channel=await cog.channel(channel_id)
        message=channel.get_partial_message(message_id)
        source=await articles.read(message,cog.bot.user.id)
        if file is None:
            # Export the live source verbatim so whitespace/version checks are reliable.
            translations={}
            for language in ROWS:
                if language=='English': continue
                content,missing=await store.resolve(message_id,source,language)
                translations[language]={'title':'','body':''} if missing else content
            bundle=dict(version=1,message_id=str(message_id),source=source,translations=translations)
            data=json.dumps(bundle,ensure_ascii=False,indent=2).encode('utf-8')
            return await ctx.followup.send('原文及现有有效译文已导出。把文件交给我补全翻译，再用同一指令上传；未翻译的语言请从文件中删除。',
                file=discord.File(io.BytesIO(data),filename=f'translations-{message_id}.json'),ephemeral=True)
        if file.size>MAX_BYTES: raise ValueError('翻译文件不能超过 2 MB。')
        translations=decode(await file.read(),source,message_id)
        revisions={}
        lines=[]
        for language,content in translations.items():
            previous=await store.lookup(message_id,language)
            revisions[language]=(previous or {}).get('revision')
            lines.append(f'• {language}：'+('替换已有译文' if previous else '新增译文')+f'，正文 {len(content["body"])} 字')
        view=discord.ui.View(timeout=300)
        button=discord.ui.Button(label=f'确认导入 {len(translations)} 种语言',emoji='✅',style=discord.ButtonStyle.success)
        lock=asyncio.Lock()
        done=False
        async def confirmed(inter):
            nonlocal done
            await inter.response.defer(ephemeral=True)
            if not await authorized(inter,ctx.author.id):
                return await inter.followup.send('没有操作权限。',ephemeral=True)
            async with lock:
                if done: return await inter.followup.send('本次译文已导入，请勿重复提交。',ephemeral=True)
                try:
                    async with cog.panel_lock:
                        if await articles.read(message,cog.bot.user.id)!=source:
                            raise ValueError('原文已修改，本次未导入，请重新导出并翻译。')
                        await store.save_batch(message_id,channel_id,source,translations,ctx.author.id,revisions)
                        done=True
                        try: await ensure_button(message,cog.bot.user.id)
                        except Exception:
                            log.exception('Batch saved but Translate button update failed: %s',message_id)
                            return await inter.followup.send('全部译文已保存，但添加 Translate 按钮失败，请检查 bot 消息权限。',ephemeral=True)
                    await inter.followup.send(f'已导入 {len(translations)} 种语言，Translate 立即生效。',ephemeral=True)
                except Exception as exc:
                    log.exception('Translation batch save failed: %s',message_id)
                    await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '保存结果需核对，请不带文件重新执行指令导出查看。',ephemeral=True)
        button.callback=confirmed
        view.add_item(button)
        # The reviewed file contains exactly what will be committed, without executable content.
        preview=json.dumps(dict(version=1,message_id=str(message_id),source=source,translations=translations),ensure_ascii=False,indent=2).encode('utf-8')
        await ctx.followup.send('待导入消息：'+message_link+'\n'+'\n'.join(lines)+'\n未包含的语言保持不变。请核对附件中的全部译文后确认。',
            file=discord.File(io.BytesIO(preview),filename='translation-preview.json'),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.exception('Translation batch preparation failed')
        await ctx.followup.send(str(exc) if isinstance(exc,ValueError) else '无法读取消息或文件，请检查消息链接、文件及 bot 权限。',ephemeral=True)
