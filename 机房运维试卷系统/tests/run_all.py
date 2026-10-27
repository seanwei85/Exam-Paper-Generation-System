#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行全部阶段测试。用法：python tests/run_all.py [阶段过滤，如 p3]"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    pattern = "test_*.py"
    if len(sys.argv) > 1:
        pattern = "test_%s*.py" % sys.argv[1].lower()
    suite = unittest.TestLoader().discover(HERE, pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\n" + "=" * 60)
    print("用例 %d，失败 %d，错误 %d，跳过 %d"
          % (result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    print("=" * 60)
    sys.exit(0 if result.wasSuccessful() else 1)
