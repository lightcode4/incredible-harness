"""Built-in model providers: address, key page, starting model and logo. Users only paste an API key.

kind: "claude" uses the Anthropic SDK; "openai" any OpenAI-compatible API; "local" a server on this computer.
Models listed here are starting points; "Load models" fetches what each account can actually use.
"""

PROVIDERS = [
    {"id": "claude", "name": "Claude", "by": "by Anthropic", "kind": "claude", "base": "", "logo": "claude", "color": "D97757",
     "model": "claude-opus-5-5", "keys": "https://console.anthropic.com/settings/keys",
     "blurb": "Thoughtful reasoning, powered by the official Anthropic SDK."},
    {"id": "openai", "name": "OpenAI", "by": "Official API", "kind": "openai", "base": "", "letter": "AI", "color": "10A37F",
     "model": "gpt-5", "keys": "https://platform.openai.com/api-keys",
     "blurb": "OpenAI models with native function calling and streaming."},
    {"id": "opencode", "name": "OpenCode Zen", "by": "opencode.ai", "kind": "openai", "base": "https://opencode.ai/inference/openai/v1",
     "logo": "opencode", "color": "211E1E", "model": "kimi-k3", "keys": "https://opencode.ai/console",
     "blurb": "Pay-as-you-go access to Claude, GPT, Kimi, GLM and more."},
    {"id": "opencode-go", "name": "OpenCode Go", "by": "opencode.ai subscription", "kind": "openai",
     "base": "https://opencode.ai/inference/go/openai/v1", "logo": "opencode", "color": "211E1E", "model": "kimi-k3",
     "keys": "https://opencode.ai/console", "blurb": "Your OpenCode Go subscription."},
    {"id": "openrouter", "name": "OpenRouter", "by": "openrouter.ai", "kind": "openai", "base": "https://openrouter.ai/api/v1",
     "logo": "openrouter", "color": "6467F2", "model": "openai/gpt-5", "keys": "https://openrouter.ai/keys",
     "blurb": "Hundreds of models from every lab behind one key."},
    {"id": "deepseek", "name": "DeepSeek", "by": "deepseek.com", "kind": "openai", "base": "https://api.deepseek.com/v1",
     "logo": "deepseek", "color": "5786FE", "model": "deepseek-chat", "keys": "https://platform.deepseek.com/api_keys",
     "blurb": "Strong, low-cost reasoning and coding models."},
    {"id": "xai", "name": "Grok", "by": "by xAI", "kind": "openai", "base": "https://api.x.ai/v1", "logo": "x", "color": "111111",
     "model": "grok-4", "keys": "https://console.x.ai", "blurb": "xAI's Grok models."},
    {"id": "gemini", "name": "Gemini", "by": "by Google", "kind": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "logo": "googlegemini", "color": "8E75B2", "model": "gemini-2.5-pro", "keys": "https://aistudio.google.com/apikey",
     "blurb": "Google's Gemini models through their OpenAI-compatible API."},
    {"id": "mistral", "name": "Mistral", "by": "mistral.ai", "kind": "openai", "base": "https://api.mistral.ai/v1", "logo": "mistralai",
     "color": "FA520F", "model": "mistral-large-latest", "keys": "https://console.mistral.ai/api-keys",
     "blurb": "Mistral Large, Codestral and friends."},
    {"id": "groq", "name": "Groq", "by": "groq.com", "kind": "openai", "base": "https://api.groq.com/openai/v1", "letter": "G",
     "color": "F55036", "model": "llama-3.3-70b-versatile", "keys": "https://console.groq.com/keys",
     "blurb": "Very fast open models."},
    {"id": "kimi", "name": "Kimi", "by": "by Moonshot AI", "kind": "openai", "base": "https://api.moonshot.ai/v1", "logo": "kimi",
     "color": "111111", "model": "kimi-k2-turbo-preview", "keys": "https://platform.moonshot.ai/console/api-keys",
     "blurb": "Moonshot's Kimi models."},
    {"id": "qwen", "name": "Qwen", "by": "by Alibaba Cloud", "kind": "openai", "base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
     "logo": "qwen", "color": "6950EF", "model": "qwen-max", "keys": "https://bailian.console.alibabacloud.com/?apiKey=1",
     "blurb": "Qwen models via DashScope."},
    {"id": "perplexity", "name": "Perplexity", "by": "perplexity.ai", "kind": "openai", "base": "https://api.perplexity.ai",
     "logo": "perplexity", "color": "1FB8CD", "model": "sonar-pro", "keys": "https://www.perplexity.ai/settings/api",
     "blurb": "Answers grounded in live web search."},
    {"id": "together", "name": "Together AI", "by": "together.ai", "kind": "openai", "base": "https://api.together.xyz/v1",
     "letter": "T", "color": "0F6FFF", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "keys": "https://api.together.ai/settings/api-keys",
     "blurb": "Open models at scale."},
    {"id": "fireworks", "name": "Fireworks", "by": "fireworks.ai", "kind": "openai", "base": "https://api.fireworks.ai/inference/v1",
     "letter": "F", "color": "6720FF", "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
     "keys": "https://fireworks.ai/account/api-keys", "blurb": "Fast inference for open models."},
    {"id": "cerebras", "name": "Cerebras", "by": "cerebras.ai", "kind": "openai", "base": "https://api.cerebras.ai/v1", "letter": "C",
     "color": "F15A29", "model": "llama-3.3-70b", "keys": "https://cloud.cerebras.ai", "blurb": "Extremely fast open models."},
    {"id": "huggingface", "name": "Hugging Face", "by": "Inference Providers", "kind": "openai", "base": "https://router.huggingface.co/v1",
     "logo": "huggingface", "color": "FFB000", "model": "openai/gpt-oss-120b", "keys": "https://huggingface.co/settings/tokens",
     "blurb": "Open models from the Hugging Face hub."},
    {"id": "haimaker", "name": "haimaker", "by": "haimaker.ai", "kind": "openai", "base": "https://api.haimaker.ai/v1", "letter": "h",
     "color": "2D6A4F", "model": "openai/gpt-5", "keys": "https://app.haimaker.ai", "blurb": "Many providers behind one key."},
    {"id": "ollama", "name": "Ollama", "by": "On this computer", "kind": "local", "base": "http://localhost:11434/v1", "logo": "ollama",
     "color": "111111", "model": "llama3.1", "keys": "https://ollama.com/download", "nokey": True,
     "blurb": "Your model, on your machine. No API key."},
    {"id": "lmstudio", "name": "LM Studio", "by": "On this computer", "kind": "local", "base": "http://localhost:1234/v1", "logo": "lmstudio",
     "color": "111111", "model": "", "keys": "https://lmstudio.ai", "nokey": True, "blurb": "Run open models locally with LM Studio."},
    {"id": "custom", "name": "Compatible", "by": "Any OpenAI-compatible API", "kind": "openai", "base": "", "letter": "+", "color": "7A6A9E",
     "model": "", "keys": "", "custom": True, "blurb": "Connect vLLM, your own endpoint or any other compatible service."},
]
BY_ID = {p["id"]: p for p in PROVIDERS}


def guess_service(provider: str, base_url: str) -> str:
    """Which catalog entry matches an existing provider + address (for older settings)."""
    if provider == "claude":
        return "claude"
    b = (base_url or "").rstrip("/")
    for p in PROVIDERS:
        if p["base"] and p["base"].rstrip("/") == b:
            return p["id"]
    if provider == "local":
        return "lmstudio" if ":1234" in b else "ollama"  # a local server keeps its own address
    return "openai" if not b else "custom"
