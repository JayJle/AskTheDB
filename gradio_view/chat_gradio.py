import sys
from pathlib import Path
from typing import List, Dict

import gradio as gr
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # Project root, so this file can be run directly

from sql_graph.askthedb_graph import make_graph


async def execute_graph(chat_bot: List[Dict]) -> List[Dict]:
    """Run the workflow on the latest user message and append the assistant's answer to the chat."""
    user_input = chat_bot[-1]['content']
    result = ''  # Latest message from the AI assistant

    async with make_graph() as graph:

        async for event in graph.astream({"messages": [{"role": "user", "content": user_input}]},
                                         stream_mode="values"):
            messages = event.get('messages')
            if messages:
                message = messages[-1]
                if isinstance(message, AIMessage) and message.content:
                    result = message.content  # Message to show in the web UI
                print(message.pretty_repr())  # Print each step to the console for debugging

    chat_bot.append({'role': 'assistant', 'content': result})
    return chat_bot


def do_graph(user_input, chat_bot):
    """Called when the input box is submitted: add the user's message to the chat and clear the box."""
    if user_input:
        chat_bot.append({'role': 'user', 'content': user_input})
    return '', chat_bot


with gr.Blocks(title='AskTheDB') as instance:
    gr.Label('AskTheDB · Ask your database questions.', container=False)

    chatbot = gr.Chatbot(type='messages', height=450, label='AI assistant')  # Chat history

    input_textbox = gr.Textbox(label='Feel free to ask any questions related to the database.', value='')  # Input box

    input_textbox.submit(do_graph, [input_textbox, chatbot], [input_textbox, chatbot]).then(execute_graph, chatbot,
                                                                                            chatbot)

if __name__ == '__main__':
    # Start the Gradio app
    instance.launch(debug=True)
