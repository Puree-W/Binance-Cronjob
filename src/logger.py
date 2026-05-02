from pathlib import Path
from loguru import logger
import sys

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

logger.remove()
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{extra[bot]:<8}</cyan> | {message}",
)
logger.add(
    _LOG_DIR / "trades.log",
    level="DEBUG",
    rotation="10 MB",
    retention="30 days",
    enqueue=True,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {extra[bot]:<8} | {message}",
)

logger = logger.bind(bot="-")


def get_logger(bot_name: str):
    return logger.bind(bot=bot_name)
