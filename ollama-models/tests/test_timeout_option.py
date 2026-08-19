"""A configured Ollama timeout must actually reach the HTTP client.

Found by driving, not by reading: a chat turn against a local 12B reasoning model kept
dying on ``httpx.ReadTimeout`` at roughly a minute even though the dev home had
``timeout_secs`` raised to 900. The cause was a type test — the old code accepted the
value only ``if isinstance(raw, (int, float))``, while Settings persists provider options
as **strings** (a real home carries ``"timeout_secs": "120"``). So no timeout a user had
ever typed was honoured, and every request silently used the 60 s default. Nothing failed
loudly; long generations just died.

Same family as the platform's "a defaulted field is an unsupplied input" rule: a value
that fails validation must not be replaced by a default in silence when the default
changes behaviour the user was trying to change.
"""

import provider


class TestTheConfiguredTimeoutIsHonoured:
    def test_a_string_from_the_settings_ui_is_used(self) -> None:
        """The exact shape a real dev home stores."""
        assert provider._timeout_or_default("120") == 120.0
        assert provider._timeout_or_default("900") == 900.0

    def test_a_number_still_works(self) -> None:
        assert provider._timeout_or_default(45) == 45.0
        assert provider._timeout_or_default(45.5) == 45.5

    def test_a_decimal_string_works(self) -> None:
        assert provider._timeout_or_default("12.5") == 12.5

    def test_the_default_is_not_silently_applied_to_a_real_value(self) -> None:
        """The regression itself: a configured value must differ from the default."""
        assert provider._timeout_or_default("900") != provider._DEFAULT_TIMEOUT


class TestGarbageFallsBackWithoutRaising:
    """A malformed option must not make the provider unbuildable."""

    def test_nonsense_strings_fall_back(self) -> None:
        for raw in ("", "   ", "abc", "12s", "None"):
            assert provider._timeout_or_default(raw) == provider._DEFAULT_TIMEOUT

    def test_none_and_missing_fall_back(self) -> None:
        assert provider._timeout_or_default(None) == provider._DEFAULT_TIMEOUT

    def test_a_bool_is_not_a_timeout(self) -> None:
        """``bool`` is an ``int`` subclass, so ``True`` would otherwise mean 1 second."""
        assert provider._timeout_or_default(True) == provider._DEFAULT_TIMEOUT
        assert provider._timeout_or_default(False) == provider._DEFAULT_TIMEOUT

    def test_non_positive_values_fall_back(self) -> None:
        """A 0 s timeout would fail every request; treat it as garbage, not as intent."""
        for raw in (0, "0", -1, "-30", 0.0):
            assert provider._timeout_or_default(raw) == provider._DEFAULT_TIMEOUT

    def test_nan_falls_back(self) -> None:
        assert provider._timeout_or_default("nan") == provider._DEFAULT_TIMEOUT

    def test_a_list_falls_back(self) -> None:
        assert provider._timeout_or_default([900]) == provider._DEFAULT_TIMEOUT
