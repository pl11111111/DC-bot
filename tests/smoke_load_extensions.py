import os
os.environ.update(DISCORD_TOKEN='offline-test',GUILD_ID='1',NEW_GUILD_ID='2',BINANCE_API_KEY='offline-test',BINANCE_API_SECRET='offline-test')
import asyncio
import unittest
import discord
from utils.guild_isolation import IsolatedBot

class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_load_without_connecting(self):
        import main
        bot=IsolatedBot(intents=discord.Intents.none())
        for name in main.COGS_TO_LOAD: bot.load_extension(name)
        self.assertNotIn('Trading',bot.cogs)
        self.assertNotIn('Rental',bot.cogs)
        self.assertNotIn('PaymentRecovery',bot.cogs)
        self.assertIn('NewModeration',bot.cogs)
        self.assertIn('NewTrading',bot.cogs)
        self.assertIn('NewCommunity',bot.cogs)
        right_click=[c for c in bot.pending_application_commands if c.name in ('开始交易(buy)','开始交易(sell)')]
        self.assertEqual(len(right_click),2)
        self.assertTrue(all(c.guild_ids==[2] for c in right_click))
        for command in bot.pending_application_commands:
            self.assertEqual(command.contexts, {discord.InteractionContextType.guild})
            self.assertTrue(command.guild_ids)
        for command in bot.pending_application_commands:
            module=getattr(getattr(command,'callback',None),'__module__','') or getattr(getattr(command,'cog',None),'__module__','')
            if module.startswith('modules.new_'):
                self.assertEqual(command.guild_ids,[2],command.name)
            elif module.startswith('modules.'):
                self.assertEqual(command.guild_ids,[1],command.name)
        names={c.name for c in bot.pending_application_commands if c.guild_ids==[2]}
        self.assertNotIn('new_trade_close_test',names)
        self.assertNotIn('new_log_release',names)
        self.assertNotIn('new_trade_review',names)
        trade=next(c for c in bot.pending_application_commands if c.name=='new_trade')
        self.assertEqual({c.name for c in trade.subcommands},{'review'})
        self.assertNotIn('modules.party',main.COGS_TO_LOAD)
        self.assertNotIn('party',{c.name for c in bot.pending_application_commands})
        for command in bot.pending_application_commands:
            if command.guild_ids==[2] and isinstance(command,(discord.SlashCommand,discord.SlashCommandGroup)):
                self.assertTrue(command.default_member_permissions.administrator)
        self.assertNotIn('查询积分',names)
        self.assertTrue({'new_panel','new_forum_rules','new_notice'}<=names)
        for name in list(bot.extensions): bot.unload_extension(name)
        await bot.close()
        # Existing legacy modules create tasks outside Cog cleanup. Cancel in this test only.
        current=asyncio.current_task()
        pending=[t for t in asyncio.all_tasks() if t is not current]
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
