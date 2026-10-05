"""Bounded, immutable item pictures. Never fetch user-entered URLs or paths."""
import asyncio
import base64
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit, unquote

import aiohttp
import discord
from PIL import Image, ImageOps
from utils.abuse_guard import TokenBucket

MAX_INPUT = 5 * 1024 * 1024
MAX_STORED = 512 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192
FORMATS = {'.jpg':'JPEG','.jpeg':'JPEG','.png':'PNG','.webp':'WEBP'}
MIMES = {'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}
_slots = asyncio.Semaphore(2)
_uploads = TokenBucket(3,60)


class ImageRejected(ValueError):
    def __init__(self, key='image_invalid'):
        self.key=key
        super().__init__(key)


def attachment_url(url, ident):
    try:
        if not isinstance(url,str) or len(url)>2048:raise ImageRejected()
        parsed=urlsplit(url)
        path=unquote(parsed.path)
        if (parsed.scheme!='https' or parsed.hostname!='cdn.discordapp.com'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None,443) or parsed.fragment
                or not re.fullmatch(r'/(?:ephemeral-attachments|attachments)/[0-9]+/'+str(ident)+r'/[^/\\\x00-\x20]+',path)
                or path.rsplit('/',1)[-1] in ('.','..')):
            raise ImageRejected()
        return url
    except (ValueError,TypeError):
        raise ImageRejected() from None


async def download(url):
    # Dedicated unauthenticated client: no bot token, proxies, redirects or decompression.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12,connect=5),
            trust_env=False,auto_decompress=False,headers={'Accept-Encoding':'identity'}) as session:
        async with session.get(url,allow_redirects=False) as response:
            if (response.status!=200 or response.headers.get('Content-Encoding','identity')!='identity'
                    or (response.content_length is not None and response.content_length>MAX_INPUT)):
                raise ImageRejected()
            data=bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(data)+len(chunk)>MAX_INPUT:raise ImageRejected('image_limits')
                data.extend(chunk)
            return bytes(data)


def normalize(data, expected, declared):
    if not data or len(data)>MAX_INPUT:raise ImageRejected('image_limits')
    try:
        with Image.open(BytesIO(data),formats=['JPEG','PNG','WEBP']) as image:
            if (image.format!=expected or declared not in (None,'',MIMES[image.format])
                    or getattr(image,'n_frames',1)!=1):
                raise ImageRejected()
            width,height=image.size
            if min(width,height)<1 or max(width,height)>MAX_SIDE or width*height>MAX_PIXELS:
                raise ImageRejected('image_limits')
            image.verify()
        with Image.open(BytesIO(data),formats=['JPEG','PNG','WEBP']) as image:
            image.load()  # Reject truncated images; never enable LOAD_TRUNCATED_IMAGES.
            oriented=ImageOps.exif_transpose(image)
            oriented.thumbnail((1280,1280),Image.Resampling.LANCZOS)
            # Fresh pixels discard EXIF/GPS, comments, ICC profiles and trailing payloads.
            with Image.new('RGB',oriented.size,'white') as clean:
                rgba=oriented.convert('RGBA')
                clean.paste(rgba,mask=rgba.getchannel('A'))
                rgba.close()
                for side in (1280,1024,800,640):
                    clean.thumbnail((side,side),Image.Resampling.LANCZOS)
                    for quality in (85,70,55):
                        output=BytesIO()
                        clean.save(output,format='JPEG',quality=quality)
                        result=output.getvalue()
                        if len(result)<=MAX_STORED:return result
                raise ImageRejected('image_limits')
    except ImageRejected:
        raise
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError):
        raise ImageRejected() from None


async def prepare(attachments, *, actor=None):
    if not attachments:return None
    if len(attachments)!=1:raise ImageRejected('image_limits')
    attachment=attachments[0]
    size=getattr(attachment,'size',None)
    ident=getattr(attachment,'id',None)
    suffix=PurePosixPath(getattr(attachment,'filename','')).suffix.lower()
    declared=getattr(attachment,'content_type',None)
    if type(size) is not int or not 0<size<=MAX_INPUT:raise ImageRejected('image_limits')
    if type(ident) is not int or ident<=0 or suffix not in FORMATS:raise ImageRejected()
    if declared not in (None,'',*MIMES.values()):raise ImageRejected()
    url=attachment_url(getattr(attachment,'url',''),ident)
    if _slots.locked():raise ImageRejected('image_busy')
    if actor is not None and not _uploads.allow(actor):raise ImageRejected('image_busy')
    async with _slots:
        try:
            data=await download(url)
            if len(data)!=size:raise ImageRejected()
            task=asyncio.create_task(asyncio.to_thread(normalize,data,FORMATS[suffix],declared))
            try:
                normalized=await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the slot until the bounded decoder stops, even on cancellation.
                try:await task
                except Exception:pass
                raise
            return {'version':1,'sha256':hashlib.sha256(normalized).hexdigest(),
                    'data':base64.b64encode(normalized).decode('ascii')}
        except ImageRejected:
            raise
        except (aiohttp.ClientError,asyncio.TimeoutError,TimeoutError):
            raise ImageRejected() from None


def file(asset):
    if asset is None:return None
    try:
        encoded=asset['data']
        if asset['version']!=1 or not isinstance(encoded,str) or len(encoded)>((MAX_STORED+2)//3)*4:
            raise ImageRejected()
        data=base64.b64decode(encoded,validate=True)
        if not data or len(data)>MAX_STORED or hashlib.sha256(data).hexdigest()!=asset['sha256']:
            raise ImageRejected()
        return discord.File(BytesIO(data),filename='trade-item.jpg')
    except (KeyError,TypeError,ValueError):
        raise ImageRejected() from None
