"""Offline hostile-upload and durable-card regression checks."""
import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import base64
import hashlib
from io import BytesIO
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
from contextlib import asynccontextmanager
import struct
import zlib
import discord
from PIL import Image, PngImagePlugin
from utils import trade_images as images, trade_card
from modules.new_trading import NewTrading


def picture(fmt='PNG',size=(32,24),**kwargs):
    out=BytesIO()
    with Image.new('RGB',size,'red') as image: image.save(out,format=fmt,**kwargs)
    return out.getvalue()


def attachment(data,**kwargs):
    return NS(**dict(dict(id=42,size=len(data),filename='item.png',content_type='image/png',
                         url='https://cdn.discordapp.com/ephemeral-attachments/7/42/item.png?ex=abc&hm=signed'),**kwargs))


def asset(data):
    return dict(version=1,sha256=hashlib.sha256(data).hexdigest(),data=base64.b64encode(data).decode('ascii'))


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_formats_and_resizing(self):
        for fmt in ('JPEG','PNG','WEBP'):
            result=images.normalize(picture(fmt,(1800,900)),fmt,images.MIMES[fmt])
            with Image.open(BytesIO(result)) as image:
                self.assertEqual(image.format,'JPEG')
                self.assertEqual(image.size,(1280,640))
            self.assertLessEqual(len(result),images.MAX_STORED)

    def test_noisy_valid_photo_is_resized_to_storage_budget(self):
        out=BytesIO()
        with Image.frombytes('RGB',(1280,1280),os.urandom(1280*1280*3)) as image:
            image.save(out,format='JPEG',quality=85)
        result=images.normalize(out.getvalue(),'JPEG','image/jpeg')
        self.assertLessEqual(len(result),images.MAX_STORED)
        with Image.open(BytesIO(result)) as image: self.assertLessEqual(max(image.size),1280)

    async def test_sdk_resolves_only_selected_modal_attachment(self):
        upload=discord.ui.FileUpload(min_values=0,max_values=1,required=False)
        modal=discord.ui.DesignerModal(discord.ui.Label('Image',item=upload),title='Trade')
        data=picture()
        uploaded=dict(id='42',filename='item.png',size=len(data),url='https://cdn.discordapp.com/attachments/7/42/item.png',proxy_url='https://media.discordapp.net/attachments/7/42/item.png',content_type='image/png',ephemeral=True)
        inter=NS(_state=NS(http=NS()),data={'resolved':{'attachments':{'42':uploaded}}})
        modal._refresh(inter,[{'type':18,'component':{'type':19,'custom_id':upload.custom_id,'values':['42']}}])
        self.assertEqual(upload.values[0].id,42)
        self.assertEqual(upload.values[0].size,len(data))
        modal._refresh(inter,[{'type':18,'component':{'type':19,'custom_id':upload.custom_id,'values':[]}}])
        self.assertEqual(upload.values,[])

    def test_metadata_and_appended_payload_removed(self):
        info=PngImagePlugin.PngInfo(); info.add_text('secret','GPS secret')
        data=picture(pnginfo=info)+b'<script>malicious trailing payload</script>'
        result=images.normalize(data,'PNG','image/png')
        self.assertNotIn(b'secret',result)
        self.assertNotIn(b'<script>',result)
        exif=Image.Exif(); exif[270]='private EXIF description'
        result=images.normalize(picture('JPEG',exif=exif),'JPEG','image/jpeg')
        with Image.open(BytesIO(result)) as image: self.assertFalse(image.getexif())
        self.assertNotIn(b'private EXIF',result)

    def test_spoofed_mime_format_and_truncation(self):
        for data,fmt,mime in ((b'<svg onload="alert(1)"/>','PNG','image/png'),
                              (picture('GIF'),'PNG','image/png'),
                              (picture(),'JPEG','image/jpeg'),
                              (picture(),'PNG','text/html'),
                              (picture()[:40],'PNG','image/png')):
            with self.subTest(fmt=fmt,mime=mime),self.assertRaises(images.ImageRejected):
                images.normalize(data,fmt,mime)

    def test_animated_and_pixel_bomb_rejected(self):
        out=BytesIO()
        with Image.new('RGB',(10,10),'red') as first, Image.new('RGB',(10,10),'blue') as second:
            first.save(out,format='WEBP',save_all=True,append_images=[second],duration=100,loop=0)
        with self.assertRaises(images.ImageRejected): images.normalize(out.getvalue(),'WEBP','image/webp')
        original=picture()
        for width,height in ((8193,1),(5000,4000)):
            header=struct.pack('>II',width,height)+original[24:29]
            data=original[:16]+header+struct.pack('>I',zlib.crc32(b'IHDR'+header))+original[33:]
            with self.assertRaises(images.ImageRejected) as rejected: images.normalize(data,'PNG','image/png')
            self.assertEqual(rejected.exception.key,'image_limits')

    def test_only_attachment_cdn_url_for_same_id(self):
        good='https://cdn.discordapp.com/attachments/7/42/item.png?ex=123'
        self.assertEqual(images.attachment_url(good,42),good)
        for url in ('http://cdn.discordapp.com/attachments/7/42/a.png',
                    'https://127.0.0.1/attachments/7/42/a.png',
                    'https://cdn.discordapp.com.evil.example/attachments/7/42/a.png',
                    'https://evil@cdn.discordapp.com/attachments/7/42/a.png',
                    'https://cdn.discordapp.com:8443/attachments/7/42/a.png',
                    'https://cdn.discordapp.com/attachments/7/43/a.png',
                    'https://cdn.discordapp.com/attachments/7/42/%2e%2e',
                    'https://cdn.discordapp.com/attachments/7/42/a%2fb.png',
                    'file:///etc/passwd',good+'#fragment',good+'a'*2048):
            with self.subTest(url=url),self.assertRaises(images.ImageRejected): images.attachment_url(url,42)

    async def test_reject_metadata_before_network_and_empty_optional(self):
        self.assertIsNone(await images.prepare(()))
        data=picture()
        with patch.object(images,'download',AsyncMock()) as download:
            for attrs in (dict(size=images.MAX_INPUT+1),dict(size=0),dict(filename='item.svg'),
                          dict(content_type='text/html'),dict(url='https://localhost/x'),dict(id='42')):
                with self.subTest(attrs=attrs),self.assertRaises(images.ImageRejected):
                    await images.prepare([attachment(data,**attrs)])
            with self.assertRaises(images.ImageRejected): await images.prepare([attachment(data)]*2)
            download.assert_not_awaited()

    async def test_durable_asset_not_dependent_on_original_url(self):
        data=picture()
        with patch.object(images,'download',AsyncMock(return_value=data)):
            stored=await images.prepare([attachment(data)])
        with patch.object(images,'download',AsyncMock()) as download:
            f=images.file(stored)
            try:
                self.assertEqual(f.filename,'trade-item.jpg')
                with Image.open(f.fp) as image: self.assertEqual(image.format,'JPEG')
            finally: f.close()
            download.assert_not_awaited()
        for corrupt in (dict(stored,sha256='0'*64),dict(stored,data='invalid!'),dict(stored,version=2)):
            with self.assertRaises(images.ImageRejected): images.file(corrupt)

    async def test_declared_length_and_network_failures(self):
        data=picture()
        for effect in (data+b'x',asyncio.TimeoutError()):
            with patch.object(images,'download',AsyncMock(side_effect=effect if isinstance(effect,Exception) else None,
                                                         return_value=effect)):
                with self.assertRaises(images.ImageRejected): await images.prepare([attachment(data)])

    async def test_busy_uploads_fail_without_queued_decoders(self):
        gate=asyncio.Semaphore(2)
        await gate.acquire(); await gate.acquire()
        with patch.object(images,'_slots',gate),patch.object(images,'download',AsyncMock()) as download:
            with self.assertRaises(images.ImageRejected) as rejected: await images.prepare([attachment(picture())])
            self.assertEqual(rejected.exception.key,'image_busy')
            download.assert_not_awaited()

    async def test_per_user_upload_rate_precedes_network(self):
        limiter=images.TokenBucket(3,60,clock=lambda:0)
        data=picture()
        with patch.object(images,'_uploads',limiter),patch.object(images,'download',AsyncMock(return_value=data)) as download:
            for _ in range(3):await images.prepare([attachment(data)],actor=(2,1))
            with self.assertRaises(images.ImageRejected): await images.prepare([attachment(data)],actor=(2,1))
            self.assertEqual(download.await_count,3)
            await images.prepare([attachment(data)],actor=(2,2))
            self.assertEqual(download.await_count,4)

    async def test_stream_size_redirect_encoding_and_unauthenticated_client(self):
        async def chunks(values):
            for value in values: yield value
        for status,encoding,length,values in ((302,'identity',None,[]),(200,'gzip',None,[]),
                (200,'identity',images.MAX_INPUT+1,[]),(200,'identity',1,[b'a'*images.MAX_INPUT,b'b'])):
            response=NS(status=status,headers={'Content-Encoding':encoding},content_length=length,
                        content=NS(iter_chunked=lambda _:chunks(values)))
            request=MagicMock(); request.__aenter__=AsyncMock(return_value=response)
            request.__aexit__=AsyncMock(return_value=False)
            session=NS(get=MagicMock(return_value=request))
            client=MagicMock(); client.__aenter__=AsyncMock(return_value=session); client.__aexit__=AsyncMock(return_value=False)
            with patch.object(images.aiohttp,'ClientSession',return_value=client) as factory:
                with self.assertRaises(images.ImageRejected): await images.download('https://cdn.discordapp.com/attachments/7/42/a.png')
                self.assertFalse(factory.call_args.kwargs['trust_env'])
                self.assertFalse(factory.call_args.kwargs['auto_decompress'])
                self.assertNotIn('Authorization',factory.call_args.kwargs['headers'])
                self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    async def test_card_item_and_payment_qr_have_distinct_attachments(self):
        files=[discord.File(BytesIO(b'test'),filename=name) for name in ('trade-step.png','payment-qr.png','trade-item.jpg')]
        http=NS(send_files=AsyncMock(return_value={'id':'9'}))
        try:
            await trade_card.send(NS(id=8,_state=NS(http=http)),discord.Embed(title='Order'),None,
                                  files[0],files[1],files[2],'Item picture')
            kwargs=http.send_files.await_args.kwargs
            self.assertEqual(kwargs['files'],files)
            galleries=[c for c in kwargs['components'][0]['components'] if c['type']==12]
            self.assertEqual([c['items'][0]['media']['url'] for c in galleries],
                ['attachment://trade-step.png','attachment://payment-qr.png','attachment://trade-item.jpg'])
            self.assertEqual(kwargs['allowed_mentions'],{'parse':[]})
            self.assertEqual(kwargs['components'][0]['components'][-2],{'type':10,'content':'-# Item picture'})
            self.assertEqual(trade_card.disable_buttons(kwargs['components'])[0]['components'][-1]['items'][0]['media'],
                             {'url':'attachment://trade-item.jpg'})
        finally:
            for f in files:f.close()

    async def test_image_modal_validation_snapshot_and_atomic_insert(self):
        cog=object.__new__(NewTrading)
        buyer=MagicMock(spec=discord.Member); buyer.id=1; buyer.bot=False
        seller=MagicMock(spec=discord.Member); seller.id=2; seller.bot=False
        guild=NS(id=2,default_role=MagicMock(),me=MagicMock(),fetch_member=AsyncMock(side_effect=[seller,buyer]),get_role=lambda _:None)
        category=MagicMock(spec=discord.CategoryChannel); category.guild=guild
        guild.create_text_channel=AsyncMock(return_value=NS(id=8,mention='#trade'))
        cog.bot=NS(get_channel=lambda _:category); cog.post=AsyncMock(); cog.order=AsyncMock(return_value={})
        ctx=NS(guild=guild,user=buyer,send_modal=AsyncMock())
        calls=[]
        cur=NS(execute=AsyncMock(side_effect=lambda *args:calls.append(args)))
        @asynccontextmanager
        async def tx(): yield cur
        with patch('modules.new_trading.cfg.PAYMENTS_ENABLED',True),patch('modules.new_trading.payments.payout_amount_quote',AsyncMock()) as quote,patch('modules.new_trading.db.transaction',tx),patch('modules.new_trading.db.query',AsyncMock()),patch('modules.new_trading.db.audit',AsyncMock()),patch('modules.new_trading.trade_admission.reserve',AsyncMock()) as reserve,patch.object(images,'prepare',AsyncMock()) as prepare:
            await cog.start(ctx,seller)
            modal=ctx.send_modal.await_args.args[0]
            serialized=modal.to_dict()
            self.assertEqual(len(serialized['components']),4)
            upload=serialized['components'][-1]['component']
            self.assertEqual(upload['type'],19); self.assertFalse(upload['required']); self.assertEqual(upload['max_values'],1)
            for label,value in zip(modal.children,['Item','10.50','Details']):label.item._input_value=value
            inter=NS(guild=guild,user=buyer,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            wrong=NS(guild=guild,user=seller,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            await modal.callback(wrong); prepare.assert_not_awaited()
            prepare.side_effect=images.ImageRejected()
            await modal.callback(inter)
            quote.assert_not_awaited(); reserve.assert_not_awaited(); guild.create_text_channel.assert_not_awaited()
            # A fresh form is required after rejection; previously submitted form is consumed.
            await modal.callback(inter); prepare.assert_awaited_once()
            await cog.start(ctx,seller); modal=ctx.send_modal.await_args.args[0]
            for label,value in zip(modal.children,['Original item','10.50','Original terms']):label.item._input_value=value
            stored=asset(images.normalize(picture(),'PNG','image/png'))
            prepare.side_effect=None; prepare.return_value=stored; prepare.reset_mock()
            async def mutate(**kwargs):
                modal.children[0].item._input_value='replayed item'
                modal.children[1].item._input_value='0'
                modal.children[2].item._input_value='replayed terms'
            inter.response.defer=AsyncMock(side_effect=mutate)
            await modal.callback(inter); await modal.callback(inter)
            prepare.assert_awaited_once(); quote.assert_awaited_once(); reserve.assert_awaited_once()
            insert=next(args for args in calls if args[0].startswith('INSERT INTO orders'))
            self.assertEqual(insert[1][5:7],('Original item','Original terms'))
            image_insert=next(args for args in calls if args[0].startswith('INSERT INTO settings'))
            self.assertEqual(image_insert[1][0],'trade_image:'+insert[1][0])
            self.assertEqual(image_insert[1][1],__import__('utils.new_store',fromlist=['encode']).encode(stored))
            guild.create_text_channel.assert_awaited_once()

    async def test_post_recovers_persisted_image_and_closes_upload_file(self):
        cog=object.__new__(NewTrading)
        channel=NS(id=8,guild=NS(id=2))
        cog.resolve_trade_channel=AsyncMock(return_value=channel)
        cog.order_embed=MagicMock(return_value=discord.Embed(title='Trade'))
        cog.view=MagicMock(return_value=None)
        uploaded=[]
        async def send(*args):
            if args[1]=='pending':
                uploaded.append(args[5]); self.assertEqual(args[5].fp.read(2),b'\xff\xd8')
            else:
                self.assertEqual(len(args),4)
            return NS(id=9)
        cog.send_step=AsyncMock(side_effect=send); cog.deliver_step_notification=AsyncMock()
        stored=asset(images.normalize(picture(),'PNG','image/png'))
        image_reads=[]
        async def setting(key,*values):
            if key=='trade_image:order':
                image_reads.append(key)
                return stored
            return None
        with patch('modules.new_trading.db.setting',AsyncMock(side_effect=setting)),patch('utils.trade_language.assign',AsyncMock(side_effect=lambda row,guild:dict(row,_languages=['English']))),patch('utils.trade_language.localize_embed',side_effect=lambda embed,row:embed),patch.object(images,'download',AsyncMock()) as download:
            for status in ('pending','pending','confirmed','paid','shipped','receipt_confirmed','releasing','completed','cancelled','refund_ready','refunded'):
                await cog._post(dict(id='order',status=status))
            download.assert_not_awaited()
        self.assertEqual(len(uploaded),2)
        self.assertEqual(len(image_reads),2)
        self.assertTrue(all(f.fp.closed for f in uploaded))
