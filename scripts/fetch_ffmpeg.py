"""Fetch and verify the smaller essentials variant of our existing FFmpeg version."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

import requests

VERSION = '8.1.2'
ARCHIVE = f'ffmpeg-{VERSION}-essentials_build.7z'
URL = f'https://www.gyan.dev/ffmpeg/builds/packages/{ARCHIVE}'
SHA256 = 'e25b682664025d49034c981afb4bae36238a40f29a3cc1c713ad9a8b5b3528f6'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(destination=None, archive_path=None):
    root = Path(__file__).resolve().parents[1]
    destination = Path(destination or root / 'tools')
    archive = Path(archive_path) if archive_path else root / '.cache/build-assets' / ARCHIVE
    metadata = destination / 'ffmpeg-release.json'
    executable = destination / 'ffmpeg.exe'
    if executable.is_file() and metadata.is_file():
        try:
            import json
            saved = json.loads(metadata.read_text(encoding='utf-8'))
            if saved.get('archive_sha256') == SHA256 and saved.get('executable_sha256') == digest(executable):
                print(f'Verified cached FFmpeg essentials {VERSION}: {executable}')
                return executable
        except (ValueError, OSError):
            pass
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or digest(archive) != SHA256:
        temporary = archive.with_suffix('.download')
        try:
            with requests.get(URL, timeout=(15, 20), stream=True) as response:
                response.raise_for_status()
                with temporary.open('wb') as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            if digest(temporary) != SHA256:
                raise RuntimeError('FFmpeg archive SHA-256 mismatch')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    tar = shutil.which('tar.exe') or shutil.which('tar')
    if not tar:
        raise RuntimeError('Windows tar.exe is required to extract the verified 7z archive')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    listing = subprocess.run([tar, '-tf', str(archive)], capture_output=True, check=True,
                             encoding='utf-8', timeout=30, creationflags=flags).stdout.splitlines()
    destination.mkdir(parents=True, exist_ok=True)
    for suffix, output in (('/bin/ffmpeg.exe', executable), ('/LICENSE', destination / 'ffmpeg-LICENSE.txt'),
                           ('/README.txt', destination / 'ffmpeg-README.txt')):
        member = next((name for name in listing if name.endswith(suffix)), None)
        if member is None:
            raise RuntimeError(f'FFmpeg release member missing: {suffix}')
        temporary = output.with_suffix(output.suffix + '.download')
        try:
            with temporary.open('wb') as stream:
                subprocess.run([tar, '-xOf', str(archive), member], stdout=stream, stderr=subprocess.PIPE,
                               check=True, timeout=60, creationflags=flags)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    import json
    metadata.write_text(json.dumps({'version': VERSION, 'variant': 'essentials', 'url': URL,
                        'archive_sha256': SHA256, 'executable_sha256': digest(executable),
                        'source': 'https://github.com/FFmpeg/FFmpeg/tree/n' + VERSION,
                        'license': 'GPL-3.0-or-later'}, indent=2), encoding='utf-8')
    print(f'Verified FFmpeg essentials {VERSION}: {executable} ({executable.stat().st_size / 1024**2:.1f} MiB)')
    return executable


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path, nargs='?')
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    fetch(args.destination, args.archive)
