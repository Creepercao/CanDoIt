"""Small deterministic lexical retrieval primitives, without external models."""

import re


def tokenize(text):
    """Lowercase identifiers/numbers; Chinese characters plus adjacent bigrams."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    tokens = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", text):
        term = match[0].lower()
        if term.isascii():
            tokens.append(term)
        else:
            tokens.extend(term)
            tokens.extend(term[index:index + 2] for index in range(len(term) - 1))
    return tokens
