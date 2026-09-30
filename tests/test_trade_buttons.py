import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import itertools
import unittest
from utils import trade_buttons as b
from utils.trade_language import PACKS
from modules.new_trading import NewTrading
from types import SimpleNamespace as NS
from unittest.mock import patch
from utils import trade_private,payout_language

class TradeButtonTests(unittest.IsolatedAsyncioTestCase):
    def test_instruction_button_names_and_private_reply_follow_language(self):
        for language in b.ROWS:
            expected=b.ROWS[language][b.ACTIONS.index('pay')]
            self.assertIn(expected,PACKS[language]['participants']['confirmed']['buyer'])
            self.assertNotIn('Get payment information',PACKS[language]['participants']['confirmed']['buyer'])
            with patch.object(payout_language.config.NEW,'LANGUAGES',{language:123}):
                self.assertEqual(trade_private.text(NS(roles=[NS(id=123)]),'payment_ready'),trade_private.ROWS[language][0])
        self.assertEqual(trade_private.text(NS(roles=[]),'payment_ready'),trade_private.ROWS['English'][0])
        self.assertEqual(PACKS['日本語']['participants']['confirmed']['buyer'],
            '15分以内に「支払情報」を押し、必要な着金額を確認してください。')

    def test_all_language_pairs_fit_and_keep_complete_labels(self):
        self.assertEqual(set(b.ROWS),set(PACKS))
        for first,second in itertools.product(b.ROWS,repeat=2):
            for action in b.ACTIONS:
                result=b.label(action,{'status':'paying','_languages':[first,second]})
                self.assertLessEqual(len(result.encode('utf-16-le'))//2,80)
                expected=' / '.join(b.ROWS[lang][b.ACTIONS.index(action)] for lang in dict.fromkeys([first,second]))
                self.assertEqual(result,expected)

    async def test_refund_and_payout_keep_same_action_but_distinct_labels(self):
        cog=object.__new__(NewTrading)
        for status,label in [('receipt_confirmed','领取货款 / Claim funds'),('refund_ready','领取退款 / Claim refund')]:
            view=cog.view(dict(id='order',status=status,_languages=['中文','English']))
            self.assertEqual(view.children[0].label,label)
            self.assertEqual(view.children[0].custom_id,'new:trade:collect:order')
            self.assertIsNotNone(view.children[0].emoji)
