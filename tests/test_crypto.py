"""加解密与文本分片的单元测试。"""

import base64
import hashlib
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wechat_bridge.crypto import (
    _aes_ecb_encrypt, _aes_ecb_decrypt, parse_aes_key, try_decrypt_variants,
)
from wechat_bridge.ilink import split_text


def test_aes_roundtrip():
    key = bytes(range(16))
    for n in (1, 15, 16, 17, 100, 1000):
        data = bytes(i % 256 for i in range(n))
        assert _aes_ecb_decrypt(_aes_ecb_encrypt(data, key), key) == data


def test_parse_aes_key_formats():
    raw16 = b"0123456789abcdef"
    assert parse_aes_key(raw16) == raw16
    assert parse_aes_key("0123456789abcdef0123456789abcdef") == bytes.fromhex(
        "0123456789abcdef0123456789abcdef")
    assert parse_aes_key(base64.b64encode(raw16).decode()) == raw16
    assert parse_aes_key(base64.b64encode(
        b"0123456789abcdef0123456789abcdef").decode()) == bytes.fromhex(
        "0123456789abcdef0123456789abcdef")
    # URL 安全 base64（无 padding）
    urlsafe = base64.urlsafe_b64encode(raw16).decode().rstrip("=")
    assert parse_aes_key(urlsafe) == raw16


def test_parse_aes_key_rejects_garbage():
    for bad in ("!!!not-valid!!!", "short", "z" * 44):
        try:
            parse_aes_key(bad)
        except ValueError:
            continue
        raise AssertionError("应拒绝非法输入: %s" % bad)


def test_try_decrypt_variants_md5():
    plain = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
    raw32 = bytes(range(32))
    key = hashlib.md5(raw32).digest()
    ct = _aes_ecb_encrypt(plain, key)
    found, strategy = try_decrypt_variants(
        ct, base64.b64encode(raw32).decode())
    assert found == plain, strategy


def test_try_decrypt_variants_aes256():
    from Crypto.Cipher import AES
    plain = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
    raw32 = bytes(range(32))
    pad = 16 - len(plain) % 16
    ct = AES.new(raw32, AES.MODE_ECB).encrypt(plain + bytes([pad]) * pad)
    found, strategy = try_decrypt_variants(
        ct, base64.b64encode(raw32).decode())
    assert found == plain, strategy


def test_split_text():
    assert split_text("hello") == ["hello"]
    long = "中" * 5000
    chunks = split_text(long)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [2000, 2000, 1000]
    assert "".join(chunks) == long


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("全部通过")
