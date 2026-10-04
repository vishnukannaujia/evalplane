import evalplane as ep


def test_openai_chat_messages():
    msgs = [
        {"role": "user", "content": "refund A1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup_order", "arguments": '{"order_id": "A1"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"total": 20}'},
        {"role": "assistant", "content": "Refunded."},
    ]
    run = ep.from_messages(msgs)
    assert run.tool_names() == ["lookup_order"]
    assert run.tool_calls()[0].args == {"order_id": "A1"}
    assert run.tool_calls()[0].result == '{"total": 20}'
    assert run.output == "Refunded." and run.input == "refund A1"


def test_anthropic_blocks():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "checking"},
                                          {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    run = ep.from_messages(msgs)
    tc = run.tool_calls()[0]
    assert (tc.name, tc.args, tc.status) == ("search", {"q": "x"}, "error")
    assert run.output == "done"


def test_openai_responses_items():
    items = [
        {"type": "function_call", "name": "get_weather", "arguments": '{"city": "Paris"}', "call_id": "x"},
        {"type": "function_call_output", "call_id": "x", "output": "sunny"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It is sunny."}]},
    ]
    run = ep.from_messages(items)
    assert run.tool_calls()[0].result == "sunny"
    assert run.output == "It is sunny."


def test_langchain_like_objects():
    class AIMessage:
        def __init__(self, content, tool_calls=None):
            self.content, self.tool_calls = content, tool_calls or []

    class ToolMessage:
        def __init__(self, content, tool_call_id):
            self.content, self.tool_call_id = content, tool_call_id

    msgs = [AIMessage("", [{"name": "lookup", "args": {"id": 1}, "id": "a"}]), ToolMessage("ok", "a"), AIMessage("fine")]
    run = ep.from_messages(msgs)
    assert run.tool_names() == ["lookup"] and run.tool_calls()[0].result == "ok" and run.output == "fine"
