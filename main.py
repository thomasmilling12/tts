import os
import re
import json
import shutil
import asyncio
import tempfile
from pathlib import Path
from ctypes.util import find_library

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS


# ─── Opus loader ─────────────────────────────────────────────────────────────

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
        from pathlib import Path as _P
        for dll in _P(pyogg.__file__).parent.rglob("*opus*.dll"):
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
        print("WARNING: ffmpeg not found.  Install: sudo apt install ffmpeg")
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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

SETTINGS_FILE = Path("settings.json")

guild_settings: dict = {}
guild_locks: dict = {}
guild_last_spoke: dict = {}     # guild_id -> float (loop time)
guild_queue_count: dict = {}    # guild_id -> int  (items waiting or playing)
guild_clear_flag: dict = {}     # guild_id -> bool (clearqueue requested)
user_last_spoke: dict = {}      # (guild_id, user_id) -> float (epoch time)


# ─── Settings ────────────────────────────────────────────────────────────────

def default_settings() -> dict:
    return {
        "tts_enabled":        True,
        "no_mic_channel_id":  None,
        "same_vc_required":   True,
        "smart_filter":       True,
        "ignored_users":      [],
        "say_name":           True,
        "use_nickname":       True,   # use server nickname vs username
        "language":           "en",
        "user_languages":     {},     # str(user_id) -> lang code
        "max_length":         300,
        "idle_timeout":       300,
        "slow_tts":           False,  # gTTS slow mode
        "required_role_id":   None,   # role ID required to use commands
        "auto_join_channel_id": None, # voice channel ID to auto-join
        "message_cooldown":   0,      # seconds between same-user messages
    }


def get_guild_settings(guild_id: int) -> dict:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = default_settings()
    s = guild_settings[guild_id]
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


# ─── Permission helper ───────────────────────────────────────────────────────

def has_permission(interaction: discord.Interaction) -> bool:
    """Returns True if the user may use restricted commands."""
    settings = get_guild_settings(interaction.guild.id)
    role_id = settings.get("required_role_id")
    if role_id is None:
        return True
    member = interaction.user
    if member.guild_permissions.manage_guild:
        return True
    return any(r.id == role_id for r in member.roles)


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

    # Role restriction — if set, only that role (or manage_guild) may be read
    role_id = settings.get("required_role_id")
    if role_id is not None:
        has_role = any(r.id == role_id for r in message.author.roles)
        if not has_role and not message.author.guild_permissions.manage_guild:
            return True

    # Per-user cooldown
    cooldown = settings.get("message_cooldown", 0)
    if cooldown > 0:
        key = (message.guild.id, message.author.id)
        last = user_last_spoke.get(key, 0.0)
        import time
        if (time.monotonic() - last) < cooldown:
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

async def speak_text(
    guild: discord.Guild,
    text: str,
    lang: str = "en",
    slow: bool = False,
    max_length: int = 300,
):
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    lock = get_guild_lock(guild.id)
    guild_queue_count[guild.id] = guild_queue_count.get(guild.id, 0) + 1

    async with lock:
        # clearqueue was requested while we waited — drop this item
        if guild_clear_flag.get(guild.id):
            guild_queue_count[guild.id] = max(0, guild_queue_count.get(guild.id, 1) - 1)
            return

        cleaned = clean_message(text)
        if not cleaned:
            guild_queue_count[guild.id] = max(0, guild_queue_count.get(guild.id, 1) - 1)
            return

        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length] + "..."

        # Retry up to 3 times on gTTS failure
        for attempt in range(3):
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    mp3 = Path(tmp) / "tts.mp3"
                    tts = gTTS(text=cleaned, lang=lang, slow=slow)
                    tts.save(str(mp3))

                    while voice_client.is_playing() or voice_client.is_paused():
                        await asyncio.sleep(0.3)

                    if not voice_client.is_connected():
                        break

                    source = discord.FFmpegPCMAudio(str(mp3))
                    voice_client.play(source)
                    guild_last_spoke[guild.id] = asyncio.get_event_loop().time()

                    while voice_client.is_playing():
                        await asyncio.sleep(0.3)
                break  # success
            except Exception as e:
                print(f"[TTS] Attempt {attempt + 1}/3 failed in guild {guild.id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.5)

        guild_queue_count[guild.id] = max(0, guild_queue_count.get(guild.id, 1) - 1)


# ─── Idle timeout ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def idle_check():
    now = asyncio.get_event_loop().time()
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            continue
        settings = get_guild_settings(guild.id)
        timeout = settings.get("idle_timeout", 300)
        if timeout <= 0:
            continue
        last = guild_last_spoke.get(guild.id, now)
        if (now - last) >= timeout:
            try:
                await vc.disconnect()
                guild_last_spoke.pop(guild.id, None)
                print(f"[Idle] Auto-left guild {guild.id}")
            except Exception as e:
                print(f"[Idle] Disconnect failed: {e}")


# ─── Bot events ──────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to {guild.name}")
        except Exception as e:
            print(f"Sync failed for {guild.name}: {e}")
    idle_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        await bot.process_commands(message)
        return

    settings = get_guild_settings(message.guild.id)

    if not should_skip_message(message, settings):
        ok, _ = await ensure_same_vc(message, settings)
        if ok:
            # Record cooldown timestamp
            cooldown = settings.get("message_cooldown", 0)
            if cooldown > 0:
                import time
                user_last_spoke[(message.guild.id, message.author.id)] = time.monotonic()

            # Choose display name
            if settings.get("use_nickname", True):
                display = message.author.display_name
            else:
                display = message.author.name

            # Per-user language override
            uid = str(message.author.id)
            lang = settings.get("user_languages", {}).get(uid, settings.get("language", "en"))

            if settings["say_name"]:
                text_to_read = f"{display} says {message.content}"
            else:
                text_to_read = message.content

            try:
                await speak_text(
                    message.guild,
                    text_to_read,
                    lang=lang,
                    slow=settings.get("slow_tts", False),
                    max_length=settings.get("max_length", 300),
                )
            except Exception as e:
                print(f"[TTS] Error in guild {message.guild.id}: {e}")

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild = member.guild
    vc = guild.voice_client
    settings = get_guild_settings(guild.id)
    auto_join_id = settings.get("auto_join_channel_id")

    # Someone joined a voice channel
    if after.channel is not None and (before.channel is None or before.channel != after.channel):
        if vc is None or not vc.is_connected():
            # If a designated auto-join channel is set, only join that one
            if auto_join_id:
                if after.channel.id == auto_join_id:
                    try:
                        await after.channel.connect()
                        guild_last_spoke[guild.id] = asyncio.get_event_loop().time()
                    except Exception as e:
                        print(f"[AutoJoin] Failed: {e}")
            else:
                # No designated channel — join any channel (original behaviour)
                try:
                    await after.channel.connect()
                    guild_last_spoke[guild.id] = asyncio.get_event_loop().time()
                except Exception as e:
                    print(f"[AutoJoin] Failed: {e}")

    # Someone left — auto-leave if bot's channel is now empty
    if before.channel is not None and vc and vc.channel == before.channel:
        non_bots = [m for m in before.channel.members if not m.bot]
        if len(non_bots) == 0:
            try:
                await vc.disconnect()
                guild_last_spoke.pop(guild.id, None)
            except Exception as e:
                print(f"[AutoLeave] Failed: {e}")


# ─── Slash commands ───────────────────────────────────────────────────────────

# — Voice —

@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return
    await interaction.response.defer()
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect()
        guild_last_spoke[interaction.guild.id] = asyncio.get_event_loop().time()
        await interaction.followup.send(f"Joined **{channel.name}**.")
    except Exception as e:
        await interaction.followup.send(f"Failed to join: `{e}`")


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    await interaction.response.defer()
    await vc.disconnect()
    guild_last_spoke.pop(interaction.guild.id, None)
    await interaction.followup.send("Disconnected.")


@bot.tree.command(name="skip", description="Stop the current TTS message")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)


@bot.tree.command(name="clearqueue", description="Stop current TTS and clear all pending messages")
async def clearqueue(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    guild_clear_flag[interaction.guild.id] = True
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    count = guild_queue_count.get(interaction.guild.id, 0)
    guild_queue_count[interaction.guild.id] = 0
    # Reset flag after a moment so future messages work
    async def reset_flag():
        await asyncio.sleep(1)
        guild_clear_flag[interaction.guild.id] = False
    asyncio.create_task(reset_flag())
    await interaction.response.send_message(f"Queue cleared. ({count} item(s) removed)")


@bot.tree.command(name="queue", description="Show how many messages are waiting to be read")
async def queue(interaction: discord.Interaction):
    count = guild_queue_count.get(interaction.guild.id, 0)
    if count == 0:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**{count}** message(s) in the queue.", ephemeral=True)


# — TTS toggles —

@bot.tree.command(name="tts_on", description="Turn TTS on")
async def tts_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = True
    save_settings()
    await interaction.response.send_message("TTS is now **ON**.")


@bot.tree.command(name="tts_off", description="Turn TTS off")
async def tts_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["tts_enabled"] = False
    save_settings()
    await interaction.response.send_message("TTS is now **OFF**.")


@bot.tree.command(name="sayname_on", description="Read 'username says' before each message")
async def sayname_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["say_name"] = True
    save_settings()
    await interaction.response.send_message("Username prefix is now **ON**.")


@bot.tree.command(name="sayname_off", description="Read only the message, no username prefix")
async def sayname_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["say_name"] = False
    save_settings()
    await interaction.response.send_message("Username prefix is now **OFF**.")


@bot.tree.command(name="nick_on", description="Use server nickname when saying names")
async def nick_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["use_nickname"] = True
    save_settings()
    await interaction.response.send_message("Bot will now use server **nicknames**.")


@bot.tree.command(name="nick_off", description="Use account username instead of nickname when saying names")
async def nick_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["use_nickname"] = False
    save_settings()
    await interaction.response.send_message("Bot will now use **usernames** (not nicknames).")


@bot.tree.command(name="samevc_on", description="Require users to be in the same VC as the bot")
async def samevc_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = True
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **ON**.")


@bot.tree.command(name="samevc_off", description="Read messages regardless of which VC the user is in")
async def samevc_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["same_vc_required"] = False
    save_settings()
    await interaction.response.send_message("Same VC requirement is now **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Filter out spam, links, and very short messages")
async def smartfilter_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = True
    save_settings()
    await interaction.response.send_message("Smart filter is now **ON**.")


@bot.tree.command(name="smartfilter_off", description="Read all messages without filtering")
async def smartfilter_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["smart_filter"] = False
    save_settings()
    await interaction.response.send_message("Smart filter is now **OFF**.")


@bot.tree.command(name="speed_slow", description="Switch TTS to slow/clear speech mode")
async def speed_slow(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["slow_tts"] = True
    save_settings()
    await interaction.response.send_message("TTS speed set to **slow**.")


@bot.tree.command(name="speed_normal", description="Switch TTS back to normal speed")
async def speed_normal(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["slow_tts"] = False
    save_settings()
    await interaction.response.send_message("TTS speed set to **normal**.")


# — Settings —

@bot.tree.command(name="setnomic", description="Set the no-mic text channel for TTS")
async def setnomic(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["no_mic_channel_id"] = channel.id
    save_settings()
    await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")


@bot.tree.command(name="setlang", description="Set the server-wide TTS language (e.g. en, es, fr, de, ja)")
async def setlang(interaction: discord.Interaction, language: str):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["language"] = language.strip().lower()
    save_settings()
    await interaction.response.send_message(f"Server TTS language set to `{language}`.")


@bot.tree.command(name="setmylang", description="Set your personal TTS language (overrides server default)")
async def setmylang(interaction: discord.Interaction, language: str):
    settings = get_guild_settings(interaction.guild.id)
    if "user_languages" not in settings:
        settings["user_languages"] = {}
    settings["user_languages"][str(interaction.user.id)] = language.strip().lower()
    save_settings()
    await interaction.response.send_message(
        f"Your personal TTS language set to `{language}`. Only your messages will use this.",
        ephemeral=True
    )


@bot.tree.command(name="clearmylang", description="Remove your personal TTS language and use the server default")
async def clearmylang(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings.get("user_languages", {}).pop(str(interaction.user.id), None)
    save_settings()
    await interaction.response.send_message("Your personal language has been cleared. Using server default.", ephemeral=True)


@bot.tree.command(name="setmaxlength", description="Set max characters per message (default 300)")
async def setmaxlength(interaction: discord.Interaction, characters: int):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if characters < 20 or characters > 1000:
        await interaction.response.send_message("Choose a value between 20 and 1000.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["max_length"] = characters
    save_settings()
    await interaction.response.send_message(f"Max message length set to **{characters}** characters.")


@bot.tree.command(name="setcooldown", description="Seconds a user must wait between TTS messages (0 = off)")
async def setcooldown(interaction: discord.Interaction, seconds: int):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if seconds < 0 or seconds > 60:
        await interaction.response.send_message("Choose a value between 0 and 60.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["message_cooldown"] = seconds
    save_settings()
    if seconds == 0:
        await interaction.response.send_message("Message cooldown **disabled**.")
    else:
        await interaction.response.send_message(f"Message cooldown set to **{seconds}s** per user.")


@bot.tree.command(name="setrole", description="Restrict TTS commands to a specific role")
async def setrole(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission to use this.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["required_role_id"] = role.id
    save_settings()
    await interaction.response.send_message(f"TTS commands restricted to **{role.name}** (and server managers).")


@bot.tree.command(name="clearrole", description="Remove the role restriction from TTS commands")
async def clearrole(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission to use this.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["required_role_id"] = None
    save_settings()
    await interaction.response.send_message("Role restriction removed. Anyone can use TTS commands.")


@bot.tree.command(name="setautojoin", description="Set a voice channel the bot auto-joins (instead of any channel)")
async def setautojoin(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["auto_join_channel_id"] = channel.id
    save_settings()
    await interaction.response.send_message(f"Bot will now only auto-join **{channel.name}**.")


@bot.tree.command(name="clearautojoin", description="Remove the designated auto-join channel (bot joins any channel again)")
async def clearautojoin(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    settings["auto_join_channel_id"] = None
    save_settings()
    await interaction.response.send_message("Auto-join channel cleared. Bot will join any channel again.")


# — Users —

@bot.tree.command(name="ignore", description="Stop reading a user's messages")
async def ignore(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    if user.id not in settings["ignored_users"]:
        settings["ignored_users"].append(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will no longer be read aloud.")


@bot.tree.command(name="unignore", description="Resume reading a user's messages")
async def unignore(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    settings = get_guild_settings(interaction.guild.id)
    if user.id in settings["ignored_users"]:
        settings["ignored_users"].remove(user.id)
        save_settings()
    await interaction.response.send_message(f"{user.display_name} will be read aloud again.")


# — Info —

@bot.tree.command(name="tts_status", description="Show current TTS settings")
async def tts_status(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    no_mic = settings["no_mic_channel_id"]
    no_mic_text = f"<#{no_mic}>" if no_mic else "Not set"
    ignored = settings["ignored_users"]
    ignored_text = ", ".join(f"<@{uid}>" for uid in ignored) if ignored else "None"
    timeout = settings.get("idle_timeout", 300)
    timeout_text = f"{timeout}s" if timeout > 0 else "Disabled"
    role_id = settings.get("required_role_id")
    role_text = f"<@&{role_id}>" if role_id else "None (everyone)"
    auto_join = settings.get("auto_join_channel_id")
    auto_join_text = f"<#{auto_join}>" if auto_join else "Any channel"
    cooldown = settings.get("message_cooldown", 0)
    user_langs = settings.get("user_languages", {})
    queue_size = guild_queue_count.get(interaction.guild.id, 0)

    msg = (
        f"**TTS Enabled:** {settings['tts_enabled']}\n"
        f"**No-Mic Channel:** {no_mic_text}\n"
        f"**Language:** `{settings.get('language', 'en')}`\n"
        f"**Speed:** {'Slow' if settings.get('slow_tts') else 'Normal'}\n"
        f"**Max Length:** {settings.get('max_length', 300)} chars\n"
        f"**Say Name:** {settings.get('say_name', True)} "
        f"({'nickname' if settings.get('use_nickname', True) else 'username'})\n"
        f"**Same VC Required:** {settings['same_vc_required']}\n"
        f"**Smart Filter:** {settings['smart_filter']}\n"
        f"**Idle Timeout:** {timeout_text}\n"
        f"**Message Cooldown:** {cooldown}s\n"
        f"**Required Role:** {role_text}\n"
        f"**Auto-Join Channel:** {auto_join_text}\n"
        f"**Queue Size:** {queue_size}\n"
        f"**Per-User Languages:** {len(user_langs)} set\n"
        f"**Ignored Users:** {ignored_text}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="panel", description="Show all TTS bot commands")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="TTS Bot — Command Panel",
        description="Reads messages from a text channel aloud in voice chat.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🔧 Setup",
        value=(
            "`/setnomic #channel` — Set which text channel to read\n"
            "`/setautojoin #vc` — Auto-join a specific voice channel\n"
            "`/clearautojoin` — Go back to joining any channel\n"
            "`/setrole @role` — Restrict commands to a role\n"
            "`/clearrole` — Remove role restriction"
        ),
        inline=False
    )
    embed.add_field(
        name="🔊 Voice",
        value=(
            "`/join` — Join your voice channel\n"
            "`/leave` — Leave the voice channel\n"
            "`/skip` — Skip the current message\n"
            "`/queue` — Show how many messages are queued\n"
            "`/clearqueue` — Stop and clear all pending messages"
        ),
        inline=False
    )
    embed.add_field(
        name="🗣️ TTS Controls",
        value=(
            "`/tts_on` / `/tts_off` — Toggle TTS\n"
            "`/setlang <code>` — Server language (en, es, fr, de, ja…)\n"
            "`/setmylang <code>` — Your personal language\n"
            "`/clearmylang` — Remove your personal language\n"
            "`/setmaxlength <n>` — Max characters (default 300)\n"
            "`/setcooldown <s>` — Seconds between same-user messages\n"
            "`/speed_slow` / `/speed_normal` — TTS speed"
        ),
        inline=False
    )
    embed.add_field(
        name="👤 User Controls",
        value=(
            "`/sayname_on` / `/sayname_off` — Toggle username prefix\n"
            "`/nick_on` / `/nick_off` — Use nickname vs username\n"
            "`/ignore @user` — Stop reading a user\n"
            "`/unignore @user` — Resume reading a user"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Filters",
        value=(
            "`/samevc_on` / `/samevc_off` — Require same VC\n"
            "`/smartfilter_on` / `/smartfilter_off` — Filter spam & links"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Info",
        value="`/tts_status` — View all current settings",
        inline=False
    )
    embed.set_footer(text="Settings persist after restarts. Bot auto-leaves after 5 min idle.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
