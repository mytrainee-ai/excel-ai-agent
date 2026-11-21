"""Преобразование схем MCP в function tools OpenAI Responses API."""

from copy import deepcopy

from mcp.types import Tool
from openai.types.responses import FunctionToolParam


def to_function_tool(tool: Tool) -> FunctionToolParam:
    schema = deepcopy(tool.input_schema)
    properties = schema.get("properties")
    required = schema.get("required", [])
    if schema.get("type") != "object" or not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(f"Инструмент {tool.name} должен описывать объект аргументов.")

    managed = {"workbook_path", "sheet_name"}
    schema["properties"] = {name: value for name, value in properties.items() if name not in managed}
    schema["required"] = [name for name in required if name not in managed]
    schema["additionalProperties"] = False

    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description or "",
        "parameters": schema,
        "strict": False,
    }
