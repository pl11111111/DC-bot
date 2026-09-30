import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import itertools
import unittest
import discord
from utils import trade_extra as extra,trade_language

class ExtraTests(unittest.TestCase):
    def test_resume_notice_is_bilingual_in_field_body(self):
        source='管理员已核实并恢复自动关闭，频道约 5 分钟后关闭。'
        embed=discord.Embed()
        embed.add_field(name='交易提示',value=source)
        result=trade_language.localize_embed(embed,dict(id='order',status='cancelled',_languages=['日本語','中文']))
        self.assertIn('管理者が確認し、自動閉鎖を再開しました。',result.fields[0].value)
        self.assertIn(source,result.fields[0].value)
        self.assertIn('約5分後',result.fields[0].value)

    def test_every_known_notice_fits_two_language_field_and_custom_text_unchanged(self):
        self.assertEqual(set(extra.ROWS),set(trade_language.PACKS))
        for source in extra.SOURCES:
            for first,second in itertools.product(extra.ROWS,repeat=2):
                result=extra.render(source,[first,second])
                self.assertLessEqual(len(result.encode('utf-16-le'))//2,1024)
                self.assertEqual(result.count('**'+first+'**'),1)
        self.assertEqual(extra.render('管理员自定义理由 123',['日本語','English']),'管理员自定义理由 123')
