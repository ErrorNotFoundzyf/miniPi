"""Run the regression harness from a source checkout."""

import sys
from pathlib import Path


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from my_pi_eval.regression import main

    raise SystemExit(main())
