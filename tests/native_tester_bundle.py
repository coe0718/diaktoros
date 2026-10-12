"""Package selected tester evidence, never runtime downloads or image contents."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


def selected(name):
    if '/' not in name:
        return name == 'result.json' or name.endswith('.log')
    return bool(re.fullmatch(r'soak/(?:result\.json|events\.jsonl|failure\.txt|cleanup-control\.json|'
                            r'turn-\d+\.json|policy-\d+\.sb|sweep-\d+\.(?:stdout|stderr)|'
                            r'(?:attachments|processes)\.txt|receipts/dk-volume-[\w-]+\.json)', name))


def bundle(output):
    output = Path(output).resolve(strict=True)
    members, omitted, total = [], [], 0
    descriptor, temporary = tempfile.mkstemp(prefix='evidence-', suffix='.zip.tmp', dir=output)
    try:
        with os.fdopen(descriptor, 'wb') as target, zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            # Enumerate only these evidence locations. Do not recurse into a
            # downloaded Hermes checkout, caches, toolchains or mount roots.
            candidates = list(output.iterdir())
            soak = output/'soak'
            if soak.is_dir() and not soak.is_symlink():
                candidates.extend(soak.iterdir())
                receipts = soak/'receipts'
                if receipts.is_dir() and not receipts.is_symlink():
                    candidates.extend(receipts.iterdir())
            for path in sorted(candidates):
                name = path.relative_to(output).as_posix()
                if not selected(name):
                    continue
                if path.is_symlink() or not path.is_file():
                    omitted.append({'path':name, 'reason':'not a regular evidence file'})
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError('evidence file changed type')
                    keep = min(info.st_size, MAX_FILE)
                    if total + keep > MAX_TOTAL:
                        omitted.append({'path':name, 'reason':'bundle size limit'})
                        continue
                    source.seek(max(0, info.st_size - keep))
                    raw = source.read(keep)
                archive.writestr(name, raw)
                total += len(raw)
                members.append({'path':name, 'bytes':len(raw), 'original_bytes':info.st_size,
                                'truncated':info.st_size > keep, 'sha256':hashlib.sha256(raw).hexdigest()})
            archive.writestr('bundle-manifest.json', json.dumps({'files':members, 'omitted':omitted}, indent=2))
            archive.writestr('READ-ME.txt', 'Experimental Diaktoros Mac tester evidence.\n'
                            'Review logs for local paths before sharing. No runtime directories or disk images are included.\n'
                            'Large files keep their last 8 MiB; truncations and omissions are listed in bundle-manifest.json.\n')
        destination = output/'evidence.zip'
        Path(temporary).replace(destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)
