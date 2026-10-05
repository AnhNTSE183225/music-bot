import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import settings


class TestRecoveryLogic(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        bot.music_state_by_guild.clear()
        bot.voice_recovery_tasks.clear()

    def tearDown(self):
        bot.music_state_by_guild.clear()
        for task in bot.voice_recovery_tasks.values():
            if not task.done():
                task.cancel()
        bot.voice_recovery_tasks.clear()

    def test_get_channel_listener_count(self):
        channel = MagicMock()
        bot_user = MagicMock(bot=True)
        human_user1 = MagicMock(bot=False)
        human_user2 = MagicMock(bot=False)

        channel.members = [bot_user, human_user1, human_user2]
        self.assertEqual(bot.get_channel_listener_count(channel), 2)

        channel.members = [bot_user]
        self.assertEqual(bot.get_channel_listener_count(channel), 0)

        self.assertEqual(bot.get_channel_listener_count(None), 0)

    def test_clear_music_state_marks_manual_stop(self):
        guild = MagicMock(id=123)
        state = bot.get_music_state(guild)
        state['queue'].append({'queue_id': 1, 'title': 'Test'})
        state['voice_channel_id'] = 456
        state['is_playing'] = True
        state['manual_stop'] = False

        bot.clear_music_state(guild)

        self.assertEqual(len(state['queue']), 0)
        self.assertIsNone(state['voice_channel_id'])
        self.assertFalse(state['is_playing'])
        self.assertFalse(state['was_playing'])
        self.assertTrue(state['manual_stop'])

    def test_save_and_load_state_staleness(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tf:
            temp_path = tf.name

        try:
            with patch.object(settings, 'STATE_FILE', temp_path):
                guild = MagicMock(id=999)
                state = bot.get_music_state(guild)
                state['queue'] = [{'queue_id': 1, 'title': 'Old Track', 'data': 'http://old', 'type': 'youtube'}]
                state['queue_index'] = 0
                state['voice_channel_id'] = 888
                state['is_playing'] = True
                state['manual_stop'] = False
                state['saved_at'] = time.time() - 3600  # 1 hour ago (stale!)

                bot.save_state_to_disk()

                # Clear state from memory
                bot.music_state_by_guild.clear()

                # Load back from disk
                bot.load_state_from_disk()

                loaded_state = bot.music_state_by_guild.get(999)
                self.assertIsNotNone(loaded_state)
                # Queue is preserved for user convenience
                self.assertEqual(len(loaded_state['queue']), 1)
                # But auto-resume properties are cleared!
                self.assertFalse(loaded_state['was_playing'])
                self.assertIsNone(loaded_state['voice_channel_id'])
                self.assertTrue(loaded_state['manual_stop'])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_load_state_recent_preserves_playback_flag(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tf:
            temp_path = tf.name

        try:
            with patch.object(settings, 'STATE_FILE', temp_path):
                guild = MagicMock(id=777)
                state = bot.get_music_state(guild)
                state['queue'] = [{'queue_id': 1, 'title': 'Recent Track', 'data': 'http://recent', 'type': 'youtube'}]
                state['queue_index'] = 0
                state['voice_channel_id'] = 555
                state['is_playing'] = True
                state['manual_stop'] = False
                state['saved_at'] = time.time() - 10  # 10 seconds ago (fresh/transient drop)

                bot.save_state_to_disk()
                bot.music_state_by_guild.clear()
                bot.load_state_from_disk()

                loaded_state = bot.music_state_by_guild.get(777)
                self.assertIsNotNone(loaded_state)
                self.assertTrue(loaded_state['was_playing'])
                self.assertEqual(loaded_state['voice_channel_id'], 555)
                self.assertFalse(loaded_state['manual_stop'])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    async def test_ensure_voice_recovery_aborts_on_manual_stop(self):
        guild_id = 111
        state = {
            'queue': [{'queue_id': 1, 'title': 'Track'}],
            'voice_channel_id': 222,
            'is_playing': False,
            'manual_stop': True,
        }
        bot.music_state_by_guild[guild_id] = state

        bot.ensure_voice_recovery_task(guild_id)
        self.assertNotIn(guild_id, bot.voice_recovery_tasks)

    async def test_ensure_voice_recovery_aborts_when_no_listeners(self):
        guild_id = 222
        state = {
            'queue': [{'queue_id': 1, 'title': 'Track'}],
            'voice_channel_id': 333,
            'text_channel_id': 444,
            'is_playing': True,
            'manual_stop': False,
        }
        bot.music_state_by_guild[guild_id] = state

        mock_guild = MagicMock()
        mock_channel = MagicMock()
        mock_channel.members = [MagicMock(bot=True)]  # only bot
        mock_channel.name = "General"
        mock_guild.get_channel.return_value = mock_channel
        mock_guild.voice_client = None

        with patch.object(bot.bot, 'get_guild', return_value=mock_guild), \
             patch.object(settings, 'VOICE_RECONNECT_INTERVAL', 0.01):
            bot.ensure_voice_recovery_task(guild_id)
            task = bot.voice_recovery_tasks.get(guild_id)
            self.assertIsNotNone(task)
            await task

            # Channel connect should NEVER have been called because listeners == 0
            mock_channel.connect.assert_not_called()

    async def test_check_and_resume_all_sessions_ignores_manual_stop(self):
        guild_id = 333
        state = {
            'queue': [{'queue_id': 1, 'title': 'Track'}],
            'voice_channel_id': 444,
            'was_playing': True,
            'manual_stop': True,
            'saved_at': time.time(),
        }
        bot.music_state_by_guild[guild_id] = state

        with patch.object(bot.bot, 'get_guild') as mock_get_guild:
            await bot.check_and_resume_all_sessions()
            mock_get_guild.assert_not_called()

    async def test_check_and_resume_all_sessions_ignores_stale_sessions(self):
        guild_id = 444
        state = {
            'queue': [{'queue_id': 1, 'title': 'Track'}],
            'voice_channel_id': 555,
            'was_playing': True,
            'manual_stop': False,
            'saved_at': time.time() - 3600,  # 1 hour ago
        }
        bot.music_state_by_guild[guild_id] = state

        with patch.object(bot.bot, 'get_guild') as mock_get_guild:
            await bot.check_and_resume_all_sessions()
            mock_get_guild.assert_not_called()

    async def test_check_and_resume_all_sessions_ignores_empty_voice_channel(self):
        guild_id = 555
        state = {
            'queue': [{'queue_id': 1, 'title': 'Track'}],
            'voice_channel_id': 666,
            'was_playing': True,
            'manual_stop': False,
            'saved_at': time.time() - 10,  # fresh
        }
        bot.music_state_by_guild[guild_id] = state

        mock_guild = MagicMock()
        mock_channel = MagicMock()
        mock_channel.members = [MagicMock(bot=True)]  # 0 humans
        mock_channel.name = "Music"
        mock_guild.get_channel.return_value = mock_channel
        mock_guild.voice_client = None

        with patch.object(bot.bot, 'get_guild', return_value=mock_guild):
            await bot.check_and_resume_all_sessions()
            mock_channel.connect.assert_not_called()

    async def test_stop_command_clears_state_and_cancels_recovery(self):
        mock_ctx = MagicMock()
        mock_ctx.guild = MagicMock(id=666)
        mock_ctx.voice_client = MagicMock()
        mock_ctx.voice_client.stop = MagicMock()
        mock_ctx.voice_client.disconnect = AsyncMock()
        mock_ctx.send = AsyncMock()

        state = bot.get_music_state(mock_ctx.guild)
        state['queue'] = [{'queue_id': 1, 'title': 'Track'}]
        state['voice_channel_id'] = 777
        state['is_playing'] = True
        state['manual_stop'] = False

        # Add a mock recovery task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        bot.voice_recovery_tasks[666] = mock_task

        with patch('bot.enforce_command_access', AsyncMock(return_value=True)), \
             patch('bot.clear_bot_status_if_idle', AsyncMock()):
            await bot.stop(mock_ctx)

        mock_task.cancel.assert_called()
        self.assertNotIn(666, bot.voice_recovery_tasks)
        self.assertTrue(state['manual_stop'])
        self.assertFalse(state['is_playing'])
        self.assertIsNone(state['voice_channel_id'])
        self.assertEqual(len(state['queue']), 0)
        mock_ctx.voice_client.stop.assert_called_once()
        mock_ctx.voice_client.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
