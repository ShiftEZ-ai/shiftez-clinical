from dataclasses import replace

import pytest

from .test_deployment_preflight import clean_env, production


def test_production_users_file_rejects_external_oidc_issuer(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    users = tmp_path / "users.json"
    users.write_text("{}", encoding="utf-8")
    settings = production(
        identity_users_file=str(users),
        fmuc_data_key="01" * 32,
        oidc_issuer="https://issuer.example",
    )

    with pytest.raises(
        RuntimeError,
        match="^ROTA_IDENTITY_USERS_FILE cannot be combined with an external OIDC issuer in production$",
    ):
        settings.check()

    replace(settings, oidc_issuer=settings.public_origin + "/authelia").check()
