import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import copy
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import long_article as long, article_translation as article, translation_batch as batch

class LongArticleTests(unittest.IsolatedAsyncioTestCase):
    def test_splitting_preserves_every_character(self):
        for body in ['a'*10001,('## 标题\n\n😀 text\n'*900)+'Final sentence.','a'*3400+'\n\nlast']:
            parts=long.split_body(body)
            self.assertEqual(''.join(parts),body)
            self.assertTrue(all(len(p)<=3400 for p in parts))

    async def test_publish_then_export_full_source_and_reject_changed_part(self):
        body=('Paragraph.\n\n'*800)+'THE END'
        channel=NS(id=30)
        saved={};messages={}
        async def setting(key,value=None):
            if value is not None: saved[key]=copy.deepcopy(value)
            return saved.get(key)
        async def send(channel,layout):
            ident=40+len(messages)
            messages[ident]={'author':{'id':'99'},'components':layout}
            return NS(id=ident,channel=channel)
        async def request(route): return messages[int(route.url.rsplit('/',1)[1])]
        message=NS(id=40,channel=channel,_state=NS(http=NS(request=AsyncMock(side_effect=request))))
        with patch.object(long.db,'setting',side_effect=setting),patch.object(long.notice_card,'send',side_effect=send):
            root,_=await long.publish(NS(),channel,'Guide',body,[])
            self.assertEqual(root,40)
            self.assertEqual(await article.read(message,99),{'title':'Guide','body':body})
            messages[41]['components'][0]['components'][0]['content']='edited'
            with self.assertRaises(ValueError): await article.read(message,99)

    async def test_interrupted_publication_cannot_export_partial_source(self):
        saved={}
        async def setting(key,value=None):
            if value is not None: saved[key]=copy.deepcopy(value)
            return saved.get(key)
        channel=NS(id=30)
        with patch.object(long.db,'setting',side_effect=setting),patch.object(long.notice_card,'send',AsyncMock(side_effect=[NS(id=40,channel=channel),RuntimeError('send failed')])):
            with self.assertRaises(RuntimeError): await long.publish(NS(),channel,'Guide','a'*7000,[])
            self.assertEqual(saved['long_article:40']['state'],'publishing')
            with self.assertRaises(ValueError): await long.read(NS(id=40,channel=channel),99,{})

    def test_long_translation_bundle_roundtrip(self):
        source={'title':'Guide','body':'English\n'*1300}
        translations={'中文':{'title':'指南','body':'完整正文\n'*2500}}
        data=json.dumps(dict(version=1,source=source,translations=translations),ensure_ascii=False).encode()
        self.assertEqual(batch.decode(data,source,40),translations)

    async def test_translation_pagination_reaches_final_text(self):
        source={'title':'Guide','body':'a'*7000+'LAST'}
        inter=NS(user=NS(id=7),guild=NS(fetch_member=AsyncMock(return_value=NS(id=7,roles=[]))),locale='en-US',message=NS(id=40),followup=NS(send=AsyncMock()))
        with patch.object(article,'read',AsyncMock(return_value=source)),patch.object(article.vf.db,'setting',AsyncMock(return_value=None)),patch.object(article.translation_store,'resolve',AsyncMock(return_value=(source,False))):
            await article.start(NS(bot=NS(user=NS(id=99))),inter)
            view=inter.followup.send.call_args.kwargs['view']
            click=NS(guild=NS(id=2),user=NS(id=7),response=NS(defer=AsyncMock()),edit_original_response=AsyncMock(),followup=NS(send=AsyncMock()))
            await view.children[-1].callback(click)
            await view.children[-1].callback(click)
            self.assertTrue(click.edit_original_response.call_args.kwargs['embeds'][0].description.endswith('LAST'))
            self.assertTrue(view.children[-1].disabled)
