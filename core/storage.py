import json
import os
import threading
import time
from typing import List, Optional
from pathlib import Path

from .models import PrintTask, AppConfig


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
CONFIG_FILE = DATA_DIR / "config.json"
EXPORT_LOG_FILE = DATA_DIR / "export_log.json"
EXPORT_RECORDS_FILE = DATA_DIR / "export_records.json"
EXPORT_RECORD_UI_STATE_FILE = DATA_DIR / "export_record_ui_state.json"


class Storage:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._rw_lock = threading.RLock()

    def save_tasks(self, tasks: List[PrintTask]) -> None:
        with self._rw_lock:
            data = [t.to_dict() for t in tasks]
            tmp = TASKS_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, TASKS_FILE)

    def load_tasks(self) -> List[PrintTask]:
        with self._rw_lock:
            if not TASKS_FILE.exists():
                return []
            try:
                with open(TASKS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return [PrintTask.from_dict(d) for d in data]
            except (json.JSONDecodeError, KeyError, ValueError):
                backup = TASKS_FILE.with_suffix(f".json.bak.{int(time.time())}")
                try:
                    os.replace(TASKS_FILE, backup)
                except OSError:
                    pass
                return []

    def save_config(self, config: AppConfig) -> None:
        with self._rw_lock:
            data = config.to_dict()
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)

    def load_config(self) -> AppConfig:
        with self._rw_lock:
            if not CONFIG_FILE.exists():
                return AppConfig()
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return AppConfig.from_dict(data)
            except (json.JSONDecodeError, KeyError, ValueError):
                return AppConfig()

    def log_export(self, export_info: dict) -> None:
        with self._rw_lock:
            logs = []
            if EXPORT_LOG_FILE.exists():
                try:
                    with open(EXPORT_LOG_FILE, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    logs = []
            logs.append({
                **export_info,
                "exported_at": time.time(),
            })
            tmp = EXPORT_LOG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, EXPORT_LOG_FILE)

    def load_export_logs(self) -> List[dict]:
        with self._rw_lock:
            if not EXPORT_LOG_FILE.exists():
                return []
            try:
                with open(EXPORT_LOG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                return []
