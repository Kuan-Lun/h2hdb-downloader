"""Install CI tools using this checkout's declared requirements."""

from pathlib import Path

from _shared.h2h_dev_tools.ci_dependencies import main

if __name__ == "__main__":
    main(Path(__file__).resolve().parents[1])
