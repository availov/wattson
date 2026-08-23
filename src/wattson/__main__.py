"""Entry point for ``python3 path/to/wattson`` and ``python3 -m wattson``."""

import os
import sys

if __package__ in (None, ''):
    # started as a directory script: put the parent of the package on sys.path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from wattson.cli import main
else:
    from .cli import main

if __name__ == '__main__':
    sys.exit(main())
