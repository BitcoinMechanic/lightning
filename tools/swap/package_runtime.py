#!/usr/bin/env python3
"""Build-time allowlisted import closure; no working-tree or runtime downloads."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOTS = {'node': ('package_gate','package_customer','package_admin'),
         'controller': ('package_service',)}


def closure(roots, sources):
    selected = {}; pending = list(roots)
    while pending:
        name = pending.pop()
        if name in selected: continue
        if name.startswith(('test_', 'fixture_')): raise ValueError('fixture_import_refused')
        choices = [p/(name+'.py') for p in sources if (p/(name+'.py')).is_file()]
        if not choices:
            if name in sys.stdlib_module_names: continue
            raise ValueError('runtime_dependency_missing: '+name)
        if len(choices) != 1: raise ValueError('runtime_dependency_ambiguous: '+name)
        path = choices[0]
        if path.is_symlink(): raise ValueError('runtime_symlink_refused')
        tree = ast.parse(path.read_text()); selected[name] = path
        for n in ast.walk(tree):
            if isinstance(n,ast.Import): pending += [a.name.split('.')[0] for a in n.names]
            if isinstance(n,ast.ImportFrom):
                if n.level or not n.module: raise ValueError('runtime_relative_import_refused')
                pending.append(n.module.split('.')[0])
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id == '__import__':
                raise ValueError('runtime_dynamic_import_refused')
    return selected


def stage(swap, controller, destination, role, generation):
    if role not in ROOTS or generation not in ('a','b'): raise ValueError('runtime_role_invalid')
    destination = Path(destination)
    destination.mkdir()  # Never replace an existing runtime.
    files = closure(ROOTS[role], [Path(swap),Path(controller)])
    manifest = dict(schema=1,fixture='0098',role=role,generation=generation,regtest_only=True,files={})
    for name, source in sorted(files.items()):
        target=destination/(name+'.py'); shutil.copyfile(source,target); target.chmod(0o755 if name in ROOTS[role] else 0o644)
        manifest['files'][target.name]=hashlib.sha256(target.read_bytes()).hexdigest()
    (destination/'runtime-manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    (destination/'runtime-manifest.json').chmod(0o644);destination.chmod(0o755)
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('swap','controller','destination'):p.add_argument(key,type=Path)
    p.add_argument('role',choices=ROOTS);p.add_argument('generation',choices=('a','b'))
    a=p.parse_args();stage(a.swap,a.controller,a.destination,a.role,a.generation)
