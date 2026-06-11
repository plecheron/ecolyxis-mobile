import tiktoken

_encoding = None
WORKSPACE_CONTEXT_BUDGET = 16384  # tokens (~16k)

def get_encoding():
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding

def count_tokens(text):
    if not text:
        return 0
    return len(get_encoding().encode(text))
