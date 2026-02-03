#!/usr/bin/env python
"""Generate production secrets for Akosha deployment.

This script generates cryptographically secure secrets for production
Kubernetes deployments and saves them to k8s/secret.production.yaml.

Usage:
    python -m akosha.scripts.generate_secrets
"""

from __future__ import annotations

import secrets
from pathlib import Path


def generate_jwt_secret() -> str:
    """Generate a secure JWT secret.

    Returns:
        Base64-encoded secret (minimum 32 bytes for HS256)
    """
    return secrets.token_urlsafe(32)


def generate_encryption_key() -> str:
    """Generate a secure encryption key.

    Returns:
        Base64-encoded encryption key (32 bytes for AES-256)
    """
    return secrets.token_urlsafe(32)


def generate_production_secrets(
    output_path: str | Path = "k8s/secret.production.yaml",
    template_path: str | Path = "k8s/secret.production.yaml.template",
) -> None:
    """Generate production Kubernetes secret file.

    Args:
        output_path: Path to write generated secret file
        template_path: Path to template file
    """
    # Get project root (assumes script is in akosha/scripts/)
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    output_file = project_root / output_path
    template_file = project_root / template_path

    # Check if template exists
    if not template_file.exists():
        print(f"❌ Template not found: {template_file}")
        print("Using default production secret template...")

        # Create default template content
        template_content = """# Production Kubernetes Secret for Akosha
apiVersion: v1
kind: Secret
metadata:
  name: akosha-secrets
  namespace: akosha
type: Opaque
stringData:
  JWT_SECRET: "GENERATED_JWT_SECRET_PLACEHOLDER"
  ENCRYPTION_KEY: "GENERATED_ENCRYPTION_KEY_PLACEHOLDER"
"""
    else:
        template_content = template_file.read_text()

    # Generate secrets
    jwt_secret = generate_jwt_secret()
    encryption_key = generate_encryption_key()

    # Replace placeholders
    secret_content = template_content.replace(
        "GENERATED_JWT_SECRET_PLACEHOLDER", jwt_secret
    ).replace("GENERATED_ENCRYPTION_KEY_PLACEHOLDER", encryption_key)

    # Write output file
    output_file.write_text(secret_content)

    print(f"✅ Production secrets generated: {output_file}")
    print()
    print("📝 Generated Secrets:")
    print(f"   JWT_SECRET: {jwt_secret[:20]}... (length: {len(jwt_secret)})")
    print(f"   ENCRYPTION_KEY: {encryption_key[:20]}... (length: {len(encryption_key)})")
    print()
    print("⚠️  IMPORTANT:")
    print(f"   1. Store {output_file.name} securely (do NOT commit to git)")
    print("   2. Apply to cluster: kubectl apply -f k8s/secret.production.yaml")
    print("   3. Add to .gitignore: echo 'k8s/secret.production.yaml' >> .gitignore")
    print()


def main() -> None:
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate production secrets for Akosha")
    parser.add_argument(
        "--output",
        "-o",
        default="k8s/secret.production.yaml",
        help="Output path for generated secret file",
    )
    parser.add_argument(
        "--template",
        "-t",
        default="k8s/secret.production.yaml.template",
        help="Template file path",
    )

    args = parser.parse_args()

    generate_production_secrets(args.output, args.template)


if __name__ == "__main__":
    main()
