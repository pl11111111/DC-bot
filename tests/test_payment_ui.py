import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from datetime import datetime,timezone
from decimal import Decimal as D
from unittest.mock import patch,AsyncMock
from PIL import Image
from utils import trade_payment_ui as ui,trade_card,shared_payments as payments
from modules.new_trading import NewTrading

ADDRESS='0x1234567890123456789012345678901234567890'

class PaymentUiTests(unittest.IsolatedAsyncioTestCase):
    def invoice(self):
        return dict(amount=D('5.011234'),address=ADDRESS,expires_at=datetime(2026,9,12,4,30))

    def test_exact_amount_fee_and_address_are_not_rounded_for_display(self):
        embed=ui.payment_embed({'id':'order'},self.invoice(),D('.01'))
        self.assertEqual(embed.title,'实际应到账：5.011234 USDT')
        self.assertIn('5.021234',embed.fields[1].value)
        self.assertEqual([field.name for field in embed.fields],['收款地址','🏦 使用币安或其他交易所提现','binance邀请链接','邀请码','⏳ 付款截止'])
        self.assertIn(ADDRESS,ui.copy_text(self.invoice()))
        self.assertEqual(ui.deadline(self.invoice()),int(datetime(2026,9,12,4,30,tzinfo=timezone.utc).timestamp()))
        layout=trade_card.components(embed,None,True,True)
        self.assertEqual(len(layout),1)
        self.assertEqual([x['type'] for x in layout[0]['components']],[12,10,12,10])
        self.assertIn('下方二维码仅包含收款地址',layout[0]['components'][1]['content'])
        self.assertNotIn(ADDRESS,layout[0]['components'][1]['content'])
        self.assertIn('payment-qr.png',layout[0]['components'][2]['items'][0]['media']['url'])
        self.assertIn(ADDRESS,layout[0]['components'][3]['content'])
        self.assertEqual(len(embed.fields[1].value.split('\n\n')),4)

    def test_qr_is_png_and_contains_only_persisted_address(self):
        original=ui.qrcode.QRCode.add_data
        values=[]
        def capture(code,data,*args,**kwargs):
            values.append(data)
            return original(code,data,*args,**kwargs)
        with patch.object(ui.qrcode.QRCode,'add_data',capture):
            file=ui.qr_file(ADDRESS)
        try:
            self.assertEqual(values,[ADDRESS])
            image=Image.open(file.fp)
            self.assertEqual(image.format,'PNG')
            self.assertEqual(image.width,image.height)
            image.verify()
        finally: file.close()

    def test_trade_details_are_compact_and_no_payment_hint_after_receipt(self):
        row=dict(id='order',status='receipt_confirmed',item='item',terms='terms',buyer_id=1,seller_id=2,amount=D('3.01'),fee=D('2'),credits=0)
        embed=NewTrading.order_embed(None,row)
        body=trade_card.components(embed,None,True)[0]['components'][1]['content']
        self.assertIn('**💰 价格** 3.01 USDT　｜　**🔒 托管费** 2 USDT',body)
        self.assertNotIn('付款时请以',body)

    async def test_dynamic_minimum_rejects_three_accepts_three_point_zero_one(self):
        response=[dict(coin='USDT',networkList=[dict(network='BSC',withdrawEnable=True,withdrawFee='.01',withdrawMin='3',withdrawIntegerMultiple='.00000001')])]
        with patch('utils.binance_api.make_api_request',AsyncMock(return_value=response)) as request:
            with self.assertRaises(ValueError): await payments.payout_amount_quote(D('3'))
            self.assertEqual(await payments.payout_amount_quote(D('3.01')),(D('.01'),D('3')))
            self.assertTrue(all(call.args[1]=='GET' for call in request.await_args_list))
