import os
import json
import time
import hashlib
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

# 多目录 → 多频道映射
# name: 只是本地标识，方便日志显示
# path: 需要监听的本地目录
# chat_id: 对应 Telegram 私有频道 ID
# scan_existing: 启动时是否扫描并上传已有文件
WATCH_CONFIGS = [
    {
        "name": "pictures",
        "path": r"D:\Pictures",
        "chat_id": "-1003502902643",
        "scan_existing": False,
    },
    {
        "name": "videos",
        "path": r"D:\videos",
        "chat_id": "-1003807689008",
        "scan_existing": False,
    },
    {
        "name": "music",
        "path": r"D:\Music",
        "chat_id": "-1003956905596",
        "scan_existing": False,
    },
]

DB_FILE = Path(__file__).with_name("uploaded_files.json")


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


uploaded_db = {}


# =========================
# 3. 数据库读写
# =========================

def load_db():
    global uploaded_db

    if DB_FILE.exists():
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                uploaded_db = json.load(f)
        except Exception:
            uploaded_db = {}
    else:
        uploaded_db = {}


def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(uploaded_db, f, ensure_ascii=False, indent=2)


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


def wait_until_file_ready(path, timeout=300):
    """
    等待文件写入/复制完成，避免上传半截文件。
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

        if size == last_size and size > 0:
            stable_count += 1

            if stable_count >= 3:
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


def get_upload_method(path):
    """
    根据扩展名选择 Telegram 上传方式。
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in PHOTO_EXTS:
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

    return {
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


# =========================
# 5. 上传逻辑
# =========================

def upload_file(path):
    if not os.path.isfile(path):
        return

    if should_ignore(path):
        return

    config = find_config_by_path(path)

    if not config:
        print(f"未找到目录映射，跳过：{path}")
        return

    print(f"[{config['name']}] 检测到文件：{path}")

    if not wait_until_file_ready(path):
        print(f"[{config['name']}] 文件未稳定，跳过：{path}")
        return

    try:
        size = os.path.getsize(path)
        sha = file_sha256(path)
    except Exception as e:
        print(f"[{config['name']}] 读取文件失败：{path}，原因：{e}")
        return

    db_key = make_db_key(config, sha)

    if db_key in uploaded_db:
        print(f"[{config['name']}] 已上传过，跳过：{path}")
        return

    relative_path = os.path.relpath(path, config["path"])
    caption = build_caption(path, config, size)

    method = get_upload_method(path)

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method['api']}"

    print(f"[{config['name']}] 开始上传：{relative_path}，类型：{method['type']}")

    try:
        with open(path, "rb") as f:
            response = requests.post(
                api_url,
                data={
                    "chat_id": config["chat_id"],
                    "caption": caption,
                },
                files={
                    method["field"]: f,
                },
                timeout=1200,
            )

        if response.ok:
            result = response.json()

            uploaded_db[db_key] = {
                "channel_name": config["name"],
                "chat_id": config["chat_id"],
                "path": path,
                "relative_path": relative_path,
                "size": size,
                "sha256": sha,
                "upload_type": method["type"],
                "upload_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": result.get("result", {}).get("message_id"),
            }

            save_db()

            print(f"[{config['name']}] 上传成功：{relative_path}")

        else:
            print(f"[{config['name']}] 上传失败：{relative_path}")
            print(response.text)

    except Exception as e:
        print(f"[{config['name']}] 上传异常：{relative_path}，原因：{e}")


# =========================
# 6. 目录监听
# =========================

class UploadHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            upload_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            upload_file(event.src_path)


def upload_existing_files(config):
    root_dir = config["path"]

    if not os.path.exists(root_dir):
        print(f"[{config['name']}] 目录不存在，跳过扫描：{root_dir}")
        return

    print(f"[{config['name']}] 扫描已有文件：{root_dir}")

    for root, dirs, files in os.walk(root_dir):
        for name in files:
            path = os.path.join(root, name)
            upload_file(path)


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
    print("=" * 60)

    for config in WATCH_CONFIGS:
        path = config["path"]

        if not os.path.exists(path):
            print(f"[{config['name']}] 目录不存在，跳过监听：{path}")
            continue

        print(f"[{config['name']}] 监听目录：{path}")
        print(f"[{config['name']}] 目标频道：{config['chat_id']}")

        if config.get("scan_existing", False):
            upload_existing_files(config)
        else:
            print(f"[{config['name']}] 跳过已有文件，只监听新增/修改文件")

        observer.schedule(handler, path, recursive=True)

    observer.start()

    print("=" * 60)
    print("开始监听。按 Ctrl + C 停止。")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("正在停止监听...")
        observer.stop()

    observer.join()


if __name__ == "__main__":
    main()