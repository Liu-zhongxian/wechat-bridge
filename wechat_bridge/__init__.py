"""微信官方 iLink / Clawbot Bot 桥接。

把微信消息接到你的个人 AI 助手，支持文本、图片、语音、视频、文件双向收发。
"""

__version__ = "0.2.0"

from .ilink import DEFAULT_API, CDN_BASE, split_text
from .crypto import parse_aes_key
from .media import download_media

__all__ = [
    "__version__",
    "DEFAULT_API",
    "CDN_BASE",
    "parse_aes_key",
    "split_text",
    "download_media",
]
