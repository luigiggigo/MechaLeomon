"""Collect wheel GEOS DLLs with their real casing on a case-sensitive WSL share.

The stock hook looks for Shapely.libs; current wheels ship shapely.libs.
Preserve the layout expected by shapely's delvewheel bootstrap.
"""
from pathlib import Path

from PyInstaller import compat
from PyInstaller.utils.hooks import collect_dynamic_libs, get_package_paths

hiddenimports = ['shapely.prepared', 'shapely._geos']
binaries = collect_dynamic_libs('shapely')
datas = []
if compat.is_win:
    base, _ = get_package_paths('shapely')
    folders = [path for path in Path(base).iterdir()
               if path.is_dir() and path.name.lower() == 'shapely.libs']
    for folder in folders:
        datas.extend((str(path), folder.name) for path in folder.iterdir() if path.is_file())
    if not any(Path(source).name.lower().startswith('geos_c') for source, _ in datas):
        raise RuntimeError('Shapely wheel GEOS DLLs missing; reinstall the Windows wheel.')
