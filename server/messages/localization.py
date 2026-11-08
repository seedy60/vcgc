"""Localization system using Mozilla Fluent."""

import builtins
import hashlib
import importlib.util
import logging
import marshal
import os
import pickle
import sys
from pathlib import Path

from babel import Locale
from babel.lists import format_list
from babel.plural import to_python as babel_plural_form
from fluent_compiler import runtime
from fluent_compiler.builtins import BUILTINS
from fluent_compiler.bundle import FluentBundle
from fluent_compiler.compiler import (
    LOCALE_NAME,
    PLURAL_FORM_FOR_NUMBER_NAME,
    TERM_SIGIL,
    _parse_resources,
    messages_to_module,
)
from fluent_compiler.resource import FtlResource


LOG = logging.getLogger("playpalace.localization")


class Localization:
    """
    Localization system using Mozilla Fluent via fluent-compiler.

    Loads .ftl files from the locales directory and provides message
    rendering with variable substitution.
    """

    _bundles: dict[str, FluentBundle] = {}
    _locales_dir: Path | None = None
    _cache_dir: Path | None = None
    _cache_enabled: bool = True
    _warmup_active: bool = False
    _CACHE_VERSION = "2"
    _CACHE_DISABLE_ENV = "PLAYPALACE_DISABLE_LOCALE_CACHE"
    _CACHE_DIR_ENV = "PLAYPALACE_LOCALE_CACHE_DIR"
    _enabled_locales: set[str] | None = None  # None = all locales
    _missing_key_fallback_warnings: set[tuple[str, str]] = set()

    @classmethod
    def set_warmup_active(cls, active: bool) -> None:
        cls._warmup_active = active

    @classmethod
    def is_warmup_active(cls) -> bool:
        return cls._warmup_active

    @classmethod
    def init(cls, locales_dir: Path | str, *, enabled_locales: list[str] | None = None) -> None:
        """Initialize the localization system with a locales directory.

        Args:
            locales_dir: Path to the locales directory.
            enabled_locales: If set, only load these locales (plus ``en`` as
                fallback).  ``None`` loads all locales.
        """
        cls._locales_dir = Path(locales_dir)
        cls._bundles = {}
        cls._missing_key_fallback_warnings = set()
        disable_cache = os.environ.get(cls._CACHE_DISABLE_ENV, "").strip().lower()
        cls._cache_enabled = disable_cache not in {"1", "true", "yes", "on"}
        cls._cache_dir = None
        if enabled_locales is not None:
            cls._enabled_locales = {*enabled_locales, "en"}
        else:
            cls._enabled_locales = None

    @classmethod
    def preload_bundles(cls) -> None:
        """Pre-load all locale bundles at startup."""
        if cls._locales_dir is None:
            print("ERROR: Localization directory is not configured.", file=sys.stderr)
            raise SystemExit(1)

        if not cls._locales_dir.exists() or not cls._locales_dir.is_dir():
            print(
                f"ERROR: Localization directory '{cls._locales_dir}' is missing or not a directory.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        found_locale = False
        for locale_dir in cls._locales_dir.iterdir():
            if not locale_dir.is_dir():
                continue
            if cls._enabled_locales is not None and locale_dir.name not in cls._enabled_locales:
                continue
            found_locale = True
            try:
                cls._get_bundle(locale_dir.name)
            except RuntimeError as exc:
                print(
                    f"ERROR: Failed to load localization bundle for '{locale_dir.name}': {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(1) from exc

        if not found_locale:
            print(
                f"ERROR: Localization directory '{cls._locales_dir}' does not contain any locale bundles.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    @classmethod
    def _get_bundle(cls, locale: str) -> FluentBundle:
        """Get or create a bundle for a locale."""
        if locale in cls._bundles:
            return cls._bundles[locale]

        if cls._locales_dir is None:
            print("ERROR: Localization directory is not configured.", file=sys.stderr)
            raise SystemExit(1)

        locale_dir = cls._locales_dir / locale
        actual_locale = locale
        if not locale_dir.exists():
            # Fall back to English
            locale_dir = cls._locales_dir / "en"
            actual_locale = "en"
            if not locale_dir.exists():
                print(
                    f"ERROR: No localization files found for '{locale}' or fallback 'en'.",
                    file=sys.stderr,
                )
                raise SystemExit(1)

        payloads, fingerprint = cls._load_locale_payloads(locale_dir, actual_locale)
        bundle = cls._load_bundle_from_cache(actual_locale, fingerprint)
        if bundle is None:
            bundle = cls._compile_bundle(actual_locale, payloads, fingerprint)
        cls._bundles[locale] = bundle
        return bundle

    @classmethod
    def _load_locale_payloads(cls, locale_dir: Path, actual_locale: str) -> tuple[list[str], str]:
        """Read locale files and compute a content fingerprint."""
        ftl_files = sorted(locale_dir.glob("*.ftl"))
        if not ftl_files:
            print(
                f"ERROR: No .ftl localization files found in {locale_dir}.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        digest = hashlib.sha256()
        digest.update(cls._CACHE_VERSION.encode("utf-8"))
        # Cached artifacts hold marshalled bytecode, which is tied to the
        # running interpreter version.
        digest.update(importlib.util.MAGIC_NUMBER)
        digest.update(actual_locale.encode("utf-8"))

        payloads: list[str] = []
        for ftl_file in ftl_files:
            text = ftl_file.read_text(encoding="utf-8")
            payloads.append(text)
            encoded = text.encode("utf-8")
            digest.update(ftl_file.name.encode("utf-8"))
            digest.update(len(encoded).to_bytes(8, "big", signed=False))
            digest.update(hashlib.sha256(encoded).digest())

        return payloads, digest.hexdigest()

    @classmethod
    def _load_bundle_from_cache(cls, actual_locale: str, fingerprint: str) -> FluentBundle | None:
        """Load a cached bundle when available.

        Cache entries hold the Python bytecode that fluent-compiler generated
        for the locale, so loading a bundle only runs ``exec`` on already
        compiled code instead of re-parsing and re-compiling every ``.ftl``
        file on each start.
        """
        cache_root = cls._resolve_cache_dir()
        if cache_root is None:
            return None

        cache_path = cache_root / actual_locale / f"{fingerprint}.pkl"
        if not cache_path.exists():
            return None

        try:
            payload = pickle.loads(cache_path.read_bytes())  # noqa: S301 - trusted local cache
            if payload.get("version") != cls._CACHE_VERSION:
                raise ValueError("Cache version mismatch")
            if payload.get("fingerprint") != fingerprint:
                raise ValueError("Cache fingerprint mismatch")
            if payload.get("locale") != actual_locale:
                raise ValueError("Cache locale mismatch")
            message_names = payload["messages"]
            modules = payload["modules"]
            builtin_aliases = payload["builtins"]
            if not isinstance(message_names, dict) or not isinstance(modules, list):
                raise ValueError("Cache payload missing")

            module_globals = cls._base_module_globals(actual_locale)
            for assigned_name, builtin_name in builtin_aliases.items():
                module_globals[assigned_name] = BUILTINS[builtin_name]
            for blob in modules:
                exec(marshal.loads(blob), module_globals)  # noqa: S102 - trusted local cache
            message_functions = {
                message_id: module_globals[name] for message_id, name in message_names.items()
            }
        except Exception:
            LOG.debug("Discarding corrupt locale cache for '%s'", actual_locale, exc_info=True)
            try:
                cache_path.unlink()
            except OSError:
                pass
            return None

        bundle = object.__new__(FluentBundle)
        bundle.locale = actual_locale
        bundle._compiled_messages = message_functions
        bundle._compilation_errors = []
        return bundle

    @classmethod
    def _base_module_globals(cls, actual_locale: str) -> dict[str, object]:
        """Rebuild the globals namespace that generated Fluent modules expect."""
        locale = Locale.parse(actual_locale.replace("-", "_"))
        plural_form = babel_plural_form(locale.plural_form)

        def plural_form_for_number(number: object) -> str | None:
            try:
                return plural_form(number)
            except TypeError:
                # Strings can reach this when we guessed a CLDR category.
                return None

        module_globals: dict[str, object] = {
            name: getattr(runtime, name) for name in runtime.__all__
        }
        module_globals.update(builtins.__dict__)
        module_globals[LOCALE_NAME] = locale
        module_globals[PLURAL_FORM_FOR_NUMBER_NAME] = plural_form_for_number
        return module_globals

    @classmethod
    def _compile_bundle(
        cls,
        actual_locale: str,
        payloads: list[str],
        fingerprint: str,
        *,
        write_cache: bool = True,
    ) -> FluentBundle:
        """Compile locale files and persist a cache entry of the generated code."""
        resources = [FtlResource.from_string(text) for text in payloads]
        messages, parse_errors = _parse_resources(resources)
        babel_locale = Locale.parse(actual_locale.replace("-", "_"))
        module, message_mapping, module_globals, compile_errors = messages_to_module(
            messages, babel_locale, functions=dict(BUILTINS)
        )

        # Mirror fluent-compiler's compile_messages(): execute every generated
        # module into the shared globals namespace and keep the bytecode so the
        # next start can skip this work entirely.
        modules: list[bytes] = []
        for module_ast in module.as_multiple_module_ast():
            filename = getattr(module_ast.body[0], "filename", "<string>")
            code = compile(module_ast, filename, "exec")
            exec(code, module_globals)  # noqa: S102 - generated from local .ftl files
            modules.append(marshal.dumps(code))

        message_names = {
            str(message_id): name
            for message_id, name in message_mapping.items()
            if not str(message_id).startswith(TERM_SIGIL)
        }
        message_functions = {
            message_id: module_globals[name] for message_id, name in message_names.items()
        }

        bundle = object.__new__(FluentBundle)
        bundle.locale = actual_locale
        bundle._compiled_messages = message_functions
        bundle._compilation_errors = list(parse_errors) + list(compile_errors)
        if write_cache:
            cls._write_cache_entry(
                actual_locale, fingerprint, message_names, modules, module_globals
            )
        return bundle

    @classmethod
    def _resolve_cache_dir(cls) -> Path | None:
        """Resolve (or create) the cache directory."""
        if not cls._cache_enabled:
            return None
        if cls._cache_dir is not None:
            return cls._cache_dir
        base = os.environ.get(cls._CACHE_DIR_ENV)
        if base:
            path = Path(base)
        elif cls._locales_dir is not None:
            path = cls._locales_dir.parent / ".cache" / "locales"
        else:
            return None
        path.mkdir(parents=True, exist_ok=True)
        cls._cache_dir = path
        return cls._cache_dir

    @classmethod
    def _write_cache_entry(
        cls,
        actual_locale: str,
        fingerprint: str,
        message_names: dict[str, str],
        modules: list[bytes],
        module_globals: dict[str, object],
    ) -> None:
        """Persist compiled bytecode artifacts for reuse on the next start."""
        cache_root = cls._resolve_cache_dir()
        if cache_root is None:
            return
        builtin_ids = {id(function): name for name, function in BUILTINS.items()}
        builtin_aliases = {
            assigned_name: builtin_ids[id(value)]
            for assigned_name, value in module_globals.items()
            if id(value) in builtin_ids
        }
        payload = {
            "version": cls._CACHE_VERSION,
            "fingerprint": fingerprint,
            "locale": actual_locale,
            "messages": message_names,
            "builtins": builtin_aliases,
            "modules": modules,
        }
        entry_dir = cache_root / actual_locale
        entry_dir.mkdir(parents=True, exist_ok=True)
        final_path = entry_dir / f"{fingerprint}.pkl"
        tmp_path = entry_dir / f"{fingerprint}.tmp"
        tmp_path.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp_path, final_path)
        for cached in entry_dir.iterdir():
            if cached == final_path or not cached.is_file():
                continue
            if cached.suffix in {".json", ".pkl", ".tmp"}:
                try:
                    cached.unlink()
                except OSError:
                    pass

    # Unicode bidi isolation characters that Fluent adds around variables
    _BIDI_CHARS = "\u2068\u2069"  # FIRST STRONG ISOLATE, POP DIRECTIONAL ISOLATE

    @classmethod
    def _format_message(cls, locale: str, message_id: str, kwargs: dict[str, object]) -> str:
        """Format a message from a specific locale bundle."""
        bundle = cls._get_bundle(locale)
        result, errors = bundle.format(message_id, kwargs)
        if errors:
            LOG.warning(
                "Fluent formatting errors for '%s' [%s]: %s",
                message_id, locale, errors,
            )
        for char in cls._BIDI_CHARS:
            result = result.replace(char, "")
        return result

    @classmethod
    def get(cls, locale: str, message_id: str, **kwargs) -> str:
        """
        Get a localized message.

        Args:
            locale: The locale code (e.g., 'en', 'es').
            message_id: The message ID from the .ftl file.
            **kwargs: Variables to substitute into the message.

        Returns:
            The formatted message string.
        """
        try:
            return cls._format_message(locale, message_id, kwargs)
        except Exception:
            if locale != "en":
                try:
                    fallback = cls._format_message("en", message_id, kwargs)
                except Exception:
                    pass
                else:
                    warning_key = (locale, message_id)
                    if warning_key not in cls._missing_key_fallback_warnings:
                        cls._missing_key_fallback_warnings.add(warning_key)
                        LOG.warning(
                            "Missing localized message '%s' for locale '%s'; falling back to English.",
                            message_id, locale,
                        )
                    return fallback
            LOG.exception(
                "Failed to format message '%s' for locale '%s'",
                message_id, locale,
            )
            return f"[{message_id}]"

    @classmethod
    def format_list_and(cls, locale: str, items: list[str]) -> str:
        """
        Format a list with 'and' conjunction using Babel.

        Args:
            locale: The locale code.
            items: List of items to format.

        Returns:
            Formatted list string (e.g., "A, B, and C").
        """
        return format_list(items, style="standard", locale=locale)

    @classmethod
    def format_list_or(cls, locale: str, items: list[str]) -> str:
        """
        Format a list with 'or' conjunction using Babel.

        Args:
            locale: The locale code.
            items: List of items to format.

        Returns:
            Formatted list string (e.g., "A, B, or C").
        """
        return format_list(items, style="or", locale=locale)

    @classmethod
    def get_available_locale_codes(cls) -> list[str]:
        """Return sorted language codes from the locales directory.

        Only returns locales that are enabled in the server configuration.
        Unlike :meth:`get_available_languages`, this only scans the
        filesystem and never triggers bundle compilation, so it is safe
        to call during warmup.
        """
        if cls._locales_dir is None:
            raise RuntimeError("Localization not initialized. Call Localization.init() first.")
        return sorted(
            locale_dir.name
            for locale_dir in cls._locales_dir.iterdir()
            if locale_dir.is_dir()
            and (cls._enabled_locales is None or locale_dir.name in cls._enabled_locales)
        )

    @classmethod
    def get_available_languages(
        cls, display_language: str = "", *, fallback: str = "en"
    ) -> dict[str, str]:
        """
        Get a dictionary of available languages.

        Args:
            display_language: The locale to use for displaying language names.
                              If empty, each language name is shown in its own
                              language (e.g., "English" for en, "中文" for zh).
            fallback: The locale to use if a language name is not found
                             in the display language. Defaults to "en".

        Returns:
            Dictionary mapping language codes to language names.
        """
        if cls._locales_dir is None:
            raise RuntimeError("Localization not initialized. Call Localization.init() first.")

        result = {}

        # Get list of valid locale directories (filtered by enabled_locales)
        locales = [
            locale_dir.name
            for locale_dir in cls._locales_dir.iterdir()
            if locale_dir.is_dir()
            and (cls._enabled_locales is None or locale_dir.name in cls._enabled_locales)
        ]

        for locale_code in sorted(locales):
            message_id = f"language-{locale_code}"
            if display_language:
                # Use the display language's bundle for all names
                name = cls.get(display_language, message_id)
            else:
                # Use each locale's own bundle for its name
                name = cls.get(locale_code, message_id)

            # If translation not found, try fallback locale
            if name in (message_id, f"[{message_id}]") and fallback != display_language:
                name = cls.get(fallback, message_id)

            # If fallback is not "en" and still not found, try "en"
            if name in (message_id, f"[{message_id}]") and fallback != "en":
                name = cls.get("en", message_id)

            result[locale_code] = name

        return result


def get_message(locale: str, message_id: str, **kwargs) -> str:
    """Convenience function to get a localized message."""
    return Localization.get(locale, message_id, **kwargs)
