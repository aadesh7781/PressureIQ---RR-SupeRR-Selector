from __future__ import annotations
import logging
from pathlib import Path

def configure_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
    """Configure consistent console and file logging."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(directory / "pressureiq.log", encoding="utf-8")], force=True)
