"""
TTS Discord Bot — Production-ready, 24/7 stable for live car meets.

Architecture:
- Per-guild async priority queue with peek/remove support
- Dedicated per-guild worker task with auto-restart
- gTTS audio cache to avoid regenerating repeated phrases
- Safe voice join/leave with settle delay and re-check
- Host priority, follow mode, pause/resume, smart filter
"""

import os
import re
import bisect
import json
import timeyes
import shutil
import asyncio
import tempfile
import itertools
import unicodedata
from collections import OrderedDict
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
                print(f"[Opus] Loaded via PyOgg: {dll}")
                return
            except Exception:
                pass
    except ImportError:
        pass
    print("[Opus] WARNING: Could not load opus — voice will not work.")


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("[ffmpeg] WARNING: Not found. Install: sudo apt install ffmpeg")
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
intents.guilds        = True
intents.messages      = True
intents.voice_states  = True
intents.members       = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ─── TTS audio cache (LRU, 50-entry) ─────────────────────────────────────────
# Caches gTTS-generated audio as bytes to avoid regenerating repeated phrases.

_tts_cache: OrderedDict[tuple, bytes] = OrderedDict()
_TTS_CACHE_MAX = 50


def cache_get(text: str, lang: str, slow: bool) -> Optional[bytes]:
    key = (text, lang, slow)
    if key in _tts_cache:
        _tts_cache.move_to_end(key)
        return _tts_cache[key]
    return None


def cache_put(text: str, lang: str, slow: bool, data: bytes) -> None:
    key = (text, lang, slow)
    _tts_cache[key] = data
    _tts_cache.move_to_end(key)
    while len(_tts_cache) > _TTS_CACHE_MAX:
        _tts_cache.popitem(last=False)


# ─── Queue item ───────────────────────────────────────────────────────────────

_seq_counter = itertools.count()


@dataclass(order=True)
class TTSItem:
    """A single entry in the guild TTS queue. Sorted by (priority, seq)."""
    priority:   int           # 0 = host/urgent, 1 = normal
    seq:        int           # monotonic — keeps FIFO ordering within priority
    text:       str  = field(compare=False)
    lang:       str  = field(compare=False)
    slow:       bool = field(compare=False)
    max_length: int  = field(compare=False)
    interrupt:  bool = field(compare=False)  # True = stop current audio first


# ─── Per-guild queue (peek + remove support) ──────────────────────────────────

class GuildQueue:
    """
    Priority queue backed by a sorted list.
    Supports peek() and remove() without consuming items,
    which asyncio.PriorityQueue does not.
    """

    def __init__(self):
        self._items: list[TTSItem] = []
        self._event = asyncio.Event()

    async def put(self, item: TTSItem) -> None:
        bisect.insort(self._items, item)
        self._event.set()

    async def get(self) -> TTSItem:
        """Block until an item is available, then return and remove it."""
        while True:
            if self._items:
                item = self._items.pop(0)
                if not self._items:
                    self._event.clear()
                return item
            self._event.clear()
            await self._event.wait()

    def peek(self, n: int = 5) -> list[TTSItem]:
        """Return next n items without removing them."""
        return list(self._items[:n])

    def remove(self, index: int) -> Optional[TTSItem]:
        """Remove item at 0-based index. Returns item or None."""
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            if not self._items:
                self._event.clear()
            return item
        return None

    def clear(self) -> int:
        """Remove all items. Returns count."""
        count = len(self._items)
        self._items.clear()
        self._event.clear()
        return count

    def size(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return len(self._items) == 0


# ─── Guild state ──────────────────────────────────────────────────────────────

guild_queues:       dict[int, GuildQueue]      = {}
guild_workers:      dict[int, asyncio.Task]    = {}
guild_paused:       dict[int, asyncio.Event]   = {}  # set=playing, clear=paused
guild_last_activity: dict[int, float]          = {}  # last relevant event (monotonic)
guild_joining:      set[int]                   = set()  # debounce concurrent joins
guild_moving:       set[int]                   = set()  # currently moving channels (follow)
user_last_spoke:    dict[tuple, float]         = {}  # (guild_id, user_id) -> monotonic
user_last_content:  dict[tuple, str]           = {}  # (guild_id, user_id) -> last msg text


def get_queue(guild_id: int) -> GuildQueue:
    if guild_id not in guild_queues:
        guild_queues[guild_id] = GuildQueue()
    return guild_queues[guild_id]


def get_pause_event(guild_id: int) -> asyncio.Event:
    if guild_id not in guild_paused:
        e = asyncio.Event()
        e.set()  # not paused by default
        guild_paused[guild_id] = e
    return guild_paused[guild_id]


def touch_activity(guild_id: int):
    """Update the last-activity timestamp for a guild."""
    guild_last_activity[guild_id] = time.monotonic()


# ─── Settings ─────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path("settings.json")
guild_settings: dict = {}


def default_settings() -> dict:
    return {
        # Core
        "tts_enabled":          True,
        "no_mic_channel_id":    None,
        "language":             "en",
        "user_languages":       {},       # {str(user_id): lang}
        "max_length":           300,
        "slow_tts":             False,
        "idle_timeout":         600,      # seconds; 0 = disabled (10 min default for long meets)
        # Name reading — default OFF for car meets (less noise)
        "say_name":             False,
        "use_nickname":         True,
        "voice_prefix":         "says",
        # Filters
        "smart_filter":         True,
        "ignored_users":        [],
        "message_cooldown":     0,
        "same_vc_required":     True,
        "required_role_id":     None,
        # Voice joining
        "autojoin_any":         True,
        "auto_join_channel_id": None,
        # Host
        "host_id":              None,
        "host_mode":            False,
        "host_interrupts":      False,
        "follow_mode":          False,
    }


def get_guild_settings(guild_id: int) -> dict:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = default_settings()
    s = guild_settings[guild_id]
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


# ─── Permission check ─────────────────────────────────────────────────────────

def has_permission(interaction: discord.Interaction) -> bool:
    s = get_guild_settings(interaction.guild.id)
    role_id = s.get("required_role_id")
    if role_id is None:
        return True
    if interaction.user.guild_permissions.manage_guild:
        return True
    return any(r.id == role_id for r in interaction.user.roles)


# ─── Message cleaning ─────────────────────────────────────────────────────────

def clean_message(text: str) -> Optional[str]:
    """Normalize raw message text into clean, natural TTS speech."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"https?://\S+", "", text)           # remove links
    text = re.sub(r"www\.\S+", "", text)
    text = re.sub(r"<@!?\d+>", "someone", text)        # @mentions
    text = re.sub(r"<#\d+>", "a channel", text)
    text = re.sub(r"<@&\d+>", "a role", text)
    text = re.sub(r"<a?:\w+:\d+>", "", text)           # custom emoji markup
    text = re.sub(r"(.)\1{3,}", r"\1\1", text)         # heyyyy → hey
    text = re.sub(r"([!?.,-]){3,}", r"\1", text)       # !!! → !
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _is_mostly_emoji(text: str) -> bool:
    """Return True if the text contains no real alphabetic/numeric content."""
    # Strip whitespace and check for at least 2 alphanumeric chars
    alpha = re.sub(r"[^\w]", "", text, flags=re.UNICODE)
    return len(alpha) < 2


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

    # Attachment-only message (no text)
    if not message.content.strip() and message.attachments:
        return True

    # Role restriction
    role_id = s.get("required_role_id")
    if role_id is not None:
        has_role = any(r.id == role_id for r in message.author.roles)
        if not has_role and not message.author.guild_permissions.manage_guild:
            return True

    # Per-user cooldown
    cooldown = s.get("message_cooldown", 0)
    if cooldown > 0:
        key = (message.guild.id, message.author.id)
        if (time.monotonic() - user_last_spoke.get(key, 0.0)) < cooldown:
            return True

    if s["smart_filter"]:
        content = message.content.strip()
        if not content:
            return True

        lowered = content.lower()

        # Spam word list
        spam_words = {"lol", "lmao", "ok", "k", "w", "?", "??", "😂", "😭",
                      "fr", "ngl", "gg", "ez", "bruh", "💀", "🔥", "👀"}
        if lowered in spam_words:
            return True

        # Links
        if any(p in lowered for p in ("http://", "https://", "www.")):
            return True

        # Emoji-only or no real text
        if _is_mostly_emoji(content):
            return True

        # Duplicate — same content as user's last message
        key = (message.guild.id, message.author.id)
        if lowered == user_last_content.get(key, ""):
            return True

    return False


async def in_same_vc(message: discord.Message, s: dict) -> bool:
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
    Long-running per-guild task.
    Pulls TTSItems from the guild queue and plays them one at a time.
    Uses audio cache to skip regeneration for repeated phrases.
    Handles pause, interrupt, disconnect, and retry gracefully.
    """
    q           = get_queue(guild.id)
    pause_event = get_pause_event(guild.id)
    loop        = asyncio.get_event_loop()

    print(f"[Worker] Started for {guild.name}")

    while True:
        # Wait for next item
        try:
            item = await q.get()
        except asyncio.CancelledError:
            print(f"[Worker] Cancelled for {guild.name}")
            break

        try:
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                print(f"[Worker] Skipping item — bot not connected in {guild.name}")
                continue

            # Respect pause state (but let interrupt items through)
            if not item.interrupt:
                await pause_event.wait()

            # Interrupt: stop current audio immediately
            if item.interrupt and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                print(f"[Worker] Interrupted playback for host message in {guild.name}")
                await asyncio.sleep(0.15)

            # Wait for any ongoing audio to finish
            while vc.is_playing() or vc.is_paused():
                await asyncio.sleep(0.2)

            # Re-check connection after waiting
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                continue

            # Clean and truncate text
            cleaned = clean_message(item.text)
            if not cleaned:
                continue
            if len(cleaned) > item.max_length:
                cleaned = cleaned[:item.max_length] + "..."

            print(f"[Playback] Starting in {guild.name}: {cleaned[:60]}...")

            # Try to play — up to 3 attempts, with audio cache
            success = False
            for attempt in range(3):
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        mp3 = Path(tmp) / "tts.mp3"

                        # Check cache first to avoid redundant gTTS calls
                        cached = cache_get(cleaned, item.lang, item.slow)
                        if cached:
                            mp3.write_bytes(cached)
                        else:
                            # Generate TTS in executor so the event loop stays free
                            def _generate():
                                gTTS(text=cleaned, lang=item.lang, slow=item.slow).save(str(mp3))
                            await loop.run_in_executor(None, _generate)
                            cache_put(cleaned, item.lang, item.slow, mp3.read_bytes())

                        done = asyncio.Event()

                        def _after(err):
                            if err:
                                print(f"[Playback] Error after play: {err}")
                            loop.call_soon_threadsafe(done.set)

                        source = discord.FFmpegPCMAudio(str(mp3))
                        vc.play(source, after=_after)

                        touch_activity(guild.id)
                        print(f"[Playback] Playing in {guild.name}")

                        # Wait inside temp dir so mp3 file stays alive
                        await done.wait()

                    print(f"[Playback] Finished in {guild.name}")
                    touch_activity(guild.id)
                    success = True
                    break

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[Worker] Attempt {attempt + 1}/3 failed in {guild.name}: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1.5)

            if not success:
                print(f"[Worker] Gave up after 3 attempts in {guild.name}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Worker] Unexpected error in {guild.name}: {e}")


def ensure_worker(guild: discord.Guild):
    """Start the guild worker if it is not running. Called on every enqueue."""
    gid  = guild.id
    task = guild_workers.get(gid)
    if task is None or task.done():
        if task is not None and task.done():
            exc = task.exception() if not task.cancelled() else None
            if exc:
                print(f"[Worker] Died in {guild.name}: {exc}")
        guild_workers[gid] = asyncio.create_task(tts_worker(guild))
        print(f"[Worker] (Re)started for {guild.name}")


async def enqueue(
    guild:      discord.Guild,
    text:       str,
    lang:       str,
    slow:       bool,
    max_length: int,
    priority:   int  = 1,
    interrupt:  bool = False,
):
    """Add a TTS item to the guild queue and ensure the worker is running."""
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
    await get_queue(guild.id).put(item)
    touch_activity(guild.id)
    print(f"[Queue] Added to {guild.name} (priority={priority}): {text[:50]}...")


# ─── Voice helpers ────────────────────────────────────────────────────────────

async def safe_join(channel: discord.VoiceChannel, guild: discord.Guild) -> bool:
    """
    Connect to or move into a voice channel safely.
    Per-guild debounce prevents reconnect spam.
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
            print(f"[Voice] Moved to {channel.name} in {guild.name}")
        else:
            await channel.connect(timeout=10.0, reconnect=True)
            print(f"[Voice] Joined {channel.name} in {guild.name}")
        touch_activity(gid)
        return True
    except Exception as e:
        print(f"[Voice] Failed to join {channel.name} in {guild.name}: {e}")
        return False
    finally:
        guild_joining.discard(gid)


async def delayed_auto_leave(guild: discord.Guild, vacated_channel: discord.VoiceChannel):
    """
    FIX: Wait a short time before leaving so voice-state updates can settle,
    then re-verify the channel is actually empty before disconnecting.
    Also aborts if the bot has already moved away or is in follow mode.
    """
    await asyncio.sleep(2.5)  # settle delay

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return  # already disconnected

    # Abort if bot moved to a different channel
    if vc.channel.id != vacated_channel.id:
        return

    # Abort if bot is in the process of moving (follow mode join)
    if guild.id in guild_moving:
        return

    # Re-check: are there still non-bot users in the channel?
    non_bots = [m for m in vc.channel.members if not m.bot]
    if non_bots:
        return  # someone is still here — stay

    try:
        await vc.disconnect()
        guild_last_activity.pop(guild.id, None)
        print(f"[AutoLeave] Left {guild.name} — channel truly empty")
    except Exception as e:
        print(f"[AutoLeave] Error in {guild.name}: {e}")


# ─── Idle timeout ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def idle_check():
    """
    FIX: Only disconnect when TRULY idle:
    - queue is empty
    - bot is not playing or paused
    - no recent activity within idle_timeout seconds
    """
    now = time.monotonic()
    for guild in bot.guilds:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            continue

        s       = get_guild_settings(guild.id)
        timeout = s.get("idle_timeout", 600)
        if timeout <= 0:
            continue

        q = get_queue(guild.id)

        # Skip if there is work in progress
        if not q.empty():
            continue
        if vc.is_playing() or vc.is_paused():
            continue

        last = guild_last_activity.get(guild.id, now)
        if (now - last) >= timeout:
            try:
                await vc.disconnect()
                guild_last_activity.pop(guild.id, None)
                print(f"[Idle] Left {guild.name} after {timeout}s of inactivity")
            except Exception as e:
                print(f"[Idle] Disconnect error in {guild.name}: {e}")


@tasks.loop(minutes=2)
async def worker_health_check():
    """Periodically restart any dead guild workers that still have queued items."""
    for guild in bot.guilds:
        gid  = guild.id
        task = guild_workers.get(gid)
        q    = get_queue(gid)
        if task is not None and task.done() and not q.empty():
            print(f"[Health] Worker for {guild.name} is dead but queue has items — restarting")
            ensure_worker(guild)


# ─── Rotating status ──────────────────────────────────────────────────────────

_STATUS_INDEX = 0
_STATUS_MESSAGES = [
    (discord.ActivityType.listening, "your messages 🎙️"),
    (discord.ActivityType.playing,   "/panel for commands"),
    (discord.ActivityType.watching,  "the meet 🚗"),
    (discord.ActivityType.listening, "#no-mic channel"),
]

@tasks.loop(seconds=30)
async def rotate_status():
    """Cycle through status messages every 30 seconds."""
    global _STATUS_INDEX
    kind, text = _STATUS_MESSAGES[_STATUS_INDEX % len(_STATUS_MESSAGES)]
    _STATUS_INDEX += 1

    # Show live queue total across all guilds as a bonus
    total_queued = sum(get_queue(g.id).size() for g in bot.guilds)
    if total_queued > 0:
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{total_queued} message{'s' if total_queued != 1 else ''} in queue"
        )
    else:
        activity = discord.Activity(type=kind, name=text)

    try:
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception:
        pass


# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user} (ID: {bot.user.id})")

    # Clear global slash commands from Discord to prevent duplicates.
    # Uses the API directly so the local command tree remains intact.
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        print("[Sync] Cleared global commands from Discord.")
    except Exception as e:
        print(f"[Sync] Could not clear global commands: {e}")

    # Sync guild-specific commands (propagates instantly, no hour-long wait)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[Sync] {len(synced)} command(s) → {guild.name}")
        except Exception as e:
            print(f"[Sync] Failed for {guild.name}: {e}")

    idle_check.start()
    worker_health_check.start()
    rotate_status.start()

    # Set initial presence immediately so the bot shows online right away
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.listening, name="your messages 🎙️")
    )


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        await bot.process_commands(message)
        return

    s = get_guild_settings(message.guild.id)

    if not should_skip(message, s):
        if await in_same_vc(message, s):
            # Record cooldown and dedup timestamps
            key = (message.guild.id, message.author.id)
            if s.get("message_cooldown", 0) > 0:
                user_last_spoke[key] = time.monotonic()
            if s["smart_filter"]:
                user_last_content[key] = message.content.strip().lower()

            # Display name
            display = (message.author.display_name
                       if s.get("use_nickname", True)
                       else message.author.name)

            # Per-user language override
            uid  = str(message.author.id)
            lang = s.get("user_languages", {}).get(uid) or s.get("language", "en")

            # Build text
            if s["say_name"]:
                prefix    = s.get("voice_prefix", "says")
                full_text = f"{display} {prefix} {message.content}"
            else:
                full_text = message.content

            # Host priority
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
    after:  discord.VoiceState,
):
    if member.bot:
        return

    guild = member.guild
    vc    = guild.voice_client
    s     = get_guild_settings(guild.id)

    # ── Follow mode: bot follows the designated host ──────────────────────────
    host_id     = s.get("host_id")
    follow_mode = s.get("follow_mode", False)
    if follow_mode and host_id and member.id == host_id and after.channel is not None:
        if before.channel is None or before.channel.id != after.channel.id:
            guild_moving.add(guild.id)
            try:
                await safe_join(after.channel, guild)
                print(f"[Follow] Followed host to {after.channel.name} in {guild.name}")
            finally:
                guild_moving.discard(guild.id)
            return

    # ── Auto-join when a user enters a voice channel ──────────────────────────
    if after.channel is not None and (before.channel is None or before.channel.id != after.channel.id):
        if vc is None or not vc.is_connected():
            auto_id  = s.get("auto_join_channel_id")
            join_any = s.get("autojoin_any", True)
            if auto_id:
                if after.channel.id == auto_id:
                    await safe_join(after.channel, guild)
            elif join_any:
                await safe_join(after.channel, guild)

    # ── FIX: Delayed auto-leave with settle period and re-check ──────────────
    # Schedule a check rather than disconnecting immediately.
    # This avoids leaving when users are switching channels quickly.
    if (before.channel is not None
            and vc and vc.channel
            and vc.channel.id == before.channel.id):
        asyncio.create_task(delayed_auto_leave(guild, before.channel))


# ─── Slash commands ───────────────────────────────────────────────────────────

# ── Voice ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="join", description="Join your current voice channel")
async def cmd_join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)
        return
    await interaction.response.defer()
    ok = await safe_join(interaction.user.voice.channel, interaction.guild)
    ch = interaction.user.voice.channel.name
    await interaction.followup.send(f"Joined **{ch}**." if ok else "Could not join — try again.")


@bot.tree.command(name="leave", description="Leave the current voice channel")
async def cmd_leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    await vc.disconnect()
    guild_last_activity.pop(interaction.guild.id, None)
    print(f"[Voice] Manually left {interaction.guild.name}")
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
    vc    = interaction.guild.voice_client
    event = get_pause_event(interaction.guild.id)
    if vc and vc.is_playing():
        vc.pause()
    event.clear()
    await interaction.response.send_message("TTS paused. Use `/resume` to continue.")


@bot.tree.command(name="resume", description="Resume paused TTS playback")
async def cmd_resume(interaction: discord.Interaction):
    vc    = interaction.guild.voice_client
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
    count = get_queue(interaction.guild.id).clear()
    print(f"[Queue] Cleared {count} item(s) in {interaction.guild.name}")
    await interaction.response.send_message(f"Queue cleared. ({count} message(s) removed)")


@bot.tree.command(name="queue", description="Show how many messages are waiting to be read")
async def cmd_queue(interaction: discord.Interaction):
    count = get_queue(interaction.guild.id).size()
    if count == 0:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**{count}** message(s) in the queue.", ephemeral=True)


@bot.tree.command(name="queueview", description="Preview the next messages waiting in the queue")
async def cmd_queueview(interaction: discord.Interaction):
    q     = get_queue(interaction.guild.id)
    items = q.peek(5)
    if not items:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines = []
    for i, item in enumerate(items, 1):
        tag     = "⭐ " if item.priority == 0 else ""
        preview = item.text[:70] + "…" if len(item.text) > 70 else item.text
        lines.append(f"`{i}.` {tag}{preview}")
    total = q.size()
    body  = "\n".join(lines)
    if total > 5:
        body += f"\n*… and {total - 5} more*"
    await interaction.response.send_message(f"**Next in queue:**\n{body}", ephemeral=True)


@bot.tree.command(name="removefromqueue", description="Remove a message from the queue by its position number")
async def cmd_removefromqueue(interaction: discord.Interaction, position: int):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    q    = get_queue(interaction.guild.id)
    item = q.remove(position - 1)  # convert to 0-based
    if item is None:
        await interaction.response.send_message(
            f"No item at position {position}. Use `/queueview` to see the queue.", ephemeral=True
        )
        return
    preview = item.text[:70] + "…" if len(item.text) > 70 else item.text
    await interaction.response.send_message(f"Removed item {position}: *{preview}*", ephemeral=True)


@bot.tree.command(name="testtts", description="Play a test TTS message to verify audio is working")
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
    await interaction.response.send_message("Using server **nicknames**.")


@bot.tree.command(name="nick_off", description="Use account username instead of nickname")
async def cmd_nick_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["use_nickname"] = False; save_settings()
    await interaction.response.send_message("Using **usernames** (not nicknames).")


@bot.tree.command(name="samevc_on", description="Only read messages from users in the bot's VC")
async def cmd_samevc_on(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["same_vc_required"] = True; save_settings()
    await interaction.response.send_message("Same VC requirement **ON**.")


@bot.tree.command(name="samevc_off", description="Read messages from users in any VC")
async def cmd_samevc_off(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["same_vc_required"] = False; save_settings()
    await interaction.response.send_message("Same VC requirement **OFF**.")


@bot.tree.command(name="smartfilter_on", description="Filter spam, links, emoji-only, and duplicate messages")
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
    await interaction.response.send_message("TTS speed: **slow**.")


@bot.tree.command(name="speed_normal", description="Switch TTS back to normal speed")
async def cmd_speed_normal(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["slow_tts"] = False; save_settings()
    await interaction.response.send_message("TTS speed: **normal**.")


# ── Settings ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="setnomic", description="Set the text channel the bot reads aloud")
async def cmd_setnomic(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["no_mic_channel_id"] = channel.id; save_settings()
    await interaction.response.send_message(f"Reading messages from {channel.mention}.")


@bot.tree.command(name="setlang", description="Set the server-wide TTS language (en, es, fr, de, ja…)")
async def cmd_setlang(interaction: discord.Interaction, language: str):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["language"] = language.strip().lower(); save_settings()
    await interaction.response.send_message(f"Server language set to `{language}`.")


@bot.tree.command(name="setmylang", description="Set your personal TTS language (overrides server default)")
async def cmd_setmylang(interaction: discord.Interaction, language: str):
    s = get_guild_settings(interaction.guild.id)
    s.setdefault("user_languages", {})[str(interaction.user.id)] = language.strip().lower()
    save_settings()
    await interaction.response.send_message(f"Your TTS language: `{language}`.", ephemeral=True)


@bot.tree.command(name="clearmylang", description="Remove your personal language, use server default")
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
    await interaction.response.send_message(f"Max message length: **{characters}** characters.")


@bot.tree.command(name="setcooldown", description="Seconds a user must wait between TTS messages (0 = off)")
async def cmd_setcooldown(interaction: discord.Interaction, seconds: int):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    if not 0 <= seconds <= 60:
        await interaction.response.send_message("Must be between 0 and 60.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["message_cooldown"] = seconds; save_settings()
    await interaction.response.send_message(
        "Cooldown **disabled**." if seconds == 0 else f"Cooldown: **{seconds}s** per user."
    )


@bot.tree.command(name="setidletimeout", description="Seconds of inactivity before bot leaves VC (0 = disabled)")
async def cmd_setidletimeout(interaction: discord.Interaction, seconds: int):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    if not 0 <= seconds <= 7200:
        await interaction.response.send_message("Must be between 0 and 7200 (2 hours).", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["idle_timeout"] = seconds; save_settings()
    if seconds == 0:
        await interaction.response.send_message("Idle timeout **disabled** — bot will stay until manually removed.")
    else:
        await interaction.response.send_message(f"Idle timeout set to **{seconds}s**.")


@bot.tree.command(name="disableidletimeout", description="Disable idle timeout — bot stays in VC until manually removed")
async def cmd_disableidletimeout(interaction: discord.Interaction):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["idle_timeout"] = 0; save_settings()
    await interaction.response.send_message("Idle timeout **disabled**. Bot will stay until you use `/leave`.")


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
        await interaction.response.send_message("Requires Manage Server permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["required_role_id"] = role.id; save_settings()
    await interaction.response.send_message(f"Commands restricted to **{role.name}**.")


@bot.tree.command(name="clearrole", description="Remove the role restriction from TTS commands")
async def cmd_clearrole(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Requires Manage Server permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    s["required_role_id"] = None; save_settings()
    await interaction.response.send_message("Role restriction removed.")


@bot.tree.command(name="setautojoin", description="Pin auto-join to a specific voice channel")
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
    await interaction.response.send_message(f"**{user.display_name}** is now ignored.")


@bot.tree.command(name="unignore", description="Resume reading a user's messages aloud")
async def cmd_unignore(interaction: discord.Interaction, user: discord.Member):
    if not has_permission(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    s = get_guild_settings(interaction.guild.id)
    if user.id in s["ignored_users"]:
        s["ignored_users"].remove(user.id); save_settings()
    await interaction.response.send_message(f"**{user.display_name}** will be read aloud again.")


# ── Info ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="tts_status", description="Show live TTS status and all current settings")
async def cmd_status(interaction: discord.Interaction):
    s   = get_guild_settings(interaction.guild.id)
    gid = interaction.guild.id
    vc  = interaction.guild.voice_client

    def ch(cid):  return f"<#{cid}>" if cid else "Not set"
    def rl(rid):  return f"<@&{rid}>" if rid else "None (everyone)"
    def usr(uid): return f"<@{uid}>" if uid else "None"
    def oo(val):  return "✅ ON" if val else "❌ OFF"

    # Live voice state
    connected   = vc is not None and vc.is_connected()
    vc_name     = vc.channel.name if connected else "Not connected"
    playing     = vc.is_playing() if connected else False
    paused      = not get_pause_event(gid).is_set()
    q           = get_queue(gid)
    q_size      = q.size()
    worker_ok   = gid in guild_workers and not guild_workers[gid].done()
    timeout     = s.get("idle_timeout", 600)
    timeout_str = f"{timeout}s" if timeout > 0 else "Disabled"
    ignored     = ", ".join(f"<@{u}>" for u in s["ignored_users"]) or "None"
    prefix      = s.get("voice_prefix", "says")

    embed = discord.Embed(title="📊 TTS Bot — Live Status", color=discord.Color.blurple())

    embed.add_field(name="🔊 Voice", value=(
        f"**Connected:** {'Yes' if connected else 'No'}\n"
        f"**Channel:** {vc_name}\n"
        f"**Playing:** {'Yes' if playing else 'No'}\n"
        f"**Paused:** {'Yes' if paused else 'No'}\n"
        f"**Queue size:** {q_size}\n"
        f"**Worker alive:** {'Yes' if worker_ok else 'No'}"
    ), inline=True)

    embed.add_field(name="⚙️ Core", value=(
        f"**TTS:** {oo(s['tts_enabled'])}\n"
        f"**No-mic channel:** {ch(s['no_mic_channel_id'])}\n"
        f"**Language:** `{s.get('language', 'en')}`\n"
        f"**Speed:** {'Slow' if s.get('slow_tts') else 'Normal'}\n"
        f"**Max length:** {s.get('max_length', 300)} chars\n"
        f"**Idle timeout:** {timeout_str}"
    ), inline=True)

    embed.add_field(name="👤 Name", value=(
        f"**Say name:** {oo(s['say_name'])}\n"
        f"**Use nickname:** {oo(s.get('use_nickname', True))}\n"
        f"**Voice prefix:** `{prefix}`\n"
        f"**Cooldown:** {s.get('message_cooldown', 0)}s"
    ), inline=True)

    embed.add_field(name="📡 Joining", value=(
        f"**Auto-join any:** {oo(s.get('autojoin_any', True))}\n"
        f"**Auto-join channel:** {ch(s.get('auto_join_channel_id'))}\n"
        f"**Same VC required:** {oo(s['same_vc_required'])}\n"
        f"**Follow mode:** {oo(s.get('follow_mode', False))}"
    ), inline=True)

    embed.add_field(name="⭐ Host", value=(
        f"**Host:** {usr(s.get('host_id'))}\n"
        f"**Host mode:** {oo(s.get('host_mode', False))}\n"
        f"**Host interrupts:** {oo(s.get('host_interrupts', False))}"
    ), inline=True)

    embed.add_field(name="🔒 Filters", value=(
        f"**Smart filter:** {oo(s['smart_filter'])}\n"
        f"**Required role:** {rl(s.get('required_role_id'))}\n"
        f"**Ignored:** {ignored}"
    ), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="panel", description="Show all TTS bot commands")
async def cmd_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="TTS Bot — Command Panel",
        description="Reads your text channel aloud in voice chat. Perfect for car meets.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="🔧 Setup", value=(
        "`/setnomic #channel` — Text channel to read\n"
        "`/setautojoin #vc` — Pin auto-join to one VC\n"
        "`/clearautojoin` — Auto-join any channel\n"
        "`/autojoin_any on|off` — Toggle auto-join\n"
        "`/setrole @role` / `/clearrole`\n"
        "`/setidletimeout <s>` / `/disableidletimeout`"
    ), inline=False)
    embed.add_field(name="🔊 Voice & Queue", value=(
        "`/join` / `/leave`\n"
        "`/skip` — Skip current message\n"
        "`/pause` / `/resume`\n"
        "`/queue` — Queue size\n"
        "`/queueview` — Preview next messages\n"
        "`/removefromqueue <n>` — Remove by position\n"
        "`/clearqueue` — Clear all\n"
        "`/testtts [text]` — Test audio"
    ), inline=False)
    embed.add_field(name="🗣️ TTS", value=(
        "`/tts_on` / `/tts_off`\n"
        "`/setlang <code>` — Server language\n"
        "`/setmylang <code>` / `/clearmylang`\n"
        "`/setmaxlength <n>`\n"
        "`/setcooldown <s>`\n"
        "`/speed_slow` / `/speed_normal`"
    ), inline=False)
    embed.add_field(name="👤 Name & Users", value=(
        "`/sayname_on` / `/sayname_off`\n"
        "`/nick_on` / `/nick_off`\n"
        "`/setvoiceprefix <word>`\n"
        "`/ignore @user` / `/unignore @user`"
    ), inline=False)
    embed.add_field(name="⭐ Host Mode", value=(
        "`/sethost @user` / `/clearhost`\n"
        "`/hostmode on|off`\n"
        "`/hostinterrupt on|off`\n"
        "`/followmode on|off`"
    ), inline=False)
    embed.add_field(name="⚙️ Filters & Info", value=(
        "`/samevc_on` / `/samevc_off`\n"
        "`/smartfilter_on` / `/smartfilter_off`\n"
        "`/tts_status` — Live status & all settings"
    ), inline=False)
    embed.set_footer(text="Settings persist after restarts. Bot auto-leaves when VC is empty.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
