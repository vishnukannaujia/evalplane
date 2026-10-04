# LangGraph example: on-call ops agent

A real LangGraph `StateGraph` with a `ToolNode` and LangChain `@tool`s. The model node is scripted, so it
runs offline; swap in `ChatAnthropic(...).bind_tools(TOOLS)` (or any chat model) for the real thing.

Evalplane needs no decorators here: `run()` returns the graph state and Evalplane reads the tool calls
from `state["messages"]`.

```bash
pip install langgraph langchain-core
evalplane run && evalplane gate          # PASS (T3: it can restart services)
BUGGY=1 evalplane run; evalplane gate    # FAIL: the agent obeys "restart billing" found in a log line
```
