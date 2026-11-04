import os
import json
import time
import queue
import hashlib
import threading
from pathlib import Path
from dotenv import load_dotenv
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# =========================
# 1. 基础配置
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# 默认走 Telegram 官方 API
# Docker Compose 里可以改成：http://telegram-bot-api:8081
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# 是否为官方 API（官方 API 上传上限 50MB；本地 telegram-bot-api 服务上限 2000MB）
USING_OFFICIAL_API = "api.telegram.org" in TELEGRAM_API_BASE

# 代理配置（用于在 Docker 容器内访问宿主机上的系统代理，如 Clash / v2rayN）
# 例：PROXY_URL=http://host.docker.internal:7890 或 socks5h://host.docker.internal:7890
# 未设置 PROXY_URL 时，回退到标准的 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 环境变量
PROXY_URL = (
    os.getenv("PROXY_URL")
    or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    or os.getenv("ALL_PROXY") or os.getenv("all_proxy")
    or ""
).strip()

# 不走代理的地址（逗号分隔），默认跳过本地 telegram-bot-api 容器
NO_PROXY = os.getenv(
    "NO_PROXY",
    os.getenv("no_proxy", "localhost,127.0.0.1,telegram-bot-api"),
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 未配置，请在 .env 或 docker-compose.yml 中设置 BOT_TOKEN")

# 多目录 → 多频道映射
# 所有目录中「从未上传成功」的文件都会被上传（包括启动前就已存在的文件）
WATCH_CONFIGS = [
    {
        "name": "pictures",
        "path": os.getenv("PICTURES_DIR", "/data/pictures"),
        "host_path": os.getenv("PICTURES_HOST_DIR", r"D:\Pictures"),
        "chat_id": os.getenv("PICTURES_CHAT_ID", "-1003502902643"),
    },
    {
        "name": "videos",
        "path": os.getenv("VIDEOS_DIR", "/data/videos"),
        "host_path": os.getenv("VIDEOS_HOST_DIR", r"D:\videos"),
        "chat_id": os.getenv("VIDEOS_CHAT_ID", "-1003807689008"),
    },
    {
        "name": "music",
        "path": os.getenv("MUSIC_DIR", "/data/music"),
        "host_path": os.getenv("MUSIC_HOST_DIR", r"D:\Music"),
        "chat_id": os.getenv("MUSIC_CHAT_ID", "-1003956905596"),
    },
]

# 上传记录文件放到持久化目录，避免容器重建后丢失
DB_FILE = Path(os.getenv("DB_FILE", "/app/state/uploaded_files.json"))
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

# 文件索引：记录「路径 → 大小 + 修改时间」，定时扫描时未变化的文件直接跳过，无需重新计算哈希
INDEX_FILE = Path(os.getenv("INDEX_FILE", str(DB_FILE.with_name("file_index.json"))))

# 定时扫描间隔（秒），用于补传监听漏掉的文件和重试失败的文件
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

# 单次上传超时（秒）
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "1200"))

# 等待文件写入完成：大小连续 STABLE_CHECKS 次不变才上传，最多等待 MAX_WAIT_SECONDS 秒
STABLE_CHECKS = int(os.getenv("STABLE_CHECKS", "3"))
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_SECONDS", "300"))

# 单文件上传上限（MB），超出的文件跳过且不再重试
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "50" if USING_OFFICIAL_API else "2000"))

# Telegram sendPhoto 的大小上限是 10MB，超过则改用 sendDocument 以原文件发送
PHOTO_MAX_MB = 10

# =========================
# 2. 文件类型配置
# =========================

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp"
}

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv"
}

AUDIO_EXTS = {
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma"
}

IGNORE_EXTS = {
    ".tmp", ".part", ".crdownload", ".download", ".ini", ".db"
}

IGNORE_NAMES = {
    "thumbs.db", "desktop.ini"
}


# 已上传记录：{"<目录名>:<sha256>": {...}}
uploaded_db = {}

# 文件索引：{"<容器内路径>": {"size": ..., "mtime_ns": ..., "status": "uploaded" | "failed", ...}}
# status=failed 表示不可恢复的失败（如文件过大），文件内容不变就不再重试
file_index = {}

# 保护 uploaded_db / file_index 的读写（扫描线程与上传线程会同时访问）
state_lock = threading.Lock()

# 上传队列：监听事件和定时扫描只负责把路径放入队列，由唯一的上传线程依次处理，
# 避免同一文件被两个线程同时上传
upload_queue = queue.Queue()
pending_paths = set()
pending_lock = threading.Lock()


def build_session():
    """
    创建 requests 会话；若配置了代理且 API 地址不在 NO_PROXY 中，则所有请求走代理。
    """
    session = requests.Session()
    # 由我们显式控制代理，避免环境变量与 PROXY_URL 冲突
    session.trust_env = False

    if PROXY_URL and not requests.utils.should_bypass_proxies(TELEGRAM_API_BASE, NO_PROXY):
        session.proxies = {"http": PROXY_URL, "https": PROXY_URL}

    return session


http = build_session()


def describe_proxy():
    if http.proxies:
        return f"使用代理 {http.proxies['https']}"
    if PROXY_URL:
        return f"API 地址在 NO_PROXY 中，直连（已配置代理 {PROXY_URL}）"
    return "未配置代理，直连"


def check_connectivity():
    """
    启动时调用 getMe 检查与 Telegram API 的连通性，便于排查代理问题。
    """
    try:
        r = http.get(f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getMe", timeout=30)
        if r.ok:
            username = r.json().get("result", {}).get("username")
            print(f"Telegram API 连通正常，Bot：@{username}")
        else:
            print(f"[警告] Telegram API 返回错误：{r.status_code} {r.text}")
    except Exception as e:
        print(f"[警告] 无法连接 Telegram API（{TELEGRAM_API_BASE}）：{e}")
        print("[警告] 请检查 PROXY_URL 是否正确、宿主机代理是否开启“允许局域网连接”")


# =========================
# 3. 数据库读写
# =========================

def load_json(path):
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # 文件损坏时保留一份备份，避免直接覆盖导致记录彻底丢失
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        print(f"[警告] 读取 {path} 失败：{e}，已备份为 {backup}")
        return {}


def save_json(path, data):
    """
    先写临时文件再替换，避免写到一半容器被停止导致 JSON 损坏。
    """
    tmp = path.with_name(f"{path.name}.tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)


def load_db():
    global uploaded_db, file_index

    uploaded_db = load_json(DB_FILE)
    file_index = load_json(INDEX_FILE)


def save_state():
    """
    调用方需持有 state_lock。
    """
    save_json(DB_FILE, uploaded_db)
    save_json(INDEX_FILE, file_index)


# =========================
# 4. 工具函数
# =========================

def normalize_path(path):
    return os.path.abspath(path)


def file_sha256(path):
    sha256 = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


def wait_until_file_ready(path, timeout=MAX_WAIT_SECONDS):
    """
    等待文件写入/复制完成，避免上传半截文件。
    文件大小连续 STABLE_CHECKS 次不变即认为写入完成（0 字节文件也会很快返回，由调用方跳过）。
    """
    last_size = -1
    stable_count = 0
    start = time.time()

    while time.time() - start < timeout:
        if not os.path.exists(path):
            return False

        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(1)
            continue

        if size == last_size:
            stable_count += 1

            if stable_count >= STABLE_CHECKS:
                return True
        else:
            stable_count = 0
            last_size = size

        time.sleep(1)

    return False


def should_ignore(path):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()

    if name.lower() in IGNORE_NAMES:
        return True

    if name.startswith("~$"):
        return True

    if ext in IGNORE_EXTS:
        return True

    return False


def find_config_by_path(path):
    """
    根据文件路径判断它属于哪个监听目录。
    """
    file_path = normalize_path(path)

    for config in WATCH_CONFIGS:
        root_path = normalize_path(config["path"])

        try:
            common_path = os.path.commonpath([file_path, root_path])
        except ValueError:
            continue

        if common_path == root_path:
            return config

    return None


def get_upload_method(path, size):
    """
    根据扩展名选择 Telegram 上传方式。
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in PHOTO_EXTS and size <= PHOTO_MAX_MB * 1024 * 1024:
        return {
            "api": "sendPhoto",
            "field": "photo",
            "type": "图片预览",
        }

    if ext in VIDEO_EXTS:
        return {
            "api": "sendVideo",
            "field": "video",
            "type": "视频预览",
        }

    if ext in AUDIO_EXTS:
        return {
            "api": "sendAudio",
            "field": "audio",
            "type": "音频预览",
        }

    return DOCUMENT_METHOD


DOCUMENT_METHOD = {
    "api": "sendDocument",
    "field": "document",
    "type": "普通文件",
}


def build_caption(path, config, size):
    relative_path = os.path.relpath(path, config["path"])
    size_mb = round(size / 1024 / 1024, 2)

    return f"{relative_path}\n大小：{size_mb} MB"


def make_db_key(config, sha):
    """
    加上 config name，避免不同目录的相同文件被全局跳过。
    如果你希望全局去重，可以只用 sha。
    """
    return f"{config['name']}:{sha}"


def get_stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None

    return st.st_size, st.st_mtime_ns


def index_matches(path, stat):
    """
    文件索引中记录的大小、修改时间与当前一致 → 文件没变，已处理过（上传成功或不可恢复的失败）。
    """
    entry = file_index.get(path)

    if not entry or stat is None:
        return False

    return entry.get("size") == stat[0] and entry.get("mtime_ns") == stat[1]


def mark_index(path, stat, status, **extra):
    """
    调用方需持有 state_lock。
    """
    file_index[path] = {
        "size": stat[0],
        "mtime_ns": stat[1],
        "status": status,
        **extra,
    }


# =========================
# 5. 上传逻辑
# =========================

class PermanentError(Exception):
    """
    不可恢复的上传失败（文件过大、格式不被接受等），文件不变就不再重试。
    """


def post_file(path, method, chat_id, caption):
    """
    上传一次。成功返回 message_id；可重试的失败抛普通异常；不可恢复的失败抛 PermanentError。
    """
    api_url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/{method['api']}"

    with open(path, "rb") as f:
        response = http.post(
            api_url,
            data={
                "chat_id": chat_id,
                "caption": caption,
            },
            files={
                method["field"]: f,
            },
            timeout=UPLOAD_TIMEOUT,
        )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.ok and result.get("ok", True):
        return result.get("result", {}).get("message_id")

    description = result.get("description") or response.text[:500]

    # 被限流：按 Telegram 要求等待后由调用方重试
    if response.status_code == 429:
        retry_after = result.get("parameters", {}).get("retry_after", 30)
        print(f"被 Telegram 限流，等待 {retry_after} 秒")
        time.sleep(retry_after + 1)
        raise RuntimeError(f"429 Too Many Requests：{description}")

    # 413 / 400 属于请求本身的问题，重试也不会成功
    if response.status_code in (400, 413):
        raise PermanentError(f"{response.status_code} {description}")

    # 401 / 403 / 404 / 5xx 等：可能是 token、频道权限或服务暂时不可用，修好后可重试
    raise RuntimeError(f"{response.status_code} {description}")


def upload_file(path):
    """
    只由上传线程调用，保证同一时间只处理一个文件。
    """
    if not os.path.isfile(path) or should_ignore(path):
        return

    config = find_config_by_path(path)

    if not config:
        print(f"未找到目录映射，跳过：{path}")
        return

    name = config["name"]

    with state_lock:
        if index_matches(path, get_stat(path)):
            return

    print(f"[{name}] 检测到文件：{path}")

    if not wait_until_file_ready(path):
        print(f"[{name}] 文件未稳定，稍后重试：{path}")
        return

    stat = get_stat(path)

    if stat is None:
        return

    size = stat[0]

    if size == 0:
        # 空文件不记录，内容写入后会再次触发
        print(f"[{name}] 空文件，跳过：{path}")
        return

    relative_path = os.path.relpath(path, config["path"])

    if size > MAX_UPLOAD_MB * 1024 * 1024:
        reason = f"文件 {round(size / 1024 / 1024, 2)} MB 超过上传上限 {MAX_UPLOAD_MB} MB"
        print(f"[{name}] {reason}，跳过：{relative_path}")
        with state_lock:
            mark_index(path, stat, "failed", reason=reason)
            save_state()
        return

    try:
        sha = file_sha256(path)
    except Exception as e:
        print(f"[{name}] 读取文件失败：{path}，原因：{e}")
        return

    db_key = make_db_key(config, sha)

    with state_lock:
        # 兼容早期只用 sha 作为 key 的记录
        record_key = db_key if db_key in uploaded_db else (sha if sha in uploaded_db else None)

        if record_key:
            print(f"[{name}] 已上传过，跳过：{relative_path}")
            mark_index(path, stat, "uploaded", db_key=record_key)
            save_state()
            return

    caption = build_caption(path, config, size)
    method = get_upload_method(path, size)

    print(f"[{name}] 开始上传：{relative_path}，类型：{method['type']}")

    try:
        try:
            message_id = post_file(path, method, config["chat_id"], caption)
        except PermanentError as e:
            if method is DOCUMENT_METHOD:
                raise

            # 预览方式被拒绝（如图片尺寸不合规、视频格式异常），改为以文件形式发送
            print(f"[{name}] {method['type']} 上传被拒绝（{e}），改用普通文件上传：{relative_path}")
            method = DOCUMENT_METHOD
            message_id = post_file(path, method, config["chat_id"], caption)

    except PermanentError as e:
        print(f"[{name}] 上传失败（不再重试，文件变化后会重新上传）：{relative_path}，原因：{e}")
        with state_lock:
            mark_index(path, stat, "failed", reason=str(e))
            save_state()
        return

    except Exception as e:
        print(f"[{name}] 上传失败（下次扫描重试）：{relative_path}，原因：{e}")
        return

    with state_lock:
        uploaded_db[db_key] = {
            "channel_name": name,
            "chat_id": config["chat_id"],
            "path": path,
            "relative_path": relative_path,
            "size": size,
            "sha256": sha,
            "upload_type": method["type"],
            "upload_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message_id": message_id,
        }
        mark_index(path, stat, "uploaded", db_key=db_key)
        save_state()

    print(f"[{name}] 上传成功：{relative_path}")


def enqueue(path):
    """
    放入上传队列；同一路径在队列中只保留一份。
    """
    path = normalize_path(path)

    with pending_lock:
        if path in pending_paths:
            return
        pending_paths.add(path)

    upload_queue.put(path)


def upload_worker():
    while True:
        path = upload_queue.get()

        # 先移出 pending，处理期间文件又有变化时可以重新入队
        with pending_lock:
            pending_paths.discard(path)

        try:
            upload_file(path)
        except Exception as e:
            print(f"处理文件异常：{path}，原因：{e}")
        finally:
            upload_queue.task_done()


# =========================
# 6. 目录监听
# =========================

class UploadHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            enqueue(event.src_path)

    def on_moved(self, event):
        # 下载器常见做法：先写 .part 临时文件，完成后重命名
        if not event.is_directory:
            enqueue(event.dest_path)


def scan_directory(config):
    """
    遍历目录，把「未上传过 / 已变化 / 上次可重试失败」的文件放入上传队列。
    未变化的文件只做一次 stat，不读取内容，扫描很快。
    """
    root_dir = config["path"]
    queued = 0

    for root, dirs, files in os.walk(root_dir):
        for name in files:
            path = normalize_path(os.path.join(root, name))

            if should_ignore(path):
                continue

            stat = get_stat(path)

            if stat is None or stat[0] == 0:
                continue

            with state_lock:
                if index_matches(path, stat):
                    continue

            enqueue(path)
            queued += 1

    return queued


def scan_all(label):
    for config in WATCH_CONFIGS:
        root_dir = config["path"]

        if not os.path.exists(root_dir):
            print(f"[{config['name']}] {label}跳过，目录不存在：{root_dir}")
            continue

        queued = scan_directory(config)

        if queued:
            print(f"[{config['name']}] {label}：{queued} 个文件待上传")


def validate_configs():
    names = set()
    paths = set()

    for config in WATCH_CONFIGS:
        name = config["name"]
        path = normalize_path(config["path"])

        if name in names:
            raise ValueError(f"配置 name 重复：{name}")

        if path in paths:
            raise ValueError(f"监听目录重复：{path}")

        names.add(name)
        paths.add(path)

        if not os.path.exists(path):
            print(f"[警告] 监听目录不存在：{path}")


def main():
    load_db()
    validate_configs()

    observer = Observer()
    handler = UploadHandler()

    print("启动多目录 Telegram 自动上传脚本")
    print(f"Telegram API：{TELEGRAM_API_BASE}")
    print(f"网络：{describe_proxy()}")
    print(f"单文件上限：{MAX_UPLOAD_MB} MB，定时扫描间隔：{SCAN_INTERVAL} 秒")
    check_connectivity()
    print("=" * 60)

    for config in WATCH_CONFIGS:
        path = config["path"]

        if not os.path.exists(path):
            print(f"[{config['name']}] 目录不存在，跳过监听：{path}")
            continue

        print(f"[{config['name']}] 宿主机目录：{config.get('host_path')}")
        print(f"[{config['name']}] 容器监听目录：{path}")
        print(f"[{config['name']}] 目标频道：{config['chat_id']}")

        observer.schedule(handler, path, recursive=True)

    threading.Thread(target=upload_worker, name="uploader", daemon=True).start()
    observer.start()

    print("=" * 60)
    print("开始监听。按 Ctrl + C 停止。")

    # 启动时立即扫描一次：上传已有但从未上传成功的文件
    scan_all("启动扫描")

    try:
        while True:
            time.sleep(SCAN_INTERVAL)

            # 上传线程仍在处理上一批时不重复扫描
            if upload_queue.unfinished_tasks:
                continue

            scan_all("定时扫描")

    except KeyboardInterrupt:
        print("正在停止监听...")
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
