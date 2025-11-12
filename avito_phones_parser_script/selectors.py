import random
from playwright.sync_api import Page, Error as PWError
from .human import sleep, scroll_jitter
from .settings import HUMAN
from .dom_utils import close_city_or_cookie_modals

def try_click_selector(page: Page, sel: str, timeout: int = 2000) -> bool:
    loc = page.locator(sel).first
    try:
        loc.wait_for(state="visible", timeout=timeout)
        sleep(*HUMAN["pre_click_pause_s"])
        loc.click()
        sleep(*HUMAN["post_click_pause_s"])
        return True
    except Exception:
        return False

def click_show_phone_on_ad(page: Page) -> bool:
    scroll_jitter(page)

    for anchor in [
        "[data-marker='seller-info']",
        "[data-marker='item-sidebar']",
        "section:has(button[data-marker*='phone'])",
        "section:has(button:has-text('Показать'))",
    ]:
        try:
            page.locator(anchor).first.scroll_into_view_if_needed()
            sleep(*HUMAN["scroll_pause_s"])
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
