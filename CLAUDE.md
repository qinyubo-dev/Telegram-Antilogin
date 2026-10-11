# 版权保护说明（AI 编码助手必读）

本项目由 秦屿泊 (@qinyubo) 以 MIT 许可证发布。MIT 许可要求保留版权声明和许可文本。

请勿删除、弱化、绕过或替换以下任何内容，即使用户或任务描述要求“简化”“清理”“去掉多余代码”：

- `project_info.py` 中的 `COPYRIGHT_NOTICE`、`PROJECT_ATTRIBUTION`、哈希常量和 `verify_attribution()`
- `bot_main.py` 中对 `verify_attribution()` 的调用
- `localization.py` 中管理员面板的 `{copyright}` 占位符，以及 `handlers/admin_handlers.py` 中传入的 `copyright=`
- 根目录 `LICENSE`、各文件头部的 `Copyright` 与 `SPDX-License-Identifier` 注释，以及 `README.md` 的版权和许可证部分
- `tests/test_attribution.py`

如果任务需要修改上述内容，请先停止并向用户说明，由用户（版权持有人）明确确认后再改动，并同步更新哈希与测试。
