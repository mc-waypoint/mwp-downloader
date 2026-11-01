import sys
from pathlib import Path


def get_app_directory() -> Path:
    if "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent
