"""Security regressions: no live Discord, payment or database access."""
import os
os.environ.update(DISCORD_TOKEN='offline-test', GUILD_ID='1', NEW_GUILD_ID='2',
                  BINANCE_API_KEY='offline-test', BINANCE_API_SECRET='offline-test')
import hashlib
import hmac
import logging
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import discord
from utils import binance_api as api, shared_payments as payments
from utils.security import PRIVILEGED_PERMISSIONS, RedactingFormatter
from modules.new_community import safe_self_role


class Role:
    id = 123
    managed = False
    def __init__(self, permissions): self.permissions = permissions
    def is_default(self): return False
    def __lt__(self, other): return True


class RoleAndLogTests(unittest.TestCase):
    def test_everyone_cannot_become_an_application_administrator(self):
        from modules.new_community import admin
        member = NS(guild_permissions=NS(administrator=False), roles=[NS(id=2)])
        self.assertFalse(admin(member, [2]))
        member.roles.append(NS(id=123))
        self.assertTrue(admin(member, [123]))

    def test_domain_substrings_mixed_links_and_fake_images_cannot_bypass(self):
        from utils.link_safety import has_restricted_link
        for text in ('https://evil.test/?youtube.com', 'https://youtube.com.evil.test/',
                     'https://youtube.com@evil.test/', 'youtube.com https://evil.test/',
                     'https://youtube.com https://evil.test/', 'https://evil.test/file.png',
                     'www.evil.test', 'ftp://youtube.com/file', 'https://[invalid'):
            with self.subTest(text=text):
                self.assertTrue(has_restricted_link(text, ['youtube.com'], True))
        for text in ('normal message', 'https://youtube.com/watch?v=123',
                     'https://www.youtube.com/watch?v=123',
                     'https://cdn.discordapp.com/attachments/test.png?token=123'):
            self.assertFalse(has_restricted_link(text, ['youtube.com'], True))

    def test_every_privileged_permission_is_blocked_for_self_service(self):
        guild = NS(me=NS(top_role=object()))
        self.assertTrue(safe_self_role(Role(discord.Permissions.none()), guild))
        for name in PRIVILEGED_PERMISSIONS:
            if name not in discord.Permissions.VALID_FLAGS: continue
            with self.subTest(permission=name):
                permissions = discord.Permissions.none()
                setattr(permissions, name, True)
                self.assertFalse(safe_self_role(Role(permissions), guild))

    def test_missing_bot_member_cannot_assign_role(self):
        self.assertFalse(safe_self_role(Role(discord.Permissions.none()), NS(me=None)))

    def test_formatter_redacts_exception_and_signed_interaction_urls(self):
        formatter = RedactingFormatter('%(message)s')
        secret = 'unit-test-secret-do-not-log'
        try:
            raise ValueError('https://example.test/?signature=signature-data&foo=1 ' + secret)
        except ValueError:
            record = logging.LogRecord('test', logging.ERROR, '', 1,
                'https://discord.com/api/webhooks/123/webhook-secret ' +
                'https://discord.com/api/interactions/123/interaction-secret/callback', (), sys.exc_info())
        with patch.dict(os.environ, BINANCE_API_SECRET=secret):
            result = formatter.format(record)
        for sensitive in (secret, 'signature-data', 'webhook-secret', 'interaction-secret'):
            self.assertNotIn(sensitive, result)
        self.assertIn('foo=1', result)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def session(self, status=200, body=b'{"id":"ok"}'):
        async def chunks(size):
            yield body
        response = NS(status=status, content=NS(iter_chunked=chunks))
        @asynccontextmanager
        async def request(*args, **kwargs):
            yield response
        session = NS(request=Mock(side_effect=request))
        @asynccontextmanager
        async def factory(**kwargs): yield session
        return factory, session

    async def test_signed_request_is_exact_and_does_not_mutate_or_redirect(self):
        factory, session = self.session()
        params = {'address':'0xabc', 'name':'space + amp&', 'signature':'stale'}
        before = dict(params)
        with patch.object(api.aiohttp, 'ClientSession', factory):
            self.assertEqual(await api.make_api_request('/sapi/v1/capital/withdraw/apply', 'POST', params), {'id':'ok'})
        self.assertEqual(params, before)
        call = session.request.call_args
        self.assertFalse(call.kwargs['allow_redirects'])
        query = str(call.args[1]).split('?',1)[1]
        signed, signature = query.rsplit('&signature=',1)
        expected = hmac.new(api.config.BINANCE_API_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertIn('name=space+%2B+amp%26', query)
        session.request.assert_called_once()

    async def test_redirect_and_response_body_are_not_logged_or_used(self):
        factory, session = self.session(302, b'provider-secret')
        with patch.object(api.aiohttp, 'ClientSession', factory), self.assertLogs(api.logger, level='ERROR') as captured:
            self.assertIsNone(await api.make_api_request('/sapi/v1/capital/config/getall'))
        self.assertNotIn('provider-secret', '\n'.join(captured.output))
        session.request.assert_called_once()

    async def test_network_error_logs_no_signed_url_and_never_retries(self):
        with patch.object(api.aiohttp, 'ClientSession', side_effect=RuntimeError('signature=do-not-log')) as factory:
            with self.assertLogs(api.logger, level='ERROR') as captured:
                self.assertIsNone(await api.make_api_request('/sapi/v1/capital/withdraw/apply', 'POST'))
        self.assertNotIn('do-not-log', '\n'.join(captured.output))
        factory.assert_called_once()

    async def test_invalid_endpoint_never_creates_session(self):
        with patch.object(api.aiohttp, 'ClientSession') as factory:
            for endpoint in ('https://example.test/', '//example.test/', '/api/v3/x?redirect=1', '/api/x\r\nHeader:x'):
                with self.assertRaises(ValueError): await api.make_api_request(endpoint)
            factory.assert_not_called()

    async def test_retired_fund_release_entrypoints_cannot_transfer(self):
        with patch.object(api, 'make_api_request', AsyncMock()) as request:
            self.assertIsNone(await api.withdraw_funds('USDT','0x'+'a'*40,10))
            self.assertFalse((await api.release_escrow_payment(1,'0x'+'a'*40,10))[0])
        request.assert_not_awaited()


class PaymentValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_network_limits_fail_closed(self):
        base = dict(withdrawEnable=True, network='BSC', withdrawFee='.01',
                    withdrawMin='3', withdrawIntegerMultiple='.000001')
        for invalid in ({'withdrawFee':'-1'}, {'withdrawFee':'NaN'},
                        {'withdrawIntegerMultiple':'0'}, {'withdrawMin':'-1'},
                        {'withdrawMax':'1'}, {'withdrawEnable':'true'}, {'withdrawTag':True}):
            data = [{'coin':'USDT','networkList':[{**base,**invalid}]}]
            with patch.object(api,'make_api_request',AsyncMock(return_value=data)):
                with self.assertRaises(ValueError): await payments.payout_amount_quote(10)

    async def test_missing_or_out_of_window_deposit_time_is_not_claimed(self):
        now = datetime.now(timezone.utc)
        inv = dict(address='address', amount=10, created_at=now.replace(tzinfo=None))
        base = dict(coin='USDT',network='BSC',address='address',amount='10',status=1,id='id',txId='tx')
        for timestamp in (None, 1, '123', int(now.timestamp()*1000)+100000, True):
            with patch.object(payments,'require_ready',AsyncMock()), \
                 patch.object(payments.db,'query',AsyncMock(side_effect=[inv,None])), \
                 patch.object(api,'make_api_request',AsyncMock(return_value=[{**base,'insertTime':timestamp}])), \
                 patch.object(payments.db,'transaction') as transaction:
                with self.assertRaises(ValueError): await payments.find_deposit('new:test','address',10)
                transaction.assert_not_called()


class GuildBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncached_legacy_message_edits_are_moderated(self):
        from modules.market import Market
        message = NS()
        channel = NS(fetch_message=AsyncMock(return_value=message))
        cog = object.__new__(Market)
        cog.bot = NS(get_channel=Mock(return_value=channel))
        cog.on_message = AsyncMock()
        payload = NS(guild_id=1,channel_id=77,message_id=88,data={'content':'https://evil.test'})
        await cog.on_raw_message_edit(payload)
        cog.on_message.assert_awaited_once_with(message)
        payload.guild_id = 2
        await cog.on_raw_message_edit(payload)
        channel.fetch_message.assert_awaited_once()

    async def test_cross_guild_giveaway_button_never_reads_or_writes_database(self):
        from modules.giveaway import GiveawayView, GiveawayAdminView
        from utils import database
        interaction = NS(guild=NS(id=2),response=NS(send_message=AsyncMock()))
        with patch.object(database,'get_pool',AsyncMock()) as pool:
            await GiveawayView(NS(),1,datetime.now()).join_button_callback(interaction)
            await GiveawayAdminView(NS(),1).settle(interaction,'refunded')
            pool.assert_not_awaited()
        self.assertEqual(interaction.response.send_message.await_count,2)
