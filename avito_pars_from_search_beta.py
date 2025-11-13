# avito_async.py
import json
import random
import asyncio
from pathlib import Path
from typing import Optional, Dict

from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PWTimeoutError,
    Error as PWError,
)

# ========== НАСТРОЙКИ ==========

CATEGORY_URL = (
    "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg?user=1"
)

OUT_DIR = Path("avito_phones_playwright")
OUT_DIR.mkdir(exist_ok=True)

HEADLESS = False          # обязательно False — логинишься руками
MAX_ITEMS = 5             # 5 ОБЪЯВЛЕНИЙ С НАЙДЕННОЙ КАРТИНКОЙ НОМЕРА

PAGE_DELAY = 5
CLICK_DELAY = 8
NAV_TIMEOUT = 90_000

USE_PROXY = False         # при необходимости включишь
PROXY_HOST = "mproxy.site"
PROXY_PORT = 228
PROXY_LOGIN = ""
PROXY_PASSWORD = ""


# ========== ХЕЛПЕРЫ ==========

async def human_sleep(a: float, b: float):
    await asyncio.sleep(random.uniform(a, b))


async def safe_get_content(page: Page) -> str:
    try:
        return await page.content()
    except PWError:
        await asyncio.sleep(1)
        try:
            return await page.content()
        except PWError:
            return ""


async def is_captcha_or_block(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
    except PWError:
        url = ""
    html = (await safe_get_content(page)).lower()
    if "captcha" in url or "firewall" in url:
        return True
    if "доступ с вашего ip-адреса временно ограничен" in html:
        return True
    return False


async def close_city_or_cookie_modals(page: Page):
    selectors = [
        "button[aria-label='Закрыть']",
        "button[data-marker='modal-close']",
        "button[class*='close']",
        "button:has-text('Понятно')",
        "button:has-text('Хорошо')",
    ]
    for sel in selectors:
        try:
            for b in await page.query_selector_all(sel):
                try:
                    if await b.is_visible():
                        await b.click()
                        await human_sleep(0.3, 0.8)
                except Exception:
                    continue
        except Exception:
            continue


async def close_login_modal_if_exists(page: Page) -> bool:
    """Если вылезла авторизация после клика — закрываем и считаем объявление неудачным."""
    selectors_modal = [
        "[data-marker='login-form']",
        "[data-marker='registration-form']",
        "div[class*='modal'][class*='auth']",
        "div[class*='modal'] form[action*='login']",
    ]
    for sel in selectors_modal:
        try:
            modals = await page.query_selector_all(sel)
        except PWError:
            continue

        for m in modals:
            try:
                if not await m.is_visible():
                    continue
            except Exception:
                continue

            # пробуем найти любую кнопку закрытия
            for btn_sel in [
                "button[aria-label='Закрыть']",
                "button[data-marker='modal-close']",
                "button[class*='close']",
                "button[type='button']",
            ]:
                btn = await m.query_selector(btn_sel)
                if btn:
                    try:
                        if await btn.is_enabled():
                            await btn.click()
                            await human_sleep(0.4, 0.8)
                            print("🔒 Модалка авторизации закрыта, объявление пропущено.")
                            return True
                    except Exception:
                        pass

            print("🔒 Модалка авторизации не закрывается — объявление пропускаем.")
            return True

    return False


async def extract_phone_image_data(item, page: Page, avito_id: str) -> Optional[str]:
    """
    После клика ищем img[data-marker='phone-image'],
    возвращаем data:image/png;base64,... (без сохранения PNG).
    """
    # сначала ищем в пределах карточки
    try:
        img = await item.query_selector("img[data-marker='phone-image']")
    except PWError:
        img = None

    # на всякий случай пробуем по всей странице
    if not img:
        try:
            img = await page.query_selector("img[data-marker='phone-image']")
        except PWError:
            img = None

    if not img:
        print(f"⚠️ [{avito_id}] Картинка с номером не найдена.")
        return None

    src = (await img.get_attribute("src")) or ""
    if not src.startswith("data:image"):
        print(f"⚠️ [{avito_id}] src не data:image, а: {src[:40]}...")
        return None

    print(f"✅ [{avito_id}] Получен data:image (длина {len(src)}).")
    return src  # просто возвращаем data-URI, не декодируем


async def parse_phone_image_for_item(page: Page, item, idx_on_page: int) -> Optional[str]:
    """
    Кликает ТОЛЬКО по 'Показать телефон/номер' и возвращает data:image... или None.
    """
    avito_id = (await item.get_attribute("id")) or ""
    if avito_id.startswith("i"):
        avito_id = avito_id[1:]

    # hover — чуть-чуть по-человечески
    try:
        await item.hover()
        await human_sleep(0.5, 1.0)
    except Exception:
        pass

    # Ищем именно кнопку "Показать телефон/номер"
    btn_selectors = [
        "button[data-marker='item-phone-button']",
        "button:has-text('Показать телефон')",
        "button:has-text('Показать номер')",
        "button[aria-label*='Показать телефон']",
        "button[aria-label*='Показать номер']",
    ]
    phone_button = None
    for sel in btn_selectors:
        try:
            b = await item.query_selector(sel)
            if b and await b.is_enabled() and await b.is_visible():
                phone_button = b
                break
        except Exception:
            continue

    if not phone_button:
        print(f"⚠️ [{avito_id}] Кнопка 'Показать телефон' не найдена.")
        return None

    await human_sleep(1.0, 2.5)

    try:
        await phone_button.scroll_into_view_if_needed()
        await human_sleep(0.3, 0.7)
        await phone_button.click()
        print(f"📞 [{avito_id}] Нажали 'Показать телефон' (#{idx_on_page}).")
    except Exception as e:
        print(f"⚠️ [{avito_id}] Не удалось кликнуть по кнопке телефона: {e}")
        return None

    print(f"⏳ [{avito_id}] Ждём {CLICK_DELAY} секунд после клика...")
    await asyncio.sleep(CLICK_DELAY)

    if await close_login_modal_if_exists(page):
        return None
    if await is_captcha_or_block(page):
        print("🚫 Капча/блок после клика телефона.")
        return None

    return await extract_phone_image_data(item, page, avito_id)


# ========== ОСНОВНОЙ СЦЕНАРИЙ ==========

async def main():
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        context.set_default_timeout(NAV_TIMEOUT)

        page = await context.new_page()

        print(f"➡️ Открываем {CATEGORY_URL}")
        try:
            await page.goto(CATEGORY_URL, wait_until="load", timeout=NAV_TIMEOUT)
        except PWTimeoutError:
            print("⚠️ Навигация по таймауту — продолжаем с тем, что есть...")

        # РУЧНОЙ ЛОГИН
        print("\n🔑 Твои действия:")
        print("   • если есть капча — реши;")
        print("   • залогинься в Авито;")
        print("   • вернись на страницу со списком объявлений.")
        input("👉 Когда на экране список объявлений, нажми Enter в консоли.\n")

        await asyncio.sleep(3)

        if await is_captcha_or_block(page):
            print("❌ Всё ещё капча/блок — выходим.")
            await browser.close()
            return

        await close_city_or_cookie_modals(page)

        # ждём карточки
        try:
            await page.wait_for_selector('div[data-marker="item"]', timeout=30000)
        except PWTimeoutError:
            print("⚠️ Не вижу объявлений. Проверь, что реально открыт список.")
            print((await safe_get_content(page))[:1200])
            await browser.close()
            return

        print(f"⏳ Ждём {PAGE_DELAY} секунд перед обработкой...")
        await asyncio.sleep(PAGE_DELAY)

        items = await page.query_selector_all('div[data-marker="item"]')
        print(f"🔎 Найдено карточек на странице: {len(items)}")

        phones_map: Dict[str, str] = {}
        found_count = 0

        for idx, item in enumerate(items, start=1):
            if found_count >= MAX_ITEMS:
                break

            try:
                url_el = await item.query_selector('a[itemprop="url"]')
                url = await url_el.get_attribute("href") if url_el else None
                if not url:
                    print("⚠️ У карточки нет ссылки, пропускаем.")
                    continue

                data_uri = await parse_phone_image_for_item(page, item, idx)

                if data_uri:
                    phones_map[url] = data_uri  # data:image/png;base64,...
                    found_count += 1
                    print(f"💾 Map: {url} -> [data:image...], всего {found_count}/{MAX_ITEMS}")
                else:
                    print("⏭ Картинка не найдена, лимит не трогаем.")

                await human_sleep(2.0, 5.0)

            except Exception as e:
                print("Ошибка по объявлению:", e)

        await browser.close()

        out_file = OUT_DIR / "phones_map.json"
        out_file.write_text(json.dumps(phones_map, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ Готово. Сохранено {len(phones_map)} записей в {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
