import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / 'scripts'))
from public_metadata import stage_metadata
helper = Path(DISTPATH) / 'runtime' / ('termana-runtime.exe' if sys.platform == 'win32' else 'termana-runtime')
assert helper.is_file(), 'Build runtime.spec first'
datas = [(str(root / 'LICENSE'), 'licenses'), (str(Path(DISTPATH).parent / 'source.zip'), 'source')]
datas += [(str(Path(DISTPATH).parent / 'build-info.json'), 'termana')]
# Copy the reviewed helper unchanged: treating it as a PyInstaller binary would
# rewrite Mach-O signatures and invalidate our exact component digest. Never
# discover a system Plink or include a different platform's executable.
import platform
sys.path.insert(0, str(root / 'src'))
from termana.rdp.component import bundle_directory, verified_component
# Include only the reviewed native component. Data, not binary processing:
# rewriting the executable would invalidate its pinned runtime digest.
rdp_helper = verified_component()
rdp_resources = bundle_directory(sys.platform, platform.machine()).as_posix()
if sys.platform == 'win32':
    datas += [(str(rdp_helper), rdp_resources)]
datas += [(str(Path(DISTPATH).parent / 'rdp-component.json'), rdp_resources)]
for notice in ('FreeRDP-LICENSE.txt', 'OpenSSL-LICENSE.txt', 'RDP-NOTICES.txt'):
    datas += [(str(root / 'packaging/licenses' / notice), 'licenses/rdp')]
legacy_area = root / 'src/termana/legacy_helpers'
legacy_native = legacy_area / f'{sys.platform}-{platform.machine().lower()}'
legacy_name = 'termana-ssh1-helper.exe' if sys.platform == 'win32' else 'termana-ssh1-helper'
if (legacy_native / legacy_name).is_file():
    if sys.platform != 'darwin':
        datas += [(str(legacy_native / legacy_name), 'termana/legacy_helpers/' + legacy_native.name)]
    datas += [(str(legacy_area / 'PuTTY-LICENCE.txt'), 'termana/legacy_helpers')]
for package in ('termana', 'asyncssh', 'cryptography', 'bcrypt', 'cffi', 'keyring', 'pyte', 'wcwidth',
                'PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'typing_extensions',
                'jaraco.classes', 'jaraco.context', 'jaraco.functools', 'more-itertools'):
    for source, target in copy_metadata(package):
        clean = stage_metadata(source, Path(DISTPATH).parent / 'public-metadata' / target,
            project_readme=(root / 'publishing/README.md').read_bytes() if package == 'termana' else None)
        datas += [(str(clean), target)]
a = Analysis([str(root / 'packaging/desktop_entry.py')], pathex=[str(root / 'src')],
             binaries=[(str(helper), '.')], datas=datas,
             hiddenimports=collect_submodules('keyring.backends'),
             excludes=['pytest', 'tkinter', 'PyQt5', 'PyQt6'], noarchive=False)
# Hooks may collect direct_url.json again; no editable-install source path should
# be distributed. Runtime discovery needs METADATA, not local installer receipts.
a.datas = [row for row in a.datas if Path(row[0]).name not in {'direct_url.json', 'RECORD'}]
pyz = PYZ(a.pure)
if sys.platform == 'win32':
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Termana',
              console=False, debug=False, strip=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Termana',
              console=False, debug=False, strip=False, upx=False)
    collection = COLLECT(exe, a.binaries, a.datas, name='Termana', strip=False, upx=False)
    if sys.platform == 'darwin':
        app = BUNDLE(collection, name='Termana.app', bundle_identifier='org.termana.desktop',
                     info_plist={'NSHighResolutionCapable': True,
                                 'CFBundleShortVersionString': '0.1.0'})
        # BUNDLE signs nested Mach-O files with --deep, including DATA that its
        # analysis reclassified. Add the already-signed reviewed helper only
        # afterwards, at Apple's conventional Helpers location. Seal just the
        # outer bundle; never rewrite nested helper signatures or relax its pin.
        import hashlib
        import shutil
        import subprocess
        from PyInstaller.utils.osx import sign_binary
        helper_directory = Path(app.name) / 'Contents' / 'Helpers'
        helper_directory.mkdir(exist_ok=False)
        from termana.terminal.legacy_ssh import verify_legacy_frontend
        legacy_helper = legacy_native / legacy_name
        verify_legacy_frontend(legacy_helper)
        for native_helper in (rdp_helper, legacy_helper):
            bundled_helper = helper_directory / native_helper.name
            with native_helper.open('rb') as source, bundled_helper.open('xb') as target:
                shutil.copyfileobj(source, target)
            bundled_helper.chmod(0o755)
        sign_binary(app.name, deep=False)
        for native_helper in (rdp_helper, legacy_helper):
            assert hashlib.sha256((helper_directory / native_helper.name).read_bytes()).digest() == hashlib.sha256(native_helper.read_bytes()).digest()
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app.name],
                       check=True, timeout=30)
