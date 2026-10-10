# wechat-bridge

微信官方 iLink / Clawbot Bot 通道的 Python 桥接：把微信消息接到你的个人 AI 助手，支持**文本、图片、语音、视频、文件**双向收发。

## 架构

学 [wx-bot](https://github.com/lawrencewzen/wx-bot) 的分层思路：

```
wechat_bridge/
  ilink.py    # 协议层：登录、收发消息、headers、文本分片
  crypto.py   # 加解密：AES-128-ECB、aes_key 解析、多策略暴力解密
  media.py    # 媒体：CDN 上传下载、send_media_message
  store.py    # 状态：路径管理、原子读写、会话（0600 权限）
  serve.py    # 业务层：长轮询、指数退避、去重、outbox 发送
  cli.py      # 命令行入口
tests/
  test_crypto.py
```

## 安装

```bash
pip install qrcode pycryptodome
pip install -e .   # 或直接用 python3 -m wechat_bridge
```

## 快速开始

```bash
python3 -m wechat_bridge login       # 生成二维码，微信扫码
python3 -m wechat_bridge wait-login  # 等待确认（二维码过期自动刷新 3 次）

# 先用微信给 Bot 发一条消息（官方限制：必须用户先发才有 context_token）
nohup python3 -m wechat_bridge serve &

python3 -m wechat_bridge send --text "你好"
python3 -m wechat_bridge send --image ./a.jpg --text "见图"
python3 -m wechat_bridge send --video ./b.mp4
python3 -m wechat_bridge status
```

也可以用安装后的命令：`wechat-bridge send --text 你好`

数据目录默认 `~/workspace/wechat-bridge`，可用环境变量 `WECHAT_BRIDGE_DIR` 覆盖。

## 特性

- **指数退避**：getupdates 失败 2 秒重试，连续 3 次失败后退避到 30 秒；服务端可下发 `longpolling_timeout_ms` 动态调轮询窗口
- **文本分片**：超 2000 字自动切多条发出
- **二维码自动刷新**：`wait-login` 遇到过期自动重取，最多 3 次
- **媒体加解密**：AES-128-ECB + PKCS7；`aes_key` 4 种格式严格解析；解密失败自动试多种密钥派生 × 模式组合
- **视频下载**：必须用 `encrypt_query_param` 拼的 CDN 地址（`full_url` 的数据解密会失败）
- **幂等去重**：按 `message_id` 去重，重启不重复回复
- **脱敏**：token 只存 `state/session.json`（gitignore），日志/状态输出打码

## 限制（微信官方）

- 仅一对一私聊，不支持群聊
- 助手不能主动发起会话，用户必须先发消息
- 连续约 10 条不回复会被限流，用户回一句重置
- 二维码有效期约 2 分钟

## 测试

```bash
python3 tests/test_crypto.py
```

## 致谢

媒体收发与分层思路参考 [lawrencewzen/wx-bot](https://github.com/lawrencewzen/wx-bot)（Go 实现）。

MIT License.
