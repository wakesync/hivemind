#!/usr/bin/env python3
"""Pretty-print a hivemind /recall response from stdin (demo helper)."""
import json
import sys
import textwrap

B, D, G, R, X = "\033[1m", "\033[2m", "\033[38;5;154m", "\033[38;5;203m", "\033[0m"
r = json.load(sys.stdin)
a = r.get("access", {})
print(f"  {B}answer{X} {D}({r.get('mode')}){X}")
for line in textwrap.wrap(r.get("answer", ""), 88):
    print(f"    {G}{line}{X}")
print(f"  {D}access: personal={a.get('personal_memories', 0)}  "
      f"member_orgs={a.get('member_orgs', 0)} ({a.get('member_memories', 0)} mem)  "
      f"derived_orgs={a.get('derived_orgs', 0)} ({a.get('derived_memories', 0)} mem)  "
      f"signal_orgs={a.get('signal_orgs', 0)}{X}")
for h in r.get("hints", []):
    print(f"  {R}⚑ signal:{X} {h['message']}")
if not r.get("hints"):
    print(f"  {D}hints: none{X}")
