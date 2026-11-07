"""Unit tests for _get_s3_session credential selection logic."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from runner.utils.s3 import S3Credentials, _get_s3_session


class TestS3CredentialsRedaction:
    """Loguru's diagnose=True walks local variables and calls repr() on them.

    Secrets must never be present in the repr / str of an S3Credentials, or
    they would leak to stdout and Datadog whenever any S3 helper raises.
    """

    def test_repr_does_not_expose_secret_access_key(self) -> None:
        creds = S3Credentials(
            access_key_id="AKID",
            secret_access_key=SecretStr("super-sensitive-secret"),
            session_token=SecretStr("super-sensitive-token"),
            region="us-east-1",
        )
        rendered = repr(creds) + str(creds)
        assert "super-sensitive-secret" not in rendered
        assert "super-sensitive-token" not in rendered

    def test_get_secret_value_still_works(self) -> None:
        creds = S3Credentials(
            access_key_id="AKID",
            secret_access_key=SecretStr("super-sensitive-secret"),
            session_token=SecretStr("super-sensitive-token"),
        )
        assert creds.secret_access_key.get_secret_value() == "super-sensitive-secret"
        assert creds.session_token is not None
        assert creds.session_token.get_secret_value() == "super-sensitive-token"


class TestGetS3Session:
    def test_explicit_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODAL_IS_REMOTE", raising=False)
        creds = S3Credentials(
            access_key_id="AKID",
            secret_access_key=SecretStr("secret"),
            session_token=SecretStr("token"),
            region="us-east-1",
        )
        with patch("runner.utils.s3.aioboto3.Session") as mock_session:
            _get_s3_session(creds)
            mock_session.assert_called_once_with(
                aws_access_key_id="AKID",
                aws_secret_access_key="secret",
                aws_session_token="token",
                region_name="us-east-1",
            )

    def test_explicit_credentials_without_session_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MODAL_IS_REMOTE", raising=False)
        creds = S3Credentials(
            access_key_id="AKID",
            secret_access_key=SecretStr("secret"),
            region="us-east-1",
        )
        with patch("runner.utils.s3.aioboto3.Session") as mock_session:
            _get_s3_session(creds)
            mock_session.assert_called_once_with(
                aws_access_key_id="AKID",
                aws_secret_access_key="secret",
                aws_session_token=None,
                region_name="us-east-1",
            )

    def test_modal_remote_with_aws_env_vars_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODAL_IS_REMOTE", "1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.delenv("MODAL_IDENTITY_TOKEN", raising=False)
        monkeypatch.delenv("MODAL_OIDC_ROLE_ARN", raising=False)

        with patch("runner.utils.s3.aioboto3.Session") as mock_session:
            _get_s3_session()
            mock_session.assert_called_once_with()

    def test_modal_remote_without_any_credentials_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODAL_IS_REMOTE", "1")
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("MODAL_IDENTITY_TOKEN", raising=False)
        monkeypatch.delenv("MODAL_OIDC_ROLE_ARN", raising=False)

        with pytest.raises(RuntimeError, match="Running on Modal without"):
            _get_s3_session()

    def test_modal_remote_with_only_access_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODAL_IS_REMOTE", "1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKID")
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("MODAL_IDENTITY_TOKEN", raising=False)
        monkeypatch.delenv("MODAL_OIDC_ROLE_ARN", raising=False)

        with pytest.raises(RuntimeError, match="Running on Modal without"):
            _get_s3_session()

    def test_no_modal_remote_falls_back_to_default_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MODAL_IS_REMOTE", raising=False)
        monkeypatch.delenv("MODAL_IDENTITY_TOKEN", raising=False)
        monkeypatch.delenv("MODAL_OIDC_ROLE_ARN", raising=False)

        with patch("runner.utils.s3.aioboto3.Session") as mock_session:
            _get_s3_session()
            mock_session.assert_called_once_with()

    def test_oidc_exchange_uses_assumed_role_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "oidc-token")
        monkeypatch.setenv("MODAL_OIDC_ROLE_ARN", "arn:aws:iam::123:role/test")
        monkeypatch.delenv("MODAL_IS_REMOTE", raising=False)

        mock_sts = MagicMock()
        mock_sts.assume_role_with_web_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "STS_KEY",
                "SecretAccessKey": "STS_SECRET",
                "SessionToken": "STS_TOKEN",
            }
        }

        with (
            patch("boto3.client", return_value=mock_sts),
            patch("runner.utils.s3.aioboto3.Session") as mock_session,
        ):
            _get_s3_session()
            mock_session.assert_called_once_with(
                aws_access_key_id="STS_KEY",
                aws_secret_access_key="STS_SECRET",
                aws_session_token="STS_TOKEN",
            )

    def test_oidc_exchange_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "oidc-token")
        monkeypatch.setenv("MODAL_OIDC_ROLE_ARN", "arn:aws:iam::123:role/test")
        monkeypatch.delenv("MODAL_IS_REMOTE", raising=False)

        mock_sts = MagicMock()
        mock_sts.assume_role_with_web_identity.side_effect = Exception("STS error")

        with patch("boto3.client", return_value=mock_sts):
            with pytest.raises(Exception, match="STS error"):
                _get_s3_session()
