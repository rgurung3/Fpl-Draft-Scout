import sys
from pathlib import Path

# Lets the tests do "import app" from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
