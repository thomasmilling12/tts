import asyncio
import json
import os
import re
import shutil
import tempfile
from ctypes.util import find_library
from pathlib import Path

import discord
from deep_translator import GoogleTranslator
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS
from gtts.lang import tts_langs
from langdetect import DetectorFactory, LangDetectException, detect


# --- Paths, constants, and simple config ---

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
SUPPORTED_LANGUAGES = tts_langs()
SHORT_MESSAGES = {"lol", "lmao", "ok", "k", "w", "?", "??", "\U0001F602", "\U0001F62D"}
AUTO_JOIN_DELAY_SECONDS = 1.0
AUTO_LEAVE_DELAY_SECONDS = 5.0
TRANSLATE_MIN_LENGTH = 6
AI_REPLY_PREFIXES = ("bot ", "bot,", "assistant ", "assistant,", "ttsbot ", "ttsbot,")
LANGUAGE_ALIASES = {
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "he": "iw",
}

DetectorFactory.seed = 0

VOICE_PROFILES = {
    "female": {
        "label": "Female-style",
        "ffmpeg_options": "-filter:a asetrate=24000*1.10,atempo=0.97,aresample=24000",
    },
    "male": {
        "label": "Male-style",
        "ffmpeg_options": "-filter:a asetrate=24000*0.90,atempo=1.08,aresample=24000",
    },
    "neutral": {
        "label": "Neutral",
        "ffmpeg_options": None,
    },
}


# --- Runtime dependency checks ---

def load_opus_auto() -> None:
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


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        print("WARNING: ffmpeg not found. TTS audio will not play.")
        print("Install ffmpeg and make sure it is available on your PATH.")
    else:
        print("ffmpeg found.")


load_opus_auto()
check_ffmpeg()


# --- Discord client setup ---

load_dotenv(dotenv_path=BASE_DIR / ".env", encoding="utf-8-sig")
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is missing from .env")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# --- In-memory bot state ---

guild_settings: dict[int, dict] = {}
guild_locks: dict[int, asyncio.Lock] = {}
guild_last_spoke: dict[int, float] = {}
guild_join_tasks: dict[int, asyncio.Task] = {}
guild_leave_tasks: dict[int, asyncio.Task] = {}
command_sync_complete = False
command_sync_lock = asyncio.Lock()


# --- Settings persistence ---

def default_settings() -> dict:
    return {
        "tts_enabled": True,
        "no_mic_channel_id": None,
        "voice_channel_id": None,
        "read_muted_only": False,
        "read_not_deafened_only": False,
        "same_vc_required": True,
        "smart_filter": True,
        "ignored_users": [],
        "language": "en",
        "max_length": 300,
        "translation_mode": "off",
        "volume": 100,
        "voice_style": "female",
        "ai_reply_enabled": False,
    }


def sanitize_settings(settings: dict) -> dict:
    clean = default_settings()
    for key in clean:
        if key in settings:
            clean[key] = settings[key]

    if "translate_enabled" in settings and "translation_mode" not in settings:
        clean["translation_mode"] = "english" if settings.get("translate_enabled") else "off"

    if clean["language"] not in SUPPORTED_LANGUAGES:
        clean["language"] = "en"
    if clean["translation_mode"] not in {"off", "english", "original"}:
        clean["translation_mode"] = "off"
    if clean["voice_style"] not in VOICE_PROFILES:
        clean["voice_style"] = "female"
    if not isinstance(clean["ignored_users"], list):
        clean["ignored_users"] = []
    if not isinstance(clean["read_muted_only"], bool):
        clean["read_muted_only"] = False
    if not isinstance(clean["read_not_deafened_only"], bool):
        clean["read_not_deafened_only"] = False
    if not isinstance(clean["ai_reply_enabled"], bool):
        clean["ai_reply_enabled"] = False
    try:
        clean["volume"] = max(0, min(100, int(clean["volume"])))
    except (TypeError, ValueError):
        clean["volume"] = 100

    return clean


def get_guild_settings(guild_id: int) -> dict:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = default_settings()
    guild_settings[guild_id] = sanitize_settings(guild_settings[guild_id])
    return guild_settings[guild_id]


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in guild_locks:
        guild_locks[guild_id] = asyncio.Lock()
    return guild_locks[guild_id]


def load_settings() -> None:
    if not SETTINGS_FILE.exists():
        return

    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        for guild_id, settings in raw.items():
            guild_settings[int(guild_id)] = sanitize_settings(settings)
        print(f"Loaded settings for {len(guild_settings)} guild(s).")
    except Exception as exc:
        print(f"Failed to load settings: {exc}")


def save_settings() -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            str(guild_id): sanitize_settings(settings)
            for guild_id, settings in guild_settings.items()
        }
        with SETTINGS_FILE.open("w", encoding="utf-8") as file:
            json.dump(serializable, file, indent=2)
    except Exception as exc:
        print(f"Failed to save settings: {exc}")


load_settings()


# --- Command sync and duplicate cleanup ---

async def sync_application_commands() -> None:
    global command_sync_complete

    if command_sync_complete:
        return

    async with command_sync_lock:
        if command_sync_complete:
            return

        # Remove stale guild-scoped copies left behind by older copy_global_to usage.
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                print(f"Cleared legacy guild commands in {guild.name}")
            except Exception as exc:
                print(f"Guild command cleanup failed for {guild.name}: {exc}")

        try:
            synced = await bot.tree.sync()
            print(f"Globally synced {len(synced)} command(s).")
            command_sync_complete = True
        except Exception as exc:
            print(f"Global slash sync failed: {exc}")


# --- Voice-channel task helpers ---

def cancel_task(task_map: dict[int, asyncio.Task], guild_id: int) -> None:
    task = task_map.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


def get_linked_voice_channel(guild: discord.Guild) -> discord.VoiceChannel | discord.StageChannel | None:
    settings = get_guild_settings(guild.id)
    channel_id = settings.get("voice_channel_id")
    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


def count_real_members(channel: discord.VoiceChannel | discord.StageChannel | None) -> int:
    if channel is None:
        return 0
    return sum(1 for member in channel.members if not member.bot)


def member_is_in_channel(member: discord.Member, channel: discord.VoiceChannel | discord.StageChannel | None) -> bool:
    return channel is not None and member.voice is not None and member.voice.channel is not None and member.voice.channel.id == channel.id


def member_matches_readmuted(member: discord.Member, settings: dict) -> bool:
    if not settings.get("read_muted_only", False):
        return True

    linked_channel = get_linked_voice_channel(member.guild)
    if not member_is_in_channel(member, linked_channel):
        return False

    voice_state = member.voice
    if voice_state is None:
        return False

    return bool(voice_state.self_mute or voice_state.mute)


def member_matches_readnotdeafened(member: discord.Member, settings: dict) -> bool:
    if not settings.get("read_not_deafened_only", False):
        return True

    linked_channel = get_linked_voice_channel(member.guild)
    if not member_is_in_channel(member, linked_channel):
        return False

    voice_state = member.voice
    if voice_state is None:
        return False

    return not bool(voice_state.self_deaf or voice_state.deaf)


def detect_supported_language(text: str, fallback: str) -> str:
    try:
        detected = detect(text)
    except LangDetectException:
        return fallback

    normalized = LANGUAGE_ALIASES.get(detected.lower(), detected)
    if normalized in SUPPORTED_LANGUAGES:
        return normalized
    return fallback


def extract_ai_prompt(message: discord.Message) -> str | None:
    content = message.content or ""

    if bot.user is not None:
        content = content.replace(bot.user.mention, " ")
        content = content.replace(f"<@!{bot.user.id}>", " ")

    cleaned = clean_message(content)
    if not cleaned:
        return None

    lowered = cleaned.lower()
    for prefix in AI_REPLY_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break

    return cleaned or None


def should_trigger_ai_reply(message: discord.Message, settings: dict) -> bool:
    if not settings.get("ai_reply_enabled", False):
        return False
    if message.guild is None or bot.user is None:
        return False
    if settings["no_mic_channel_id"] is None or message.channel.id != settings["no_mic_channel_id"]:
        return False
    if bot.user in message.mentions:
        return True

    cleaned = clean_message(message.content or "")
    if not cleaned:
        return False

    prompt = extract_ai_prompt(message)
    return prompt is not None and cleaned.lower().startswith(AI_REPLY_PREFIXES)


def generate_ai_reply(prompt: str) -> str:
    lowered = prompt.lower()
    words = set(re.findall(r"[a-z']+", lowered))

    if {"hello", "hey", "hi"} & words:
        return "Hello. I am here and ready to read the channel."
    if {"thank", "thanks", "ty"} & words:
        return "You are welcome."
    if any(word in lowered for word in ("help", "what can you do", "commands")):
        return "Use slash commands like panel, voice, translate, volume, and muted filters to control me."
    if "voice" in lowered:
        return "Use the voice command to switch between male, female, and neutral styles."
    if "translate" in lowered:
        return "Use the translate command to choose off, english, or original mode."
    if "volume" in lowered or "loud" in lowered or "quiet" in lowered:
        return "Use the volume command with a value from zero to one hundred."
    if "muted" in lowered or "deaf" in lowered:
        return "I can filter by muted users or by users who are not deafened."
    if "join" in lowered or "leave" in lowered:
        return "Use join to link the voice channel and leave to disconnect."

    return "I can read messages, translate them, change voices, and manage voice filters."


async def schedule_auto_join(guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel) -> None:
    cancel_task(guild_leave_tasks, guild.id)

    existing = guild_join_tasks.get(guild.id)
    if existing and not existing.done():
        return

    async def runner() -> None:
        try:
            await asyncio.sleep(AUTO_JOIN_DELAY_SECONDS)

            settings = get_guild_settings(guild.id)
            linked_channel = get_linked_voice_channel(guild)
            voice_client = guild.voice_client

            if not settings["tts_enabled"] or settings["no_mic_channel_id"] is None:
                return
            if linked_channel is None or linked_channel.id != channel.id:
                return
            if count_real_members(channel) == 0:
                return
            if voice_client and voice_client.is_connected():
                return

            await channel.connect()
            guild_last_spoke[guild.id] = asyncio.get_running_loop().time()
            print(f"Auto-joined voice channel in guild {guild.id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Auto-join failed in guild {guild.id}: {exc}")
        finally:
            guild_join_tasks.pop(guild.id, None)

    guild_join_tasks[guild.id] = asyncio.create_task(runner())


async def schedule_delayed_leave(guild: discord.Guild) -> None:
    cancel_task(guild_leave_tasks, guild.id)

    async def runner() -> None:
        try:
            await asyncio.sleep(AUTO_LEAVE_DELAY_SECONDS)

            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                return
            if count_real_members(voice_client.channel) > 0:
                return

            await voice_client.disconnect()
            guild_last_spoke.pop(guild.id, None)
            print(f"Auto-left empty voice channel in guild {guild.id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Delayed leave failed in guild {guild.id}: {exc}")
        finally:
            guild_leave_tasks.pop(guild.id, None)

    guild_leave_tasks[guild.id] = asyncio.create_task(runner())


# --- Message cleanup and translation helpers ---

def clean_message(text: str) -> str | None:
    if not text:
        return None

    text = text.strip()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)
    text = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r"\1 emoji", text)
    text = re.sub(r"<@!?\d+>", "someone", text)
    text = re.sub(r"<#\d+>", "a channel", text)
    text = re.sub(r"<@&\d+>", "a role", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"[*_~|]", " ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"([!?.,])\1+", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def describe_attachments(count: int) -> str:
    if count <= 0:
        return ""
    if count == 1:
        return "Sent an attachment."
    return f"Sent {count} attachments."


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
    if not isinstance(message.author, discord.Member):
        return True
    if not member_matches_readmuted(message.author, settings):
        return True
    if not member_matches_readnotdeafened(message.author, settings):
        return True

    cleaned = clean_message(message.content)
    if cleaned is None and not message.attachments:
        return True

    if settings["smart_filter"]:
        lowered = (message.content or "").strip().lower()
        if lowered in SHORT_MESSAGES:
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


def translate_to_english_sync(text: str) -> tuple[str, bool]:
    if len(text) < TRANSLATE_MIN_LENGTH:
        return text, False

    detected_language = detect_supported_language(text, "en")
    if detected_language == "en":
        return text, False

    try:
        translated = GoogleTranslator(source="auto", target="en").translate(text)
    except Exception:
        return text, False

    if not translated:
        return text, False

    return translated, True


async def build_spoken_text(message: discord.Message, settings: dict) -> tuple[str | None, str]:
    content = clean_message(message.content)
    spoken_language = settings.get("language", "en")
    translation_mode = settings.get("translation_mode", "off")

    if content:
        if translation_mode == "english":
            content, translated = await asyncio.to_thread(translate_to_english_sync, content)
            if translated:
                spoken_language = "en"
            content = clean_message(content)
        elif translation_mode == "original":
            spoken_language = detect_supported_language(content, spoken_language)

    parts: list[str] = []
    if content:
        parts.append(content)

    attachment_text = describe_attachments(len(message.attachments))
    if attachment_text:
        parts.append(attachment_text)

    if not parts:
        return None, spoken_language

    spoken_text = " ".join(parts).strip()
    return spoken_text, spoken_language


# --- TTS generation and playback ---

def generate_tts_file(text: str, language: str, output_path: str) -> None:
    tts = gTTS(text=text, lang=language)
    tts.save(output_path)


async def play_tts_text(
    guild: discord.Guild,
    text: str,
    settings: dict,
    *,
    spoken_language: str | None = None,
) -> None:
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    cleaned_text = clean_message(text)
    if not cleaned_text:
        return

    lock = get_guild_lock(guild.id)
    async with lock:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return

        max_length = settings.get("max_length", 300)
        if len(cleaned_text) > max_length:
            cleaned_text = cleaned_text[:max_length].rstrip() + "..."

        voice_style = settings.get("voice_style", "female")
        voice_profile = VOICE_PROFILES.get(voice_style, VOICE_PROFILES["female"])
        language = spoken_language or settings.get("language", "en")
        volume = settings.get("volume", 100) / 100

        try:
            loop = asyncio.get_running_loop()
            guild_last_spoke[guild.id] = loop.time()

            with tempfile.TemporaryDirectory() as temp_dir:
                mp3_path = Path(temp_dir) / "tts.mp3"
                await asyncio.to_thread(generate_tts_file, cleaned_text, language, str(mp3_path))

                while voice_client.is_playing() or voice_client.is_paused():
                    await asyncio.sleep(0.3)

                if not voice_client.is_connected():
                    return

                source = discord.FFmpegPCMAudio(
                    str(mp3_path),
                    options=voice_profile["ffmpeg_options"],
                )
                wrapped_source = discord.PCMVolumeTransformer(source, volume=volume)
                voice_client.play(wrapped_source)
                guild_last_spoke[guild.id] = loop.time()

                while voice_client.is_playing():
                    await asyncio.sleep(0.3)

        except Exception as exc:
            print(f"TTS generation/playback error in guild {guild.id}: {exc}")


async def speak_message(message: discord.Message, settings: dict) -> None:
    spoken_text, spoken_language = await build_spoken_text(message, settings)
    if not spoken_text:
        return

    await play_tts_text(
        message.guild,
        spoken_text,
        settings,
        spoken_language=spoken_language,
    )


# --- Safety cleanup task ---

@tasks.loop(seconds=30)
async def idle_check() -> None:
    for guild in bot.guilds:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            continue

        if count_real_members(voice_client.channel) == 0:
            try:
                await voice_client.disconnect()
                guild_last_spoke.pop(guild.id, None)
                print(f"Fallback cleanup disconnected empty voice channel in guild {guild.id}")
            except Exception as exc:
                print(f"Idle cleanup failed in guild {guild.id}: {exc}")


@idle_check.before_loop
async def before_idle_check() -> None:
    await bot.wait_until_ready()


# --- Bot events ---

@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await sync_application_commands()

    if not idle_check.is_running():
        idle_check.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild is not None:
        settings = get_guild_settings(message.guild.id)
        if not should_skip_message(message, settings):
            ok, _ = await ensure_same_vc(message, settings)
            if ok:
                await speak_message(message, settings)

                if should_trigger_ai_reply(message, settings):
                    prompt = extract_ai_prompt(message)
                    if prompt:
                        ai_reply = generate_ai_reply(prompt)
                        await play_tts_text(
                            message.guild,
                            ai_reply,
                            settings,
                            spoken_language="en",
                        )

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return

    guild = member.guild
    settings = get_guild_settings(guild.id)
    linked_channel = get_linked_voice_channel(guild)
    voice_client = guild.voice_client
    before_channel = before.channel if isinstance(before.channel, (discord.VoiceChannel, discord.StageChannel)) else None
    after_channel = after.channel if isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)) else None
    current_channel = voice_client.channel if voice_client and voice_client.is_connected() else None

    if current_channel and after_channel and after_channel.id == current_channel.id and not member.bot:
        cancel_task(guild_leave_tasks, guild.id)

    joined_linked_channel = linked_channel is not None and after_channel is not None and after_channel.id == linked_channel.id
    left_linked_channel = linked_channel is not None and before_channel is not None and before_channel.id == linked_channel.id
    moved_channels = before_channel != after_channel

    if joined_linked_channel and moved_channels:
        human_count = count_real_members(linked_channel)
        if human_count > 0:
            cancel_task(guild_leave_tasks, guild.id)

        should_auto_join = (
            human_count == 1
            and settings["tts_enabled"]
            and settings["no_mic_channel_id"] is not None
            and (voice_client is None or not voice_client.is_connected())
        )
        if should_auto_join:
            await schedule_auto_join(guild, linked_channel)

    if current_channel and left_linked_channel and before_channel and before_channel.id == current_channel.id and moved_channels:
        if count_real_members(current_channel) == 0:
            await schedule_delayed_leave(guild)

    if current_channel and after_channel is None and before_channel and before_channel.id == current_channel.id:
        if count_real_members(current_channel) == 0:
            await schedule_delayed_leave(guild)

    if current_channel and count_real_members(current_channel) > 0:
        cancel_task(guild_leave_tasks, guild.id)


# --- Slash commands: voice connection and playback control ---

@bot.tree.command(name="join", description="Join your voice channel and link it for smart auto-join")
async def join(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer()
    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    settings = get_guild_settings(interaction.guild.id)

    try:
        cancel_task(guild_leave_tasks, interaction.guild.id)
        cancel_task(guild_join_tasks, interaction.guild.id)

        if voice_client and voice_client.is_connected():
            await voice_client.move_to(channel)
        else:
            await channel.connect()

        settings["voice_channel_id"] = channel.id
        save_settings()
        guild_last_spoke[interaction.guild.id] = asyncio.get_running_loop().time()
        await interaction.followup.send(f"Joined **{channel.name}** and linked it for smart auto-join.")
    except Exception as exc:
        await interaction.followup.send(f"Failed to join VC: `{exc}`")


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def leave(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        cancel_task(guild_leave_tasks, interaction.guild.id)
        cancel_task(guild_join_tasks, interaction.guild.id)
        await voice_client.disconnect()
        guild_last_spoke.pop(interaction.guild.id, None)
        await interaction.followup.send("Disconnected from voice channel.")
    except Exception as exc:
        await interaction.followup.send(f"Failed to leave VC: `{exc}`")


@bot.tree.command(name="skip", description="Stop whatever is currently being read aloud")
async def skip(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
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


# --- Slash commands: TTS settings ---

@bot.tree.command(name="ignore", description="Stop reading a user's messages")
async def ignore(interaction: discord.Interaction, user: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    if user.id not in settings["ignored_users"]:
        settings["ignored_users"].append(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will no longer be read aloud.")


@bot.tree.command(name="unignore", description="Resume reading a user's messages")
async def unignore(interaction: discord.Interaction, user: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    if user.id in settings["ignored_users"]:
        settings["ignored_users"].remove(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will be read aloud again.")


@bot.tree.command(name="setnomic", description="Set the no-mic text channel for TTS")
async def setnomic(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["no_mic_channel_id"] = channel.id
    save_settings()
    await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")


@bot.tree.command(name="setlang", description="Set the TTS language when translation is off")
async def setlang(interaction: discord.Interaction, language: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    language = language.strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        examples = ", ".join(sorted(SUPPORTED_LANGUAGES)[:12])
        await interaction.response.send_message(
            f"`{language}` is not supported by gTTS. Try one of: {examples}",
            ephemeral=True,
        )
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["language"] = language
    save_settings()
    await interaction.response.send_message(f"TTS language set to `{language}`.")


@bot.tree.command(name="setmaxlength", description="Set max characters to read per message")
async def setmaxlength(interaction: discord.Interaction, characters: int) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if characters < 20 or characters > 1000:
        await interaction.response.send_message("Please choose a value between 20 and 1000.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["max_length"] = characters
    save_settings()
    await interaction.response.send_message(f"Max message length set to **{characters}** characters.")


@bot.tree.command(name="voice", description="Choose the TTS voice style for this server")
@app_commands.describe(style="Pick a male-style, female-style, or neutral voice")
@app_commands.choices(
    style=[
        app_commands.Choice(name="Female", value="female"),
        app_commands.Choice(name="Male", value="male"),
        app_commands.Choice(name="Neutral", value="neutral"),
    ]
)
async def voice(interaction: discord.Interaction, style: app_commands.Choice[str]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["voice_style"] = style.value
    save_settings()
    await interaction.response.send_message(f"Voice style set to **{VOICE_PROFILES[style.value]['label']}**.")


@bot.tree.command(name="translate", description="Choose how translation works before speech")
@app_commands.describe(mode="Off uses your set language, English translates to English, Original speaks the detected language")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="English", value="english"),
        app_commands.Choice(name="Original", value="original"),
    ]
)
async def translate_toggle(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["translation_mode"] = mode.value
    save_settings()
    await interaction.response.send_message(f"Translation mode is now **{mode.value}**.")


@bot.tree.command(name="volume", description="Set playback volume from 0 to 100")
async def volume(interaction: discord.Interaction, level: int) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    clamped_level = max(0, min(100, level))
    settings = get_guild_settings(interaction.guild.id)
    settings["volume"] = clamped_level
    save_settings()
    await interaction.response.send_message(f"Volume set to **{clamped_level}%**.")


@bot.tree.command(name="readnotdeafened_on", description="Only read messages from users who are not deafened in the linked voice channel")
async def readnotdeafened_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["read_not_deafened_only"] = True
    save_settings()
    await interaction.response.send_message("Not-deafened-only reading is now **ON**.")


@bot.tree.command(name="readnotdeafened_off", description="Stop filtering reads by deafened state")
async def readnotdeafened_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["read_not_deafened_only"] = False
    save_settings()
    await interaction.response.send_message("Not-deafened-only reading is now **OFF**.")


@bot.tree.command(name="aireply_on", description="Enable short assistant-style voice replies")
async def aireply_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["ai_reply_enabled"] = True
    save_settings()
    await interaction.response.send_message("AI reply mode is now **ON**.")


@bot.tree.command(name="aireply_off", description="Disable assistant-style voice replies")
async def aireply_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["ai_reply_enabled"] = False
    save_settings()
    await interaction.response.send_message("AI reply mode is now **OFF**.")


@bot.tree.command(name="readmuted_on", description="Only read messages from muted users in the linked voice channel")
async def readmuted_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["read_muted_only"] = True
    save_settings()
    await interaction.response.send_message("Muted-only reading is now **ON**.")


@bot.tree.command(name="readmuted_off", description="Read users normally instead of only muted users")
async def readmuted_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["read_muted_only"] = False
    save_settings()
    await interaction.response.send_message("Muted-only reading is now **OFF**.")


@bot.tree.command(name="tts_on", description="Turn TTS on")
async def tts_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = True
    save_settings()
    await interaction.response.send_message("TTS is now **ON**.")


@bot.tree.command(name="tts_off", description="Turn TTS off")
async def tts_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = False
    save_settings()
    await interaction.response.send_message("TTS is now **OFF**.")


@bot.tree.command(name="samevc_on", description="Require users to be in the same VC as the bot")
async def samevc_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = True
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **ON**.")


@bot.tree.command(name="samevc_off", description="Turn off same VC requirement")
async def samevc_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = False
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Skip spam, links, and very short messages")
async def smartfilter_on(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = True
    save_settings()
    await interaction.response.send_message("Smart filter is now **ON**.")


@bot.tree.command(name="smartfilter_off", description="Read all messages without filtering")
async def smartfilter_off(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = False
    save_settings()
    await interaction.response.send_message("Smart filter is now **OFF**.")


@bot.tree.command(name="tts_status", description="Show current TTS settings")
async def tts_status(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    settings = get_guild_settings(interaction.guild.id)
    linked_voice = settings.get("voice_channel_id")
    linked_voice_text = f"<#{linked_voice}>" if linked_voice else "Not linked yet"
    no_mic = settings["no_mic_channel_id"]
    no_mic_text = f"<#{no_mic}>" if no_mic else "Not set"
    ignored = settings["ignored_users"]
    ignored_text = ", ".join(f"<@{user_id}>" for user_id in ignored) if ignored else "None"
    voice_label = VOICE_PROFILES[settings.get("voice_style", "female")]["label"]
    translate_text = settings.get("translation_mode", "off")
    read_muted_text = "On" if settings.get("read_muted_only") else "Off"
    read_not_deafened_text = "On" if settings.get("read_not_deafened_only") else "Off"
    ai_reply_text = "On" if settings.get("ai_reply_enabled") else "Off"
    volume_text = f"{settings.get('volume', 100)}%"

    message = (
        f"**TTS Enabled:** {settings['tts_enabled']}\n"
        f"**No-Mic Channel:** {no_mic_text}\n"
        f"**Linked Voice Channel:** {linked_voice_text}\n"
        f"**Voice Style:** {voice_label}\n"
        f"**Translation Mode:** {translate_text}\n"
        f"**Volume:** {volume_text}\n"
        f"**Read Muted Only:** {read_muted_text}\n"
        f"**Read Not Deafened Only:** {read_not_deafened_text}\n"
        f"**AI Reply Mode:** {ai_reply_text}\n"
        f"**Language When Not Translating:** {settings.get('language', 'en')}\n"
        f"**Max Length:** {settings.get('max_length', 300)} chars\n"
        f"**Same VC Required:** {settings['same_vc_required']}\n"
        f"**Smart Filter:** {settings['smart_filter']}\n"
        f"**Ignored Users:** {ignored_text}\n"
        f"**Username Reading:** Disabled"
    )
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="panel", description="Show how to use the TTS bot")
async def panel(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="TTS Bot - How to Use",
        description="This bot reads messages from a designated text channel aloud in voice chat.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Setup",
        value=(
            "1. Join a voice channel\n"
            "2. Use `/join` once to link that voice channel\n"
            "3. Use `/setnomic #channel` to pick the text channel to read\n"
            "4. The bot auto-joins that linked VC when the first real user joins it"
        ),
        inline=False,
    )
    embed.add_field(
        name="Voice Commands",
        value=(
            "`/join` - Join your voice channel and link it\n"
            "`/leave` - Leave the current voice channel\n"
            "`/skip` - Stop the current TTS message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Voice Options",
        value=(
            "`/voice` - Choose male, female, or neutral style\n"
            "`/translate` - Choose off, english, or original translation mode\n"
            "`/volume` - Set playback volume from 0 to 100\n"
            "`/readmuted_on` / `/readmuted_off` - Read only muted users or everyone\n"
            "`/readnotdeafened_on` / `/readnotdeafened_off` - Read only users who are not deafened\n"
            "`/aireply_on` / `/aireply_off` - Toggle short assistant-style replies\n"
            "`/setlang <code>` - Language used when translation is off"
        ),
        inline=False,
    )
    embed.add_field(
        name="TTS Controls",
        value=(
            "`/setnomic #channel` - Set which text channel gets read aloud\n"
            "`/tts_on` / `/tts_off` - Toggle TTS\n"
            "`/setmaxlength <n>` - Max characters per message\n"
            "`/tts_status` - View all current settings"
        ),
        inline=False,
    )
    embed.add_field(
        name="Extra Settings",
        value=(
            "`/ignore @user` / `/unignore @user` - Ignore or restore a user\n"
            "`/samevc_on` / `/samevc_off` - Require authors to share the VC\n"
            "`/smartfilter_on` / `/smartfilter_off` - Filter spam and links"
        ),
        inline=False,
    )
    embed.set_footer(text="Usernames are never read. The bot leaves a linked VC about 5 seconds after the last real user leaves.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
