"""
PubChat 宿主层。

两个宿主：
- hosts/server.py: 公网多实例宿主（反代后，读 PUBCHAT_* 环境变量）
- hosts/local.py: 单 exe 本机宿主（单端口静态直服 + 回环管理面 + WebView2 + 隧道）
"""
