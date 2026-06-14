# Hermès 澳洲库存监控（多款）

每 10 分钟自动检查爱马仕澳洲官网指定的几款包是否上货，一上线就**发邮件通知你**并附商品链接。
跑在 GitHub Actions 上，**免费、不用开电脑**。

当前监控：**Lindy**（含 Lindy II mini）、**Neo Garden 23**。
要加/改监控目标，编辑 `monitor.py` 顶部的 `WATCHES` 列表即可（每款一条：名字 / 搜索网址 / slug 关键词）。

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
