#!/usr/bin/env python
"""追溯仓 CLI 入口。

隔离模式（-I）不会把脚本目录加入 sys.path，因此这里显式按 __file__
注入项目根目录，保证在 -I 下仍可导入本仓库的 traceware 包，且不会从
当前工作目录或来源资料目录加载任何同名模块。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from traceware.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
