"""统一配置与小工具：所有路径相对env文件，不打印连接凭据。"""

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


class AppError(Exception):
    def __init__(self, code: str, message: str, *, denied: bool = False):
        super().__init__(message)
        self.code, self.denied = code, denied


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AppError("CONFIG", f"无法读取JSON：{path}") from exc


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    root: Path
    values: dict[str, str] = field(repr=False)
    env_file: Path

    @classmethod
    def load(cls, env_file=".env"):
        path = Path(env_file).resolve()
        if not path.is_file():
            raise AppError("CONFIG", f"请先复制.env.example并填写：{path}")
        values = {k: v for k, v in dotenv_values(path).items() if v is not None}
        values.update(os.environ)
        settings = cls(path.parent, values, path)
        if settings.integer("MAX_ROWS", 100) > 10000:
            raise AppError("CONFIG", "MAX_ROWS不能超过10000")
        return settings

    def get(self, key, default=""):
        return self.values.get(key, default)

    def required(self, key):
        value = self.get(key)
        if not value:
            raise AppError("CONFIG", f"请配置{key}")
        return value

    def integer(self, key, default, *, minimum=1):
        try:
            value = int(self.get(key, str(default)))
            if value < minimum:
                raise ValueError
            return value
        except ValueError as exc:
            raise AppError("CONFIG", f"{key}必须为不小于{minimum}的整数") from exc

    def boolean(self, key, default=True):
        value = self.get(key, str(default)).lower()
        if value not in {"true", "false"}:
            raise AppError("CONFIG", f"{key}必须为true或false")
        return value == "true"

    def path(self, key, default):
        value = Path(self.get(key, default))
        return (value if value.is_absolute() else self.root / value).resolve()
