import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import copy
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import patch
from utils import translation_batch as batch, translation_store as store


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source={'title':'Rules','body':'Exact original\nwith newlines'}
        self.bundle=dict(version=1,message_id='40',source=self.source,translations={
            '中文':{'title':'规则','body':'规则正文'},'日本語':{'title':'ルール','body':'本文'}})

    def test_valid_multiple_languages_and_bom(self):
        data=('\ufeff'+json.dumps(self.bundle,ensure_ascii=False)).encode('utf-8')
        self.assertEqual(batch.decode(data,self.source,40),self.bundle['translations'])

    def test_wrong_source_message_empty_unknown_and_oversized_rejected(self):
        for mutate in [lambda b:b.update(source={'title':'Rules','body':'stale'}),
                       lambda b:b.update(message_id='41'),lambda b:b.update(translations={}),
                       lambda b:b['translations'].update(English={'title':'x','body':'x'}),
                       lambda b:b['translations']['中文'].update(body=''),
                       lambda b:b['translations']['中文'].update(body='x'*3501)]:
            bundle=copy.deepcopy(self.bundle);mutate(bundle)
            with self.assertRaises(ValueError): batch.decode(json.dumps(bundle).encode(),self.source,40)
        with self.assertRaises(ValueError): batch.decode(b'x'*(batch.MAX_BYTES+1),self.source,40)
        with self.assertRaises(ValueError): batch.decode(b'{"version":1,"version":1}',self.source,40)

    async def test_conflict_rolls_back_all_languages_and_audits(self):
        ordered=sorted(self.bundle['translations'])
        rows={store.key(40,ordered[1]):json.dumps({'revision':'concurrent-edit'})}
        initial=copy.deepcopy(rows)
        audits=[]
        class Cursor:
            async def execute(self,sql,args):
                if sql.startswith('INSERT INTO settings'): rows.setdefault(args[0],'null')
                elif sql.startswith('SELECT'): self.key=args[0]
                elif sql.startswith('UPDATE'): rows[args[1]]=args[0]
                elif sql.startswith('INSERT INTO audit'): audits.append(args)
            async def fetchone(self): return {'value':rows[self.key]}
        @asynccontextmanager
        async def transaction():
            try: yield Cursor()
            except BaseException:
                rows.clear();rows.update(initial);audits.clear()
                raise
        with patch.object(store.db,'transaction',transaction):
            with self.assertRaisesRegex(ValueError,'全部取消'):
                await store.save_batch(40,30,self.source,self.bundle['translations'],7,{x:None for x in ordered})
            self.assertEqual(rows,initial)
            self.assertEqual(audits,[])
            records=await store.save_batch(40,30,self.source,self.bundle['translations'],7,
                {ordered[0]:None,ordered[1]:'concurrent-edit'})
        self.assertEqual(set(records),set(ordered))
        self.assertEqual(len(audits),2)
        for language in ordered:
            self.assertEqual(json.loads(rows[store.key(40,language)])['source_hash'],store.fingerprint(self.source))
