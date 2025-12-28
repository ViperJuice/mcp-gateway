// BAML Function Definitions for Capability Summarization
// Generates concise summaries of MCP tool capabilities

class ToolDescription {
  server_name string
  tool_name string
  description string
}

class CapabilityCategory {
  name string @description("Category name like 'Browser Automation' or 'File Operations'")
  server string @description("Server name providing this capability")
  summary string @description("3-5 word capability summary")
}

class CapabilitySummary {
  categories CapabilityCategory[] @description("3-5 high-level capability categories")
  usage_hint string @description("Hint for how to explore tools further")
}

function SummarizeCapabilities(tools: ToolDescription[]) -> CapabilitySummary {
  client Groq
  prompt #"
    Categorize these MCP server tools into 3-5 high-level capability categories.
    Group by what the user can DO, not by individual tool names.

    Tools available:
    {% for tool in tools %}
    - {{ tool.server_name }}::{{ tool.tool_name }}: {{ tool.description }}
    {% endfor %}

    Create categories that describe capabilities from the user's perspective.
    Each category should reference the server that provides it.
    Keep summaries concise (3-5 words).

    The usage_hint should always be: "Use gateway.catalog_search to explore tools."

    {{ ctx.output_format }}
  "#
}

test TestName {
  functions [SummarizeCapabilities]
  args {
    tools [
      {
          server_name #"
          hello world
        "#
          tool_name #"
          hello world
        "#
          description #"
          hello world
        "#
      },
      {
          server_name #"
          hello world
        "#
          tool_name #"
          hello world
        "#
          description #"
          hello world
        "#
      }
    ]
  }
}

