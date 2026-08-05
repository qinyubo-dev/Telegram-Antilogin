# Security Policy

由作者 [秦屿泊 @qinyubo](https://t.me/qinyubo) 维护运行的 [Anti-Login](https://t.me/QinShield_Bot)。

## Reporting a vulnerability

Do not disclose credentials, Telegram Session files, payment secrets, private
keys, or personal data in a public issue.

Contact the maintainer privately through the channel listed by 秦屿泊
(`@qinyubo`) and include only the minimum information needed to reproduce the
problem. Use sanitized logs and synthetic accounts. If a credential may have
been exposed, revoke it before sending any report.

Security reports should include the affected release, impact, reproduction
steps, and a proposed mitigation when available. Do not attach production
Session databases, configuration files, backups, payment payloads, or user
records.

## Supported version

Security fixes target the latest published release. Operators should verify
Release SHA-256 checksums, keep the service account unprivileged, protect
`/etc/anti-login/config.py`, and restrict `/var/lib/anti-login` and its backups.

Copyright (c) 2026 秦屿泊 (`@qinyubo`). Licensed under MIT.
