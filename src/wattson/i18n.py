"""Interface language and message catalogs.

Every user-visible string is written in English in the source, so English
is the source language and needs no catalog. Any other language is a JSON
file in the ``wattson.locales`` package, named after its language code
(``de.json``, ``pt_br.json``)::

    {
      "language": "de",
      "name": "Deutsch",
      "messages": {
        "Fans": "Lüfter",
        "Charge threshold {value} %": "Ladeschwelle {value} %"
      }
    }

A catalog key is the exact English string from the source; placeholders in
curly braces must survive translation. Unknown keys fall back to English,
so a partially translated catalog is still usable. The optional ``name``
field is what the language switcher of the GUI shows.

The language is taken from ``--lang``, then ``WATTSON_LANG``, then the
usual locale variables. Selection happens once at import time and can be
redone at any moment with :func:`set_language`, so nothing user-visible may
be translated at module import time — call :func:`translate` when the
string is actually shown.
"""

from __future__ import annotations

import json
import os

SOURCE_LANGUAGE = 'en'
LOCALE_PACKAGE = f'{__package__}.locales'
CATALOG_SUFFIX = '.json'
# order matters: an explicit choice wins over the locale of the session
LANGUAGE_VARIABLES = ('WATTSON_LANG', 'LC_ALL', 'LC_MESSAGES', 'LANG')
# locales that mean "no localisation at all"
NEUTRAL_LOCALES = ('c', 'posix', '')

_language = SOURCE_LANGUAGE
_messages: dict[str, str] = {}


def normalize(code: str) -> str:
    """Language code without encoding and modifier.

    ``pt_BR.UTF-8`` and ``pt-br`` both become ``pt_br``.

    :param code: raw code from the command line or the environment.
    """
    bare = code.strip().split('.')[0].split('@')[0]
    return bare.replace('-', '_').lower()


def _candidates(code: str) -> list[str]:
    """Catalog names to try: region-specific first, bare language second."""
    normalized = normalize(code)
    if normalized in NEUTRAL_LOCALES:
        return []
    language = normalized.split('_')[0]
    return [normalized] if normalized == language else [normalized, language]


def _locale_root():
    """Directory with the catalogs, also inside the zipapp. None if absent."""
    try:
        from importlib.resources import files
        return files(LOCALE_PACKAGE)
    except (ImportError, ModuleNotFoundError, TypeError, OSError):
        return None


def available_languages() -> list[str]:
    """Codes that can be selected: English plus every catalog shipped."""
    found = {SOURCE_LANGUAGE}
    root = _locale_root()
    if root is not None:
        try:
            entries = list(root.iterdir())
        except (OSError, NotADirectoryError):
            entries = []
        for entry in entries:
            if entry.name.endswith(CATALOG_SUFFIX):
                found.add(entry.name[: -len(CATALOG_SUFFIX)])
    return sorted(found)


def _read_catalog(name: str) -> dict | None:
    """Raw contents of one catalog file, None when missing or unreadable."""
    root = _locale_root()
    if root is None:
        return None
    try:
        raw = json.loads(root.joinpath(name + CATALOG_SUFFIX).read_text(encoding='utf-8'))
    except (OSError, KeyError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _load_catalog(name: str) -> dict[str, str] | None:
    """Messages of one catalog, or None if it is missing or malformed."""
    raw = _read_catalog(name)
    messages = raw.get('messages') if raw is not None else None
    if not isinstance(messages, dict):
        return None
    return {str(key): str(value) for key, value in messages.items() if value}


def language_name(code: str) -> str:
    """Name of a language as it is shown in the interface.

    A catalog names itself through its optional ``name`` field; without one
    the bare code is shown.

    :param code: language code, e.g. ``de``.
    """
    if code == SOURCE_LANGUAGE:
        return 'English'
    raw = _read_catalog(code) or {}
    name = raw.get('name')
    return name if isinstance(name, str) and name else code


def language_from_environment() -> str:
    """Language wanted by the session, English if nothing meaningful is set."""
    for variable in LANGUAGE_VARIABLES:
        value = os.environ.get(variable, '')
        if normalize(value) not in NEUTRAL_LOCALES:
            return value
    return SOURCE_LANGUAGE


def set_language(code: str | None) -> str:
    """Switch the interface language and return the code actually in use.

    Falls back to the bare language of a regional code, and to English when
    no catalog matches.

    :param code: wanted code, e.g. ``de`` or ``pt_BR``; None means English.
    """
    global _language, _messages
    for candidate in _candidates(code or ''):
        if candidate == SOURCE_LANGUAGE:
            break
        catalog = _load_catalog(candidate)
        if catalog is not None:
            _language, _messages = candidate, catalog
            return _language
    _language, _messages = SOURCE_LANGUAGE, {}
    return _language


def current_language() -> str:
    """Code of the language in use right now."""
    return _language


def translate(message: str, **fields: object) -> str:
    """English source string rendered in the current language.

    Placeholders are filled with ``str.format``; a catalog entry with broken
    placeholders is discarded in favour of the English original instead of
    raising in the middle of drawing the interface.

    :param message: English source string, used as the catalog key.
    :param fields: values for the ``{name}`` placeholders of the message.
    """
    text = _messages.get(message, message)
    if not fields:
        return text
    try:
        return text.format(**fields)
    except (IndexError, KeyError, ValueError):
        return message.format(**fields)


set_language(language_from_environment())
