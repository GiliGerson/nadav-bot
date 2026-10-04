"""Local development entry point: `python -m app.polling`.

Uses long polling, so no public URL or webhook is needed.
"""
from telegram import Update

from app.bot import build_application
from app.config import get_settings, setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    application = build_application(settings, webhook_mode=False)

    # run_polling deletes any webhook left by a previous deployment during its bootstrap.
    # Don't call delete_webhook() via asyncio.run() first: that binds the bot's HTTP
    # client to a loop that is then closed, and polling fails with "Event loop is closed".
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
