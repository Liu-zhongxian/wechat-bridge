"""命令行入口：login / wait-login / serve / send / status。"""

import argparse
import os
import sys
import time
import urllib.parse

from .ilink import (
    DEFAULT_API, QR_MAX_REFRESH, auth_headers, fetch_qrcode, http_json,
    is_token_invalid, send_message, app_headers,
)
from .media import media_type_from_path, send_media_message
from .serve import run as serve_run
from .store import (
    Store, atomic_write_json, load_json, mark_needs_relogin, masked,
)


def get_store():
    base = os.environ.get("WECHAT_BRIDGE_DIR",
                          os.path.join(os.path.expanduser("~"), "workspace", "wechat-bridge"))
    return Store(base)


def cmd_login(args):
    store = get_store()
    qrcode, _ = fetch_qrcode(
        lambda obj: atomic_write_json(store.qr_path, obj),
        store.qr_img_path,
    )
    if not qrcode:
        return 1
    print("二维码图片已保存: %s" % store.qr_img_path)
    print("请用手机微信扫码，扫完后运行: python3 -m wechat_bridge wait-login")
    return 0


def cmd_wait_login(args):
    store = get_store()
    qr = load_json(store.qr_path)
    if not qr or not qr.get("qrcode"):
        print("没有待扫码的二维码，请先运行: python3 -m wechat_bridge login")
        return 1
    qrcode = qr["qrcode"]
    api = qr.get("api") or DEFAULT_API
    refresh_left = QR_MAX_REFRESH
    deadline = time.time() + 600
    print("等待扫码确认（最长 10 分钟，二维码过期自动刷新 %d 次）…" % QR_MAX_REFRESH)
    while time.time() < deadline:
        url = api + "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(qrcode, safe="")
        try:
            resp = http_json("GET", url, app_headers(), None, timeout=45)
        except Exception as e:
            print("轮询出错，2 秒后重试: %s" % e)
            time.sleep(2)
            continue
        status = resp.get("status")
        if status == "expired" and refresh_left > 0:
            refresh_left -= 1
            print("二维码已过期，自动刷新（剩余 %d 次）…" % refresh_left)
            qrcode, _ = fetch_qrcode(
                lambda obj: atomic_write_json(store.qr_path, obj),
                store.qr_img_path,
            )
            if not qrcode:
                print("刷新二维码失败。")
                return 1
            print("新二维码已生成: %s，请重新扫码" % store.qr_img_path)
            continue
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
                print("confirmed 但未返回 bot_token")
                return 1
            atomic_write_json(store.session_path, session)
            try:
                os.remove(store.qr_path)
            except OSError:
                pass
            for p in (store.needs_relogin_path,
                      os.path.join(store.state_dir, "notified_relogin")):
                try:
                    os.remove(p)
                except OSError:
                    pass
            print("登录成功，会话已保存（bot_token 已脱敏存放）。")
            return 0
        elif status == "expired":
            print("二维码已过期且刷新次数用完，请重新运行 login。")
            return 1
        elif status == "need_verifycode":
            print("手机上显示了验证码，请重新运行 login 并按手机提示操作。")
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


def cmd_serve(args):
    store = get_store()
    session = load_json(store.session_path)
    if not session or not session.get("bot_token"):
        print("未登录，请先运行 login / wait-login。")
        return 1
    if os.path.exists(store.needs_relogin_path):
        print("检测到 NEEDS_RELOGIN 标记，token 已失效，请重新扫码登录。")
        return 1
    baseurl = (session.get("baseurl") or DEFAULT_API).rstrip("/")
    return serve_run(store, baseurl, session["bot_token"], session.get("ilink_user_id"))


def _send_target(store, session, args):
    ctx = load_json(store.last_ctx_path) or {}
    to_user_id = args.to or ctx.get("to_user_id") or session.get("ilink_user_id")
    context_token = args.context or ctx.get("context_token") or ""
    return to_user_id, context_token


def cmd_send(args):
    store = get_store()
    session = load_json(store.session_path)
    if not session or not session.get("bot_token"):
        print("未登录，请先运行 login / wait-login。")
        return 1
    if os.path.exists(store.needs_relogin_path):
        print("token 已失效，请重新扫码登录。")
        return 1
    if not args.text and not args.image and not args.file and not args.video:
        print("请用 --text / --image / --file / --video 指定内容。")
        return 1
    to_user_id, context_token = _send_target(store, session, args)
    baseurl = (session.get("baseurl") or DEFAULT_API).rstrip("/")
    media_path = args.image or args.file or args.video
    try:
        if media_path:
            if not os.path.exists(media_path):
                print("文件不存在: %s" % media_path)
                return 1
            resp = send_media_message(baseurl, session["bot_token"], to_user_id,
                                      context_token, media_path,
                                      media_type_from_path(media_path),
                                      caption=args.text or "")
        else:
            resp = send_message(baseurl, session["bot_token"], to_user_id,
                                args.text, context_token)
    except Exception as e:
        print("发送出错: %s" % e)
        return 1
    if is_token_invalid(resp):
        mark_needs_relogin(store, "send 返回 -14，token 失效")
        print("token 失效，已写 NEEDS_RELOGIN 标记，请重新扫码。")
        return 1
    print("发送结果: %s" % str(resp)[:300])
    return 0


def cmd_status(args):
    store = get_store()
    session = load_json(store.session_path)
    print("== 登录状态 ==")
    if session and session.get("bot_token"):
        print("已登录  bot_id=%s  user_id=%s"
              % (session.get("ilink_bot_id"), session.get("ilink_user_id")))
        print("baseurl=%s  登录时间=%s" % (session.get("baseurl"), session.get("saved_at")))
        print("bot_token=%s（已脱敏）" % masked(session.get("bot_token")))
    else:
        print("未登录（请先运行 login / wait-login）")
    if os.path.exists(store.needs_relogin_path):
        with open(store.needs_relogin_path, encoding="utf-8") as f:
            print("!! 需要重新扫码登录: %s" % f.read().strip())
    print("== 运行状态 ==")
    pid = None
    if os.path.exists(store.pid_path):
        with open(store.pid_path, encoding="utf-8") as f:
            pid = f.read().strip()
    alive = bool(pid and pid.isdigit())
    if alive:
        try:
            os.kill(int(pid), 0)
        except OSError:
            alive = False
    print("serve: %s" % ("运行中 (pid %s)" % pid if alive else "未运行"))
    try:
        inbox_n = len([f for f in os.listdir(store.inbox_dir) if f.endswith(".json")])
    except OSError:
        inbox_n = 0
    try:
        pending_n = len([f for f in os.listdir(store.pending_dir) if f.endswith(".json")])
    except OSError:
        pending_n = 0
    print("inbox: %d 条，outbox/pending: %d 条待发送" % (inbox_n, pending_n))
    ctx = load_json(store.last_ctx_path)
    print("最近会话: %s" % ("有" if ctx else "无（需先在微信里发一条消息）"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="微信 iLink / Clawbot Bot 桥接")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="获取登录二维码")
    sub.add_parser("wait-login", help="轮询扫码状态完成登录")
    sub.add_parser("serve", help="常驻收发消息")
    sp = sub.add_parser("send", help="发送消息")
    sp.add_argument("--text", default="", help="消息文本（发媒体时作为配文）")
    sp.add_argument("--image", default=None, help="图片路径")
    sp.add_argument("--file", default=None, help="文件路径")
    sp.add_argument("--video", default=None, help="视频路径")
    sp.add_argument("--to", default=None, help="目标用户 ID（默认用缓存的）")
    sp.add_argument("--context", default=None, help="context_token（默认用缓存的）")
    sub.add_parser("status", help="查看登录和运行状态")
    args = p.parse_args(argv)
    fn = {
        "login": cmd_login,
        "wait-login": cmd_wait_login,
        "serve": cmd_serve,
        "send": cmd_send,
        "status": cmd_status,
    }
    return fn[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
