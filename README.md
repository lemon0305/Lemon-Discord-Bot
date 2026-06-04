# Discord 每日計畫機器人

一個用 Python、discord.py、SQLite 製作的 Discord 每日計畫 bot。

## 功能

- `!add <內容>`：新增今天的計畫
- `!today`：查看今天的計畫
- `!done <編號>`：標記今天的計畫完成
- `!delete <編號>`：刪除今天的計畫
- `!plan <YYYY-MM-DD>`：查看指定日期的計畫
- `!reminder`：查看每日提醒設定
- `!testreminder`：立即測試每日提醒
- `!helpme`：查看指令說明

## 安裝

```powershell
cd E:\Codex\discord-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

也可以直接使用專案附的啟動腳本：

```powershell
cd E:\Codex\discord-bot
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

## 設定

複製 `.env.example` 成 `.env`，並填入 Discord Bot Token：

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 範例：

```env
DISCORD_TOKEN=你的 Discord Bot Token
COMMAND_PREFIX=!
DATABASE_PATH=data/plans.db
ALLOWED_CHANNEL_ID=
REMINDER_CHANNEL_ID=
REMINDER_TIME=09:00
LOL_PATCH_CHANNEL_ID=
LOL_PATCH_LOCALE=zh-tw
LOL_PATCH_CHECK_MINUTES=30
LOL_PATCH_AUTO_POST=true
```

## 限定指令頻道

如果只想讓 bot 在某個頻道接受指令，請填入該頻道 ID：

```env
ALLOWED_CHANNEL_ID=你的 Discord 頻道 ID
```

`ALLOWED_CHANNEL_ID` 留空時，所有頻道都可以呼叫 bot。填入後，其他頻道使用指令時 bot 會安靜忽略，不會回覆。

## 每日提醒

如果要啟用每日提醒，請在 `.env` 填入：

```env
REMINDER_CHANNEL_ID=你的 Discord 頻道 ID
REMINDER_TIME=09:00
```

`REMINDER_TIME` 使用台北時間，格式是 `HH:MM`。`REMINDER_CHANNEL_ID` 留空時，每日提醒不會啟用。

設定完成並重啟 bot 後，可以使用 `!testreminder` 立即發送一次測試提醒。

取得頻道 ID 的方式：

1. Discord 使用者設定中開啟「開發者模式」。
2. 右鍵點你要使用的頻道。
3. 選擇「複製頻道 ID」。

## 避免重複回覆

bot 已加入單一執行鎖。如果同一個專案已經有一份 bot 在執行，再啟動第二份時會自動退出。

如果你已經不小心開了多份，請把多餘的終端機用 `Ctrl+C` 停掉，或在工作管理員結束多餘的 `python.exe`。

## Discord Developer Portal 設定

在 Discord Developer Portal 的 Bot 設定頁：

1. 開啟 `MESSAGE CONTENT INTENT`。
2. 邀請 bot 時至少給 `Send Messages`、`Read Message History` 權限。

