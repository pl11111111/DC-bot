import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
import discord
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_trading import NewTrading,OrderStateChanged


class PayoutConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self):
        cog=object.__new__(NewTrading)
        row=dict(id='order',status='receipt_confirmed',buyer_id=1,seller_id=2,amount=D('3.01'))
        inter=NS(response=NS(send_modal=AsyncMock()))
        await cog.address_modal(inter,row)
        modal=inter.response.send_modal.await_args.args[0]
        modal.children[0]._input_value='0x1234567890123456789012345678901234567890'
        click=NS(response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch('modules.new_trading.payments.payout_quote',AsyncMock(return_value=(D('.01'),D('3')))):
            await modal.callback(click)
        return cog,row,click.followup.send.await_args.kwargs['view'].children[0]

    def confirm(self):
        return NS(user=NS(id=2),response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()),message=NS(edit=AsyncMock()),edit_original_response=AsyncMock())

    async def test_stale_confirmation_reads_progress_without_quote_or_transfer(self):
        cog,row,button=await self.prepare()
        cog.order=AsyncMock(return_value={**row,'status':'releasing'})
        cog.transition=AsyncMock()
        inter=self.confirm()
        with patch('modules.new_trading.db.query',AsyncMock(return_value={'state':'submitted'})), patch('modules.new_trading.payments.release',AsyncMock()) as release, patch('modules.new_trading.payments.payout_quote',AsyncMock()) as quote:
            await button.callback(inter)
            quote.assert_not_awaited()
            release.assert_not_awaited()
            cog.transition.assert_not_awaited()
        inter.edit_original_response.assert_awaited_once_with(view=None)
        inter.message.edit.assert_not_awaited()
        self.assertIn('不要重复提交',inter.followup.send.await_args.args[0])

    async def test_missing_ephemeral_confirmation_does_not_block_processing(self):
        cog=object.__new__(NewTrading)
        inter=self.confirm()
        inter.edit_original_response.side_effect=discord.NotFound(NS(status=404,reason='Not Found'),{'code':10008,'message':'Unknown Message'})
        await cog.retire_payout_confirmation(inter)
        inter.message.edit.assert_not_awaited()

    async def test_compare_and_swap_loser_does_not_submit_transfer(self):
        cog,row,button=await self.prepare()
        cog.order=AsyncMock(side_effect=[row,{**row,'status':'releasing'}])
        cog.transition=AsyncMock(side_effect=OrderStateChanged())
        inter=self.confirm()
        with patch('modules.new_trading.db.query',AsyncMock(return_value=None)), patch('modules.new_trading.payments.release',AsyncMock()) as release, patch('modules.new_trading.payments.payout_quote',AsyncMock(return_value=(D('.01'),D('3')))):
            await button.callback(inter)
            release.assert_not_awaited()
        self.assertIn('不要重复提交',inter.followup.send.await_args.args[0])
