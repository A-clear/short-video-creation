"""benchmarks/ 配下から _harness / _data を import できるようにする。"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))
