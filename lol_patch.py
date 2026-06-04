import asyncio  # 控制非同步發送節奏，例如圖片之間稍微等待，避免 Discord 訊息太擠。
import re  # 使用正規表示式解析版本號、圖片尺寸、HTML 片段與文字格式。
from datetime import datetime  # 解析 Riot 官方文章的發布時間，並顯示在 Discord Embed。
from html import unescape  # 將 HTML entity 還原成正常文字，例如 &amp; 變成 &。
from html.parser import HTMLParser  # 建立簡易 HTML 解析器，把官方公告拆成文字區塊與圖片區塊。
from typing import Any  # 標註 patch_note 這類混合字典，裡面可能有文字、圖片、時間等不同型別。

import aiohttp  # 非同步抓取 Riot 官方網站內容，不會卡住 Discord bot。
import discord  # 建立 Discord Embed、發送圖片與文字公告。


LOL_PATCH_COLOR = 0x0A7CFF
LOL_PATCH_BASE_URL = "https://www.leagueoflegends.com"
DISCORD_MESSAGE_LIMIT = 2000
LOL_PATCH_TEXT_CHUNK_LIMIT = 1800
LOL_PATCH_IMAGE_DELAY_SECONDS = 0.6
LOL_PATCH_ICON_MAX_SIZE = 512
LOL_PATCH_ALL_CATEGORY = "all"
LOL_PATCH_DEFAULT_CATEGORIES = {"systems", "champions", "items"}
LOL_PATCH_CATEGORY_LABELS = {
    "champions": "英雄改動",
    "items": "物品改動",
    "aram": "隨機單中 / 大亂鬥改動",
    "arena": "競技場改動",
    "systems": "前置系統 / 活動更動",
}
# 用官方公告的章節標題判斷分類；只要標題含有這些關鍵字，就歸到對應分類。
LOL_PATCH_CATEGORY_KEYWORDS = {
    "champions": ("英雄",),
    "items": ("物品", "裝備", "道具"),
    "aram": ("隨機單中", "大亂鬥"),
    "arena": ("競技場",),
}
LOL_PATCH_ALWAYS_INCLUDE_HEADINGS = {"版本概要"}
LOL_PATCH_PLAIN_IMAGE_HEADINGS = {"全新造型與炫彩造型"}



def normalize_space(value: str) -> str:
    """把 HTML entity 還原，並將多個空白壓成一個空白。"""
    return re.sub(r"\s+", " ", unescape(value)).strip()


def strip_tags(value: str) -> str:
    """移除簡單 HTML tag，用在網頁標題備援解析。"""
    return normalize_space(re.sub(r"<[^>]+>", "", value))


def first_srcset_url(value: str) -> str:
    """srcset 可能有多張不同尺寸圖片，取第一張當作 Discord 顯示用圖片。"""
    return value.split(",", 1)[0].strip().split(" ", 1)[0].strip()


def absolute_lol_url(url: str) -> str:
    """Riot 網頁有時只給相對網址，這裡補成完整網址。"""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return f"{LOL_PATCH_BASE_URL}{url}"


def is_lol_image_url(url: str) -> bool:
    """過濾掉空值、base64 data URL 和 SVG，保留 Discord 比較適合預覽的圖片。"""
    if not url or url.startswith("data:"):
        return False

    clean_url = unescape(url).lower()
    return clean_url.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def get_lol_image_dimensions(url: str) -> tuple[int, int] | None:
    """從 Riot 圖片網址中的 64x64、1920x1080 這類片段推測圖片尺寸。"""
    match = re.search(r"-(\d{2,4})x(\d{2,4})\.(?:jpg|jpeg|png|webp|gif)(?:$|\?)", url, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def classify_lol_image(url: str) -> str:
    """判斷圖片是正文大圖還是小圖示。

    英雄頭像、技能、物品 icon 通常是正方形且尺寸較小；
    版本概要、造型展示等圖片通常寬高較大，所以用一般大圖 Embed 顯示。
    """
    lower_url = unescape(url).lower()
    # 只把英雄與物品視為可顯示 icon；技能圖示會在 _add_image 直接略過。
    if "/img/champion/" in lower_url or "/img/item/" in lower_url:
        return "icon"

    dimensions = get_lol_image_dimensions(url)
    if not dimensions:
        return "image"

    width, height = dimensions
    if width <= LOL_PATCH_ICON_MAX_SIZE and height <= LOL_PATCH_ICON_MAX_SIZE:
        return "icon"
    return "image"


def is_lol_spell_icon(url: str) -> bool:
    """判斷圖片是否為技能圖示。"""
    lower_url = unescape(url).lower()
    return "/img/spell/" in lower_url or "/img/passive/" in lower_url


def get_meta_content(html: str, key: str) -> str | None:
    """從 HTML 的 meta 標籤取 Open Graph 資訊，例如 og:title、og:image。"""
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return normalize_space(match.group(1))
    return None


class LolPatchBodyParser(HTMLParser):
    """把 LoL 官方公告正文轉成 Discord 可發送的區塊。

    官方頁面的正文順序本來就是 HTML 節點順序；這個 parser 只做輕量轉換：
    標題轉成 Markdown 標題、段落保留文字、條列加上 -、圖片保留 URL。
    """

    SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "svg", "button"}
    TEXT_TAGS = {"h1", "h2", "h3", "h4", "p", "li"}
    STOP_HEADINGS = {"相關文章", "更多文章", "推薦文章"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str]] = []
        self.pending_images: list[dict[str, str]] = []
        self.started = False
        self.stopped = False
        self.skip_depth = 0
        self.quote_depth = 0
        self.current_tag: str | None = None
        self.current_text: list[str] = []
        self.seen_images: set[str] = set()
        self.seen_section_heading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stopped:
            return

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        attrs_map = {name: value or "" for name, value in attrs}

        if tag == "blockquote":
            self.quote_depth += 1
            return

        if tag == "br" and self.current_tag:
            self.current_text.append("\n")
            return

        if tag == "hr" and self.started:
            self._flush_text()
            self.blocks.append({"type": "text", "value": "---"})
            return

        if tag in {"img", "source"} and self.started:
            self._add_image(attrs_map)
            return

        if tag in self.TEXT_TAGS:
            if tag == "h1":
                self.started = True
            if not self.started:
                return
            self._flush_text()
            self.current_tag = tag
            self.current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return

        if self.skip_depth or self.stopped:
            return

        if tag == "blockquote" and self.quote_depth:
            self.quote_depth -= 1
            return

        if tag == self.current_tag:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if self.skip_depth or self.stopped or not self.current_tag:
            return
        self.current_text.append(data)

    def close(self) -> None:
        self._flush_text()
        super().close()

    def _flush_text(self) -> None:
        if not self.current_tag:
            return

        text = normalize_space(" ".join(self.current_text))
        tag = self.current_tag
        self.current_tag = None
        self.current_text = []

        if not text:
            return

        if tag in {"h1", "h2", "h3", "h4"} and text in self.STOP_HEADINGS:
            self.stopped = True
            return

        if tag in {"h2", "h3", "h4"}:
            # 作者頭像等頁首圖片通常出現在第一個章節前；正文圖片從章節文字出現後才送出。
            self.seen_section_heading = True

        self.blocks.append({"type": "text", "value": self._format_text(tag, text)})
        if tag in {"h2", "h3", "h4"} and self.pending_images:
            # 官方常把英雄頭像、技能、物品 icon 放在標題文字前。
            # 先等標題文字進入 blocks，再把暫存 icon 接在標題後，分類篩選才會把它歸到正確章節。
            self.blocks.extend(self.pending_images)
            self.pending_images = []

    def _format_text(self, tag: str, text: str) -> str:
        if tag == "h1":
            return f"# {text}"
        if tag == "h2":
            return f"## {text}"
        if tag == "h3":
            return f"### {text}"
        if tag == "h4":
            return f"**{text}**"
        if tag == "li":
            return f"- {text}"
        if self.quote_depth:
            # Discord 的 `>` 會變成大片灰色引用區；公告文字很多時版面會很破碎。
            # 保留原文內容，但改用一般文字顯示。
            return text
        return text

    def _add_image(self, attrs: dict[str, str]) -> None:
        if not self.seen_section_heading:
            return

        raw_url = attrs.get("src") or attrs.get("data-src") or first_srcset_url(attrs.get("srcset", ""))
        url = absolute_lol_url(raw_url.strip())
        if not is_lol_image_url(url) or url in self.seen_images:
            return
        # 使用者目前只想保留英雄/裝備縮圖；技能圖示不顯示，避免版面太碎。
        if is_lol_spell_icon(url):
            return

        if self.current_tag and normalize_space(" ".join(self.current_text)):
            self._flush_text()
        self.seen_images.add(url)
        image_block = {
            "type": classify_lol_image(url),
            "url": url,
            "alt": normalize_space(attrs.get("alt", "")),
        }
        if self.current_tag in {"h2", "h3", "h4"}:
            self.pending_images.append(image_block)
            return

        self.blocks.append(image_block)


def extract_lol_patch_blocks(html: str) -> list[dict[str, str]]:
    parser = LolPatchBodyParser()
    parser.feed(html)
    parser.close()
    return parser.blocks


def clean_lol_heading(value: str) -> str:
    """移除 Markdown 標題符號，留下官方章節名稱用來判斷分類。"""
    return re.sub(r"^#+\s*", "", value).strip()


def lol_block_heading_level(value: str) -> int | None:
    """判斷文字區塊是不是 Markdown 標題，回傳 # 的層級。"""
    match = re.match(r"^(#{1,6})\s+", value)
    return len(match.group(1)) if match else None


def detect_lol_patch_category(heading: str) -> str:
    """依照官方章節標題歸類；不屬於指定分類的章節放進 systems。"""
    clean_heading = clean_lol_heading(heading)
    for category, keywords in LOL_PATCH_CATEGORY_KEYWORDS.items():
        if any(keyword in clean_heading for keyword in keywords):
            return category
    return "systems"


def filter_lol_patch_blocks(blocks: list[dict[str, str]], categories: set[str] | None) -> list[dict[str, str]]:
    """依分類篩選公告區塊。

    LoL 官方公告的主章節通常是 h2，也就是這裡的 `## 標題`。
    程式遇到新的 h2 就切換目前分類，之後的段落、h3/h4、小圖片都跟著該分類發送。
    """
    if categories is None or LOL_PATCH_ALL_CATEGORY in categories:
        return blocks

    # current_category 代表「目前走到官方公告的哪個大章節」。
    # 例如遇到 `## 英雄` 後，後面所有 h3 英雄條目都歸在 champions。
    filtered: list[dict[str, str]] = []
    current_category: str | None = None
    include_current_section = False

    for block in blocks:
        value = block.get("value", "") if block.get("type") == "text" else ""
        heading_level = lol_block_heading_level(value)
        if heading_level == 2:
            clean_heading = clean_lol_heading(value)
            include_current_section = clean_heading in LOL_PATCH_ALWAYS_INCLUDE_HEADINGS
            current_category = detect_lol_patch_category(value)

        if include_current_section or current_category in categories:
            filtered.append(block)

    return filtered


def parse_lol_patch_categories(values: tuple[str, ...]) -> set[str]:
    """把使用者輸入的分類名稱轉成程式內部分類代碼。"""
    if not values:
        return set(LOL_PATCH_DEFAULT_CATEGORIES)

    aliases = {
        "default": set(LOL_PATCH_DEFAULT_CATEGORIES),
        "預設": set(LOL_PATCH_DEFAULT_CATEGORIES),
        "all": {LOL_PATCH_ALL_CATEGORY},
        "full": {LOL_PATCH_ALL_CATEGORY},
        "全部": {LOL_PATCH_ALL_CATEGORY},
        "完整": {LOL_PATCH_ALL_CATEGORY},
        "champion": {"champions"},
        "champions": {"champions"},
        "hero": {"champions"},
        "heroes": {"champions"},
        "英雄": {"champions"},
        "item": {"items"},
        "items": {"items"},
        "物品": {"items"},
        "aram": {"aram"},
        "隨機單中": {"aram"},
        "大亂鬥": {"aram"},
        "arena": {"arena"},
        "競技場": {"arena"},
        "system": {"systems"},
        "systems": {"systems"},
        "other": {"systems"},
        "其他": {"systems"},
        "系統": {"systems"},
    }

    categories: set[str] = set()
    for value in values:
        key = value.strip().lower()
        if key in aliases:
            categories.update(aliases[key])

    if LOL_PATCH_ALL_CATEGORY in categories:
        return {LOL_PATCH_ALL_CATEGORY}
    return categories


def describe_lol_patch_categories(categories: set[str] | None) -> str:
    """產生給 Discord 顯示的分類說明文字。"""
    if categories is None or LOL_PATCH_ALL_CATEGORY in categories:
        return "完整公告"

    labels = [LOL_PATCH_CATEGORY_LABELS[category] for category in LOL_PATCH_CATEGORY_LABELS if category in categories]
    return "、".join(labels) if labels else "未指定分類"


def find_lol_patch_overview_image(blocks: list[dict[str, str]]) -> str | None:
    """尋找「版本概要」章節中的第一張大圖。

    發布舊版本時只需要連結與版本概要圖片，因此不用展開完整公告內容。
    """
    in_overview = False
    for block in blocks:
        block_type = block.get("type")
        value = block.get("value", "") if block_type == "text" else ""
        heading_level = lol_block_heading_level(value)

        if heading_level == 2:
            in_overview = clean_lol_heading(value) in LOL_PATCH_ALWAYS_INCLUDE_HEADINGS
            continue

        if in_overview and block_type == "image":
            return block.get("url")

    return None


def escape_lol_discord_conflicts(value: str) -> str:
    """跳脫會和 Discord Markdown 衝突的字元。

    官方公告常用 `||` 分隔近戰/遠程數值，但 Discord 會把 `||文字||`
    解讀成暴雷遮罩；這裡只處理直線，保留我們自己加的標題與粗體格式。
    """
    return value.replace("|", "\\|")


def bold_lol_new_values(value: str) -> str:
    """把 `⇒` 後面的新數值加粗，讓改動後的結果比較醒目。"""

    def replace_match(match: re.Match[str]) -> str:
        new_value = match.group(1).strip()
        if not new_value or new_value.startswith("**"):
            return match.group(0)
        return f"⇒ **{new_value}**"

    return re.sub(r"⇒\s*([^\n]+)", replace_match, value)


def format_lol_patch_text_for_discord(value: str) -> str:
    """套用 LoL 公告文字輸出格式。"""
    return escape_lol_discord_conflicts(bold_lol_new_values(value))


def extract_lol_patch_urls(html: str, locale: str) -> list[str]:
    """從 LoL 版本更新列表頁抓出所有 patch notes 文章網址，並保留原本順序。"""
    pattern = (
        rf'(?:https?://www\.leagueoflegends\.com)?/'
        rf'{re.escape(locale)}/news/game-updates/[^"\'<>\s]*patch[^"\'<>\s]*notes/?'
    )
    urls: list[str] = []
    for match in re.finditer(pattern, html, flags=re.IGNORECASE):
        url = absolute_lol_url(match.group(0))
        if url not in urls:
            urls.append(url)
    return urls


def extract_lol_patch_version(value: str) -> str | None:
    """從標題或網址抓出版本號，例如 26.11。"""
    match = re.search(r"(\d{2})[.-](\d{1,2})", value)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def is_lol_patch_version_query(value: str) -> bool:
    """判斷使用者輸入是否像版本號，例如 26.11 或 26-11。"""
    return extract_lol_patch_version(value) is not None


def lol_patch_note_from_html(article_url: str, article_html: str) -> dict[str, Any]:
    """把單篇 LoL 公告 HTML 整理成統一資料格式。"""
    title = get_meta_content(article_html, "og:title")
    if not title:
        # 如果官方頁面沒有 og:title，就退一步從 h1 標題抓文字。
        heading = re.search(r"<h1[^>]*>(.*?)</h1>", article_html, flags=re.IGNORECASE | re.DOTALL)
        title = strip_tags(heading.group(1)) if heading else "League of Legends 版本更新公告"

    return {
        "id": article_url.rstrip("/"),
        "title": title,
        "description": get_meta_content(article_html, "og:description"),
        "image": get_meta_content(article_html, "og:image"),
        "published_at": get_meta_content(article_html, "article:published_time"),
        "url": article_url,
        "version": extract_lol_patch_version(title or article_url),
        "blocks": extract_lol_patch_blocks(article_html),
    }


async def fetch_lol_page(url: str) -> str:
    """非同步下載 LoL 官方網頁內容；aiohttp 不會阻塞 Discord bot 事件迴圈。"""
    headers = {
        "User-Agent": "LemonDiscordBot/1.0",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            return await response.text()


async def fetch_latest_lol_patch_note(locale: str) -> dict[str, Any]:
    """抓取指定語系最新一篇 LoL 版本公告，整理成 dict 給 Embed 使用。"""
    tag_url = f"{LOL_PATCH_BASE_URL}/{locale}/news/tags/patch-notes/"
    tag_html = await fetch_lol_page(tag_url)
    urls = extract_lol_patch_urls(tag_html, locale)
    if not urls:
        raise RuntimeError(f"在官方版本更新列表找不到文章：{tag_url}")

    article_url = urls[0]
    article_html = await fetch_lol_page(article_url)
    return lol_patch_note_from_html(article_url, article_html)


async def fetch_lol_patch_note_list(locale: str, limit: int = 5) -> list[dict[str, str | None]]:
    """抓取官方版本公告列表，用於查詢前幾個版本。"""
    tag_url = f"{LOL_PATCH_BASE_URL}/{locale}/news/tags/patch-notes/"
    tag_html = await fetch_lol_page(tag_url)
    urls = extract_lol_patch_urls(tag_html, locale)[:limit]
    notes: list[dict[str, str | None]] = []

    for url in urls:
        version = extract_lol_patch_version(url)
        notes.append(
            {
                "version": version,
                "url": url,
                "title": f"{version} 版本更新公告" if version else "版本更新公告",
            }
        )

    return notes


async def fetch_lol_patch_note_by_version(locale: str, version: str) -> dict[str, Any] | None:
    """依版本號抓指定版本公告；找不到時回傳 None。"""
    normalized_version = version.strip().lower().replace("patch", "").replace("版本", "").strip()
    notes = await fetch_lol_patch_note_list(locale, limit=12)

    for note in notes:
        note_version = note.get("version")
        if note_version == normalized_version:
            article_url = note["url"]
            if not article_url:
                return None
            article_html = await fetch_lol_page(article_url)
            return lol_patch_note_from_html(article_url, article_html)

    return None


def build_lol_patch_embed(patch_note: dict[str, Any]) -> discord.Embed:
    """把 LoL 公告資料轉成 Discord Embed。"""
    embed = discord.Embed(
        title=patch_note["title"] or "League of Legends 版本更新公告",
        description=patch_note["description"] or "官方版本更新公告已發布。",
        url=patch_note["url"],
        color=LOL_PATCH_COLOR,
    )
    embed.set_footer(text="資料來源：League of Legends 官方網站；本 bot 非 Riot 官方服務")
    if patch_note["published_at"]:
        try:
            embed.timestamp = datetime.fromisoformat(patch_note["published_at"].replace("Z", "+00:00"))
        except ValueError:
            pass
    return embed


async def send_lol_patch_note(
    channel: discord.abc.Messageable,
    patch_note: dict[str, Any],
    categories: set[str] | None = None,
) -> None:
    """把 LoL 版本公告送到指定 Discord 頻道。

    會先送摘要 Embed，再依照分類與官方正文順序送出文字區塊和圖片。
    categories=None 或 {"all"} 代表完整公告；預設自動公告會傳入精簡分類。
    """
    await channel.send(
        content="新的《英雄聯盟》版本更新公告發布了：",
        embed=build_lol_patch_embed(patch_note),
    )

    blocks = patch_note.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        await channel.send(f"官方原文：{patch_note['url']}")
        return

    # 先依使用者指定分類過濾 blocks，再交給發送流程決定要純文字或卡片化。
    selected_blocks = filter_lol_patch_blocks(blocks, categories)
    category_text = describe_lol_patch_categories(categories)
    if not selected_blocks:
        await channel.send(f"找不到「{category_text}」相關段落，可能這版官方公告沒有此分類。官方原文：{patch_note['url']}")
        return

    await channel.send(f"發佈分類：**{category_text}**\n內容依官方公告順序整理：{patch_note['url']}")
    await send_lol_patch_blocks(channel, selected_blocks)


async def send_lol_patch_summary(channel: discord.abc.Messageable, patch_note: dict[str, Any]) -> None:
    """只發布公告網站與版本概要圖片。

    用於舊版本查詢，避免使用者回頭查資料時把整份舊公告刷進頻道。
    """
    blocks = patch_note.get("blocks")
    overview_image = find_lol_patch_overview_image(blocks) if isinstance(blocks, list) else None

    embed = discord.Embed(
        title=patch_note["title"] or "League of Legends 版本更新公告",
        description=f"舊版本查詢只顯示官方公告連結與版本概要圖片。\n{patch_note['url']}",
        url=patch_note["url"],
        color=LOL_PATCH_COLOR,
    )
    embed.set_footer(text="資料來源：League of Legends 官方網站；本 bot 非 Riot 官方服務")
    if patch_note["published_at"]:
        try:
            embed.timestamp = datetime.fromisoformat(str(patch_note["published_at"]).replace("Z", "+00:00"))
        except ValueError:
            pass
    await channel.send(embed=embed)
    if overview_image:
        await send_lol_image(channel, overview_image, "")
    elif patch_note["image"]:
        await send_lol_image(channel, patch_note["image"], "")


async def send_lol_patch_blocks(channel: discord.abc.Messageable, blocks: list[dict[str, str]]) -> None:
    """依序送出公告區塊，並自動把太長的文字切成多則訊息。"""
    # 預設公告包含 `## 英雄` 時，改用卡片流程，讓每位英雄/裝備更醒目。
    if any(block.get("value") == "## 英雄" for block in blocks if block.get("type") == "text"):
        await send_lol_patch_blocks_with_champion_cards(channel, blocks)
        return

    pending_text: list[str] = []
    pending_length = 0

    async def flush_text() -> None:
        nonlocal pending_text, pending_length
        if not pending_text:
            return

        # 用單換行串接區塊，讓 Discord 內文更緊湊，不會每段之間空太多行。
        await send_lol_text(channel, "\n".join(pending_text))
        pending_text = []
        pending_length = 0

    for block in blocks:
        block_type = block.get("type")
        if block_type == "icon":
            # Discord 不能把外部圖片嵌在一般文字行內，也就是無法像網頁一樣
            # 把英雄/技能圖示放在名稱正前方；若用 Embed 或網址預覽又會產生框。
            # 因此這裡略過小圖示，只保留官方文字內容，避免版面被框線打亂。
            continue

        if block_type == "image":
            await flush_text()
            await send_lol_image(channel, block.get("url", ""), block.get("alt", ""))
            await asyncio.sleep(LOL_PATCH_IMAGE_DELAY_SECONDS)
            continue

        if block_type != "text":
            continue

        value = block.get("value", "").strip()
        if not value:
            continue

        value = format_lol_patch_text_for_discord(value)
        next_length = pending_length + len(value) + (1 if pending_text else 0)
        if pending_text and next_length > LOL_PATCH_TEXT_CHUNK_LIMIT:
            await flush_text()

        pending_length += len(value) + (1 if pending_text else 0)
        pending_text.append(value)

    await flush_text()


def split_lol_card_sections(blocks: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """把 h3 條目切成一張張卡片。

    官方 HTML 常把英雄/裝備 icon 放在 h3 標題前，所以先暫存這些 icon，
    等讀到下一個 h3 時再放進新卡片，避免縮圖錯位。
    """
    sections: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    pending_icons: list[dict[str, str]] = []

    for block in blocks:
        # icon 有時在標題前，例如 `<img> 布蘭德`，所以先暫存。
        if block.get("type") == "icon" and not current:
            pending_icons.append(block)
            continue

        value = block.get("value", "") if block.get("type") == "text" else ""
        if lol_block_heading_level(value) == 3:
            # 遇到新的 h3，就代表一張新卡片開始。
            if current:
                sections.append(current)
            current = [*pending_icons, block]
            pending_icons = []
            continue

        if block.get("type") == "icon" and current:
            if is_lol_spell_icon(block.get("url", "")):
                current.append(block)
            else:
                pending_icons.append(block)
            continue

        if current:
            current.append(block)

    if current:
        sections.append(current)

    return sections


async def send_lol_patch_blocks_with_champion_cards(channel: discord.abc.Messageable, blocks: list[dict[str, str]]) -> None:
    """發送公告區塊，英雄章節改用較醒目的卡片顯示。"""
    # 官方公告順序大致是：英雄前更動 -> 英雄 -> 物品/其他。
    # 這裡先把三段拆開，英雄段用「每位英雄一張卡」，前後段也可卡片化。
    before_champions: list[dict[str, str]] = []
    champion_blocks: list[dict[str, str]] = []
    after_champions: list[dict[str, str]] = []
    mode = "before"

    for block in blocks:
        value = block.get("value", "") if block.get("type") == "text" else ""
        if value == "## 英雄":
            mode = "champions"
            continue

        if mode == "champions" and lol_block_heading_level(value) == 2:
            mode = "after"

        if mode == "before":
            before_champions.append(block)
        elif mode == "champions":
            champion_blocks.append(block)
        else:
            after_champions.append(block)

    if before_champions:
        await send_lol_patch_blocks_plain(channel, before_champions, use_cards=True)

    if champion_blocks:
        await channel.send("## 英雄")
        for section in split_lol_card_sections(champion_blocks):
            await send_lol_card(channel, section, "英雄改動")
            await asyncio.sleep(LOL_PATCH_IMAGE_DELAY_SECONDS)

    if after_champions:
        await send_lol_patch_blocks_plain(channel, after_champions, use_cards=True)


async def send_lol_patch_blocks_plain(
    channel: discord.abc.Messageable,
    blocks: list[dict[str, str]],
    use_cards: bool = False,
) -> None:
    """一般段落發送流程；必要時把 h3 條目整理成卡片。"""
    if use_cards:
        await send_lol_patch_blocks_as_cards(channel, blocks)
        return

    pending_text: list[str] = []
    pending_length = 0

    async def flush_text() -> None:
        nonlocal pending_text, pending_length
        if not pending_text:
            return
        await send_lol_text(channel, "\n".join(pending_text))
        pending_text = []
        pending_length = 0

    for block in blocks:
        block_type = block.get("type")
        if block_type == "icon":
            continue

        if block_type == "image":
            await flush_text()
            await send_lol_image(channel, block.get("url", ""), block.get("alt", ""))
            await asyncio.sleep(LOL_PATCH_IMAGE_DELAY_SECONDS)
            continue

        if block_type != "text":
            continue

        value = block.get("value", "").strip()
        if not value:
            continue

        value = format_lol_patch_text_for_discord(value)
        next_length = pending_length + len(value) + (1 if pending_text else 0)
        if pending_text and next_length > LOL_PATCH_TEXT_CHUNK_LIMIT:
            await flush_text()

        pending_length += len(value) + (1 if pending_text else 0)
        pending_text.append(value)

    await flush_text()


async def send_lol_patch_blocks_as_cards(channel: discord.abc.Messageable, blocks: list[dict[str, str]]) -> None:
    """把前置更動、裝備、符文、天賦等章節整理成卡片。"""
    heading = "改動"
    card_blocks: list[dict[str, str]] = []
    plain_blocks: list[dict[str, str]] = []
    mode = "before"

    for block in blocks:
        value = block.get("value", "") if block.get("type") == "text" else ""
        if lol_block_heading_level(value) == 2:
            # h2 是官方大章節，例如「輔助調整」「道具」。
            # 遇到新 h2 前，要先把上一個章節累積的內容送出去。
            if mode == "cards" and card_blocks:
                await send_lol_card_sections(channel, card_blocks, heading)
                card_blocks = []
            if mode == "plain" and plain_blocks:
                await send_lol_patch_blocks_plain(channel, plain_blocks)
                plain_blocks = []

            heading = clean_lol_heading(value)
            if heading in LOL_PATCH_PLAIN_IMAGE_HEADINGS:
                plain_blocks = [block]
                mode = "plain"
            else:
                await channel.send(value)
                mode = "cards"
            continue

        if mode == "cards":
            card_blocks.append(block)
        elif mode == "plain":
            plain_blocks.append(block)

    if card_blocks:
        await send_lol_card_sections(channel, card_blocks, heading)
    if plain_blocks:
        await send_lol_patch_blocks_plain(channel, plain_blocks)


async def send_lol_card_sections(channel: discord.abc.Messageable, blocks: list[dict[str, str]], fallback_title: str) -> None:
    """依 h3 拆卡；如果沒有 h3，就整個 h2 章節做成一張卡。"""
    # 有 h3：通常是每個英雄/裝備/天賦各一張卡。
    # 沒 h3：整個章節只有說明文字，就用 h2 標題做一張卡。
    has_h3 = any(lol_block_heading_level(block.get("value", "")) == 3 for block in blocks if block.get("type") == "text")
    sections = split_lol_card_sections(blocks) if has_h3 else [blocks]
    for section in sections:
        await send_lol_card(channel, section, fallback_title)
        await asyncio.sleep(LOL_PATCH_IMAGE_DELAY_SECONDS)


async def send_lol_card(channel: discord.abc.Messageable, section: list[dict[str, str]], fallback_title: str) -> None:
    """把單一 h3 條目整理成一張 Discord Embed 卡片。"""
    title = fallback_title
    thumbnail_url: str | None = None
    lines: list[str] = []

    for block in section:
        block_type = block.get("type")
        if block_type == "icon":
            url = block.get("url", "")
            # 縮圖只抓英雄/裝備 icon；技能 icon 已在解析階段被過濾。
            if not is_lol_spell_icon(url) and not thumbnail_url:
                thumbnail_url = url
            continue

        if block_type != "text":
            continue

        value = block.get("value", "").strip()
        if not value:
            continue

        if lol_block_heading_level(value) == 3:
            title = clean_lol_heading(value)
            continue

        if value == "---":
            continue

        lines.append(format_lol_patch_text_for_discord(value))

    description = "\n".join(lines).strip()
    if len(description) > 4000:
        description = description[:3990].rstrip() + "..."

    embed = discord.Embed(title=title, description=description or "官方公告未提供詳細文字。", color=LOL_PATCH_COLOR)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    await channel.send(embed=embed)


async def send_lol_text(channel: discord.abc.Messageable, text: str) -> None:
    """發送文字，若單段仍超過 Discord 限制就再切小段。"""
    text = text.strip()
    while len(text) > DISCORD_MESSAGE_LIMIT:
        split_at = text.rfind("\n", 0, LOL_PATCH_TEXT_CHUNK_LIMIT)
        if split_at <= 0:
            split_at = LOL_PATCH_TEXT_CHUNK_LIMIT

        await channel.send(text[:split_at].strip())
        text = text[split_at:].strip()
        await asyncio.sleep(LOL_PATCH_IMAGE_DELAY_SECONDS)

    if text:
        await channel.send(text)


async def send_lol_image(channel: discord.abc.Messageable, url: str, alt: str) -> None:
    """直接送官方圖片網址。

    不用自訂 Embed，避免 Discord 上出現框線或標題文字。
    小圖示不走這裡，避免在 Discord 上造成版面跑掉。
    """
    if not url:
        return

    await channel.send(url)
