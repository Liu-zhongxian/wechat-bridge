#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信 iLink / Clawbot Bot 桥接脚本（微信官方开放通道）。

命令：
  login       向官方接口请求登录二维码，生成二维码图片
  wait-login  轮询扫码状态，直到 confirmed，保存会话到 state/session.json
  serve       常驻：长轮询收消息 -> inbox/；outbox/pending/ -> sendmessage 发送
  send        发一条测试消息（用缓存的 context_token）
  status      查看登录和运行状态

安全红线：state/session.json 含登录凭据，绝不写入聊天记录、记忆文件或发给任何人。
本目录下 state/ 已加入 .gitignore，绝不进代码仓库。
"""

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
INBOX_DIR = os.path.join(HERE, "inbox")
PENDING_DIR = os.path.join(HERE, "outbox", "pending")
SENT_DIR = os.path.join(HERE, "outbox", "sent")

SESSION_PATH = os.path.join(STATE_DIR, "session.json")
QR_PATH = os.path.join(STATE_DIR, "qr.json")
BUF_PATH = os.path.join(STATE_DIR, "get_updates_buf.txt")
PID_PATH = os.path.join(STATE_DIR, "serve.pid")
LAST_CTX_PATH = os.path.join(STATE_DIR, "last_context.json")
NEEDS_RELOGIN_PATH = os.path.join(STATE_DIR, "NEEDS_RELOGIN")
QR_IMG_PATH = os.path.join(HERE, "qrcode.png")
MEDIA_DIR = os.path.join(HERE, "media")

DEFAULT_API = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
APP_ID = "bot"
CLIENT_VERSION = "132102"      # 0x020406 -> 2.4.6
CHANNEL_VERSION = "2.4.8"
BOT_AGENT = "Muse"

# 消息 item 类型（收发共用）
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
# getuploadurl 的 media_type（与 item 类型编号不同）
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE, UPLOAD_VOICE = 1, 2, 3, 4
MAX_MEDIA_BYTES = 100 << 20  # 100 MB

for _d in (STATE_DIR, INBOX_DIR, PENDING_DIR, SENT_DIR, MEDIA_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------- 基础工具 ----------

def rand_uin_b64():
    """随机 uint32 转十进制字符串再 base64。"""
    n = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(n).encode("utf-8")).decode("ascii")


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def app_headers():
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}


def auth_headers(token):
    h = app_headers()
    h.update({
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + token,
        "X-WECHAT-UIN": rand_uin_b64(),
    })
    return h


def http_json(method, url, headers, body=None, timeout=40, retries=3):
    """发 HTTP JSON 请求。微信服务端偶发直接断开连接，连接层错误自动重试。"""
    import http.client
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Accept": "*/*", "User-Agent": "Mozilla/5.0 (compatible; wechat-bridge/1.0)"}
    h.update(headers or {})
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            raw = e.read()  # HTTP 错误响应也带业务体，不重试
            break
        except (http.client.RemoteDisconnected, http.client.HTTPException,
                ConnectionError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            raise
    else:
        raise last_err
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:500]}


def is_token_invalid(resp):
    """token 失效的唯一信号：ret / errcode == -14。"""
    if not isinstance(resp, dict):
        return False
    return resp.get("ret") == -14 or resp.get("errcode") == -14


def mark_needs_relogin(reason):
    atomic_write_text(
        NEEDS_RELOGIN_PATH,
        "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason),
    )


def masked(s):
    s = s or ""
    return s[:4] + "****" + s[-4:] if len(s) > 12 else "****"


# ---------- 媒体收发（移植自 wx-bot 的 Go 实现思路） ----------

def _aes_ecb_encrypt(data, key):
    from Crypto.Cipher import AES
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def _aes_ecb_decrypt(data, key):
    from Crypto.Cipher import AES
    out = AES.new(key, AES.MODE_ECB).decrypt(data)
    pad = out[-1]
    if pad < 1 or pad > 16:
        raise ValueError("AES 解密 padding 非法")
    return out[:-pad]


def _is_hex(b):
    return all((48 <= c <= 57) or (97 <= c <= 102) or (65 <= c <= 70) for c in b)


def parse_aes_key(s):
    """微信 CDN 的 aes_key 有 4 种格式：16 字节原文 / 32 hex 字符 / base64(16字节) / base64(32hex)。
    base64 严格解析（标准和 URL 安全两种字母表，带/不带 padding 都试），避免静默丢字符。"""
    if isinstance(s, str):
        s = s.encode("utf-8")
    if len(s) == 16:
        return bytes(s)
    if len(s) == 32 and _is_hex(s):
        return bytes.fromhex(s.decode("ascii"))

    def _b64_try(data, altchars=None):
        # 补齐 padding 后严格解码
        data = data.strip()
        pad = (-len(data)) % 4
        try:
            return base64.b64decode(data + b"=" * pad, altchars=altchars, validate=True)
        except Exception:
            return None

    for alt in (None, b"-_"):
        decoded = _b64_try(s, altchars=alt)
        if decoded is None:
            continue
        if len(decoded) == 32 and _is_hex(decoded):
            return bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) == 16:
            return decoded
    raise ValueError("aes_key 格式无法解析 (len=%d)" % len(s))


def _try_decrypt_variants(encrypted, raw_key_str, key_override=None):
    """暴力尝试多种密钥派生 × 加密模式组合，返回 (明文, 策略名)。
    用于诊断微信视频等搞不清密钥格式的场景。"""
    from Crypto.Cipher import AES
    import hashlib
    candidates = []  # (key_bytes, mode, iv, name)
    raw = raw_key_str.encode("utf-8") if isinstance(raw_key_str, str) else (raw_key_str or b"")
    if key_override:
        candidates.append((key_override, "ECB", None, "override-ECB"))
    # 派生候选密钥
    derivations = []
    if len(raw) == 16:
        derivations.append((raw, "raw16"))
    if len(raw) == 32 and _is_hex(raw):
        derivations.append((bytes.fromhex(raw.decode("ascii")), "hex32"))
    # base64 变体
    for alt in (None, b"-_"):
        d = raw.strip()
        pad = (-len(d)) % 4
        try:
            dec = base64.b64decode(d + b"=" * pad, altchars=alt, validate=True)
        except Exception:
            continue
        tag = "b64" if alt is None else "b64url"
        if len(dec) == 16:
            derivations.append((dec, tag + "-16"))
        if len(dec) == 32:
            if _is_hex(dec):
                derivations.append((bytes.fromhex(dec.decode("ascii")), tag + "-hex32"))
            derivations.append((dec, tag + "-32(AES256)"))
            derivations.append((dec[:16], tag + "-first16"))
            derivations.append((hashlib.md5(dec).digest(), tag + "-md5"))
        if len(dec) == 24:  # 可能是 base64(16字节) 无 padding 的变体
            derivations.append((dec, tag + "-24?"))
    for kb, dname in derivations:
        if len(kb) == 16:
            candidates.append((kb, "ECB", None, dname + "-ECB"))
            candidates.append((kb, "CBC", b"\x00" * 16, dname + "-CBC0"))
        elif len(kb) == 32:
            candidates.append((kb, "ECB", None, dname + "-ECB"))
    for kb, mode, iv, name in candidates:
        try:
            if mode == "ECB":
                out = AES.new(kb, AES.MODE_ECB).decrypt(encrypted)
            else:
                out = AES.new(kb, AES.MODE_CBC, iv=iv).decrypt(encrypted)
        except Exception:
            continue
        # 校验：PKCS7 padding 合法 且 看起来像 MP4 (ftyp) 或 JPEG (FFD8)
        pad = out[-1]
        if pad < 1 or pad > 16:
            continue
        if out[-pad:] != bytes([pad]) * pad:
            continue
        plain = out[:-pad]
        if plain[:4] == b"\xff\xd8\xff\xe0" or plain[:4] == b"\xff\xd8\xff\xe1" \
           or b"ftyp" in plain[:32] or plain[:8].startswith(b"\x00\x00\x00"):
            return plain, name
    return None, None


def download_media(media, aes_key_override=None, dest_path=None):
    """从微信 CDN 下载并解密媒体文件。media 为 dict（含 encrypt_query_param/full_url/aes_key）。"""
    if aes_key_override:
        key = aes_key_override
        key_src = "override"
    else:
        raw_key = media.get("aes_key") or ""
        key = parse_aes_key(raw_key)
        key_src = "media.aes_key(len=%d)" % len(raw_key)
    full_url = media.get("full_url")
    param = media.get("encrypt_query_param") or ""
    urls = []
    if param:
        urls.append(("param", CDN_BASE + "/download?encrypted_query_param=" +
                     urllib.parse.quote(param, safe="")))
    if full_url:
        urls.append(("full_url", full_url))
    if not urls:
        raise ValueError("media 既无 full_url 也无 encrypt_query_param")
    encrypted = None
    used = ""
    for name, url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read(MAX_MEDIA_BYTES + 1)
            print("下载源 %s: %d 字节, 头部=%s" % (name, len(data), data[:16].hex()), flush=True)
            if encrypted is None:
                encrypted, used = data, name
            elif data != encrypted:
                print("注意：param 与 full_url 数据不一致!", flush=True)
        except Exception as e:
            print("下载源 %s 失败: %s" % (name, e), flush=True)
    if encrypted is None:
        raise ValueError("所有下载源均失败")
    print("采用下载源: %s" % used, flush=True)
    if len(encrypted) > MAX_MEDIA_BYTES:
        raise ValueError("媒体文件超过 100MB 上限")
    print("媒体下载: %d 字节, 密钥来源=%s" % (len(encrypted), key_src), flush=True)
    try:
        plain = _aes_ecb_decrypt(encrypted, key)
    except Exception as e:
        # 主策略失败：暴力尝试其他密钥派生 × 模式组合
        print("主解密失败(%s)，尝试其他组合…" % e, flush=True)
        raw_key = "" if aes_key_override else (media.get("aes_key") or "")
        plain, strategy = _try_decrypt_variants(encrypted, raw_key, aes_key_override)
        if plain is None:
            print("所有解密组合均失败，放弃", flush=True)
            try:
                with open("/tmp/wxbot_fail.bin", "wb") as f:
                    f.write(encrypted)
            except OSError:
                pass
            raise
        print("解密成功，策略: %s" % strategy, flush=True)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(plain)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest_path)
    return dest_path


def upload_media(baseurl, token, to_user_id, data, item_type):
    """上传媒体到微信 CDN，返回 (encrypt_query_param, aes_key_b64, cipher_size)。"""
    import hashlib
    aes_key = os.urandom(16)
    filekey = os.urandom(16).hex()
    encrypted = _aes_ecb_encrypt(data, aes_key)
    upload_media_type = {ITEM_IMAGE: UPLOAD_IMAGE, ITEM_VIDEO: UPLOAD_VIDEO,
                         ITEM_VOICE: UPLOAD_VOICE}.get(item_type, UPLOAD_FILE)
    body = {
        "filekey": filekey,
        "media_type": upload_media_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aes_key.hex(),
        "base_info": base_info(),
    }
    resp = http_json("POST", baseurl + "/ilink/bot/getuploadurl",
                     auth_headers(token), body, timeout=30)
    if resp.get("ret") not in (0, None):
        raise RuntimeError("getuploadurl 失败: %s" % json.dumps(resp, ensure_ascii=False)[:300])
    upload_url = (resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = resp.get("upload_param") or ""
        if not param:
            raise RuntimeError("getuploadurl 未返回上传地址")
        upload_url = (CDN_BASE + "/upload?encrypted_query_param=" +
                      urllib.parse.quote(param, safe="") +
                      "&filekey=" + urllib.parse.quote(filekey, safe=""))
    # POST 密文到 CDN，取 x-encrypted-param 响应头
    req = urllib.request.Request(
        upload_url, data=encrypted,
        headers={"Content-Type": "application/octet-stream",
                 "User-Agent": "Mozilla/5.0"},
        method="POST")
    last_err = None
    download_param = ""
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                if 400 <= r.status < 500:
                    raise RuntimeError("CDN 上传客户端错误: %s" % r.status)
                if r.status != 200:
                    raise RuntimeError("CDN 上传服务端错误: %s" % r.status)
                download_param = r.headers.get("x-encrypted-param") or ""
            if not download_param:
                raise RuntimeError("CDN 响应缺少 x-encrypted-param")
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    else:
        raise RuntimeError("CDN 上传失败: %s" % last_err)
    # 与官方插件一致：aes_key 取 32 位 hex 字符串再 base64
    aes_key_b64 = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
    return download_param, aes_key_b64, len(encrypted)


def send_media_message(baseurl, token, to_user_id, context_token, file_path, item_type, caption=""):
    """发图片/视频/文件到微信。caption 会作为单独的文本消息先发。"""
    with open(file_path, "rb") as f:
        data = f.read()
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("文件超过 100MB 上限")
    if caption:
        r = send_message(baseurl, token, to_user_id, caption, context_token)
        if is_token_invalid(r):
            return r
    download_param, aes_key_b64, cipher_size = upload_media(
        baseurl, token, to_user_id, data, item_type)
    media_ref = {"encrypt_query_param": download_param,
                 "aes_key": aes_key_b64, "encrypt_type": 1}
    if item_type == ITEM_IMAGE:
        item = {"type": ITEM_IMAGE,
                "image_item": {"media": media_ref, "mid_size": cipher_size}}
    elif item_type == ITEM_VIDEO:
        item = {"type": ITEM_VIDEO,
                "video_item": {"media": media_ref, "video_size": cipher_size}}
    else:
        item = {"type": ITEM_FILE,
                "file_item": {"media": media_ref,
                              "file_name": os.path.basename(file_path),
                              "len": str(len(data))}}
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token or "",
            "item_list": [item],
        },
        "base_info": base_info(),
    }
    return http_json("POST", baseurl + "/ilink/bot/sendmessage",
                     auth_headers(token), payload, timeout=60)


def media_type_from_path(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ITEM_IMAGE
    if ext in (".mp4", ".mov", ".avi", ".mkv"):
        return ITEM_VIDEO
    return ITEM_FILE


# ---------- login ----------

def cmd_login(args):
    url = DEFAULT_API + "/ilink/bot/get_bot_qrcode?bot_type=3"
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": rand_uin_b64(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }
    # 注意：不带 Authorization 头，不带 base_info
    resp = http_json("POST", url, headers, {"local_token_list": []}, timeout=30)
    qrcode = resp.get("qrcode")
    img_content = resp.get("qrcode_img_content")
    if not qrcode or not img_content:
        print("获取二维码失败: %s" % json.dumps(resp, ensure_ascii=False)[:500])
        return 1
    atomic_write_json(QR_PATH, {
        "qrcode": qrcode,
        "qrcode_img_content": img_content,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "api": DEFAULT_API,
    })
    import qrcode as qr_lib
    img = qr_lib.make(img_content)
    img.save(QR_IMG_PATH)
    print("二维码图片已保存: %s" % QR_IMG_PATH)
    print("请用手机微信扫码，扫完后运行: python3 bridge.py wait-login")
    return 0


# ---------- wait-login ----------

def cmd_wait_login(args):
    qr = load_json(QR_PATH)
    if not qr or not qr.get("qrcode"):
        print("没有待扫码的二维码，请先运行: python3 bridge.py login")
        return 1
    qrcode = qr["qrcode"]
    api = qr.get("api") or DEFAULT_API
    deadline = time.time() + 600  # 最长等 10 分钟
    print("等待扫码确认（最长 10 分钟）…")
    while time.time() < deadline:
        url = api + "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(qrcode, safe="")
        try:
            # 只带 iLink-App-Id 和 iLink-App-ClientVersion 两个头，长轮询 35 秒
            resp = http_json("GET", url, app_headers(), None, timeout=45)
        except Exception as e:
            print("轮询出错，2 秒后重试: %s" % e)
            time.sleep(2)
            continue
        status = resp.get("status")
        if status == "wait":
            pass
        elif status == "scaned":
            print("已扫码，等待在手机上确认…")
        elif status == "confirmed":
            session = {
                "bot_token": resp.get("bot_token"),
                "baseurl": resp.get("baseurl") or DEFAULT_API,
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "ilink_user_id": resp.get("ilink_user_id"),
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if not session["bot_token"]:
                print("confirmed 但未返回 bot_token: %s" % json.dumps(resp, ensure_ascii=False)[:300])
                return 1
            atomic_write_json(SESSION_PATH, session)  # 原子写入
            try:
                os.remove(QR_PATH)
            except OSError:
                pass
            # 重新登录成功：清除失效标记与"已通知"标记
            for p in (NEEDS_RELOGIN_PATH, os.path.join(STATE_DIR, "notified_relogin")):
                try:
                    os.remove(p)
                except OSError:
                    pass
            print("登录成功，会话已保存到 state/session.json（bot_token 已脱敏存放）。")
            return 0
        elif status == "expired":
            print("二维码已过期，请重新运行 login 获取新码。")
            return 1
        elif status == "need_verifycode":
            print("手机上显示了验证码，需要输入验证码才能继续；请重新运行 login 并按手机提示操作。")
            return 1
        elif status == "verify_code_blocked":
            print("验证失败次数过多，请重新运行 login。")
            return 1
        elif status == "scaned_but_redirect":
            host = resp.get("redirect_host")
            if host:
                api = host if host.startswith("http") else "https://" + host
                print("切换到重定向地址继续轮询: %s" % api)
            continue
        elif status == "binded_redirect":
            print("该 Bot 已绑定到当前实例，无需重复登录。")
            return 0 if resp.get("bot_token") else 1
        else:
            print("未知状态 %r，继续等待…" % (status,))
        time.sleep(1)
    print("等待超时（10 分钟），请重新运行 login。")
    return 1


# ---------- serve ----------

def extract_text(msg):
    parts = []
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:  # 文本
            t = (item.get("text_item") or {}).get("text")
            if t:
                parts.append(t)
        elif item.get("type") == 3:  # 语音：可能带转写文本
            t = (item.get("voice_item") or {}).get("text")
            if t:
                parts.append("[语音转写]" + t)
    return "".join(parts)


def handle_incoming(msg, owner_id):
    if msg.get("group_id"):
        return  # 群消息忽略
    if msg.get("message_type") != 1:
        return  # 只处理用户发来的消息（1=用户，2=Bot）
    from_id = msg.get("from_user_id") or ""
    if owner_id and from_id != owner_id:
        return  # 只处理机主微信 ID 发来的消息
    text = extract_text(msg)
    media_paths = []
    if not text.strip():
        # 非文本消息：图片则下载到本地，语音/文件/视频记录占位
        item_types = sorted({str(it.get("type")) for it in (msg.get("item_list") or [])})
        print(f"非文本消息 [{msg.get('message_id')}] item_types={item_types}", flush=True)
        for it in (msg.get("item_list") or []):
            itype = it.get("type")
            sub = it.get("image_item") or it.get("video_item") or it.get("file_item")
            if itype in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE) and sub:
                key_hex = sub.get("aeskey") or ""
                key = bytes.fromhex(key_hex) if key_hex else None
                kind = {ITEM_IMAGE: "image", ITEM_VIDEO: "video"}.get(itype, "file")
                ext = {ITEM_IMAGE: "jpg", ITEM_VIDEO: "mp4"}.get(itype, "bin")
                fname = sub.get("file_name") if itype == ITEM_FILE else ""
                if fname:
                    fname = "".join(c for c in fname if c not in '/\\')
                    dest_name = "%s_%s" % (msg.get("message_id"), fname)
                else:
                    dest_name = "%s_%s.%s" % (msg.get("message_id"), kind, ext)
                day = time.strftime("%Y%m%d")
                dest = os.path.join(MEDIA_DIR, day, dest_name)
                try:
                    download_media(sub.get("media") or {}, key, dest)
                    media_paths.append(dest)
                    print("已下载%s -> %s" % (kind, dest), flush=True)
                except Exception as e:
                    m = sub.get("media") or {}
                    print("%s下载失败: %s | encrypt_type=%s full_url=%s param_len=%d keys=%s"
                          % (kind, e, m.get("encrypt_type"),
                             bool(m.get("full_url")),
                             len(m.get("encrypt_query_param") or ""),
                             sorted(sub.keys())), flush=True)
            elif itype == ITEM_VOICE:
                pass  # 语音转写文本已在 extract_text 中提取
        if media_paths:
            kinds = {".jpg": "[图片]", ".mp4": "[视频]"}
            text = kinds.get(os.path.splitext(media_paths[0])[1], "[文件]")
        else:
            return  # 其他未知类型暂不处理
    message_id = msg.get("message_id")
    record = {
        "message_id": message_id,
        "text": text,
        "from_user_id": from_id,
        "context_token": msg.get("context_token") or "",
        "create_time_ms": msg.get("create_time_ms"),
    }
    if media_paths:
        record["media_paths"] = media_paths
    path = os.path.join(INBOX_DIR, "%s.json" % message_id)
    if not os.path.exists(path):
        atomic_write_json(path, record)
        print("收到消息 [%s]: %s" % (message_id, text[:60]), flush=True)
    # 缓存最新会话上下文，供 send --text 测试用
    atomic_write_json(LAST_CTX_PATH, {
        "to_user_id": from_id,
        "context_token": record["context_token"],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def send_message(baseurl, token, to_user_id, text, context_token):
    payload = {
        "msg": {
            "from_user_id": "",          # 必须空字符串，否则发不出去还不报错
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,  # 每次随机唯一
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token or "",  # 原样回传用户那条消息里的值
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": base_info(),        # 顶层必须带 base_info
    }
    return http_json("POST", baseurl + "/ilink/bot/sendmessage",
                     auth_headers(token), payload, timeout=30)


def flush_outbox(baseurl, token):
    """把 outbox/pending/ 里的回复发出去，发完移到 outbox/sent/。"""
    try:
        files = sorted(f for f in os.listdir(PENDING_DIR) if f.endswith(".json"))
    except OSError:
        return True
    for name in files:
        path = os.path.join(PENDING_DIR, name)
        job = load_json(path)
        if not job:
            continue
        text = job.get("text") or ""
        to_user_id = job.get("to_user_id") or ""
        context_token = job.get("context_token") or ""
        media_path = job.get("media_path") or ""
        if not to_user_id or (not text and not media_path):
            print("跳过无效回复任务: %s" % name, flush=True)
            continue
        try:
            if media_path:
                # 媒体任务：先发文字说明（如果有），再发图片/文件
                if not os.path.exists(media_path):
                    print("媒体文件不存在，跳过: %s" % media_path, flush=True)
                    continue
                item_type = media_type_from_path(media_path)
                resp = send_media_message(baseurl, token, to_user_id,
                                          context_token, media_path, item_type,
                                          caption=text)
            else:
                resp = send_message(baseurl, token, to_user_id, text, context_token)
        except Exception as e:
            print("发送失败，稍后重试: %s" % e, flush=True)
            continue
        if is_token_invalid(resp):
            mark_needs_relogin("sendmessage 返回 -14，token 失效")
            print("sendmessage 返回 -14，token 失效。", flush=True)
            return False
        if resp.get("ret") not in (0, None):
            print("发送返回异常，稍后重试: %s" % json.dumps(resp, ensure_ascii=False)[:200], flush=True)
            continue
        os.replace(path, os.path.join(SENT_DIR, name))
        print("已发送回复 -> %s" % name, flush=True)
    return True


def cmd_serve(args):
    session = load_json(SESSION_PATH)
    if not session or not session.get("bot_token"):
        print("未登录，请先运行 login / wait-login。")
        return 1
    if os.path.exists(NEEDS_RELOGIN_PATH):
        print("检测到 NEEDS_RELOGIN 标记，token 已失效，请重新扫码登录。")
        return 1
    baseurl = (session.get("baseurl") or DEFAULT_API).rstrip("/")
    token = session["bot_token"]
    owner_id = session.get("ilink_user_id")
    atomic_write_text(PID_PATH, str(os.getpid()))
    buf = ""
    if os.path.exists(BUF_PATH):
        with open(BUF_PATH, encoding="utf-8") as f:
            buf = f.read().strip()
    print("serve 启动，监听用户 %s 的消息…" % owner_id, flush=True)
    try:
        while True:
            if os.path.exists(NEEDS_RELOGIN_PATH):
                print("NEEDS_RELOGIN 存在，退出 serve。", flush=True)
                return 1
            url = baseurl + "/ilink/bot/getupdates"
            body = {"get_updates_buf": buf, "base_info": base_info()}
            try:
                resp = http_json("POST", url, auth_headers(token), body, timeout=45)
            except Exception as e:
                print("getupdates 出错，5 秒后重试: %s" % e, flush=True)
                time.sleep(5)
                continue
            if not isinstance(resp, dict):
                resp = {}
            if is_token_invalid(resp):
                # -14 也可能是限流：先暂停 5 分钟重试一次，仍失败才判 token 死亡
                print("收到 -14，暂停 5 分钟后重试一次…", flush=True)
                time.sleep(300)
                try:
                    resp = http_json("POST", url, auth_headers(token), body, timeout=45)
                except Exception:
                    resp = {}
                if is_token_invalid(resp):
                    mark_needs_relogin("getupdates 返回 -14，token 失效")
                    print("token 确认失效，已写 NEEDS_RELOGIN 标记，退出。", flush=True)
                    return 1
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                buf = new_buf
                atomic_write_text(BUF_PATH, buf)
            for msg in resp.get("msgs") or []:
                try:
                    handle_incoming(msg, owner_id)
                except Exception as e:
                    print("处理消息出错: %s" % e, flush=True)
            if not flush_outbox(baseurl, token):
                return 1
    except KeyboardInterrupt:
        print("收到中断，退出 serve。", flush=True)
        return 0
    finally:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass


# ---------- send ----------

def cmd_send(args):
    session = load_json(SESSION_PATH)
    if not session or not session.get("bot_token"):
        print("未登录，请先运行 login / wait-login。")
        return 1
    if os.path.exists(NEEDS_RELOGIN_PATH):
        print("token 已失效，请重新扫码登录。")
        return 1
    if not args.text and not args.image and not args.file and not args.video:
        print("请用 --text / --image / --file / --video 指定内容，例如:")
        print("  python3 bridge.py send --text 你好")
        print("  python3 bridge.py send --image ./a.jpg --text '见图'")
        return 1
    ctx = load_json(LAST_CTX_PATH) or {}
    to_user_id = args.to or ctx.get("to_user_id") or session.get("ilink_user_id")
    context_token = args.context or ctx.get("context_token") or ""
    baseurl = (session.get("baseurl") or DEFAULT_API).rstrip("/")
    media_path = args.image or args.file or args.video
    try:
        if media_path:
            if not os.path.exists(media_path):
                print("文件不存在: %s" % media_path)
                return 1
            item_type = media_type_from_path(media_path)
            resp = send_media_message(baseurl, session["bot_token"], to_user_id,
                                      context_token, media_path, item_type,
                                      caption=args.text or "")
        else:
            resp = send_message(baseurl, session["bot_token"], to_user_id,
                                args.text, context_token)
    except Exception as e:
        print("发送出错: %s" % e)
        return 1
    if is_token_invalid(resp):
        mark_needs_relogin("send 返回 -14，token 失效")
        print("token 失效，已写 NEEDS_RELOGIN 标记，请重新扫码。")
        return 1
    print("发送结果: %s" % json.dumps(resp, ensure_ascii=False)[:300])
    return 0


# ---------- status ----------

def cmd_status(args):
    session = load_json(SESSION_PATH)
    print("== 登录状态 ==")
    if session and session.get("bot_token"):
        print("已登录  bot_id=%s  user_id=%s" % (session.get("ilink_bot_id"), session.get("ilink_user_id")))
        print("baseurl=%s  登录时间=%s" % (session.get("baseurl"), session.get("saved_at")))
        print("bot_token=%s（已脱敏，不显示完整值）" % masked(session.get("bot_token")))
    else:
        print("未登录（请先运行 login / wait-login）")
    if os.path.exists(NEEDS_RELOGIN_PATH):
        with open(NEEDS_RELOGIN_PATH, encoding="utf-8") as f:
            print("!! 需要重新扫码登录（NEEDS_RELOGIN）: %s" % f.read().strip())
    print("== 运行状态 ==")
    pid = None
    if os.path.exists(PID_PATH):
        with open(PID_PATH, encoding="utf-8") as f:
            pid = f.read().strip()
    alive = False
    if pid and pid.isdigit():
        try:
            os.kill(int(pid), 0)
            alive = True
        except OSError:
            alive = False
    print("serve: %s" % ("运行中 (pid %s)" % pid if alive else "未运行"))
    try:
        inbox_n = len([f for f in os.listdir(INBOX_DIR) if f.endswith(".json")])
    except OSError:
        inbox_n = 0
    try:
        pending_n = len([f for f in os.listdir(PENDING_DIR) if f.endswith(".json")])
    except OSError:
        pending_n = 0
    print("inbox: %d 条未处理，outbox/pending: %d 条待发送" % (inbox_n, pending_n))
    ctx = load_json(LAST_CTX_PATH)
    print("最近会话上下文: %s" % ("有（可 send --text 测试）" if ctx else "无（需先在微信里给我发一条消息）"))
    return 0


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description="微信 iLink / Clawbot Bot 桥接")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="获取登录二维码")
    sub.add_parser("wait-login", help="轮询扫码状态完成登录")
    sub.add_parser("serve", help="常驻收发消息")
    sp = sub.add_parser("send", help="发送一条测试消息")
    sp.add_argument("--text", default="", help="消息文本（发媒体时作为配文先发）")
    sp.add_argument("--image", default=None, help="图片路径")
    sp.add_argument("--file", default=None, help="文件路径")
    sp.add_argument("--video", default=None, help="视频路径")
    sp.add_argument("--to", default=None, help="目标用户 ID（默认用缓存的）")
    sp.add_argument("--context", default=None, help="context_token（默认用缓存的）")
    sub.add_parser("status", help="查看登录和运行状态")
    args = p.parse_args()
    fn = {
        "login": cmd_login,
        "wait-login": cmd_wait_login,
        "serve": cmd_serve,
        "send": cmd_send,
        "status": cmd_status,
    }[args.cmd]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
