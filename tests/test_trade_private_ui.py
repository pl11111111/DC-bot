import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import patch,AsyncMock
from utils import trade_private_ui as ui,private_payment_help as payment,payout_language
from utils.trade_language import PACKS
from modules.new_trading import NewTrading

class PrivateUiTests(unittest.IsolatedAsyncioTestCase):
    def test_all_languages_have_complete_messages_and_fit_discord_limits(self):
        self.assertEqual(set(ui.ROWS),set(PACKS))
        invoice={'amount':'7.104461','expires_at':datetime.now(timezone.utc)}
        for lang,row in ui.ROWS.items():
            self.assertEqual(len(row),len(ui.KEYS))
            member=NS(id=1,roles=[NS(id=12)])
            with patch.object(payout_language.config.NEW,'LANGUAGES',{lang:12}):
                for key in ui.KEYS:
                    text=ui.text(member,key,channel='<#123>',minimum='5.01',limit=3,status='paying',amount='7.104461',address='0x'+'1'*40,deadline='2026-10-01')
                    self.assertTrue(text)
                    if key in ('form','item','amount','terms'): self.assertLessEqual(len(text.encode('utf-16-le'))//2,45,(lang,key))
                text=payment.render(member,invoice,'https://discord.com/channels/2/3/4')
                self.assertIn('7.104461',text)
                self.assertIn('BSC / BEP20',text)
                self.assertIn('1024490102',text)
                self.assertLessEqual(len(text.encode('utf-16-le'))//2,2000,lang)
                self.assertIn('5.10',ui.error(member,ValueError('金额不足，当前最低需 5.10 USDT。已付款请联系管理员。')))

    async def test_receipt_prompt_uses_actor_language_and_preserves_permission_guard(self):
        cog=object.__new__(NewTrading)
        row=dict(id='order',channel_id=8,buyer_id=1,seller_id=2,status='shipped')
        cog.order=AsyncMock(return_value=row);cog.transition=AsyncMock()
        member=NS(id=1,roles=[NS(id=12)])
        inter=NS(user=member,guild=NS(id=2),channel_id=8,data={'custom_id':'new:trade:receipt:order'},response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch.object(payout_language.config.NEW,'LANGUAGES',{'日本語':12}):
            await cog.on_interaction(inter)
            self.assertEqual(inter.followup.send.call_args.args[0],ui.text(member,'receipt'))
            button=inter.followup.send.call_args.kwargs['view'].children[0]
            self.assertEqual(button.label,'受取を確認')
            await button.callback(NS(user=NS(id=2)))
            cog.transition.assert_not_awaited()
