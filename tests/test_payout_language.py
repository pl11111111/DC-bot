import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from decimal import Decimal
from utils import payout_language as pl
from utils.trade_language import PACKS
from modules.new_trading import NewTrading

class PayoutLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_participant_gets_step_specific_notice_without_payout(self):
        self.assertEqual(set(pl.PAYEE_ONLY),set(pl.ROWS))
        for language in pl.ROWS:
            for refund in (False,True):
                row=dict(id='order',channel_id=10,buyer_id=1,seller_id=2,
                         status='refund_ready' if refund else 'receipt_confirmed')
                member=NS(id=2 if refund else 1,roles=[NS(id=123)])
                cog=object.__new__(NewTrading)
                cog.order=AsyncMock(return_value=row)
                cog.address_modal=AsyncMock()
                inter=NS(data={'custom_id':'new:trade:collect:order'},guild=NS(id=pl.config.NEW.GUILD_ID),
                         channel_id=10,user=member,response=NS(send_message=AsyncMock()))
                with patch.object(pl.config.NEW,'LANGUAGES',{language:123}):
                    await cog.on_interaction(inter)
                    inter.response.send_message.assert_awaited_once_with(pl.payee_only(member,refund),ephemeral=True)
                    self.assertNotEqual(pl.payee_only(member,refund),pl.texts(member)['stale'])
                cog.address_modal.assert_not_awaited()

    async def test_every_language_modal_and_summary_preserves_amounts(self):
        self.assertEqual(set(pl.ROWS),set(PACKS))
        for language,row in pl.ROWS.items():
            self.assertEqual(len(row),len(pl.KEYS))
            member=NS(id=2,roles=[NS(id=123)])
            with patch.object(pl.config.NEW,'LANGUAGES',{language:123}):
                inter=NS(user=member,response=NS(send_modal=AsyncMock()))
                cog=object.__new__(NewTrading)
                await cog.address_modal(inter,dict(id='order',status='receipt_confirmed',buyer_id=1,seller_id=2,amount=Decimal('7.01')))
                modal=inter.response.send_modal.call_args.args[0]
                ui=pl.texts(member)
                self.assertEqual(modal.title,ui['title'])
                self.assertLessEqual(len(modal.title),45)
                self.assertLessEqual(len(modal.children[0].label),45)
                self.assertLessEqual(len(ui['confirm']),80)
                modal.children[0]._input_value='0x'+'1'*40
                click=NS(user=member,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
                with patch('modules.new_trading.payments.payout_quote',AsyncMock(return_value=(Decimal('.01'),Decimal('7')))):
                    await modal.callback(click)
                message=click.followup.send.call_args.args[0]
                self.assertIn('7.01 USDT',message)
                self.assertIn('0.01 USDT',message)
                self.assertIn(ui['seller_note'],message)
                self.assertEqual(click.followup.send.call_args.kwargs['view'].children[0].label,ui['confirm'])

    def test_role_free_defaults_to_english_and_errors_localized(self):
        member=NS(roles=[])
        self.assertEqual(pl.language(member),'English')
        self.assertEqual(pl.error(member,ValueError('请输入有效的 USDT-BEP20 地址')),pl.texts(member)['invalid'])
