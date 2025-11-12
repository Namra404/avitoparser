import random
import time
from playwright.sync_api import Page
from .settings import HUMAN

def sleep(a: float, b: float):
    time.sleep(random.uniform(a, b))

def pause():
    sleep(*HUMAN["between_actions_pause"])

def scroll_jitter(page: Page, count: int | None = None):
    if count is None:
        count = random.randint(*HUMAN["pre_page_warmup_scrolls"])
    try:
        height = page.evaluate("() => document.body.scrollHeight") or 3000
        for _ in range(count):
            step = random.randint(*HUMAN["scroll_step_px"])
            direction = 1 if random.random() > 0.25 else -1
            y = max(0, min(height, page.evaluate("() => window.scrollY") + step * direction))
            page.evaluate("y => window.scrollTo({top: y, behavior: 'smooth'})", y)
            sleep(*HUMAN["scroll_pause_s"])
    except Exception:
        pass

def wiggle_mouse(page: Page, x: float, y: float):
    steps = random.randint(*HUMAN["mouse_wiggle_steps"])
    amp = random.randint(*HUMAN["mouse_wiggle_px"])
    for _ in range(steps):
        dx = random.randint(-amp, amp)
        dy = random.randint(-amp, amp)
        try:
            page.mouse.move(x + dx, y + dy)
        except Exception:
            pass
        pause()

def hover(page: Page, loc):
    try:
        box = loc.bounding_box()
        if not box:
            return
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        page.mouse.move(cx, cy)
        wiggle_mouse(page, cx, cy)
        sleep(*HUMAN["hover_pause_s"])
    except Exception:
        pass
