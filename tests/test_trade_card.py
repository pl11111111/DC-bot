import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace as NS
import discord
from utils import trade_card as card
from modules.new_community import buttons


class TradeCardTests(unittest.IsolatedAsyncioTestCase):
    def layout(self):
        embed=discord.Embed(title='Trade',description='Item and terms')
        embed.add_field(name='Amount',value='100 USDT')
        embed.set_footer(text='Order 123')
        return embed,buttons([('Confirm','trade:confirm:123')])

    async def test_one_container_in_image_text_buttons_order(self):
        embed,view=self.layout()
        result=card.components(embed,view,True)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['type'],17)
        children=result[0]['components']
        self.assertEqual([x['type'] for x in children],[12,10,14,1])
        self.assertEqual(children[-1]['components'][0]['custom_id'],'new:trade:confirm:123')
        self.assertIn('100 USDT',children[1]['content'])

    async def test_transport_sets_v2_without_legacy_embeds(self):
        http=NS(send_files=AsyncMock(return_value={'id':'99'}))
        channel=NS(id=8,_state=NS(http=http))
        embed,view=self.layout()
        message=await card.send(channel,embed,view,None)
        self.assertEqual(message.id,99)
        kwargs=http.send_files.await_args.kwargs
        self.assertEqual(kwargs['flags'],32768)
        self.assertNotIn('embeds',kwargs)
        self.assertEqual(kwargs['allowed_mentions'],{'parse':[]})

    async def test_retire_preserves_raw_image_and_text(self):
        embed,view=self.layout()
        layout=card.components(embed,view,True)
        http=NS(request=AsyncMock(side_effect=[{'components':layout},{}]))
        msg=NS(id=9,channel=NS(id=8),flags=NS(value=32768),_state=NS(http=http))
        await card.retire(msg)
        patched=http.request.await_args.kwargs['json']['components']
        self.assertEqual(patched[0]['components'][0],layout[0]['components'][0])
        self.assertEqual(patched[0]['components'][1],layout[0]['components'][1])
        self.assertTrue(patched[0]['components'][-1]['components'][0]['disabled'])
        self.assertFalse(layout[0]['components'][-1]['components'][0]['disabled'])
