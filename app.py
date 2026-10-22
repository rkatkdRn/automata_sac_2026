"""Vercel / Uvicorn entry point (uvicorn app:app)."""
from server.app import create_app
app = create_app()
