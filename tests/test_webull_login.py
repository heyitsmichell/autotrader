"""Tests for Webull login expiry."""

import pytest
import time
from pathlib import Path


class TestWebullLogin:
    """Test Webull login token handling."""

    @pytest.mark.asyncio
    async def test_expiry_read_from_token_txt(self, test_settings, tmp_data_dir):
        """Expiry should be read from token.txt."""
        from app.supervisor import read_token_info

        (tmp_data_dir / "conf").mkdir(exist_ok=True)
        exp_ms = int((time.time() + 2 * 86_400) * 1000)
        (tmp_data_dir / "conf" / "token.txt").write_text(
            f"secret-token\n{exp_ms}\nNORMAL\n", encoding="utf-8"
        )

        info = read_token_info(tmp_data_dir / "conf")
        assert info["status"] == "NORMAL"
        assert 1.9 < info["days_left"] < 2.1
        assert info["estimated"] is False

    @pytest.mark.asyncio
    async def test_token_itself_never_read(self, test_settings, tmp_data_dir):
        """The token itself should never be read."""
        from app.supervisor import read_token_info

        (tmp_data_dir / "conf").mkdir(exist_ok=True)
        exp_ms = int((time.time() + 2 * 86_400) * 1000)
        (tmp_data_dir / "conf" / "token.txt").write_text(
            f"secret-token\n{exp_ms}\nNORMAL\n", encoding="utf-8"
        )

        info = read_token_info(tmp_data_dir / "conf")
        assert "secret" not in str(info)

    @pytest.mark.asyncio
    async def test_missing_login_detected(self, test_settings, tmp_data_dir):
        """Missing login should be detected."""
        from app.supervisor import read_token_info

        info = read_token_info(tmp_data_dir / "nowhere")
        assert info["status"] == "missing"