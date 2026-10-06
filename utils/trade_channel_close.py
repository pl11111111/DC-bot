"""Close retained settled channels without changing a settlement or moving funds."""
import json
import secrets
import time
from utils import new_store as db

SETTLED=('completed','refunded','test_closed')


class CloseRejected(ValueError):
    def __init__(self,key):
        self.key=key
        super().__init__(key)


async def locked(cur,ident):
    await cur.execute('SELECT * FROM orders WHERE id=%s FOR UPDATE',(ident,))
    row=await cur.fetchone()
    if not row or row['status'] not in SETTLED or not row.get('channel_id'):
        raise CloseRejected('channel_close_unavailable')
    await cur.execute('SELECT kind,hold FROM tracked_channels WHERE channel_id=%s FOR UPDATE',(row['channel_id'],))
    tracked=await cur.fetchone()
    await cur.execute('SELECT value FROM settings WHERE setting_key=%s',('channel_deleted:'+ident,))
    if not tracked or tracked['kind']!='trade' or not tracked['hold'] or await cur.fetchone():
        raise CloseRejected('channel_close_unavailable')
    return row


async def save(cur,key,value):
    encoded=db.encode(value)
    await cur.execute('INSERT INTO settings(setting_key,value) VALUES(%s,%s) ON DUPLICATE KEY UPDATE value=%s',(key,encoded,encoded))


async def prepare(ident,actor,reason):
    if not reason.strip() or len(reason)>500:raise CloseRejected('reason_required')
    async with db.transaction() as cur:
        row=await locked(cur,ident)
        await cur.execute('SELECT value FROM settings WHERE setting_key=%s',('channel_close:'+ident,))
        current=await cur.fetchone()
        if current and json.loads(current['value']).get('phase')=='queued':
            raise CloseRejected('channel_close_pending')
        await cur.execute('SELECT COALESCE(MAX(id),0) AS revision FROM audit WHERE order_id=%s',(ident,))
        revision=(await cur.fetchone())['revision']
        data=dict(actor=actor,status=row['status'],channel=row['channel_id'],reason=reason.strip(),
                  revision=revision,expires=time.time()+300,code=secrets.token_hex(16),used=False)
        await save(cur,'channel_close_preview:'+ident,data)
    return data


async def confirm(ident,actor,code):
    async with db.transaction() as cur:
        row=await locked(cur,ident)
        await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE',('channel_close_preview:'+ident,))
        stored=await cur.fetchone()
        data=json.loads(stored['value']) if stored else None
        await cur.execute('SELECT COALESCE(MAX(id),0) AS revision FROM audit WHERE order_id=%s',(ident,))
        revision=(await cur.fetchone())['revision']
        if (not data or data.get('used') or data['actor']!=actor or time.time()>data['expires']
                or not secrets.compare_digest(data['code'],str(code)) or data['status']!=row['status']
                or data['channel']!=row['channel_id'] or data['revision']!=revision):
            raise CloseRejected('preview_stale')
        data['used']=True
        await save(cur,'channel_close_preview:'+ident,data)
        state=dict(phase='queued',generation=secrets.token_hex(16),actor=actor,reason=data['reason'],
                   status=row['status'],channel=row['channel_id'])
        await save(cur,'channel_close:'+ident,state)
        await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (actor,'channel_close_requested',db.encode(state),ident))
        # Retain until the notification has actually been sent; no financial writes.
    return state


async def activate(ident,generation):
    async with db.transaction() as cur:
        row=await locked(cur,ident)
        await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE',('channel_close:'+ident,))
        stored=await cur.fetchone()
        state=json.loads(stored['value']) if stored else None
        if (not state or state.get('phase')!='queued' or state.get('generation')!=generation
                or state['status']!=row['status'] or state['channel']!=row['channel_id']):
            raise CloseRejected('preview_stale')
        state.update(phase='waiting',announced_at=time.time())
        await save(cur,'channel_close:'+ident,state)
        await cur.execute('UPDATE tracked_channels SET hold=FALSE WHERE channel_id=%s',(row['channel_id'],))
        await cur.execute('INSERT INTO audit(actor_id,action,details,order_id) VALUES(%s,%s,%s,%s)',
                          (state['actor'],'channel_close_announced',db.encode(state),ident))


async def cancel(ident):
    existing=await db.setting('channel_close:'+ident)
    if not existing or existing.get('phase') not in ('queued','waiting'):return
    async with db.transaction() as cur:
        await cur.execute('SELECT id FROM orders WHERE id=%s FOR UPDATE',(ident,))
        await cur.execute('SELECT value FROM settings WHERE setting_key=%s FOR UPDATE',('channel_close:'+ident,))
        stored=await cur.fetchone()
        if stored:
            state=json.loads(stored['value'])
            if state.get('phase') in ('queued','waiting'):
                state['phase']='cancelled'
                await save(cur,'channel_close:'+ident,state)
