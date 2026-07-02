# TG_Auto_Upload

监听并自动将本地文件夹中新添加或修改的文件上传到指定的 Telegram 频道/群组。

主要场景：把本地的图片、视频、音频或任意文件自动同步到 Telegram 频道，用于备份、分发或媒体发布自动化。

## 功能

- 监控多个目录（可为每个目录分配不同的目标频道）
- 支持按文件扩展名自动选择 Telegram 上传方法（sendPhoto / sendVideo / sendAudio / sendDocument）
- 防止重复上传（基于 SHA256 + 目录名的去重策略）
- 可选择在启动时扫描已有文件或仅监听新增/修改的文件
- 以 JSON 文件持久化已上传记录（便于容器重建后恢复状态）

## 技术栈

- 语言：Python 3.12+
- 主要依赖：python-dotenv, requests, watchdog
- 容器：Dockerfile + docker-compose.yml（提供本地/容器化部署示例）

## 仓库结构（重要文件）

```
Dockerfile                # 镜像构建配置
docker-compose.yml        # 推荐的 docker-compose 示例（含本地 telegram-bot-api 服务）
pyproject.toml            # Python 依赖声明
tg_folder_watch.py        # 主脚本，包含目录监听与上传逻辑
uploaded_files.json       # 示例的已上传记录（演示用）
telegram-bot-api-data/    # 用于本地运行 telegram-bot-api 的持久化数据目录
README.md                 # 本文件
```

## 快速开始（使用 Docker Compose，推荐）

1. 复制仓库到宿主机：

```bash
git clone https://github.com/CGTappear/TG_Auto_Upload.git
cd TG_Auto_Upload
```

2. 在仓库根目录创建 `.env` 文件并设置必需变量（示例见下）：

```env
# 必需
BOT_TOKEN=123456:ABC-DEF...your-bot-token

# 可选（如果使用本地 telegram-bot-api 映射）
TELEGRAM_API_ID=xxxxx
TELEGRAM_API_HASH=yyyyy
```

3. 编辑 `docker-compose.yml` 中的卷映射以匹配你的宿主机目录（示例中为 Windows 路径 `D:/Pictures` 等）。

4. 启动服务：

```bash
docker compose up -d --build
```

5. 查看日志并确认已启动：

```bash
docker compose logs -f cgtappear-uploader
```

## 本地运行（开发/调试）

1. 创建并激活 Python 虚拟环境（推荐 Python 3.12+）：

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

2. 安装依赖：

```bash
pip install python-dotenv requests watchdog
```

3. 创建 `.env` 并设置 BOT_TOKEN 等环境变量（同 Docker 部分）。

4. 运行脚本：

```bash
python tg_folder_watch.py
```

## 配置说明

主要配置位于脚本顶部的 WATCH_CONFIGS 列表和环境变量：

- BOT_TOKEN (必需)：Telegram Bot Token，用于调用 Telegram Bot API。
- TELEGRAM_API_BASE：Telegram API 基础地址，默认 `https://api.telegram.org`。如果在同一 Docker Compose 中运行本地 `telegram-bot-api` 服务，可设置为 `http://telegram-bot-api:8081`。
- WATCH_CONFIGS：在 `tg_folder_watch.py` 中定义的多目录映射，每个条目包含：
  - name：该监听项名称（用于区分不同目录的去重键）
  - path：容器内监听目录（如 `/data/pictures`）
  - host_path：宿主机路径（仅用于打印/参考，容器运行时在 docker-compose.yml 中映射）
  - chat_id：目标频道/群组 ID（例如 `-1001234567890`）
  - scan_existing：启动时是否扫描已有文件（true/false，默认 false）

- PICTURES_DIR / VIDEOS_DIR / MUSIC_DIR：在 docker-compose 中示例化的环境变量，分别对应 WATCH_CONFIGS 中的 path。
- DB_FILE：已上传记录的 json 文件路径，容器默认 `/app/state/uploaded_files.json`（该目录在 docker-compose.yml 中映射到宿主机的 `./state`，确保持久化）。

## 文件类型与忽略规则

默认支持的扩展名：

- 图片：.jpg .jpeg .png .webp
- 视频：.mp4 .mov .mkv .webm .avi .flv .wmv
- 音频：.mp3 .flac .wav .m4a .aac .ogg .wma

忽略的扩展名与文件名（默认）：.tmp .part .crdownload .download .ini .db 以及 thumbs.db、desktop.ini 和以 `~$` 开头的临时文件。

上传方式会根据扩展名自动选择 Telegram API（sendPhoto / sendVideo / sendAudio / sendDocument），普通二进制文件使用 sendDocument。

## 去重策略

- 使用文件的 SHA256 摘要与监听配置的 `name` 组合（`{name}:{sha256}`）作为去重键，保存到 DB_FILE 中。
- 这样可以避免同一目录中重复上传，但允许不同目录上传内容相同的文件（如果你希望全局去重，可修改 `make_db_key` 只使用 sha 值）。

## 如何更改监听目录或频道

编辑 `WATCH_CONFIGS` 列表（在 `tg_folder_watch.py` 顶部），或通过环境变量设置对应的 `*_DIR` 与 `*_CHAT_ID`。如果使用 Docker，请确保在 `docker-compose.yml` 中正确映射宿主机目录到容器内的监听路径。

## 持久化与状态恢复

- 已上传文件信息写入 `DB_FILE`（默认 `/app/state/uploaded_files.json`），请确保该路径被挂载为持久化卷，以避免容器重建后丢失上传记录。

## 常见问题与故障排除

- 上传失败或返回 403/401：检查 BOT_TOKEN 是否有效，目标 chat_id 是否允许 bot 发送消息（频道 bot 需为管理员或已加入）。
- 大文件上传较慢或超时：脚本对 requests.post 的 timeout 设置为 1200 秒，可根据需要调整；也需注意 Telegram 的限制（文件大小上限等）。
- 权限问题：容器内监听目录需要有读取权限；在宿主机上运行时确保 Python 进程对指定目录有访问权限。
- 容器内路径与宿主机路径不一致：docker-compose 示例已展示如何将宿主机路径映射到容器内路径，请根据你的系统（Windows/Mac/Linux）修改卷映射语法。

## 自定义与扩展建议

- 增加对图片压缩/缩略图/元数据提取的逻辑（在上传前处理文件）。
- 将上传记录迁移到数据库（SQLite / PostgreSQL）以便更复杂的查询与清理策略。
- 支持按文件名模式或子目录分配不同的 chat_id（更细粒度的路由）。
- 在上传失败时实现重试队列与告警通知。

## 许可

（在此处添加许可证，例如 MIT）

---

如果你希望我直接把这个 README.md 写入到仓库并提交，我可以为你创建/更新该文件。