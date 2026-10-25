"""Cache public frontend files with content versions, independent of account data."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading


@dataclass(frozen=True)
class PublicAsset:
    data: bytes
    content_type: str
    etag: str
    cache_control: str

    def matches(self, supplied: str) -> bool:
        # If-None-Match uses weak comparison for GET, including compressed variants.
        return any(tag.strip().removeprefix('W/') in {'*', self.etag.removeprefix('W/')}
                   for tag in supplied.split(','))


class StaticAssets:
    def __init__(self, files: dict[str, tuple[Path, str]]):
        self.files = files
        self._cache = {}
        self._lock = threading.Lock()

    def _read(self, path):
        file, content_type = self.files[path]
        stat = file.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
        with self._lock:
            cached = self._cache.get(file)
            if cached is None or cached[0] != stamp:
                data = file.read_bytes()
                cached = (stamp, data, hashlib.sha256(data).hexdigest())
                self._cache[file] = cached
        return cached[1], cached[2], content_type

    def get(self, path, version=''):
        data, digest, content_type = self._read(path)
        html = content_type.startswith('text/html')
        if html:
            versions = {url: self._read(url)[1] for url, (_, kind) in self.files.items()
                        if not kind.startswith('text/html')}
            text = data.decode('utf-8')
            for url, identifier in versions.items():
                text = text.replace(f'"{url}"', f'"{url}?v={identifier}"')
            text = text.replace('<script id="asset-manifest" type="application/json">{}</script>',
                '<script id="asset-manifest" type="application/json">'
                + json.dumps(versions, separators=(',', ':')) + '</script>')
            data = text.encode('utf-8')
            digest = hashlib.sha256(data).hexdigest()
        policy = ('public, max-age=31536000, immutable' if not html and version == digest
                  else 'public, max-age=0, must-revalidate')
        return PublicAsset(data, content_type, f'W/"{digest}"', policy)
