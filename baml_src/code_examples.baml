// BAML Function for Code Example Generation
// Generates code snippets for tools that don't have static templates

class ToolInfo {
  tool_id string
  tool_name string
  description string
  args ToolArg[]
}

class ToolArg {
  name string
  type string
  required bool
  description string
}

class CodeSnippet {
  snippet string @description("Python code snippet, max 4 lines, showing practical usage")
  pattern string @description("Code pattern used: loop, filter, if/else, try/catch, or poll")
}

function GenerateCodeSnippet(tool: ToolInfo) -> CodeSnippet {
  client Groq
  prompt #"
    Generate a minimal, practical Python code snippet (MAX 4 lines) for this MCP tool.

    Tool: {{ tool.tool_id }}
    Description: {{ tool.description }}
    Arguments:
    {% for arg in tool.args %}
    - {{ arg.name }} ({{ arg.type }}{% if arg.required %}, required{% endif %}): {{ arg.description }}
    {% endfor %}

    REQUIREMENTS:
    1. Show invocation via mcp.call_tool("gateway.invoke", ...)
    2. Use realistic argument values
    3. MAX 4 lines of code
    4. Include one of these patterns: loop, filter, if/else, try/catch, poll
    5. Add a brief comment explaining the pattern

    EXAMPLES:
    # Loop pattern
    for item in items:
        mcp.call_tool("gateway.invoke", {"tool_id": "server::tool", "arguments": {"param": item}})

    # Filter pattern
    results = mcp.call_tool("gateway.invoke", {"tool_id": "server::tool", "arguments": {}})
    filtered = [r for r in results if r["matches"]]

    # Try/catch pattern
    try:
        result = mcp.call_tool("gateway.invoke", {"tool_id": "server::tool", "arguments": {}})
    except Exception as e: print(f"Error: {e}")

    Generate a snippet following one of these patterns that makes sense for this tool.

    {{ ctx.output_format }}
  "#
}

test TestCodeSnippet {
  functions [GenerateCodeSnippet]
  args {
    tool {
      tool_id "playwright::browser_navigate"
      tool_name "browser_navigate"
      description "Navigate browser to a URL"
      args [
        {
          name "url"
          type "string"
          required true
          description "URL to navigate to"
        }
      ]
    }
  }
}
