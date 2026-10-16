# X-Agent：本地 X 帖子采集与摘要

X-Agent 是一个 **Edge 扩展 + Python 本地服务**，用于在用户已经登录的 X（原 Twitter）标签页中采集可见帖子，实时保存、永久去重、按北京时间归档，并生成本地或可选的模型摘要。

项目采用本地优先设计：扩展不读取或导出 Cookie，不调用 X 私有 API；Python 服务默认只监听 `127.0.0.1`。请只采集你有权正常查看的内容，并遵守 X 的条款和当地法律。

## 功能

- 使用当前 Edge 中已经登录的 X 会话，不需要把 Cookie 复制给脚本。
- 自动点击帖子正文中的 `Show more`，尽量保存完整文字。
- 保留正文，同时追加媒体标记：`[视频]`、`[图片]`、`[文章]`。
- 自动滚动并持续采集，**不设置帖子总数上限**；通过扩展中的“停止并总结”结束本轮。
- 按帖子 ID，以及“作者 + 标准化正文”双重永久去重。
- 每条帖子实时写入，浏览器或服务中断时尽量保留已经收到的内容。
- 每轮结束后生成摘要，并把有明确发布时间的帖子按北京时间增量归档到每日文件。
- 无 API key 时使用本地统计式摘要；配置 `OPENAI_API_KEY` 后可选择模型摘要。

## 项目结构

```text
.
├── edge-extension/             # Edge Manifest V3 扩展
│   ├── background.js            # 本地连接、后台保活
│   ├── content.js               # 展开、提取、滚动、前端去重
│   ├── manifest.json
│   ├── popup.html / popup.js    # 设置与开始/停止操作
│   └── popup.css
├── x_collector_server.py        # 本地 HTTP 服务与采集生命周期
├── x_digest.py                  # 帖子模型、写入器、摘要与旧版采集器
├── organize_posts_by_date.py    # 北京时间增量归档
├── test_*.py                    # 离线测试
├── docs/architecture.md         # 组件、数据流和安全边界
├── CONTRIBUTING.md
├── SECURITY.md
├── requirements.txt
├── requirements-playwright.txt  # 仅旧版 Playwright 采集器需要
└── xscraper.bat                 # Windows 便捷启动脚本
```

## 快速开始（Windows + Edge）

### 1. 创建环境并安装依赖

在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活脚本，也可以直接使用 `.venv\Scripts\python.exe`，不必激活环境。

### 2. （可选）配置模型摘要

不配置时，程序仍能运行并生成本地统计摘要。需要模型摘要时：

```powershell
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填写 `OPENAI_API_KEY`。`.env` 已被 `.gitignore` 忽略，绝不要把真实 key 提交到 GitHub。

### 3. 加载 Edge 扩展

1. 在 Edge 地址栏打开 `edge://extensions`。
2. 开启“开发人员模式”。
3. 点击“加载解压缩的扩展”。
4. 选择本项目中的 `edge-extension` 文件夹。
5. 回到已登录的 `https://x.com/` 页面并刷新。

修改扩展代码后，在 `edge://extensions` 点击“重新加载”，再刷新 X 页面。

### 4. 启动服务

推荐使用虚拟环境：

```powershell
.venv\Scripts\python.exe x_collector_server.py
```

也可以双击 `xscraper.bat`。它会先切换到脚本所在的项目目录，优先使用 `.venv`，不存在时再尝试系统 PATH 中的 `python`，不依赖开发者电脑上的固定 Python 路径。

启动后终端会显示：

```text
地址：http://127.0.0.1:8765
扩展连接令牌：...
```

第一次使用时把令牌复制到扩展弹窗。令牌会保存到本机 `.collector-token`，服务重启后保持不变。

### 5. 开始和结束采集

1. 在 X 中打开首页、搜索结果、用户主页、收藏或其他时间线。
2. 点击“X 帖子采集与摘要”扩展。
3. 填写本地服务显示的连接令牌。
4. 设置滚动间隔并点击“开始采集”。
5. 需要结束时点击“停止并总结”。

采集没有帖子总数上限。关闭扩展弹窗不会停止采集；Edge 最小化或标签页进入睡眠时，后台保活机制会尝试继续，但速度可能变慢。若要提高稳定性，可将 `https://x.com` 加入 Edge 的“永不使这些网站进入睡眠状态”。

摘要生成后 Python 服务不会退出，可以直接在另一个 X 时间线再次点击“开始采集”。

## 输出和数据生命周期

默认生成的目录均为本地个人数据，已在 `.gitignore` 中排除：

| 路径 | 用途 |
| --- | --- |
| `output/browsed-*.jsonl` | 实时追加的结构化帖子，中断时也尽量保留 |
| `output/browsed-*.md` | 实时追加的可读版本 |
| `output/posts-*.json` | 本轮完成后的完整数据 |
| `output/summary-*.md` | 本轮摘要 |
| `output/collector-history.sqlite3` | 永久去重索引 |
| `output-by-date/YYYYMMDD.json` | 按北京时间排序的每日归档 |

没有明确时区的发布时间不会进入按日期归档。SQLite 可能产生 `collector-history.sqlite3-wal` 和 `collector-history.sqlite3-shm`，这些文件同样属于本地运行数据，不应提交。

如果需要手动重新归档已有批次：

```powershell
.venv\Scripts\python.exe organize_posts_by_date.py
```

## 配置参数

```powershell
.venv\Scripts\python.exe x_collector_server.py --help
```

常用参数：

- `--port`：本地服务端口，默认 `8765`。
- `--output-dir`：批次输出目录，默认 `output`。
- `--date-output-dir`：北京时间归档目录，默认 `output-by-date`。
- `--history-db`：自定义 SQLite 历史索引位置。
- `--summarizer auto|local|openai`：摘要模式，默认自动选择。
- `--model`：模型名称，默认读取 `OPENAI_MODEL`。

## 开发与验证

不需要登录 X，也不需要联网即可运行 Python 测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -v
node --check edge-extension/content.js
node --check edge-extension/popup.js
git diff --check
```

完整架构、数据流、信任边界和扩展发布检查表见 [docs/architecture.md](docs/architecture.md)。贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请阅读 [SECURITY.md](SECURITY.md)。

## 常见问题

### “无法连接 Python 服务”

确认服务仍在运行、端口没有被占用，并把终端显示的完整令牌粘贴到扩展。服务只监听本机，不需要也不应该把端口暴露到局域网。

### 点击“开始采集”没有反应

在 `edge://extensions` 重新加载扩展，刷新当前 X 页面，再重新打开扩展弹窗。确认当前标签页地址是 `x.com` 或 `twitter.com`。

### 最小化 Edge 后采集变慢

这是 Edge 对后台页面计时器和标签页睡眠的限制。把 X 加入 Edge 的“不进入睡眠”例外列表，并适当增大滚动间隔。

### `collector-history.sqlite3-wal` 越来越大

这是 SQLite WAL 写前日志。先停止 Python 服务，再确认没有其他采集进程运行，然后可安全删除同目录下的 `-wal` 和 `-shm` 文件；主数据库 `collector-history.sqlite3` 不要删除，否则会丢失永久去重历史。

### 如何停止

优先点击扩展中的“停止并总结”。如果服务终端收到 `Ctrl+C`，服务也会尝试为已经收到的帖子生成摘要。

## 许可与第三方服务

本项目采用 [MIT License](LICENSE)。除非另有说明，仓库中的源代码按 MIT 协议授权。

使用 OpenAI 摘要时，仍需遵守 OpenAI API 的适用条款；使用 X 和 Edge 扩展时，也需遵守 X、Microsoft Edge 及当地法律的适用条款。采集到的帖子正文、图片、视频和文章不因本项目采用 MIT 协议而自动获得再分发授权，请勿把个人采集数据提交到公开仓库。

