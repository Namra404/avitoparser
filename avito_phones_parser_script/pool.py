import random, time
from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from multitabs_pars_test import make_page_pool, human_scroll_jitter
from .dom_utils import close_city_or_cookie_modals, is_captcha_or_block, save_phone_png_from_data_uri, \
    get_avito_id_from_url
from .settings import (
    CONCURRENCY, NAV_TIMEOUT, PAGE_DELAY_BETWEEN_BATCHES, SAVE_DATA_URI,
    PENDING_JSON, QUOTA_WAIT, HUMAN,
)
from .selectors import click_show_phone_on_ad, extract_phone_data_uri_on_ad
from .status import classify_ad_status, is_quota_limit

from .io_progress import save_pending, load_pending

def process_with_pool(context, urls, on_result):
    pages = make_page_pool(context, CONCURRENCY)
    pending_queue = load_pending(PENDING_JSON)

    try:
        it = iter(urls)
        while True:
            batch = []
            skip_urls = set()  # ⬅️ не собирать телефоны для этих URL в конце итерации

            # Навигация на следующих N ссылок на уже открытых вкладках
            for p in pages:
                try:
                    url = next(it)
                except StopIteration:
                    save_pending(PENDING_JSON, pending_queue)
                    return
                batch.append((url, p))
                try:
                    p.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                except PWTimeoutError:
                    print(f"⚠️ Таймаут: {url}")
                    skip_urls.add(url)
                    continue
                time.sleep(random.uniform(0.2, 0.6))
                human_scroll_jitter(p, count=random.randint(1, 2))

            # Классификация/модалки/клик
            for url, p in batch:
                status = classify_ad_status(p)
                if status == "blocked":
                    print(f"🚫 Капча/блок на {url}")
                    skip_urls.add(url)
                    continue
                if status == "on_review":
                    print(f"⏳ На проверке: {url}")
                    on_result(url, "__SKIP_ON_REVIEW__")
                    pending_queue.append(url)
                    skip_urls.add(url)
                    continue
                if status == "unavailable":
                    print(f"⏭️ Недоступно/закрыто: {url}")
                    on_result(url, "__SKIP_UNAVAILABLE__")
                    skip_urls.add(url)
                    continue
                if status == "no_calls":
                    print(f"⏭️ Без звонков: {url}")
                    on_result(url, "__SKIP_NO_CALLS__")
                    skip_urls.add(url)
                    continue

                close_city_or_cookie_modals(p)

                # Клик «Показать телефон»
                if not click_show_phone_on_ad(p):
                    # повторная быстрая проверка статуса
                    status2 = classify_ad_status(p)
                    if status2 == "on_review":
                        on_result(url, "__SKIP_ON_REVIEW__"); pending_queue.append(url)
                    elif status2 == "unavailable":
                        on_result(url, "__SKIP_UNAVAILABLE__")
                    elif status2 == "no_calls":
                        on_result(url, "__SKIP_NO_CALLS__")
                    else:
                        # возможно, лимит уже всплыл без клика
                        if is_quota_limit(p):
                            print(f"⏳ Лимит контактов (без клика): {url}")
                            time.sleep(random.uniform(*QUOTA_WAIT))
                            on_result(url, "__SKIP_QUOTA__")
                            pending_queue.append(url)
                        else:
                            # сохраним разметку для диагностики
                            from .dom_utils import dump_debug
                            dump_debug(p, url)
                    skip_urls.add(url)
                    continue

                # Успешно кликнули — проверим модалку лимита контактов
                if is_quota_limit(p):
                    print(f"⏳ Лимит контактов: {url} — отложим.")
                    time.sleep(random.uniform(*QUOTA_WAIT))
                    on_result(url, "__SKIP_QUOTA__")
                    pending_queue.append(url)
                    skip_urls.add(url)
                    continue

            # Ждём отрисовку картинок телефонов
            time.sleep(random.uniform(*HUMAN["click_delay_jitter"]))

            # Сбор картинок
            for url, p in batch:
                if url in skip_urls:
                    continue
                if is_captcha_or_block(p):
                    continue
                from .selectors import close_login_modal_if_exists
                if close_login_modal_if_exists(p):
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
            time.sleep(random.uniform(*PAGE_DELAY_BETWEEN_BATCHES))

    finally:
        save_pending(PENDING_JSON, pending_queue)
        for p in pages:
            try:
                p.close()
            except Exception:
                pass
