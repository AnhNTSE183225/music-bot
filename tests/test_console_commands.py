import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot as musicbot


class FakeVoiceClient:
    def __init__(self, channel=None, connected=True):
        self.channel = channel
        self._connected = connected

    def is_connected(self):
        return self._connected


class FakeMember:
    def __init__(self, user_id, display_name="Console User", voice=None):
        self.id = user_id
        self.display_name = display_name
        self.name = display_name
        self.voice = voice
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.guild_permissions = SimpleNamespace(administrator=False)

    def __str__(self):
        return self.display_name


class FakeGuild:
    def __init__(self, name, member=None, voice_client=None):
        self.name = name
        self._member = member
        self.voice_client = voice_client

    def get_member(self, user_id):
        if self._member and self._member.id == user_id:
            return self._member
        return None


class TestConsoleCommands(unittest.IsolatedAsyncioTestCase):
    async def test_owner_voice_is_preferred(self):
        owner_id = 368435858950324235
        connected_guild = FakeGuild(
            "Active Guild",
            member=FakeMember(owner_id, voice=None),
            voice_client=FakeVoiceClient(channel=SimpleNamespace(name="Lounge"), connected=True),
        )
        voice_guild = FakeGuild(
            "Voice Guild",
            member=FakeMember(owner_id, voice=SimpleNamespace(channel=SimpleNamespace(name="Studio"))),
            voice_client=None,
        )

        with patch.object(musicbot.settings, "CONSOLE_USER_ID", owner_id), patch.object(
            musicbot.bot, "_connection", SimpleNamespace(guilds=[voice_guild, connected_guild])
        ):
            guild, author = await musicbot.resolve_console_target()

        self.assertIs(guild, voice_guild)
        self.assertEqual(author.id, owner_id)
        self.assertEqual(author.voice.channel.name, "Studio")
        self.assertTrue(author.guild_permissions.administrator)

    async def test_connected_guild_is_used_when_owner_has_no_voice_state(self):
        owner_id = 368435858950324235
        connected_guild = FakeGuild(
            "Active Guild",
            member=FakeMember(owner_id, voice=None),
            voice_client=FakeVoiceClient(channel=SimpleNamespace(name="Lounge"), connected=True),
        )

        with patch.object(musicbot.settings, "CONSOLE_USER_ID", owner_id), patch.object(
            musicbot.bot, "_connection", SimpleNamespace(guilds=[connected_guild])
        ):
            guild, author = await musicbot.resolve_console_target()

        self.assertIs(guild, connected_guild)
        self.assertEqual(author.voice.channel.name, "Lounge")
        self.assertTrue(author.guild_permissions.administrator)


if __name__ == "__main__":
    unittest.main()