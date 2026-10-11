"""System prompts for the workflow, stored as plain-text files next to this module.

Each prompt lives in its own ``<name>.txt`` file so it can be read and edited without touching
Python code. Placeholders such as ``{schema}`` are filled in with ``str.format()`` by the caller,
so any literal brace in a prompt file must be doubled (``{{`` / ``}}``).
"""
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@cache
def load_prompt(name: str) -> str:
    """Return the text of ``<name>.txt`` in this folder, without leading or trailing whitespace."""
    return (PROMPTS_DIR / f'{name}.txt').read_text(encoding='utf-8').strip()
