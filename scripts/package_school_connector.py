"""Build the browser connector download from its reviewed sources."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
with ZipFile(root / 'web/school-connector.zip', 'w', ZIP_DEFLATED) as archive:
    for path in sorted((root / 'extras/school-connector').iterdir()):
        if path.is_file():
            archive.write(path, path.name)
