# wechat-bridge

微信官方 iLink / Clawbot Bot 通道的 Python 桥接：把微信消息接到你的个人 AI 助手，支持**文本、图片、视频、文件**双向收发。

## 功能

- **收**：长轮询收文本、图片、语音（带微信转写）、视频、文件；图片/视频/文件自动从微信 CDN 下载解密到本地
- **发**：`send --text/--image/--file/--video`，媒体经 AES-128-ECB 加密上传 CDN 后发送
- **常驻**：`serve` 后台运行 + `watchdog.sh` 健康看门狗（5 分钟检查，挂了自动拉起）
- **去重**：按 `message_id` 幂等，重启不重复回复
- **脱敏**：token 只存 `state/session.json`（已 gitignore），日志/状态输出自动打码

## 快速开始

```bash
pip install qrcode pycryptodome

python3 bridge.py login        # 生成二维码，微信扫码
python3 bridge.py wait-login   # 等待扫码确认

# 先用微信给 Bot 发一条消息（官方限制：必须用户先发才有 context_token）
nohup python3 bridge.py serve &

python3 bridge.py send --text "你好"
python3 bridge.py send --image ./a.jpg --text "见图"
python3 bridge.py send --video ./b.mp4
```

## 媒体加解密

微信 CDN 媒体为 AES-128-ECB + PKCS7 加密。`aes_key` 有 4 种格式（16 字节原文 / 32 hex / base64 / base64 包 hex），`parse_aes_key` 严格解析（标准 + URL 安全 base64）。

**坑**：视频下载必须用 `encrypt_query_param` 拼的 CDN 地址，`full_url` 返回的数据解密会失败（多了 wrapper 字节导致块不对齐）。图片两种地址都可用。

收到的媒体解密失败时会自动尝试多种密钥派生 × 模式组合（`_try_decrypt_variants`），命中有效文件头（MP4 `ftyp` / JPEG）即采用。

## 文件说明

| 文件 | 说明 |
|---|---|
| `bridge.py` | 主程序：login / wait-login / serve / send / status |
| `watchdog.sh` | 健康检查脚本，输出 `SERVE_ALIVE` / `SERVE_RESTARTED` / `NEEDS_RELOGIN_NOTIFY` 等 |
| `PROMPT-优化版.md` | 给 AI 助手用的完整搭建提示词（含 12 条优化经验） |

## 限制（微信官方）

- 仅一对一私聊，不支持群聊
- 助手不能主动发起会话，用户必须先发消息
- 连续约 10 条不回复会被限流，用户回一句重置
- 二维码有效期约 2 分钟

## 致谢

媒体收发思路参考 [lawrencewzen/wx-bot](https://github.com/lawrencewzen/wx-bot)（Go 实现），用 Python 重写并修复了视频下载地址选择问题。

MIT License.
