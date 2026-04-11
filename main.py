"""
TTS Discord Bot — Production-ready, 24/7 stable.
Proper per-guild async queue, host priority, follow mode, pause/resume, and more.
"""

import os
import re
import json
import time
import shutil
import asyncio
import tempfile
import itertools
from pathlib import Path
from ctypes.util import find_library
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gtts import gTTS


# ─── Opus & ffmpeg ────────────────────────────────────────────────────────────

def load_opus_auto():
    if discord.opus.is_loaded():
        return
    found = find_library("opus")
    if found:
        try:
            discord.opus.load_opus(found)
            print(f"[Opus] Loaded: {found}")
            return
        except Exception:
            pass
    for name in ("opus", "libopus-0", "libopus", "opus-0", "libopus.so.0"):
        try:
            discord.opus.load_opus(name)
            print(f"[Opus] Loaded: {name}")
            return
        except Exception:
            pass
    try:
        import pyogg
        for dll in Path(pyogg.__file__).parent.rglob("*opus*.dll"):
            try:
                discord.opus.load_opus(str(dll))
                print(f"[Opus] Loaded from PyOgg: {dll}")
                return
            except Exception:
                pass
    except ImportError:
        pass
    print("[Opus] WARNING: Could not load opus. Voice will not work.")


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("[ffmpeg] WARNING: not found. Install: sudo apt install ffmpeg")
    else:
        print("[ffmpeg] Found.")


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


# ─── TTS Queue item ───────────────────────────────────────────────────────────

_seq_counter = itertools.count()  # global monotonic counter for stable FIFO ordering


@dataclass(order=True)
class TTSItem:
    """A single item in the TTS playback queue."""
    priority:   int           # 0 = host/urgent, 1 = normal
    seq:        int           # monotonic — ensures FIFO within same priority
    text:       str  = field(compare=False)
    lang:       str  = field(compare=False)
    slow:       bool = field(compare=False)
    max_length: int  = field(compare=False)
    interrupt:  bool = field(compare=False)  # stop current audio before playing


# ─── Per-guild state ──────────────────────────────────────────────────────────

guild_queues:    dict[int, asyncio.PriorityQueue] = {}
guild_workers:   dict[int, asyncio.Task]          = {}
guild_paused:    dict[int, asyncio.Event]         = {}  # set=play, clear=paused
guild_last_spoke: dict[int, float]               = {}  # guild_id -> monotonic time
guild_joining:   set[int]                         = set()  # debounce concurrent joins
user_last_spoke: dict[tuple, float]              = {}  # (guild_id, user_id) -> monotonic


def get_queue(guild_id: int) -> asyncio.PriorityQueue:
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.PriorityQueue()
    return guild_queues[guild_id]


def get_pause_event(guild_id: int) -> asyncio.Event:
    if guild_id not in guild_paused:
        e = asyncio.Event()
        e.set()  # not paused by default
        guild_paused[guild_id] = e
    return guild_paused[guild_id]


# ─── Settings ─────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path("settings.json")
guild_settings: dict = {}


def default_settings() -> dict:
    return {
        # Core
        "tts_enabled":          True,
        "no_mic_channel_id":    None,
        "language":             "en",
        "user_languages":       {},       # str(user_id) -> lang code
        "max_length":           300,
        "slow_tts":             False,
        "idle_timeout":         300,      # seconds; 0 = disabled
        # Name reading
        "say_name":             True,
        "use_nickname":         True,     # nickname vs account username
        "voice_prefix":         "says",   # e.g. "Thomas says hello"
        # Filters
        "smart_filter":         True,
        "ignored_users":        [],
        "message_cooldown":     0,        # seconds between same-user messages
        "same_vc_required":     True,
        "required_role_id":     None,
        # Voice joining
        "autojoin_any":         True,     # auto-join any VC when someone enters
        "auto_join_channel_id": None,     # if set, only auto-join THIS channel
        # Host mode
        "host_id":              None,     # user ID of priority host
        "host_mode":            False,    # host messages jump the queue
        "host_interrupts":      False,    # host messages stop current audio
        "follow_mode":          False,    # bot follows host between VCs
    }


def get_guild_settings(guild_id: int) -> dict:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = default_settings()
    s = guild_settings[guild_id]
    # Back-fill keys added in newer versions
    for k, v in default_settings().items():
        s.setdefault(k, v)
    return s


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for gid_str, s in raw.items():
                guild_settings[int(gid_str)] = s
            print(f"[Settings] Loaded for {len(guild_settings)} guild(s).")
        except Exception as e:
            print(f"[Settings] Failed to load: {e}")


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in guild_settings.items()}, f, indent=2)
    except Exception as e:
        print(f"[Settings] Failed to save: {e}")


load_settings()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def has_permission(interaction: discord.Interaction) -> bool:
    """True if the user may run restricted commands."""
    s = get_guild_settings(interaction.guild.id)
    role_id = s.get("required_role_id")
    if role_id is None:
        return True
    if interaction.user.guild_permissions.manage_guild:
        return True
    return any(r.id == role_id for r in interaction.user.roles)


def clean_message(text: str) -> Optional[str]:
    """Clean raw message text for natural TTS speech."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"https?://\S+", "", text)          # remove links
    text = re.sub(r"www\.\S+", "", text)               # remove www links
    text = re.sub(r"<@!?\d+>", "someone", text)        # @mentions
    text = re.sub(r"<#\d+>", "a channel", text)        # #channel mentions
    text = re.sub(r"<@&\d+>", "a role", text)          # @role mentions
    text = re.sub(r"<a?:\w+:\d+>", "", text)           # custom emoji
    text = re.sub(r"(.)\1{3,}", r"\1\1", text)         # heyyyy -> hey
    text = re.sub(r"([!?.,-]){3,}", r"\1", text)       # !!! -> !
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def should_skip(message: discord.Message, s: dict) -> bool:
    """Return True if this message should NOT be read aloud."""
    if message.author.bot:
        return True
    if not s["tts_enabled"]:
        return True
    if s["no_mic_channel_id"] is None:
        return True
    if message.channel.id != s["no_mic_channel_id"]:
        return True
    if message.author.id in s["ignored_users"]:
        return True

    # Role restriction
    role_id = s.get("required_role_id")
    if role_id is not None:
        has_role = any(r.id == role_id for r in message.author.roles)
        if not has_role and not message.author.guild_permissions.manage_guild:
            return True

    # Per-user message cooldown
    cooldown = s.get("message_cooldown", 0)
    if cooldown > 0:
        key = (message.guild.id, message.author.id)
        if (time.monotonic() - user_last_spoke.get(key, 0.0)) < cooldown:
            return True

    # Smart filter: drop spam, links, very short noise
    if s["smart_filter"]:
        content = message.content.strip()
        if not content:
            return True
        lowered = content.lower()
        spam = {"lol", "lmao", "ok", "k", "w", "?", "??", "😂", "😭"}
        if lowered in spam:
            return True
        if any(p in lowered for p in ("http://", "https://", "www.")):
            return True

    return False


async def in_same_vc(message: discord.Message, s: dict) -> bool:
    """Return True if the bot is connected and the same-VC check passes."""
    vc = message.guild.voice_client
    if not vc or not vc.channel:
        return False
    if not s["same_vc_required"]:
        return True
    if not message.author.voice or not message.author.voice.channel:
        return False
    return message.author.voice.channel.id == vc.channel.id


# ─── Per-guild queue worker ───────────────────────────────────────────────────

async def tts_worker(guild: discord.Guild):
    """
    Long-running task per guild. Pulls TTSItems from the priority queue and
    plays them sequentially. Respects pause state, interrupt flag, and
    voice client reconnects.
    """
    q = get_queue(guild.id)
    pause_event = get_pause_event(guild.id)

    while True:
        # Wait for the next item
        try:
            _pri, _seq, item = await q.get()
        except asyncio.CancelledError:
            break

        try:
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                q.task_done()
                continue

            # Respect pause — but let interrupts through
            if not item.interrupt:
                await pause_event.wait()

            # Stop current audio if this is a priority interrupt
            if item.interrupt and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                await asyncio.sleep(0.15)

            # Wait for any ongoing audio to finish
            while vc.is_playing() or vc.is_paused():
                await asyncio.sleep(0.2)

            vc = guild.voice_client  # re-fetch in case reconnect happened
            if not vc or not vc.is_connected():
                q.task_done()
                continue

            # Clean and truncate text
            cleaned = clean_message(item.text)
            if not cleaned:
                q.task_done()
                continue
            if len(cleaned) > item.max_length:
                cleaned = cleaned[:item.max_length] + "..."

            # Try up to 3 times to generate and play the audio
            for attempt in range(3):
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        mp3 = Path(tmp) / "tts.mp3"

                        # Generate speech (blocking I/O — run in executor)
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: gTTS(text=cleaned, lang=item.lang, slow=item.slow).save(str(mp3))
                        )

                        # Signal when playback is done (called from audio thread)
                        done = asyncio.Event()
                        def _after(err):
                            if err:
                                print(f"[Worker] Playback error: {err}")
                            loop.call_soon_threadsafe(done.set)

                        source = discord.FFmpegPCMAudio(str(mp3))
                        vc.play(source, after=_after)
                        guild_last_spoke[guild.id] = time.monotonic()

                        # Wait for audio to finish (temp dir kept alive)
                        await done.wait()
                    break  # success — exit retry loop

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[Worker] Attempt {attempt + 1}/3 failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1.5)

        except asyncio.CancelledError:
            try:
                q.task_done()
            except Exception:
                pass
            break
        except Exception as e:
            print(f"[Worker] Unexpected error: {e}")
        finally:
            try:
                q.task_done()
            except Exception:
                pass


def ensure_worker(guild: discord.Guild):
    """Start the per-guild worker task if it isn't already running."""
    gid = guild.id
    task = guild_workers.get(gid)
    if task is None or task.done():
        guild_workers[gid] = asyncio.create_task(tts_worker(guild))
        print(f"[Worker] Started for guild {guild.name}")


async def enqueue(
    guild: discord.Guild,
    text: str,
    lang: str,
    slow: bool,
    max_length: int,
    priority: int = 1,
    interrupt: bool = False,
):
    """Add a TTS item to the guild's priority queue."""
    ensure_worker(guild)
    item = TTSItem(
        priority=priority,
        seq=next(_seq_counter),
        text=text,
        lang=lang,
        slow=slow,
        max_length=max_length,
        interrupt=interrupt,
    )
    await get_queue(guild.id).put((item.priority, item.seq, item))


# ─── Safe voice join ──────────────────────────────────────────────────────────

async def safe_join(channel: discord.VoiceChannel, guild: discord.Guild) -> bool:
    """
    Connect to or move into a voice channel safely.
    Uses a per-guild lock to prevent reconnect spam.
    Returns True on success.
    """
    gid = guild.id
    if gid in guild_joining:
        return False
    guild_joining.add(gid)
    try:
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id == channel.id:
                return True  # already there
            await vc.move_to(channel)
        else:
            await channel.connect(timeout=10.0, reconnect=True)
        guild_last_spoke[gid] = time.monotonic()
        return True
    except Exception as e:
        print(f"[Voice] Failed to join {channel.name}: {e}")
        return False
    finally:
        guild_joining.discard(gid)


# ─── Idle timeout ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def idle_check():
    """Leave voice channels that have been idle past the configured timeout."""
    now = time.monotonic()
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            continue
        s = get_guild_settings(guild.id)
        timeout = s.get("idle_timeout", 300)
        if timeout <= 0:
            continue
        last = guild_last_spoke.get(guild.id, now)
        if (now - last) >= timeout:
            try:
                await vc.disconnect()
                guild_last_spoke.pop(guild.id, None)
                print(f"[Idle] Left {guild.name} (idle for {timeout}s)")
            except Exception as e:
                print(f"[Idle] Disconnect error: {e}")


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user} (ID: {bot.user.id})")

    # Clear global commands from Discord API (without touching local tree)
    # This removes the duplicate commands problem permanently.
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        print("[Sync] Cleared global slash commands from Discord.")
    except Exception as e:
        print(f"[Sync] Could not clear global commands: {e}")

    # Sync guild-specific commands (instant — no hour-long wait)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[Sync] {len(synced)} command(s) → {guild.name}")
        except Exception as e:
            print(f"[Sync] Failed for {guild.name}: {e}")

    idle_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        await bot.process_commands(message)
        return

    s = get_guild_settings(message.guild.id)

    if not should_skip(message, s):
        if await in_same_vc(message, s):
            # Record cooldown timestamp
            if s.get("message_cooldown", 0) > 0:
                user_last_spoke[(message.guild.id, message.author.id)] = time.monotonic()

            # Choose display name
            display = (message.author.display_name
                       if s.get("use_nickname", True)
                       else message.author.name)

            # Per-user language override, fall back to server default
            uid = str(message.author.id)
            lang = s.get("user_languages", {}).get(uid) or s.get("language", "en")

            # Build the text to speak
            if s["say_name"]:
                prefix = s.get("voice_prefix", "says")
                full_text = f"{display} {prefix} {message.content}"
            else:
                full_text = message.content

            # Determine priority (host messages jump the queue)
            host_id   = s.get("host_id")
            host_mode = s.get("host_mode", False)
            is_host   = bool(host_id and message.author.id == host_id and host_mode)
            priority  = 0 if is_host else 1
            interrupt = is_host and s.get("host_interrupts", False)

            await enqueue(
                message.guild,
                full_text,
                lang=lang,
                slow=s.get("slow_tts", False),
                max_length=s.get("max_length", 300),
                priority=priority,
                interrupt=interrupt,
            )

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    guild = member.guild
    vc = guild.voice_client
    s = get_guild_settings(guild.id)

    # ── Follow mode: bot follows the designated host ──────────────────────────
    host_id     = s.get("host_id")
    follow_mode = s.get("follow_mode", False)
    if follow_mode and host_id and member.id == host_id and after.channel is not None:
        if before.channel is None or before.channel.id != after.channel.id:
            await safe_join(after.channel, guild)
            return

    # ── Auto-join when a user enters a voice channel ──────────────────────────
    if after.channel is not None and (before.channel is None or before.channel.id != after.channel.id):
        if vc is None or not vc.is_connected():
            auto_id     = s.get("auto_join_channel_id")
            any_channel = s.get("autojoin_any", True)
            if auto_id:
                if after.channel.id == auto_id:
                    await safe_join(after.channel, guild)
            elif any_channel:
                await safe_join(after.channel, guild)

    # ── Auto-leave when bot's channel has no real users left ──────────────────
    if (before.channel is not None
            and vc and vc.channel
            and vc.channel.id == before.channel.id):
        non_bots = [m for m in before.channel.members if not m.bot]
        if not non_bots:
            try:
                await vc.disconnect()
                guild_last_spoke.pop(guild.id, None)
                print(f"[AutoLeave] Left {guild.name} (channel empty)")
            except Exception as e:
                print(f"[AutoLeave] Error: {e}")


# ─── Slash commands ───────────────────────────────────────────────────────────

# ── Voice ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="join", description="Join your current voice channel")
async def cmd_join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return
    await interaction.response.defer()
    ok = await safe_join(interaction.user.voice.channel, interaction.guild)
    if ok:
        await interaction.followup.send(f"Joined **{interaction.user.voice.channel.name}**.")
    else:
        await interaction.followup.send("Could not join — try again in a moment.")


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def cmd_leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    await vc.disconnect()
    guild_last_spoke.pop(interaction.guild.id, None)
    await interaction.response.send_message("Left the voice channel.")


@bot.tree.command(name="skip", description="Skip the current TTS message")
async def cmd_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause TTS playback")
async def cmd_pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    event = get_pause_event(interaction.guild.id)
    if vc and vc.is_playing():
        vc.pause()
    event.clear()
    await interaction.response.send_message("TTS paused. Use `/resume` to continue.")


@bot.tree.command(name="resume", description="Resume paused TTS playback")
async def cmd_resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    event = get_pause_event(interaction.guild.id)
    if vc and vc.is_paused():
        vc.resume()
    event.set()
    await interaction.response.send_message("TTS resumed.")


@bot.tree.command(name="clearqueue", description="Stop current TTS and clear all pending messages")
async def cmd_clearqueue(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    q = get_queue(interaction.guild.id)
    count = 0
    while not q.empty():
        try:
            q.get_nowait()
            q.task_done()
            count += 1
        except asyncio.QueueEmpty:
            break
    await interaction.response.send_message(f"Queue cleared. ({count} item(s) removed)")


@bot.tree.command(name="queue", description="Show how many messages are waiting to be read")
async def cmd_queue(interaction: discord.Interaction):
    count = get_queue(interaction.guild.id).qsize()
    if count == 0:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**{count}** message(s) in the queue.", ephemeral=True)


@bot.tree.command(name="testtts", description="Play a test TTS message to check audio is working")
async def cmd_testtts(interaction: discord.Interaction, text: str = "TTS is working correctly."):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    s = get_guild_settings(interaction.guild.id)
    await interaction.response.send_message(f"Testing: *{text}*", ephemeral=True)
    await enqueue(
        interaction.guild, text,
        lang=s.get("language", "en"),
        slow=s.get("slow_tts", False),
        max_length=500,
        priority=0,
    )


# ── TTS toggles ───────────────────────────────────────────────────────────────

@bot.tree.command(name="tts_on", description="Turn TTS reading on")
async def cmd_tts_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["tts_enabled"] = True; save_settings()
    await interaction.response.send_message("TTS is now **ON**.")


@bot.tree.command(name="tts_off", description="Turn TTS reading off")
async def cmd_tts_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["tts_enabled"] = False; save_settings()
    await interaction.response.send_message("TTS is now **OFF**.")


@bot.tree.command(name="sayname_on", description="Read 'username says' before each message")
async def cmd_sayname_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["say_name"] = True; save_settings()
    await interaction.response.send_message("Username prefix **ON**.")


@bot.tree.command(name="sayname_off", description="Read only message content, no username prefix")
async def cmd_sayname_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["say_name"] = False; save_settings()
    await interaction.response.send_message("Username prefix **OFF**.")


@bot.tree.command(name="nick_on", description="Use server nickname when reading names")
async def cmd_nick_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["use_nickname"] = True; save_settings()
    await interaction.response.send_message("Bot will use server **nicknames**.")


@bot.tree.command(name="nick_off", description="Use account username instead of nickname")
async def cmd_nick_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["use_nickname"] = False; save_settings()
    await interaction.response.send_message("Bot will use **usernames** (not nicknames).")


@bot.tree.command(name="samevc_on", description="Only read messages from users in the same VC as the bot")
async def cmd_samevc_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["same_vc_required"] = True; save_settings()
    await interaction.response.send_message("Same VC requirement **ON**.")


@bot.tree.command(name="samevc_off", description="Read messages regardless of which VC the user is in")
async def cmd_samevc_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["same_vc_required"] = False; save_settings()
    await interaction.response.send_message("Same VC requirement **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Filter spam, links, and short filler messages")
async def cmd_sf_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["smart_filter"] = True; save_settings()
    await interaction.response.send_message("Smart filter **ON**.")


@bot.tree.command(name="smartfilter_off", description="Read all messages without filtering")
async def cmd_sf_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["smart_filter"] = False; save_settings()
    await interaction.response.send_message("Smart filter **OFF**.")


@bot.tree.command(name="speed_slow", description="Switch TTS to slow, clear speech mode")
async def cmd_speed_slow(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["slow_tts"] = True; save_settings()
    await interaction.response.send_message("TTS speed set to **slow**.")


@bot.tree.command(name="speed_normal", description="Switch TTS back to normal speed")
async def cmd_speed_normal(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["slow_tts"] = False; save_settings()
    await interaction.response.send_message("TTS speed set to **normal**.")


# ── Settings ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="setnomic", description="Set the text channel the bot reads aloud")
async def cmd_setnomic(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["no_mic_channel_id"] = channel.id; save_settings()
    await interaction.response.send_message(f"No-mic channel set to {channel.mention}.")


@bot.tree.command(name="setlang", description="Set the server-wide TTS language (en, es, fr, de, ja…)")
async def cmd_setlang(interaction: discord.Interaction, language: str):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["language"] = language.strip().lower(); save_settings()
    await interaction.response.send_message(f"Server TTS language set to `{language}`.")


@bot.tree.command(name="setmylang", description="Set your personal TTS language (overrides server default)")
async def cmd_setmylang(interaction: discord.Interaction, language: str):
    s = get_guild_settings(interaction.guild.id)
    s.setdefault("user_languages", {})[str(interaction.user.id)] = language.strip().lower()
    save_settings()
    await interaction.response.send_message(
        f"Your TTS language set to `{language}`.", ephemeral=True
    )


@bot.tree.command(name="clearmylang", description="Remove your personal language and use server default")
async def cmd_clearmylang(interaction: discord.Interaction):
    s = get_guild_settings(interaction.guild.id)
    s.get("user_languages", {}).pop(str(interaction.user.id), None)
    save_settings()
    await interaction.response.send_message("Personal language cleared.", ephemeral=True)


@bot.tree.command(name="setmaxlength", description="Set max characters per message (20–1000, default 300)")
async def cmd_setmaxlength(interaction: discord.Interaction, characters: int):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    if not 20 <= characters <= 1000:
        await interaction.response.send_message("Must be between 20 and 1000.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["max_length"] = characters; save_settings()
    await interaction.response.send_message(f"Max message length set to **{characters}** characters.")


@bot.tree.command(name="setcooldown", description="Seconds a user must wait between TTS messages (0 = off)")
async def cmd_setcooldown(interaction: discord.Interaction, seconds: int):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    if not 0 <= seconds <= 60:
        await interaction.response.send_message("Must be between 0 and 60.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["message_cooldown"] = seconds; save_settings()
    msg = "Cooldown **disabled**." if seconds == 0 else f"Cooldown set to **{seconds}s** per user."
    await interaction.response.send_message(msg)


@bot.tree.command(name="setvoiceprefix", description="Word between username and message (default: 'says')")
async def cmd_setvoiceprefix(interaction: discord.Interaction, prefix: str):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["voice_prefix"] = prefix.strip(); save_settings()
    await interaction.response.send_message(
        f"Voice prefix set to `{prefix.strip()}`. (e.g. 'Thomas {prefix.strip()} hello')"
    )


@bot.tree.command(name="setrole", description="Restrict TTS commands to users with a specific role")
async def cmd_setrole(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["required_role_id"] = role.id; save_settings()
    await interaction.response.send_message(f"TTS commands restricted to **{role.name}**.")


@bot.tree.command(name="clearrole", description="Remove the role restriction from TTS commands")
async def cmd_clearrole(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["required_role_id"] = None; save_settings()
    await interaction.response.send_message("Role restriction removed. Anyone can use TTS commands.")


@bot.tree.command(name="setautojoin", description="Set a specific voice channel to auto-join")
async def cmd_setautojoin(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["auto_join_channel_id"] = channel.id; save_settings()
    await interaction.response.send_message(f"Auto-join pinned to **{channel.name}**.")


@bot.tree.command(name="clearautojoin", description="Remove the designated auto-join channel")
async def cmd_clearautojoin(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["auto_join_channel_id"] = None; save_settings()
    await interaction.response.send_message("Auto-join channel cleared.")


@bot.tree.command(name="autojoin_any", description="Toggle whether bot auto-joins any voice channel")
async def cmd_autojoin_any(interaction: discord.Interaction, enabled: bool):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["autojoin_any"] = enabled; save_settings()
    await interaction.response.send_message(f"Auto-join any channel: **{'ON' if enabled else 'OFF'}**.")


# ── Host mode ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="sethost", description="Set the priority host — their messages jump the queue")
async def cmd_sethost(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["host_id"] = user.id; save_settings()
    await interaction.response.send_message(f"**{user.display_name}** is now the priority host.")


@bot.tree.command(name="clearhost", description="Remove the priority host")
async def cmd_clearhost(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["host_id"] = None; save_settings()
    await interaction.response.send_message("Priority host cleared.")


@bot.tree.command(name="hostmode", description="Toggle host priority — host messages jump the queue")
async def cmd_hostmode(interaction: discord.Interaction, enabled: bool):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["host_mode"] = enabled; save_settings()
    await interaction.response.send_message(f"Host priority mode: **{'ON' if enabled else 'OFF'}**.")


@bot.tree.command(name="hostinterrupt", description="Toggle whether host messages interrupt current playback")
async def cmd_hostinterrupt(interaction: discord.Interaction, enabled: bool):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["host_interrupts"] = enabled; save_settings()
    await interaction.response.send_message(f"Host interrupt: **{'ON' if enabled else 'OFF'}**.")


@bot.tree.command(name="followmode", description="Bot automatically follows the host between voice channels")
async def cmd_followmode(interaction: discord.Interaction, enabled: bool):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["follow_mode"] = enabled; save_settings()
    await interaction.response.send_message(f"Follow mode: **{'ON' if enabled else 'OFF'}**.")


# ── Users ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ignore", description="Stop reading a user's messages aloud")
async def cmd_ignore(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    if user.id not in s["ignored_users"]:
        s["ignored_users"].append(user.id); save_settings()
    await interaction.response.send_message(f"{user.display_name} is now ignored.")


@bot.tree.command(name="unignore", description="Resume reading a user's messages aloud")
async def cmd_unignore(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    if user.id in s["ignored_users"]:
        s["ignored_users"].remove(user.id); save_settings()
    await interaction.response.send_message(f"{user.display_name} will be read aloud again.")


# ── Info ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="tts_status", description="Show all current TTS settings")
async def cmd_status(interaction: discord.Interaction):
    s = get_guild_settings(interaction.guild.id)

    def ch(cid):   return f"<#{cid}>" if cid else "Not set"
    def rl(rid):   return f"<@&{rid}>" if rid else "None (everyone)"
    def usr(uid):  return f"<@{uid}>" if uid else "None"
    def oo(val):   return "✅ ON" if val else "❌ OFF"

    ignored   = ", ".join(f"<@{u}>" for u in s["ignored_users"]) or "None"
    timeout   = f"{s.get('idle_timeout', 300)}s" if s.get("idle_timeout", 300) > 0 else "Disabled"
    q_size    = get_queue(interaction.guild.id).qsize()
    prefix    = s.get("voice_prefix", "says")
    paused    = not get_pause_event(interaction.guild.id).is_set()

    embed = discord.Embed(title="📊 TTS Bot Status", color=discord.Color.blurple())
    embed.add_field(name="Core", value=(
        f"**TTS:** {oo(s['tts_enabled'])}\n"
        f"**No-mic channel:** {ch(s['no_mic_channel_id'])}\n"
        f"**Language:** `{s.get('language','en')}`\n"
        f"**Speed:** {'Slow' if s.get('slow_tts') else 'Normal'}\n"
        f"**Max length:** {s.get('max_length', 300)} chars\n"
        f"**Queue size:** {q_size}\n"
        f"**Paused:** {'Yes' if paused else 'No'}"
    ), inline=True)
    embed.add_field(name="Name & Voice", value=(
        f"**Say name:** {oo(s['say_name'])}\n"
        f"**Use nickname:** {oo(s.get('use_nickname', True))}\n"
        f"**Voice prefix:** `{prefix}`\n"
        f"**Cooldown:** {s.get('message_cooldown', 0)}s\n"
        f"**Idle timeout:** {timeout}"
    ), inline=True)
    embed.add_field(name="Joining", value=(
        f"**Same VC required:** {oo(s['same_vc_required'])}\n"
        f"**Auto-join any:** {oo(s.get('autojoin_any', True))}\n"
        f"**Auto-join channel:** {ch(s.get('auto_join_channel_id'))}\n"
        f"**Follow mode:** {oo(s.get('follow_mode', False))}"
    ), inline=True)
    embed.add_field(name="Host", value=(
        f"**Host:** {usr(s.get('host_id'))}\n"
        f"**Host mode:** {oo(s.get('host_mode', False))}\n"
        f"**Host interrupts:** {oo(s.get('host_interrupts', False))}"
    ), inline=True)
    embed.add_field(name="Filters & Access", value=(
        f"**Smart filter:** {oo(s['smart_filter'])}\n"
        f"**Required role:** {rl(s.get('required_role_id'))}\n"
        f"**Ignored users:** {ignored}"
    ), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="panel", description="Show all TTS bot commands")
async def cmd_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="TTS Bot — Command Panel",
        description="Reads your text channel aloud in voice chat for car meets and more.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="🔧 Setup", value=(
        "`/setnomic #channel` — Text channel to read\n"
        "`/setautojoin #vc` — Pin auto-join to one VC\n"
        "`/clearautojoin` — Go back to any channel\n"
        "`/autojoin_any on|off` — Toggle auto-join\n"
        "`/setrole @role` / `/clearrole` — Restrict commands"
    ), inline=False)
    embed.add_field(name="🔊 Voice", value=(
        "`/join` — Join your VC\n"
        "`/leave` — Leave VC\n"
        "`/skip` — Skip current message\n"
        "`/pause` / `/resume` — Pause or resume TTS\n"
        "`/queue` — Show queue size\n"
        "`/clearqueue` — Clear all pending messages\n"
        "`/testtts [text]` — Test TTS audio"
    ), inline=False)
    embed.add_field(name="🗣️ TTS Controls", value=(
        "`/tts_on` / `/tts_off` — Toggle TTS\n"
        "`/setlang <code>` — Server language (en, es, fr…)\n"
        "`/setmylang <code>` / `/clearmylang` — Personal language\n"
        "`/setmaxlength <n>` — Max chars (default 300)\n"
        "`/setcooldown <s>` — Per-user message cooldown\n"
        "`/speed_slow` / `/speed_normal` — Speech speed"
    ), inline=False)
    embed.add_field(name="👤 Name & Users", value=(
        "`/sayname_on` / `/sayname_off` — Toggle name prefix\n"
        "`/nick_on` / `/nick_off` — Nickname vs username\n"
        "`/setvoiceprefix <word>` — e.g. 'says', 'yells'\n"
        "`/ignore @user` / `/unignore @user`"
    ), inline=False)
    embed.add_field(name="⭐ Host Mode", value=(
        "`/sethost @user` / `/clearhost` — Set priority host\n"
        "`/hostmode on|off` — Enable host queue priority\n"
        "`/hostinterrupt on|off` — Host skips current audio\n"
        "`/followmode on|off` — Bot follows host's VC"
    ), inline=False)
    embed.add_field(name="⚙️ Filters", value=(
        "`/samevc_on` / `/samevc_off` — Same VC requirement\n"
        "`/smartfilter_on` / `/smartfilter_off` — Filter spam & links"
    ), inline=False)
    embed.add_field(name="📊 Info", value="`/tts_status` — View all settings", inline=False)
    embed.set_footer(text="Settings persist after restarts. Bot auto-leaves when VC is empty or after idle timeout.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
