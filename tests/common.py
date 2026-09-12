from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings import Settings

DUMMY_TOKEN = "123456:" + "A" * 32

def settings_at(directory: Path, **overrides) -> Settings:
    from dataclasses import replace
    home = directory / "home"
    work = directory / "work"
    state = directory / "state"
    for path in (home, work, state):
        path.mkdir(mode=0o700, exist_ok=True)
    return replace(Settings(
        token=DUMMY_TOKEN, allowed=frozenset({12345, 67890}),
        agy=directory / "fake-agy", home=home, workspace=work,
        state_dir=state,
    ), **overrides)

def config_values():
    return {"TELEGRAM_BOT_TOKEN": DUMMY_TOKEN, "ALLOWED_USER_IDS": "12345"}
