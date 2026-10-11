# Anti-login · Telegram 反登录托管系统

由 [秦屿泊 @qinyubo](https://t.me/qinyubo) 维护运行的 [Anti-login](https://t.me/QinShield_Bot)

基于 Telethon 开发，监控 Telegram 官方账户（777000）发送的登录通知，并强制登录验证码失效，帮助用户实时掌握账户登录活动，提高账户安全性。

> Developed and open-sourced by 秦屿泊 (@qinyubo). Free for everyone to use and modify under the MIT License.

## 功能特点

- 异常登录验证码强制失效，降低账户被登录的风险
- 多账户托管：获取验证码、终止其他会话、二级密码、账户清理
- Bot 内账户转让
- 登录限制检测与解限提醒
- 新设备处理、订阅支付、到期提醒

## 技术架构

- Python 3.12 + [Telethon](https://github.com/LonamiWebs/Telethon)，单进程运行主 Bot 和所有托管账户
- `accounts/` 账户托管与登录监控，`handlers/` Bot 交互，`payments/` OkayPay 支付，`reminders/` 到期与解限提醒，`storage/` 数据持久化
- 本地 JSON 文件 + SQLite Session，无需外部数据库；日志写入 `logs/`，按天轮转

## 环境要求

- Debian、Ubuntu 或其他 Linux
- Python 3.12（推荐）
- Telegram Bot Token、管理员 ID
- OkayPay 商户 ID 和支付 Token

## 快速开始

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# 新建 config.py，填入下方「配置」中的必填项（见下表）
python bot_main.py
```

停止服务：`python scripts/stop.py`（先让进程完成清理再退出，加 `--force` 可超时强制结束）。

## 配置

`config.py` 只保存部署凭据；默认值、校验和运行路径由 `settings.py` 管理，日常部署无需修改 `settings.py`。

| 配置项 | 说明 |
| --- | --- |
| `API_ID`、`API_HASH` | 默认使用 Telegram 官方电脑客户端配置 |
| `BOT_TOKEN` | BotFather 提供的 Bot Token |
| `ADMIN_IDS` | 管理员数字 ID 列表，例如 `[123456789]` |
| `MERCHANT_ID`、`PAYMENT_TOKEN` | OkayPay 商户凭据 |
| `PAYMENT_RETURN_URL` | 你的 Bot 地址，例如 `https://t.me/QinShield_Bot` |

其他配置保持模板默认值即可。

## 注意事项

- 本程序仅用于个人账户安全保护，请勿用于非法用途
- 使用本程序时请遵守Telegram的服务条款
- 过度频繁的API调用可能导致账户被限制
- 请勿修改或删除版权文本，程序启动时会校验，被改动将拒绝启动
- 升级或迁移前先停止服务并备份 `config.py` 和数据目录

## 安全

1. 请勿泄露Bot Token 和支付密钥
2. 定期检查账户的活跃会话
3. 开启两步验证以提高账户安全性

## 许可证

Copyright (c) 2026 秦屿泊 (`@qinyubo`)

本项目采用 [MIT License](LICENSE)，允许免费使用、修改、商用和再发布，但必须保留版权与许可声明。
