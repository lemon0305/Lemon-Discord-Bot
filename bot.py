import os  # 讀取 .env 載入後的環境變數，例如 Discord Token、頻道 ID。
import sqlite3  # 使用 SQLite 資料庫儲存每日計畫、完成狀態與 bot 狀態。
import sys  # 在設定錯誤或啟動失敗時結束程式。
from contextlib import closing  # 讓資料庫連線用完後自動關閉。
from datetime import datetime, timezone, timedelta  # 處理台灣時間、日期字串與提醒時間。
from pathlib import Path  # 用物件方式組合專案路徑，例如 data/plans.db。
from typing import Any  # 標註 LoL 公告資料這類混合型別 dict。

import discord  # discord.py 主套件，用來建立 Embed、讀取訊息與發送內容。
from discord.ext import commands, tasks  # commands 管理 !指令，tasks 建立背景循環任務。
from dotenv import load_dotenv  # 讀取 .env，避免把 Token 等秘密資料寫死在程式碼中。

from lol_patch import (  # 匯入 LoL 版本更新模組，讓主程式和公告解析邏輯分開。
    LOL_PATCH_ALL_CATEGORY,  # 代表「全部分類」的特殊分類名稱。
    LOL_PATCH_COLOR,  # LoL 更新公告 Embed 使用的主題顏色。
    LOL_PATCH_DEFAULT_CATEGORIES,  # 自動公告預設發布的分類。
    describe_lol_patch_categories,  # 把分類代號轉成使用者看得懂的中文描述。
    fetch_latest_lol_patch_note,  # 從官方網站抓取最新版本公告。
    fetch_lol_patch_note_by_version,  # 依照版本號抓取指定版本公告。
    fetch_lol_patch_note_list,  # 抓取最近幾個版本公告清單。
    is_lol_patch_version_query,  # 判斷使用者輸入是否像版本號，例如 26.11。
    parse_lol_patch_categories,  # 把使用者輸入的分類文字轉成程式內部分類集合。
    send_lol_patch_note,  # 將完整 LoL 版本公告整理後發送到 Discord。
    send_lol_patch_summary,  # 發送舊版本的簡要公告與官方連結。
)


TAIPEI_TZ = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
LOCK_FILE = BASE_DIR / ".bot.lock"
THEME_COLOR = 0xF7B267
SUCCESS_COLOR = 0x7BC67B
WARNING_COLOR = 0xF2C94C
ERROR_COLOR = 0xEB5757


def today_string() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def acquire_single_instance_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w", encoding="utf-8")

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        raise RuntimeError("機器人已經在執行中，請先關閉另一個 bot.py 再啟動。")

    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle


class PlanStore:
    def __init__(self, database_path: str) -> None:
        path = Path(database_path)
        self.database_path = path if path.is_absolute() else BASE_DIR / path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    plan_date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def add_plan(self, guild_id: int, user_id: int, content: str, plan_date: str) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO plans (guild_id, user_id, plan_date, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    plan_date,
                    content,
                    datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_plans(self, guild_id: int, user_id: int, plan_date: str) -> list[sqlite3.Row]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, content, is_done
                FROM plans
                WHERE guild_id = ? AND user_id = ? AND plan_date = ?
                ORDER BY id ASC
                """,
                (guild_id, user_id, plan_date),
            ).fetchall()
            return list(rows)

    def set_done(self, guild_id: int, user_id: int, plan_date: str, display_index: int) -> sqlite3.Row | None:
        return self._update_by_display_index(
            guild_id,
            user_id,
            plan_date,
            display_index,
            "UPDATE plans SET is_done = 1 WHERE id = ?",
        )

    def delete_plan(self, guild_id: int, user_id: int, plan_date: str, display_index: int) -> sqlite3.Row | None:
        return self._update_by_display_index(
            guild_id,
            user_id,
            plan_date,
            display_index,
            "DELETE FROM plans WHERE id = ?",
        )

    def _update_by_display_index(
        self,
        guild_id: int,
        user_id: int,
        plan_date: str,
        display_index: int,
        sql: str,
    ) -> sqlite3.Row | None:
        rows = self.list_plans(guild_id, user_id, plan_date)
        if display_index < 1 or display_index > len(rows):
            return None

        selected = rows[display_index - 1]
        with closing(self._connect()) as connection:
            connection.execute(sql, (selected["id"],))
            connection.commit()
        return selected

    def get_state(self, key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO bot_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            connection.commit()


def build_embed(title: str, description: str, color: int = THEME_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="每日計畫小助手 | 今天也慢慢把事情完成吧 (๑•̀ㅂ•́)و✧")
    return embed


def build_plan_embed(member: discord.abc.User, plan_date: str, rows: list[sqlite3.Row]) -> discord.Embed:
    if not rows:
        return build_embed(
            "今天的清單還是空的",
            f"{member.mention}，{plan_date} 還沒有任何計畫。\n用 `!add <內容>` 放進第一件小任務吧 (｡•̀ᴗ-)✧",
            WARNING_COLOR,
        )

    embed = build_embed(
        f"{plan_date} 每日計畫",
        f"{member.mention}，這是你的今日節奏表。慢慢來，但有在前進就很棒 (ง •̀_•́)ง",
    )
    for index, row in enumerate(rows, start=1):
        status = "✅ 完成" if row["is_done"] else "🌱 進行中"
        embed.add_field(
            name=f"{index}. {status}",
            value=row["content"],
            inline=False,
        )
    return embed


load_dotenv(BASE_DIR / ".env")

token = os.getenv("DISCORD_TOKEN")
prefix = os.getenv("COMMAND_PREFIX", "!")
database_path = os.getenv("DATABASE_PATH", "data/plans.db")
allowed_channel_id = os.getenv("ALLOWED_CHANNEL_ID", "").strip()
reminder_channel_id = os.getenv("REMINDER_CHANNEL_ID", "").strip()
reminder_time = os.getenv("REMINDER_TIME", "09:00").strip()
lol_patch_channel_id = os.getenv("LOL_PATCH_CHANNEL_ID", "").strip()
lol_patch_locale = os.getenv("LOL_PATCH_LOCALE", "zh-tw").strip() or "zh-tw"
lol_patch_check_minutes = os.getenv("LOL_PATCH_CHECK_MINUTES", "30").strip()
lol_patch_auto_post = os.getenv("LOL_PATCH_AUTO_POST", "true").strip().lower() not in {"0", "false", "no", "off"}
last_reminder_date: str | None = None
last_lol_patch_check_at: datetime | None = None
LOL_USER_COOLDOWN_SECONDS = 60
LOL_USER_QUERY_DELETE_SECONDS = 120
lol_user_cooldowns: dict[int, datetime] = {}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=prefix, intents=intents, help_command=None)
store = PlanStore(database_path)


def parse_channel_id(value: str, setting_name: str) -> int | None:
    if not value:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{setting_name} 請填 Discord 頻道 ID 數字") from exc


def is_allowed_command_channel(channel_id: int) -> bool:
    allowed_id = parse_channel_id(allowed_channel_id, "ALLOWED_CHANNEL_ID")
    return allowed_id is None or channel_id == allowed_id


def is_lol_patch_command_channel(channel_id: int) -> bool:
    lol_channel_id = parse_channel_id(lol_patch_channel_id, "LOL_PATCH_CHANNEL_ID")
    return lol_channel_id is not None and channel_id == lol_channel_id


def looks_like_lol_patch_command(content: str) -> bool:
    content = content.strip().lower()
    return content.startswith(f"{prefix}lolpatch") or content.startswith(f"{prefix}helpme")


def looks_like_prefixed_command(content: str) -> bool:
    return bool(prefix) and content.strip().startswith(prefix)


def is_admin_context(ctx: commands.Context) -> bool:
    return bool(ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator)


async def require_lol_admin(ctx: commands.Context) -> bool:
    if is_admin_context(ctx):
        return True

    await ctx.reply(
        embed=build_embed(
            "需要管理員權限",
            f"這個指令會發布公告到頻道，請使用 `{prefix}lolpatch list` 或 `{prefix}lolpatch <版本號>` 查找版本資訊。",
            WARNING_COLOR,
        ),
        mention_author=False,
        delete_after=LOL_USER_QUERY_DELETE_SECONDS,
    )
    return False


async def enforce_lol_user_cooldown(ctx: commands.Context) -> bool:
    if is_admin_context(ctx):
        return True

    now = datetime.now(TAIPEI_TZ)
    last_used_at = lol_user_cooldowns.get(ctx.author.id)
    if last_used_at:
        remaining = LOL_USER_COOLDOWN_SECONDS - int((now - last_used_at).total_seconds())
        if remaining > 0:
            await ctx.reply(
                embed=build_embed(
                    "指令冷卻中",
                    f"一般使用者每 {LOL_USER_COOLDOWN_SECONDS} 秒可查找一次 LoL 版本資訊，請再等 **{remaining} 秒**。",
                    WARNING_COLOR,
                ),
                mention_author=False,
                delete_after=LOL_USER_QUERY_DELETE_SECONDS,
            )
            return False

    lol_user_cooldowns[ctx.author.id] = now
    return True


def parse_reminder_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise RuntimeError("REMINDER_TIME 請使用 HH:MM 格式，例如 09:00") from exc

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise RuntimeError("REMINDER_TIME 請使用 00:00 到 23:59 之間的時間")

    return hour, minute


def parse_positive_int(value: str, setting_name: str, default: int) -> int:
    if not value:
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{setting_name} 請設定為正整數") from exc

    if parsed < 1:
        raise RuntimeError(f"{setting_name} 請設定為 1 以上的正整數")

    return parsed


async def send_daily_reminder() -> None:
    if not reminder_channel_id:
        return

    channel_id = parse_channel_id(reminder_channel_id, "REMINDER_CHANNEL_ID")
    if channel_id is None:
        return

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        raise RuntimeError("REMINDER_CHANNEL_ID 指定的頻道無法發送訊息")

    today = today_string()
    await channel.send(
        embed=build_embed(
            "早安，該整理今天的計畫囉",
            f"今天是 **{today}**。\n先用 `{prefix}today` 看看清單，或用 `{prefix}add <內容>` 放進新的小任務 (っ･ω･)っ",
            THEME_COLOR,
        )
    )


def build_lol_category_help_embed() -> discord.Embed:
    return build_embed(
        "LoL 分類指令",
        "\n".join(
            [
                f"`<{prefix}lolpatch champions|items|aram|arena 依分類發布公告>`",
                f"`{prefix}lolpatch <版本號>`  查找指定版本，例如 `{prefix}lolpatch 26.11`",
                f"`{prefix}lolpatch list [數量]`  查找近期版本清單",
            ]
        ),
        WARNING_COLOR,
    )


def build_lol_help_embed() -> discord.Embed:
    return build_embed(
        "LoL 更新公告指令",
        "\n".join(
            [
                f"`<{prefix}lolpatch champions|items|aram|arena 依分類發布公告>`",
                f"`{prefix}lolpatch <版本號>`  查找指定版本，例如 `{prefix}lolpatch 26.11`",
                f"`{prefix}lolpatch list [數量]`  查找近期版本清單",
                f"`{prefix}lolpatch` / `{prefix}lolpatch all`  發布預設/全部公告，限管理員",
                f"`{prefix}lolpatch check` / `{prefix}lolpatch force`  檢查或強制發布，限管理員",
                f"`{prefix}helpme`  只顯示 LoL 更新公告指令",
            ]
        ),
        THEME_COLOR,
    )


def build_general_help_embed() -> discord.Embed:
    return build_embed(
        "個人行程指令",
        "\n".join(
            [
                f"`{prefix}add <內容>`  新增今天的行程",
                f"`{prefix}today`  查看今天的行程",
                f"`{prefix}done <編號>`  標記行程完成",
                f"`{prefix}delete <編號>`  刪除行程",
                f"`{prefix}plan <YYYY-MM-DD>`  查看指定日期行程",
                f"`{prefix}reminder`  查看提醒設定",
                f"`{prefix}testreminder`  測試提醒訊息",
                "",
                "LoL 更新公告指令請在 LoL 版本更新頻道使用。",
            ]
        ),
        THEME_COLOR,
    )


def build_unknown_command_embed(is_lol_channel: bool) -> discord.Embed:
    if is_lol_channel:
        return build_embed(
            "未知的 LoL 指令",
            f"可用：`{prefix}helpme`、`{prefix}lolpatch <版本號>`、`{prefix}lolpatch list`、`{prefix}lolpatch champions|items|aram|arena`。",
            WARNING_COLOR,
        )

    return build_embed(
        "未知的指令",
        f"請輸入 `{prefix}helpme` 查看個人行程指令，例如 `{prefix}today`、`{prefix}add <內容>`。",
        WARNING_COLOR,
    )


def build_lol_check_force_usage_embed(command_name: str) -> discord.Embed:
    return build_embed(
        "LoL 指令用法錯誤",
        "\n".join(
            [
                f"`{prefix}lolpatch {command_name}`  由管理員檢查或發布 LoL 更新公告。",
                f"分類發布請使用 `{prefix}lolpatch champions`、`{prefix}lolpatch items`、`{prefix}lolpatch aram`、`{prefix}lolpatch arena`。",
            ]
        ),
        WARNING_COLOR,
    )


async def check_lol_patch_note(
    force: bool = False,
    categories: set[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    patch_note = await fetch_latest_lol_patch_note(lol_patch_locale)
    last_patch_id = store.get_state("last_lol_patch_note_id")
    should_post = force or patch_note["id"] != last_patch_id

    if should_post:
        channel_id = parse_channel_id(lol_patch_channel_id, "LOL_PATCH_CHANNEL_ID")
        if channel_id is None:
            raise RuntimeError("請先在 .env 設定 LOL_PATCH_CHANNEL_ID")

        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("LOL_PATCH_CHANNEL_ID 指定的頻道無法發送訊息")

        await send_lol_patch_note(channel, patch_note, categories or set(LOL_PATCH_DEFAULT_CATEGORIES))
        store.set_state("last_lol_patch_note_id", patch_note["id"] or "")

    store.set_state("last_lol_patch_checked_at", datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"))
    return patch_note, should_post


@tasks.loop(minutes=1)
async def daily_reminder_loop() -> None:
    global last_reminder_date

    if not reminder_channel_id:
        return

    now = datetime.now(TAIPEI_TZ)
    reminder_hour, reminder_minute = parse_reminder_time(reminder_time)
    if now.hour != reminder_hour or now.minute != reminder_minute:
        return

    current_date = now.strftime("%Y-%m-%d")
    if last_reminder_date == current_date:
        return

    await send_daily_reminder()
    last_reminder_date = current_date


@tasks.loop(minutes=1)
async def lol_patch_check_loop() -> None:
    global last_lol_patch_check_at

    if not lol_patch_auto_post or not lol_patch_channel_id:
        return

    now = datetime.now(TAIPEI_TZ)
    interval_minutes = parse_positive_int(lol_patch_check_minutes, "LOL_PATCH_CHECK_MINUTES", 30)
    if last_lol_patch_check_at and now - last_lol_patch_check_at < timedelta(minutes=interval_minutes):
        return

    last_lol_patch_check_at = now
    try:
        patch_note, posted = await check_lol_patch_note()
        if posted:
            print(f"Posted LoL patch note: {patch_note['title']}")
    except Exception as exc:
        print(f"LoL patch check failed: {exc}", file=sys.stderr)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")
    if allowed_channel_id:
        print(f"Commands are limited to channel ID: {allowed_channel_id}")
    else:
        print("Commands are allowed in every channel.")

    if reminder_channel_id and not daily_reminder_loop.is_running():
        daily_reminder_loop.start()

    if lol_patch_auto_post and lol_patch_channel_id and not lol_patch_check_loop.is_running():
        lol_patch_check_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    is_allowed_lol_channel = is_lol_patch_command_channel(message.channel.id) and looks_like_lol_patch_command(message.content)
    if not is_allowed_command_channel(message.channel.id) and not is_allowed_lol_channel:
        if is_lol_patch_command_channel(message.channel.id) and looks_like_prefixed_command(message.content):
            await message.reply(embed=build_unknown_command_embed(True), mention_author=False)
        return

    await bot.process_commands(message)


@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    if is_allowed_command_channel(ctx.channel.id):
        return True

    command_name = ctx.command.qualified_name if ctx.command else ""
    return is_lol_patch_command_channel(ctx.channel.id) and command_name.split(" ", 1)[0] in {"lolpatch", "helpme"}


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        return

    if isinstance(error, commands.CommandNotFound):
        await ctx.reply(
            embed=build_unknown_command_embed(is_lol_patch_command_channel(ctx.channel.id)),
            mention_author=False,
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(
            embed=build_lol_help_embed() if is_lol_patch_command_channel(ctx.channel.id) else build_general_help_embed(),
            mention_author=False,
        )
        return

    raise error


@bot.command(name="add")
async def add_plan(ctx: commands.Context, *, content: str) -> None:
    if not ctx.guild:
        await ctx.reply(embed=build_embed("這裡不能使用", "請在伺服器頻道中使用這個指令喔 (´・ω・`)", ERROR_COLOR))
        return

    plan_date = today_string()
    store.add_plan(ctx.guild.id, ctx.author.id, content.strip(), plan_date)
    await ctx.reply(
        embed=build_embed(
            "已加入今天的計畫",
            f"**{plan_date}**\n🌱 {content.strip()}\n\n收到，放進清單裡了 (｡•̀ᴗ-)✧",
            SUCCESS_COLOR,
        ),
        mention_author=False,
    )


@bot.command(name="today")
async def show_today(ctx: commands.Context) -> None:
    if not ctx.guild:
        await ctx.reply(embed=build_embed("這裡不能使用", "請在伺服器頻道中使用這個指令喔 (´・ω・`)", ERROR_COLOR))
        return

    rows = store.list_plans(ctx.guild.id, ctx.author.id, today_string())
    await ctx.reply(embed=build_plan_embed(ctx.author, today_string(), rows), mention_author=False)


@bot.command(name="plan")
async def show_plan(ctx: commands.Context, plan_date: str) -> None:
    if not ctx.guild:
        await ctx.reply(embed=build_embed("這裡不能使用", "請在伺服器頻道中使用這個指令喔 (´・ω・`)", ERROR_COLOR))
        return

    try:
        datetime.strptime(plan_date, "%Y-%m-%d")
    except ValueError:
        await ctx.reply(
            embed=build_embed(
                "日期格式不對",
                f"請使用 `YYYY-MM-DD`，例如：`{prefix}plan 2026-06-04` (´･_･`)",
                ERROR_COLOR,
            ),
            mention_author=False,
        )
        return

    rows = store.list_plans(ctx.guild.id, ctx.author.id, plan_date)
    await ctx.reply(embed=build_plan_embed(ctx.author, plan_date, rows), mention_author=False)


@bot.command(name="done")
async def mark_done(ctx: commands.Context, index: int) -> None:
    if not ctx.guild:
        await ctx.reply(embed=build_embed("這裡不能使用", "請在伺服器頻道中使用這個指令喔 (´・ω・`)", ERROR_COLOR))
        return

    selected = store.set_done(ctx.guild.id, ctx.author.id, today_string(), index)
    if not selected:
        await ctx.reply(
            embed=build_embed(
                "找不到這個編號",
                f"先用 `{prefix}today` 看一下今天的清單，再選正確編號喔 (´･ω･`)",
                WARNING_COLOR,
            ),
            mention_author=False,
        )
        return

    await ctx.reply(
        embed=build_embed(
            "完成一件事了",
            f"✅ {selected['content']}\n\n漂亮，今天又往前一步 (๑•̀ㅂ•́)و✧",
            SUCCESS_COLOR,
        ),
        mention_author=False,
    )


@bot.command(name="delete")
async def delete_plan(ctx: commands.Context, index: int) -> None:
    if not ctx.guild:
        await ctx.reply(embed=build_embed("這裡不能使用", "請在伺服器頻道中使用這個指令喔 (´・ω・`)", ERROR_COLOR))
        return

    selected = store.delete_plan(ctx.guild.id, ctx.author.id, today_string(), index)
    if not selected:
        await ctx.reply(
            embed=build_embed(
                "找不到這個編號",
                f"先用 `{prefix}today` 看一下今天的清單，再選正確編號喔 (´･ω･`)",
                WARNING_COLOR,
            ),
            mention_author=False,
        )
        return

    await ctx.reply(
        embed=build_embed(
            "已從清單移除",
            f"🗑️ {selected['content']}\n\n清單變輕一點了 (｡･ω･｡)",
            SUCCESS_COLOR,
        ),
        mention_author=False,
    )


@bot.command(name="reminder")
async def show_reminder(ctx: commands.Context) -> None:
    if not reminder_channel_id:
        await ctx.reply(
            embed=build_embed(
                "每日提醒尚未啟用",
                "請在 `.env` 設定 `REMINDER_CHANNEL_ID`，我就能每天提醒你整理計畫 (｡•́︿•̀｡)",
                WARNING_COLOR,
            ),
            mention_author=False,
        )
        return

    await ctx.reply(
        embed=build_embed(
            "每日提醒已啟用",
            f"每天 **{reminder_time}**（台北時間）會發送到 <#{reminder_channel_id}>。\n我會準時敲你一下 (っ･ω･)っ",
            SUCCESS_COLOR,
        ),
        mention_author=False,
    )


@bot.command(name="testreminder")
async def test_reminder(ctx: commands.Context) -> None:
    if not reminder_channel_id:
        await ctx.reply(
            embed=build_embed(
                "還不能測試提醒",
                "請先在 `.env` 設定 `REMINDER_CHANNEL_ID`，再重啟 bot 後測試 (｡•́︿•̀｡)",
                WARNING_COLOR,
            ),
            mention_author=False,
        )
        return

    await send_daily_reminder()
    await ctx.reply(
        embed=build_embed(
            "測試提醒已送出",
            f"我剛剛已經把每日提醒送到 <#{reminder_channel_id}> 了。\n如果那邊有看到訊息，排程提醒也會用同樣方式發送 (๑•̀ㅂ•́)و✧",
            SUCCESS_COLOR,
        ),
        mention_author=False,
    )


@bot.group(name="lolpatch", invoke_without_command=True)
async def lol_patch(ctx: commands.Context, *category_names: str) -> None:
    if category_names and is_lol_patch_version_query(category_names[0]):
        if not await enforce_lol_user_cooldown(ctx):
            return
        await send_lol_patch_version(ctx, category_names[0], publish=is_admin_context(ctx))
        return

    if not await require_lol_admin(ctx):
        return

    categories = parse_lol_patch_categories(category_names)
    if category_names and not categories:
        await ctx.reply(embed=build_lol_category_help_embed(), mention_author=False)
        return

    patch_note = await fetch_latest_lol_patch_note(lol_patch_locale)
    await send_lol_patch_note(ctx.channel, patch_note, categories)


@lol_patch.command(name="check")
async def check_lol_patch(ctx: commands.Context, *category_names: str) -> None:
    if not await require_lol_admin(ctx):
        return

    if category_names:
        await ctx.reply(embed=build_lol_check_force_usage_embed("check"), mention_author=False)
        return

    if not lol_patch_channel_id:
        await ctx.reply(embed=build_embed("尚未設定 LoL 公告頻道", "請在 `.env` 設定 `LOL_PATCH_CHANNEL_ID`。", WARNING_COLOR), mention_author=False)
        return

    patch_note, posted = await check_lol_patch_note()
    if posted:
        await ctx.reply(embed=build_embed("LoL 更新公告已發送", f"已發送 **{describe_lol_patch_categories(set(LOL_PATCH_DEFAULT_CATEGORIES))}**：[{patch_note['title']}]({patch_note['url']})", SUCCESS_COLOR), mention_author=False)
        return

    await ctx.reply(embed=build_embed("沒有新的 LoL 更新公告", f"目前最新公告是：[{patch_note['title']}]({patch_note['url']})", WARNING_COLOR), mention_author=False)


@lol_patch.command(name="force")
async def force_lol_patch(ctx: commands.Context, *category_names: str) -> None:
    if not await require_lol_admin(ctx):
        return

    if category_names:
        await ctx.reply(embed=build_lol_check_force_usage_embed("force"), mention_author=False)
        return

    patch_note, _ = await check_lol_patch_note(force=True)
    await ctx.reply(embed=build_embed("LoL 更新公告已強制發送", f"已發送 **{describe_lol_patch_categories(set(LOL_PATCH_DEFAULT_CATEGORIES))}**：[{patch_note['title']}]({patch_note['url']})", SUCCESS_COLOR), mention_author=False)


async def send_lol_patch_category(ctx: commands.Context, categories: set[str]) -> None:
    if not await require_lol_admin(ctx):
        return

    patch_note = await fetch_latest_lol_patch_note(lol_patch_locale)
    await send_lol_patch_note(ctx.channel, patch_note, categories)


async def send_lol_patch_version(ctx: commands.Context, version: str, publish: bool = True) -> None:
    patch_note = await fetch_lol_patch_note_by_version(lol_patch_locale, version)
    if not patch_note:
        await ctx.reply(
            embed=build_embed("找不到版本", f"官方版本列表中找不到 `{version}`，可用 `{prefix}lolpatch list` 查看近期版本。", WARNING_COLOR),
            mention_author=False,
            delete_after=None if publish else LOL_USER_QUERY_DELETE_SECONDS,
        )
        return

    if not publish:
        await ctx.reply(
            embed=build_embed(
                f"LoL {version} 版本資訊",
                f"查找到官方版本公告：[{patch_note['title']}]({patch_note['url']})\n\n一般使用者只提供查找連結；完整公告發布需管理員權限。",
                THEME_COLOR,
            ),
            mention_author=False,
            delete_after=LOL_USER_QUERY_DELETE_SECONDS,
        )
        return

    latest_note = await fetch_latest_lol_patch_note(lol_patch_locale)
    if patch_note["id"] != latest_note["id"]:
        await send_lol_patch_summary(ctx.channel, patch_note)
        return

    await send_lol_patch_note(ctx.channel, patch_note, set(LOL_PATCH_DEFAULT_CATEGORIES))


@lol_patch.command(name="list", aliases=["versions", "版本列表"])
async def lol_patch_list(ctx: commands.Context, limit: int = 5) -> None:
    if not await enforce_lol_user_cooldown(ctx):
        return

    limit = max(1, min(limit, 10))
    notes = await fetch_lol_patch_note_list(lol_patch_locale, limit=limit)
    if not notes:
        await ctx.reply(embed=build_embed("找不到版本列表", "官方版本更新列表目前沒有可解析的文章。", WARNING_COLOR), mention_author=False, delete_after=None if is_admin_context(ctx) else LOL_USER_QUERY_DELETE_SECONDS)
        return

    lines = []
    for note in notes:
        version = note.get("version") or "未知版本"
        url = note.get("url") or ""
        lines.append(f"`{prefix}lolpatch {version}`  [{version} 版本更新公告]({url})")

    await ctx.reply(embed=build_embed("LoL 前幾個版本", "\n".join(lines), THEME_COLOR), mention_author=False, delete_after=None if is_admin_context(ctx) else LOL_USER_QUERY_DELETE_SECONDS)


@lol_patch.command(name="version", aliases=["ver", "版本"])
async def lol_patch_version(ctx: commands.Context, version: str, *category_names: str) -> None:
    if not await enforce_lol_user_cooldown(ctx):
        return

    if category_names:
        await ctx.reply(embed=build_embed("版本查詢用法錯誤", f"請使用 `{prefix}lolpatch {version}`，版本查詢不再支援後方分類參數。", WARNING_COLOR), mention_author=False, delete_after=None if is_admin_context(ctx) else LOL_USER_QUERY_DELETE_SECONDS)
        return

    await send_lol_patch_version(ctx, version, publish=is_admin_context(ctx))


@lol_patch.command(name="champions", aliases=["champion", "heroes", "hero", "英雄"])
async def lol_patch_champions(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {"champions"})


@lol_patch.command(name="items", aliases=["item", "物品"])
async def lol_patch_items(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {"items"})


@lol_patch.command(name="aram", aliases=["大亂鬥", "隨機單中"])
async def lol_patch_aram(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {"aram"})


@lol_patch.command(name="arena", aliases=["競技場"])
async def lol_patch_arena(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {"arena"})


@lol_patch.command(name="systems", aliases=["system", "other", "系統", "活動"])
async def lol_patch_systems(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {"systems"})


@lol_patch.command(name="all", aliases=["full", "全部", "完整"])
async def lol_patch_all(ctx: commands.Context) -> None:
    await send_lol_patch_category(ctx, {LOL_PATCH_ALL_CATEGORY})


@bot.command(name="helpme")
async def help_me(ctx: commands.Context) -> None:
    if is_lol_patch_command_channel(ctx.channel.id):
        await ctx.reply(embed=build_lol_help_embed(), mention_author=False)
        return

    await ctx.reply(embed=build_general_help_embed(), mention_author=False)


try:
    if not token or token == "put_your_bot_token_here":
        raise RuntimeError("請先在 .env 設定 DISCORD_TOKEN")

    lock_handle = acquire_single_instance_lock()
    parse_channel_id(allowed_channel_id, "ALLOWED_CHANNEL_ID")
    parse_channel_id(reminder_channel_id, "REMINDER_CHANNEL_ID")
    parse_channel_id(lol_patch_channel_id, "LOL_PATCH_CHANNEL_ID")
    parse_reminder_time(reminder_time)
    parse_positive_int(lol_patch_check_minutes, "LOL_PATCH_CHECK_MINUTES", 30)
    bot.run(token)
except RuntimeError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
finally:
    if "lock_handle" in locals():
        lock_handle.close()
