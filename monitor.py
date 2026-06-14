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
    for attempt in range(1, 3):
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


# ----------------------------------------------------------------------
# 状态持久化：每款记一组见过的 slug   {watches: {name: [slug,...]}}
# ----------------------------------------------------------------------
def load_state() -> dict[str, list[str]]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("watches", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(watches: dict[str, list[str]]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"watches": watches}, f, ensure_ascii=False, indent=2)


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
def main() -> int:
    # 自检：发一封测试邮件确认通道，然后退出
    if os.environ.get("SEND_TEST") == "1":
        send_email(
            "✅ Hermès 监控 - 测试邮件",
            "能收到这封，说明邮件通道配置正确，监控到货时就能正常通知你。",
        )
        return 0

    state = load_state()
    had_error = False

    with sync_playwright() as p:
        # channel="chrome" 用系统真实 Chrome（比内置 Chromium 更难被识破）
        browser = p.chromium.launch(
            headless=HEADLESS,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="en-AU", viewport={"width": 1366, "height": 900}
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        for w in WATCHES:
            name = w["name"]
            try:
                current = fetch_products(page, w["url"], w["slug"], name)
            except Exception as e:
                # 某一款抓取失败（如临时被拦）不影响其它款；保留它的旧基线
                print(f"[ERROR] [{name}] 抓取失败: {e}", file=sys.stderr)
                had_error = True
                continue

            print(f"[INFO] [{name}] 当前在售 {len(current)} 个: {sorted(current)}")

            first_run = name not in state
            seen = set(state.get(name, []))
            new_slugs = set(current) - seen

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
                print(f"[INFO] [{name}] 没有新款")

            # 更新该款基线（只在成功抓取时更新）
            state[name] = sorted(current)

        browser.close()

    save_state(state)
    # 有任何一款抓取失败就让这次运行标红，方便发现问题
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
