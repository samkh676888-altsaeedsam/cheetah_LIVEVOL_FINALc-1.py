import asyncio
import re
import sqlite3
import time
from pathlib import Path

import yt_dlp
import numpy as np
from aiortc import MediaStreamTrack
from rubpy import Client
from rubpy.types import Update

try:
    from rubpy.bot.models import Keypad, KeypadRow, Button
    from rubpy.bot.enums import ButtonTypeEnum
except Exception:
    Keypad = KeypadRow = Button = ButtonTypeEnum = None

OWNER_GUID = "u0KYqq20165c697a3bc78ec75ea21b4f"

BASE = Path.home() / "CheetahMusic"
DOWNLOADS = BASE / "downloads"
DB = BASE / "cheetah.db"
PANEL_IMAGE = BASE / "panel.jpg"

DOWNLOADS.mkdir(parents=True, exist_ok=True)

VOLUME_PERCENT = 50
AUTO_DELETE_SECONDS = 0

db = sqlite3.connect(DB, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS group_owners (group_guid TEXT PRIMARY KEY, user_guid TEXT NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS music_admins (group_guid TEXT, user_guid TEXT, PRIMARY KEY(group_guid,user_guid))")
db.commit()

bot = Client(name="cheetah", display_welcome=True, lang_code="fa")

queues = {}
players = {}
workers = {}
locks = {}
current_items = {}
panel_messages = {}
paused_groups = set()
panel_locks = {}


def q(g):
    return queues.setdefault(g, [])


def lock(g):
    return locks.setdefault(g, asyncio.Lock())


def panel_lock(g):
    return panel_locks.setdefault(g, asyncio.Lock())


def owner(g):
    r = db.execute("SELECT user_guid FROM group_owners WHERE group_guid=?", (g,)).fetchone()
    return r[0] if r else None


def is_admin(g, u):
    if not u:
        return False
    if u == OWNER_GUID:
        return True
    if u == owner(g):
        return True
    return db.execute(
        "SELECT 1 FROM music_admins WHERE group_guid=? AND user_guid=?",
        (g, u)
    ).fetchone() is not None


def add_admin(g, u):
    db.execute("INSERT OR IGNORE INTO music_admins VALUES (?,?)", (g, u))
    db.commit()


def del_admin(g, u):
    db.execute("DELETE FROM music_admins WHERE group_guid=? AND user_guid=?", (g, u))
    db.commit()


def text(u):
    return (getattr(u, "text", None) or "").strip()


def guid(u):
    return (
        getattr(u, "author_guid", None)
        or getattr(u, "author_id", None)
        or getattr(u, "user_guid", None)
    )


def chat(u):
    return (
        getattr(u, "object_guid", None)
        or getattr(u, "chat_guid", None)
    )


async def delete_later(message, seconds=AUTO_DELETE_SECONDS):
    if not message:
        return
    await asyncio.sleep(seconds)
    try:
        fn = getattr(message, "delete", None)
        if callable(fn):
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            return
    except Exception:
        pass
    try:
        inner = (getattr(message, "message", None) or
                 getattr(message, "raw_message", None) or
                 getattr(message, "original_message", None))
        fn = getattr(inner, "delete", None)
        if callable(fn):
            result = fn()
            if asyncio.iscoroutine(result):
                await result
    except Exception:
        pass


#def schedule_delete(message):
#    if message:
#        asyncio.create_task(delete_later(message))
#

async def say(u, msg):
    result = None
    try:
        result = await u.reply(msg)
    except Exception:
        g = chat(u)
        if g:
            try:
                result = await bot.send_message(g, msg)
            except Exception:
                result = None
#    schedule_delete(result)
    return result


async def replied(u):
    try:
        for name in ("get_reply_message", "get_replied_message", "reply_message"):
            fn=getattr(u,name,None)
            if callable(fn):
                r=fn()
                if asyncio.iscoroutine(r):
                    r=await r
                if r:
                    return r
            elif fn:
                return fn

        for name in ("reply_message","reply","reply_to_message"):
            try:
                r=getattr(u,name,None)
                if r:
                    if asyncio.iscoroutine(r):
                        r=await r
                    if r:
                        return r
            except Exception:
                pass
    except Exception:
        pass
    return None


async def download_item(item):
    if item is None:
        return None

    if isinstance(item, Path) and item.exists():
        return item

    for name in ("download", "download_file", "download_media", "save"):
        fn=getattr(item,name,None)
        if not callable(fn):
            continue
        for arg in (str(DOWNLOADS), DOWNLOADS):
            try:
                result=fn(arg)
                if asyncio.iscoroutine(result):
                    result=await result
                if result:
                    path=Path(str(result))
                    if path.exists() and path.is_file():
                        return path
            except Exception:
                pass

    for name in ("file_inline","music","voice","video","file"):
        try:
            obj=getattr(item,name,None)
            if obj and obj is not item:
                result=await download_item(obj)
                if result:
                    return result
        except Exception:
            pass

    return None


def media_from_update(u):
    if not u:
        return None

    names=("file_inline","music","voice","video","file")

    for name in names:
        try:
            value=getattr(u,name,None)
            if value:
                return value
        except Exception:
            pass

    for container_name in ("message","raw_message","original_message","data"):
        try:
            obj=getattr(u,container_name,None)
            if obj and obj is not u:
                for name in names:
                    try:
                        value=getattr(obj,name,None)
                        if value:
                            return value
                    except Exception:
                        pass
        except Exception:
            pass

    return None



async def youtube(query):
    loop = asyncio.get_running_loop()

    def work():
        out = str(DOWNLOADS / "%(id)s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": out,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "geo_bypass": True,
            "cookiefile": "/data/data/com.termux/files/home/storage/downloads/cookies.txt",
            "retries": 10,
            "fragment_retries": 10,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }

        target = query if re.match(r"^https?://", query) else "ytsearch1:" + query

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True)
            if "entries" in info:
                info = info["entries"][0]

            title = info.get("title") or "آهنگ"
            artist = info.get("artist") or info.get("uploader") or "نامشخص"
            requested = Path(ydl.prepare_filename(info))
            mp3 = requested.with_suffix(".mp3")

            if mp3.exists():
                path = mp3
            elif requested.exists():
                path = requested
            else:
                files = sorted(
                    DOWNLOADS.glob(f"{info.get('id','')}.*"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )
                path = files[0] if files else None

            return title, artist, path

    return await loop.run_in_executor(None, work)


async def media_duration(path):
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await process.communicate()
        return float(out.decode().strip())
    except Exception:
        return 0.0


def _scale_frame(frame, factor):
    arr = frame.to_ndarray()
    dtype = arr.dtype
    scaled = arr.astype(np.float32) * factor
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        scaled = np.clip(scaled, info.min, info.max)
    new_arr = scaled.astype(dtype)
    new_frame = frame.from_ndarray(new_arr, format=frame.format.name, layout=frame.layout.name)
    new_frame.sample_rate = frame.sample_rate
    new_frame.pts = frame.pts
    new_frame.time_base = frame.time_base
    return new_frame


class LiveVolumeTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, original_track, group_guid):
        super().__init__()
        self.original_track = original_track
        self.group_guid = group_guid

    def pause(self):
        if hasattr(self.original_track, "pause"):
            self.original_track.pause()

    def resume(self):
        if hasattr(self.original_track, "resume"):
            self.original_track.resume()

    async def recv(self):
        frame = await self.original_track.recv()
        factor = max(50, min(200, VOLUME_PERCENT)) / 100.0
        if factor == 1.0:
            return frame
        try:
            return _scale_frame(frame, factor)
        except Exception:
            return frame


def _attach_live_volume(connection, g):
    wrapped = LiveVolumeTrack(connection.track, g)
    sender = next(
        (s for s in connection.pc.getSenders() if s.track and s.track.kind == "audio"),
        None
    )
    if sender:
        sender.replaceTrack(wrapped)
        connection.track = wrapped
    return connection


async def play(g, path):
    connection = await bot.voice_chat_player(g, Path(path))
    _attach_live_volume(connection, g)
    players[g] = connection
    return connection


async def stop(g):
    p = players.get(g)
    if p:
        try:
            result = p.stop()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

        try:
            await bot.leave_group_voice_chat(p.chat_guid, p.voice_chat_id)
        except Exception:
            pass

        for task_name in ("_speaking_task", "_heartbeat_task"):
            task = getattr(bot, task_name, None)
            if task:
                try:
                    task.cancel()
                except Exception:
                    pass

    players.pop(g, None)
    paused_groups.discard(g)


def keypad_dict():
    rows = [
        [
            {"id": "pause", "type": "Simple", "button_text": "⏸ مکث"},
            {"id": "resume", "type": "Simple", "button_text": "▶️ ادامه"},
            {"id": "next", "type": "Simple", "button_text": "⏭ بعدی"},
        ],
        [
            {"id": "stop", "type": "Simple", "button_text": "⏹ توقف"},
            {"id": "queue", "type": "Simple", "button_text": "📋 صف"},
            {"id": "refresh", "type": "Simple", "button_text": "🔄 پنل"},
        ],
        [
            {"id": "vol_down", "type": "Simple", "button_text": "🔉 صدا -25"},
            {"id": "vol_up", "type": "Simple", "button_text": "🔊 صدا +25"},
        ],
    ]
    return {
        "rows": [{"buttons": row} for row in rows],
        "resize_keyboard": True,
        "on_time_keyboard": False,
    }


def panel_text(g, status=None):
    item = current_items.get(g)
    if item:
        title = item.get("title", "آهنگ")
        artist = item.get("artist", "نامشخص")
    else:
        title = "هنوز آهنگی پخش نشده"
        artist = "—"

    if status is None:
        status = "⏸ مکث شده" if g in paused_groups else (
            "▶️ در حال پخش" if g in players else "⏹ متوقف"
        )

    return (
        "🐆 چیتا موزیک\n\n"
        f"🎵 {title}\n"
        f"👤 {artist}\n"
        f"{status}\n"
        f"🔊 صدا: {VOLUME_PERCENT}%\n"
        f"📋 آهنگ‌های بعدی: {len(q(g))}"
    )


async def refresh_panel(g, status=None):
    async with panel_lock(g):
        old = panel_messages.get(g)
        if old:
            try:
                result = old.delete()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
            panel_messages.pop(g, None)

        try:
            metadata = {
                "chat_keypad_type": "New",
                "chat_keypad": keypad_dict(),
            }

            if PANEL_IMAGE.exists():
                msg = await bot.send_message(
                    g,
                    panel_text(g, status),
                    file_inline=str(PANEL_IMAGE),
                    type="Image",
                    metadata=metadata,
                )
            else:
                msg = await bot.send_message(
                    g,
                    panel_text(g, status),
                    metadata=metadata,
                )

            panel_messages[g] = msg
            return msg
        except Exception:
            return None


async def worker(g):
    if workers.get(g):
        return

    workers[g] = True

    try:
        async with lock(g):
            while q(g):
                item = q(g).pop(0)
                path = Path(item["path"])

                if not path.exists():
                    continue

                current_items[g] = item
                paused_groups.discard(g)
                await refresh_panel(g, "📥 آماده‌سازی پخش...")

                p = None
                try:
                    p = await play(g, path)
                    duration = await media_duration(path)
                    started = time.monotonic()
                    paused_at = None
                    paused_total = 0.0

                    await refresh_panel(g, "▶️ در حال پخش")

                    while players.get(g) is p:
                        await asyncio.sleep(1)

                        if g in paused_groups:
                            if paused_at is None:
                                paused_at = time.monotonic()
                            continue

                        if paused_at is not None:
                            paused_total += time.monotonic() - paused_at
                            paused_at = None

                        if duration > 0 and time.monotonic() - started - paused_total >= duration + 1.5:
                            break

                        try:
                            info = p.get_info()
                            if asyncio.iscoroutine(info):
                                info = await info
                            if isinstance(info, dict):
                                state = str(
                                    info.get("state") or info.get("status") or ""
                                ).lower()
                                if state in {
                                    "stopped", "finished", "ended",
                                    "idle", "completed"
                                }:
                                    break
                        except Exception:
                            pass

                except Exception as e:
                    await refresh_panel(g, f"❌ خطای پخش: {e}")

                finally:
                    if players.get(g) is p:
                        players.pop(g, None)
                    paused_groups.discard(g)
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass

                    if q(g):
                        await refresh_panel(g, "⏳ رفتن به آهنگ بعدی...")
                    else:
                        current_items.pop(g, None)
                        await refresh_panel(g, "⏹ پخش تمام شد")

    finally:
        workers.pop(g, None)


@bot.on_message_updates()
async def handler(u: Update):
    t = text(u)
    g = chat(u)
    user = guid(u)

    if not g or not user:
        return

    # اگر کلیک دکمه به‌صورت متن نیامده باشد، شناسه دکمه را می‌خوانیم.
    if not t:
        for key in ("button_id", "button_text", "id"):
            try:
                value = u.find_key(key)
                if value:
                    t = str(value).strip()
                    break
            except Exception:
                pass

    # دکمه‌های پنل
    button_map = {
        "⏸ مکث": "مکث",
        "▶️ ادامه": "ادامه",
        "⏭ بعدی": "بعدی",
        "⏹ توقف": "توقف",
        "📋 صف": "صف",
        "🔄 پنل": "پنل",
    }
    t = button_map.get(t, t)

    if t == "🔉 صدا -25":
        t = "صدای کم"
    elif t == "🔊 صدا +25":
        t = "صدای زیاد"

    if not t:
        return

    print(f"📩 {user} | {g} | {t}")
#    schedule_delete(u)

    if t in ("چیتا", "چیتا؟", "چیتا !"):
        await say(u, "جانم سعید")
        return

    if t == "راهنما":
        if not is_admin(g, user):
            await say(u, "⛔ دسترسی موزیک نداری.")
            return

        await say(
            u,
            "🐆 دستورات چیتا\n\n"
            "🎵 پخش نام آهنگ\n"
            "🔗 پخش لینک\n"
            "📁 روی فایل ریپلای + پخش\n"
            "⏸ مکث\n"
            "▶️ ادامه\n"
            "⏭ بعدی\n"
            "⏹ توقف\n"
            "📋 صف\n"
            "🔊 صدا 50 تا 200\n\n"
            "👑 مدیریت:\n"
            "روی پیام کاربر ریپلای + ترفیع موزیک\n"
            "روی پیام کاربر ریپلای + عزل موزیک"
        )
        return

    if t == "ترفیع موزیک":
        if user != OWNER_GUID and user != owner(g):
            return

        r = None
        _rid = getattr(u, "reply_message_id", None)
        if _rid:
            try:
                _res = await bot.get_messages_by_id(g, [_rid])
                _msgs = getattr(_res, "messages", None)
                r = _msgs[0] if _msgs else None
                print(f"reply msg: {r}")
            except Exception as e:
                print(f"get msg err: {e}")
        target = guid(r) if r else None
        if not target and r:
            for k in ("author_object_guid","author_guid","author_id","user_guid","sender_guid","from_guid"):
                target = getattr(r, k, None)
                if target: break

        if target:
            add_admin(g, target)
            await say(u, "✅ دسترسی موزیک فعال شد.")
        else:
            await say(u, "⛔ روی پیام کاربر ریپلای کن.")
        return

    if t == "عزل موزیک":
        if user != OWNER_GUID and user != owner(g):
            return

        r = None
        _rid = getattr(u, "reply_message_id", None)
        if _rid:
            try:
                _res = await bot.get_messages_by_id(g, [_rid])
                _msgs = getattr(_res, "messages", None)
                r = _msgs[0] if _msgs else None
                print(f"reply msg: {r}")
            except Exception as e:
                print(f"get msg err: {e}")
        target = guid(r) if r else None
        if not target and r:
            for k in ("author_object_guid","author_guid","author_id","user_guid","sender_guid","from_guid"):
                target = getattr(r, k, None)
                if target: break

        if target:
            del_admin(g, target)
            await say(u, "✅ دسترسی موزیک لغو شد.")
        else:
            await say(u, "⛔ روی پیام کاربر ریپلای کن.")
        return

    if t == "مالک موزیک":
        if user != OWNER_GUID:
            return

        r = None
        _rid = getattr(u, "reply_message_id", None)
        if _rid:
            try:
                _res = await bot.get_messages_by_id(g, [_rid])
                _msgs = getattr(_res, "messages", None)
                r = _msgs[0] if _msgs else None
                print(f"reply msg: {r}")
            except Exception as e:
                print(f"get msg err: {e}")
        target = guid(r) if r else None

        if target:
            db.execute(
                "INSERT OR REPLACE INTO group_owners VALUES (?,?)",
                (g, target)
            )
            db.commit()
            await say(u, "👑 مالک موزیک گروه تعیین شد.")
        else:
            await say(u, "⛔ روی پیام کاربر ریپلای کن.")
        return

    global VOLUME_PERCENT

    if t == "صدا":
        await say(u, f"🔊 صدای پخش: {VOLUME_PERCENT}%\n\nحداقل: 50% | حداکثر: 200%")
        await refresh_panel(g)
        return

    if t.startswith("صدا "):
        try:
            value = int(t.split(maxsplit=1)[1].strip())
        except ValueError:
            await say(u, "❌ مقدار صدا باید بین 50 تا 200 باشد.")
            return

        if value < 50 or value > 200:
            await say(u, "❌ محدوده صدا فقط 50 تا 200 درصد است.")
            return

        VOLUME_PERCENT = value
        await say(u, f"🔊 صدای پخش روی {VOLUME_PERCENT}% تنظیم شد.")

        await refresh_panel(g)
        return

    if t == "صدای کم":
        if not is_admin(g, user):
            return
        VOLUME_PERCENT = max(50, VOLUME_PERCENT - 25)
        await say(u, f"🔉 صدا روی {VOLUME_PERCENT}% تنظیم شد.")

        await refresh_panel(g)
        return

    if t == "صدای زیاد":
        if not is_admin(g, user):
            return
        VOLUME_PERCENT = min(200, VOLUME_PERCENT + 25)
        await say(u, f"🔊 صدا روی {VOLUME_PERCENT}% تنظیم شد.")

        await refresh_panel(g)
        return

    if t == "پنل":
        if not is_admin(g, user):
            return
        await refresh_panel(g)
        return

    if t == "صف":
        if not is_admin(g, user):
            await say(u, "⛔ دسترسی موزیک نداری.")
            return

        if not q(g):
            await say(u, "📋 صف خالی است.")
            return

        await say(
            u,
            "📋 صف پخش\n\n" +
            "\n".join(
                f"{i}. 🎵 {x['title']}"
                for i, x in enumerate(q(g), 1)
            )
        )
        return

    if t == "مکث":
        if not is_admin(g, user):
            return

        p = players.get(g)
        if not p:
            await say(u, "❌ چیزی در حال پخش نیست.")
            return

        try:
            result = p.pause()
            if asyncio.iscoroutine(result):
                await result
            paused_groups.add(g)
            await refresh_panel(g, "⏸ مکث شده")
            await say(u, "⏸ پخش مکث شد.")
        except Exception as e:
            await say(u, f"❌ {e}")
        return

    if t == "ادامه":
        if not is_admin(g, user):
            return

        p = players.get(g)
        if not p:
            await say(u, "❌ چیزی برای ادامه نیست.")
            return

        try:
            result = p.resume()
            if asyncio.iscoroutine(result):
                await result
            paused_groups.discard(g)
            await refresh_panel(g, "▶️ در حال پخش")
            await say(u, "▶️ پخش ادامه یافت.")
        except Exception as e:
            await say(u, f"❌ {e}")
        return

    if t == "توقف":
        if not is_admin(g, user):
            return

        q(g).clear()
        await stop(g)
        current_items.pop(g, None)
        await refresh_panel(g, "⏹ متوقف شد")
        await say(u, "⏹ پخش و صف متوقف شد.")
        return

    if t == "بعدی":
        if not is_admin(g, user):
            return

        await stop(g)

        if q(g):
            await refresh_panel(g, "⏭ آهنگ بعدی...")
        else:
            current_items.pop(g, None)
            await refresh_panel(g, "⏹ صف تمام شد")

        await say(u, "⏭ رفتن به آهنگ بعدی...")
        return

    if t == "پخش":
        if not is_admin(g, user):
            await say(u, "⛔ دسترسی موزیک نداری.")
            return

        r = None
        _rid = getattr(u, "reply_message_id", None)
        if _rid:
            try:
                _res = await bot.get_messages_by_id(g, [_rid])
                _msgs = getattr(_res, "messages", None)
                r = _msgs[0] if _msgs else None
                print(f"reply msg: {r}")
            except Exception as e:
                print(f"get msg err: {e}")

        if not r:
            await say(u, "📁 روی فایل آهنگ، ویس یا ویدئو ریپلای کن.")
            return

        await say(u, "📥 فایل دریافت شد؛ در حال آماده‌سازی...")

        import time as _t
        _fname = str(_t.time()).replace(".","_") + ".mp3"
        raw = await bot.download(r, str(DOWNLOADS / _fname))
        path = Path(str(DOWNLOADS / _fname))

        if not path:
            await say(u, "❌ دریافت فایل ریپلای‌شده انجام نشد.")
            return

        q(g).append({
            "title": path.stem,
            "artist": "فایل گروه",
            "path": str(path)
        })

        await say(u, f"✅ به صف اضافه شد:\n🎵 {path.stem}")
        await refresh_panel(g, "⏳ به صف اضافه شد")

        if not workers.get(g):
            asyncio.create_task(worker(g))

        return

    if t.startswith("پخش "):
        if not is_admin(g, user):
            await say(u, "⛔ دسترسی موزیک نداری.")
            return

        query = t[4:].strip()

        if not query:
            await say(u, "اسم آهنگ یا لینک را بنویس.")
            return

        await say(u, f"📥 درخواست دریافت شد.\n🔎 {query}\n\nدر حال دانلود...")

        try:
            title, artist, path = await youtube(query)
        except Exception as e:
            await say(u, f"❌ دانلود انجام نشد:\n{e}")
            return

        if not path or not Path(path).exists():
            await say(u, "❌ فایل دانلودشده پیدا نشد.")
            return

        q(g).append({
            "title": title,
            "artist": artist,
            "path": str(path)
        })

        await say(u, f"✅ به صف اضافه شد\n\n🎵 {title}\n👤 {artist}")
        await refresh_panel(g, "⏳ به صف اضافه شد")

        if not workers.get(g):
            asyncio.create_task(worker(g))

        return


print("🐆 Cheetah Music is starting...")
print("Rubpy 7.3.5")

bot.run()
