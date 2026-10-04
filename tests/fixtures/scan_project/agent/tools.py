from langchain_core.tools import tool
from agents import function_tool
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("x")


@tool
def search_orders(customer_id: str) -> list:
    """Find a customer's orders."""


@function_tool
def issue_refund(order_id: str, amount: float) -> dict:
    """Refund an order."""


@mcp.tool()
def send_slack_message(channel: str, text: str) -> None:
    """Post a message."""


@tool("update_ticket")
def _upd(ticket_id: str, status: str): ...


def delete_file(path: str):
    """Permanently delete a file."""


def helper(): ...


from google.adk.tools import FunctionTool  # noqa: E402
file_tool = FunctionTool(delete_file)

TOOLS = [
    {"name": "spawn_task", "description": "Delegate work to a background agent.",
     "input_schema": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}},
    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}},
]
