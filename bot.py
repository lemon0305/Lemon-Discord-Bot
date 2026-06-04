import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


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
last_reminder_date: str | None = None

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


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")
    if allowed_channel_id:
        print(f"Commands are limited to channel ID: {allowed_channel_id}")
    else:
        print("Commands are allowed in every channel.")

    if reminder_channel_id and not daily_reminder_loop.is_running():
        daily_reminder_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if not is_allowed_command_channel(message.channel.id):
        return

    await bot.process_commands(message)


@bot.check
async def only_allowed_channel(ctx: commands.Context) -> bool:
    return is_allowed_command_channel(ctx.channel.id)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
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


@bot.command(name="helpme")
async def help_me(ctx: commands.Context) -> None:
    await ctx.reply(
        embed=build_embed(
            "每日計畫指令",
            "\n".join(
                [
                    f"`{prefix}add <內容>`  新增今天的計畫",
                    f"`{prefix}today`  查看今天的計畫",
                    f"`{prefix}done <編號>`  標記今天的計畫完成",
                    f"`{prefix}delete <編號>`  刪除今天的計畫",
                    f"`{prefix}plan <YYYY-MM-DD>`  查看指定日期的計畫",
                    f"`{prefix}reminder`  查看每日提醒設定",
                    f"`{prefix}testreminder`  立即測試每日提醒",
                    "",
                    "慢慢排，穩穩做。今天也可以很有章法 (´｡• ᵕ •｡`)",
                ]
            ),
        ),
        mention_author=False,
    )


try:
    if not token or token == "put_your_bot_token_here":
        raise RuntimeError("請先在 .env 設定 DISCORD_TOKEN")

    lock_handle = acquire_single_instance_lock()
    parse_channel_id(allowed_channel_id, "ALLOWED_CHANNEL_ID")
    parse_channel_id(reminder_channel_id, "REMINDER_CHANNEL_ID")
    parse_reminder_time(reminder_time)
    bot.run(token)
except RuntimeError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
finally:
    if "lock_handle" in locals():
        lock_handle.close()
