# 可能遇见的问题

按现象找；找不到就在 [Issues](https://github.com/imginez/pubchat/issues) 里问，安全问题走私密（见 [安全策略](../.github/SECURITY.md)）。

## 安装与启动

**Windows 提示"已保护你的电脑" / Defender 报毒？**
PyInstaller 单文件 + 没有代码签名，误报常见。点「更多信息 → 仍要运行」，或用源码运行，或自己给 exe 做代码签名。

**窗口白屏 / 提示缺 WebView2？**
装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)，或加 `--classic-gui` / `--no-gui` 用兜底界面。

**双击 exe 没反应？**
先确认端口没被占（默认 8765 / 8766），再看数据目录 `%LOCALAPPDATA%\PubChat` 下有没有日志。

## 连不上 / 打不开

**手机打不开局域网地址？**
确认同一个 Wi-Fi、路由器没开 AP 隔离；Windows 防火墙首次运行要允许 `PubChat.exe` 访问专用网络。

**点开启公网一直转圈？**
cloudflared 需要能连 Cloudflare；受限网络会失败，等十几秒看设置页的错误摘要。

**反代后全是 502？**
指向 8865 的 location 少了 `proxy_http_version 1.1;` 和 `proxy_set_header Connection "";`（见 [服务器部署](../使用说明/服务器部署.md)）。

**反代后管理页 403？**
管理 API 要 `X-Forwarded-Proto: https`，nginx 里写 `proxy_set_header X-Forwarded-Proto $scheme;`。

**桌面窗口里白屏（iframe 装实例）？**
反代或页面下发了 `X-Frame-Options` / `CSP frame-ancestors`，去掉即可（这条有冒烟用例锁着）。

## 数据

**数据在哪、怎么备份？**
默认 `%LOCALAPPDATA%\PubChat`（服务器版是 `--data-dir`），整个目录拷走即可；**没有自动备份，请自己备份**。

**能换端口吗？**
`--port 9000 --admin-port 9001`，防火墙放行新端口。

**换电脑怎么搬？**
把数据目录整个拷过去；旧库也能直接接管（`PubChat.exe --import D:\old\pubchat.db`，导入前自动备份并校验表结构）。

## 安全与边界

- 管理口只绑 `127.0.0.1` 并校验 `Host` / `Origin`（防 DNS rebinding 与跨站提交）；服务器模式的管理 API 只走回环口 + `Authorization: Bearer` 设备凭证。
- 二维码只含地址，不把登录 Key 拼进 URL；登录态按 origin 存在浏览器里。
- 明文 HTTP/WS 只适合本机/局域网；公网那侧由 Cloudflare 提供 HTTPS/WSS。
- **它不是公网生产级 IM**：没有找回密码、没有邮件/短信、没有多机同步、没有多副本；服务器模式已有限流与审计，但别当多租户平台用。
- 发现安全问题请按 [安全策略](../.github/SECURITY.md) 私下报告，不要开公开 issue。
