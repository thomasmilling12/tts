import os
import re
import sys
import json
import shutil
import asyncio
import tempfile
import subprocess
from pathlib import Path
from ctypes.util import find_library

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS


# ─── Opus loader ────────────────────────────────────────────────────────────

def load_opus_auto():
    if discord.opus.is_loaded():
        return

    found = find_library("opus")
    if found:
        try:
            discord.opus.load_opus(found)
            print(f"Opus loaded: {found}")
            return
        except Exception:
            pass

    for name in ("opus", "libopus-0", "libopus", "opus-0", "libopus.so.0"):
        try:
            discord.opus.load_opus(name)
            print(f"Opus loaded: {name}")
            return
        except Exception:
            pass

    try:
        import pyogg
        pyogg_dir = Path(pyogg.__file__).parent
        for dll in pyogg_dir.rglob("*opus*.dll"):
            try:
                discord.opus.load_opus(str(dll))
                print(f"Opus loaded from PyOgg: {dll}")
                return
            except Exception:
                pass
    except ImportError:
        pass

    print("WARNING: Could not load opus. Voice will not work.")


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("WARNING: ffmpeg not found. TTS audio will not play.")
        print("  Install it with:  sudo apt install ffmpeg")
    else:
        print("ffmpeg found.")


load_opus_auto()
check_ffmpeg()

load_dotenv(encoding="utf-8-sig")
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is missing from .env")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

SETTINGS_FILE = Path("settings.json")

guild_settings = {}
guild_locks = {}
guild_last_spoke = {}   # guild_id -> asyncio.Event timestamp (float)


# ─── Settings persistence ────────────────────────────────────────────────────

def default_settings():
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


def get_guild_settings(guild_id: int) -> dict:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = default_settings()
    s = guild_settings[guild_id]
    # Back-fill any keys added after first save
    for k, v in default_settings().items():
        s.setdefault(k, v)
    return s


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in guild_locks:
        guild_locks[guild_id] = asyncio.Lock()
    return guild_locks[guild_id]


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for gid_str, s in raw.items():
                guild_settings[int(gid_str)] = s
            print(f"Loaded settings for {len(guild_settings)} guild(s).")
        except Exception as e:
            print(f"Failed to load settings: {e}")


def save_settings():
    try:
        serializable = {str(k): v for k, v in guild_settings.items()}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        print(f"Failed to save settings: {e}")


load_settings()


# ─── Message helpers ─────────────────────────────────────────────────────────

def clean_message(text: str) -> str | None:
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


def should_skip_message(message: discord.Message, settings: dict) -> bool:
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
        spam_words = {"lol", "lmao", "ok", "k", "w", "?", "??", "😂", "😭"}
        if lowered in spam_words:
            return True
        if "http://" in lowered or "https://" in lowered or "www." in lowered:
            return True
    return False


async def ensure_same_vc(message: discord.Message, settings: dict) -> tuple[bool, str | None]:
    voice_client = message.guild.voice_client
    if not voice_client or not voice_client.channel:
        return False, "Bot is not in a voice channel."
    if not settings["same_vc_required"]:
        return True, None
    if not message.author.voice or not message.author.voice.channel:
        return False, "You are not in a voice channel."
    if message.author.voice.channel.id != voice_client.channel.id:
        return False, "You are not in the same voice channel as the bot."
    return True, None


# ─── TTS playback ────────────────────────────────────────────────────────────

async def speak_text(guild: discord.Guild, text: str, lang: str = "en", max_length: int = 300):
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    lock = get_guild_lock(guild.id)

    async with lock:
        cleaned = clean_message(text)
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

                source = discord.FFmpegPCMAudio(str(mp3_path))
                voice_client.play(source)

                guild_last_spoke[guild.id] = asyncio.get_event_loop().time()

                while voice_client.is_playing():
                    await asyncio.sleep(0.3)

        except Exception as e:
            print(f"TTS generation/playback error in guild {guild.id}: {e}")


# ─── Idle timeout background task ────────────────────────────────────────────

@tasks.loop(seconds=30)
async def idle_check():
    now = asyncio.get_event_loop().time()
    for guild in bot.guilds:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            continue
        settings = get_guild_settings(guild.id)
        timeout = settings.get("idle_timeout", 300)
        if timeout <= 0:
            continue
        last = guild_last_spoke.get(guild.id, now)
        if (now - last) >= timeout:
            try:
                await voice_client.disconnect()
                guild_last_spoke.pop(guild.id, None)
                print(f"Auto-left guild {guild.id} due to idle timeout.")
            except Exception as e:
                print(f"Idle disconnect failed: {e}")


# ─── Bot events ──────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to guild {guild.name}")
        except Exception as e:
            print(f"Slash sync failed for {guild.name}: {e}")
    idle_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.guild is not None:
        settings = get_guild_settings(message.guild.id)
        if not should_skip_message(message, settings):
            ok, _ = await ensure_same_vc(message, settings)
            if ok:
                if settings["say_name"]:
                    text_to_read = f"{message.author.display_name} says {message.content}"
                else:
                    text_to_read = message.content
                try:
                    await speak_text(
                        message.guild,
                        text_to_read,
                        lang=settings.get("language", "en"),
                        max_length=settings.get("max_length", 300),
                    )
                except Exception as e:
                    print(f"TTS error in guild {message.guild.id}: {e}")
    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild = member.guild
    voice_client = guild.voice_client

    # Someone joined — auto-join if bot isn't in a channel
    if after.channel is not None and (before.channel is None or before.channel != after.channel):
        if voice_client is None or not voice_client.is_connected():
            try:
                await after.channel.connect()
                guild_last_spoke[guild.id] = asyncio.get_event_loop().time()
            except Exception as e:
                print(f"Auto-join failed: {e}")

    # Someone left — auto-leave if bot's channel is now empty
    if before.channel is not None and voice_client and voice_client.channel == before.channel:
        non_bots = [m for m in before.channel.members if not m.bot]
        if len(non_bots) == 0:
            try:
                await voice_client.disconnect()
                guild_last_spoke.pop(guild.id, None)
            except Exception as e:
                print(f"Auto-leave failed: {e}")


# ─── Slash commands ──────────────────────────────────────────────────────────

@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
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
        guild_last_spoke[interaction.guild.id] = asyncio.get_event_loop().time()
        await interaction.followup.send(f"Joined **{channel.name}**.")
    except Exception as e:
        await interaction.followup.send(f"Failed to join VC: `{e}`")


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        await voice_client.disconnect()
        guild_last_spoke.pop(interaction.guild.id, None)
        await interaction.followup.send("Disconnected from voice channel.")
    except Exception as e:
        await interaction.followup.send(f"Failed to leave VC: `{e}`")


@bot.tree.command(name="skip", description="Stop whatever is currently being read aloud")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)


@bot.tree.command(name="ignore", description="Stop reading a user's messages")
async def ignore(interaction: discord.Interaction, user: discord.Member):
    settings = get_guild_settings(interaction.guild.id)
    if user.id not in settings["ignored_users"]:
        settings["ignored_users"].append(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will no longer be read aloud.")


@bot.tree.command(name="unignore", description="Resume reading a user's messages")
async def unignore(interaction: discord.Interaction, user: discord.Member):
    settings = get_guild_settings(interaction.guild.id)
    if user.id in settings["ignored_users"]:
        settings["ignored_users"].remove(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will be read aloud again.")


@bot.tree.command(name="setnomic", description="Set the no-mic text channel for TTS")
async def setnomic(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = get_guild_settings(interaction.guild.id)
    settings["no_mic_channel_id"] = channel.id
    save_settings()
    await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")


@bot.tree.command(name="setlang", description="Set the TTS language (e.g. en, es, fr, de, ja)")
async def setlang(interaction: discord.Interaction, language: str):
    settings = get_guild_settings(interaction.guild.id)
    settings["language"] = language.strip().lower()
    save_settings()
    await interaction.response.send_message(f"TTS language set to `{language}`.")


@bot.tree.command(name="setmaxlength", description="Set max characters to read per message (default 300)")
async def setmaxlength(interaction: discord.Interaction, characters: int):
    if characters < 20 or characters > 1000:
        await interaction.response.send_message("Please choose a value between 20 and 1000.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["max_length"] = characters
    save_settings()
    await interaction.response.send_message(f"Max message length set to **{characters}** characters.")


@bot.tree.command(name="tts_on", description="Turn TTS on")
async def tts_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = True
    save_settings()
    await interaction.response.send_message("TTS is now **ON**.")


@bot.tree.command(name="tts_off", description="Turn TTS off")
async def tts_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = False
    save_settings()
    await interaction.response.send_message("TTS is now **OFF**.")


@bot.tree.command(name="sayname_on", description="Read 'username says' before each message")
async def sayname_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["say_name"] = True
    save_settings()
    await interaction.response.send_message("Username prefix is now **ON**.")


@bot.tree.command(name="sayname_off", description="Read only the message, skip the username prefix")
async def sayname_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["say_name"] = False
    save_settings()
    await interaction.response.send_message("Username prefix is now **OFF**.")


@bot.tree.command(name="samevc_on", description="Require users to be in the same VC as the bot")
async def samevc_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = True
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **ON**.")


@bot.tree.command(name="samevc_off", description="Turn off same VC requirement")
async def samevc_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = False
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Skip spam, links, and very short messages")
async def smartfilter_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = True
    save_settings()
    await interaction.response.send_message("Smart filter is now **ON**.")


@bot.tree.command(name="smartfilter_off", description="Read all messages without filtering")
async def smartfilter_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = False
    save_settings()
    await interaction.response.send_message("Smart filter is now **OFF**.")


@bot.tree.command(name="tts_status", description="Show current TTS settings")
async def tts_status(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
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


@bot.tree.command(name="panel", description="Show how to use the TTS bot")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="TTS Bot — How to Use",
        description="This bot reads messages from a designated text channel aloud in voice chat.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Setup",
        value=(
            "1. Join a voice channel\n"
            "2. Use `/setnomic #channel` to pick which text channel the bot reads\n"
            "3. The bot auto-joins when anyone enters VC and auto-leaves when empty"
        ),
        inline=False
    )
    embed.add_field(
        name="Voice Commands",
        value=(
            "`/join` — Join your voice channel\n"
            "`/leave` — Leave the voice channel\n"
            "`/skip` — Stop the current TTS message"
        ),
        inline=False
    )
    embed.add_field(
        name="TTS Controls",
        value=(
            "`/setnomic #channel` — Set which channel to read aloud\n"
            "`/tts_on` / `/tts_off` — Toggle TTS\n"
            "`/setlang <code>` — Change language (en, es, fr, de, ja…)\n"
            "`/setmaxlength <n>` — Max characters per message (default 300)\n"
            "`/tts_status` — View all current settings"
        ),
        inline=False
    )
    embed.add_field(
        name="User Controls",
        value=(
            "`/ignore @user` — Stop reading a specific user's messages\n"
            "`/unignore @user` — Resume reading their messages\n"
            "`/sayname_on` / `/sayname_off` — Toggle the 'username says' prefix"
        ),
        inline=False
    )
    embed.add_field(
        name="Extra Settings",
        value=(
            "`/samevc_on` / `/samevc_off` — Require users to be in same VC\n"
            "`/smartfilter_on` / `/smartfilter_off` — Filter out spam and links"
        ),
        inline=False
    )
    embed.set_footer(text="Settings save automatically and persist after restarts. Bot auto-leaves after 5 min idle.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
