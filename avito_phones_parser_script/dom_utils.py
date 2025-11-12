import re
import time
from base64 import b64decode
from io import BytesIO
from pathlib import Path
from PIL import Image
from playwright.sync_api import Page, Error as PWError
from .human import sleep, hover, pause
from .settings import HUMAN, IMG_DIR

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
                    hover(page, loc)
                    loc.click()
                    sleep(0.25, 0.7)
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
            for m in page.locator(sel).all():
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
                            hover(page, btn)
                            sleep(*HUMAN["pre_click_pause_s"])
                            btn.click()
                            sleep(*HUMAN["post_click_pause_s"])
                            print("🔒 Модалка авторизации закрыта, объявление пропущено.")
                            return True
                        except Exception:
                            pass
                print("🔒 Модалка авторизации не закрывается — объявление пропускаем.")
                return True
        except PWError:
            continue
    return False

def get_avito_id_from_url(url: str) -> str:
    m = re.search(r'(\d{7,})', url)
    return m.group(1) if m else str(int(time.time()))

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
