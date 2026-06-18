"""
Hermès Australia 库存监控（多款）
-------------------------------------------------
做法：用真浏览器内核(Playwright)逐个打开爱马仕澳洲官网的搜索页，
抓出"当前在售的目标系列"，和上次记录对比。一旦出现【新的款】，
就发邮件通知你，并附上商品链接。每款独立记基线、独立通知。

为什么用真浏览器：爱马仕用了 Akamai 反爬，普通 HTTP 请求会被 403 拦截，
只有真浏览器内核的网络指纹能通过。

要加/改监控目标，编辑下面的 WATCHES 即可。
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formataddr

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# 监控目标列表 —— 想加新款就往这里加一条
#   name    : 邮件里显示的名字
#   url     : 爱马仕搜索页（缺货时返回无关推荐，slug 不含目标词 → 零误报）
#   slug    : 商品链接 slug 含此词才算命中（用来排除推荐兜底的无关货）
#   keyword : 邮件标题高亮用的词（不参与筛选）
# ----------------------------------------------------------------------
WATCHES = [
    {
        "name": "Lindy",
        "url": "https://www.hermes.com/au/en/search/?s=lindy",
        "slug": "lindy",
        "keyword": "mini",
    },
    {
        "name": "Neo Garden 23",
        "url": "https://www.hermes.com/au/en/search/?s=neo%20garden",
        "slug": "neo-garden",
        "keyword": "23",
    },
]

# 是否无头运行。爱马仕的反爬对无头很敏感，必须有头(云端用 xvfb 提供虚拟显示器)。
HEADLESS = os.environ.get("HEADLESS", "0") == "1"

GMAIL_USER = os.environ.get("GMAIL_USER", "")            # 发信 Gmail 地址
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # 16 位应用专用密码
MAIL_TO = os.environ.get("MAIL_TO", "")                  # 收件人

STATE_FILE = "state.json"

# 反检测脚本：抹掉自动化痕迹，让 Akamai 把我们当成真人浏览器
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-AU','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}};
"""


# ----------------------------------------------------------------------
# 抓取：在已打开的页面里访问某个搜索页，取出命中 slug 的商品 {slug: url}
# ----------------------------------------------------------------------
def fetch_products(page, url: str, slug_filter: str, name: str = "") -> dict[str, str]:
    # 正常搜索页一定有商品链接（即使缺货也有约 10 个推荐兜底）。
    # 所以"等到商品元素出现"，慢渲染就多等；拿到 0 个就重试一次。
    hrefs: list[str] = []
    for attempt in range(1, 4):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("a[href*='/product/']", timeout=20000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2500)  # 让网格补齐

        body_sample = page.content().lower()
        if any(s in body_sample for s in
               ("temporarily restricted", "unusual activity", "access denied")):
            raise RuntimeError("被反爬拦截（Access temporarily restricted）")

        hrefs = page.eval_on_selector_all(
            "a[href*='/product/']", "els => els.map(e => e.href)"
        )
        if hrefs:
            break
        print(f"[WARN] {url} 第 {attempt} 次拿到 0 个商品，等待后重试…")
        page.wait_for_timeout(4000)

    # 渲染正常时总数 > 0；重试后仍为 0，多半是被静默给了空页面 → 报错而非漏报。
    print(f"[INFO] {url} → 商品链接总数 {len(hrefs)}")
    if len(hrefs) == 0:
        # 诊断：把云端实际看到的页面信息和截图留下来
        try:
            print(f"[DEBUG] title={page.title()!r} content_len={len(page.content())}")
            safe = re.sub(r"[^a-z0-9]+", "-", (name or "page").lower())
            page.screenshot(path=f"debug-{safe}.png", full_page=True)
            print(f"[DEBUG] 已保存截图 debug-{safe}.png")
        except Exception as e:
            print(f"[DEBUG] 截图失败: {e}")
        raise RuntimeError("重试后仍拿到空页面 —— 真浏览器伪装可能失效")

    products: dict[str, str] = {}
    for href in hrefs:
        m = re.search(r"/product/([^/?#]+)", href)
        if not m:
            continue
        slug = m.group(1)
        if slug_filter.lower() not in slug.lower():
            continue
        products[slug] = href.split("?")[0]
    return products


# 连续失败多少次才告警。数据中心 IP 被 Akamai 软拦是偶发常态，一段 40 分钟的
# 坏窗口属正常波动；只有持续约 1.5 小时（9 轮）都拿不到，才像真出问题，才告警。
ALERT_THRESHOLD = 9

# 某货号要"连续缺席"多少轮才判定真下架（防单轮渲染抖动误判 → 避免重复发邮件）。
# 2 轮≈20 分钟。下架满此阈值后再上架才会再次通知。
ABSENT_GRACE = 2


# ----------------------------------------------------------------------
# 状态持久化
#   watches: {name: [slug,...]}   每款见过的 slug（基线）
#   fails:   {name: 连续失败次数}  用来区分"偶发被拦"和"真的坏了"
# ----------------------------------------------------------------------
def load_state() -> dict:
    empty = {"watches": {}, "fails": {}, "misses": {}}
    if not os.path.exists(STATE_FILE):
        return empty
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "watches": data.get("watches", {}),   # {name: [已通知且视为在售的 slug]}
            "fails": data.get("fails", {}),        # {name: 连续抓取失败次数}
            "misses": data.get("misses", {}),      # {name: {slug: 连续缺席轮数}}
        }
    except (json.JSONDecodeError, OSError):
        return empty


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 抓取一款：用全新 context（首访最不易被拦）；失败时再换全新 context 补抓一次
# ----------------------------------------------------------------------
def fetch_watch(browser, w: dict) -> dict[str, str]:
    # 关键经验（已多次验证）：每次都用全新 context，并把搜索页作为该 context 的
    # “首次访问”——本站对“同一 context 内的第二次导航”会间歇性软拦返回空页面，
    # 所以【不做首页预热】，直接开搜索页最不易被拦。失败就换全新 context 重试。
    last_err: Exception | None = None
    attempts = 3
    for tryno in range(1, attempts + 1):
        context = browser.new_context(
            locale="en-AU", viewport={"width": 1366, "height": 900}
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        try:
            return fetch_products(page, w["url"], w["slug"], w["name"])
        except Exception as e:  # noqa: BLE001
            last_err = e
            if tryno < attempts:
                print(f"[WARN] [{w['name']}] 第 {tryno} 次失败，换全新浏览器再试…")
        finally:
            context.close()
    assert last_err is not None
    raise last_err


# ----------------------------------------------------------------------
# 发邮件（Gmail SMTP）
# ----------------------------------------------------------------------
def send_email(subject: str, body: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and MAIL_TO):
        raise RuntimeError("未设置 GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Hermès 监控", GMAIL_USER))
    msg["To"] = MAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [MAIL_TO], msg.as_string())
    print("[OK] 通知邮件已发送")


def nice_name(slug: str) -> str:
    """把 lindy-mini-bag-H082608CC0G 变成可读的 'lindy mini bag'。"""
    name = re.sub(r"-[A-Z0-9]{6,}$", "", slug)   # 去掉末尾货号
    return name.replace("-", " ")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def launch_browser(p):
    # channel="chrome" 用系统真实 Chrome（比内置 Chromium 更难被识破）
    return p.chromium.launch(
        headless=HEADLESS,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )


def run_pass(browser, state: dict) -> None:
    """对所有 WATCHES 做一轮检查：抓取→对比基线→发上新邮件，原地更新 state。"""
    watches = state["watches"]
    fails = state["fails"]
    misses = state["misses"]

    for w in WATCHES:
        name = w["name"]
        try:
            current = fetch_watch(browser, w)
        except Exception as e:  # noqa: BLE001
            # 抓取失败（多半是 Akamai 临时软拦）：保留旧基线，累计连续失败次数。
            # 不立刻报错——偶发被拦是常态，连续多次才说明真出问题。
            fails[name] = fails.get(name, 0) + 1
            print(f"[ERROR] [{name}] 抓取失败({fails[name]} 连续): {e}",
                  file=sys.stderr)
            # 连续失败正好到阈值时告警一次（避免偶发抖动刷屏 / 持续坏了又不刷屏）
            if fails[name] == ALERT_THRESHOLD:
                try:
                    send_email(
                        "⚠️ Hermès 监控可能失效，请检查",
                        f"监控款【{name}】已连续 {ALERT_THRESHOLD} 次抓取失败"
                        f"（约 {ALERT_THRESHOLD * 10} 分钟没查成），"
                        "可能是爱马仕反爬升级或网站改版。\n\n"
                        "监控仍会继续重试；恢复后会自动静默，无需理会本邮件。",
                    )
                except Exception as ee:  # noqa: BLE001
                    print(f"[ERROR] 告警邮件发送失败: {ee}", file=sys.stderr)
            continue

        # 成功 → 清零失败计数
        fails[name] = 0
        print(f"[INFO] [{name}] 当前在售 {len(current)} 个: {sorted(current)}")

        first_run = name not in watches
        notified = set(watches.get(name, []))   # 已通知且仍视为在售的货号
        miss = misses.setdefault(name, {})       # 各货号的连续缺席轮数
        cur = set(current)

        # 仅当某货号【不在已通知清单里】才算"新增/重新上架"→ 发邮件
        new_slugs = cur - notified

        # 处理"消失"的货号：连续缺席满 ABSENT_GRACE 轮才真正移出清单
        #（防单轮渲染抖动误判下架，从而避免它"再现"时重复发邮件）
        for slug in list(notified):
            if slug in cur:
                miss.pop(slug, None)             # 还在 → 缺席清零
            else:
                miss[slug] = miss.get(slug, 0) + 1
                if miss[slug] >= ABSENT_GRACE:
                    notified.discard(slug)        # 判定真下架，移出清单（将来重上会再通知）
                    miss.pop(slug, None)
                    print(f"[INFO] [{name}] 货号 {slug} 连续缺席，判定下架")

        if new_slugs and not first_run:
            kw = w["keyword"].lower()
            ordered = sorted(new_slugs, key=lambda s: (kw not in s.lower(), s))
            lines = [f"· {nice_name(s)}\n{current[s]}" for s in ordered]
            body = (f"爱马仕澳洲官网【{name}】出现新款（可能上线了）：\n\n"
                    + "\n\n".join(lines))
            title = f"🎉 Hermès 上新！{nice_name(ordered[0])}"
            send_email(title, body)
        elif first_run:
            print(f"[INFO] [{name}] 首次运行，记录基线，不发通知")
        else:
            print(f"[INFO] [{name}] 无新增（在售的都已通知过）")

        # 新增的并入清单；缺席未到阈值的仍保留（不立即丢，避免抖动重复）
        notified |= new_slugs
        watches[name] = sorted(notified)


def main() -> int:
    # 自检：发一封测试邮件确认通道，然后退出
    if os.environ.get("SEND_TEST") == "1":
        send_email(
            "✅ Hermès 监控 - 测试邮件",
            "能收到这封，说明邮件通道配置正确，监控到货时就能正常通知你。",
        )
        return 0

    # RUN_MINUTES>0 → 循环模式：每 INTERVAL_SEC 查一轮，连续跑约 RUN_MINUTES 分钟。
    # =0（默认）→ 只查一轮就退出（本地调试 / 兜底单次）。
    run_minutes = int(os.environ.get("RUN_MINUTES", "0") or "0")
    interval = int(os.environ.get("INTERVAL_SEC", "600") or "600")

    state = load_state()
    deadline = time.time() + run_minutes * 60

    with sync_playwright() as p:
        browser = launch_browser(p)
        round_no = 0
        while True:
            round_no += 1
            print(f"\n===== 第 {round_no} 轮检查 @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
            try:
                run_pass(browser, state)
            except Exception as e:  # noqa: BLE001
                # 浏览器层面崩了（极少）→ 重启浏览器，下一轮继续
                print(f"[ERROR] 本轮异常，重启浏览器: {e}", file=sys.stderr)
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = launch_browser(p)

            save_state(state)  # 每轮都落盘，任务被强杀时缓存仍是最新

            # 单次模式，或再睡一轮就超时了 → 收工（留时间给"接力"步骤）
            if run_minutes <= 0 or time.time() + interval >= deadline:
                break
            print(f"[INFO] 等待 {interval}s 后进行下一轮…")
            time.sleep(interval)

        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
