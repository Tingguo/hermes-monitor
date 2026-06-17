# Hermès 澳洲库存监控（多款）

**每 10 分钟**自动检查爱马仕澳洲官网指定的几款包是否上货，一上线就**发邮件通知你**并附商品链接。
跑在 GitHub Actions 上，**免费、不用开电脑**。

当前监控：**Lindy**（含 Lindy II mini）、**Neo Garden 23**。
要加/改监控目标，编辑 `monitor.py` 顶部的 `WATCHES` 列表即可（每款一条：名字 / 搜索网址 / slug 关键词）。

## 怎么做到“真·每 10 分钟”（GitHub 免费 cron 其实很慢）
GitHub 免费版定时（cron）是“尽力而为”，实测会被压到**每几小时才跑一次**，抢爱马仕太慢。
所以改成：**一个任务内部循环**，每 10 分钟查一轮、连续跑约 5.5 小时；结束前用 PAT
**自动“接力”触发下一棒**，首尾相接、永不间断。`concurrency` 保证同一时间只有一个在跑、
不会重复堆积；另留一条低频 cron 仅作“接力链万一断了”的兜底重启。
> 这需要**无限的 Actions 分钟数**，所以仓库设为 **Public**（公开的只是监控代码本身，
> Gmail 密码等 Secret 始终加密、不暴露）。接力依赖名为 `DISPATCH_TOKEN` 的 Secret（一个
> GitHub PAT），**不要删它**，删了接力链就断、只剩 cron 兜底。

## 工作原理（为什么这么绕）
- 爱马仕用了 **Akamai 反爬**，是全网最强之一。普通请求被 403，连普通无头浏览器也会被
  "Access temporarily restricted" 拦截。
- 实测**有头模式 + 系统真实 Chrome + 反检测脚本**可以骗过它（已在真实环境验证通过）。
  云端用 `xvfb` 提供虚拟显示器来跑有头浏览器。
- 判定信号：在搜索页搜 `lindy`，缺货时返回的是无关推荐（slug 不含 `lindy`）；
  一旦出现 slug 含 `lindy` 的商品，就是上货了 —— **零误报**。

## ⚠️ 唯一的未知数
GitHub 服务器是**数据中心 IP**，Akamai 对这类 IP 比住宅网络更警惕。
真实 Chrome 指纹能大幅提高成功率，但能否过要**部署后手动跑一次才知道**（见下）。
如果被 IP 拦，再走备选方案（住宅代理 / 在自己设备上跑）。

## 一次性配置步骤

### 1. 传到一个新的 GitHub 仓库（建议 Private）

### 2. 拿到 Gmail 应用专用密码
1. 开启两步验证：myaccount.google.com → 安全性 → 两步验证
2. 生成应用密码：myaccount.google.com/apppasswords → 起名生成 → 复制那 16 位

### 3. 配置 Secrets
`Settings` → `Secrets and variables` → `Actions` → `New repository secret`：

| Secret 名字          | 值 | 必填 |
| -------------------- | --- | --- |
| `GMAIL_USER`         | 发信 Gmail 地址 | ✅ |
| `GMAIL_APP_PASSWORD` | Gmail 的 16 位应用专用密码 | ✅ |
| `MAIL_TO`            | 收通知的邮箱 | ✅ |

（监控哪几款写在 `monitor.py` 的 `WATCHES` 里，不走 Secret。）

### 4. 【先测邮件】
`Actions` → `Hermès Lindy 监控` → `Run workflow` → 勾选
**"只发一封测试邮件"** → 运行。收到测试邮件 = 邮件通道 OK。

### 5. 【再测反爬】
再 `Run workflow` 一次（这次**不勾**测试邮件）。看日志：
- `当前在售 Lindy 款式 N 个` → ✅ 成功穿过反爬，大功告成，坐等到货邮件。
- `被 Akamai 反爬拦截` → ❌ 数据中心 IP 被卡，需要走备选方案。

## 注意
- 免费定时最快约 10 分钟一次，是"近实时"非秒级。
- 首次正式运行只记录基线、不通知；之后只通知**新增**的 Lindy 款。

## 本地运行（可选，调试用）
```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m playwright install chrome
.venv/Scripts/python monitor.py        # Windows 本地默认有头，直接能跑
```
