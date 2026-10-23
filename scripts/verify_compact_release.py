"""Exercise the browser-free EXE, native TXT, engines and local FFmpeg downloads."""
from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import shutil
import sys


def run(command, *, env=None, timeout=60):
    result = subprocess.run([str(item) for item in command], env=env, capture_output=True,
                            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(f'{Path(command[0]).name} exited with {result.returncode}: '
                           + (result.stdout + result.stderr).decode('utf-8', 'replace')[-2200:])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('release', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--skip-ui', action='store_true', help='Only check media/engines after a separate UI run')
    parser.add_argument('--ffmpeg', type=Path, help='Existing local ffmpeg.exe; never copied into the release')
    args = parser.parse_args()
    release = args.release.resolve()
    executable = release / 'Omini工具箱.exe'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from local_ffmpeg import resolve_ffmpeg
    ffmpeg = resolve_ffmpeg(str(args.ffmpeg or ''), [release / 'tools', Path(__file__).resolve().parents[1] / 'tools'])
    report = {'ok': False, 'release': str(release)}
    with tempfile.TemporaryDirectory(prefix='omini compact verification ') as temporary:
        root = Path(temporary)
        env = os.environ.copy()
        env['APPDATA'] = str(root / 'appdata')
        env.pop('QT_QPA_PLATFORM', None)
        env.pop('OMINI_AI_CAPTURE_DIR', None)
        env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1,::1'
        env['PATH'] = str(ffmpeg.parent) + os.pathsep + env.get('PATH', '')
        assert not (release / '_internal/PySide6/qml').exists()
        assert not (release / '_internal/PySide6/Qt6Quick.dll').exists()
        assert not (release / '_internal/PySide6/Qt6Qml.dll').exists()
        forbidden = [p for p in release.rglob('*') if 'webengine' in p.name.lower()
                     or p.name.lower().startswith(('ffmpeg', 'ffprobe', 'ffplay'))]
        assert not forbidden, forbidden
        from PyInstaller.archive.readers import CArchiveReader
        modules = CArchiveReader(str(executable)).open_embedded_archive('PYZ.pyz').toc
        assert not any(name == 'omini_api' or name.startswith(('omini_api.', 'omini_api_', 'brightdata')) for name in modules)
        checks = {'no_embedded_browser_or_ffmpeg': True, 'retired_modules_removed': True}
        for name in (() if args.skip_ui else ('ai-assistants', 'timetable', 'notes')):
            target = root / f'{name}.json'
            env['OMINI_SELF_TEST_LOG'] = str(target)
            run([executable, '--self-test-' + name], env=env)
            if name in ('notes', 'timetable'):
                target = root / 'appdata/Omini工具箱' / (name + '-smoke.json')
            data = json.loads(target.read_text(encoding='utf-8'))
            assert data.get('ok', True), data
            if name == 'notes':
                assert data['encrypted_notes_saved'] and data['no_embedded_browser'], data
                args.report.parent.mkdir(parents=True, exist_ok=True)
                for theme in ('dark', 'light'):
                    shutil.copy2(root / 'appdata/Omini工具箱' / f'notes-{theme}.png', args.report.parent / f'txt-notes-{theme}.png')
            if name == 'timetable':
                assert data['external_login'] and data['external_class_query'], data
            checks[name] = True
            print('PASS', name, flush=True)
        media = root / 'media'
        media.mkdir()
        video = media / 'sample.mp4'
        run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1.2',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', video])
        for codec, extension in (('libmp3lame', 'mp3'), ('flac', 'flac'), ('aac', 'm4a'),
                                 ('libopus', 'opus'), ('libvorbis', 'ogg')):
            audio = media / ('sample.' + extension)
            run([ffmpeg, '-v', 'error', '-y', '-i', video, '-vn', '-c:a', codec, audio])
            assert audio.stat().st_size > 0
        run([ffmpeg, '-v', 'error', '-y', '-i', video, '-c', 'copy', '-f', 'hls', '-hls_time', '0.6',
             '-hls_list_size', '0', '-hls_segment_filename', media / 'part%03d.ts', media / 'playlist.m3u8'])
        for name in ('gallery-dl', 'spotdl', 'yt-dlp', 'aria2c'):
            run([release / 'tools' / (name + '.exe'), '--version'], env=env, timeout=30)
        checks['local_ffmpeg_codecs_and_bundled_engines'] = True
        print('PASS media codecs and bundled engines', flush=True)

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(media)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = root / 'download.json'
            run([executable, '--self-test-download', '--source', f'http://127.0.0.1:{server.server_port}/playlist.m3u8',
                 '--output', root / 'downloaded', '--report', target, '--ui-submit', '--timeout', '35'], env=env, timeout=45)
            data = json.loads(target.read_text(encoding='utf-8'))
            assert data['success'] and data['submitted_by_button'], data
            checks['actual_ui_hls_download'] = True
            print('PASS actual UI HLS download', flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        report.update(ok=True, checks=checks, local_ffmpeg=str(ffmpeg),
                      unpacked_bytes=sum(p.stat().st_size for p in release.rglob('*') if p.is_file()))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
