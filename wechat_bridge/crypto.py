"""加解密：微信 CDN 媒体的 AES-128-ECB + PKCS7。

aes_key 有 4 种格式：16 字节原文 / 32 hex 字符 / base64(16字节) / base64(32hex)。
"""

import base64
import hashlib


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
    if out[-pad:] != bytes([pad]) * pad:
        raise ValueError("AES 解密 padding 校验失败")
    return out[:-pad]


def _is_hex(b):
    return all((48 <= c <= 57) or (97 <= c <= 102) or (65 <= c <= 70) for c in b)


def parse_aes_key(s):
    """解析微信 CDN 的 aes_key。base64 严格解析（标准 + URL 安全），避免静默丢字符。"""
    if isinstance(s, str):
        s = s.encode("utf-8")
    if len(s) == 16:
        return bytes(s)
    if len(s) == 32 and _is_hex(s):
        return bytes.fromhex(s.decode("ascii"))

    def _b64_try(data, altchars=None):
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


def try_decrypt_variants(encrypted, raw_key_str, key_override=None):
    """暴力尝试多种密钥派生 × 加密模式组合，返回 (明文, 策略名)。

    用于微信视频等搞不清密钥格式的场景：哪个能解出有效文件头
    （MP4 的 ftyp / JPEG 头）就用哪个。
    """
    from Crypto.Cipher import AES
    candidates = []
    raw = raw_key_str.encode("utf-8") if isinstance(raw_key_str, str) else (raw_key_str or b"")
    if key_override:
        candidates.append((key_override, "ECB", None, "override-ECB"))
    derivations = []
    if len(raw) == 16:
        derivations.append((raw, "raw16"))
    if len(raw) == 32 and _is_hex(raw):
        derivations.append((bytes.fromhex(raw.decode("ascii")), "hex32"))
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
        pad = out[-1]
        if pad < 1 or pad > 16:
            continue
        if out[-pad:] != bytes([pad]) * pad:
            continue
        plain = out[:-pad]
        if plain[:2] == b"\xff\xd8" or b"ftyp" in plain[:32] \
           or plain[:4] == b"\x00\x00\x00\x20" or plain[:8].startswith(b"\x00\x00\x00"):
            return plain, name
    return None, None
