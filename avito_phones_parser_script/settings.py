from pathlib import Path

# База — корень проекта
BASE_DIR = Path(__file__).resolve().parents[1]

# ========== НАСТРОЙКИ ==========
INPUT_FILE = BASE_DIR / "АВТОСАЛОН 11.11.xlsx"   # или BASE_DIR / "phones.xlsx"
INPUT_SHEET = None
URL_COLUMN = None

OUT_DIR = BASE_DIR / "avito_phones_playwright"
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "phones"; IMG_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUT_DIR / "debug"; DEBUG_DIR.mkdir(exist_ok=True)

OUT_JSON = OUT_DIR / "phones_map.json"
PENDING_JSON = OUT_DIR / "pending_review.json"   # очередь ссылок «на проверке/лимит»
PENDING_RECHECK = True
PENDING_RECHECK_LIMIT = 150
PENDING_RECHECK_WAIT = (3.0, 6.0)                # пауза между повторными проверками

# ⬇ добавлено: ожидание при модалке лимита контактов
QUOTA_WAIT = (8.0, 14.0)

SAVE_DATA_URI = True
HEADLESS = False

TEST_TOTAL = 400
CONCURRENCY = 3

CLICK_DELAY = 8
NAV_TIMEOUT = 90_000

USE_PROXY = False
PROXY_HOST = "mproxy.site"
PROXY_PORT = 17518
PROXY_LOGIN = "YT4aBK"
PROXY_PASSWORD = "nUg2UTut9UMU"

PAGE_DELAY_BETWEEN_BATCHES = (2.0, 4.0)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# === ЧЕЛОВЕЧНОСТЬ / АНТИБАН-ПОВЕДЕНИЕ ===
HUMAN = {
    "pre_page_warmup_scrolls": (1, 3),
    "scroll_step_px": (250, 900),
    "scroll_pause_s": (0.15, 0.6),
    "hover_pause_s": (0.12, 0.35),
    "pre_click_pause_s": (0.08, 0.22),
    "post_click_pause_s": (0.10, 0.25),
    "mouse_wiggle_px": (4, 12),
    "mouse_wiggle_steps": (2, 5),
    "between_actions_pause": (0.08, 0.25),
    "click_delay_jitter": (CLICK_DELAY * 0.8, CLICK_DELAY * 1.2),
    "randomize_selectors": True,
}
