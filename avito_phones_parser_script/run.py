import atexit
import random
import signal
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from multitabs_pars_test import recheck_pending_once
from avito_phones_parser_script.settings import *
from avito_phones_parser_script.io_progress import atomic_write_json, load_progress
from avito_phones_parser_script.input_urls import read_urls_from_excel_or_csv
from avito_phones_parser_script.pool import process_with_pool
from avito_phones_parser_script.dom_utils import is_captcha_or_block

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
            atomic_write_json(OUT_JSON, phones_map)  # сохраняем сразу

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
