"""Vercel / Uvicorn entry point (uvicorn app:app)."""
import sys
from pathlib import Path

# File-based loaders do not always add this file's directory to sys.path.
# Resolve our own package independently of the runtime's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.app import create_app

app = create_app()
