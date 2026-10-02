"""Exercise permission boundaries with mocked side effects, never live services."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',
                  BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.admin import Admin
from modules.social import Social


def context(role_ids=(1,)):
    guild=NS(id=1,owner_id=99)
    member=NS(id=10,name='caller',guild=guild,roles=[NS(id=i) for i in role_ids],
              guild_permissions=NS(administrator=False))
    return NS(guild=guild,author=member,respond=AsyncMock(),defer=AsyncMock(),
              followup=NS(send=AsyncMock()))


def interaction(member,guild):
    return NS(user=member,guild=guild,response=NS(send_message=AsyncMock(),defer=AsyncMock()),
              followup=NS(send=AsyncMock()))


class LegacyPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_everyone_role_cannot_authorize_credit_refund(self):
        cog=object.__new__(Admin); ctx=context()
        with patch('config.ADMIN_ROLE_ID',1),patch('utils.giveaway_credits.settle',AsyncMock(return_value={'count':1,'total':5})) as settle:
            await Admin.refund_giveaway_credits_standalone.callback(cog,ctx,'123')
        settle.assert_not_awaited()
        ctx.respond.assert_awaited_once()

    async def test_everyone_role_cannot_close_or_reroll_giveaway(self):
        cog=object.__new__(Admin)
        with patch('config.ADMIN_ROLE_ID',1),patch('utils.database.get_giveaway',AsyncMock(return_value=None)) as query:
            await Admin.close_giveaway.callback(cog,context(),'123')
            await Admin.reroll.callback(cog,context(),'123')
        query.assert_not_awaited()

    async def test_everyone_role_cannot_authorize_market_deletion(self):
        cog=object.__new__(Admin)
        with patch('config.BOT_ADMIN_ROLE',1),patch('utils.database.get_currency_trades_by_type',AsyncMock(return_value=[])) as query:
            await Admin.delete_currency_trade_cmd.callback(cog,context(),'NXPC','sell')
        query.assert_not_awaited()

    async def test_everyone_role_cannot_trigger_boost_credit_grants(self):
        cog=object.__new__(Social)
        cog.sync_server_boosters=AsyncMock(); cog.update_booster_rewards_once=AsyncMock()
        with patch('config.ADMIN_ROLE_ID',1),patch('config.MODERATOR_ROLE_ID',1):
            await Social.check_and_grant_boost_credits.callback(cog,context())
        cog.sync_server_boosters.assert_not_awaited()
        cog.update_booster_rewards_once.assert_not_awaited()

    async def batch_button(self):
        cog=object.__new__(Admin); cog.bot=NS(get_cog=lambda _:None)
        ctx=context((1,50))
        with patch('config.BOT_ADMIN_ROLE',50),patch('utils.database.get_currency_trades_by_user',AsyncMock(return_value=[{'id':123}])):
            await Admin.delete_user_currency_trades.callback(cog,ctx,NS(id=20,display_name='target'))
        return ctx,ctx.respond.await_args.kwargs['view'].children[0]

    async def test_revoked_admin_cannot_use_existing_delete_confirmation(self):
        ctx,button=await self.batch_button()
        ctx.author.roles=[NS(id=1)]
        with patch('config.BOT_ADMIN_ROLE',50),patch('utils.database.admin_delete_currency_trade',AsyncMock()) as delete:
            await button.callback(interaction(ctx.author,ctx.guild))
        delete.assert_not_awaited()

    async def test_admin_confirmation_rejects_other_guild_and_other_user(self):
        ctx,button=await self.batch_button()
        other=context((1,50)).author; other.id=11
        with patch('config.BOT_ADMIN_ROLE',50),patch('utils.database.admin_delete_currency_trade',AsyncMock()) as delete:
            await button.callback(interaction(ctx.author,NS(id=2)))
            await button.callback(interaction(other,ctx.guild))
        delete.assert_not_awaited()

    async def test_current_authorized_admin_can_confirm(self):
        ctx,button=await self.batch_button()
        with patch('config.BOT_ADMIN_ROLE',50),patch('utils.database.admin_delete_currency_trade',AsyncMock(return_value=True)) as delete:
            await button.callback(interaction(ctx.author,ctx.guild))
        delete.assert_awaited_once_with(123)

    async def test_owner_can_refund_with_no_custom_admin_role(self):
        cog=object.__new__(Admin); ctx=context(); ctx.author.id=99
        with patch('config.ADMIN_ROLE_ID',1),patch('utils.giveaway_credits.settle',AsyncMock(return_value={'count':1,'total':5})) as settle:
            await Admin.refund_giveaway_credits_standalone.callback(cog,ctx,'123')
        settle.assert_awaited_once_with('123',99,'refunded')
