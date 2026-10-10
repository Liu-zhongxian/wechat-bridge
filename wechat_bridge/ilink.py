"""iLink 协议层：HTTP 基础、认证头、登录、消息收发。

只处理微信官方接口的请求/响应，不含业务逻辑。
"""

import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_API = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
APP_ID = "bot"
CLIENT_VERSION = "132102"      # 0x020406 -> 2.4.6
CHANNEL_VERSION = "2.4.8"
BOT_AGENT = "Muse"

# 消息 item 类型
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
# getuploadurl 的 media_type（编号与 item 类型不同）
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE, UPLOAD_VOICE = 1, 2, 3, 4

MAX_MEDIA_BYTES = 100 << 20  # 100 MB
MAX_TEXT_RUNES = 2000  # 微信单条文本上限（按字符计）
QR_MAX_REFRESH = 3  # 二维码过期自动刷新次数


def rand_uin_b64():
    """随机 uint32 转十进制字符串再 base64（X-WECHAT-UIN 头）。"""
    n = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(n).encode("utf-8")).decode("ascii")


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


def split_text(text, max_runes=MAX_TEXT_RUNES):
    """超长文本按 max_runes 切分（学 wx-bot 的 SplitText）。"""
    if max_runes <= 0:
        max_runes = MAX_TEXT_RUNES
    if len(text) <= max_runes:
        return [text]
    return [text[i:i + max_runes] for i in range(0, len(text), max_runes)]


def send_message(baseurl, token, to_user_id, text, context_token):
    """发文本。超 2000 字自动分片多条发出，返回最后一条的响应。"""
    last_resp = {}
    for chunk in split_text(text):
        payload = {
            "msg": {
                "from_user_id": "",          # 必须空字符串，否则发不出去还不报错
                "to_user_id": to_user_id,
                "client_id": uuid.uuid4().hex,  # 每次随机唯一
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token or "",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
            },
            "base_info": base_info(),
        }
        last_resp = http_json("POST", baseurl + "/ilink/bot/sendmessage",
                              auth_headers(token), payload, timeout=30)
        if is_token_invalid(last_resp):
            break
    return last_resp


def fetch_qrcode(save_qr_json, save_qr_img):
    """请求新的登录二维码。

    save_qr_json: 保存 {"qrcode","qrcode_img_content","created_at","api"} 的回调
    save_qr_img: 保存二维码图片文件的回调
    返回 (qrcode, img_content)，失败返回 (None, None)。
    """
    url = DEFAULT_API + "/ilink/bot/get_bot_qrcode?bot_type=3"
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": rand_uin_b64(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }
    resp = http_json("POST", url, headers, {"local_token_list": []}, timeout=30)
    qrcode = resp.get("qrcode")
    img_content = resp.get("qrcode_img_content")
    if not qrcode or not img_content:
        print("获取二维码失败: %s" % json.dumps(resp, ensure_ascii=False)[:500])
        return None, None
    save_qr_json({
        "qrcode": qrcode,
        "qrcode_img_content": img_content,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "api": DEFAULT_API,
    })
    import qrcode as qr_lib
    qr_lib.make(img_content).save(save_qr_img)
    return qrcode, img_content
