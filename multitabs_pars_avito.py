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
from itertools import islice

import pandas as pd
from PIL import Image
from playwright.sync_api import (
    sync_playwright,
    Page,
    TimeoutError as PWTimeoutError,
    Error as PWError,
)

# ========== НАСТРОЙКИ ==========

INPUT_FILE = Path("АВТОСАЛОН 12.11 1000.xlsx")
INPUT_SHEET = None
URL_COLUMN = None

OUT_DIR = Path("avito_phones_playwright")
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "phones"
IMG_DIR.mkdir(exist_ok=True)
DEBUG_DIR = OUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

OUT_JSON = OUT_DIR / "phones_map.json"
SAVE_DATA_URI = True
HEADLESS = False

# Тестовый прогон: 6 объявлений, по 3 во вкладках
TEST_TOTAL = 550
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
    "pre_page_warmup_scrolls": (1, 3),   # сколько «ленивых» скроллов при открытии карточки
    "scroll_step_px": (250, 900),
    "scroll_pause_s": (0.15, 0.6),
    "hover_pause_s": (0.12, 0.35),
    "pre_click_pause_s": (0.08, 0.22),   # пауза перед кликом (джиттер)
    "post_click_pause_s": (0.10, 0.25),
    "mouse_wiggle_px": (4, 12),          # амплитуда «покачивания» курсора
    "mouse_wiggle_steps": (2, 5),
    "between_actions_pause": (0.08, 0.25),
    "click_delay_jitter": (CLICK_DELAY * 0.8, CLICK_DELAY * 1.2),
    "randomize_selectors": True,
}


# ========== ХЕЛПЕРЫ ==========

def human_sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))


def human_pause_jitter():
    human_sleep(*HUMAN["between_actions_pause"])


def human_scroll_jitter(page: Page, count: int | None = None):
    """Небольшие скроллы вверх/вниз с паузами."""
    if count is None:
        count = random.randint(*HUMAN["pre_page_warmup_scrolls"])
    height = page.evaluate("() => document.body.scrollHeight") or 3000
    for i in range(count):
        step = random.randint(*HUMAN["scroll_step_px"])
        direction = 1 if random.random() > 0.25 else -1
        y = max(0, min(height, page.evaluate("() => window.scrollY") + step * direction))
        page.evaluate("y => window.scrollTo({top: y, behavior: 'smooth'})", y)
        human_sleep(*HUMAN["scroll_pause_s"])


def human_wiggle_mouse(page: Page, x: float, y: float):
    """Микродвижения курсора, чтобы имитировать человека."""
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


def human_hover(page: Page, el):
    """Наводим курсор на элемент с лёгким покачиванием."""
    try:
        box = el.bounding_box()
        if not box:
            return
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        page.mouse.move(cx, cy)
        human_wiggle_mouse(page, cx, cy)
        human_sleep(*HUMAN["hover_pause_s"])
    except Exception:
        pass


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
            for b in page.query_selector_all(sel):
                if b.is_visible():
                    human_hover(page, b)
                    b.click()
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
            modals = page.query_selector_all(sel)
        except PWError:
            continue

        for m in modals:
            if not m.is_visible():
                continue

            for btn_sel in [
                "button[aria-label='Закрыть']",
                "button[data-marker='modal-close']",
                "button[class*='close']",
                "button[type='button']",
            ]:
                btn = m.query_selector(btn_sel)
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

    return False


def save_phone_png_from_data_uri(data_uri: str, file_stem: str) -> str | None:
    try:
        header, b64_data = data_uri.split(",", 1)
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


def try_click(page: Page, el) -> bool:
    """Наводим, ждём, кликаем, если нужно — кликаем через JS."""
    try:
        el.scroll_into_view_if_needed()
    except Exception:
        pass
    human_hover(page, el)
    human_sleep(*HUMAN["pre_click_pause_s"])
    try:
        el.click()
        human_sleep(*HUMAN["post_click_pause_s"])
        return True
    except Exception:
        try:
            box = el.bounding_box() or {}
            if box:
                # небольшой сдвиг курсора в «реальную» точку клика
                page.mouse.move(box.get("x", 0) + 6, box.get("y", 0) + 6)
                human_sleep(*HUMAN["pre_click_pause_s"])
            page.evaluate("(e)=>e.click()", el)
            human_sleep(*HUMAN["post_click_pause_s"])
            return True
        except Exception:
            return False


def click_show_phone_on_ad(page: Page) -> bool:
    """
    Ищем и нажимаем кнопку "Показать телефон/номер" в разных вариантах вёрстки.
    Возвращаем True при успехе.
    """
    # Разминка: немного прокруток
    human_scroll_jitter(page)

    # Иногда кнопка в блоке контактов — подскроллим к нему
    for anchor in [
        "[data-marker='seller-info']",
        "[data-marker='item-sidebar']",
        "section:has(button[data-marker*='phone'])",
        "section:has(button:has-text('Показать'))",
    ]:
        try:
            a = page.query_selector(anchor)
            if a:
                a.scroll_into_view_if_needed()
                human_sleep(*HUMAN["scroll_pause_s"])
                break
        except Exception:
            pass

    # Наборы селекторов на случай разных вёрсток
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

    # Случайно перемешиваем порядок обхода селекторов — меньше паттернов
    if HUMAN["randomize_selectors"]:
        random.shuffle(selector_groups)
        for g in selector_groups:
            random.shuffle(g)

    # Ждём появления чего-то «похожего на кнопку» недолго
    try:
        page.wait_for_selector("button", timeout=2000)
    except Exception:
        pass

    for group in selector_groups:
        for sel in group:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible() and el.is_enabled():
                    if try_click(page, el):
                        print("📞 Нажали 'Показать телефон'.")
                        return True
            except Exception:
                continue

    # Иногда кнопка в «липком» футере карточки
    try:
        sticky = page.query_selector("footer:has(button)")
        if sticky:
            btn = sticky.query_selector("button")
            if btn and btn.is_visible() and btn.is_enabled():
                if try_click(page, btn):
                    print("📞 Нажали кнопку в липком футере.")
                    return True
    except Exception:
        pass

    print("⚠️ Кнопка 'Показать телефон' не найдена.")
    return False


def extract_phone_data_uri_on_ad(page: Page) -> str | None:
    try:
        img = page.query_selector("img[data-marker='phone-image']")
    except PWError:
        img = None

    if not img or not img.is_visible():
        print("⚠️ Картинка с номером не найдена.")
        return None

    src = img.get_attribute("src") or ""
    if not src.startswith("data:image"):
        print(f"⚠️ src не data:image, а: {src[:60]}...")
        return None
    return src


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


def batched(iterable, n):
    it = iter(iterable)
    while True:
        batch = list(islice(it, n))
        if not batch:
            return
        yield batch


# === Безопасное сохранение и восстановление прогресса ===

def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_progress(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ Не удалось прочитать существующий прогресс: {e}")
    return {}


def dump_debug(page: Page, url: str):
    """Сохраняем скрин и HTML, если кнопка не нашлась — для диагностики верстки."""
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


def process_batch(context, batch_urls, on_result):
    pages: list[tuple[str, Page]] = []
    try:
        # 1) Открыли вкладки и перешли по URL
        for url in batch_urls:
            p = context.new_page()
            pages.append((url, p))
            try:
                p.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
            except PWTimeoutError:
                print(f"⚠️ Навигация по таймауту: {url}")
            human_sleep(0.2, 0.6)
            # Рандомные лёгкие скроллы после загрузки
            human_scroll_jitter(p, count=random.randint(1, 2))

        # 2) Обработка модалок + попытка клика по кнопке
        for url, p in pages:
            if is_captcha_or_block(p):
                print(f"🚫 Капча/блок на {url}")
                continue
            close_city_or_cookie_modals(p)
            if not click_show_phone_on_ad(p):
                dump_debug(p, url)

        # 3) Ждём отрисовку картинок телефонов (с джиттером)
        human_sleep(*HUMAN["click_delay_jitter"])

        # 4) Сбор картинок телефонов
        for url, p in pages:
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

    finally:
        # 5) Закрываем вкладки (с разными маленькими задержками)
        for _, p in pages:
            try:
                human_pause_jitter()
                p.close()
            except Exception:
                pass


# ========== ОСНОВНОЙ СЦЕНАРИЙ ==========

def main():
    urls = read_urls_from_excel_or_csv(INPUT_FILE, INPUT_SHEET, URL_COLUMN)

    # ТЕСТ: берём только первые 6 ссылок
    urls = urls[:TEST_TOTAL]

    # Поднимаем прогресс и пропускаем обработанные
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

        # Ручной логин на первой ссылке тестового набора
        page = context.new_page()
        first_url = urls[0]
        try:
            page.goto(first_url, wait_until="load", timeout=NAV_TIMEOUT)
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
            phones_map[url] = value
            atomic_write_json(OUT_JSON, phones_map)

        # Обработка пакетами по 3 вкладки (ровно две пачки на наш TEST_TOTAL=6)
        for batch_urls in batched(urls, CONCURRENCY):
            try:
                process_batch(context, batch_urls, on_result)
            except KeyboardInterrupt:
                print("⏹ Остановлено пользователем.")
                flush_progress()
                break
            except Exception as e:
                print(f"⚠️ Ошибка при обработке пакета: {e}")
                flush_progress()
            human_sleep(*PAGE_DELAY_BETWEEN_BATCHES)

        browser.close()
        flush_progress()

        print(f"\n✅ Готово. В {OUT_JSON} сейчас {len(phones_map)} записей.")
        if not SAVE_DATA_URI:
            print(f"📂 PNG лежат в {IMG_DIR}")


if __name__ == "__main__":
    main()






# import json
# import re
# import time
# import random
# import atexit
# import signal
# from base64 import b64decode
# from io import BytesIO
# from pathlib import Path
# from urllib.parse import urljoin
# from itertools import islice
#
# import pandas as pd
# from PIL import Image
# from playwright.sync_api import (
#     sync_playwright,
#     Page,
#     TimeoutError as PWTimeoutError,
#     Error as PWError,
# )
#
# # ========== НАСТРОЙКИ ==========
#
# INPUT_FILE = Path("АВТОСАЛОН 09.11 2000.xlsx")
# INPUT_SHEET = None
# URL_COLUMN = None
#
# OUT_DIR = Path("avito_phones_playwright")
# OUT_DIR.mkdir(exist_ok=True)
# IMG_DIR = OUT_DIR / "phones"
# IMG_DIR.mkdir(exist_ok=True)
# DEBUG_DIR = OUT_DIR / "debug"
# DEBUG_DIR.mkdir(exist_ok=True)
#
# OUT_JSON = OUT_DIR / "phones_map.json"
# SAVE_DATA_URI = True
# HEADLESS = False
#
# # Тестовый прогон: 6 объявлений, по 3 во вкладках
# TEST_TOTAL = 6
# CONCURRENCY = 3
#
# CLICK_DELAY = 8
# NAV_TIMEOUT = 90_000
#
# USE_PROXY = False
# PROXY_HOST = "mproxy.site"
# PROXY_PORT = 17518
# PROXY_LOGIN = "YT4aBK"
# PROXY_PASSWORD = "nUg2UTut9UMU"
#
# PAGE_DELAY_BETWEEN_BATCHES = (2.0, 4.0)
# UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#       "AppleWebKit/537.36 (KHTML, like Gecko) "
#       "Chrome/120.0.0.0 Safari/537.36")
#
#
# # ========== ХЕЛПЕРЫ ==========
#
# def human_sleep(a: float, b: float):
#     time.sleep(random.uniform(a, b))
#
#
# def safe_get_content(page: Page) -> str:
#     try:
#         return page.content()
#     except PWError:
#         time.sleep(0.8)
#         try:
#             return page.content()
#         except PWError:
#             return ""
#
#
# def is_captcha_or_block(page: Page) -> bool:
#     try:
#         url = page.url.lower()
#     except PWError:
#         url = ""
#     html = safe_get_content(page).lower()
#     if "captcha" in url or "firewall" in url:
#         return True
#     if "доступ с вашего ip-адреса временно ограничен" in html:
#         return True
#     return False
#
#
# def close_city_or_cookie_modals(page: Page):
#     selectors = [
#         "button[aria-label='Закрыть']",
#         "button[data-marker='modal-close']",
#         "button[class*='close']",
#         "button:has-text('Понятно')",
#         "button:has-text('Хорошо')",
#         "button:has-text('Согласен')",
#         "button:has-text('Принять')",
#     ]
#     for sel in selectors:
#         try:
#             for b in page.query_selector_all(sel):
#                 if b.is_visible():
#                     b.click()
#                     human_sleep(0.25, 0.7)
#         except Exception:
#             continue
#
#
# def close_login_modal_if_exists(page: Page) -> bool:
#     selectors_modal = [
#         "[data-marker='login-form']",
#         "[data-marker='registration-form']",
#         "div[class*='modal'][class*='auth']",
#         "div[class*='modal'] form[action*='login']",
#     ]
#     for sel in selectors_modal:
#         try:
#             modals = page.query_selector_all(sel)
#         except PWError:
#             continue
#
#         for m in modals:
#             if not m.is_visible():
#                 continue
#
#             for btn_sel in [
#                 "button[aria-label='Закрыть']",
#                 "button[data-marker='modal-close']",
#                 "button[class*='close']",
#                 "button[type='button']",
#             ]:
#                 btn = m.query_selector(btn_sel)
#                 if btn and btn.is_enabled():
#                     try:
#                         btn.click()
#                         human_sleep(0.3, 0.6)
#                         print("🔒 Модалка авторизации закрыта, объявление пропущено.")
#                         return True
#                     except Exception:
#                         pass
#
#             print("🔒 Модалка авторизации не закрывается — объявление пропускаем.")
#             return True
#
#     return False
#
#
# def save_phone_png_from_data_uri(data_uri: str, file_stem: str) -> str | None:
#     try:
#         header, b64_data = data_uri.split(",", 1)
#         raw = b64decode(b64_data)
#         image = Image.open(BytesIO(raw)).convert("RGB")
#         file_name = f"{file_stem}.png"
#         out_path = IMG_DIR / file_name
#         image.save(out_path)
#         print(f"💾 PNG сохранён: {out_path}")
#         return str(out_path)
#     except Exception as e:
#         print(f"⚠️ Ошибка при сохранении PNG: {e}")
#         return None
#
#
# def get_avito_id_from_url(url: str) -> str:
#     m = re.search(r'(\d{7,})', url)
#     return m.group(1) if m else str(int(time.time()))
#
#
# def try_click(page: Page, el) -> bool:
#     """Пробуем обычный click, если не вышло — кликаем через JS."""
#     try:
#         el.scroll_into_view_if_needed()
#         human_sleep(0.15, 0.4)
#         el.click()
#         return True
#     except Exception:
#         try:
#             box = el.bounding_box() or {}
#             if box:
#                 page.mouse.move(box.get("x", 0) + 5, box.get("y", 0) + 5)
#                 human_sleep(0.1, 0.2)
#             page.evaluate("(e)=>e.click()", el)
#             return True
#         except Exception:
#             return False
#
#
# def click_show_phone_on_ad(page: Page) -> bool:
#     """
#     Ищем и нажимаем кнопку "Показать телефон/номер" в разных вариантах вёрстки.
#     Возвращаем True при успехе.
#     """
#     # Иногда кнопка в блоке контактов — подскроллим к нему
#     for anchor in [
#         "[data-marker='seller-info']",
#         "[data-marker='item-sidebar']",
#         "section:has(button[data-marker*='phone'])",
#         "section:has(button:has-text('Показать'))",
#     ]:
#         try:
#             a = page.query_selector(anchor)
#             if a:
#                 a.scroll_into_view_if_needed()
#                 human_sleep(0.2, 0.4)
#                 break
#         except Exception:
#             pass
#
#     # Наборы селекторов на случай разных вёрсток
#     selector_groups = [
#         [
#             "button[data-marker='item-phone-button']",
#             "button[data-marker='phone-button/number']",
#             "button[data-marker*='phone-button']",
#         ],
#         [
#             "button:has-text('Показать телефон')",
#             "button:has-text('Показать номер')",
#             "a:has-text('Показать телефон')",
#             "a:has-text('Показать номер')",
#         ],
#         [
#             "button[aria-label*='Показать телефон']",
#             "button[aria-label*='Показать номер']",
#         ],
#         [
#             "[data-marker*='phone'] button",
#             "[data-marker*='contacts'] button",
#         ],
#     ]
#
#     # Попробуем дождаться появления чего-то «похожего на кнопку» недолго
#     try:
#         page.wait_for_selector("button", timeout=2000)
#     except Exception:
#         pass
#
#     for group in selector_groups:
#         for sel in group:
#             try:
#                 el = page.query_selector(sel)
#                 if el and el.is_visible() and el.is_enabled():
#                     if try_click(page, el):
#                         print("📞 Нажали 'Показать телефон'.")
#                         return True
#             except Exception:
#                 continue
#
#     # Иногда кнопка в «липком» футере карточки
#     try:
#         sticky = page.query_selector("footer:has(button)")
#         if sticky:
#             btn = sticky.query_selector("button")
#             if btn and btn.is_visible() and btn.is_enabled():
#                 if try_click(page, btn):
#                     print("📞 Нажали кнопку в липком футере.")
#                     return True
#     except Exception:
#         pass
#
#     print("⚠️ Кнопка 'Показать телефон' не найдена.")
#     return False
#
#
# def extract_phone_data_uri_on_ad(page: Page) -> str | None:
#     try:
#         img = page.query_selector("img[data-marker='phone-image']")
#     except PWError:
#         img = None
#
#     if not img or not img.is_visible():
#         print("⚠️ Картинка с номером не найдена.")
#         return None
#
#     src = img.get_attribute("src") or ""
#     if not src.startswith("data:image"):
#         print(f"⚠️ src не data:image, а: {src[:60]}...")
#         return None
#     return src
#
#
# def read_urls_from_excel_or_csv(
#     path: Path,
#     sheet: str | int | None = None,
#     url_column: str | None = None
# ) -> list[str]:
#     url_re = re.compile(r'https?://(?:www\.)?avito\.ru/[^\s"]+')
#     urls: list[str] = []
#
#     if path.suffix.lower() in {".xlsx", ".xls"}:
#         xls = pd.ExcelFile(path)
#         sheets = [sheet] if sheet is not None else xls.sheet_names
#         for sh in sheets:
#             df = xls.parse(sh, dtype=str)
#             if url_column and url_column in df.columns:
#                 col = df[url_column].dropna().astype(str)
#                 urls.extend(col.tolist())
#             else:
#                 for col in df.columns:
#                     s = df[col].dropna().astype(str)
#                     for val in s:
#                         urls.extend(url_re.findall(val))
#     elif path.suffix.lower() in {".csv", ".txt"}:
#         df = pd.read_csv(path, dtype=str, sep=None, engine="python")
#         if url_column and url_column in df.columns:
#             col = df[url_column].dropna().astype(str)
#             urls.extend(col.tolist())
#         else:
#             for col in df.columns:
#                 s = df[col].dropna().astype(str)
#                 for val in s:
#                     urls.extend(url_re.findall(val))
#     else:
#         raise ValueError("Поддерживаются .xlsx/.xls/.csv/.txt")
#
#     cleaned = []
#     seen = set()
#     for u in urls:
#         u = u.strip()
#         if not u.startswith("http"):
#             u = urljoin("https://www.avito.ru", u)
#         u = u.split("#", 1)[0]
#         u = u.split("?", 1)[0]
#         if u not in seen:
#             seen.add(u)
#             cleaned.append(u)
#     return cleaned
#
#
# def batched(iterable, n):
#     it = iter(iterable)
#     while True:
#         batch = list(islice(it, n))
#         if not batch:
#             return
#         yield batch
#
#
# # === Безопасное сохранение и восстановление прогресса ===
#
# def atomic_write_json(path: Path, data: dict):
#     tmp = path.with_suffix(path.suffix + ".tmp")
#     tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
#     tmp.replace(path)
#
#
# def load_progress(path: Path) -> dict[str, str]:
#     if path.exists():
#         try:
#             return json.loads(path.read_text(encoding="utf-8"))
#         except Exception as e:
#             print(f"⚠️ Не удалось прочитать существующий прогресс: {e}")
#     return {}
#
#
# def dump_debug(page: Page, url: str):
#     """Сохраняем скрин и HTML, если кнопка не нашлась — для диагностики верстки."""
#     try:
#         ad_id = get_avito_id_from_url(url)
#         png_path = DEBUG_DIR / f"{ad_id}.png"
#         html_path = DEBUG_DIR / f"{ad_id}.html"
#         page.screenshot(path=str(png_path), full_page=True)
#         html = safe_get_content(page)
#         html_path.write_text(html, encoding="utf-8")
#         print(f"🪪 Debug сохранён: {png_path.name}, {html_path.name}")
#     except Exception as e:
#         print(f"⚠️ Не удалось сохранить debug: {e}")
#
#
# def process_batch(context, batch_urls, on_result):
#     pages: list[tuple[str, Page]] = []
#     try:
#         # 1) Открыли вкладки и перешли по URL
#         for url in batch_urls:
#             p = context.new_page()
#             pages.append((url, p))
#             try:
#                 p.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
#             except PWTimeoutError:
#                 print(f"⚠️ Навигация по таймауту: {url}")
#             human_sleep(0.2, 0.6)
#
#         # 2) Обработка модалок + попытка клика по кнопке
#         for url, p in pages:
#             if is_captcha_or_block(p):
#                 print(f"🚫 Капча/блок на {url}")
#                 continue
#             close_city_or_cookie_modals(p)
#             if not click_show_phone_on_ad(p):
#                 dump_debug(p, url)
#
#         # 3) Ждём отрисовку картинок телефонов
#         time.sleep(CLICK_DELAY)
#
#         # 4) Сбор картинок телефонов
#         for url, p in pages:
#             if close_login_modal_if_exists(p) or is_captcha_or_block(p):
#                 continue
#             data_uri = extract_phone_data_uri_on_ad(p)
#             if not data_uri:
#                 continue
#
#             if SAVE_DATA_URI:
#                 value = data_uri
#             else:
#                 avito_id = get_avito_id_from_url(url)
#                 out_path = save_phone_png_from_data_uri(data_uri, avito_id)
#                 if not out_path:
#                     continue
#                 value = out_path
#
#             on_result(url, value)
#             print(f"✅ {url} -> {'[data:image...]' if SAVE_DATA_URI else value}")
#
#     finally:
#         # 5) Закрываем вкладки
#         for _, p in pages:
#             try:
#                 p.close()
#             except Exception:
#                 pass
#
#
# # ========== ОСНОВНОЙ СЦЕНАРИЙ ==========
#
# def main():
#     urls = read_urls_from_excel_or_csv(INPUT_FILE, INPUT_SHEET, URL_COLUMN)
#
#     # ТЕСТ: берём только первые 6 ссылок
#     urls = urls[:TEST_TOTAL]
#
#     # Поднимаем прогресс и пропускаем обработанные
#     phones_map: dict[str, str] = load_progress(OUT_JSON)
#     already_done = set(phones_map.keys())
#     urls = [u for u in urls if u not in already_done]
#
#     print(f"🔎 Новых ссылок к обработке: {len(urls)} (уже сохранено ранее: {len(already_done)})")
#     if not urls:
#         print(f"ℹ️ Нечего делать. Прогресс в {OUT_JSON}: {len(phones_map)} записей.")
#         return
#
#     def flush_progress():
#         try:
#             atomic_write_json(OUT_JSON, phones_map)
#         except Exception as e:
#             print(f"❗ Ошибка записи прогресса: {e}")
#
#     atexit.register(flush_progress)
#     try:
#         signal.signal(signal.SIGINT, lambda *a: (flush_progress(), exit(1)))
#     except Exception:
#         pass
#     try:
#         signal.signal(signal.SIGTERM, lambda *a: (flush_progress(), exit(1)))
#     except Exception:
#         pass
#
#     with sync_playwright() as p:
#         launch_kwargs = {
#             "headless": HEADLESS,
#             "args": [
#                 "--disable-blink-features=AutomationControlled",
#                 "--start-maximized",
#             ],
#         }
#         if USE_PROXY:
#             launch_kwargs["proxy"] = {
#                 "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
#                 "username": PROXY_LOGIN,
#                 "password": PROXY_PASSWORD,
#             }
#
#         browser = p.chromium.launch(**launch_kwargs)
#         context = browser.new_context(
#             viewport={"width": 1280, "height": 800},
#             user_agent=UA,
#         )
#         context.set_default_navigation_timeout(NAV_TIMEOUT)
#         context.set_default_timeout(NAV_TIMEOUT)
#
#         # Ручной логин на первой ссылке тестового набора
#         page = context.new_page()
#         first_url = urls[0]
#         try:
#             page.goto(first_url, wait_until="load", timeout=NAV_TIMEOUT)
#         except PWTimeoutError:
#             pass
#
#         print("\n🔑 Твои действия:")
#         print("   • если есть капча — реши;")
#         print("   • залогинься в Авито;")
#         print("   • оставь открытую страницу объявления.")
#         input("👉 Готов? Нажми Enter в консоли.\n")
#
#         if is_captcha_or_block(page):
#             print("❌ Всё ещё капча/блок — выходим.")
#             browser.close()
#             flush_progress()
#             return
#
#         try:
#             page.close()
#         except Exception:
#             pass
#
#         def on_result(url: str, value: str):
#             phones_map[url] = value
#             atomic_write_json(OUT_JSON, phones_map)
#
#         # Обработка пакетами по 3 вкладки (ровно две пачки на наш TEST_TOTAL=6)
#         for batch_urls in batched(urls, CONCURRENCY):
#             try:
#                 process_batch(context, batch_urls, on_result)
#             except KeyboardInterrupt:
#                 print("⏹ Остановлено пользователем.")
#                 flush_progress()
#                 break
#             except Exception as e:
#                 print(f"⚠️ Ошибка при обработке пакета: {e}")
#                 flush_progress()
#             human_sleep(*PAGE_DELAY_BETWEEN_BATCHES)
#
#         browser.close()
#         flush_progress()
#
#         print(f"\n✅ Готово. В {OUT_JSON} сейчас {len(phones_map)} записей.")
#         if not SAVE_DATA_URI:
#             print(f"📂 PNG лежат в {IMG_DIR}")
#
#
# if __name__ == "__main__":
#     main()
