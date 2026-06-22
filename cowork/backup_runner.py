#!/usr/bin/env python3
"""
H2 Quant Backup Runner
Copies H2_QUANT_V1 to a timestamped folder under C:/Users/Admin/H2_Backups/
Also writes a git bundle and BACKUP_MANIFEST.json.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR  = Path("C:/Users/Admin/Desktop/H2_QUANT_V1")
BACKUP_ROOT  = Path("C:/Users/Admin/H2_Backups")

# Files/dirs to EXCLUDE from the copy (large, regenerable, or sensitive)
EXCLUDE = {
    ".git",
    "__pycache__",
    "data/raw",
    "data/processed",
    "logs",
    "outputs/briefs",       # keep outputs/ root but skip large brief archive
    "node_modules",
    "*.pyc",
}


def should_exclude(path: Path, project_root: Path) -> bool:
    rel = path.relative_to(project_root)
    parts = set(rel.parts)
    for ex in EXCLUDE:
        if ex.startswith("*"):
            if path.suffix == ex[1:]:
                return True
        else:
            ex_path = Path(ex)
            # Check if any leading portion of rel matches
            rel_str = rel.as_posix()
            if rel_str == ex or rel_str.startswith(ex + "/"):
                return True
            if ex in parts:
                return True
    return False


def copy_project(src: Path, dst: Path) -> int:
    """Copy project tree to dst, excluding EXCLUDE patterns. Returns file count."""
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in src.rglob("*"):
        if should_exclude(item, src):
            continue
        rel = item.relative_to(src)
        dest = dst / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            count += 1
    return count


def make_git_bundle(project_dir: Path, bundle_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "bundle", "create", str(bundle_path), "--all"],
            cwd=project_dir, capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[BACKUP] git bundle failed: {e}")
        return False


def get_git_info(project_dir: Path) -> dict:
    def git(args):
        try:
            r = subprocess.run(["git"] + args, cwd=project_dir,
                                capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""
    return {
        "branch":       git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "commit":       git(["rev-parse", "--short", "HEAD"]),
        "commit_msg":   git(["log", "-1", "--pretty=%s"]),
        "status_clean": git(["status", "--porcelain"]) == "",
    }


def folder_size_mb(folder: Path) -> float:
    total = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    return round(total / (1024 * 1024), 2)


def main():
    now     = datetime.now(timezone.utc)
    ts      = now.strftime("%Y-%m-%d_%H%M")
    dst_dir = BACKUP_ROOT / f"H2_Backup_{ts}"

    print(f"[BACKUP] Source:      {PROJECT_DIR}")
    print(f"[BACKUP] Destination: {dst_dir}")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    # 1. Copy project files
    print("[BACKUP] Copying project files...")
    file_count = copy_project(PROJECT_DIR, dst_dir)
    print(f"[BACKUP] Copied {file_count} files")

    # 2. Git bundle
    bundle_path = dst_dir / "h2_quant.bundle"
    print("[BACKUP] Creating git bundle...")
    bundle_ok = make_git_bundle(PROJECT_DIR, bundle_path)
    print(f"[BACKUP] Git bundle: {'OK' if bundle_ok else 'FAILED'}")

    # 3. Git info
    git_info = get_git_info(PROJECT_DIR)

    # 4. BACKUP_MANIFEST.json
    size_mb = folder_size_mb(dst_dir)
    manifest = {
        "backup_timestamp_utc": now.isoformat(),
        "backup_folder":        str(dst_dir),
        "source_folder":        str(PROJECT_DIR),
        "files_copied":         file_count,
        "git_bundle":           bundle_ok,
        "git":                  git_info,
        "size_mb":              size_mb,
        "excluded_patterns":    list(EXCLUDE),
    }
    manifest_path = dst_dir / "BACKUP_MANIFEST.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # 5. Prune: keep only 10 most recent backups
    backups = sorted(BACKUP_ROOT.glob("H2_Backup_*"), key=lambda p: p.stat().st_mtime)
    if len(backups) > 10:
        for old in backups[:-10]:
            shutil.rmtree(old, ignore_errors=True)
            print(f"[BACKUP] Pruned old backup: {old.name}")

    print(f"[BACKUP] ✓ Complete — {size_mb} MB in {dst_dir.name}")
    print(f"[BACKUP]   Files: {file_count}  |  Git bundle: {'OK' if bundle_ok else 'FAILED'}")
    print(f"[BACKUP]   Commit: {git_info.get('commit', '?')} — {git_info.get('commit_msg', '?')}")
    print(f"[BACKUP]   Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
