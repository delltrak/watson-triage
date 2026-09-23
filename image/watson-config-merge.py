"""Merge runtime/mcp-watson.yaml into a Hermes config.yaml (idempotent).

Used by the Dockerfile seed step and by cont-init 20-watson-mcp on every boot.
- mcp_servers.watson: replaced by the overlay block.
- skills.platform_disabled.<platform>: union (never re-enables anything).

Usage: python watson-config-merge.py CONFIG OVERLAY
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path


def merge(data, overlay):
    """Apply overlay onto data in place. Returns True when data changed."""
    changed = False
    watson = (overlay.get('mcp_servers') or {}).get('watson')
    if watson:
        servers = data.get('mcp_servers') or {}
        if servers.get('watson') != watson:
            servers['watson'] = watson
            changed = True
        data['mcp_servers'] = servers
    wanted = (overlay.get('skills') or {}).get('platform_disabled') or {}
    for platform, names in wanted.items():
        skills = data.get('skills') or {}
        disabled = skills.get('platform_disabled') or {}
        current = list(disabled.get(platform) or [])
        missing = [name for name in names or () if name not in current]
        if missing:
            disabled[platform] = current + missing
            skills['platform_disabled'] = disabled
            data['skills'] = skills
            changed = True
    return changed


def main(config_path, overlay_path):
    import yaml  # Hermes venv has it; merge() stays importable without it

    config_path, overlay_path = Path(config_path), Path(overlay_path)
    # The home is writable by the agent: never follow a planted symlink.
    if config_path.is_symlink() or not config_path.is_file():
        print(f'watson-config-merge: skip {config_path} (not a regular file)', flush=True)
        return
    data = yaml.safe_load(config_path.read_text()) or {}
    overlay = yaml.safe_load(overlay_path.read_text()) or {}
    if not merge(data, overlay):
        return
    st = config_path.stat()
    # mkstemp: O_CREAT|O_EXCL with a random name, so no pre-planted temp path.
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix='.config.yaml.watson-')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
            os.fchmod(fh.fileno(), st.st_mode & 0o777)
            if os.geteuid() == 0:
                os.fchown(fh.fileno(), st.st_uid, st.st_gid)
        os.replace(tmp, config_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    print(f'watson-config-merge: updated {config_path}', flush=True)


if __name__ == '__main__':
    main(*sys.argv[1:3])
