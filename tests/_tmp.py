"""测试专用临时目录。

两条环境限制（都在本机实测过）：
  ① 不用系统临时目录 —— DSH 沙箱只放行**工作区内**的目录创建；
  ② 不用 ``tempfile.mkdtemp`` —— 它建出来的目录在沙箱下会拒绝后续写入
     （``PermissionError: 13``），改成 ``Path.mkdir`` + uuid 名字就正常。
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BASE = Path(__file__).resolve().parent.parent / ".test-tmp"


@contextmanager
def temp_dir() -> Iterator[Path]:
    BASE.mkdir(parents=True, exist_ok=True)
    path = BASE / f"case-{uuid.uuid4().hex[:10]}"
    path.mkdir(exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
