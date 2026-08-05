# Anti-Login

由作者 [秦屿泊 @qinyubo](https://t.me/qinyubo) 维护运行的 [Anti-Login](https://t.me/QinShield_Bot)。

基于 Telethon 的 Telegram Bot，提供账号托管、反登录监控、新设备处理、
订阅支付、到期提醒和账号转让。用户界面支持简体中文和英文。

Developed and open-sourced by 秦屿泊 (@qinyubo). Free for everyone to use and modify under the MIT License.

## 系统特性

最新版 Anti-Login 系统支持：

- 异常登录验证码不转发并强制失效，降低账户被登录的风险；
- 多账户托管，以及获取验证码、终止其他会话、二级密码和账户清理等托管工具；
- 系统内账户转让，也支持通过 Telegram 私聊发起和接收转让；
- 登录限制检测与解限提醒，按用户时区显示解限时间；
- 新设备处理、订阅支付、到期提醒，以及简体中文和英文界面。

## 环境要求

- Debian、Ubuntu 或其他使用 systemd 的 Linux；
- Python 3.12（推荐）或 3.14；
- Telegram Bot Token、管理员 ID；
- OkayPay 商户 ID 和支付 Token。

生产部署请下载 [GitHub Release](https://github.com/qinyubo211/Anti-Login/releases)
中的 `Anti-Login-v1.0.0.zip`，不要使用包含测试和 CI 文件的 Source code ZIP。

## 快速部署

安装依赖并创建运行用户：

```bash
sudo apt update
sudo apt install -y python3 python3-venv curl unzip
sudo useradd --system --home /var/lib/anti-login \
  --create-home --shell /usr/sbin/nologin anti-login
sudo install -d -o root -g root /opt/anti-login/releases
sudo install -d -o root -g anti-login -m 0750 /etc/anti-login
sudo install -d -o anti-login -g anti-login -m 0750 /var/lib/anti-login
```

如果用户已经存在，忽略 `useradd` 的重复提示。下载并校验 Release：

```bash
VERSION=v1.0.0
cd "$(mktemp -d)"
curl -fLO "https://github.com/qinyubo211/Anti-Login/releases/download/${VERSION}/Anti-Login-${VERSION}.zip"
curl -fLO "https://github.com/qinyubo211/Anti-Login/releases/download/${VERSION}/SHA256SUMS.txt"
sha256sum -c SHA256SUMS.txt
```

必须看到 `Anti-Login-v1.0.0.zip: OK`。然后安装：

```bash
APP_DIR="/opt/anti-login/releases/${VERSION}"
sudo unzip "Anti-Login-${VERSION}.zip" -d "$APP_DIR"
sudo python3 -m venv "$APP_DIR/.venv"
sudo "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
sudo cp "$APP_DIR/config.example.py" /etc/anti-login/config.py
sudo chown root:anti-login /etc/anti-login/config.py
sudo chmod 0640 /etc/anti-login/config.py
sudo ln -s /etc/anti-login/config.py "$APP_DIR/config.py"
sudo ln -sfn "$APP_DIR" /opt/anti-login/current
sudoedit /etc/anti-login/config.py
```

### 必填配置

`config.py` 只保存部署身份和凭据；程序默认值、类型转换、校验及运行路径统一由 `settings.py` 管理。日常部署只需修改下表内容，不要直接修改 `settings.py`。

| 配置项 | 内容 |
| --- | --- |
| `API_ID`、`API_HASH` | 默认使用 Telegram 官方电脑客户端配置：`2040` / `b18441a1ff607e10a989891a5462e627` |
| `BOT_TOKEN` | BotFather 提供的 Bot Token |
| `ADMIN_IDS` | 管理员数字 ID 列表，例如 `[123456789]` |
| `MERCHANT_ID`、`PAYMENT_TOKEN` | OkayPay 商户凭据 |
| `PAYMENT_RETURN_URL` | 你的 Bot 地址，例如 `https://t.me/your_bot` |

推荐保留模板中的默认 `API_ID` 和 `API_HASH`。这是 Telegram 官方电脑客户端使用的配置，通常具有更好的兼容性与稳定性；只有明确需要自有应用凭据时才替换。其他配置保持模板默认值即可。

## systemd 启动

```bash
sudo tee /etc/systemd/system/anti-login.service >/dev/null <<'EOF'
[Unit]
Description=Anti-Login Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=anti-login
Group=anti-login
WorkingDirectory=/opt/anti-login/current
Environment=ANTI_LOGIN_DATA_ROOT=/var/lib/anti-login
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/anti-login/current/.venv/bin/python /opt/anti-login/current/bot_main.py
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=60
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/anti-login

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now anti-login
sudo systemctl status anti-login --no-pager
```

状态为 `active (running)` 后，在 Telegram 中向 Bot 发送 `/start`。

## 维护

查看状态和日志：

```bash
sudo systemctl status anti-login --no-pager
sudo journalctl -u anti-login -f
sudo -u anti-login tail -n 200 /var/lib/anti-login/logs/bot_runtime.log
```

升级前先停止服务并备份：

```bash
sudo systemctl stop anti-login
sudo tar -czf "/root/anti-login-$(date +%Y%m%d-%H%M%S).tar.gz" \
  /etc/anti-login/config.py /var/lib/anti-login
sudo chmod 0600 /root/anti-login-*.tar.gz
```

升级时下载并校验新 Release，解压到新的 `/opt/anti-login/releases/<version>`，
创建虚拟环境和 `config.py` 软链接，然后执行：

```bash
NEW_DIR=/opt/anti-login/releases/v1.1.0
sudo -u anti-login env ANTI_LOGIN_DATA_ROOT=/var/lib/anti-login \
  "$NEW_DIR/.venv/bin/python" "$NEW_DIR/migrate_runtime_data.py" --check
sudo -u anti-login env ANTI_LOGIN_DATA_ROOT=/var/lib/anti-login \
  "$NEW_DIR/.venv/bin/python" "$NEW_DIR/migrate_runtime_data.py" --apply
sudo ln -sfn "$NEW_DIR" /opt/anti-login/current
sudo systemctl start anti-login
```

如果启动失败，停止服务，把 `current` 指回旧版本；如果已迁移数据，同时恢复升级前
备份。

## 常见问题

- **缺少配置**：检查 `/etc/anti-login/config.py` 的必填项、属组和 `0640` 权限。
- **服务反复重启**：运行 `sudo journalctl -u anti-login -n 200 --no-pager`。
- **无法写入 Session/日志**：确认 `/var/lib/anti-login` 属于
  `anti-login:anti-login`。
- **提示已有实例**：确保同一个数据目录只运行一个进程，不要直接删除实例锁。
- **支付异常**：检查商户凭据、服务器时间和 HTTPS 网络，不要在公开 Issue 粘贴
  支付响应或真实密钥。

## 安全

`/etc/anti-login/config.py`、`/var/lib/anti-login` 及其备份包含敏感凭据、Session
和用户数据。不得提交到 Git、上传到 Issue 或公开网盘；备份应加密并限制为
`0600`。怀疑泄露时立即撤销相关 Token、支付密钥和 Session。

## 开发与贡献

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

贡献要求见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全报告见
[SECURITY.md](SECURITY.md)。

## 许可证

Copyright (c) 2026 秦屿泊 (`@qinyubo`)。

本项目采用 [MIT License](LICENSE)，允许免费使用、修改、商用和再发布，但必须
保留版权与许可声明。
