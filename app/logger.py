from loguru import logger
import sys

def setup_logger():
    """
    Configures and returns a loguru logger with colored,
    structured, production-grade output.
    """
    logger.remove()

    # Set global default for extra["name"] — overridden per-module via
    # logger.bind(name="..."). Without this, bare logger.info() calls
    # (without a bound name) raise KeyError: 'name' in the format handler.
    logger.configure(extra={"name": "app"})

    logger.add(
        sys.stdout,
        level="INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level}</level> | "
            "<cyan>{extra[name]}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    return logger
