from playwright.sync_api import Page
from .dom_utils import safe_get_content
from .settings import UA  # не используется напрямую, просто для единообразия импорта

# “Без звонков”
NO_CALLS_MARKERS = [
    "без звонков",
    "пользователь предпочитает сообщения",
]

# Может стать доступным позже
MODERATION_MARKERS = [
    "оно ещё на проверке",
    "объявление на проверке",
    "объявление ещё на проверке",
]

# Навсегда недоступно
UNAVAILABLE_MARKERS = [
    "объявление не посмотреть",
    "объявление снято с продажи",
    "объявление удалено",
    "объявление закрыто",
    "объявление больше не доступно",
]

# ⬇ добавлено: лимит на просмотр контактов
QUOTA_MARKERS = [
    "закончился лимит на просмотр контактов",
    "купить контакты",
    "проверку по документам",
]

def is_quota_limit(page: Page) -> bool:
    """Возвращает True, если всплыла модалка лимита контактов."""
    html = safe_get_content(page).lower()
    if any(m in html for m in QUOTA_MARKERS):
        return True
    try:
        if page.locator("text=Закончился лимит на просмотр контактов").first.is_visible():
            return True
    except Exception:
        pass
    try:
        if page.locator("text=Купить контакты").first.is_visible():
            return True
    except Exception:
        pass
    return False

def classify_ad_status(page: Page) -> str:
    """
    'ok' | 'no_calls' | 'on_review' | 'unavailable' | 'blocked'
    (лимит контактов не здесь, потому что он появляется ПОСЛЕ клика)
    """
    try:
        url = page.url.lower()
    except Exception:
        url = ""

    html = safe_get_content(page).lower()
    if "captcha" in url or "firewall" in url:
        return "blocked"
    if "доступ с вашего ip-адреса временно ограничен" in html:
        return "blocked"

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
