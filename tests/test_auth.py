"""Tests for the AuthManager."""

from spark.web.auth import AuthManager


class TestAuthManager:
    def test_generate_code_returns_string(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        assert isinstance(code, str)
        assert len(code) == 8

    def test_generate_code_is_uppercase_hex(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        assert code == code.upper()
        # Should be valid hex
        int(code, 16)

    def test_validate_correct_code(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        assert auth.validate(code) is True

    def test_validate_case_insensitive(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        assert auth.validate(code.lower()) is True

    def test_validate_wrong_code(self) -> None:
        auth = AuthManager()
        auth.generate_code()
        assert auth.validate("ZZZZZZZZ") is False

    def test_validate_empty_code(self) -> None:
        auth = AuthManager()
        assert auth.validate("") is False

    def test_validate_no_codes_generated(self) -> None:
        auth = AuthManager()
        assert auth.validate("ABCDEF01") is False

    def test_code_reusable(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        assert auth.validate(code) is True
        assert auth.validate(code) is True

    def test_multiple_codes(self) -> None:
        auth = AuthManager()
        code1 = auth.generate_code()
        code2 = auth.generate_code()
        assert code1 != code2
        assert auth.validate(code1) is True
        assert auth.validate(code2) is True

    def test_use_count_increments(self) -> None:
        auth = AuthManager()
        code = auth.generate_code()
        auth.validate(code)
        auth.validate(code)
        # Access internal state to verify
        import hashlib

        code_hash = hashlib.sha256(code.encode()).hexdigest()
        record = auth._codes[code_hash]
        assert record.use_count == 2
        assert record.last_used is not None
