from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis([str(root / 'packaging/runtime_entry.py')], pathex=[str(root / 'src')],
             excludes=['PySide6', 'pytest', 'asyncssh', 'keyring'], noarchive=False)
pyz = PYZ(a.pure)
# A console bootloader sharing the desktop's exact Python libraries. No per-tab
# onefile extraction: the outer Windows exe extracts once, macOS is an app bundle.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='termana-runtime',
          contents_directory='.', console=True, debug=False, strip=False, upx=False)
collection = COLLECT(exe, a.binaries, a.datas, name='runtime', strip=False, upx=False)
