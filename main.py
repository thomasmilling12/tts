import os
import re
import asyncio
import tempfile
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from gtts import gTTS

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise ValueError("DISCORD_TOKEN is missing from .env")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild settings
guild_settings = {}
guild_locks = {}


def get_guild_settings(guild_id: int):
    if guild_id not in guild_settings:
        guild_settings[guild_id] = {
            "tts_enabled": True,
            "no_mic_channel_id": None,
            "same_vc_required": True,
            "smart_filter": True,
        }
    return guild_settings[guild_id]


def get_guild_lock(guild_id: int):
    if guild_id not in guild_locks:
        guild_locks[guild_id] = asyncio.Lock()
    return guild_locks[guild_id]


def clean_message(text: str) -> str | None:
    if not text:
        return None

    text = text.strip()

    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)

    # Remove custom emoji markup
    text = re.sub(r"<a?:\w+:\d+>", "", text)

    # Remove mentions formatting
    text = re.sub(r"<@!?\d+>", "someone", text)
    text = re.sub(r"<#\d+>", "a channel", text)
    text = re.sub(r"<@&\d+>", "a role", text)

    # Collapse spaces
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None

    return text


def should_skip_message(message: discord.Message, settings: dict) -> bool:
    if message.author.bot:
        return True

    if not settings["tts_enabled"]:
        return True

    no_mic_channel_id = settings["no_mic_channel_id"]
    if no_mic_channel_id is None:
        return True

    if message.channel.id != no_mic_channel_id:
        return True

    if settings["smart_filter"]:
        content = message.content.strip()

        # Skip empty messages or attachment-only posts
        if not content:
            return True

        # Skip very short spammy things
        lowered = content.lower()
        spam_words = {"lol", "lmao", "ok", "k", "w", "?", "??", "😂", "😭"}
        if lowered in spam_words:
            return True

        # Skip obvious link posts
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


async def speak_text(guild: discord.Guild, text: str):
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    lock = get_guild_lock(guild.id)

    async with lock:
        cleaned = clean_message(text)
        if not cleaned:
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            mp3_path = Path(temp_dir) / "tts.mp3"

            tts = gTTS(text=cleaned, lang="en")
            tts.save(str(mp3_path))

            # Wait for anything currently playing
            while voice_client.is_playing() or voice_client.is_paused():
                await asyncio.sleep(0.3)

            source = discord.FFmpegPCMAudio(str(mp3_path))
            voice_client.play(source)

            while voice_client.is_playing():
                await asyncio.sleep(0.3)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash sync failed: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.guild is not None:
        settings = get_guild_settings(message.guild.id)

        if not should_skip_message(message, settings):
            ok, _ = await ensure_same_vc(message, settings)
            if ok:
                text_to_read = f"{message.author.display_name} says {message.content}"
                try:
                    await speak_text(message.guild, text_to_read)
                except Exception as e:
                    print(f"TTS error in guild {message.guild.id}: {e}")

    await bot.process_commands(message)


@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "You need to be in a voice channel first.",
            ephemeral=True
        )
        return

    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    try:
        if voice_client and voice_client.is_connected():
            await voice_client.move_to(channel)
        else:
            await channel.connect()

        await interaction.response.send_message(f"Joined **{channel.name}**.")
    except Exception as e:
        await interaction.response.send_message(f"Failed to join VC: `{e}`", ephemeral=True)


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return

    try:
        await voice_client.disconnect()
        await interaction.response.send_message("Disconnected from voice channel.")
    except Exception as e:
        await interaction.response.send_message(f"Failed to leave VC: `{e}`", ephemeral=True)


@bot.tree.command(name="setnomic", description="Set the no-mic text channel for TTS")
async def setnomic(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = get_guild_settings(interaction.guild.id)
    settings["no_mic_channel_id"] = channel.id
    await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")


@bot.tree.command(name="tts_on", description="Turn TTS on")
async def tts_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = True
    await interaction.response.send_message("TTS is now **ON**.")


@bot.tree.command(name="tts_off", description="Turn TTS off")
async def tts_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = False
    await interaction.response.send_message("TTS is now **OFF**.")


@bot.tree.command(name="samevc_on", description="Require users to be in the same VC as the bot")
async def samevc_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = True
    await interaction.response.send_message("Same VC requirement is now **ON**.")


@bot.tree.command(name="samevc_off", description="Turn off same VC requirement")
async def samevc_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = False
    await interaction.response.send_message("Same VC requirement is now **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Turn smart filter on")
async def smartfilter_on(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = True
    await interaction.response.send_message("Smart filter is now **ON**.")


@bot.tree.command(name="smartfilter_off", description="Turn smart filter off")
async def smartfilter_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = False
    await interaction.response.send_message("Smart filter is now **OFF**.")


@bot.tree.command(name="tts_status", description="Show current TTS settings")
async def tts_status(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    no_mic = settings["no_mic_channel_id"]
    no_mic_text = f"<#{no_mic}>" if no_mic else "Not set"

    msg = (
        f"**TTS Enabled:** {settings['tts_enabled']}\n"
        f"**No-Mic Channel:** {no_mic_text}\n"
        f"**Same VC Required:** {settings['same_vc_required']}\n"
        f"**Smart Filter:** {settings['smart_filter']}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


bot.run(TOKEN)