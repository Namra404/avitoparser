import json
import re
import time
import random
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

# Файл со ссылками (Excel/CSV). Для твоего файла просто укажи путь:
INPUT_FILE = Path("АВТОСАЛОН 09.11 2000.xlsx")   # можно положить рядом со скриптом
INPUT_SHEET = None   # None = все листы; либо укажи имя листа/индекс
URL_COLUMN = None    # None = извлекать URL из всех колонок через regex; либо укажи имя колонки

OUT_DIR = Path("avito_phones_playwright")
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "phones"
IMG_DIR.mkdir(exist_ok=True)

HEADLESS = False          # обязательно False — логинишься руками
MAX_ITEMS = None          # None = все ссылки из файла; либо число
CLICK_DELAY = 8           # ожидание после клика по "Показать телефон"
NAV_TIMEOUT = 90_000

USE_PROXY = False
PROXY_HOST = "mproxy.site"
PROXY_PORT = 17518
PROXY_LOGIN = "YT4aBK"
PROXY_PASSWORD = "nUg2UTut9UMU"

PAGE_DELAY = (1.5, 3.5)   # пауза между URL, случайный интервал
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


# ========== ХЕЛПЕРЫ ==========

def human_sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))


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
    ]
    for sel in selectors:
        try:
            for b in page.query_selector_all(sel):
                if b.is_visible():
                    b.click()
                    human_sleep(0.25, 0.7)
        except Exception:
            continue


def close_login_modal_if_exists(page: Page) -> bool:
    """Если вылезла авторизация после клика — закрываем и считаем объявление неудачным."""
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
                        btn.click()
                        human_sleep(0.3, 0.6)
                        print("🔒 Модалка авторизации закрыта, объявление пропущено.")
                        return True
                    except Exception:
                        pass

            print("🔒 Модалка авторизации не закрывается — объявление пропускаем.")
            return True

    return False


def save_phone_png_from_data_uri(data_uri: str, file_stem: str) -> str | None:
    """Сохраняет картинку из data:image/... в phones/{file_stem}.png"""
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
    """Пытается вытащить числовой ID из URL объявления."""
    m = re.search(r'(\d{7,})', url)
    return m.group(1) if m else str(int(time.time()))


def click_show_phone_on_ad(page: Page) -> bool:
    """На странице объявления ищет и кликает кнопку 'Показать телефон/номер'."""
    btn_selectors = [
        "button[data-marker='item-phone-button']",
        "button:has-text('Показать телефон')",
        "button:has-text('Показать номер')",
        "button[aria-label*='Показать телефон']",
        "button[aria-label*='Показать номер']",
    ]
    for sel in btn_selectors:
        try:
            b = page.query_selector(sel)
            if b and b.is_enabled() and b.is_visible():
                b.scroll_into_view_if_needed()
                human_sleep(0.25, 0.6)
                b.click()
                print("📞 Нажали 'Показать телефон'.")
                return True
        except Exception:
            continue
    print("⚠️ Кнопка 'Показать телефон' не найдена.")
    return False


def extract_phone_data_uri_on_ad(page: Page) -> str | None:
    """
    На странице объявления ищет img[data-marker='phone-image'],
    возвращает data:image/png;base64,....
    """
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

    # нормализуем и убираем дубли
    cleaned = []
    seen = set()
    for u in urls:
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin("https://www.avito.ru", u)
        # обрезаем мусорные суффиксы вида ?context=...
        u = u.split("#", 1)[0]
        u = u.split("?", 1)[0]
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned


def process_ad_url(page: Page, url: str) -> tuple[str, str | None]:
    """
    Открывает страницу объявления, нажимает 'Показать телефон',
    ждёт CLICK_DELAY, забирает data-uri картинки. Возвращает (url, data_uri|None).
    """
    print(f"\n➡️ Открываем объявление: {url}")
    try:
        page.goto(url, wait_until="load", timeout=NAV_TIMEOUT)
    except PWTimeoutError:
        print("⚠️ Навигация по таймауту — пробуем продолжать...")

    human_sleep(1.0, 2.0)
    if is_captcha_or_block(page):
        print("🚫 Капча/блок на странице — пропускаем.")
        return url, None

    close_city_or_cookie_modals(page)

    if not click_show_phone_on_ad(page):
        return url, None

    print(f"⏳ Ждём {CLICK_DELAY} секунд после клика...")
    time.sleep(CLICK_DELAY)

    if close_login_modal_if_exists(page):
        return url, None
    if is_captcha_or_block(page):
        print("🚫 Капча/блок после клика — пропускаем.")
        return url, None

    data_uri = extract_phone_data_uri_on_ad(page)
    return url, data_uri


# ========== ОСНОВНОЙ СЦЕНАРИЙ ==========

def main():
    urls = read_urls_from_excel_or_csv(INPUT_FILE, INPUT_SHEET, URL_COLUMN)
    if MAX_ITEMS:
        urls = urls[:MAX_ITEMS]
    print(f"🔎 Всего ссылок к обработке: {len(urls)}")

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
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=UA,
        )
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        context.set_default_timeout(NAV_TIMEOUT)
        page = context.new_page()

        # РУЧНОЙ ЛОГИН перед стартом: откроем первый URL и подождём
        if urls:
            try:
                page.goto(urls[0], wait_until="load", timeout=NAV_TIMEOUT)
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
            return

        phones_map: dict[str, str] = {}
        processed = 0

        for url in urls:
            try:
                _, data_uri = process_ad_url(page, url)
                if data_uri:
                    # --- ВАРИАНТ A: хранить data-uri в JSON (как ты просил) ---
                    phones_map[url] = data_uri

                    # --- ВАРИАНТ B: вместо data-uri хранить путь к PNG ---
                    # avito_id = get_avito_id_from_url(url)
                    # out_path = save_phone_png_from_data_uri(data_uri, avito_id)
                    # if out_path:
                    #     phones_map[url] = out_path

                    print(f"✅ Сохранено: {url} -> [data:image...]")
                else:
                    print(f"⏭ Нет картинки номера: {url}")

                processed += 1
                human_sleep(*PAGE_DELAY)

            except Exception as e:
                print(f"⚠️ Ошибка по ссылке {url}: {e}")
                human_sleep(1.0, 2.0)

        browser.close()

        out_file = OUT_DIR / "phones_map.json"
        out_file.write_text(json.dumps(phones_map, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ Готово. Сохранено {len(phones_map)} записей в {out_file}")
        print(f"📂 PNG (если включишь вариант B) будут лежать в {IMG_DIR}")


if __name__ == "__main__":
    main()
