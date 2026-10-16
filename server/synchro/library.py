"""Explicitly granted media files, opaque stream IDs, and cached track metadata."""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading


@dataclass(frozen=True)
class MediaFile:
    id: str
    path: Path


class MediaLibrary:
    def __init__(self, ffprobe=None):
        self.ffprobe = shutil.which('ffprobe') if ffprobe is None else ffprobe
        self.files: dict[str, MediaFile] = {}
        self.metadata: dict[tuple, dict] = {}
        self.lock = threading.RLock()

    def add(self, path):
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('The selected media is not a regular file')
        media_id = hashlib.sha256(str(path).casefold().encode('utf-8')).hexdigest()[:32]
        item = MediaFile(media_id, path)
        with self.lock:
            self.files[media_id] = item
        return item

    def get(self, media_id):
        with self.lock:
            return self.files.get(media_id)

    def probe(self, item, timeout=20):
        stat = item.path.stat()
        key = (item.id, stat.st_size, stat.st_mtime_ns)
        with self.lock:
            if key in self.metadata:
                return self.metadata[key]
        result = {'audioTracks': [], 'subtitleTracks': [], 'durationMs': None,
                  'metadataAvailable': False}
        if self.ffprobe:
            process = subprocess.run([
                self.ffprobe, '-v', 'error', '-show_entries',
                'format=duration:stream=index,codec_type,codec_name:stream_tags=language,title',
                '-of', 'json', str(item.path)], capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode:
                raise ValueError('ffprobe could not inspect the selected media')
            data = json.loads(process.stdout)
            for kind, name in [('audio', 'audioTracks'), ('subtitle', 'subtitleTracks')]:
                for stream in data.get('streams', []):
                    if stream.get('codec_type') == kind:
                        tags = stream.get('tags', {})
                        result[name].append({'index': len(result[name]),
                                             'streamIndex': stream['index'],
                                             'language': tags.get('language', ''),
                                             'title': tags.get('title', ''),
                                             'codec': stream.get('codec_name', '')})
            duration = data.get('format', {}).get('duration')
            if duration:
                result['durationMs'] = round(float(duration) * 1000)
            result['metadataAvailable'] = True
        with self.lock:
            self.metadata[key] = result
        return result

    def preload(self, items, current_index):
        """Bound initial probing so track identity can survive episode reordering."""
        selected = items if len(items) <= 32 else items[max(0, current_index-1):current_index+3]
        def optional_probe(item):
            try:
                self.probe(item, timeout=3)
            except (OSError, ValueError, subprocess.SubprocessError):
                # An unreadable future episode must not prevent the current film.
                pass
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix='media-metadata') as pool:
            list(pool.map(optional_probe, selected))

    def describe(self, item, base_url, token, include_metadata=False):
        result = {'id': item.id, 'title': item.path.name,
                  'url': f'{base_url}/media/{token}/{item.id}'}
        if include_metadata:
            result.update(self.probe(item))
        else:
            try:
                stat = item.path.stat()
                with self.lock:
                    cached = self.metadata.get((item.id, stat.st_size, stat.st_mtime_ns))
                if cached is not None:
                    result.update(cached)
            except OSError:
                result['available'] = False
        return result
