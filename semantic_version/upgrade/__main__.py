# -*- coding: utf-8 -*-
"""``python -m semantic_version.upgrade`` entry point."""

import sys

from .._upgrade import main

sys.exit(main())
