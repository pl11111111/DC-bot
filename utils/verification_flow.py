"""Private, owner-bound verification and version-matched rule translations."""
import asyncio
import json
from pathlib import Path
import discord
import config
from utils import new_store as db

cfg=config.NEW
LOCALES={'zh':'中文','id':'Bahasa Indonesia','ja':'日本語','ko':'한국어',
         'pt':'Português','es':'Español','th':'ภาษาไทย','vi':'Tiếng Việt',
         'ms':'Bahasa Melayu','fil':'Tagalog'}

def languages():
    return ['English']+[name for name,role in cfg.LANGUAGES.items() if role]

def preferred(member,locale,saved=None):
    roles={r.id for r in member.roles}
    chosen=next((name for name,rid in cfg.LANGUAGES.items() if rid and rid in roles),None)
    locale=str(getattr(locale,'value',locale)).split('-')[0]
    return chosen or (saved if saved in languages() else None) or (
        LOCALES.get(locale) if LOCALES.get(locale) in languages() else 'English')

async def source(channel_id,message_id):
    from modules.new_community import texts
    custom=await db.setting('community_content:channel:'+str(channel_id))
    if custom:
        if not custom.get('verification'): raise ValueError('此面板已停用验证。')
        panel=await db.setting('panel:channel:'+str(channel_id))
        # A saved edit may still refer to the original verification message.
        if not panel and channel_id==cfg.VERIFY_CHANNEL_ID:
            panel=await db.setting('panel:verify')
        content=custom
    else:
        if channel_id!=cfg.VERIFY_CHANNEL_ID: raise ValueError('请使用验证频道的入口。')
        panel=await db.setting('panel:verify')
        content=await db.setting('community_content:verify')
        if not content:
            raw=texts()
            content={'title':raw.get('verify_title','Community Rules'), 'body':raw.get('verify_body','')}
    if not panel or panel['message']!=message_id:
        raise ValueError('验证入口已更新，请重新点击最新入口。')
    return {'title':content['title'],'body':content['body']}

def translated(original,language):
    if language=='English': return original,False
    path=Path(__file__).resolve().parent.parent/'config'/'verify_translations.json'
    records=json.loads(path.read_text(encoding='utf-8'))
    for item in records:
        if item.get('source')==original:
            content=item.get('translations',{}).get(language)
            if content and content.get('title') and content.get('body'):
                if len(content['body'])<=3500 and len(content['title'])<=200:
                    return content,False
    return original,True

class VerificationView(discord.ui.View):
    def __init__(self,cog,inter,original,language,translate=False):
        super().__init__(timeout=600)
        self.cog=cog
        self.owner=inter.user.id
        self.channel_id=inter.channel_id
        self.message_id=inter.message.id
        self.original=original
        self.language=language
        self.translate=translate
        self.stage='rules' if translate else 'language'
        self.done=False
        self.lock=asyncio.Lock()
        self.build()

    def build(self):
        self.clear_items()
        if self.stage=='language' or self.translate:
            menu=discord.ui.Select(placeholder='Choose your language / 选择语言',options=[
                discord.SelectOption(label=name,value=name,default=name==self.language) for name in languages()])
            async def choose(inter):
                self.language=menu.values[0]
                await db.setting('verify_language:'+str(self.owner),self.language)
            self.control(menu,choose)
        if not self.translate:
            if self.stage=='language':
                button=discord.ui.Button(label='Next / 下一步',emoji='➡️')
                async def next_step(inter):
                    await db.setting('verify_language:'+str(self.owner),self.language)
                    self.stage='rules'
                self.control(button,next_step)
            else:
                back=discord.ui.Button(label='Back / 返回选择语言',emoji='⬅️')
                async def go_back(inter): self.stage='language'
                self.control(back,go_back)
                agree=discord.ui.Button(label='I agree / 我已阅读并同意',emoji='✅',style=discord.ButtonStyle.success)
                self.control(agree,self.agree)

    def control(self,item,action):
        async def callback(inter):
            if not inter.guild or inter.guild.id!=cfg.GUILD_ID or inter.user.id!=self.owner:
                return await inter.response.send_message('此验证界面仅供发起者使用。',ephemeral=True)
            await inter.response.defer()
            async with self.lock:
                try:
                    if self.done: return
                    if await source(self.channel_id,self.message_id)!=self.original:
                        raise ValueError('规则已更新，请重新点击公共入口阅读最新规则。')
                    await action(inter)
                    self.build()
                    if self.done: self.clear_items()
                    await inter.edit_original_response(embed=self.render(),view=self,allowed_mentions=discord.AllowedMentions.none())
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).exception('Verification interaction failed')
                    await inter.followup.send(str(exc) if isinstance(exc,ValueError) else '操作未完成，请重试或联系管理员。',ephemeral=True)
        item.callback=callback
        self.add_item(item)

    def render(self):
        if self.done:
            return discord.Embed(title='✅ Verified / 验证完成',description=f'Language / 语言：{self.language}',color=0x9854DE)
        if self.stage=='language':
            return discord.Embed(title='🌐 Step 1 / 选择语言',description=f'Language / 当前语言：**{self.language}**\n确认语言后点击 Next，阅读并同意规则后才会完成验证。\nChoose Next to read the rules. Roles are granted only after you agree.',color=0x9854DE)
        article,fallback=translated(self.original,self.language)
        prefix='⚠️ 此语言的最新译文尚未配置，以下为公共原文。\nTranslation unavailable; showing the original text.\n\n' if fallback else ''
        return discord.Embed(title=article['title'],description=prefix+article['body'],color=0x9854DE).set_footer(
            text=f'{self.language} · '+('Translation only / 仅翻译，不授予身份组' if self.translate else 'Step 2 / 阅读规则后点击同意'))

    async def agree(self,inter):
        from modules.new_community import safe_self_role
        if self.translate or self.stage!='rules': raise ValueError('请先阅读规则。')
        async with self.cog.role_locks.setdefault(self.owner,asyncio.Lock()):
            member=await inter.guild.fetch_member(self.owner)
            verified=inter.guild.get_role(cfg.VERIFIED_ROLE_ID)
            target_id=cfg.LANGUAGES.get(self.language) if self.language!='English' else None
            target=inter.guild.get_role(target_id) if target_id else None
            if not safe_self_role(verified,inter.guild): raise ValueError('验证身份组配置或层级不正确。')
            if self.language!='English' and (not safe_self_role(target,inter.guild) or target.id==verified.id):
                raise ValueError('语言身份组配置或层级不正确。')
            old=[r for r in member.roles if r.id in set(cfg.LANGUAGES.values()) and r.id!=cfg.VERIFIED_ROLE_ID and (not target or r.id!=target.id)]
            if old: await member.remove_roles(*old,reason='Verification language selection')
            try:
                if target: await member.add_roles(target,reason='Verification language selection')
                await member.add_roles(verified,reason='Accepted current community rules')
            except Exception:
                if target and all(r.id!=target.id for r in member.roles):
                    await member.remove_roles(target,reason='Rollback failed verification language')
                if old: await member.add_roles(*old,reason='Restore language after failed verification')
                raise
            await self.cog.verify_count(self.owner)
            await db.audit(self.owner,'verified',{'role':verified.id,'language':self.language,'rules':self.original})
            self.done=True

async def start(cog,inter,translate=False):
    original=await source(inter.channel_id,inter.message.id)
    saved=await db.setting('verify_language:'+str(inter.user.id))
    member=await inter.guild.fetch_member(inter.user.id)
    language=preferred(member,inter.locale,saved)
    view=VerificationView(cog,inter,original,language,translate)
    await inter.followup.send(embed=view.render(),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
