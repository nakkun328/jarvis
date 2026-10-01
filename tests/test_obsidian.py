"""Obsidian vault adapter tests without network or API credentials."""

from pathlib import Path
from uuid import uuid4

import pytest

from backend.memory.obsidian import (
    ObsidianVault,
    VaultConflictError,
    VaultError,
    VaultFormatError,
)


def test_create_read_and_update_after_human_body_edit(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    memory_id = uuid4()
    metadata = {"category": "project", "importance": 0.8, "tags": ["日本語", "alpha"]}

    created = vault.create(memory_id, "# Initial\n\nA fact.\n", metadata)
    assert created.path.name == f"{memory_id}.md"
    assert created.path.parent == vault.root
    assert created.path.read_text(encoding="utf-8").startswith("---\nid: ")
    assert vault.read(memory_id) == created

    # Obsidian can edit the Markdown body directly. The new revision protects it.
    created.path.write_text(
        created.path.read_text(encoding="utf-8").replace("A fact.", "Human edit."),
        encoding="utf-8",
    )
    edited = vault.read(memory_id)
    assert edited is not None
    assert edited.body == "# Initial\n\nHuman edit.\n"
    assert edited.metadata == metadata
    assert edited.revision != created.revision
    with pytest.raises(VaultConflictError):
        vault.update(memory_id, "Overwrite", metadata, expected_revision=created.revision)
    assert "Human edit." in created.path.read_text(encoding="utf-8")

    updated = vault.update(memory_id, "# Revised\n", metadata, expected_revision=edited.revision)
    assert vault.read(memory_id) == updated
    assert list(vault.root.glob(".jarvis-*.tmp")) == []


def test_create_does_not_overwrite_existing_note(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    memory_id = uuid4()
    original = vault.create(memory_id, "First", {"confidence": 1.0})
    with pytest.raises(FileExistsError):
        vault.create(memory_id, "Second", {})
    assert vault.read(memory_id) == original
    assert list(vault.root.glob(".jarvis-*.tmp")) == []


def test_rejects_unsafe_paths_and_symlinks(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    with pytest.raises(TypeError, match="UUID"):
        vault.create("../escape", "bad", {})  # type: ignore[arg-type]

    external = tmp_path / "external.md"
    external.write_text("secret", encoding="utf-8")
    vault.root.mkdir()
    memory_id = uuid4()
    try:
        (vault.root / f"{memory_id}.md").symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    with pytest.raises(VaultError, match="symlink"):
        vault.read(memory_id)
    with pytest.raises(FileExistsError):
        vault.create(memory_id, "bad", {})
    assert external.read_text(encoding="utf-8") == "secret"

    linked_root = tmp_path / "linked-vault"
    linked_root.symlink_to(vault.root, target_is_directory=True)
    with pytest.raises(VaultError, match="symlink"):
        ObsidianVault(linked_root).create(uuid4(), "bad", {})


def test_frontmatter_must_be_simple_and_identify_the_note(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    memory_id = uuid4()
    with pytest.raises(ValueError, match="reserved"):
        vault.create(memory_id, "bad", {"id": "override"})
    with pytest.raises(ValueError, match="Non-finite"):
        vault.create(memory_id, "bad", {"importance": float("nan")})
    assert not vault.root.exists()

    note = vault.create(memory_id, "Body", {"source": "user"})
    note.path.write_text("---\nid: \"wrong\"\n---\nBody", encoding="utf-8")
    with pytest.raises(VaultFormatError, match="Invalid frontmatter"):
        vault.read(memory_id)


def test_missing_note_and_missing_update(tmp_path: Path) -> None:
    vault = ObsidianVault(tmp_path / "vault")
    memory_id = uuid4()
    vault.root.mkdir()
    assert vault.read(memory_id) is None
    with pytest.raises(FileNotFoundError):
        vault.update(memory_id, "Body", {}, expected_revision="previous")
