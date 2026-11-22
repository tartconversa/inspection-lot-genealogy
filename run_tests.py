#!/usr/bin/env python
"""测试入口：在隔离模式下运行全部 unittest 用例。

    python -I run_tests.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"),
        top_level_dir=os.path.dirname(os.path.abspath(__file__)),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
