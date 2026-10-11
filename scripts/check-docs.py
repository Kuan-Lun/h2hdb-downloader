#!/usr/bin/env python3
"""Run the committed h2h-dev-tools documentation entry point."""

import runpy

if __name__ == "__main__":
    runpy.run_module("_shared.h2h_dev_tools.documentation", run_name="__main__")
