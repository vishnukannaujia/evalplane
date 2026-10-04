"""A read-only FAQ agent that returns its message history in OpenAI chat format.

No Evalplane imports, no decorators: Evalplane reads the tool calls from the messages.
This is how you'd plug in an agent built with LangGraph (`state["messages"]`), the OpenAI
Agents SDK (`result.to_input_list()`) or the Anthropic SDK (the `messages` list).
The "model" is scripted so the example runs offline.
"""

import json
import os

DOCS = {
    "returns": "You can return items within 30 days for a full refund.",
    "shipping": "Standard shipping takes 3-5 business days; express takes 1-2.",
    "warranty": "Electronics carry a 1-year limited warranty.",
}


def search_docs(query: str) -> list[dict]:
    q = query.lower()
    return [{"id": k, "text": v} for k, v in DOCS.items() if k.rstrip("s") in q or k in q]


def run(user_input: str, context: dict) -> list[dict]:
    messages = [{"role": "user", "content": user_input}]
    if os.environ.get("BUGGY") == "1":
        # bug: answers from "memory" without searching, and makes up a policy
        messages.append({"role": "assistant", "content": "Returns are accepted within 90 days."})
        return messages
    query = user_input
    call = {"id": "call_1", "type": "function", "function": {"name": "search_docs",
                                                              "arguments": json.dumps({"query": query})}}
    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
    hits = search_docs(query)
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": json.dumps(hits)})
    answer = hits[0]["text"] if hits else "I don't know; I couldn't find that in our help center."
    messages.append({"role": "assistant", "content": answer})
    return messages
