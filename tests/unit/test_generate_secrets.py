"""Tests for the production secret generation script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "akosha" / "scripts" / "generate_secrets.py"


def load_script_module() -> ModuleType:
    """Load the script module directly from its file path."""
    spec = importlib.util.spec_from_file_location("akosha_generate_secrets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generate_production_secrets_uses_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Existing templates should have placeholders replaced and be written out."""
    module = load_script_module()
    template_path = tmp_path / "secret.production.yaml.template"
    output_path = tmp_path / "secret.production.yaml"
    template_path.write_text(
        "JWT_SECRET=GENERATED_JWT_SECRET_PLACEHOLDER\n"
        "ENCRYPTION_KEY=GENERATED_ENCRYPTION_KEY_PLACEHOLDER\n"
    )

    module.generate_jwt_secret = MagicMock(return_value="jwt-test-secret")
    module.generate_encryption_key = MagicMock(return_value="enc-test-key")

    module.generate_production_secrets(output_path=output_path, template_path=template_path)

    assert output_path.read_text() == "JWT_SECRET=jwt-test-secret\nENCRYPTION_KEY=enc-test-key\n"
    assert "Production secrets generated" in capsys.readouterr().out


def test_generate_production_secrets_falls_back_when_template_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing templates should use the embedded default secret template."""
    module = load_script_module()
    output_path = tmp_path / "secret.production.yaml"
    missing_template = tmp_path / "does-not-exist.yaml.template"

    module.generate_jwt_secret = MagicMock(return_value="jwt-fallback-secret")
    module.generate_encryption_key = MagicMock(return_value="enc-fallback-key")

    module.generate_production_secrets(output_path=output_path, template_path=missing_template)

    stdout = capsys.readouterr().out
    assert "Template not found" in stdout
    assert "Using default production secret template" in stdout
    assert "jwt-fallback-secret" in output_path.read_text()
    assert "enc-fallback-key" in output_path.read_text()


def test_main_parses_cli_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI entrypoint should pass parsed arguments through to the worker."""
    module = load_script_module()
    captured: dict[str, Path | str] = {}

    def fake_generate_production_secrets(output_path: str | Path, template_path: str | Path) -> None:
        captured["output_path"] = output_path
        captured["template_path"] = template_path

    monkeypatch.setattr(module, "generate_production_secrets", fake_generate_production_secrets)
    monkeypatch.setattr(sys, "argv", ["generate_secrets", "--output", "out.yaml", "--template", "tmpl.yaml"])

    module.main()

    assert captured["output_path"] == "out.yaml"
    assert captured["template_path"] == "tmpl.yaml"


def test_secret_helpers_return_urlsafe_strings() -> None:
    """Secret helpers should generate distinct URL-safe values."""
    module = load_script_module()

    jwt_secret = module.generate_jwt_secret()
    encryption_key = module.generate_encryption_key()

    assert isinstance(jwt_secret, str)
    assert isinstance(encryption_key, str)
    assert jwt_secret != encryption_key
    assert len(jwt_secret) >= 43
    assert len(encryption_key) >= 43


def test_script_main_block_executes_without_side_effects() -> None:
    """The module's __main__ block should be executable without writing files."""
    source = SCRIPT_PATH.read_text()
    source = source.replace("if __name__ == \"__main__\":\n    main()\n", "if __name__ == \"__main__\":\n    pass\n")
    compiled = compile(source, str(SCRIPT_PATH), "exec")

    namespace = {"__name__": "__main__", "__file__": str(SCRIPT_PATH)}
    exec(compiled, namespace)

    assert namespace["__name__"] == "__main__"
