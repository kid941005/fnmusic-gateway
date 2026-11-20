"""网关运行期配置（当前用于飞牛音乐地址/账号），持久化到 data/config.json。

- 网页「网关设置」读写这里的值；环境变量只作为首次启动的默认兜底。
- 文件权限 0600，密码明文保存（单机部署），请注意 data/ 目录权限。
"""

import json
import os
import threading

FIELDS = {
    "fnos": ("base", "username", "password", "device_id", "insecure_tls"),
    # 兜底在线音源，逗号分隔的 type@url 列表
    "fallback": ("urls",),
}


class ConfigStore:
    def __init__(self, path, logger=None):
        self.path = path
        self.log = logger
        self._lock = threading.Lock()
        self._data = {}
        self.reload()

    def reload(self):
        data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass
        except Exception as exc:
            if self.log:
                self.log.warning("读取配置失败 %s：%s", self.path, exc)
        with self._lock:
            self._data = data

    def section(self, name):
        with self._lock:
            cur = self._data.get(name)
            return dict(cur) if isinstance(cur, dict) else {}

    def update(self, name, patch):
        """按字段白名单写入；空字符串表示删除该键，None 表示忽略。"""
        allowed = FIELDS.get(name, ())
        with self._lock:
            cur = dict(self._data.get(name) or {})
            for key, value in (patch or {}).items():
                if key not in allowed or value is None:
                    continue
                value = str(value).strip()
                if value:
                    cur[key] = value
                else:
                    cur.pop(key, None)
            if cur:
                self._data[name] = cur
            else:
                self._data.pop(name, None)
            self._write()
            return dict(cur)

    def _write(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
