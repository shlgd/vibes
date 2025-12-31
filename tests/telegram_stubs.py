import sys
import types


def install() -> None:
    if "telegram" in sys.modules:
        return

    telegram = types.ModuleType("telegram")
    telegram_constants = types.ModuleType("telegram.constants")
    telegram_error = types.ModuleType("telegram.error")
    telegram_ext = types.ModuleType("telegram.ext")

    class Update:  # pragma: no cover
        ALL_TYPES: object = object()

    class InlineKeyboardButton:  # pragma: no cover
        def __init__(self, text: str, callback_data: str | None = None) -> None:
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:  # pragma: no cover
        def __init__(self, inline_keyboard: object) -> None:
            self.inline_keyboard = inline_keyboard

    class ParseMode:  # pragma: no cover
        HTML = "HTML"

    class TelegramError(Exception):  # pragma: no cover
        pass

    class BadRequest(TelegramError):  # pragma: no cover
        pass

    class RetryAfter(TelegramError):  # pragma: no cover
        def __init__(self, retry_after: float = 0.0) -> None:
            super().__init__(f"Retry after {retry_after}")
            self.retry_after = retry_after

    class Application:  # pragma: no cover
        pass

    class ApplicationBuilder:  # pragma: no cover
        def token(self, _token: str) -> "ApplicationBuilder":
            return self

        def build(self) -> Application:
            return Application()

    class CallbackQueryHandler:  # pragma: no cover
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class CommandHandler:  # pragma: no cover
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class MessageHandler:  # pragma: no cover
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class ContextTypes:  # pragma: no cover
        DEFAULT_TYPE = object

    class _Filter:  # pragma: no cover
        def __init__(self, name: str) -> None:
            self.name = name

        def __and__(self, other: object) -> "_Filter":
            other_name = getattr(other, "name", repr(other))
            return _Filter(f"({self.name}&{other_name})")

        def __invert__(self) -> "_Filter":
            return _Filter(f"(~{self.name})")

    telegram.Update = Update
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    telegram_constants.ParseMode = ParseMode

    telegram_error.BadRequest = BadRequest
    telegram_error.RetryAfter = RetryAfter
    telegram_error.TelegramError = TelegramError

    telegram_ext.Application = Application
    telegram_ext.ApplicationBuilder = ApplicationBuilder
    telegram_ext.CallbackQueryHandler = CallbackQueryHandler
    telegram_ext.CommandHandler = CommandHandler
    telegram_ext.ContextTypes = ContextTypes
    telegram_ext.MessageHandler = MessageHandler
    telegram_ext.filters = types.SimpleNamespace(TEXT=_Filter("TEXT"), COMMAND=_Filter("COMMAND"))

    sys.modules["telegram"] = telegram
    sys.modules["telegram.constants"] = telegram_constants
    sys.modules["telegram.error"] = telegram_error
    sys.modules["telegram.ext"] = telegram_ext

