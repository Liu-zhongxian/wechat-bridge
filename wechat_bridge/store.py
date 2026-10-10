"""状态管理：路径、原子读写、会话。

凭据只存 state/session.json（0600），绝不进 git。
"""

import json
import os
import time


class Store:
    """桥接的数据目录布局。"""

    def __init__(self, base_dir):
        self.base = os.path.abspath(base_dir)
        self.state_dir = os.path.join(self.base, "state")
        self.inbox_dir = os.path.join(self.base, "inbox")
        self.pending_dir = os.path.join(self.base, "outbox", "pending")
        self.sent_dir = os.path.join(self.base, "outbox", "sent")
        self.media_dir = os.path.join(self.base, "media")
        for d in (self.state_dir, self.inbox_dir, self.pending_dir,
                  self.sent_dir, self.media_dir):
            os.makedirs(d, exist_ok=True)

        self.session_path = os.path.join(self.state_dir, "session.json")
        self.qr_path = os.path.join(self.state_dir, "qr.json")
        self.buf_path = os.path.join(self.state_dir, "get_updates_buf.txt")
        self.pid_path = os.path.join(self.state_dir, "serve.pid")
        self.last_ctx_path = os.path.join(self.state_dir, "last_context.json")
        self.needs_relogin_path = os.path.join(self.state_dir, "NEEDS_RELOGIN")
        self.processed_path = os.path.join(self.state_dir, "processed.json")
        self.qr_img_path = os.path.join(self.base, "qrcode.png")


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


def mark_needs_relogin(store, reason):
    atomic_write_text(
        store.needs_relogin_path,
        "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason),
    )


def masked(s):
    """脱敏显示 token。"""
    s = s or ""
    return s[:4] + "****" + s[-4:] if len(s) > 12 else "****"
