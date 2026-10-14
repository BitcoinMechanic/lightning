#!/usr/bin/python3 -I
"""Packaged finite local grant actions; the request travels on private stdin."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import package_verify
    package_verify.verify(Path(__file__).parent, 'node')
    import coordinator_admin_cli
    coordinator_admin_cli.main()
except Exception:
    print('{"result":"REFUSED","records_preserved":true}')
    raise SystemExit(1) from None
