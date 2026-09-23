import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import copy
import time
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from utils import trade_review as r
from modules.new_trading import NewTrading

ADDRESS='0x1234567890123456789012345678901234567890'
RETURN='0x2234567890123456789012345678901234567890'


def proof():
    start=datetime(2026,9,20,tzinfo=timezone.utc)
    invoice={'address':ADDRESS,'created_at':start}
    deposit={'id':'d1','txId':'tx1','coin':'USDT','network':'BSC','status':1,'address':ADDRESS,'amount':'6','insertTime':r.milliseconds(start)+1000}
    withdrawal={'id':'w1','txId':'tx2','coin':'USDT','network':'BSC','status':6,'address':RETURN,'amount':'5.99','transactionFee':'.01','transferType':0,'applyTime':'2026-09-20 00:01:00','completeTime':'2026-09-20 00:02:00'}
    return invoice,deposit,withdrawal


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_refund_returns_exact_amount_less_fee(self):
        i,d,w=proof(); data=r.evidence(i,d,w,RETURN)
        self.assertEqual(data['received'],'6'); self.assertEqual(data['net'],'5.99')

    def test_incomplete_wrong_network_address_fee_time_rejected(self):
        i,d,w=proof()
        for field,value in [('status',4),('network','ETH'),('address',ADDRESS),('amount','5.98'),('transactionFee','NaN'),('transferType',1),('completeTime',None),('applyTime','2026-09-19 00:00:00')]:
            with self.subTest(field=field),self.assertRaises((ValueError,ArithmeticError)):
                r.evidence(i,d,{**w,field:value},RETURN)
        for change in ({'status':6},{'coin':'BTC'},{'address':RETURN},{'insertTime':1},{'amount':'5.9'}):
            with self.assertRaises(ValueError): r.evidence(i,{**d,**change},w,RETURN)

    def test_confirmation_binding_expiry_revision_and_single_use(self):
        data={'actor':1,'code':'1234','expires':time.time()+100,'status':'payment_review','revision':3}
        r.validate_confirmation(data,1,'1234','payment_review',3)
        for modified in ({'actor':2},{'code':'wrong'},{'expires':0},{'used':True},{'revision':4},{'status':'releasing'}):
            with self.assertRaises(ValueError): r.validate_confirmation({**data,**modified},1,'1234','payment_review',3)

    async def test_provider_verification_only_get_and_unique_records(self):
        i,d,w=proof()
        with patch.object(r.db,'query',AsyncMock(return_value=i)),patch.object(r.binance_api,'make_api_request',AsyncMock(side_effect=[[d],[w]])) as api:
            await r.verify_manual('order','tx1','w1',RETURN)
            self.assertTrue(all(c.args[1]=='GET' for c in api.await_args_list))
        with patch.object(r.db,'query',AsyncMock(return_value=i)),patch.object(r.binance_api,'make_api_request',AsyncMock(side_effect=[[d,d],[w]])):
            with self.assertRaises(ValueError): await r.verify_manual('order','tx1','w1',RETURN)


class CloseTests(unittest.IsolatedAsyncioTestCase):
    def setup_cog(self,phase='unnotified'):
        cog=object.__new__(NewTrading); cog.cleanup_lock=asyncio.Lock()
        row={'id':'order','status':'manual_refunded','buyer_id':1,'seller_id':2,'channel_id':8}
        cog.order=AsyncMock(return_value=row); cog.alert=AsyncMock()
        channel=NS(id=8,guild=NS(id=2),send=AsyncMock(return_value=NS(id=99)),delete=AsyncMock())
        cog.bot=NS(get_channel=lambda _:channel,user=NS(id=9))
        self.state={'phase':phase,'generation':'g1','deadline':time.time()+1800,'details':{'net':'5.99','fee':'.01','address':RETURN,'withdrawal':{'txId':'tx2'}}}
        self.held=phase=='unnotified'
        async def setting(key,value=None):
            if value is not None: self.state=copy.deepcopy(value)
            return copy.deepcopy(self.state)
        async def query(sql,args=(),**kw):
            if sql.startswith('SELECT hold'): return {'hold':self.held}
            if 'hold=FALSE' in sql: self.held=False
            elif 'hold=%s' in sql: self.held=args[0]
        async def save(row,state,hold,*args):
            self.state=copy.deepcopy(state)
            self.held=hold
        cog.save_manual_close=AsyncMock(side_effect=save)
        self.patches=[patch('modules.new_trading.db.setting',AsyncMock(side_effect=setting)),patch('modules.new_trading.db.query',AsyncMock(side_effect=query)),patch('modules.new_trading.db.audit',AsyncMock())]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        return cog,channel

    def inter(self,user=1,channel=8):
        return NS(user=NS(id=user),channel_id=channel,channel=NS(),response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))

    async def test_notify_then_wait_no_reset_and_restart_expires(self):
        cog,channel=self.setup_cog()
        await cog.manual_close_tick('order')
        deadline=self.state['deadline'];self.assertEqual(self.state['phase'],'waiting')
        self.assertAlmostEqual(deadline-time.time(),1800,delta=2)
        await cog.manual_close_tick('order')
        channel.send.assert_awaited_once();self.assertEqual(deadline,self.state['deadline'])
        self.state['deadline']=0
        restarted=object.__new__(NewTrading); restarted.cleanup_lock=asyncio.Lock(); restarted.order=cog.order;restarted.bot=cog.bot
        await restarted.manual_close_tick('order')
        channel.delete.assert_awaited_once();self.assertEqual(self.state['phase'],'deleted')

    async def test_failed_notice_does_not_arm(self):
        cog,channel=self.setup_cog();channel.send.side_effect=RuntimeError('network')
        with self.assertRaises(RuntimeError): await cog.manual_close_tick('order')
        self.assertEqual(self.state['phase'],'unnotified');self.assertTrue(self.held)
        channel.delete.assert_not_awaited()

    async def test_refusal_prevents_delete_even_past_deadline(self):
        cog,channel=self.setup_cog('waiting');self.state['deadline']=0
        await cog.refund_close_response(self.inter(),'new:refund_close:hold:order:g1')
        await cog.manual_close_tick('order')
        self.assertEqual(self.state['phase'],'held');self.assertTrue(self.held)
        channel.delete.assert_not_awaited();cog.alert.assert_awaited_once()

    async def test_only_buyer_current_channel_current_generation(self):
        cog,channel=self.setup_cog('waiting')
        for inter,code in [(self.inter(user=2),'g1'),(self.inter(channel=999),'g1'),(self.inter(),'old')]:
            await cog.refund_close_response(inter,'new:refund_close:accept:order:'+code)
        self.assertEqual(self.state['phase'],'waiting');channel.delete.assert_not_awaited()

    async def test_confirmation_deletes_once_no_transfer(self):
        cog,channel=self.setup_cog('waiting')
        with patch('modules.new_trading.payments.release',AsyncMock()) as release:
            await cog.refund_close_response(self.inter(),'new:refund_close:accept:order:g1')
            await cog.refund_close_response(self.inter(),'new:refund_close:accept:order:g1')
            release.assert_not_awaited()
        channel.delete.assert_awaited_once();self.assertEqual(self.state['phase'],'deleted')

    async def test_admin_hold_blocks_expiry(self):
        cog,channel=self.setup_cog('waiting');self.held=True;self.state['deadline']=0
        await cog.manual_close_tick('order');channel.delete.assert_not_awaited()

    async def test_manual_refund_has_no_payment_or_collection_buttons(self):
        cog=object.__new__(NewTrading)
        self.assertEqual(cog.view({'id':'order','status':'manual_refunded'}).children,[])

class ConfirmationTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def run_confirm(self,*,payout=None,claimed=None,withdraw_used=None,conflict=None,stored_deposit=None,revision=0):
        from contextlib import asynccontextmanager
        import json
        from decimal import Decimal as D
        i,d,w=proof(); i.update(amount=D('6.001234'),deposit_id=None,state='waiting')
        verified=r.evidence(i,d,w,RETURN)
        pending={'actor':99,'decision':'登记手动退款','reason':'verified owner and address','status':'payment_review','revision':0,'expires':time.time()+100,'code':'1234','manual':{'deposit_txid':'tx1','withdrawal_id':'w1','address':RETURN,'evidence':verified}}
        row={'status':'payment_review','credits':D(2),'buyer_id':1,'channel_id':8}
        responses=[{'value':''},payout,row,{'value':json.dumps(pending)},{'revision':revision},i,stored_deposit,claimed,conflict,withdraw_used,None,None]
        cursor=NS(execute=AsyncMock(),fetchone=AsyncMock(side_effect=responses),rowcount=1)
        outcome=[]
        @asynccontextmanager
        async def transaction(shared=False):
            self.assertTrue(shared)
            try: yield cursor
            except Exception: outcome.append('rollback');raise
            else: outcome.append('commit')
        self.cursor=cursor; self.outcome=outcome
        with patch.object(r.db,'setting',AsyncMock(return_value=pending)),patch.object(r.db,'transaction',transaction),patch.object(r,'verify_manual',AsyncMock(return_value=verified)),patch.object(r.binance_api,'make_api_request',AsyncMock()) as api:
            try: return await r.confirm('order',99,'1234')
            finally: api.assert_not_awaited()

    async def test_atomic_shared_ledger_and_credit_return_no_transfer(self):
        self.assertEqual(await self.run_confirm(),'manual_refunded')
        self.assertEqual(self.outcome,['commit'])
        calls=self.cursor.execute.await_args_list
        sql='\n'.join(c.args[0] for c in calls)
        self.assertIn('INSERT INTO deposits',sql)
        self.assertIn('INSERT INTO payouts',sql)
        self.assertIn('reserved=reserved-',sql)
        self.assertIn('available=available+',sql)
        self.assertIn('manual_close:',str(calls))
        self.assertIn('"used": true',str(calls))

    async def test_any_prior_payout_stops_before_changes(self):
        for status in ('submitting','unknown','failed','completed','manual_refunded'):
            with self.assertRaises(ValueError): await self.run_confirm(payout={'state':status})
            self.assertEqual(self.outcome,['rollback'])
            self.assertFalse(any(c.args[0].startswith('UPDATE ') or 'INSERT INTO payouts' in c.args[0] for c in self.cursor.execute.await_args_list))

    async def test_deposit_bound_to_another_order_rejected(self):
        with self.assertRaises(ValueError): await self.run_confirm(claimed={'order_key':'new:other'})
        self.assertEqual(self.outcome,['rollback'])

    async def test_already_different_deposit_rejected(self):
        with self.assertRaises(ValueError): await self.run_confirm(stored_deposit={'id':'other'})
        self.assertEqual(self.outcome,['rollback'])

    async def test_withdrawal_reuse_rejected(self):
        with self.assertRaises(ValueError): await self.run_confirm(withdraw_used={'value':'new:other'})
        self.assertEqual(self.outcome,['rollback'])

    async def test_amount_matching_other_invoice_rejected(self):
        with self.assertRaises(ValueError): await self.run_confirm(conflict={'order_key':'new:other'})
        self.assertEqual(self.outcome,['rollback'])

    async def test_revision_change_rejected_inside_transaction(self):
        with self.assertRaises(ValueError): await self.run_confirm(revision=1)
        self.assertEqual(self.outcome,['rollback'])
