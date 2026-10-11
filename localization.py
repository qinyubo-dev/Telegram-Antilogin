# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

"""Chinese text catalog for user-facing Bot text."""

from __future__ import annotations

from typing import Dict



ZH: Dict[str, str] = {
    "common.back": "返回",
    "common.previous": "⬅️ 上一页",
    "common.next": "下一页 ➡️",
    "common.no_access": "❌ 您没有使用权限",
    "common.starting": "⏳ 系统正在恢复托管账户，请稍后再试。",
    "common.user": "用户",
    "main.add_account": "添加账户",
    "main.login_unlock": "解限提醒",
    "main.more": "更多功能",
    "main.more_text": "更多功能\n\n请选择需要使用的功能：",
    "main.account_list": "账户列表",
    "main.hosting_tools": "托管工具",
    "main.vip_center": "VIP中心",
    "main.transfer_account": "转让账户",
    "main.reload": "重新加载",
    "main.antilogin": "反登录设置",
    "main.support": "客服支持",
    "main.help": "帮助信息",
    "main.admin": "管理面板",
    "main.text": (
        "🛡️ 𝑨𝒏𝒕𝒊𝒍𝒐𝒈𝒊𝒏\n\n"
        "**🆔 用户信息**\n"
        "├ {name} · `{user_id}`\n\n"
        "**⚙️ 运行状态**\n"
        "• 托管账户  {quota}\n"
        "• 在线账户  {online} 个\n\n"
        "**◉ 操作面板**\n"
        "选择下方功能，开启尊享体验"
    ),
    "main.quota_unlimited": "∞",
    "main.quota_count": "{count} 个",
    "access.text": (
        "💵 𝐕𝐈𝐏 𝐀𝐜𝐜𝐞𝐬𝐬\n\n"
        "未开通 VIP · 𝐀𝐜𝐜𝐞𝐬𝐬 𝐃𝐞𝐧𝐢𝐞𝐝\n"
        "🆔 `{user_id}`\n\n"
        "开通后可使用：\n"
        "· 🛡️ 实时反登录防护\n"
        "· 👥 多账户托管管理\n"
        "· 🔄 自动运行与稳定维护\n\n"
        "期待为您提供尊享服务。"
    ),
    "access.buy": "开启尊享订阅",
    "support.contact": "联系客服",
    "support.unavailable": "客服资料暂时无法加载，请稍后重试。",
    "support.pointer": "👇",
    "start.qr_cleanup_failed": "二维码消息清理失败，请再次发送 /start 重试。",
    "start.session_releasing": "旧登录会话正在释放中，请稍后重新添加账户。",
    "back.cleanup_failed": "二维码消息清理失败，请点击返回重试",
    "help.text": (
        "📉 使用指南\n\n"
        "① 添加账户\n"
        "• 手机号登录：按提示输入手机号、验证码和二级密码\n"
        "• 扫码登录：使用 Telegram 客户端扫码授权\n"
        "• 上传 session：可直接发送 .session 或 .zip 文件\n\n"
        "② 账户列表\n"
        "• 查看已添加账户的防护状态及最后加载时间\n\n"
        "③ 托管工具\n"
        "• 踢出其他会话、获取验证码和管理二级密码\n"
        "• 清理账户：删除全部对话、联系人，或执行全部清理\n"
        "• 清理操作不可恢复，请确认账户后再执行\n\n"
        "④ 转让账户\n"
        "• 将符合条件的账户转给其他 VIP 用户\n"
        "• 首次创建满 24 小时后可转让；每次转让间隔 1 小时\n"
        "• 上传 Session 添加的账户不可转让，转让不可撤销\n\n"
        "⑤ 反登录设置\n"
        "• 为指定账户开启或暂停防护\n"
        "• 删除会销毁 Session 并移除托管账户，且不可撤销\n\n"
        "⑥ 重新加载\n"
        "• 立即刷新账户运行状态\n\n"
        "⑦ 登录解限提醒\n"
        "• 登录受限时自动读取官方秒数，也可手动检测并管理提醒\n"
        "• 使用独立监听额度，不占托管账户配额\n\n"
        "⑧ VIP中心\n"
        "• 查看状态、开通、续费和权益说明\n\n"
        "⑨ 客服支持\n"
        "• 点击主菜单的客服支持，或发送 /support\n\n"
        "由秦屿泊（@qinyubo）开发并开源，所有人均可依照 MIT License 免费使用和二次开发。"
    ),
    "accounts.empty": "📭 暂无托管账户",
    "accounts.title": "📋 您的账户：\n\n",
    "accounts.last_reload": "最后加载：{time}\n\n",
    "account.add.choose": "请选择添加账户方式：",
    "account.add.phone": "手机号登录",
    "account.add.qr": "扫码登录",
    "account.add.upload": "上传 session",
    "account.phone.prompt": "📱 请输入手机号 (如+8612345678900 或 +1 234 567 8888):",
    "account.phone.invalid": "❌ 手机号格式无效。\n\n示例：\n• +15676548502\n• +1 567 654 8502\n• +44 7911 123456\n\n请重新输入：",
    "account.qr.generating": "正在生成扫码登录二维码，请稍候…",
    "account.qr.instructions": "🖥 请使用 Telegram 手机客户端扫码。\n\n路径：设置 > 设备 > 添加设备\n二维码有效期较短。",
    "account.qr.failed": "❌ 生成二维码失败：{error}",
    "account.qr.login_failed": "❌ 扫码登录失败：{error}",
    "account.qr.no_phone": "❌ 已扫码，但无法读取手机号。请改用手机号登录或上传 session。",
    "account.qr.success": "✅ 扫码登录成功\n\n📱 手机号：{phone}\n🔐 反登录监控：开启\n⏱ 加载时间：{time}",
    "account.qr.password": "🔐 账户已启用两步验证，请输入二级密码：",
    "account.qr.expired": "⏱ 二维码已过期，请重新发起扫码登录。",
    "account.upload.prompt": "📤 请发送 .session 或 .zip 文件",
    "account.client_failed": "❌ 创建客户端失败：{error}",
    "auth.already_logged_in": "✅ 账户已登录！",
    "auth.code_sent": "📱 验证码已发送，请输入您收到的验证码：\n\n⚠️ 注意：验证码通常在 5 分钟内有效",
    "auth.rate_limited": "⏳ 请求过于频繁，请等待 {seconds} 秒后再试",
    "auth.login_code_rate_interval_exceeded": "⏳ 登录验证码请求过于频繁，请 {seconds} 秒后重试。\n\n{rule}",
    "auth.login_code_rate_window_exceeded": "⏳ 已达到登录验证码请求次数上限，请 {seconds} 秒后重试。\n\n{rule}",
    "auth.login_code_rate_storage_failed": "❌ 暂时无法保存登录验证码请求限制，请稍后重试。此次没有请求 Telegram。",
    "auth.login_code_rate_rule": "当前规则：每 {interval} 秒最多 1 次，滚动 {window} 最多 {max_requests} 次。",
    "auth.login_code_rate_window_minutes": "{value} 分钟",
    "auth.login_code_rate_window_seconds": "{value} 秒",
    "auth.send_code_failed": "❌ 发送验证码失败：{error}",
    "auth.failed": "❌ 认证失败：{error}",
    "auth.code_request_expired": "❌ 没有等待验证码的请求或请求已过期",
    "auth.code_attempts_exhausted": "❌ 验证码尝试次数过多，请重新开始登录流程",
    "auth.code_format_invalid": "❌ 验证码格式无效，请输入纯数字验证码（剩余尝试次数：{remaining}）",
    "auth.hosting_init_failed": "❌ 登录成功但托管初始化失败：{error}",
    "auth.login_success": "✅ {phone} 登录成功！",
    "auth.password_required": "🔐 账户已启用两步验证，请输入您的密码：",
    "auth.code_invalid": "❌ 验证码无效，请重新输入（剩余尝试次数：{remaining}）",
    "auth.code_expired": "❌ 验证码已过期，请重新开始登录流程",
    "auth.code_empty": "❌ 验证码不能为空，请输入有效的验证码",
    "auth.operation_rate_limited": "⏳ 操作过于频繁，请等待 {seconds} 秒后再试",
    "auth.login_failed_retry": "❌ 登录失败：{error}（剩余尝试次数：{remaining}）",
    "auth.login_failed_exhausted": "❌ 登录失败且尝试次数已用完：{error}",
    "auth.password_request_expired": "❌ 没有等待密码的请求或请求已过期",
    "auth.password_empty": "❌ 二级密码不能为空",
    "auth.password_attempts_exhausted": "❌ 二级密码尝试次数过多，请重新开始登录流程",
    "auth.password_invalid": "❌ 二级密码错误，请重新输入（剩余尝试次数：{remaining}）",
    "auth.phone_unavailable": "❌ 登录成功，但无法读取账号手机号，请改用手机号登录或上传 session。",
    "auth.password_init_failed": "❌ 二级密码验证通过，但托管初始化失败：{error}",
    "login_unlock.offset_seconds": "{value} 秒",
    "login_unlock.offset_minutes": "{value} 分钟",
    "login_unlock.unlimited": "无限",
    "login_unlock.reminder_time_line": "🔔 提醒时间：{time}",
    "login_unlock.scheduled": "⏳ 登录解限提醒\n\n📱 账户：`{phone}`\n⏱ 官方限制：{seconds} 秒\n🕒 解限时间：{unlock}\n{reminders}\n📊 监听用量：{quota}",
    "login_unlock.immediate": "⏳ 登录解限提醒\n\n📱 账户：`{phone}`\n⏱ 官方限制：{seconds} 秒\n🕒 解限时间：{unlock}\n\n已进入最近一次提醒窗口，请在解限后重试。本次不占监听位。",
    "login_unlock.full": "⏳ 登录解限提醒\n\n📱 账户：`{phone}`\n⏱ 官方限制：{seconds} 秒\n🕒 解限时间：{unlock}\n\n❌ 独立监听额度已满（{quota}），未建立提醒。请进入管理页取消不需要的监听。",
    "login_unlock.save_failed": "❌ 解限时间为 {unlock}，但提醒保存失败，请稍后重试。",
    "login_unlock.unavailable": "❌ 登录解限提醒服务暂不可用，请稍后重试。",
    "login_unlock.manual_available": "✅ 当前没有登录验证码限制\n\n📱 账户：`{phone}`\n验证码可能已经发送。本次检测已结束，没有进入登录流程。",
    "login_unlock.manual_failed": "❌ 检测登录验证码限制失败，请稍后重试。",
    "login_unlock.manual_remove_failed": "❌ 当前没有登录验证码限制，但旧提醒清理失败，请进入管理页手动取消。",
    "login_unlock.notification": "⏳ 登录即将解限\n\n📱 账户：`{phone}`\n⏱ 距离解限：{remaining}\n🕒 解限时间：{unlock}\n\n请在解限后重新请求登录验证码。",
    "login_unlock.manual_add": "添加",
    "login_unlock.list_node": "🔔 将在解限前 {remaining}提醒您",
    "login_unlock.list_item": "📱 `{phone}`\n🕒 解限：{unlock}\n\n{nodes}",
    "login_unlock.account_button": "📱 {phone}",
    "login_unlock.cancel_item": "取消提醒",
    "login_unlock.menu": "⏳ 登录解限提醒\n\n时区：中国标准时间（UTC+8）\n独立监听额度：{used} / {limit}\n监听不占用托管账户配额。\n\n{items}",
    "login_unlock.empty": "暂无等待中的解限提醒。",
    "login_unlock.choose_account": "请选择账户查看提醒详情。",
    "login_unlock.not_found": "该提醒不存在或已经结束。",
    "login_unlock.cancel_failed": "❌ 取消失败，请稍后重试。",
    "login_unlock.cancelled": "✅ 已取消解限提醒",
    "login_unlock.manual_prompt": "⏳ 检测登录限制\n\n请发送需要检测的手机号。\n\n⚠️ 系统会真实请求一次 Telegram 登录验证码；若当前没有限制，验证码可能会发送到该账户。",
    "login_unlock.manual_full": "❌ 独立监听额度已满（{used} / {limit}）。请先取消其他监听。",
    "login_unlock.manual_checking": "⏳ 正在读取 Telegram 官方登录限制…",
    "account.upload.too_large": "❌ `{file}` 超过 {limit} 大小限制。",
    "account.upload.releasing": "❌ 旧登录会话正在释放中，请稍后重新上传。",
    "account.upload.processing": "📥 正在处理上传文件…",
    "account.upload.success": "✅ Session 上传成功\n\n📱 手机号：{phone}\n🔐 反登录监控：{status}\n⏰ 加载时间：{time}",
    "account.upload.on": "开启",
    "account.upload.off": "关闭",
    "account.upload.no_info": "✅ Session 上传成功，但未能获取账户信息",
    "account.upload.invalid_format": "❌ `{file}` 不是有效的 Session 文件。\n\n请发送兼容的 SQLite `.session` 文件；不支持 StringSession 文本。",
    "account.upload.busy": "❌ 现有账户会话正在使用中，请稍后重新上传。",
    "account.upload.restore_ok": "❌ 新 Session 加载失败，原有 Session 已恢复，请稍后重试。",
    "account.upload.restore_failed": "❌ Session 替换失败，原有会话未能恢复，请联系管理员。",
    "account.upload.invalid": "❌ Session 文件无效或已过期。请检查文件、账户状态和网络连接。",
    "account.upload.failed": "❌ 上传失败：{error}",
    "vip.admin_status": "👑 管理员权限",
    "vip.active_status": "{badge} {plan} · 生效中\n到期时间：{date}",
    "vip.inactive_status": "✦ 未开通 VIP",
    "vip.benefits_current": "当前权益：",
    "vip.benefits_available": "开通后可使用：",
    "vip.features": "· 🛡️ 实时反登录防护\n· 🧾 多账户托管管理\n· ⏳ 登录解限提醒\n· 🔄 自动运行与稳定维护",
    "vip.thanks": "感谢您使用尊享服务。",
    "vip.invite": "期待为您提供尊享服务。",
    "vip.upgrade": "尊享订阅 · 升级续期",
    "vip.select_accounts": "选择启用账户",
    "vip.center_text": "💎 VIP中心\n\n{status}\n🆔 `{user_id}`\n\n{heading}\n{features}\n\n{closing}",
    "subscription.no_selection": "当前订阅无需选择账户",
    "subscription.save": "保存并应用",
    "subscription.selection": "请选择要启用监控的账户\n\n已选择 {selected} / {quota}\n未选择的 Session 会保留，但不会连接或运行监控。",
    "subscription.changed": "订阅状态已变化",
    "subscription.limit": "已达到套餐配额上限",
    "subscription.save_failed": "保存选择失败，请重试",
    "subscription.empty": "请至少选择一个账户后再保存",
    "subscription.auto_selected": "您的订阅最多可托管 {quota} 个账户，但超过 24 小时未选择，系统已为您随机启用以下账户：\n{accounts}\n\n其余账户会保留但保持离线，您可随时在 VIP 中心重新选择。",
    "subscription.applied": "已应用，恢复 {count} 个账户",
    "reload.start": "🔄 开始重新加载账户…",
    "reload.done": "📁 重新加载完成\n\n✅ 存活：{alive}\n❄️ 冻结：{frozen}\n❌ 失效：{dead}",
    "reload.failed": "❌ 重新加载失败：{error}",
    "reminder.today": "💎 VIP 到期提醒\n\n您的订阅将在今天到期。\n到期时间：{expiry}",
    "reminder.soon": "💎 VIP 到期提醒\n\n剩余 {days} 天\n到期时间：{expiry}",
    "notify.offline": "⚠️ 托管连接暂时离线\n\n📞 账户：`{phone}`\n原因：{reason}\n\nSession 已保留，请稍后重新加载或重新添加账户。",
    "notify.code_mode_ended": "⏱ 获取验证码模式已结束\n\n📞 账户：`{phone}`\n🛡️ 反登录转发已恢复",
    "notify.protection_resumed": "✅ {phone} 的反登录监控已自动开启",
    "notify.transfer_received": "📥 收到账户转让\n📱 账户：{phone}\n👤 转出用户：{source}\n账户已进入您的托管列表。",
    "transfer.success": "✅ 账户转让成功\n\n📱 账户：`{phone}`\n👤 接收用户：`{target}`\n\n账户会话与关联凭据已转移。",
    "notify.login_code": "🔢 验证码：`{code}`\n📞 账户：`{phone}`\n⏰ 时间：{time}",
    "notify.login_blocked": "🚨 异常登录拦截提醒\n\n⚠️ 发现异常登录行为\n🛡️ 系统已拦截验证码，登录请求强制失效\n\n🔢 验证码：`{code}`\n📞 目标账户：`{phone}`\n⏰ 拦截时间：{time}",
    "notify.login_block_failed": "🚨 无法拦截异常登录\n\n请立即检查登录活动。\n🔢 验证码：`{code}`\n📞 账户：`{phone}`\n⏰ 时间：{time}",
    "device.unknown": "未知设备",
    "device.location_unknown": "未知位置",
    "device.prompt": "🔐 新设备登录\n\n📱 账户：`{phone}`\n💻 设备：{device}\n📍 位置：{location}\n⏰ 时间：{time}\n\n{suffix}",
    "device.choose": "请选择是否允许此设备登录。",
    "device.allow": "允许",
    "device.deny": "拒绝",
    "device.auto_denied": "🛡️ 反登录防护已自动拒绝并踢出该设备。",
    "device.expired": "该设备已经退出登录或请求已失效。",
    "device.allowed": "✅ 已允许此设备登录",
    "device.denied": "🚫 已拒绝并踢出此设备",
    "device.frozen": "❄️ 该账号已被冻结，Telegram 暂时禁止允许或拒绝新设备。请求已保留，解冻后可再次处理。",
    "protection.unknown": "未知",
    "protection.offline": "⚠️ 防护未生效 · 连接中断",
    "protection.paused": "⏸️ 暂停中 · 剩余{minutes}分钟",
    "protection.code_mode": "🔵 验证码获取中",
    "protection.active": "🛡️ 防护中",
    "protection.off": "⚪ 防护已关闭",
    "protection.enabled": "✅ 已开启反登录监控",
    "protection.pause_set": "⏸ 已暂停 {minutes} 分钟（到期自动开启）",
    "protection.no_account": "❌ 账户不存在或未托管",
    "protection.empty": "❌ 暂无账户",
    "protection.list": "🔐 选择要操作的账户：\n共 {count} 个账户",
    "protection.detail": "📱 **账户：** `{phone}`\n**防护状态：** {status}\n\n请选择操作：",
    "protection.enable": "✅ 开启",
    "protection.pause": "⏸ 暂停",
    "protection.delete": "🗑 删除",
    "protection.home": "🏠 主菜单",
    "protection.invalid_device": "无效的设备登录请求",
    "protection.device_missing": "该账户已不在您的托管列表中",
    "protection.processing_failed": "处理失败，请稍后重试：{error}",
    "protection.processed": "该请求已处理",
    "protection.delete_confirm": "⚠️ 危险操作\n\n账户：{phone}\n\n这会删除当前会话、销毁 Session 并终止相关任务。操作不可撤销。\n\n是否继续？",
    "protection.confirm_delete": "🗑 确认删除",
    "protection.cancelled": "已取消",
    "protection.processing": "处理中…",
    "protection.delete_failed": "❌ 删除失败：{error}",
    "plans.quota_unlimited": "无限席位",
    "plans.quota": "{quota} 席",
    "plans.catalog": "💎 VIP 订阅\n\n按托管账户数量选择方案。登录解限监听使用独立额度。\n\n✦ GO\n  {go_price} USDT / 30 天 · {go_quota} 托管席\n  登录解限监听 · {go_alerts} 个\n\n✦ PLUS\n  {plus_price} USDT / 30 天 · {plus_quota} 托管席\n  登录解限监听 · {plus_alerts} 个\n  最低增加 {plus_min} 托管席 · 每席 {plus_unit} USDT\n\n✦ PRO\n  {pro_price} USDT / 30 天 · 无限托管席\n  登录解限监听 · 无限\n\n请选择方案。",
    "plans.go": "GO · 轻享",
    "plans.plus": "PLUS · 进阶",
    "plans.plus_custom": "PLUS · 定制席位",
    "plans.pro": "PRO · 尊享",
    "plans.create_failed": "❌ 创建支付失败：{error}",
    "plans.unknown_error": "未知错误",
    "plans.order": "💎 VIP 订单\n\n{change}\n\n方案：{plan}\n托管席位：{quota}\n\n{billing}\n\n订单编号：`{order_id}`\n\n支付后将在 5 分钟内自动确认。",
    "plans.pay": "立即支付",
    "plans.confirm": "核验支付",
    "plans.period_line": "{days} 天 · {price} USDT",
    "plans.period_page": "💎 选择订阅周期\n\n{lines}\n\n续费从当前到期日顺延。",
    "plans.period_button": "{days} 天",
    "plans.selection_expired": "订阅选择已失效，请重新进入 VIP 中心",
    "plans.plus_prompt": "💠 PLUS 席位扩容\n\n基础席位：{base}\n最低总席位：{minimum}\n单价：每席 {unit} USDT / 30 天\n\n请输入升级后的总席位数量。",
    "plans.plus_invalid": "❌ {error}\n请重新进入订阅中心选择扩容。",
    "plans.order_wrong_user": "该订单不属于您",
    "plans.checking": "正在检查支付状态…",
    "plans.unpaid": "支付尚未完成",
    "plans.check_failed": "检查失败：{error}",
    "plans.cancelled": "✓ 原订单已取消",
    "plans.already_paid": "订单已经支付，订阅已生效，无法取消",
    "plans.cancel_failed": "取消订单失败",
    "plans.success": "💎 {title}\n\n支付已确认，订阅已激活。\n\n{details}\n\n支付金额：{amount} {coin}\n订单编号：`{order_id}`",
    "plans.activated": "订阅已生效",
    "plans.billing_standard": "服务周期：{days} 天\n应付金额：{amount} USDT",
    "plans.change_new": "开启订阅",
    "plans.change_renewal": "续费订阅",
    "plans.change_downgrade": "预约下期方案",
    "plans.change_scheduled": "预订下期服务",
    "plans.change_default": "VIP 订阅",
    "plans.current": "\n\n━━━━━━━━━━━━\n\n{badge} {plan} · 当前方案\n{period}\n托管配额 · {quota}",
    "plans.period_active": "有效期至 {date} · 剩余 {days} 天",
    "plans.period_expiry": "有效期至 {date}",
    "plans.details": "方案：{plan}\n到期时间：{expiry}",
    "payment.confirmed": "💎 支付已确认\n\n{detail}\n\n支付金额：{amount} {coin}\n订单编号：`{order_id}`",
    "payment.detail": "方案：{plan}\n订阅周期：{days} 天",
    "transfer.remaining_hours_minutes": "{hours}小时{minutes}分钟",
    "transfer.remaining_hours": "{hours}小时",
    "transfer.remaining_minutes": "{minutes}分钟",
    "transfer.locked": "剩余 {remaining}",
    "transfer.page": "🔄 转让账户\n\n请选择要转让的账户：",
    "transfer.not_owned": "❌ 账户不存在或已不在您名下",
    "transfer.upload_locked": "上传 Session 添加的账户不允许转让",
    "transfer.wait": "还需等待约 {remaining}",
    "transfer.target_prompt": "👤 输入接收用户\n\n📱 账户：`{phone}`\n\n请发送接收方的数字用户 ID。接收方必须是有效 VIP 用户。",
    "transfer.timeout": "❌ 转让操作已超时，请重新进入转让账户。",
    "transfer.invalid_id": "❌ 用户 ID 格式错误，请发送纯数字用户 ID。",
    "transfer.confirm": "⚠️ 确认转让\n\n📱 账户：`{phone}`\n👤 接收用户：`{target}`\n\n账户会立即转移，且不可撤销。",
    "transfer.confirm_button": "✅ 确认转让",
    "transfer.cancel": "❌ 取消",
    "transfer.none_pending": "❌ 没有待确认的转让操作",
    "transfer.in_progress": "正在转让账户…",
    "transfer.self": "❌ 不能接收自己发起的转让",
    "transfer.vip_required": "❌ 只有 VIP 或管理员可以接收账户",
    "transfer.failure_not_owned": "账户已失效、已被接收，或已不在发起者名下。",
    "transfer.failure_source_access": "发起者已无账户转让权限。",
    "transfer.failure_locked": "该账户当前不允许转让。",
    "transfer.failure_too_new": "该账户尚未满足转让条件。",
    "hosting.kick": "踢出其他会话",
    "hosting.code": "获取验证码",
    "hosting.password": "二级密码",
    "hosting.clean": "清理账户",
    "hosting.empty": "📭 暂无托管账户",
    "hosting.page": "🧾 托管工具\n\n请选择要操作的账户：\n共 {count} 个账户",
    "hosting.account_missing": "❌ 账户不存在",
    "hosting.detail": "🧾 托管账户\n\n📱 账户：`{phone}`\n托管状态：{status}\n\n请选择操作：",
    "hosting.offline": "🔴 连接中断",
    "hosting.frozen": "❄️ 账户冻结",
    "hosting.operation_frozen": "❄️ 该账号已被冻结，Telegram 暂时禁止此设备管理操作。请在解冻后重试。",
    "hosting.recovered_retry": "✅ 托管连接已自动恢复，请重新执行刚才的操作。",
    "hosting.restricted": "⛔️ 操作受限 · 剩余{wait}",
    "hosting.available": "🟢 可操作",
    "hosting.kick_confirm": "🚪 终止其他会话\n\n账户：{phone}\n\n这会终止托管会话之外的所有授权设备。请勿频繁操作。\n\n是否继续？",
    "hosting.confirm_kick": "🚪 确认终止",
    "hosting.code_error": "🔢 获取验证码模式\n\n📱 账户：`{phone}`\n\n{message}",
    "hosting.code_active": "🔢 获取验证码模式已开启\n\n📱 账户：`{phone}`\n\n{message}\n\n该账户收到的 777000 验证码会发送到这里。退出或 30 分钟后恢复反登录转发。",
    "hosting.code_exit": "✅ 退出获取验证码",
    "hosting.clean_offline": "❌ 托管会话离线，无法清理",
    "hosting.clean_menu": "🧹 清理账户\n\n账户：{phone}\n\n请选择清理内容。所有清理操作均不可恢复。",
    "hosting.clean_chats": "删除全部对话",
    "hosting.clean_contacts": "删除全部联系人",
    "hosting.clean_all": "全部清理",
    "hosting.clean_running": "🧹 正在清理账户，请勿重复操作…",
    "hosting.password_menu": "🔐 二级密码\n\n📱 账户：`{phone}`\n\n请选择处理方式：",
    "hosting.password_reset": "♻️ 重置密码",
    "hosting.password_change": "✏️ 修改/清除",
    "hosting.password_set": "➕ 设置密码",
    "hosting.password_change_prompt": "✏️ 修改二级密码\n\n📱 账户：`{phone}`\n\n请发送：`旧密码 新密码`\n清除密码请发送：`旧密码 none`",
    "hosting.password_set_prompt": "➕ 设置二级密码\n\n📱 账户：`{phone}`\n\n请发送新二级密码。",
    "hosting.password_state_expired": "❌ 二级密码操作已失效，请重新进入托管菜单。",
    "hosting.password_timeout": "❌ 二级密码输入已超时，请重新操作。",
    "hosting.password_format": "❌ 格式错误，请发送：`旧密码 新密码`",
    "hosting.password_format_cancelled": "❌ 格式错误次数过多，操作已取消。",
    "hosting.password_empty": "❌ 二级密码不能为空，请重新输入。",
    "hosting.password_empty_cancelled": "❌ 空密码次数过多，操作已取消。",
    "hosting.password_old_cancelled": "❌ 旧二级密码错误次数过多，操作已取消。",
    "hosting.password_retry": "{result}\n\n请重新发送：`旧密码 新密码`",
    "hosting.password_result": "🔐 二级密码结果\n\n📱 账户：`{phone}`\n\n{result}",
    "hosting.clean_warn_chats": "• 删除所有私聊；可能退出群组/频道并拉黑机器人",
    "hosting.clean_warn_contacts": "• 删除全部联系人",
    "hosting.clean_confirm": "⚠️ 确认清理账户\n\n账户：{phone}\n操作：{action}\n\n{warnings}\n\n此操作不可恢复，是否继续？",
    "hosting.clean_confirm_button": "确认清理",
    "hosting.password_reset_page": "🔐 官方二级密码重置\n\n📱 账户：`{phone}`\n\n{result}\n\n等待期内请勿退出托管会话。",
}


ADMIN_ZH: Dict[str, str] = {
    "admin.access_denied": "❌ 您不是管理员",
    "admin.main.entry": "管理面板",
    "admin.common.unknown": "未知",
    "admin.common.none": "无",
    "admin.common.unlimited": "无限",
    "admin.common.seats": "{count} 席",
    "admin.common.audit_warning": "⚠️ 操作已完成，但审计记录失败。",
    "admin.common.query_audit_warning": "⚠️ 查询成功，但审计记录失败。",
    "admin.common.cancel": "取消",
    "admin.common.confirm": "确认执行",
    "admin.common.save": "确认保存",
    "admin.common.cancelled": "已取消",
    "admin.common.cancelled_text": "已取消操作。",
    "admin.common.expired_or_processed": "操作已失效或已处理",
    "admin.common.confirm_expired": "确认已过期",
    "admin.common.state_changed": "用户状态已变化，请重新发起",
    "admin.common.running": "正在执行…",
    "admin.common.failed_safe": "❌ 操作失败，数据未安全完成变更。",
    "admin.common.payment_unavailable": "支付系统尚未初始化",
    "admin.panel.text": (
        "👑 **管理员控制台**\n\n"
        "**账户概览**\n"
        "• 已加载账户：{total_accounts} 个\n"
        "• 已开启保护：{protected_accounts} 个\n\n"
        "💎 **订阅概览**\n"
        "• 有效订阅：{total_vips} 个\n"
        "• 待处理订单：{review_orders} 个\n\n"
        "📈 **今日运营**\n"
        "• 订阅收入：{revenue}\n"
        "• 新增付费用户：{new_paid_users} 人\n\n"
        "🕒 更新：{updated_at}"
    ),
    "admin.panel.orders": "订单中心",
    "admin.panel.user_search": "用户搜索",
    "admin.panel.grant": "发放订阅",
    "admin.panel.delete": "删除订阅",
    "admin.panel.subscribers": "订阅用户",
    "admin.panel.config": "方案设置",
    "admin.panel.report": "运营报表",
    "admin.panel.audit": "审计日志",
    "admin.panel.reminders": "提醒设置",
    "admin.panel.refresh": "刷新数据",
    "admin.report.title.1": "今日",
    "admin.report.title.7": "最近 7 日",
    "admin.report.title.30": "最近 30 日",
    "admin.report.text": (
        "📈 运营统计 · {title}\n\n"
        "订阅收入：{revenue}\n"
        "新增付费用户：{new_paid_users} 人\n\n"
        "统计起点：{start_time}\n"
        "更新时间：{end_time}"
    ),
    "admin.report.tab.1": "今日",
    "admin.report.tab.7": "7 日",
    "admin.report.tab.30": "30 日",
    "admin.order.category.review": "🚨 待处理",
    "admin.order.category.active": "⏳ 处理中",
    "admin.order.category.completed": "✅ 已完成",
    "admin.order.category.closed": "⌛ 已关闭",
    "admin.order.category.all": "📋 全部",
    "admin.order.tab.review": "待处理",
    "admin.order.tab.active": "处理中",
    "admin.order.tab.completed": "已完成",
    "admin.order.tab.closed": "已关闭",
    "admin.order.tab.all": "全部",
    "admin.order.search_button": "搜索",
    "admin.order.list": "{title}订单\n\n共 {total} 条 · 第 {page}/{pages} 页{suffix}",
    "admin.order.search_suffix": "\n搜索条件：精确匹配",
    "admin.order.search_prompt": "🔎 搜索订单\n\n请输入完整订单号、unique_id 或用户 ID：",
    "admin.order.search_result": "🔎 订单搜索\n\n共 {count} 条",
    "admin.order.not_found": "订单不存在",
    "admin.order.detail": (
        "🧾 订单详情\n\n"
        "订单号：{order_id}\n状态：{status}\n用户 ID：{user_id}\n类型：{order_type}\n"
        "金额：{amount} {coin}\n套餐：{plan}\n周期：{days} 天\n"
        "创建时间：{created}\n验证时间：{verified}\n发放时间：{fulfilled}"
    ),
    "admin.order.reason": "异常原因：{reason}",
    "admin.order.retry_failures": "自动重试失败次数：{count}",
    "admin.order.next_check": "下次检查：{time}",
    "admin.order.recheck": "重新查单",
    "admin.order.retry": "重试发放",
    "admin.order.checking": "正在向支付平台查单…",
    "admin.order.check_audit_warning": "⚠️ 查单已执行，但审计记录失败。",
    "admin.order.state_changed": "订单状态已变化，请刷新",
    "admin.order.retry_prompt": (
        "⚠️ 确认重试发放\n\n订单：{order_id}\n用户：{user_id}\n\n"
        "仅会按原始订单快照重试，不会绕过状态校验。"
    ),
    "admin.order.confirm_retry": "确认重试",
    "admin.order.retrying": "正在重试发放…",
    "admin.order.retry_audit_warning": "⚠️ 发放已执行，但审计记录失败。",
    "admin.order.status.completed": "✅ 已完成",
    "admin.order.status.review": "🚨 待处理",
    "admin.order.status.pending": "⏳ 待支付",
    "admin.order.status.expired": "⌛ 已过期",
    "admin.order.status.cancelled": "🚫 已取消",
    "admin.user.no_match": "📭 未找到匹配用户",
    "admin.user.search_failed": "❌ 搜索失败，请稍后重试",
    "admin.user.search_title": "🔎 用户搜索结果\n\n共 {count} 条{warning}",
    "admin.user.search_audit_warning": "\n\n⚠️ 查询成功，但审计记录失败。",
    "admin.user.search_prompt": "🔎 全局用户搜索\n\n请输入用户 ID、@用户名、显示名称或完整托管手机号：",
    "admin.user.search_expired": "搜索结果已失效，请重新搜索",
    "admin.user.fallback_name": "用户 {user_id}",
    "admin.user.detail": (
        "👤 用户详情\n\n名称：{name}\n用户名：{username}\n用户 ID：{user_id}\n"
        "身份：{role}\n套餐：{plan}\n到期时间：{expires_at}\n"
        "托管配额：{quota}\n托管使用：{used}"
    ),
    "admin.user.role.admin": "管理员",
    "admin.user.role.subscriber": "有效订阅",
    "admin.user.role.regular": "普通/过期用户",
    "admin.user.scheduled": "待生效方案：{plan} / {quota}",
    "admin.user.hosted_accounts": "托管账户：",
    "admin.user.more_accounts": "…另有 {count} 个账户未展开",
    "admin.user.no_accounts": "暂无",
    "admin.user.order_count": "订单记录：共 {count} 笔",
    "admin.user.manage_subscription": "💎 管理订阅",
    "admin.user.grant_subscription": "💎 发放订阅",
    "admin.user.orders_button": "🧾 订单 · {count}",
    "admin.user.account_actions": "⚙️ 账户操作",
    "admin.user.subscription_admin": "管理员无需管理订阅",
    "admin.user.subscription_text": "💎 管理订阅\n\n用户：{user_id}\n状态：{status}\n套餐：{plan}\n到期：{expires_at}",
    "admin.user.subscription_active": "有效",
    "admin.user.subscription_expired": "已过期",
    "admin.user.extend_subscription": "延期 / 变更订阅",
    "admin.user.delete_subscription": "⚠️ 删除订阅",
    "admin.user.accounts_text": "⚙️ 账户操作\n\n用户：{user_id}\n运行中：{running} 个\n可恢复：{resumable} 个",
    "admin.user.reload": "🔄 重连运行账户",
    "admin.user.resume": "▶️ 恢复已选托管账户",
    "admin.user.suspend": "⚠️ 停用全部运行账户",
    "admin.user.choose_plan": "💎 为用户 {user_id} 选择订阅方案：",
    "admin.user.days_prompt": "请输入订阅天数：",
    "admin.user.plus_days_prompt": "\n可选格式：天数 Plus总配额，例如 `30 15`。",
    "admin.user.no_subscription": "该用户没有可删除的订阅",
    "admin.user.delete_confirm": (
        "⚠️ 确认删除订阅\n\n用户：{user_id}\n套餐：{plan}\n到期：{expires_at}\n\n"
        "确认后会停用运行账户，但保留账号资料和 session 文件。"
    ),
    "admin.user.reconnecting": "正在重连…",
    "admin.user.reload_failed": "❌ 重连运行账户失败。",
    "admin.user.reload_done": "✅ 重载完成：共 {total}，成功 {success}，失败 {failed}。",
    "admin.user.suspend_confirm": (
        "⚠️ 确认停用全部运行账户\n\n用户：{user_id}\n运行账户：{count} 个\n\n"
        "session 文件不会删除，可稍后恢复。"
    ),
    "admin.user.resuming": "正在恢复…",
    "admin.user.resume_failed": "❌ 恢复账户失败。",
    "admin.user.resume_done": "✅ 已恢复 {count} 个已选账户。",
    "admin.user.orders_text": "🧾 用户 {user_id} 的订单\n\n共 {total} 条 · 第 {page}/{pages} 页",
    "admin.account.status.alive": "正常",
    "admin.account.status.frozen": "冻结",
    "admin.account.status.online": "在线",
    "admin.account.status.offline": "离线",
    "admin.account.status.unknown": "未知",
    "admin.subscription.no_subscription": "无订阅",
    "admin.subscription.current": "{plan} / 到期 {expires_at}",
    "admin.subscription.grant_confirm": (
        "⚠️ 确认订阅变更\n\n用户：{user_id}\n变更前：{current}\n"
        "变更后：{plan} / {days} 天 / {quota}\n\n确认后才会写入订阅数据。"
    ),
    "admin.subscription.changed": "✅ 订阅变更已完成。",
    "admin.subscription.deleted": "✅ 订阅已删除，并停用 {count} 个运行账户。",
    "admin.subscription.accounts_suspended": "✅ 已停用 {count} 个运行账户，session 文件仍保留。",
    "admin.subscription.input_invalid": "❌ 输入无效：{error}",
    "admin.subscription.retry_input": "❌ 参数无效：{error}\n\n请重新输入。",
    "admin.subscription.error.days_or_plus_quota": "请输入天数，Plus 可额外输入总配额",
    "admin.subscription.help_grant": (
        "🎁 尊享订阅 · 权益发放\n\n`/sub 用户ID go 天数`\n"
        "`/sub 用户ID plus 天数 [总配额]`\n`/sub 用户ID pro 天数`\n\n"
        "天数按自然日计算；Plus 未填写总配额时使用方案默认配额。"
    ),
    "admin.subscription.sub_format": "❌ 格式：/sub 用户ID go|plus|pro 天数 [Plus总配额]",
    "admin.subscription.invalid_params": "❌ 参数无效：{error}",
    "admin.subscription.error.user_id_positive": "用户ID必须大于 0",
    "admin.subscription.error.admin_unlimited": "管理员已拥有无限托管权限",
    "admin.subscription.error.plan": "套餐必须是 go、plus 或 pro",
    "admin.subscription.error.days_positive": "天数必须大于 0",
    "admin.subscription.error.days_range": "天数超出可支持的日期范围",
    "admin.subscription.error.plus_args": "Plus 参数过多",
    "admin.subscription.error.quota_not_allowed": "{plan} 不接受配额参数",
    "admin.subscription.delsub_format": "❌ 格式：/delsub 用户ID",
    "admin.subscription.error.delete_admin": "不能删除管理员权限",
    "admin.subscription.nothing_to_delete": "ℹ️ 用户 `{user_id}` 当前没有可删除的订阅。",
    "admin.subscription.delete_command_confirm": (
        "⚠️ 确认删除订阅\n\n用户：{user_id}\n当前套餐：{plan}\n到期时间：{expires_at}\n\n"
        "确认后将删除订阅并停用运行账户；账号资料与 session 文件会保留。"
    ),
    "admin.subscription.help_delete": (
        "🗑 删除订阅\n\n`/delsub 用户ID`\n\n"
        "删除后会立即停用该用户运行中的托管账户，但保留账号资料与 session 文件。"
    ),
    "admin.subscription.list_empty": "📭 暂无订阅用户",
    "admin.subscription.list_text": "⭐ 订阅用户列表\n\n点击条目查看用户详情。\n共 {count} 个有效订阅用户",
    "admin.subscription.hosted_label": "托管:{count}",
    "admin.config.field.go_price": "GO 价格（USDT）",
    "admin.config.field.go_quota": "GO 席位",
    "admin.config.field.plus_price": "PLUS 价格（USDT）",
    "admin.config.field.plus_quota": "PLUS 基础席位",
    "admin.config.field.plus_addon_price": "PLUS 加席单价（USDT）",
    "admin.config.field.plus_min_addon": "PLUS 最低加席数",
    "admin.config.field.pro_price": "PRO 价格（USDT）",
    "admin.config.field.discount_90": "90 天折扣（%）",
    "admin.config.field.discount_180": "180 天折扣（%）",
    "admin.config.field.discount_365": "365 天折扣（%）",
    "admin.config.long_discounts": "长期折扣",
    "admin.config.prompt": "{prefix}✏️ 修改{target}\n\n第 {index}/{total} 项：请输入{label}\n当前值：{current}\n例如：`{example}`",
    "admin.config.error.positive_integer": "请输入正整数",
    "admin.config.error.gt_zero": "数值必须大于 0",
    "admin.config.error.valid_number": "请输入有效数字",
    "admin.config.error.discount_range": "折扣必须大于等于 0 且小于 100",
    "admin.config.plan.go": "GO：{price} USDT，{quota} 席",
    "admin.config.plan.plus": "PLUS：{price} USDT，{quota} 席\n加席：{addon_price} USDT / 席，至少加 {min_addon} 席",
    "admin.config.plan.pro": "PRO：{price} USDT，无限席",
    "admin.config.discount_item": "{days} 天 {percent}%",
    "admin.config.preview": "⚠️ 确认配置修改\n\n修改前\n{before}\n\n修改后\n{after}\n\n确认后仅影响新订单。",
    "admin.config.expired": "配置已过期，请重新开始",
    "admin.config.expired_notice": "⌛ 配置已过期，未保存任何修改。",
    "admin.config.invalid": "配置已失效，请重新开始",
    "admin.config.changed": "配置已被其他管理员修改",
    "admin.config.changed_notice": "⚠️ 当前配置已变化，请重新填写。",
    "admin.config.save_failed": "保存失败",
    "admin.config.save_failed_notice": "❌ 保存失败，原配置保持不变。",
    "admin.config.saved": "保存成功",
    "admin.config.saved_notice": "✅ 配置已保存，仅影响新订单。",
    "admin.config.audit_warning": "\n⚠️ 审计记录失败。",
    "admin.config.reenter": "重新填写",
    "admin.config.state_invalid_notice": "⌛ 配置状态已失效，请重新开始。",
    "admin.config.text": (
        "{notice}⚙️ 订阅方案配置\n\n当前价格（30 天）\n"
        "🥉 GO：{go_price} USDT，{go_quota} 席\n"
        "🥈 PLUS：{plus_price} USDT，{plus_quota} 席\n"
        "加席：{addon_price} USDT / 席，至少加 {min_addon} 席\n"
        "🥇 PRO：{pro_price} USDT，无限席\n\n长期折扣\nGO：无折扣\n"
        "PLUS / PRO：90 天 {discount_90}%，180 天 {discount_180}%，365 天 {discount_365}%\n\n"
        "仅影响新订单。"
    ),
    "admin.config.edit_go": "修改 GO",
    "admin.config.edit_plus": "修改 PLUS",
    "admin.config.edit_pro": "修改 PRO",
    "admin.config.edit_discounts": "修改长期折扣",
    "admin.reminder.text": "⏰ 到期提醒设置\n\n当前设置: 提前 {days} 天提醒\n即将到期用户: {count} 人\n\n请选择提醒天数:",
    "admin.reminder.option": "提前{days}天提醒",
    "admin.reminder.saved": "✅ 已设置提前 {days} 天提醒",
    "admin.reminder.failed": "❌ 设置失败",
    "admin.reminder.hub": "⏰ 提醒设置\n\n请选择要配置的提醒类型：",
    "admin.reminder.expiry_button": "订阅到期提醒",
    "admin.reminder.login_unlock_button": "登录解限提醒",
    "admin.login_unlock.text": "⏳ 登录解限提醒设置\n\n当前提醒次数：{count}\n当前节点：{schedule}\n\n请选择新的提醒次数（1–5 次）：",
    "admin.login_unlock.prompt": "⏳ 设置登录解限提醒\n\n请输入 **{count}** 个时间节点，按距离解限最远到最近排列。\n\n示例：`60m 10m 30s`\n• 输入数量必须与提醒次数一致\n• 除最后一项外只能使用分钟 `m`\n• 最后一项可使用分钟 `m` 或 1–59 秒 `s`\n• 分钟范围为 1–43200",
    "admin.login_unlock.invalid": "❌ 配置无效：{error}\n\n请重新输入 {count} 个时间节点，例如：`60m 10m 30s`",
    "admin.login_unlock.error.count": "提醒次数必须为 1–5",
    "admin.login_unlock.error.item_count": "输入项数量与提醒次数不一致",
    "admin.login_unlock.error.format": "每项必须使用数字加 m 或 s",
    "admin.login_unlock.error.range": "时间必须大于零",
    "admin.login_unlock.error.seconds_last": "只有最后一个最近节点可以使用秒",
    "admin.login_unlock.error.seconds_range": "秒数必须为 1–59",
    "admin.login_unlock.error.minutes_range": "分钟数必须为 1–43200",
    "admin.login_unlock.error.descending": "时间节点必须互不重复并严格递减",
    "admin.login_unlock.save_failed": "❌ 登录解限提醒配置保存失败，原配置未改变。",
    "admin.login_unlock.saved": "✅ 已设置 {count} 次登录解限提醒：{schedule}",
    "admin.login_unlock.recalculate_warning": "\n⚠️ 配置已保存，但现有监听未能立即重算，将由后台任务重试。",
    "admin.audit.title": "🧾 管理操作审计{filter}\n\n共 {total} 条 · 第 {page}/{pages} 页",
    "admin.audit.filtered": " · 已筛选",
    "admin.audit.filter": "筛选",
    "admin.audit.clear": "清除筛选",
    "admin.audit.download": "下载日志",
    "admin.audit.filter_prompt": (
        "🔎 筛选审计日志\n\n"
        "输入：管理员:ID、动作:名称、目标:ID\n"
        "例如：动作:重新查单\n\n"
        "多个条件用空格分隔，也可直接输入动作名称。"
    ),
    "admin.audit.filter_result": "🧾 审计筛选结果\n\n共 {count} 条",
    "admin.audit.not_found": "审计记录不存在",
    "admin.audit.detail_title": "🧾 审计详情",
    "admin.audit.entry": (
        "记录 {index}\n时间：{timestamp}\n管理员 ID：{admin_id}\n动作：{action}\n"
        "目标类型：{target_type}\n目标 ID：{target_id}\n结果：{result}\n"
        "修改前：{before}\n修改后：{after}\n元数据：{metadata}\n错误：{error}"
    ),
    "admin.audit.no_logs": "暂无审计日志",
    "admin.audit.sending": "正在发送审计日志…",
    "admin.audit.caption": "管理员审计日志（最近 180 天）",
    "admin.audit.sent_warning": "⚠️ 文件已发送，但审计记录失败。",
    "admin.audit.send_failed": "❌ 审计日志发送失败",
    "admin.audit.result.attempt": "尝试",
    "admin.audit.result.success": "成功",
    "admin.audit.result.failed": "失败",
    "admin.audit.result.cancelled": "已取消",
    "admin.audit.target.user": "用户",
    "admin.audit.target.order": "订单",
    "admin.audit.target.audit": "审计",
    "admin.audit.target.system": "系统",
    "admin.audit.target.system_setting": "系统设置",
    "admin.audit.target.subscription_catalog": "订阅方案",
    "admin.audit.target.subscription_periods": "订阅周期",
    "admin.audit.action.user.search": "搜索用户",
    "admin.audit.action.user.detail": "查看用户详情",
    "admin.audit.action.order.list": "查看订单列表",
    "admin.audit.action.order.search": "搜索订单",
    "admin.audit.action.order.detail": "查看订单详情",
    "admin.audit.action.order.recheck": "重新查单",
    "admin.audit.action.order.retry_fulfillment": "重试发放",
    "admin.audit.action.subscription.grant": "发放订阅",
    "admin.audit.action.subscription.delete": "删除订阅",
    "admin.audit.action.accounts.reload": "重连账户",
    "admin.audit.action.accounts.resume": "恢复账户",
    "admin.audit.action.accounts.suspend": "停用账户",
    "admin.audit.action.audit.download": "下载审计日志",
    "admin.audit.action.config.expiry_reminder_set": "设置到期提醒",
    "admin.audit.action.config.login_unlock_reminder_schedule_set": "设置登录解限提醒",
    "admin.audit.action.config.subscription_catalog_set": "修改订阅方案",
    "admin.audit.action.config.subscription_discounts_set": "修改长期折扣",
    "admin.reason.paid_order_validation_failed": "平台显示已支付，但订单字段校验失败",
    "admin.reason.subscription_state_changed": "差价升级的源订阅状态已发生变化",
    "admin.reason.fulfillment_failed": "支付已确认，但权益发放失败",
    "admin.reason.local_order_save_failed": "支付链接已创建，但本地订单保存失败",
    "admin.notification.startup": (
        "🛡 𝑨𝒏𝒕𝒊𝒍𝒐𝒈𝒊𝒏 系统已完成初始化\n\n安全矩阵部署完成。\n"
        "监控核心节点运行正常。\n风险识别模块已激活。\n\n当前状态：STABLE\n防护等级：FULL PROTECTION"
    ),
    "admin.notification.new_subscription": (
        "💎 新订阅开通\n\n用户 ID：`{user_id}`\n订阅方案：{plan}\n{seat_line}"
        "订阅周期：{days} 天\n实付金额：{amount} {coin}\n订单编号：`{order_id}`"
    ),
    "admin.notification.plan": "{badge} {plan}{custom}",
    "admin.notification.plan_name.go": "GO · 轻享方案",
    "admin.notification.plan_name.plus": "PLUS · 进阶方案",
    "admin.notification.plan_name.pro": "PRO · 尊享方案",
    "admin.notification.custom_plan_suffix": " · 专属席位",
    "admin.notification.custom_seats": "专属席位：{quota} 席\n",
    "admin.notification.order_exception": (
        "🚨 支付异常订单\n\n异常原因：{reason}\n用户 ID：`{user_id}`\n"
        "订单类型：{order_type}\n订单金额：{amount} {coin}\n订单状态：{status}\n"
        "订单编号：`{order_id}`\n\n请先通过支付平台核实订单，不要直接发放权益。"
    ),
}


USER_RESULT_ZH: Dict[str, str] = {
    "hosting.clean_result.success": "✅ 清理完成",
    "hosting.clean_result.partial": "⚠️ 部分清理完成",
    "hosting.clean_result.failed": "❌ 清理失败",
    "hosting.clean_result.text": "{status}\n\n账户：{phone}\n清理类型：{action}\n\n删除对话：{chats}\n删除联系人：{contacts}{error_block}",
    "hosting.clean_result.errors": "\n\n问题：\n{errors}",
    "hosting.clean_result.more_errors": "• 另有 {count} 项错误，详见服务日志",
    "hosting.quota_full": "❌ 托管配额已满（已使用 {used} / {quota}）。\n\n请升级订阅、扩容 Plus，或移除不再需要的托管账户。",
    "hosting.no_access": "❌ 无权限",
    "hosting.clean_age": "⏳ 协议创建未满 1 小时，清理功能将在 {wait}后可用",
    "hosting.wait_seconds": "{seconds} 秒",
    "hosting.wait_hours": "{hours} 小时",
    "hosting.wait_minutes": "{minutes} 分钟",
    "hosting.account_healthy": "✅ 账户 `{phone}` 已在托管中，当前健康检查正常。",
    "hosting.account_busy": "⚠️ 账户 `{phone}` 的会话文件仍在使用中，请稍后重试。",
    "hosting.account_rechecked": "✅ 账户 `{phone}` 已在托管中，复核健康检查正常。",
    "hosting.account_existing_offline": "⚠️ 账户 `{phone}` 已存在，但当前托管连接暂时不可用。",
    "hosting.operation_invalid": "❌ 托管会话已失效，无法执行该操作。\n\n📞 账户：`{phone}`\n请重新添加该账号或上传新的 `.session` 文件。",
    "hosting.operation_offline": "❌ 托管会话离线，当前连接暂时不可用，无法执行该操作。\n\n📞 账户：`{phone}`\n原因：{reason}\n\n系统已保留 session 文件，请稍后重试或手动重新加载。",
    "hosting.session_too_new": "⏳ 当前托管会话创建时间过短，Telegram 暂不允许踢出其他设备。\n\n请稍后重试，或暂时在 Telegram 官方客户端中管理登录设备。",
    "hosting.rate_limited": "⏳ Telegram 请求受限，请 {seconds} 秒后再试",
    "hosting.cooldown": "⏳ 操作过于频繁，请 {seconds} 秒后再试",
    "hosting.code_started": "✅ 已进入获取验证码模式（已临时暂停反登录转发，30 分钟后恢复原状态）",
    "hosting.code_stopped": "✅ 已退出获取验证码模式（已恢复原状态）",
    "hosting.account_deleted": "🗑 {phone} 删除成功！",
    "hosting.sessions_kicked": "✅ 已踢出其他会话（保留当前托管会话）",
    "hosting.clean_timeout_result": "清理任务超过 {seconds} 秒，已停止",
    "hosting.password_reset_wait": "✅ 已发起官方二级密码重置等待期\n\n⏳ 预计解除时间：{time}\n\n到时间后，二级密码会被移除，你就可以在 Telegram 客户端里重新设置新的二级密码。",
    "hosting.password_reset_retry": "⚠️ 当前无法发起重置，请稍后再试\n⏳ 可重试时间：{time}",
    "hosting.password_reset_ok": "✅ 重置请求已处理\n\n如果该号原本有二级密码，Telegram 可能已进入等待期或已完成解除。\n请在 Telegram 客户端的“两步验证”里查看状态。",
    "hosting.password_reset_sent": "✅ 已发送重置请求（请在客户端查看两步验证状态）",
    "hosting.password_change_format": "❌ 格式错误，请输入：旧密码 新密码",
    "hosting.password_changed": "✅ 二级密码修改成功",
    "hosting.password_old_wrong": "❌ 旧二级密码错误，请重新输入",
    "hosting.password_new_empty": "❌ 二级密码不能为空，请重新输入",
    "hosting.password_set": "✅ 二级密码设置成功",
    "hosting.password_already_set": "❌ 当前账号已有二级密码，请选择“修改密码”并输入旧密码",
    "hosting.password_old_empty": "❌ 旧密码不能为空，请重新输入",
    "hosting.password_cleared": "✅ 二级密码已清除",
    "hosting.clean_error.dialogs": "获取对话失败",
    "hosting.clean_error.block_bot": "机器人拉黑失败",
    "hosting.clean_error.delete_chat": "删除对话失败",
    "hosting.clean_error.missing_contacts": "{count} 个联系人缺少用户信息，未能删除",
    "hosting.clean_error.delete_contacts": "删除联系人失败",
    "hosting.clean_error.invalid_type": "无效的清理类型",
    "notify.session_unavailable": "📡 托管连接状态提醒\n\n「{source}」时发现托管账户的连接凭据已失效\n\n📱 账户：{phone}\n🔌 连接状态：已断开\n\n请重新添加该账户，或上传新的 .session 文件来恢复托管。",
    "notify.session_source.startup": "系统启动检查",
    "notify.session_source.manual_reload": "手动重新加载",
    "notify.session_source.connection": "连接状态检查",
    "device.already_expired": "🛡️ 该设备授权已失效，无需重复踢出。",
    "transfer.error.invalid_phone": "❌ 账户格式错误",
    "transfer.error.target_quota": "❌ 接收方托管配额已满",
    "transfer.error.target_duplicate": "❌ 接收方已托管该账户",
    "transfer.error.target_session_exists": "❌ 接收方已存在该账户的 Session",
    "transfer.error.source_session_missing": "❌ 未找到该账户的 Session 文件",
    "transfer.error.journal_failed": "❌ 无法安全创建转移事务，请稍后重试",
    "transfer.error.metadata_failed": "❌ 无法保存转移状态，请稍后重试",
    "transfer.error.rollback_failed": "❌ 自动恢复失败，请立即联系管理员",
    "transfer.error.source_busy": "❌ 当前托管 Session 正在使用中，请稍后重试",
    "transfer.error.move_failed": "❌ 转移失败，原账户已恢复",
    "transfer.error.target_load_failed": "❌ 接收方加载失败，原账户已恢复",
    "transfer.notify_failed": "⚠️ 接收方通知发送失败，但转移已经完成。",
    "account.upload.zip_done": "📦 ZIP Session 导入完成",
    "account.upload.zip_success": "✅ 成功：{count}",
    "account.upload.zip_failed": "❌ 失败：{count}",
    "account.upload.zip_quota": "⏭ 因配额已满跳过：{count}",
    "account.upload.unknown_phone": "未知手机号",
    "account.upload.unnamed": "未命名.session",
    "account.upload.zip_invalid": "❌ ZIP 压缩包已损坏或格式无效",
    "account.upload.zip_empty": "❌ ZIP 中未找到 .session 文件",
    "account.upload.zip_too_many": "❌ ZIP 中最多允许 {count} 个 Session 文件",
    "account.upload.zip_encrypted": "❌ 加密的 Session 文件不受支持",
    "account.upload.zip_session_too_large": "❌ Session 文件解压后超过 40 KB",
    "account.upload.zip_extract_failed": "❌ Session 文件解压失败",
    "account.no_access": "无权限使用此功能",
    "account.session_releasing": "旧登录会话仍在释放中，请稍后重新添加账号",
    "account.qr_session_releasing": "旧扫码登录会话仍在释放中，请稍后重新添加账号",
    "account.qr.dependency_missing": "缺少二维码依赖，请先安装 requirements.txt 中的 qrcode[pil]",
    "plans.admin_current": "\n\n━━━━━━━━━━━━\n\n{badge} {plan} · 管理权限\n托管配额 · {quota}",
    "payment.fulfilled_standard": "╭─ ✦ 尊享权益\n│ 方案等级  ·  {plan}\n│ 托管席位  ·  {quota}\n│ 尊享周期  ·  {days} 天{discount}\n╰─ 权益已自动激活",
    "payment.fulfilled_discount": "\n│ 周期礼遇  ·  实际节省 {percent}%",
    "payment.fulfilled_selection": "\n\n如果现有托管账户超过新配额，请进入 VIP 中心选择要恢复的账户。",
    "payment.plan.go": "GO · 轻享方案",
    "payment.plan.plus": "PLUS · 进阶方案",
    "payment.plan.pro": "PRO · 尊享方案",
    "payment.plan.custom": " · 专属席位",
    "payment.provider.subscription": "{title}｜{days}天尊享订阅{seats}",
    "payment.provider.seats": "｜{quota}席",
    "payment.error.admin": "管理员已拥有无限托管权限，无需购买订阅",
    "payment.error.open_order": "已有未完成的订阅订单，请先支付或取消原订单",
    "payment.error.scheduled_conflict": "已有待生效降档，只能续购相同的待生效方案",
    "payment.error.invalid_plan": "订阅方案无效",
    "payment.error.invalid_quota": "订阅配额无效",
    "payment.error.invalid_period": "订阅周期无效",
    "payment.error.rate_limited": "支付请求过于频繁，请稍后重试",
    "payment.error.unavailable": "支付服务暂时不可用，请稍后重试",
    "payment.error.invalid_response": "支付服务返回了无效响应，请稍后重试",
    "payment.error.save_failed": "支付订单保存失败，请稍后重试",
    "payment.error.invalid_request": "支付请求参数无效",
    "payment.error.order_owner": "该订单不属于您",
    "payment.error.order_paid": "订单已支付，无法取消",
    "payment.error.order_active": "订单已支付并生效，无法取消",
    "payment.error.cancel_check": "取消前无法确认订单状态：{error}",
    "payment.error.cancel_save": "取消订单保存失败，请稍后重试",
    "payment.error.not_found": "订单不存在",
    "payment.error.expired": "订单已过期",
    "payment.error.cancelled": "订单已取消",
    "payment.error.validation": "支付订单校验失败，请联系管理员",
    "payment.error.fulfillment": "支付已确认，但订阅发放失败，请联系管理员",
}


ZH.update(ADMIN_ZH)
ZH.update(USER_RESULT_ZH)


def t(key: str, **values: object) -> str:
    """Return the Chinese text for a key."""
    try:
        template = ZH[key]
    except KeyError as error:
        raise KeyError(f"unknown localization key: {key}") from error
    return template.format(**values)


def localized_payment_error(value: object) -> str:
    return str(value or "")


def localized_result(value: object) -> str:
    return str(value or "")
