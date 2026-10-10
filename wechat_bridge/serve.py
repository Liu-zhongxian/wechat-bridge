"""常驻服务：长轮询收消息 → inbox/；outbox/pending/ → sendmessage 发出。

指数退避：失败 2 秒重试，连续 3 次失败后退避到 30 秒（学 wx-bot）。
"""

import os
import time

from .ilink import (
    ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE,
    auth_headers, http_json, is_token_invalid, send_message,
)
from .media import download_media, media_type_from_path, send_media_message
from .store import (
    Store, atomic_write_json, atomic_write_text, load_json,
    mark_needs_relogin,
)


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


def handle_incoming(store, msg, owner_id):
    """处理一条收到的消息，写入 inbox/。"""
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
        # 非文本消息：图片/视频/文件下载到本地
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
                    fname = "".join(c for c in fname if c not in "/\\")
                    dest_name = "%s_%s" % (msg.get("message_id"), fname)
                else:
                    dest_name = "%s_%s.%s" % (msg.get("message_id"), kind, ext)
                day = time.strftime("%Y%m%d")
                dest = os.path.join(store.media_dir, day, dest_name)
                try:
                    download_media(sub.get("media") or {}, key, dest)
                    media_paths.append(dest)
                    print("已下载%s -> %s" % (kind, dest), flush=True)
                except Exception as e:
                    m = sub.get("media") or {}
                    print("%s下载失败: %s | encrypt_type=%s full_url=%s" % (
                        kind, e, m.get("encrypt_type"), bool(m.get("full_url"))), flush=True)
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
    path = os.path.join(store.inbox_dir, "%s.json" % message_id)
    if not os.path.exists(path):
        atomic_write_json(path, record)
        print("收到消息 [%s]: %s" % (message_id, text[:60]), flush=True)
    # 缓存最新会话上下文，供 send 测试用
    atomic_write_json(store.last_ctx_path, {
        "to_user_id": from_id,
        "context_token": record["context_token"],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def flush_outbox(store, baseurl, token):
    """把 outbox/pending/ 里的回复发出去，发完移到 outbox/sent/。

    任务支持两种格式：
    - 文本：{"to_user_id", "text", "context_token", "message_id"}
    - 媒体：{"to_user_id", "text"(配文), "media_path", "context_token", "message_id"}
    """
    try:
        files = sorted(f for f in os.listdir(store.pending_dir) if f.endswith(".json"))
    except OSError:
        return True
    for name in files:
        path = os.path.join(store.pending_dir, name)
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
            mark_needs_relogin(store, "sendmessage 返回 -14，token 失效")
            print("sendmessage 返回 -14，token 失效。", flush=True)
            return False
        if resp.get("ret") not in (0, None):
            print("发送返回异常，稍后重试: %s" % str(resp)[:200], flush=True)
            continue
        os.replace(path, os.path.join(store.sent_dir, name))
        print("已发送回复 -> %s" % name, flush=True)
    return True


def run(store, baseurl, token, owner_id):
    """常驻收发主循环。返回 0 正常退出，1 异常退出。"""
    atomic_write_text(store.pid_path, str(os.getpid()))
    buf = ""
    if os.path.exists(store.buf_path):
        with open(store.buf_path, encoding="utf-8") as f:
            buf = f.read().strip()
    print("serve 启动，监听用户 %s 的消息…" % owner_id, flush=True)
    consec_failures = 0
    poll_timeout = 45
    try:
        while True:
            if os.path.exists(store.needs_relogin_path):
                print("NEEDS_RELOGIN 存在，退出 serve。", flush=True)
                return 1
            url = baseurl + "/ilink/bot/getupdates"
            body = {"get_updates_buf": buf, "base_info": {"channel_version": "2.4.8", "bot_agent": "Muse"}}
            try:
                resp = http_json("POST", url, auth_headers(token), body, timeout=poll_timeout)
            except Exception as e:
                consec_failures += 1
                delay = 30 if consec_failures >= 3 else 2
                print("getupdates 出错，%d 秒后重试 (连续失败 %d 次): %s"
                      % (delay, consec_failures, e), flush=True)
                time.sleep(delay)
                continue
            consec_failures = 0
            try:
                suggested = int(resp.get("longpolling_timeout_ms") or 0)
                if suggested > 0:
                    poll_timeout = min(suggested // 1000 + 10, 120)
            except (TypeError, ValueError):
                pass
            if not isinstance(resp, dict):
                resp = {}
            if is_token_invalid(resp):
                # -14 也可能是限流：先暂停 5 分钟重试一次，仍失败才判 token 死亡
                print("收到 -14，暂停 5 分钟后重试一次…", flush=True)
                time.sleep(300)
                try:
                    resp = http_json("POST", url, auth_headers(token), body, timeout=poll_timeout)
                except Exception:
                    resp = {}
                if is_token_invalid(resp):
                    mark_needs_relogin(store, "getupdates 返回 -14，token 失效")
                    print("token 确认失效，已写 NEEDS_RELOGIN 标记，退出。", flush=True)
                    return 1
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                buf = new_buf
                atomic_write_text(store.buf_path, buf)
            for msg in resp.get("msgs") or []:
                try:
                    handle_incoming(store, msg, owner_id)
                except Exception as e:
                    print("处理消息出错: %s" % e, flush=True)
            if not flush_outbox(store, baseurl, token):
                return 1
    except KeyboardInterrupt:
        print("收到中断，退出 serve。", flush=True)
        return 0
    finally:
        try:
            os.remove(store.pid_path)
        except OSError:
            pass
