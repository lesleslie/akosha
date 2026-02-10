"""Data migration commands for Akosha.

Provides CLI commands for migrating data from legacy project-local paths
to XDG-compliant locations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from akosha.storage.path_resolver import get_default_resolver


@click.group()
def migrate() -> None:
    """Data migration commands for Akosha."""
    pass


@migrate.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be migrated without making changes",
)
@click.option(
    "--from-path",
    type=click.Path(exists=True, path_type=Path),
    default=Path.cwd() / "data",
    help="Source path to migrate from (default: ./data/)",
)
@click.option(
    "--to-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Destination path (default: XDG-compliant location)",
)
def data(dry_run: bool, from_path: Path, to_path: Path | None) -> None:
    """Migrate data from project-local to XDG-compliant location.

    This command migrates your Akosha data from the legacy ./data/ directory
    to the standard XDG-compliant location.

    Examples:
        akosha migrate data --dry-run
        akosha migrate data --from ./data --to ~/.local/share/akosha
    """
    # Determine destination
    if to_path is None:
        resolver = get_default_resolver()
        to_path = resolver.base_path

    click.echo(f"{'[DRY RUN] ' if dry_run else ''}Migrating Akosha data...")
    click.echo(f"  Source: {from_path}")
    click.echo(f"  Target: {to_path}")

    # Check source exists
    if not from_path.exists():
        click.echo(f"❌ Source path not found: {from_path}", err=True)
        raise click.ClickException(1)

    # Check for data in subdirectories
    subdirs = [d for d in from_path.iterdir() if d.is_dir() and any(d.iterdir())]
    if not subdirs:
        click.echo("⚠️  No data found in source path (already migrated?)")
        return

    # Show what will be migrated
    click.echo(f"\nFound {len(subdirs)} data directories to migrate:")
    for subdir in subdirs:
        size = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())
        size_mb = size / (1024 * 1024)
        click.echo(f"  - {subdir.name}/ ({size_mb:.1f} MB)")

    if dry_run:
        click.echo("\n✅ Dry run complete - no changes made")
        return

    # Confirm migration
    if not click.confirm("\nProceed with migration?", default=False):
        click.echo("Migration cancelled")
        return

    # Create destination directory
    to_path.mkdir(parents=True, exist_ok=True)

    # Migrate each subdirectory
    results = {"migrated": 0, "skipped": 0, "errors": 0}

    for subdir in subdirs:
        try:
            dest_dir = to_path / subdir.name
            dest_dir.mkdir(exist_ok=True)

            # Copy files
            for item in subdir.iterdir():
                dest = dest_dir / item.name
                if item.is_file() and not dest.exists():
                    shutil.copy2(item, dest)
                elif item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)

            results["migrated"] += 1
            click.echo(f"✅ Migrated {subdir.name}/")

        except Exception as e:
            results["errors"] += 1
            click.echo(f"❌ Error migrating {subdir.name}/: {e}", err=True)

    # Summary
    click.echo("\nMigration complete:")
    click.echo(f"  ✅ Migrated: {results['migrated']}")
    click.echo(f"  ⏭️  Skipped: {results['skipped']}")
    click.echo(f"  ❌ Errors: {results['errors']}")

    if results["errors"] == 0:
        click.echo(f"\n✅ You can now safely delete: {from_path}")


@migrate.command()
@click.option(
    "--check",
    is_flag=True,
    help="Check if migration is needed",
)
def status(check: bool) -> None:
    """Check migration status and show current paths."""
    resolver = get_default_resolver()

    click.echo("Akosha Storage Paths:")
    click.echo(f"  Environment: {resolver.env}")
    click.echo(f"  Base path: {resolver.base_path}")
    click.echo(f"  Warm store: {resolver.get_warm_store_path()}")
    click.echo(f"  WAL path: {resolver.get_hot_store_wal_path()}")
    click.echo(f"  Config dir: {resolver.get_config_dir()}")
    click.echo(f"  Cache dir: {resolver.get_cache_dir()}")

    # Check for legacy data
    legacy_path = Path.cwd() / "data"
    if legacy_path.exists() and any(legacy_path.iterdir()):
        click.echo(f"\n⚠️  Legacy data detected at: {legacy_path}")
        click.echo("    Run 'akosha migrate data' to migrate to XDG-compliant location")
    else:
        click.echo("\n✅ No legacy data found")


@migrate.command()
@click.argument("path", type=click.Path(path_type=Path))
def rollback(path: Path) -> None:
    """Rollback migration to legacy path.

    Examples:
        akosha migrate rollback ./data/warm
    """
    resolver = get_default_resolver()
    current_path = resolver.get_warm_store_path().parent

    click.echo("Rolling back migration...")
    click.echo(f"  From: {current_path}")
    click.echo(f"  To: {path}")

    if not current_path.exists():
        click.echo(f"❌ Current path not found: {current_path}", err=True)
        raise click.ClickException(1)

    # Create destination
    path.mkdir(parents=True, exist_ok=True)

    # Copy data back
    if not click.confirm(f"Copy data from {current_path} to {path}?", default=False):
        click.echo("Rollback cancelled")
        return

    try:
        shutil.copytree(current_path, path, dirs_exist_ok=True)
        click.echo("✅ Rollback complete")
        click.echo(f"  Data copied to: {path}")
    except Exception as e:
        click.echo(f"❌ Rollback failed: {e}", err=True)
        raise click.ClickException(1)
