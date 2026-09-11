"""Keep legacy listeners and commands out of the new guild, including button IDs."""
import functools
import config
from discord.ext import commands

LEGACY={'modules.trading','modules.rental','modules.social','modules.admin','modules.market','modules.giveaway','modules.party'}

class IsolatedBot(commands.Bot):
    def add_listener(self,func,name=None):
        if func.__module__ in LEGACY:
            original=func
            @functools.wraps(original)
            async def isolated(*args,**kwargs):
                for arg in args:
                    data=getattr(arg,'data',None)
                    if isinstance(data,dict) and str(data.get('custom_id','')).startswith('new:'):
                        return
                    guild=getattr(arg,'guild',None)
                    if guild and guild.id!=config.GUILD_ID:
                        return
                    if getattr(arg,'id',None)==config.NEW.GUILD_ID and config.NEW.GUILD_ID:
                        return
                return await original(*args,**kwargs)
            func=isolated
        return super().add_listener(func,name)

def command_allowed(ctx):
    guild=getattr(ctx,'guild',None)
    callback=getattr(getattr(ctx,'command',None),'callback',None)
    module=getattr(callback,'__module__','')
    if module in ('modules.trading','modules.rental'):
        return False
    if not config.LEGACY_RANKING_ENABLED and module in ('modules.social','modules.admin'):
        if getattr(callback,'__name__','') in ('update_ranks','update_ranks_slash','my_invites','my_invites_slash'):
            return False
    if module in LEGACY and guild and guild.id!=config.GUILD_ID:
        return module=='modules.trading' and guild.id==config.NEW.GUILD_ID and getattr(callback,'__name__','') in ('trade_callback','trade_sell_callback')
    return True
