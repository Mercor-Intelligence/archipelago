"""Entry point for the app's ``populate`` task: prepare the keyword index, never fail.

Studio runs ``populate`` after the world and task files are in place and before
the agent starts, and a failing populate task can mark the whole app unavailable.
So this always exits 0: any problem is logged and the run carries on.
"""

import sys

from loguru import logger


def main() -> int:
    try:
        from keyword_search.config import load_config
        from keyword_search.runtime import build_for_config

        config = load_config()
        if not config.enabled:
            logger.info(
                "keyword index build skipped: not enabled in KEYWORD_SEARCH_CONFIG"
            )
            return 0
        build_for_config(config)
    except BaseException as exc:  # populate must never fail, whatever went wrong
        logger.exception(f"keyword index populate step failed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
