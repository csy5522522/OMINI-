"""Explicit local FFmpeg selection, PATH isolation and engine arguments."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from local_ffmpeg import resolve_ffmpeg
from release_packaging import copy_release_tools, keep_resource


class LocalFfmpegTest(unittest.TestCase):
    def test_select_path_with_spaces_and_system_path(self):
        with tempfile.TemporaryDirectory(prefix='local ffmpeg ') as directory:
            binary = Path(directory)/'ffmpeg.exe'; binary.write_bytes(b'fixture')
            self.assertEqual(resolve_ffmpeg(str(binary)), binary.resolve())
            self.assertEqual(resolve_ffmpeg(directory), binary.resolve())
            with patch.dict(os.environ, {'PATH': directory}):
                self.assertEqual(resolve_ffmpeg(), binary.resolve())
            with self.assertRaisesRegex(ValueError, '路径不存在'):
                resolve_ffmpeg(str(binary.parent/'missing.exe'))

    def test_missing_local_binary_does_not_use_bundled_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'tools').mkdir(); (root/'tools/ffmpeg.exe').write_bytes(b'fixture')
            with patch.object(app, 'resource_path', side_effect=lambda name: root/name), \
                 patch.object(app, 'SETTINGS_FILE', root/'settings.json'), \
                 patch('app.resolve_ffmpeg', side_effect=ValueError('未找到本机 FFmpeg')) as resolve:
                with self.assertRaises(app.EngineError): app.find_engine('ffmpeg')
                self.assertIn(root/'tools', resolve.call_args.args[1])

    def test_selected_path_reaches_tools_without_explicit_ffmpeg_argument(self):
        with tempfile.TemporaryDirectory(prefix='local ffmpeg ') as directory:
            root = Path(directory); binary = root/'ffmpeg.exe'; binary.write_bytes(b'fixture')
            settings = root/'settings.json'
            settings.write_text(json.dumps({'ffmpeg_path': str(binary)}), encoding='utf-8')
            with patch.object(app, 'SETTINGS_FILE', settings):
                self.assertEqual(app.engine_environment()['PATH'].split(os.pathsep)[0], str(root.resolve()))

    def test_media_commands_pass_selected_ffmpeg_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            local = str(Path(directory)/'local ffmpeg/ffmpeg.exe')
            def engine(name): return local if name == 'ffmpeg' else name+'.exe'
            with patch.object(app, 'find_engine', side_effect=engine):
                for kind in ('video', 'audio', 'm3u8', 'spotify'):
                    task = app.DownloadTask(id='test', type=kind, source='https://example.com/media', output_dir=directory, title='test')
                    command = app.build_command(task)
                    flag = '--ffmpeg' if kind == 'spotify' else '--ffmpeg-location'
                    self.assertEqual(command[command.index(flag)+1], local)
                self.assertEqual(app.engine_environment(local)['PATH'].split(os.pathsep)[0], str(Path(local).parent))

    def test_release_excludes_ffmpeg_browser_and_keeps_download_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; destination = root/'release'; source.mkdir(); destination.mkdir()
            for name in ('ffmpeg.exe', 'ffmpeg-release.json', 'ffprobe.exe', 'ffplay.exe',
                         'syncthing.exe', 'syncthing-LICENSE.txt', 'yt-dlp.exe'):
                (source/name).write_bytes(b'fixture')
            (destination/'ffmpeg.exe').write_bytes(b'old stale copy')
            (destination/'syncthing.exe').write_bytes(b'retired engine')
            copy_release_tools(source, destination)
            self.assertEqual([p.name for p in destination.iterdir()], ['yt-dlp.exe'])
        for name in ('PySide6/Qt6WebEngineCore.dll', 'PySide6/QtWebEngineWidgets.pyd', 'PySide6/resources/icudtl.dat',
                     'PySide6/translations/qtwebengine_locales/en-US.pak', 'PySide6/Qt6Quick.dll',
                     'PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll', 'PySide6/plugins/imageformats/qpdf.dll'):
            self.assertFalse(keep_resource(name))
        self.assertTrue(keep_resource('PySide6/Qt6Widgets.dll'))
