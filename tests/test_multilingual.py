import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import trade_language as t,verification_flow as v,article_translation as a

class MultilingualTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_scan_pattern_survives_driver_parameter_formatting(self):
        import pymysql
        connection=pymysql.connect(defer_connect=True)
        connection.server_status=0
        async def query(sql,args=()):
            rendered=connection.cursor().mogrify(sql,args)
            self.assertIn("LIKE 'notice:%'",rendered)
            self.assertEqual(args,('notice:%',))
            return []
        with patch.object(a.vf.db,'query',query):
            await a.sync_existing(NS())

    async def test_trade_languages_follow_roles_and_deduplicate(self):
        row=dict(buyer_id=1,seller_id=2,status='paying')
        members={1:NS(roles=[NS(id=12)]),2:NS(roles=[])}
        with patch.object(t.config.NEW,'LANGUAGES',{'中文':12}):
            localized=await t.assign(row,NS(get_member=lambda uid:members[uid]))
            self.assertEqual(localized['_languages'],['中文','English'])
            msg,_=t.notify(localized)
            self.assertEqual(msg.count('<@1>'),1)
            self.assertEqual(msg.count('<@2>'),1)
            self.assertIn('6 decimals',msg)
            members[2]=members[1]
            localized=await t.assign(row,NS(get_member=lambda uid:members[uid]))
            self.assertEqual(localized['_languages'],['中文'])

    async def test_all_verification_translations_match_new_source_and_fit(self):
        config=json.loads(Path('config/new_content.json').read_text(encoding='utf-8'))
        source=dict(title=config['verify_title'],body=config['verify_body'])
        for language in t.PACKS:
            translated,missing=v.translated(source,language)
            self.assertFalse(missing,language)
            self.assertLess(len(translated['body']),3501)
            self.assertIn('<#1546465989896568873>',translated['body'])
            self.assertIn('<#1546918875235360928>',translated['body'])

    async def test_article_reads_live_text_and_rejects_other_authors(self):
        raw={'author':{'id':'9'},'components':[{'type':17,'components':[{'type':10,'content':'## Rules\n\nCurrent rules'}]}]}
        message=NS(id=4,channel=NS(id=3),_state=NS(http=NS(request=AsyncMock(return_value=raw))))
        self.assertEqual(await a.read(message,9),dict(title='Rules',body='Current rules'))
        with self.assertRaises(ValueError): await a.read(message,8)
