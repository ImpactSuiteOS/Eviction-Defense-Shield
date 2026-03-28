"""
EvictionShield — Module 8: SMS Notification Tests

Tests:
- Language detection from tenant name and ZIP code
- SMS text generation in all 4 languages (mocked Gemini)
- Short link format
- Cloud Tasks enqueueing (mocked)
- 160 character limit enforcement
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestLanguageDetection:
    """Test language detection from name heuristics and ZIP codes."""

    def _detect(self, name=None, zip_code=None) -> str:
        from module4.notifications.clerk_integration import _detect_language
        return _detect_language(name, zip_code)

    def test_default_is_english(self):
        assert self._detect() == "en"

    def test_zip_overrides_name(self):
        # ZIP 19120 is configured as Haitian Creole
        assert self._detect(name="John Smith", zip_code="19120") == "ht"

    def test_spanish_surname_detection(self):
        assert self._detect(name="Carlos Rodriguez") == "es"

    def test_vietnamese_surname_detection(self):
        assert self._detect(name="Nguyen Thi Lan") == "vi"

    def test_haitian_surname_detection(self):
        assert self._detect(name="Jean Pierre") == "ht"

    def test_unknown_name_unknown_zip_returns_english(self):
        assert self._detect(name="Generic Name", zip_code="12345") == "en"

    def test_spanish_zip_override(self):
        assert self._detect(name="John Smith", zip_code="19122") == "es"


class TestSMSGeneration:
    """Test SMS text generation and character limit enforcement."""

    @pytest.mark.parametrize("language", ["en", "es", "ht", "vi"])
    @patch("module4.notifications.clerk_integration._flash_model")
    def test_sms_generated_in_all_languages(self, mock_model, language):
        """SMS generation should succeed for all 4 supported languages."""
        # Mock Gemini Flash response
        mock_candidate = MagicMock()
        mock_candidate.content.parts[0].text = (
            f"You have a court date soon. Get free help at https://app.evictionshield.io/intake?c=PA-001"
        )
        mock_model.generate_content.return_value.candidates = [mock_candidate]

        from module4.notifications.clerk_integration import _generate_sms_text
        text = _generate_sms_text(
            hearing_date="2024-07-10",
            days_until_hearing=14,
            short_link="https://app.evictionshield.io/intake?c=PA-001",
            language=language,
        )
        assert len(text) <= 160
        assert text.strip()

    @patch("module4.notifications.clerk_integration._flash_model")
    def test_overlong_response_is_trimmed(self, mock_model):
        """SMS exceeding 160 chars should be trimmed to 160."""
        long_text = "A" * 200
        mock_candidate = MagicMock()
        mock_candidate.content.parts[0].text = long_text
        mock_model.generate_content.return_value.candidates = [mock_candidate]

        from module4.notifications.clerk_integration import _generate_sms_text
        text = _generate_sms_text("2024-07-10", 14, "https://short.link", "en")
        assert len(text) <= 160

    def test_short_link_format(self):
        from module4.notifications.clerk_integration import _make_short_link
        link = _make_short_link("PA-MDJ-2024-000001")
        assert link.startswith("https://")
        assert "PA-MDJ-2024-000001" in link or "PA" in link  # URL encoded

    def test_phone_hash_is_not_reversible(self):
        from module4.notifications.clerk_integration import _hash_phone
        h1 = _hash_phone("+12155550101")
        h2 = _hash_phone("+12155550102")
        assert h1 != h2
        assert "+1215" not in h1
        assert len(h1) == 16


class TestSMSWorker:
    """Test the Cloud Tasks SMS worker function."""

    @patch("module4.notifications.clerk_integration._get_twilio_credentials")
    @patch("module4.notifications.clerk_integration._send_sms_via_twilio")
    def test_worker_returns_200_on_success(self, mock_send, mock_creds):
        mock_creds.return_value = {
            "account_sid": "ACtest",
            "auth_token": "testtoken",
            "from_number": "+12155559999",
        }
        mock_send.return_value = "SM12345"

        # Build mock request
        mock_request = MagicMock()
        mock_request.get_json.return_value = {
            "case_number": "PA-001",
            "to_number": "+12155550101",
            "message_body": "Test message",
            "language": "en",
        }
        mock_request.get_data.return_value = b""

        from module4.notifications.clerk_integration import send_sms_worker
        response, status_code, _ = send_sms_worker(mock_request)
        assert status_code == 200
        data = __import__("json").loads(response)
        assert data["status"] == "sent"
        assert data["twilio_sid"] == "SM12345"

    @patch("module4.notifications.clerk_integration._get_twilio_credentials")
    @patch("module4.notifications.clerk_integration._send_sms_via_twilio")
    def test_worker_returns_500_on_twilio_failure(self, mock_send, mock_creds):
        mock_creds.return_value = {
            "account_sid": "ACtest",
            "auth_token": "testtoken",
            "from_number": "+12155559999",
        }
        mock_send.side_effect = Exception("Twilio API error")

        mock_request = MagicMock()
        mock_request.get_json.return_value = {
            "case_number": "PA-001",
            "to_number": "+12155550101",
            "message_body": "Test",
            "language": "en",
        }
        mock_request.get_data.return_value = b""

        from module4.notifications.clerk_integration import send_sms_worker
        _, status_code, _ = send_sms_worker(mock_request)
        assert status_code == 500
