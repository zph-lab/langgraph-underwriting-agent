"""Stable application paths shared by entry points and tool adapters."""
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WEB_DIR = PROJECT_DIR / 'web'
STATE_DIR = PROJECT_DIR / 'config' / 'langgraph'
