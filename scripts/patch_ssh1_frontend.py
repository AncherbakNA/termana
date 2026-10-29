"""Guarded PuTTY 0.85 frontend-only IPC hooks; SSH/crypto sources are untouched."""

from pathlib import Path


def patch_frontend(root: Path) -> None:
    bridge = Path(__file__).with_name("ssh1_frontend.h").read_text()
    storage = root / "stubs/no-storage.c"
    text = storage.read_text()
    old = 'const char *keytype, const char *key) { return 0; }'
    if text.count(old) != 1:
        raise ValueError("The reviewed no-storage trust hook changed")
    # Upstream's test stub returns MATCH (0), not MISSING (1). A production
    # no-storage frontend MUST ask our broker for every connection's full key.
    storage.write_text(text.replace(old, 'const char *keytype, const char *key) { return 1; }'))
    doc = root / "doc/CMakeLists.txt"
    text = doc.read_text()
    if text.count("include(FindGit)") != 1:
        raise ValueError("The reviewed documentation build hook changed")
    doc.write_text(text.replace("include(FindGit)", "# Termana: no Git discovery"))
    for platform in ("unix", "windows"):
        path = root / platform / "plink.c"
        text = path.read_text()
        hooks = {
            "static const SeatVtable plink_seat_vt = {":
                '#include "termana-ssh1-frontend.h"\n\nstatic const SeatVtable plink_seat_vt = {',
            ".get_userpass_input = plink_get_userpass_input,":
                ".get_userpass_input = tm_userpass,",
            ".notify_session_started = nullseat_notify_session_started,":
                ".notify_session_started = tm_ready,",
            ".confirm_ssh_host_key = console_confirm_ssh_host_key,":
                ".confirm_ssh_host_key = tm_host_key,",
            ".confirm_weak_crypto_primitive = console_confirm_weak_crypto_primitive,":
                ".confirm_weak_crypto_primitive = tm_weak,",
        }
        call = "        error = backend_init("
        hooks[call] = "        tm_init();\n" + call
        if platform == "windows":
            hooks[".echoedit_update = plink_echoedit_update,"] = ".echoedit_update = tm_echoedit,"
        for old, new in hooks.items():
            if text.count(old) != 1:
                raise ValueError("The reviewed Plink frontend hook changed")
            text = text.replace(old, new)
        path.write_text(text)
        (root / platform / "termana-ssh1-frontend.h").write_text(bridge)
