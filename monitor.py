"""
Hermès Australia 库存监控（Lindy 系列）
-------------------------------------------------
做法：用真浏览器内核(Playwright)打开爱马仕澳洲官网的 Lindy 筛选列表页，
抓出"当前在售的所有 Lindy 款"，和上次记录对比。一旦出现【新的款】
（尤其名字带 mini），就发邮件通知你，并附上商品链接。

为什么用真浏览器：爱马仕用了 Akamai 反爬，普通 HTTP 请求会被 403 拦截，
只有真浏览器内核的网络指纹能通过。

所有敏感信息（邮箱密码、收件人）都从环境变量读取，由 GitHub Secrets 注入。
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from email.utils import formataddr

from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# 配置（通过环境变量传入，见 README）
# ----------------------------------------------------------------------
# 默认用搜索页找 Lindy（缺货时返回的是无关推荐，slug 不含 lindy → 零误报）
# 注意用 `or`：GitHub 未设置的 secret 会传空字符串，空串也要回退到默认值
LISTING_URL = os.environ.get("LISTING_URL") or \
    "https://www.hermes.com/au/en/search/?s=lindy"
# 只保留 slug 含此词的商品才算"真·目标命中"。默认 lindy。
SLUG_FILTER = (os.environ.get("SLUG_FILTER") or "lindy").lower()
# 优先关注的关键词（名字含此词的新款会在邮件里高亮）。
MATCH_KEYWORD = (os.environ.get("MATCH_KEYWORD") or "mini").lower()
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
# 抓取：用真浏览器打开列表页，取出所有 Lindy 商品 {slug: url}
# ----------------------------------------------------------------------
def fetch_lindy_products() -> dict[str, str]:
    products: dict[str, str] = {}
    with sync_playwright() as p:
        # channel="chrome" 用系统真实 Chrome（比内置 Chromium 更难被识破）
        browser = p.chromium.launch(
            headless=HEADLESS,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="en-AU",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        page.goto(LISTING_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)  # 等商品网格渲染

        body_sample = page.content().lower()
        blocked = (
            "temporarily restricted" in body_sample
            or "unusual activity" in body_sample
            or "access denied" in body_sample
        )

        # 取所有指向 /product/ 的链接
        hrefs = page.eval_on_selector_all(
            "a[href*='/product/']",
            "els => els.map(e => e.href)",
        )
        browser.close()

    if blocked:
        raise RuntimeError("被 Akamai 反爬拦截（Access temporarily restricted）")

    # 从链接 slug 里解析款式名，过滤出 lindy
    for href in hrefs:
        m = re.search(r"/product/([^/?#]+)", href)
        if not m:
            continue
        slug = m.group(1)
        if SLUG_FILTER not in slug.lower():
            continue
        products[slug] = href.split("?")[0]
    return products


# ----------------------------------------------------------------------
# 状态持久化：记录上次见到的 Lindy 款式集合
# ----------------------------------------------------------------------
def load_seen() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("seen", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(slugs: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(slugs)}, f, ensure_ascii=False, indent=2)


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

    try:
        current = fetch_lindy_products()
    except Exception as e:
        print(f"[ERROR] 抓取失败: {e}", file=sys.stderr)
        return 1

    print(f"[INFO] 当前在售 Lindy 款式 {len(current)} 个: {sorted(current)}")

    seen = load_seen()
    first_run = not os.path.exists(STATE_FILE)
    new_slugs = set(current) - seen

    if new_slugs and not first_run:
        # 关键词优先排序（带 mini 的排前面）
        ordered = sorted(
            new_slugs, key=lambda s: (MATCH_KEYWORD not in s.lower(), s)
        )
        lines = [f"· {nice_name(s)}\n{current[s]}" for s in ordered]
        body = "爱马仕澳洲官网 Lindy 系列出现新款（可能上线了）：\n\n" + "\n\n".join(lines)
        hot = [s for s in ordered if MATCH_KEYWORD in s.lower()]
        title = (
            f"🎉 Hermès 上新！{nice_name(hot[0])}"
            if hot
            else f"🎉 Hermès Lindy 上新 {len(new_slugs)} 款"
        )
        send_email(title, body)
    elif first_run:
        print("[INFO] 首次运行，记录基线，不发通知")
    else:
        print("[INFO] 没有新款")

    # 更新基线（记录所有当前款式，下次只对比新增）
    save_seen(set(current))
    return 0


if __name__ == "__main__":
    sys.exit(main())
