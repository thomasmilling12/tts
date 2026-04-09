from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from ctypes.util import find_library
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from gtts import gTTS

SETTINGS_FILE = Path("diff_data/tts_settings.json")


def load_opus_auto() -> None:
    if discord.opus.is_loaded():
        return

    found = find_library("opus")
    if found:
        try:
            discord.opus.load_opus(found)
            print(f"[TTS] Opus loaded: {found}")
            return
        except Exception:
            pass

    for name in ("opus", "libopus-0", "libopus", "opus-0", "libopus.so.0"):
        try:
            discord.opus.load_opus(name)
            print(f"[TTS] Opus loaded: {name}")
            return
        except Exception:
            pass

    try:
        import pyogg

        pyogg_dir = Path(pyogg.__file__).parent
        for dll in pyogg_dir.rglob("*opus*.dll"):
            try:
                discord.opus.load_opus(str(dll))
                print(f"[TTS] Opus loaded from PyOgg: {dll}")
                return
            except Exception:
                pass
    except ImportError:
        pass

    print("[TTS] Warning: could not load opus. Voice playback may not work.")


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        print("[TTS] Warning: ffmpeg not found. TTS audio will not play.")


def default_settings() -> dict:
    return {
        "tts_enabled": True,
        "no_mic_channel_id": None,
        "same_vc_required": True,
        "smart_filter": True,
        "ignored_users": [],
        "say_name": True,
        "language": "en",
        "max_length": 300,
        "idle_timeout": 300,
    }


class TTSCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_settings: dict[int, dict] = {}
        self.guild_locks: dict[int, asyncio.Lock] = {}
        self.guild_last_spoke: dict[int, float] = {}
        load_opus_auto()
        check_ffmpeg()
        self.load_settings()
        self.idle_check.start()

    def cog_unload(self) -> None:
        self.idle_check.cancel()

    def get_guild_settings(self, guild_id: int) -> dict:
        if guild_id not in self.guild_settings:
            self.guild_settings[guild_id] = default_settings()
        settings = self.guild_settings[guild_id]
        for key, value in default_settings().items():
            settings.setdefault(key, value)
        return settings

    def get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.guild_locks:
            self.guild_locks[guild_id] = asyncio.Lock()
        return self.guild_locks[guild_id]

    def load_settings(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            for guild_id, settings in raw.items():
                self.guild_settings[int(guild_id)] = settings
            print(f"[TTS] Loaded settings for {len(self.guild_settings)} guild(s).")
        except Exception as exc:
            print(f"[TTS] Failed to load settings: {exc}")

    def save_settings(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            serializable = {str(k): v for k, v in self.guild_settings.items()}
            with SETTINGS_FILE.open("w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as exc:
            print(f"[TTS] Failed to save settings: {exc}")

    def clean_message(self, text: str) -> str | None:
        if not text:
            return None
        text = text.strip()
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"www\.\S+", "", text)
        text = re.sub(r"<a?:\w+:\d+>", "", text)
        text = re.sub(r"<@!?\d+>", "someone", text)
        text = re.sub(r"<#\d+>", "a channel", text)
        text = re.sub(r"<@&\d+>", "a role", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    def should_skip_message(self, message: discord.Message, settings: dict) -> bool:
        if message.author.bot:
            return True
        if not settings["tts_enabled"]:
            return True
        if settings["no_mic_channel_id"] is None:
            return True
        if message.channel.id != settings["no_mic_channel_id"]:
            return True
        if message.author.id in settings["ignored_users"]:
            return True
        if settings["smart_filter"]:
            content = message.content.strip()
            if not content:
                return True
            lowered = content.lower()
            if lowered in {"lol", "lmao", "ok", "k", "w", "?", "??"}:
                return True
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                return True
        return False

    async def ensure_same_vc(self, message: discord.Message, settings: dict) -> tuple[bool, str | None]:
        voice_client = message.guild.voice_client if message.guild else None
        if not voice_client or not voice_client.channel:
            return False, "Bot is not in a voice channel."
        if not settings["same_vc_required"]:
            return True, None
        if not message.author.voice or not message.author.voice.channel:
            return False, "You are not in a voice channel."
        if message.author.voice.channel.id != voice_client.channel.id:
            return False, "You are not in the same voice channel as the bot."
        return True, None

    async def speak_text(self, guild: discord.Guild, text: str, lang: str = "en", max_length: int = 300) -> None:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return

        lock = self.get_guild_lock(guild.id)
        async with lock:
            cleaned = self.clean_message(text)
            if not cleaned:
                return
            if len(cleaned) > max_length:
                cleaned = cleaned[:max_length] + "..."

            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    mp3_path = Path(temp_dir) / "tts.mp3"
                    tts = gTTS(text=cleaned, lang=lang)
                    tts.save(str(mp3_path))

                    while voice_client.is_playing() or voice_client.is_paused():
                        await asyncio.sleep(0.3)

                    voice_client.play(discord.FFmpegPCMAudio(str(mp3_path)))
                    self.guild_last_spoke[guild.id] = asyncio.get_running_loop().time()

                    while voice_client.is_playing():
                        await asyncio.sleep(0.3)
            except Exception as exc:
                print(f"[TTS] Generation/playback error in guild {guild.id}: {exc}")

    @tasks.loop(seconds=30)
    async def idle_check(self) -> None:
        now = asyncio.get_running_loop().time()
        for guild in self.bot.guilds:
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                continue

            settings = self.get_guild_settings(guild.id)
            timeout = settings.get("idle_timeout", 300)
            if timeout <= 0:
                continue

            last = self.guild_last_spoke.get(guild.id, now)
            if now - last >= timeout:
                try:
                    await voice_client.disconnect()
                    self.guild_last_spoke.pop(guild.id, None)
                    print(f"[TTS] Auto-left guild {guild.id} due to idle timeout.")
                except Exception as exc:
                    print(f"[TTS] Idle disconnect failed in guild {guild.id}: {exc}")

    @idle_check.before_loop
    async def before_idle_check(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        settings = self.get_guild_settings(message.guild.id)
        if self.should_skip_message(message, settings):
            return

        ok, _ = await self.ensure_same_vc(message, settings)
        if not ok:
            return

        text_to_read = f"{message.author.display_name} says {message.content}" if settings["say_name"] else message.content
        await self.speak_text(
            message.guild,
            text_to_read,
            lang=settings.get("language", "en"),
            max_length=settings.get("max_length", 300),
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        guild = member.guild
        voice_client = guild.voice_client

        if after.channel is not None and (before.channel is None or before.channel != after.channel):
            if voice_client is None or not voice_client.is_connected():
                try:
                    await after.channel.connect()
                    self.guild_last_spoke[guild.id] = asyncio.get_running_loop().time()
                except Exception as exc:
                    print(f"[TTS] Auto-join failed in guild {guild.id}: {exc}")

        if before.channel is not None and voice_client and voice_client.channel == before.channel:
            non_bots = [m for m in before.channel.members if not m.bot]
            if not non_bots:
                try:
                    await voice_client.disconnect()
                    self.guild_last_spoke.pop(guild.id, None)
                except Exception as exc:
                    print(f"[TTS] Auto-leave failed in guild {guild.id}: {exc}")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer()
        channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client
        try:
            if voice_client and voice_client.is_connected():
                await voice_client.move_to(channel)
            else:
                await channel.connect()
            self.guild_last_spoke[interaction.guild.id] = asyncio.get_running_loop().time()
            await interaction.followup.send(f"Joined **{channel.name}**.")
        except Exception as exc:
            await interaction.followup.send(f"Failed to join VC: `{exc}`")

    @app_commands.command(name="leave", description="Leave the current voice channel")
    async def leave(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            await voice_client.disconnect()
            self.guild_last_spoke.pop(interaction.guild.id, None)
            await interaction.followup.send("Disconnected from voice channel.")
        except Exception as exc:
            await interaction.followup.send(f"Failed to leave VC: `{exc}`")

    @app_commands.command(name="skip", description="Stop whatever is currently being read aloud")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
            return
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @app_commands.command(name="ignore", description="Stop reading a user's messages")
    async def ignore(self, interaction: discord.Interaction, user: discord.Member) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        if user.id not in settings["ignored_users"]:
            settings["ignored_users"].append(user.id)
            self.save_settings()
        await interaction.response.send_message(f"{user.display_name} will no longer be read aloud.")

    @app_commands.command(name="unignore", description="Resume reading a user's messages")
    async def unignore(self, interaction: discord.Interaction, user: discord.Member) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        if user.id in settings["ignored_users"]:
            settings["ignored_users"].remove(user.id)
            self.save_settings()
        await interaction.response.send_message(f"{user.display_name} will be read aloud again.")

    @app_commands.command(name="setnomic", description="Set the no-mic text channel for TTS")
    async def setnomic(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["no_mic_channel_id"] = channel.id
        self.save_settings()
        await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")

    @app_commands.command(name="setlang", description="Set the TTS language")
    async def setlang(self, interaction: discord.Interaction, language: str) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["language"] = language.strip().lower()
        self.save_settings()
        await interaction.response.send_message(f"TTS language set to `{language}`.")

    @app_commands.command(name="setmaxlength", description="Set max characters to read per message")
    async def setmaxlength(self, interaction: discord.Interaction, characters: int) -> None:
        if characters < 20 or characters > 1000:
            await interaction.response.send_message("Please choose a value between 20 and 1000.", ephemeral=True)
            return
        settings = self.get_guild_settings(interaction.guild.id)
        settings["max_length"] = characters
        self.save_settings()
        await interaction.response.send_message(f"Max message length set to **{characters}** characters.")

    @app_commands.command(name="tts_on", description="Turn TTS on")
    async def tts_on(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["tts_enabled"] = True
        self.save_settings()
        await interaction.response.send_message("TTS is now **ON**.")

    @app_commands.command(name="tts_off", description="Turn TTS off")
    async def tts_off(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["tts_enabled"] = False
        self.save_settings()
        await interaction.response.send_message("TTS is now **OFF**.")

    @app_commands.command(name="sayname_on", description="Read the username before each message")
    async def sayname_on(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["say_name"] = True
        self.save_settings()
        await interaction.response.send_message("Username prefix is now **ON**.")

    @app_commands.command(name="sayname_off", description="Read only the message")
    async def sayname_off(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["say_name"] = False
        self.save_settings()
        await interaction.response.send_message("Username prefix is now **OFF**.")

    @app_commands.command(name="samevc_on", description="Require users to be in the same VC as the bot")
    async def samevc_on(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["same_vc_required"] = True
        self.save_settings()
        await interaction.response.send_message("Same VC requirement is now **ON**.")

    @app_commands.command(name="samevc_off", description="Turn off same VC requirement")
    async def samevc_off(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["same_vc_required"] = False
        self.save_settings()
        await interaction.response.send_message("Same VC requirement is now **OFF**.")

    @app_commands.command(name="smartfilter_on", description="Skip spam, links, and very short messages")
    async def smartfilter_on(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["smart_filter"] = True
        self.save_settings()
        await interaction.response.send_message("Smart filter is now **ON**.")

    @app_commands.command(name="smartfilter_off", description="Read all messages without filtering")
    async def smartfilter_off(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        settings["smart_filter"] = False
        self.save_settings()
        await interaction.response.send_message("Smart filter is now **OFF**.")

    @app_commands.command(name="tts_status", description="Show current TTS settings")
    async def tts_status(self, interaction: discord.Interaction) -> None:
        settings = self.get_guild_settings(interaction.guild.id)
        no_mic = settings["no_mic_channel_id"]
        no_mic_text = f"<#{no_mic}>" if no_mic else "Not set"
        ignored = settings["ignored_users"]
        ignored_text = ", ".join(f"<@{uid}>" for uid in ignored) if ignored else "None"
        timeout = settings.get("idle_timeout", 300)
        timeout_text = f"{timeout}s" if timeout > 0 else "Disabled"

        msg = (
            f"**TTS Enabled:** {settings['tts_enabled']}\n"
            f"**No-Mic Channel:** {no_mic_text}\n"
            f"**Language:** {settings.get('language', 'en')}\n"
            f"**Max Length:** {settings.get('max_length', 300)} chars\n"
            f"**Say Name:** {settings.get('say_name', True)}\n"
            f"**Same VC Required:** {settings['same_vc_required']}\n"
            f"**Smart Filter:** {settings['smart_filter']}\n"
            f"**Idle Timeout:** {timeout_text}\n"
            f"**Ignored Users:** {ignored_text}"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="ttspanel", description="Show how to use the TTS bot")
    async def ttspanel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="TTS Bot - How to Use",
            description="This bot reads messages from a designated text channel aloud in voice chat.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Setup",
            value=(
                "1. Join a voice channel\n"
                "2. Use /setnomic to pick which text channel the bot reads\n"
                "3. The bot auto-joins when anyone enters VC and auto-leaves when empty"
            ),
            inline=False,
        )
        embed.add_field(name="Voice Commands", value="/join, /leave, /skip", inline=False)
        embed.add_field(
            name="TTS Controls",
            value="/setnomic, /tts_on, /tts_off, /setlang, /setmaxlength, /tts_status",
            inline=False,
        )
        embed.add_field(
            name="User Controls",
            value="/ignore, /unignore, /sayname_on, /sayname_off",
            inline=False,
        )
        embed.add_field(
            name="Extra Settings",
            value="/samevc_on, /samevc_off, /smartfilter_on, /smartfilter_off",
            inline=False,
        )
        embed.set_footer(text="Settings save automatically and persist after restarts.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TTSCog(bot))
