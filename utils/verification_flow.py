"""Private, owner-bound verification and version-matched rule translations."""
import asyncio
import json
from pathlib import Path
import discord
import config
from utils import new_store as db
from utils.verification_text import text, ROWS
from utils import community_private as cp

cfg=config.NEW
LOCALES={'zh':'中文','id':'Bahasa Indonesia','ja':'日本語','ko':'한국어',
         'pt':'Português','es':'Español','th':'ภาษาไทย','vi':'Tiếng Việt',
         'ms':'Bahasa Melayu','fil':'Tagalog'}

def client_language(locale):
    return LOCALES.get(str(getattr(locale,'value',locale)).split('-')[0],'English')

def languages():
    return ['English']+[name for name,role in cfg.LANGUAGES.items() if role]

def role_error(role,guild,setting):
    prefix=f'{setting} (role ID: {getattr(role,"id",None)}): '
    if role is None: return prefix+'role not found. Ask an administrator to check the configured role ID.'
    if role.is_default(): return prefix+'@everyone cannot be assigned as a verification or language role.'
    if role.managed: return prefix+'this role is managed by an integration and cannot be assigned by this bot.'
    if role.id in set(cfg.TRADE_ADMIN_ROLES+cfg.NOTICE_ADMIN_ROLES):
        return prefix+'this is a configured administrator role and cannot be self-assigned.'
    if role.permissions.administrator or role.permissions.manage_roles:
        return prefix+'this role has Administrator or Manage Roles permission and cannot be self-assigned.'
    if not guild.me: return prefix+'bot membership information is unavailable. Please retry.'
    if not guild.me.guild_permissions.manage_roles: return prefix+'the bot needs Manage Roles permission.'
    if not role<guild.me.top_role: return prefix+'move the bot role above this role in Server Settings > Roles.'
    return prefix+'role cannot be safely assigned. Contact an administrator.'

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
        if not custom.get('verification'): raise ValueError('Verification is disabled on this panel.')
        panel=await db.setting('panel:channel:'+str(channel_id))
        # A saved edit may still refer to the original verification message.
        if not panel and channel_id==cfg.VERIFY_CHANNEL_ID:
            panel=await db.setting('panel:verify')
        content=custom
    else:
        if channel_id!=cfg.VERIFY_CHANNEL_ID: raise ValueError('Please use the verification channel.')
        panel=await db.setting('panel:verify')
        content=await db.setting('community_content:verify')
        if not content:
            raw=texts()
            content={'title':raw.get('verify_title','Community Rules'), 'body':raw.get('verify_body','')}
    if not panel or panel['message']!=message_id:
        raise ValueError('This panel has changed. Please use the latest verification panel.')
    return {'title':content['title'],'body':content['body']}

def translated(original,language):
    if language=='English': return original,False
    path=Path(__file__).resolve().parent.parent/'config'/'verify_translations.json'
    records=json.loads(path.read_text(encoding='utf-8'))
    article_path=path.with_name('article_translations.json')
    if article_path.exists(): records+=json.loads(article_path.read_text(encoding='utf-8'))
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
        self.ui_language=language if translate else client_language(getattr(inter,'locale',None))
        self.translate=translate
        self.stage='rules' if translate else 'language'
        self.done=False
        self.translation=None
        self.lock=asyncio.Lock()
        self.build()

    def build(self):
        self.clear_items()
        ui=text(self.ui_language)
        if self.stage=='language' or self.translate:
            menu=discord.ui.Select(placeholder=ui['choose'],options=[
                discord.SelectOption(label=name,value=name,default=name==self.language) for name in (list(ROWS) if self.translate else languages())])
            async def choose(inter):
                self.language=menu.values[0]
                self.ui_language=self.language
                await db.setting('verify_language:'+str(self.owner),self.language)
            self.control(menu,choose)
        if not self.translate:
            if self.stage=='language':
                button=discord.ui.Button(label=ui['next'],emoji='➡️')
                async def next_step(inter):
                    await db.setting('verify_language:'+str(self.owner),self.language)
                    self.stage='rules'
                self.control(button,next_step)
            else:
                back=discord.ui.Button(label=ui['back'],emoji='⬅️')
                async def go_back(inter): self.stage='language'
                self.control(back,go_back)
                agree=discord.ui.Button(label=ui['agree'],emoji='✅',style=discord.ButtonStyle.success)
                self.control(agree,self.agree)

    def control(self,item,action):
        async def callback(inter):
            if not inter.guild or inter.guild.id!=cfg.GUILD_ID or inter.user.id!=self.owner:
                return await inter.response.send_message(cp.text(client_language(getattr(inter,'locale',None)),'owner'),ephemeral=True)
            await inter.response.defer()
            async with self.lock:
                try:
                    if self.done: return
                    if await source(self.channel_id,self.message_id)!=self.original:
                        raise ValueError('The rules have changed. Please reopen verification and read the latest rules.')
                    await action(inter)
                    await self.refresh_translation()
                    self.build()
                    if self.done: self.clear_items()
                    await inter.edit_original_response(embed=self.render(),view=self,allowed_mentions=discord.AllowedMentions.none())
                except Exception as exc:
                    import logging
                    logger=logging.getLogger(__name__)
                    if isinstance(exc,ValueError): logger.warning('Verification blocked: user=%s reason=%s',self.owner,exc)
                    else: logger.exception('Verification interaction failed')
                    await inter.followup.send(cp.text(self.ui_language,'stale' if str(exc).startswith('The rules have changed.') else 'error'),ephemeral=True)
        item.callback=callback
        self.add_item(item)

    async def refresh_translation(self):
        if self.stage=='rules' and not self.done:
            from utils.translation_store import resolve
            self.translation=await resolve(self.message_id,self.original,self.language)

    def render(self):
        ui=text(self.ui_language)
        if self.done:
            return discord.Embed(title='✅ '+ui['done'],description=self.language,color=0x9854DE)
        if self.stage=='language':
            return discord.Embed(title='🌐 '+ui['choose'],description=f"**{self.language}**\n\n{ui['intro']}",color=0x9854DE)
        article,fallback=self.translation if self.translation is not None else translated(self.original,self.language)
        prefix='⚠️ '+ui['fallback']+'\n\n' if fallback else ''
        return discord.Embed(title=article['title'],description=prefix+article['body'],color=0x9854DE).set_footer(
            text=f"{self.language} · "+ui['only' if self.translate else 'read'])

    async def agree(self,inter):
        from modules.new_community import safe_self_role
        if self.translate or self.stage!='rules': raise ValueError('Please read the rules first.')
        async with self.cog.role_locks.setdefault(self.owner,asyncio.Lock()):
            member=await inter.guild.fetch_member(self.owner)
            verified=inter.guild.get_role(cfg.VERIFIED_ROLE_ID)
            existing_levels={r.id for r in member.roles}&cfg.level_role_ids()
            needs_lv1=not existing_levels
            target_id=cfg.LANGUAGES.get(self.language) if self.language!='English' else None
            target=inter.guild.get_role(target_id) if target_id else None
            if not inter.guild.me or not inter.guild.me.guild_permissions.manage_roles:
                raise ValueError('The bot needs Manage Roles permission. Please contact an administrator.')
            if needs_lv1 and not safe_self_role(verified,inter.guild): raise ValueError(role_error(verified,inter.guild,'NEW_INVITE_VERIFIED_ROLE_ID'))
            if self.language!='English' and (not safe_self_role(target,inter.guild) or target.id in cfg.level_role_ids()):
                raise ValueError(role_error(target,inter.guild,'language role') if not target or target.id not in cfg.level_role_ids() else 'Language roles must differ from LV1–LV5 roles.')
            old=[r for r in member.roles if r.id in set(cfg.LANGUAGES.values()) and r.id not in cfg.level_role_ids() and (not target or r.id!=target.id)]
            if old: await member.remove_roles(*old,reason='Verification language selection')
            try:
                if target: await member.add_roles(target,reason='Verification language selection')
                if needs_lv1: await member.add_roles(verified,reason='Accepted current community rules')
            except Exception:
                if target and all(r.id!=target.id for r in member.roles):
                    await member.remove_roles(target,reason='Rollback failed verification language')
                if old: await member.add_roles(*old,reason='Restore language after failed verification')
                raise
            await self.cog.verify_count(self.owner)
            await db.audit(self.owner,'verified',{'role':cfg.VERIFIED_ROLE_ID,'lv1_granted':needs_lv1,'retained_level_roles':sorted(existing_levels),'language':self.language,'rules':self.original})
            self.done=True

async def start(cog,inter,translate=False):
    original=await source(inter.channel_id,inter.message.id)
    # The public verification entry follows this click's client locale, even if
    # a previous visit saved a preference. Reading translations needs no role.
    language=client_language(getattr(inter,'locale',None))
    if language not in (ROWS if translate else languages()): language='English'
    view=VerificationView(cog,inter,original,language,translate)
    await view.refresh_translation()
    await inter.followup.send(embed=view.render(),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
