from langchain_openai import ChatOpenAI
from openai import OpenAI

from sql_graph.env_utils import QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL

if not QWEN_API_KEY or QWEN_API_KEY == 'your-api-key-here':
    raise RuntimeError('QWEN_API_KEY is not set: add your Alibaba Cloud Model Studio (DashScope) API key '
                       'to .env (or .env.local, which Git ignores) in the project root')

# Native OpenAI client, for direct calls such as web search
qwen_client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)

# LangChain chat model, used by the workflow
llm = ChatOpenAI(
    temperature=0,
    model=QWEN_MODEL,
    api_key=QWEN_API_KEY,
    base_url=QWEN_BASE_URL,
    # Qwen3 models require thinking mode to be off for non-streaming calls; no effect on models like qwen-plus
    extra_body={"enable_thinking": False},
)
