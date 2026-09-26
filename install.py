#!/usr/bin/env python3
"""Install the obsidian-agent-vault skills and starter files.

    python3 install.py new <path>                   create a new vault at <path>
    python3 install.py into <existing-vault> [--yes] add the skills to a vault you have
    python3 install.py update <vault>                refresh the skills after a git pull

The installer never overwrites a note or a config file. The package rules in
AGENTS.md live between two marker comments, so `into` can append them to your
own AGENTS.md and `update` can refresh them. Put your own rules outside the
markers. Before it changes AGENTS.md or CLAUDE.md, it saves <name>.bak.
"""
import argparse
import filecmp
import json
import os
import re
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
START = "<!-- obsidian-agent-vault:start -->"
END = "<!-- obsidian-agent-vault:end -->"
# Package files that describe the example notes. `into` never copies them.
# vault-config.yml is written by setup_config instead.
INTO_SKIP = ["index.md", "vault-config.yml"]
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


def replace_tree(src, dst):
    if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)


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
                "Run 'install.py update' after each git pull.")


def install_skills(pkg, vault):
    conflicts = copy_no_clobber(pkg / "skills", vault / ".agents" / "skills")
    print(link_skills(vault))
    return conflicts


# ----- AGENTS.md and CLAUDE.md ------------------------------------------------

def rules_block(pkg):
    return f"{START}\n{(pkg / 'vault' / 'AGENTS.md').read_text(encoding='utf-8').rstrip()}\n{END}\n"


def backup(path):
    shutil.copy2(path, path.with_name(path.name + ".bak"))


def merge_agents_md(pkg, vault):
    """Put the package rules into AGENTS.md between the markers. Return a message or None."""
    path, block = vault / "AGENTS.md", rules_block(pkg)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return None
    text = path.read_text(encoding="utf-8")
    if START in text and END in text:
        head, rest = text.split(START, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        new = head + block + tail
        if new == text:
            return None
        backup(path)
        path.write_text(new, encoding="utf-8")
        return "Refreshed the package rules in AGENTS.md. The old file is AGENTS.md.bak."
    backup(path)
    path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    return "Appended the package rules to your AGENTS.md. The old file is AGENTS.md.bak."


def merge_claude_md(pkg, vault):
    """Make CLAUDE.md import AGENTS.md. Return a message or None."""
    path = vault / "CLAUDE.md"
    if not path.exists():
        shutil.copy2(pkg / "vault" / "CLAUDE.md", path)
        return None
    text = path.read_text(encoding="utf-8")
    if "@AGENTS.md" in text:
        return None
    backup(path)
    path.write_text(text.rstrip("\n") + "\n\n@AGENTS.md\n", encoding="utf-8")
    return "Added '@AGENTS.md' to your CLAUDE.md. The old file is CLAUDE.md.bak."


# ----- folder config ----------------------------------------------------------

def detect_note_folders(vault):
    """Top-level folders that hold at least one .md note, skipping _ and . folders."""
    found = []
    for d in sorted(vault.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        if next(d.rglob("*.md"), None):
            found.append(d.name)
    return found


def yaml_list(names):
    items = [n if re.fullmatch(r"[\w][\w .-]*", n) else json.dumps(n) for n in names]
    return "[" + ", ".join(items) + "]"


def ask(question):
    try:
        return input(question).strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def setup_config(pkg, vault, confirm=None):
    """Write _meta/vault-config.yml from the vault's own folders. Return a message."""
    path = vault / "_meta" / "vault-config.yml"
    if path.exists():
        return "Kept your existing _meta/vault-config.yml."
    template = (pkg / "vault" / "_meta" / "vault-config.yml").read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    folders = detect_note_folders(vault)
    if folders:
        print("These top-level folders hold notes:")
        for f in folders:
            print(f"  - {f}")
        if confirm is None:
            confirm = ask("Use these as your note folders? [Y/n] ")
    if folders and confirm:
        listed = yaml_list(folders)
        for key in ("writable_roots", "audited_roots"):
            template = re.sub(rf"(?m)^{key}:.*$", f"{key}: {listed}", template)
        path.write_text(template, encoding="utf-8")
        return "Wrote your folders to _meta/vault-config.yml."
    path.write_text(template, encoding="utf-8")
    return "Wrote the default _meta/vault-config.yml. Edit it to list your note folders."


# ----- commands ---------------------------------------------------------------

def cmd_new(pkg, dest):
    """Create a new vault at dest from the package vault/."""
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty. Use 'into' to add the skills to an existing vault.")
    shutil.copytree(pkg / "vault", dest, dirs_exist_ok=True, ignore=IGNORE)
    (dest / "AGENTS.md").write_text(rules_block(pkg), encoding="utf-8")
    return install_skills(pkg, dest)


def cmd_into(pkg, vault, confirm=None):
    """Add the skills and the starter files to an existing vault."""
    if not vault.is_dir():
        raise SystemExit(f"{vault} is not a folder.")
    messages = [setup_config(pkg, vault, confirm), merge_agents_md(pkg, vault),
                merge_claude_md(pkg, vault)]
    conflicts = copy_no_clobber(pkg / "vault" / "_meta", vault / "_meta", INTO_SKIP)
    (vault / "_sources").mkdir(exist_ok=True)
    conflicts += install_skills(pkg, vault)
    for m in filter(None, messages):
        print(m)
    print("Next: open the vault with your agent and run /obsidian-onboard.")
    return conflicts


def cmd_update(pkg, vault):
    """Replace the package skills and the package rules. Leave notes and config alone."""
    skills = vault / ".agents" / "skills"
    if not skills.is_dir():
        raise SystemExit(f"No skills installed in {vault}. Use 'into' first.")
    names = sorted(p.name for p in (pkg / "skills").iterdir() if p.is_dir() and p.name != "__pycache__")
    for name in names:
        replace_tree(pkg / "skills" / name, skills / name)
    copied = vault / ".claude" / "skills"
    if copied.is_dir() and not copied.is_symlink():
        for name in names:
            replace_tree(pkg / "skills" / name, copied / name)
    elif not copied.exists():
        print(link_skills(vault))
    print(f"Updated {len(names)} skills: {', '.join(names)}")
    message = merge_agents_md(pkg, vault)
    if message:
        print(message)
    return copy_no_clobber(pkg / "vault" / "_meta" / "schema.md", vault / "_meta" / "schema.md")


def main(argv=None):
    p = argparse.ArgumentParser(description="Install the obsidian-agent-vault skills.")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("new", help="create a new vault").add_argument("path", type=Path)
    into = sub.add_parser("into", help="add the skills to an existing vault")
    into.add_argument("path", type=Path)
    into.add_argument("--yes", action="store_true", help="use the detected folders without asking")
    sub.add_parser("update", help="refresh the skills and rules after a git pull").add_argument(
        "path", type=Path)
    args = p.parse_args(argv)
    problems = check_requirements()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    target = args.path.expanduser().resolve()
    if args.mode == "new":
        conflicts = cmd_new(PKG, target)
    elif args.mode == "into":
        conflicts = cmd_into(PKG, target, True if args.yes else None)
    else:
        conflicts = cmd_update(PKG, target)
    for new in conflicts:
        print(f"Kept your {new.with_suffix('').name}. The package version is at {new}. Merge by hand.")
    print(f"Done. Vault: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
