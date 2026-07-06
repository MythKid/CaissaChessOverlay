"""Run Caissa Chess Overlay from source (and the PyInstaller entry point).

    python run.py

Adds src/ to the import path and launches the app package.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from caissa.app import main   # noqa: E402  (path set up above)

if __name__ == "__main__":
    main()
