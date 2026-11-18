"""Make `import store`, `import main`, `import decision_flow` work from any cwd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
