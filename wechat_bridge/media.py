"""媒体收发：微信 CDN 的上传下载。

上传：随机 AES 密钥加密 → getuploadurl → POST 密文到 CDN → sendmessage 带媒体引用。
下载：CDN 下载 → AES 解密 → 落盘。
思路参考 lawrencewzen/wx-bot 的 Go 实现。
"""

import base64
import hashlib
import os
import time
import urllib.parse
import urllib.request
import uuid

from . import crypto
from .ilink import (
    CDN_BASE, MAX_MEDIA_BYTES,
    ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE,
    UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE, UPLOAD_VOICE,
    auth_headers, base_info, http_json, is_token_invalid, send_message,
)


def media_type_from_path(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ITEM_IMAGE
    if ext in (".mp4", ".mov", ".avi", ".mkv"):
        return ITEM_VIDEO
    return ITEM_FILE


def download_media(media, aes_key_override=None, dest_path=None):
    """从微信 CDN 下载并解密媒体文件。

    media: dict，含 encrypt_query_param / full_url / aes_key。
    注意：视频必须用 encrypt_query_param 拼的地址，full_url 的数据解密会失败。
    """
    if aes_key_override:
        key = aes_key_override
        key_src = "override"
    else:
        raw_key = media.get("aes_key") or ""
        key = crypto.parse_aes_key(raw_key)
        key_src = "media.aes_key(len=%d)" % len(raw_key)

    # 优先用 encrypt_query_param（视频只有这个能解开）
    urls = []
    param = media.get("encrypt_query_param") or ""
    if param:
        urls.append(("param", CDN_BASE + "/download?encrypted_query_param=" +
                     urllib.parse.quote(param, safe="")))
    if media.get("full_url"):
        urls.append(("full_url", media["full_url"]))
    if not urls:
        raise ValueError("media 既无 full_url 也无 encrypt_query_param")

    encrypted = None
    for name, url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read(MAX_MEDIA_BYTES + 1)
            print("下载源 %s: %d 字节" % (name, len(data)), flush=True)
            if encrypted is None:
                encrypted = data
            break  # 用第一个成功的源
        except Exception as e:
            print("下载源 %s 失败: %s" % (name, e), flush=True)
    if encrypted is None:
        raise ValueError("所有下载源均失败")
    if len(encrypted) > MAX_MEDIA_BYTES:
        raise ValueError("媒体文件超过 100MB 上限")

    try:
        plain = crypto._aes_ecb_decrypt(encrypted, key)
    except Exception as e:
        print("主解密失败(%s)，尝试其他组合…" % e, flush=True)
        raw_key = "" if aes_key_override else (media.get("aes_key") or "")
        plain, strategy = crypto.try_decrypt_variants(encrypted, raw_key, aes_key_override)
        if plain is None:
            print("所有解密组合均失败，放弃 (密钥来源=%s)" % key_src, flush=True)
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
    aes_key = os.urandom(16)
    filekey = os.urandom(16).hex()
    encrypted = crypto._aes_ecb_encrypt(data, aes_key)
    upload_media_type = {ITEM_IMAGE: UPLOAD_IMAGE, ITEM_VIDEO: UPLOAD_VIDEO}.get(
        item_type, UPLOAD_FILE)

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
        raise RuntimeError("getuploadurl 失败: %s" % str(resp)[:300])
    upload_url = (resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = resp.get("upload_param") or ""
        if not param:
            raise RuntimeError("getuploadurl 未返回上传地址")
        upload_url = (CDN_BASE + "/upload?encrypted_query_param=" +
                      urllib.parse.quote(param, safe="") +
                      "&filekey=" + urllib.parse.quote(filekey, safe=""))

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


def send_media_message(baseurl, token, to_user_id, context_token, file_path,
                       item_type, caption=""):
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
