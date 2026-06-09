"""Utility helpers for the LinkedIn bot."""
import logging
import random
import time
import yaml
from pathlib import Path


def load_config(path: str = None) -> dict:
    config_path = Path(path) if path else Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def human_delay(min_s: float = 0.5, max_s: float = 2.0):
    """Sleep for a random human-like duration."""
    time.sleep(random.uniform(min_s, max_s))


def safe_text(el) -> str:
    """Extract stripped text from a Playwright element, empty string on failure."""
    try:
        return el.inner_text().strip()
    except Exception:
        return ""


# Map LinkedIn experience level filter values
EXPERIENCE_MAP = {
    "internship":  "1",
    "entry":       "2",
    "associate":   "3",
    "mid_senior":  "4",
    "director":    "5",
    "executive":   "6",
}

JOB_TYPE_MAP = {
    "full_time":  "F",
    "part_time":  "P",
    "contract":   "C",
    "temporary":  "T",
    "internship": "I",
    "volunteer":  "V",
    "other":      "O",
}

DATE_POSTED_MAP = {
    "day":   "r86400",
    "week":  "r604800",
    "month": "r2592000",
}
