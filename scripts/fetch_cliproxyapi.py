"""Bundle the same pinned, verified CLIProxyAPI engine used by the desktop UI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ai_assistants import install_engine


if __name__ == "__main__":
    print(install_engine(Path(__file__).resolve().parents[1] / "tools", progress=print))
