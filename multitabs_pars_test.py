import os
import json
import re
import time
import random
import atexit
import signal
from base64 import b64decode
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
from PIL import Image
from playwright.sync_api import (
    sync_playwright,
    Page,
    TimeoutError as PWTimeoutError,
    Error as PWError,
)

# ========== НАСТРОЙКИ ==========

INPUT_FILE = Path("АВТОСАЛОН 11.11.xlsx")
INPUT_SHEET = None
URL_COLUMN = None

OUT_DIR = Path("avito_phones_playwright")
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "phones"
IMG_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

OUT_JSON = OUT_DIR / "phones_map.json"
PENDING_JSON = OUT_DIR / "pending_review.json"   # очередь ссылок «на проверке»
PENDING_RECHECK = True                            # делать повторный лёгкий проход в конце
PENDING_RECHECK_LIMIT = 150                       # максимум ссылок для повтора за запуск
PENDING_RECHECK_WAIT = (3.0, 6.0)                 # пауза между повторными проверками

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

# ========== ХЕЛПЕРЫ: человечность ==========

def human_sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))

def human_pause_jitter():
    human_sleep(*HUMAN["between_actions_pause"])

def human_scroll_jitter(page: Page, count: int | None = None):
    if count is None:
        count = random.randint(*HUMAN["pre_page_warmup_scrolls"])
    try:
        height = page.evaluate("() => document.body.scrollHeight") or 3000
        for _ in range(count):
            step = random.randint(*HUMAN["scroll_step_px"])
            direction = 1 if random.random() > 0.25 else -1
            y = max(0, min(height, page.evaluate("() => window.scrollY") + step * direction))
            page.evaluate("y => window.scrollTo({top: y, behavior: 'smooth'})", y)
            human_sleep(*HUMAN["scroll_pause_s"])
    except Exception:
        pass

def human_wiggle_mouse(page: Page, x: float, y: float):
    steps = random.randint(*HUMAN["mouse_wiggle_steps"])
    amp = random.randint(*HUMAN["mouse_wiggle_px"])
    for _ in range(steps):
        dx = random.randint(-amp, amp)
        dy = random.randint(-amp, amp)
        try:
            page.mouse.move(x + dx, y + dy)
        except Exception:
            pass
        human_pause_jitter()

def human_hover(page: Page, loc):
    try:
        box = loc.bounding_box()
        if not box:
            return
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        page.mouse.move(cx, cy)
        human_wiggle_mouse(page, cx, cy)
        human_sleep(*HUMAN["hover_pause_s"])
    except Exception:
        pass

# ========== ХЕЛПЕРЫ: DOM/страницы ==========

def safe_get_content(page: Page) -> str:
    try:
        return page.content()
    except PWError:
        time.sleep(0.8)
        try:
            return page.content()
        except PWError:
            return ""

def is_captcha_or_block(page: Page) -> bool:
    try:
        url = page.url.lower()
    except PWError:
        url = ""
    html = safe_get_content(page).lower()
    if "captcha" in url or "firewall" in url:
        return True
    if "доступ с вашего ip-адреса временно ограничен" in html:
        return True
    return False

def close_city_or_cookie_modals(page: Page):
    selectors = [
        "button[aria-label='Закрыть']",
        "button[data-marker='modal-close']",
        "button[class*='close']",
        "button:has-text('Понятно')",
        "button:has-text('Хорошо')",
        "button:has-text('Согласен')",
        "button:has-text('Принять')",
    ]
    for sel in selectors:
        try:
            for loc in page.locator(sel).all():
                if loc.is_visible():
                    human_hover(page, loc)
                    loc.click()
                    human_sleep(0.25, 0.7)
        except Exception:
            continue

def close_login_modal_if_exists(page: Page) -> bool:
    selectors_modal = [
        "[data-marker='login-form']",
        "[data-marker='registration-form']",
        "div[class*='modal'][class*='auth']",
        "div[class*='modal'] form[action*='login']",
    ]
    for sel in selectors_modal:
        try:
            modals = page.locator(sel)
            for m in modals.all():
                if not m.is_visible():
                    continue
                for btn_sel in [
                    "button[aria-label='Закрыть']",
                    "button[data-marker='modal-close']",
                    "button[class*='close']",
                    "button[type='button']",
                ]:
                    btn = m.locator(btn_sel).first
                    if btn and btn.is_enabled():
                        try:
                            human_hover(page, btn)
                            human_sleep(*HUMAN["pre_click_pause_s"])
                            btn.click()
                            human_sleep(*HUMAN["post_click_pause_s"])
                            print("🔒 Модалка авторизации закрыта, объявление пропущено.")
                            return True
                        except Exception:
                            pass
                print("🔒 Модалка авторизации не закрывается — объявление пропускаем.")
                return True
        except PWError:
            continue
    return False

def save_phone_png_from_data_uri(data_uri: str, file_stem: str) -> str | None:
    try:
        _, b64_data = data_uri.split(",", 1)
        raw = b64decode(b64_data)
        image = Image.open(BytesIO(raw)).convert("RGB")
        file_name = f"{file_stem}.png"
        out_path = IMG_DIR / file_name
        image.save(out_path)
        print(f"💾 PNG сохранён: {out_path}")
        return str(out_path)
    except Exception as e:
        print(f"⚠️ Ошибка при сохранении PNG: {e}")
        return None

def get_avito_id_from_url(url: str) -> str:
    m = re.search(r'(\d{7,})', url)
    return m.group(1) if m else str(int(time.time()))

# =========================
# locator-клики (быстрее и стабильнее)
# =========================
def try_click_selector(page: Page, sel: str, timeout: int = 2000) -> bool:
    loc = page.locator(sel).first
    try:
        loc.wait_for(state="visible", timeout=timeout)
        human_sleep(*HUMAN["pre_click_pause_s"])
        loc.click()
        human_sleep(*HUMAN["post_click_pause_s"])
        return True
    except Exception:
        return False

# =========================
# кнопка «Показать телефон» через locator
# =========================
def click_show_phone_on_ad(page: Page) -> bool:
    human_scroll_jitter(page)

    for anchor in [
        "[data-marker='seller-info']",
        "[data-marker='item-sidebar']",
        "section:has(button[data-marker*='phone'])",
        "section:has(button:has-text('Показать'))",
    ]:
        try:
            page.locator(anchor).first.scroll_into_view_if_needed()
            human_sleep(*HUMAN["scroll_pause_s"])
            break
        except Exception:
            pass

    selector_groups = [
        [
            "button[data-marker='item-phone-button']",
            "button[data-marker='phone-button/number']",
            "button[data-marker*='phone-button']",
        ],
        [
            "button:has-text('Показать телефон')",
            "button:has-text('Показать номер')",
            "a:has-text('Показать телефон')",
            "a:has-text('Показать номер')",
        ],
        [
            "button[aria-label*='Показать телефон']",
            "button[aria-label*='Показать номер']",
        ],
        [
            "[data-marker*='phone'] button",
            "[data-marker*='contacts'] button",
        ],
    ]

    if HUMAN["randomize_selectors"]:
        random.shuffle(selector_groups)
        for g in selector_groups:
            random.shuffle(g)

    for group in selector_groups:
        for sel in group:
            if try_click_selector(page, sel):
                print("📞 Нажали 'Показать телефон'.")
                return True

    if try_click_selector(page, "footer:has(button) button"):
        print("📞 Нажали кнопку в липком футере.")
        return True

    print("⚠️ Кнопка 'Показать телефон' не найдена.")
    return False

# =========================
# картинка телефона через locator
# =========================
def extract_phone_data_uri_on_ad(page: Page) -> str | None:
    loc = page.locator("img[data-marker='phone-image']").first
    try:
        loc.wait_for(state="visible", timeout=1500)
    except Exception:
        print("⚠️ Картинка с номером не найдена.")
        return None
    try:
        src = loc.get_attribute("src") or ""
    except PWError:
        return None
    if not src.startswith("data:image"):
        print(f"⚠️ src не data:image, а: {src[:60]}...")
        return None
    return src

# ========== Парсинг входных ссылок из Excel/CSV ==========

def read_urls_from_excel_or_csv(
    path: Path,
    sheet: str | int | None = None,
    url_column: str | None = None
) -> list[str]:
    url_re = re.compile(r'https?://(?:www\.)?avito\.ru/[^\s"]+')
    urls: list[str] = []

    if path.suffix.lower() in {".xlsx", ".xls"}:
        xls = pd.ExcelFile(path)
        sheets = [sheet] if sheet is not None else xls.sheet_names
        for sh in sheets:
            df = xls.parse(sh, dtype=str)
            if url_column and url_column in df.columns:
                col = df[url_column].dropna().astype(str)
                urls.extend(col.tolist())
            else:
                for col in df.columns:
                    s = df[col].dropna().astype(str)
                    for val in s:
                        urls.extend(url_re.findall(val))
    elif path.suffix.lower() in {".csv", ".txt"}:
        df = pd.read_csv(path, dtype=str, sep=None, engine="python")
        if url_column and url_column in df.columns:
            col = df[url_column].dropna().astype(str)
            urls.extend(col.tolist())
        else:
            for col in df.columns:
                s = df[col].dropna().astype(str)
                for val in s:
                    urls.extend(url_re.findall(val))
    else:
        raise ValueError("Поддерживаются .xlsx/.xls/.csv/.txt")

    cleaned = []
    seen = set()
    for u in urls:
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin("https://www.avito.ru", u)
        u = u.split("#", 1)[0]
        u = u.split("?", 1)[0]
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned

# === Безопасное сохранение и восстановление прогресса (Windows-friendly) ===

def atomic_write_json(path: Path, data):
    """
    Надёжная запись на Windows:
    - уникальный tmp-файл;
    - до 10 ретраев os.replace при PermissionError;
    - при постоянной блокировке — безопасный fallback прямой записью.
    """
    tmp = path.with_suffix(path.suffix + f".tmp_{int(time.time()*1000)}_{random.randint(1000,9999)}")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    attempts = 10
    delay = 0.1
    for _ in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 1.7, 1.0)
        except Exception:
            time.sleep(delay)
            delay = min(delay * 1.7, 1.0)
    # Fallback
    try:
        path.write_text(payload, encoding="utf-8")
    except Exception as e:
        print(f"❗ Критическая ошибка записи прогресса: {e}")

def load_progress(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ Не удалось прочитать существующий прогресс: {e}")
    return {}

def load_pending(path: Path) -> list[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [u for u in data if isinstance(u, str)]
        except Exception:
            pass
    return []

def save_pending(path: Path, urls: list[str]):
    unique = sorted(set(urls))
    atomic_write_json(path, unique)

def dump_debug(page: Page, url: str):
    try:
        ad_id = get_avito_id_from_url(url)
        png_path = DEBUG_DIR / f"{ad_id}.png"
        html_path = DEBUG_DIR / f"{ad_id}.html"
        page.screenshot(path=str(png_path), full_page=True)
        html = safe_get_content(page)
        html_path.write_text(html, encoding="utf-8")
        print(f"🪪 Debug сохранён: {png_path.name}, {html_path.name}")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить debug: {e}")

# --------- Классификация статуса объявления ---------

NO_CALLS_MARKERS = [
    "без звонков",
    "пользователь предпочитает сообщения",
]

# может стать доступным позже
MODERATION_MARKERS = [
    "оно ещё на проверке",
    "объявление на проверке",
    "объявление ещё на проверке",
]

# навсегда недоступно
UNAVAILABLE_MARKERS = [
    "объявление не посмотреть",
    "объявление снято с продажи",
    "объявление удалено",
    "объявление закрыто",
    "объявление больше не доступно",
]

def classify_ad_status(page: Page) -> str:
    """
    Возвращает: 'ok' | 'no_calls' | 'on_review' | 'unavailable' | 'blocked'
    """
    if is_captcha_or_block(page):
        return "blocked"

    html = safe_get_content(page).lower()

    if any(m in html for m in MODERATION_MARKERS):
        return "on_review"

    if any(m in html for m in UNAVAILABLE_MARKERS):
        return "unavailable"

    if any(m in html for m in NO_CALLS_MARKERS):
        return "no_calls"

    try:
        if page.locator("text=Без звонков").first.is_visible():
            return "no_calls"
    except Exception:
        pass
    return "ok"

# ========== Пул вкладок с переиспользованием ==========

def make_page_pool(context, size: int) -> list[Page]:
    return [context.new_page() for _ in range(size)]

def process_with_pool(context, urls, on_result):
    pages = make_page_pool(context, CONCURRENCY)
    pending_queue = load_pending(PENDING_JSON)

    try:
        it = iter(urls)
        while True:
            batch = []
            # Навигация на следующих N ссылок на уже открытых вкладках
            for p in pages:
                try:
                    url = next(it)
                except StopIteration:
                    # сохраним очередь перед выходом
                    save_pending(PENDING_JSON, pending_queue)
                    return
                batch.append((url, p))
                try:
                    # быстрее: domcontentloaded
                    p.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                except PWTimeoutError:
                    print(f"⚠️ Таймаут: {url}")
                    continue
                human_sleep(0.2, 0.6)
                human_scroll_jitter(p, count=random.randint(1, 2))

            # Классификация/модалки/клик
            for url, p in batch:
                status = classify_ad_status(p)
                if status == "blocked":
                    print(f"🚫 Капча/блок на {url}")
                    continue
                if status == "on_review":
                    print(f"⏳ На проверке: {url}")
                    on_result(url, "__SKIP_ON_REVIEW__")
                    pending_queue.append(url)
                    continue
                if status == "unavailable":
                    print(f"⏭️ Недоступно/закрыто: {url}")
                    on_result(url, "__SKIP_UNAVAILABLE__")
                    continue
                if status == "no_calls":
                    print(f"⏭️ Без звонков: {url}")
                    on_result(url, "__SKIP_NO_CALLS__")
                    continue

                close_city_or_cookie_modals(p)
                if not click_show_phone_on_ad(p):
                    # повторная быстрая проверка статуса
                    status2 = classify_ad_status(p)
                    if status2 == "on_review":
                        on_result(url, "__SKIP_ON_REVIEW__")
                        pending_queue.append(url)
                    elif status2 == "unavailable":
                        on_result(url, "__SKIP_UNAVAILABLE__")
                    elif status2 == "no_calls":
                        on_result(url, "__SKIP_NO_CALLS__")
                    else:
                        dump_debug(p, url)

            # Ждём отрисовку картинок телефонов
            human_sleep(*HUMAN["click_delay_jitter"])

            # Сбор картинок
            for url, p in batch:
                if close_login_modal_if_exists(p) or is_captcha_or_block(p):
                    continue
                data_uri = extract_phone_data_uri_on_ad(p)
                if not data_uri:
                    continue

                if SAVE_DATA_URI:
                    value = data_uri
                else:
                    avito_id = get_avito_id_from_url(url)
                    out_path = save_phone_png_from_data_uri(data_uri, avito_id)
                    if not out_path:
                        continue
                    value = out_path

                on_result(url, value)
                print(f"✅ {url} -> {'[data:image...]' if SAVE_DATA_URI else value}")

            # Пауза между партиями (антибан)
            human_sleep(*PAGE_DELAY_BETWEEN_BATCHES)

    finally:
        save_pending(PENDING_JSON, pending_queue)
        for p in pages:
            try:
                p.close()
            except Exception:
                pass

# ========== Одноразовая перепроверка «на модерации» ==========

def recheck_pending_once(context, on_result):
    if not PENDING_RECHECK:
        return
    pend = load_pending(PENDING_JSON)
    if not pend:
        return

    pend = pend[:PENDING_RECHECK_LIMIT]
    print(f"\n🔁 Повторная проверка ссылок на модерации: {len(pend)}")

    page = context.new_page()
    still_pending = []

    for url in pend:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception:
            still_pending.append(url)
            continue

        st = classify_ad_status(page)
        if st == "ok":
            close_city_or_cookie_modals(page)
            if click_show_phone_on_ad(page):
                time.sleep(random.uniform(*HUMAN["click_delay_jitter"]))
                data_uri = extract_phone_data_uri_on_ad(page)
                if data_uri:
                    if SAVE_DATA_URI:
                        on_result(url, data_uri)
                    else:
                        out = save_phone_png_from_data_uri(data_uri, get_avito_id_from_url(url))
                        if out:
                            on_result(url, out)
                    print(f"✅ (повтор) {url}")
                else:
                    still_pending.append(url)
            else:
                st2 = classify_ad_status(page)
                if st2 == "no_calls":
                    on_result(url, "__SKIP_NO_CALLS__")
                elif st2 == "on_review":
                    still_pending.append(url)
                else:
                    on_result(url, "__SKIP_UNAVAILABLE__")
        elif st == "on_review":
            still_pending.append(url)
        elif st == "no_calls":
            on_result(url, "__SKIP_NO_CALLS__")
        else:
            on_result(url, "__SKIP_UNAVAILABLE__")

        time.sleep(random.uniform(*PENDING_RECHECK_WAIT))

    try:
        page.close()
    except Exception:
        pass
    save_pending(PENDING_JSON, still_pending)
    print(f"ℹ️ В очереди осталось: {len(still_pending)}")

# ========== ОСНОВНОЙ СЦЕНАРИЙ ==========

def main():
    urls = read_urls_from_excel_or_csv(INPUT_FILE, INPUT_SHEET, URL_COLUMN)
    urls = urls[:TEST_TOTAL]

    phones_map: dict[str, str] = load_progress(OUT_JSON)
    already_done = set(phones_map.keys())
    urls = [u for u in urls if u not in already_done]

    print(f"🔎 Новых ссылок к обработке: {len(urls)} (уже сохранено ранее: {len(already_done)})")
    if not urls:
        print(f"ℹ️ Нечего делать. Прогресс в {OUT_JSON}: {len(phones_map)} записей.")
        return

    def flush_progress():
        try:
            atomic_write_json(OUT_JSON, phones_map)
        except Exception as e:
            print(f"❗ Ошибка записи прогресса: {e}")

    atexit.register(flush_progress)
    try:
        signal.signal(signal.SIGINT, lambda *a: (flush_progress(), exit(1)))
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, lambda *a: (flush_progress(), exit(1)))
    except Exception:
        pass

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        }
        if USE_PROXY:
            launch_kwargs["proxy"] = {
                "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
                "username": PROXY_LOGIN,
                "password": PROXY_PASSWORD,
            }

        browser = p.chromium.launch(**launch_kwargs)

        # Чуть рандомизируем размер вьюпорта
        vp_w = random.randint(1200, 1368)
        vp_h = random.randint(760, 900)

        context = browser.new_context(
            viewport={"width": vp_w, "height": vp_h},
            user_agent=UA,
        )
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        context.set_default_timeout(NAV_TIMEOUT)

        # Ручной логин на первой ссылке
        page = context.new_page()
        first_url = urls[0]
        try:
            page.goto(first_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PWTimeoutError:
            pass

        print("\n🔑 Твои действия:")
        print("   • если есть капча — реши;")
        print("   • залогинься в Авито;")
        print("   • оставь открытую страницу объявления.")
        input("👉 Готов? Нажми Enter в консоли.\n")

        if is_captcha_or_block(page):
            print("❌ Всё ещё капча/блок — выходим.")
            browser.close()
            flush_progress()
            return

        try:
            page.close()
        except Exception:
            pass

        def on_result(url: str, value: str):
            # value — либо data:image..., либо путь к PNG, либо __SKIP_...
            phones_map[url] = value
            atomic_write_json(OUT_JSON, phones_map)  # надёжно и сразу

        # Основной проход по ссылкам с пулом вкладок
        try:
            process_with_pool(context, urls, on_result)
        except KeyboardInterrupt:
            print("⏹ Остановлено пользователем.")
            flush_progress()

        # Одноразовая перепроверка «на модерации»
        recheck_pending_once(context, on_result)

        browser.close()
        flush_progress()

        print(f"\n✅ Готово. В {OUT_JSON} сейчас {len(phones_map)} записей.")
        if not SAVE_DATA_URI:
            print(f"📂 PNG лежат в {IMG_DIR}")

if __name__ == "__main__":
    main()
