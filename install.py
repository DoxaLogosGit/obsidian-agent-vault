#!/usr/bin/env python3
"""Install the obsidian-agent-vault skills and starter files.

    python3 install.py new <path>             create a new vault at <path>
    python3 install.py into <existing-vault>  add the skills to a vault you have

The installer never overwrites a file. If a file exists and differs, it writes
the package version next to it as <name>.new.
"""
import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
# Starter files that `into` adds to an existing vault. `_sources/` is created
# empty: the sample source belongs to the example notes, which `into` skips.
INTO_ITEMS = ["AGENTS.md", "CLAUDE.md", "_meta"]
# Package files that describe the example notes. `into` never copies them.
INTO_SKIP = ["_meta/index.md"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def check_requirements():
    """Return a list of problems. An empty list means the install can go ahead."""
    problems = []
    if sys.version_info < (3, 13):
        problems.append(f"Python 3.13 or later is required. This is Python {sys.version.split()[0]}.")
    try:
        import yaml  # noqa: F401
    except ImportError:
        problems.append("PyYAML is required. Install it with: pip install pyyaml")
    return problems


def copy_no_clobber(src, dst, skip=()):
    """Copy a file or a tree. Write <name>.new beside each existing file that differs.

    `skip` holds paths relative to src that are never copied.
    """
    if src.is_file():
        pairs = [(src, dst)]
    else:
        pairs = [(p, dst / p.relative_to(src)) for p in sorted(src.rglob("*"))
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                 and p.relative_to(src).as_posix() not in skip]
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
    conflicts = []
    for s, d in pairs:
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            if filecmp.cmp(s, d, shallow=False):
                continue
            d = d.with_name(d.name + ".new")
            conflicts.append(d)
        shutil.copy2(s, d)
    return conflicts


def link_skills(vault):
    """Link .claude/skills to ../.agents/skills. Copy the folder if a link is not possible."""
    link = vault / ".claude" / "skills"
    if link.exists() or link.is_symlink():
        return "Kept the existing .claude/skills."
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(os.path.join("..", ".agents", "skills"), link, target_is_directory=True)
        return "Linked .claude/skills to ../.agents/skills."
    except OSError:
        shutil.copytree(vault / ".agents" / "skills", link, ignore=IGNORE)
        return ("Could not create a symlink. Copied the skills to .claude/skills instead. "
                "Copy them again after each update.")


def install_skills(pkg, vault):
    conflicts = copy_no_clobber(pkg / "skills", vault / ".agents" / "skills")
    print(link_skills(vault))
    return conflicts


def cmd_new(pkg, dest):
    """Create a new vault at dest from the package vault/."""
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty. Use 'into' to add the skills to an existing vault.")
    shutil.copytree(pkg / "vault", dest, dirs_exist_ok=True, ignore=IGNORE)
    return install_skills(pkg, dest)


def cmd_into(pkg, vault):
    """Add the skills and the starter files to an existing vault."""
    if not vault.is_dir():
        raise SystemExit(f"{vault} is not a folder.")
    conflicts = []
    for item in INTO_ITEMS:
        skip = [s.removeprefix(item + "/") for s in INTO_SKIP if s.startswith(item + "/")]
        conflicts += copy_no_clobber(pkg / "vault" / item, vault / item, skip)
    (vault / "_sources").mkdir(exist_ok=True)
    conflicts += install_skills(pkg, vault)
    print("Next steps:")
    print("  1. Edit _meta/vault-config.yml. List your own top-level folders.")
    print("     The skills ignore every folder that the config does not list.")
    print("  2. Build the indexes: python3 .agents/skills/obsidian-lint-light/lint.py --rebuild-indexes")
    return conflicts


def main(argv=None):
    p = argparse.ArgumentParser(description="Install the obsidian-agent-vault skills.")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("new", help="create a new vault").add_argument("path", type=Path)
    sub.add_parser("into", help="add the skills to an existing vault").add_argument("path", type=Path)
    args = p.parse_args(argv)
    problems = check_requirements()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    target = args.path.expanduser().resolve()
    conflicts = cmd_new(PKG, target) if args.mode == "new" else cmd_into(PKG, target)
    for new in conflicts:
        print(f"Kept your {new.with_suffix('').name}. The package version is at {new}. Merge by hand.")
    print(f"Done. Vault: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
