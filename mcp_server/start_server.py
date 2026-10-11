import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.mcp_tools import mcp_server

if __name__ == '__main__':
    mcp_server.run(transport='sse')
