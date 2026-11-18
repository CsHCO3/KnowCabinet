"""统一日志模块"""

import logging
import sys
from pathlib import Path


def setup_logger(
    verbose: int = 1,
    name: str = "infosystem",
    log_file: str | None = None,
) -> logging.Logger:
    level = {0: logging.ERROR, 1: logging.INFO, 2: logging.DEBUG}.get(verbose, logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    return logger
