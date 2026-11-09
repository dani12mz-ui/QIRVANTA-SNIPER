"""Stable fingerprint of executable sources, fixtures and delivered Pine bytes."""
from pathlib import Path
import hashlib
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def source_fingerprint():
    paths=[]
    for directory in ('src','tools','tests'):
        paths += [p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    paths += [ROOT/name for name in ('QIRVANTA_SNIPER_PRO.pine','QIRVANTA_SNIPER_VALIDATION.pine')]
    records={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    digest=hashlib.sha256(''.join(f'{name}\0{value}\n' for name,value in records.items()).encode()).hexdigest()
    return digest,records

def git_commit():
    try:
        return subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,stderr=subprocess.DEVNULL,text=True).strip()
    except (OSError,subprocess.CalledProcessError):
        return None
