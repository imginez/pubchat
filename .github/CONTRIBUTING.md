# 参与开发

先谢过。这个项目的要求不复杂，但有几条硬边界，动手前请先看一眼。

## 环境

```bash
pip install websockets qrcode          # 跑服务端/测试够用
pip install pywebview                  # 想要桌面窗口再加这个
python src/tests/ci.py                     # 377 项可移植子集（CI 跑的就是它，几秒）
python src/tests/smoke.py                  # 888 项完整冒烟，真起实例，只碰 127.0.0.1
python src/desktop.py                      # 源码起桌面宿主
```

Windows 下再想要单 exe：`powershell -ExecutionPolicy Bypass -File src\packaging\build.ps1`。

## 硬边界（改了就拒）

1. **WS 协议字段名不动**。帧是 4 字节长度头 + JSON，字段名一变，已经在跑的客户端就全废。要加字段走可选字段 + 兼容默认值。
2. **DB schema 只加不改**：`users` / `agents` / `rooms` / `messages` 现有列不改语义；新表新列要能对旧库自动补齐。
3. **前端只有一份**。`web/` 同时给桌面窗口（iframe）和手机网页用，**不要再写第二套**。手机端密度与桌面不同，靠视口适配解决，不靠复制页面。
4. **反代要能被嵌入**：页面不得下发 `X-Frame-Options`，也不得写 `CSP frame-ancestors`；前端不许出现顶层跳转。这几条有冒烟用例锁着（`src/tests/smoke.py` 的「前端硬约束」一节）。
5. **保持很轻**：新增依赖、常驻服务、后台轮询都要有强理由。能标准库解决就别加包。

## 提 PR 的流程

1. 先开 issue 说清要解决的问题（小改动可以直接 PR，但要在描述里写清动机）。
2. 一个 PR 只做一件事，别夹带格式化、改名、顺手重构。
3. 自测必须是真的跑过：`src/tests/ci.py` 必须全绿，改动涉及宿主/界面/反代时再跑 `src/tests/smoke.py` 并贴结尾统计；界面改动附改前改后截图。
4. PR 模板里的「影响面」清单逐项勾，动了协议 / schema / 前端 / 反代约束要单独说明。
5. 提交信息用一句话说清做了什么（中文或英文都行），例如 `fix: 修反代下 502（补 proxy_http_version）`。

## 代码风格

- Python：标准库风格，4 空格缩进，`snake_case`；业务逻辑进 `core/`，宿主差异只留在 `hosts/`。
- 前端：原生 JS，不引框架；颜色用语义化 token，不逐屏写死 hex；正文对比度不低于 4.5:1。
- 注释写"为什么"，不写"这行在赋值"。

## 许可

提交即表示同意以 [MIT](../LICENSE) 授权你的贡献。别提交来源不明的代码或资源；引用第三方要写清出处与许可证。
