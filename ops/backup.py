#!/usr/bin/python3
"""Verified SQLite snapshots with the model photos referenced by each snapshot."""
import datetime
import os
import pathlib
import re
import sqlite3
import tarfile

SOURCE = pathlib.Path(os.environ.get('JAIL_BACKUP_SOURCE', '/var/lib/jail/jail.db'))
BACKUPS = pathlib.Path(os.environ.get('JAIL_BACKUP_DIRECTORY', '/srv/backups/jail'))
BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
target = BACKUPS / f'jail-{stamp}.sqlite3'
temporary = BACKUPS / f'.jail-{stamp}.tmp'
photos = BACKUPS / f'jail-{stamp}.photos.tar.gz'
photo_tmp = BACKUPS / f'.jail-{stamp}.photos.tmp'
try:
    with sqlite3.connect(f'file:{SOURCE}?mode=ro', uri=True) as src:
        with sqlite3.connect(temporary) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
    with sqlite3.connect(f'file:{temporary}?mode=ro&immutable=1', uri=True) as check:
        result = check.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError(f'Backup integrity check failed: {result}')
        columns = {row[1] for row in check.execute('PRAGMA table_info(models)')}
        names = [row[0] for row in check.execute("SELECT DISTINCT photo_filename FROM models WHERE photo_filename IS NOT NULL AND photo_filename<>''")] if 'photo_filename' in columns else []
    if names:
        with tarfile.open(photo_tmp, 'w:gz') as archive:
            for name in names:
                if not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|webp)', name):
                    raise RuntimeError('Invalid model photo reference in backup')
                source = SOURCE.parent / 'model-photos' / name
                if not source.is_file() or source.is_symlink():
                    raise RuntimeError(f'Missing model photo: {name}')
                archive.add(source, arcname=f'model-photos/{name}', recursive=False)
        os.chmod(photo_tmp, 0o600)
        photo_tmp.replace(photos)
    os.chmod(temporary, 0o600)
    temporary.replace(target)
except Exception:
    temporary.unlink(missing_ok=True)
    photo_tmp.unlink(missing_ok=True)
    raise
for old in sorted(BACKUPS.glob('jail-*.sqlite3'), reverse=True)[14:]:
    old.with_suffix('.photos.tar.gz').unlink(missing_ok=True)
    old.unlink()
print(target)
