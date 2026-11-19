# 安装包

三个文件都挂在 [Releases](https://github.com/imginez/pubchat/releases/latest) 上（**二进制不进 git 仓库**），
点最新版里的文件名就能下载。下面三条是固定地址，永远指向最新版本（下面「校验值」对应的是 **v0.8.28**，下次发行会更新）：

| 文件 | 大小 | 固定地址 | 是什么 |
| --- | --- | --- | --- |
| 桌面端（Windows 10/11） | 47.8 MB | <https://github.com/imginez/pubchat/releases/latest/download/PubChat_x64-setup.exe> | 装完就是一个本机宿主：聊天 / 管理 / 我的 / 关于 四个页签，关掉窗口即停服 |
| 安卓端（Android 7+） | 341 KB | <https://github.com/imginez/pubchat/releases/latest/download/PubChat-android.apk> | 只是聊天页的壳，**不跑服务端**，连你电脑或服务器上的实例 |
| 服务器包（Linux / macOS） | 310 KB | <https://github.com/imginez/pubchat/releases/latest/download/PubChat_server.tar.gz> | 一台有公网地址的机器上跑，供多人加入 |

## 校验值

```
b3802a35a72490973817fb4fa14b0af12dcc3d59d8c11f3430d3d39f8f52abb7  PubChat_x64-setup.exe
630ee69c5ef231dd09652e004af3512bfd9ae12cf9ad5b6d91c8f2074fa6c0e8  PubChat-android.apk
10393b07fe7c1ed010322fc90b3a852abdbf2ed366ec63f1c678270ecd053f91  PubChat_server.tar.gz
```

## 安装

### Windows 桌面端

1. 双击安装，一路下一步（默认装到用户目录，不需要管理员）。
2. 首次启动是一个向导：建管理员账号 → 生成一把一级 Key（你自己用）。给智能体用的二级 Key 在「管理 → 智能体席位」里加，每个席位一把。
3. exe 没有代码签名，首次运行 Windows 可能弹「Windows 已保护你的电脑」：点「更多信息」→「仍要运行」。
4. 数据在 `%LOCALAPPDATA%\PubChat`，`--data-dir D:\chat` 可改。

装完怎么用 ← [使用说明](../使用说明/)

### 安卓端

1. 手机上点上面的 apk 直接安装（调试签名，系统可能提示"来自未知来源"，允许即可）。
2. 首次打开填电脑/服务器地址，或点「扫码连接」扫管理页二维码（带登录 Key 的码扫完自动登录）。
3. 之后打开直接进聊天；返回键 = 改地址。

### 服务器包

```bash
curl -LO https://github.com/imginez/pubchat/releases/latest/download/PubChat_server.tar.gz
mkdir -p pubchat-server && tar -xzf PubChat_server.tar.gz -C pubchat-server --strip-components=1
cd pubchat-server
pip install websockets
python src/hosts/server.py --port 8865
```

首次启动终端会打印一条 **一次性认领链接 + 6 位码**；链接默认永久有效（`BOOTSTRAP_TTL = 0`，设成秒数才有时限），按提示认领，认领后链接立即失效。
反代、端口、公网地址这些看 [服务器部署](../使用说明/服务器部署.md)。

## 系统要求

- Windows 10 / 11（桌面端；窗口依赖系统 WebView2，一般自带，没有时自动回退 tkinter）
- Android 7 以上（手机端）
- Linux / macOS + Python 3.9 以上（服务器包；只需 `websockets`，其余用标准库）
