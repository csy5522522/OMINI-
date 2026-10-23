"""Package native Widgets and download engines; use external browsers/FFmpeg."""
from pathlib import Path
import shutil


EXCLUDED_QT_FRONTENDS = ['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtOpenGL',
                         'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                         'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel',
                         'icloud_notes_page', 'xuanwu_ai_page',
                         'omini_api', 'omini_api_page', 'omini_api_runner', 'omini_api_self_test',
                         'brightdata', 'brightdata_page', 'brightdata_self_test']
QT_LOCALES = ('_en.qm', '_en_US.qm', '_en_GB.qm', '_zh_CN.qm', '_zh_TW.qm')


def keep_resource(destination: str) -> bool:
    name = destination.replace('\\', '/')
    if 'webengine' in name.lower() or name.startswith('PySide6/resources/'):
        return False
    # These Addons plugins drag in Qt Quick/PDF despite a Widgets-only UI.
    # Windows native IME and bitmap image plugins remain available.
    if name in {'PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll',
                'PySide6/plugins/imageformats/qpdf.dll'}:
        return False
    if name.startswith(('PySide6/Qt6Quick', 'PySide6/Qt6Qml', 'PySide6/Qt6Pdf',
                        'PySide6/Qt6VirtualKeyboard', 'PySide6/Qt6OpenGL')):
        return False
    if name.startswith('PySide6/qml/') or name.startswith('PySide6/plugins/qmltooling/'):
        return False
    if name.startswith('PySide6/translations/') and name.endswith('.qm'):
        return name.endswith(QT_LOCALES)
    return True


def external_tool_file(name: str) -> bool:
    return name.lower().startswith(('ffmpeg', 'ffprobe', 'ffplay'))


def copy_release_tools(source: Path, destination: Path) -> None:
    """All build entry points share the same external-tool exclusion."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError('Build destination must differ from source tools')
    destination.mkdir(parents=True, exist_ok=True)
    for existing in destination.iterdir():
        if existing.is_file() and external_tool_file(existing.name):
            existing.unlink()
    for item in source.iterdir():
        if external_tool_file(item.name) or item.name == '__pycache__':
            continue
        if item.is_dir():
            shutil.copytree(item, destination / item.name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        else:
            shutil.copy2(item, destination / item.name)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--copy-tools', type=Path, required=True)
    args = parser.parse_args()
    copy_release_tools(Path(__file__).resolve().parent / 'tools', args.copy_tools)
