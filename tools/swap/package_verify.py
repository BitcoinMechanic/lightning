"""Integrity preflight for immutable packaged code; never reads a node volume."""
import hashlib
import json
from pathlib import Path
import re


def verify(directory, role):
    root = Path(directory)
    raw = (root/'runtime-manifest.json').read_bytes()
    if len(raw) > 65536: raise ValueError('runtime_manifest_invalid')
    m = json.loads(raw)
    if (set(m) != {'schema','fixture','role','generation','regtest_only','files'}
            or m['schema'] != 1 or m['fixture'] != '0098' or m['role'] != role
            or m['generation'] not in ('a','b') or m['regtest_only'] is not True
            or not isinstance(m['files'],dict) or not m['files']):
        raise ValueError('runtime_manifest_invalid')
    present = {p.name for p in root.iterdir()}
    if present != set(m['files']) | {'runtime-manifest.json'}:
        raise ValueError('runtime_files_changed')
    for name, expected in m['files'].items():
        p = root/name
        if (re.fullmatch(r'[a-z][a-z0-9_]*\.py',name) is None
                or name.startswith(('test_','fixture_')) or p.is_symlink() or not p.is_file()
                or hashlib.sha256(p.read_bytes()).hexdigest() != expected):
            raise ValueError('runtime_files_changed')
    return m
