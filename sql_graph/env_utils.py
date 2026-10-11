import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env holds the shared settings (committed, placeholders only).
# .env.local holds your own values such as the real API key (Git-ignored) and overrides .env
load_dotenv(PROJECT_ROOT / '.env', override=True)
load_dotenv(PROJECT_ROOT / '.env.local', override=True)

# Alibaba Cloud Model Studio (DashScope) API key
QWEN_API_KEY = os.getenv('QWEN_API_KEY')

# OpenAI-compatible endpoint. Keys from the China (Beijing) and international (Singapore) sites
# are not interchangeable, so this must match the region the key belongs to
QWEN_BASE_URL = os.getenv('QWEN_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
QWEN_MODEL = os.getenv('QWEN_MODEL', 'qwen-plus')
