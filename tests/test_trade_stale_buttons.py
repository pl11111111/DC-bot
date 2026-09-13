import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from modules.new_trading import NewTrading,OrderStateChanged


class StaleButtonTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return NS(data={'custom_id':'new:trade:confirm:order'},guild=NS(id=2),channel_id=8,user=NS(id=2),
                  response=NS(defer=AsyncMock(),send_message=AsyncMock()),
                  followup=NS(send=AsyncMock()),message=NS(edit=AsyncMock()))

    async def test_old_confirmation_does_not_change_state_or_create_invoice(self):
        cog=object.__new__(NewTrading)
        cog.order=AsyncMock(return_value={'id':'order','channel_id':8,'buyer_id':1,'seller_id':2,'initiator_id':1,'status':'confirmed'})
        cog.transition=AsyncMock(); cog.prepare_invoice=AsyncMock()
        inter=self.interaction()
        await cog.on_interaction(inter)
        cog.transition.assert_not_awaited()
        cog.prepare_invoice.assert_not_awaited()
        inter.message.edit.assert_awaited_once_with(view=None)
        sent=inter.followup.send.await_args
        self.assertIn('confirmed',sent.args[0])
        self.assertIn('new:trade:pay:order',[b.custom_id for b in sent.kwargs['view'].children])

    async def test_racing_confirmation_recovers_current_step(self):
        cog=object.__new__(NewTrading)
        row={'id':'order','channel_id':8,'buyer_id':1,'seller_id':2,'initiator_id':1,'status':'pending'}
        cog.order=AsyncMock(side_effect=[row,{**row,'status':'confirmed'}])
        cog.transition=AsyncMock(side_effect=OrderStateChanged())
        cog.current_step=AsyncMock()
        await cog.on_interaction(self.interaction())
        self.assertEqual(cog.current_step.await_args.args[1]['status'],'confirmed')

    async def test_paying_recovery_uses_existing_invoice_only(self):
        cog=object.__new__(NewTrading)
        row={'id':'order','status':'paying'}
        query=AsyncMock(return_value={'amount':'3.001234','address':'original','expires_at':'deadline'})
        with patch('modules.new_trading.db.query',query):
            inter=self.interaction()
            await cog.current_step(inter,row)
        self.assertTrue(query.await_args.args[0].startswith('SELECT'))
        self.assertIn('3.001234',inter.followup.send.await_args.args[0])

    async def test_new_message_retires_previous_buttons(self):
        cog=object.__new__(NewTrading)
        old=NS(edit=AsyncMock())
        channel=NS(id=8,guild=NS(id=2),fetch_message=AsyncMock(return_value=old))
        cog.bot=NS(get_channel=lambda cid:channel)
        cog.send_step=AsyncMock(return_value=NS(id=11))
        cog.notify_step=AsyncMock()
        row={'id':'order','channel_id':8,'status':'confirmed','item':'item','terms':'terms','buyer_id':1,'seller_id':2,'amount':3,'fee':2}
        with patch('modules.new_trading.db.setting',AsyncMock(side_effect=[{'channel':8,'message':10},None])):
            await cog._post(row)
        old.edit.assert_awaited_once_with(view=None)
