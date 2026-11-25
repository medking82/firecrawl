#!/usr/bin/env python3
"""Embed search_runtime_contract.py into firecrawl_update.sh.

The updater is installed as one standalone file, so the reviewed contract is
copied into its heredocs (full module) and two exec() strings (without the live
transport check). Run with --check to fail when the embedded copies drift.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'search_runtime_contract.py'
TARGET = HERE / 'firecrawl_update.sh'
BEGIN = '# BEGIN SEARCH CONTRACT (generated from search_runtime_contract.py)\n'
END = '# END SEARCH CONTRACT'
BLOCK = re.compile(re.escape(BEGIN) + r'.*?(?=' + re.escape(END) + ')', re.S)
EXEC = re.compile(r"exec\('\"\"\"Frozen reviewed search runtime;(?:[^'\\]|\\.)*'")


def quoted(text):
    if '\\' in text:
        raise SystemExit('contract source must not contain backslashes')
    return "'" + text.replace("'", "\\'").replace('\n', '\\n') + "'"


def render(script, source):
    embedded = source.split('\ndef verify_live_transport', 1)[0].rstrip('\n') + '\n'
    script, blocks = BLOCK.subn(lambda _: BEGIN + source, script)
    script, execs = EXEC.subn(lambda _: 'exec(' + quoted(embedded), script)
    if (blocks, execs) != (3, 2):
        raise SystemExit(f'unexpected contract sites: {blocks} blocks, {execs} exec strings')
    return script


def main():
    source = SOURCE.read_text()
    current = TARGET.read_text()
    rendered = render(current, source)
    if '--check' in sys.argv[1:]:
        if rendered != current:
            raise SystemExit('firecrawl_update.sh search contract is stale; run render_search_contract.py')
        return
    TARGET.write_text(rendered)


if __name__ == '__main__':
    main()
