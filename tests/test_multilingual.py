import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import trade_language as t,verification_flow as v,article_translation as a

class MultilingualTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_action_stage_has_distinct_buyer_seller_notifications(self):
        stages=('confirmed','invoicing','paying','waiting','paid','shipped','receipt_confirmed',
                'releasing','completed','refund_ready','releasing_refund','refunded','manual_refunded')
        for language,pack in t.PACKS.items():
            for stage in stages:
                with self.subTest(language=language,stage=stage):
                    row=dict(buyer_id=1,seller_id=2,status=stage,_languages=[language],
                        _user_languages={'buyer_id':language,'seller_id':language})
                    roles=pack['participants'][stage]
                    self.assertNotEqual(roles['buyer'],roles['seller'])
                    message,_=t.notify(row)
                    self.assertIn('<@1>：'+roles['buyer'],message)
                    self.assertIn('<@2>：'+roles['seller'],message)
                    self.assertEqual(message.count(f'**{language}**'),1)
                    self.assertEqual(message.count('<@1>'),1)
                    self.assertEqual(message.count('<@2>'),1)
        confirmed=t.PACKS['English']['participants']['confirmed']
        self.assertIn('15 minutes',confirmed['buyer'])
        self.assertNotIn('15 minutes',confirmed['seller'])

    async def test_pending_instructions_follow_initiator_not_buyer(self):
        for initiator in (1,2):
            for languages in (['中文','English'],['中文','中文']):
                row=dict(buyer_id=1,seller_id=2,initiator_id=initiator,status='pending',
                    _user_languages=dict(zip(('buyer_id','seller_id'),languages)),_languages=list(dict.fromkeys(languages)))
                result,_=t.notify(row)
                for key,lang in row['_user_languages'].items():
                    action='pending_sender' if row[key]==initiator else 'pending_recipient'
                    self.assertIn(f'<@{row[key]}>：'+t.PACKS[lang][action],result)
                self.assertEqual(result.count('<@1>'),1)
                self.assertEqual(result.count('<@2>'),1)

    async def test_localized_fields_retain_emojis(self):
        import discord
        embed=discord.Embed()
        embed.add_field(name='📦 物品',value='item')
        embed.add_field(name='付款截止',value='deadline')
        result=t.localize_embed(embed,dict(id='order',status='paid',_languages=['English','中文']))
        self.assertTrue(result.fields[0].name.startswith('📦 '))
        self.assertTrue(result.fields[1].name.startswith('⏳ '))

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
            self.assertIn('6 位小数',msg)
            self.assertIn('Seller <@2>：Wait for the bot',msg)
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
