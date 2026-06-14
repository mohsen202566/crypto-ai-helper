# -*- coding: utf-8 -*-
"""Local validation helper: compile all Python files and check important imports."""
import compileall
import importlib
import os
import sys

ok = compileall.compile_dir('.', quiet=1, maxlevels=1)
if not ok:
    print('COMPILE_FAILED')
    sys.exit(1)

modules = [
    'config','data_store','ai_memory','coin_learning','coin_risk','coin_rotation',
    'slot_manager','ghost_signals','sr_learning','paper_trader','analysis','scanner',
    'signal_tracker','bot'
]
failed = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append((name, repr(exc)))

if failed:
    print('IMPORT_WARNINGS_OR_FAILURES:')
    for name, exc in failed:
        print(f'- {name}: {exc}')
    # Some imports can fail when optional runtime deps/env/network libraries are missing.
    sys.exit(2)

print('OK: compile and imports passed')
