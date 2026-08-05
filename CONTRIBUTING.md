# Contributing

由作者 [秦屿泊 @qinyubo](https://t.me/qinyubo) 维护运行的 [Anti-Login](https://t.me/QinShield_Bot)。

Contributions are welcome. Anti-Login is tested with Python 3.12 and 3.14.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Requirements

1. Never commit real configuration, tokens, credentials, Session files,
   runtime JSON/JSONL data, logs, caches, backups, or generated release
   archives.
2. Put user-visible text in `localization.py`. Chinese and English catalogs
   must have identical keys, placeholders, and behavior.
3. Keep tests deterministic across Linux and Windows. Do not depend on machine
   uptime, wall-clock timezone, network access, or execution order.
4. Run the complete test suite before submitting. For localization changes,
   also run:

   ```bash
   python -m pytest tests/test_localization.py tests/test_english_coverage.py -q
   ```

5. Keep changes focused, document operational changes in README, and preserve
   all MIT and copyright notices.

Do not put vulnerability details or real secrets in a public issue. Follow
[SECURITY.md](SECURITY.md) instead.

By contributing, you agree that your contribution is provided under the MIT
License.

Developed and open-sourced by 秦屿泊 (`@qinyubo`).
