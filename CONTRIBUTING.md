# Contributing

感谢你对 X-Agent 的改进建议和代码贡献。

## 本地开发

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果要测试旧版 Playwright 采集器，再安装：

```powershell
python -m pip install -r requirements-playwright.txt
python -m playwright install chromium
```

## 提交前检查

```powershell
python -m unittest discover -v
node --check edge-extension/content.js
node --check edge-extension/popup.js
git diff --check
```

不要提交以下内容：

- `.env`、`.collector-token` 或任何 API key；
- `output/`、`output-by-date/`、`*.jsonl`、`*.sqlite3*` 等个人采集数据；
- Edge/Playwright profile、Cookie、截图和日志；
- 从 X 帖子复制的、没有再分发授权的内容。

提交信息请说明变更目的和验证方式。涉及扩展交互时，最好附上手动复现步骤；涉及数据格式时，同时更新 `docs/architecture.md` 和 README。
