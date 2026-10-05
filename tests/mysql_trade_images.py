"""Atomic image/order persistence on disposable loopback MySQL only."""
from tests.mysql_integration import bootstrap
import asyncio
from io import BytesIO
from PIL import Image
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace as NS
from utils import new_store as db,trade_images as images,trade_admission


async def main():
    try:
        source=BytesIO()
        with Image.new('RGB',(1280,1280)) as image:
            # A realistically sized stored image exercises LONGTEXT and JSON decoding.
            image.frombytes(__import__('os').urandom(1280*1280*3))
            image.save(source,format='JPEG',quality=85)
        data=source.getvalue()
        with patch.object(images,'download',AsyncMock(return_value=data)):
            stored=await images.prepare([NS(id=42,size=len(data),filename='item.jpg',content_type='image/jpeg',url='https://cdn.discordapp.com/attachments/7/42/item.jpg')])
        async with db.transaction() as cur:
            await trade_admission.reserve(cur,1,2)
            await cur.execute('INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee) VALUES(%s,1,2,1,%s,%s,10.5,2)',('image_order',"Item'; DROP TABLE orders; --",'Details'))
            await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s)',('trade_image:image_order',db.encode(stored)))
        assert await db.setting('trade_image:image_order')==stored
        row=await db.query('SELECT * FROM orders WHERE id=%s',('image_order',),one=True)
        assert row['amount']==__import__('decimal').Decimal('10.5')
        print('PASS image and unchanged order price commit together with parameterized text')

        try:
            async with db.transaction() as cur:
                await trade_admission.reserve(cur,3,4)
                await cur.execute("INSERT INTO orders(id,buyer_id,seller_id,initiator_id,item,terms,amount,fee) VALUES('rollback_image',3,4,3,'Item','',10,2)")
                await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s)',('trade_image:rollback_image',db.encode(stored)))
                raise RuntimeError('simulated failure before commit')
        except RuntimeError:pass
        assert await db.query("SELECT id FROM orders WHERE id='rollback_image'",one=True) is None
        assert await db.setting('trade_image:rollback_image') is None
        assert await db.setting('trade_rate:3') is None
        print('PASS order, image and admission quota roll back together')

        pools=list(db._pools.values()); db._pools.clear()
        for pool in pools:pool.close()
        for pool in pools:await pool.wait_closed()
        recovered=await db.setting('trade_image:image_order')
        with patch.object(images,'download',AsyncMock()) as download:
            upload=images.file(recovered)
            try: assert upload.fp.read()==__import__('base64').b64decode(stored['data'])
            finally:upload.close();upload.fp.close()
            download.assert_not_awaited()
        print('PASS fresh database connections restore picture bytes without original attachment URL')
    finally:
        for pool in db._pools.values():pool.close()
        for pool in db._pools.values():await pool.wait_closed()


if __name__=='__main__':
    bootstrap()
    asyncio.run(main())
