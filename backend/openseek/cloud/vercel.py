"""Vercel entry point (`[tool.vercel] entrypoint` in pyproject.toml). Vercel serves every path through this app."""
from .app import create_cloud_app

app = create_cloud_app()
