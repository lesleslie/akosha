"""Data migration commands for Akosha.

Provides CLI commands for moving project-local data into the default
application storage location.
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
    help="Destination path (default: application storage location)",
)
def data(dry_run: bool, from_path: Path, to_path: Path | None) -> None:
    """Move data from a project-local path to the default storage location."""
    to_path = _resolve_destination(to_path)
    click.echo(f"{'[DRY RUN] ' if dry_run else ''}Moving Akosha data...")
    click.echo(f"  Source: {from_path}")
    click.echo(f"  Target: {to_path}")

    if not from_path.exists():
        click.echo(f"❌ Source path not found: {from_path}", err=True)
        raise SystemExit(1)

    subdirs = _discover_subdirs(from_path)
    if not subdirs:
        click.echo("⚠️  No project-local data found in the source path.")
        return

    _print_migration_size(subdirs)

    if dry_run:
        click.echo("\n✅ Dry run complete - no changes made")
        return

    if not click.confirm("\nProceed with migration?", default=False):
        click.echo("Migration cancelled")
        return

    to_path.mkdir(parents=True, exist_ok=True)
    results = _migrate_subdirs(subdirs, to_path)
    _print_migration_summary(results, from_path)


def _resolve_destination(to_path: Path | None) -> Path:
    if to_path is not None:
        return to_path
    resolver = get_default_resolver()
    return resolver.base_path


def _discover_subdirs(from_path: Path) -> list[Path]:
    return [d for d in from_path.iterdir() if d.is_dir() and any(d.iterdir())]


def _print_migration_size(subdirs: list[Path]) -> None:
    click.echo(f"\nFound {len(subdirs)} data directories to migrate:")
    for subdir in subdirs:
        size = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())
        click.echo(f"  - {subdir.name}/ ({size / (1024 * 1024):.1f} MB)")


def _migrate_subdirs(subdirs: list[Path], to_path: Path) -> dict[str, int]:
    results: dict[str, int] = {"migrated": 0, "skipped": 0, "errors": 0}
    for subdir in subdirs:
        dest_dir = to_path / subdir.name
        dest_dir.mkdir(exist_ok=True)
        ok, skipped = _copy_dir_contents(subdir, dest_dir)
        results["migrated"] += ok
        results["skipped"] += skipped
        click.echo(f"✅ Migrated {subdir.name}/")
    return results


def _copy_dir_contents(src: Path, dest: Path) -> tuple[int, int]:
    migrated, skipped = 0, 0
    for item in src.iterdir():
        item_dest = dest / item.name
        try:
            if item.is_file() and not item_dest.exists():
                shutil.copy2(item, item_dest)
                migrated += 1
            elif item.is_dir():
                shutil.copytree(item, item_dest, dirs_exist_ok=True)
                migrated += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return migrated, skipped


def _print_migration_summary(results: dict[str, int], from_path: Path) -> None:
    click.echo("\nMove complete:")
    click.echo(f"  ✅ Migrated: {results['migrated']}")
    click.echo(f"  ⏭️  Skipped: {results['skipped']}")
    click.echo(f"  ❌ Errors: {results['errors']}")
    if results["errors"] == 0:
        click.echo(f"\n✅ You can now remove the source directory: {from_path}")


@migrate.command()
@click.option(
    "--check",
    is_flag=True,
    help="Check if migration is needed",
)
def status(check: bool) -> None:  # noqa: ARG001
    """Check storage status and show current paths."""
    resolver = get_default_resolver()
    click.echo("Akosha Storage Paths:")
    click.echo(f"  Environment: {resolver.env}")
    click.echo(f"  Base path: {resolver.base_path}")
    from_path = Path.cwd() / "data"
    if from_path.exists() and any(from_path.iterdir()):
        click.echo(f"  Project-local data: present at {from_path}")
    else:
        click.echo("  Project-local data: none")


@migrate.command()
def version() -> None:
    """Show version information."""
    from akosha import __version__

    click.echo(f"Akosha migration tools v{__version__}")
