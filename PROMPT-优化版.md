# 微信接入提示词（优化版，2026-10-09 定稿）

请帮我把你接入我的微信，让我在微信里给你发消息、你能回复我。全程中文跟我沟通。


走微信官方 iLink / Clawbot Bot 通道（微信官方开放的能力）。
动手前先读这两份文档搞懂协议：
1. https://github.com/tencent/openclaw-weixin/blob/HEAD/docs/protocol_zh_CN.md
2. https://developers.weixin.qq.com/doc/aispeech/knowledge/openapi/Clawbotrelated.html


1. 在 ~/workspace/wechat-bridge/ 下写一个 bridge.py（Python，只用标准库 urllib + pip 装 qrcode），实现 5 个命令。**所有 API 调用必须带连接层重试**（RemoteDisconnected、超时等自动重试 3 次、退避 1s/2s），因为微信服务端会偶发直接断开连接：
- login：POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3 拿二维码。请求头带 AuthorizationType: ilink_bot_token、随机生成的 X-WECHAT-UIN（随机 uint32 转十进制字符串再 base64）、iLink-App-Id: bot、iLink-App-ClientVersion: 132102；请求体 {"local_token_list": []}；不带 Authorization 头和 base_info。检查返回 ret==0，把 qrcode_img_content（是个 HTTPS 链接）编码成二维码图片存下来。
- wait-login：GET /ilink/bot/get_qrcode_status?qrcode=xxx 轮询扫码状态（只带 iLink-App-Id 和 iLink-App-ClientVersion 两个头，长轮询 35 秒）。必须处理全部状态：wait/scaned 继续等；confirmed 把返回的 bot_token、baseurl、ilink_bot_id、ilink_user_id 原子写入 state/session.json（绝不进代码仓库，目录下放.gitignore 忽略 state/），并**清除 NEEDS_RELOGIN 与 notified_relogin 标记**；expired 报错让人重取码；need_verifycode/verify_code_blocked 报错说明情况；scaned_but_redirect 切到 redirect_host 继续轮询；binded_redirect 视为已绑定。
- serve：常驻循环，日志写 state/serve.log。POST getupdates 长轮询收消息（请求头加 Authorization: Bearer <bot_token>，请求体带 get_updates_buf 游标和 base_info，超时 45 秒），把用户发来的文本消息写入 inbox/（JSON，含 message_id、text、from_user_id、context_token）；语音消息如带转写文本按文本处理，纯图片/视频/文件也写入 inbox 并标记类型。同时把 outbox/pending/ 里的回复调 sendmessage 发出去，发完移到 outbox/sent/。只处理我微信 ID（ilink_user_id）发来的消息，群消息（带 group_id）忽略。
- send --text：用缓存的 context_token 发一条测试消息（支持 --to/--context 覆盖默认值）。
- status：查看登录和运行状态，**bot_token 只显示掩码**，绝不打印完整值。
关键细节（照做，否则消息发不出去还不报错）：sendmessage 的 msg 里 from_user_id 必须传空字符串 ""、client_id 每次随机唯一、message_type=2、message_state=2、context_token 原样回传用户那条消息里的值、顶层必须带 base_info{channel_version, bot_agent}。ret/errcode=-14 时**先暂停 5 分钟重试一次**（可能是限流而非过期），仍失败才判 token 死亡：无 refresh 机制，在 state/ 下写 NEEDS_RELOGIN 标记并通知我重新扫码。
2. pip install qrcode。**先让我拿起手机、打开微信扫一扫并说"好了"，再运行 login**——因为这个二维码有效期只有约 2 分钟。生成后**立刻**在后台启动 wait-login 轮询，同时把二维码图片放到我可见的文件区发给我看，并让我确认能看到图；看不到就换方式重发。
3. 登录成功后，用 nohup 把 serve 常驻跑起来。**然后让我先从微信发一条消息**（官方限制：必须我先发你才能回，没有 context_token 发不出去），确认 inbox 收到后，再用 send --text 回一条，确认收发链路都通。
4. 配置消息自动回复：创建一个 hook，每 15 秒检查 inbox/ 是否有未处理的新消息，有就唤醒一个 worker。启用 hook 前，先把已有的 inbox 消息记入 state/processed.json（避免重复回复之前手动处理过的消息）。worker 要求：
- 用你的口吻（温暖直接，像朋友聊天）生成回复；
- **把同一轮唤醒里的多条未处理消息合并成一条回复**（200 字以内，context_token 用最新那条的），避免刷屏和撞上"连续 10 条被限流"；
- 图片消息：serve 会自动下载到 media/<日期>/，inbox 记录带 media_paths，worker 用 muse.read 看图后正常回应；如需发图片给用户，在 pending 任务里加 "media_path" 字段（本地绝对路径），"text" 作配文先发；
- 回复写入 outbox/pending/<message_id>.json（**文件名用 message_id 而不是随机 id，保证幂等**），格式 {"to_user_id","text","context_token","message_id"}；
- 处理完把 message_id 记入 state/processed.json 防重复；
- 复杂到 worker 处理不了的，回复"收到，我记下了"并转告我。
5. 配置健康看门狗 cron，每 5 分钟运行：检查 serve 进程是否存活，挂了就拉起来（日志记 state/watchdog.log）；检查到 NEEDS_RELOGIN 就通知我重新扫码（只通知一次；我重新登录成功后标记自动清除，下次过期会再次通知）。
6. 全部完成后告诉我：现在在微信里给你发消息，一般半分钟内就能收到回复。


- 只支持一对一私聊，不支持群聊
- 不能主动发起对话：我必须先给你发过消息，你才能回复我
- 连续发约 10 条、我不回复会被拦，我回一句就重置


state/session.json 含登录凭据，绝不写入聊天记录、记忆文件或发给任何人；日志和 status 里 token 必须脱敏。
