import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # Project root, so this file can be run directly

from sql_graph.draw_png import draw_graph
from sql_graph.askthedb_graph import make_graph

async def execute_graph():
    """Run the workflow as a command-line chat; type q, exit or quit to stop."""
    async with make_graph() as graph:
        draw_graph(graph, 'askthedb.png')
        while True:
            user_input = input("You: ")
            if user_input.lower() in ['q', 'exit', 'quit']:
                print('Conversation ended. Goodbye!')
                break
            else:
                async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]},
                                                 stream_mode="values"):
                    event["messages"][-1].pretty_print()


if __name__ == '__main__':
    asyncio.run(execute_graph())
