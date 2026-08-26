#!/usr/bin/env python3
"""Merge the codesearch SessionStart hook and permission into an existing
.claude/settings.json without disturbing anything already there.

Writes a .bak only when something actually changes, so re-running the
installer cannot overwrite a real backup with already-merged content.
"""
import json
import shutil
import sys


def main(target_path: str, template_path: str) -> None:
    with open(target_path) as fh:
        cfg = json.load(fh)
    with open(template_path) as fh:
        tpl = json.load(fh)

    changed = False
    hook_block = tpl["hooks"]["SessionStart"][0]
    hooks = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])

    present = any("refresh_index.sh" in h.get("command", "")
                  for block in hooks for h in block.get("hooks", []))
    if present:
        print("  SessionStart hook already present, left alone")
    else:
        hooks.append(hook_block)
        print("  merged SessionStart hook")
        changed = True

    perm = tpl["permissions"]["allow"][0]
    allow = cfg.setdefault("permissions", {}).setdefault("allow", [])
    if perm in allow:
        print("  permission already present, left alone")
    else:
        allow.append(perm)
        print("  merged codesearch permission")
        changed = True

    if not changed:
        print("  settings.json unchanged")
        return

    shutil.copyfile(target_path, target_path + ".bak")
    with open(target_path, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    print("  previous settings backed up to .claude/settings.json.bak")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
