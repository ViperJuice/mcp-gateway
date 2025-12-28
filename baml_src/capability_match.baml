// BAML Function for matching capability requests to manifest entries
// Returns ranked candidates for Claude Code to choose from

class ManifestServer {
  name string @description("Server identifier")
  description string @description("What this server provides")
  keywords string[] @description("Associated keywords")
  requires_api_key bool @description("Whether an API key is needed")
  env_var string? @description("Environment variable for API key if required")
}

class ManifestCLI {
  name string @description("CLI tool name")
  description string @description("What this CLI provides")
  keywords string[] @description("Associated keywords")
}

class ManifestSummary {
  servers ManifestServer[] @description("Available MCP servers")
  clis ManifestCLI[] @description("Available CLI tools")
}

class CapabilityCandidate {
  name string @description("Name of the server or CLI")
  candidate_type string @description("'cli' or 'server'")
  relevance_score float @description("0.0 to 1.0 relevance score")
  reasoning string @description("Brief explanation of why this matches (1-2 sentences)")
  requires_api_key bool @description("Whether API key is needed (always false for CLIs)")
}

class CapabilityMatchResult {
  candidates CapabilityCandidate[] @description("Top 1-3 candidates ranked by relevance")
  recommendation string @description("Brief recommendation for Claude Code on which to choose")
}

function MatchCapability(
  query: string,
  manifest: ManifestSummary,
  available_clis: string[],
  running_servers: string[]
) -> CapabilityMatchResult {
  client Groq
  prompt #"
    Match this capability request to the best options from the manifest.
    Return up to 3 candidates ranked by relevance.

    User's capability request: "{{ query }}"

    Already installed CLIs: {{ available_clis | join(", ") }}
    Already running servers: {{ running_servers | join(", ") }}

    Available CLI tools:
    {% for cli in manifest.clis %}
    - {{ cli.name }}: {{ cli.description }}
      Keywords: {{ cli.keywords | join(", ") }}
    {% endfor %}

    Available MCP servers:
    {% for server in manifest.servers %}
    - {{ server.name }}: {{ server.description }}
      Keywords: {{ server.keywords | join(", ") }}
      Requires API key: {{ server.requires_api_key }}{% if server.env_var %} ({{ server.env_var }}){% endif %}
    {% endfor %}

    Rules:
    1. Prefer CLIs that are already installed for simple tasks (lower overhead)
    2. Prefer servers that are already running (zero startup cost)
    3. Consider whether the task really needs MCP vs CLI capabilities
    4. For complex automation (browser, APIs), MCP servers are usually better
    5. If a server requires an API key, still include it but note the requirement
    6. Return 1-3 candidates - fewer if there's a clear winner
    7. If nothing matches well, return empty candidates array

    Scoring guide:
    - 0.9-1.0: Perfect match, exactly what user needs
    - 0.7-0.8: Good match, can handle the request well
    - 0.5-0.6: Partial match, might work with limitations
    - Below 0.5: Don't include

    {{ ctx.output_format }}
  "#
}

test TestCapabilityMatch {
  functions [MatchCapability]
  args {
    query "I need to search the web for information"
    manifest {
      servers [
        {
          name "brave-search"
          description "Web search API"
          keywords ["search", "web"]
          requires_api_key true
          env_var "BRAVE_API_KEY"
        }
      ]
      clis [
        {
          name "curl"
          description "HTTP client"
          keywords ["http", "request"]
        }
      ]
    }
    available_clis ["curl"]
    running_servers []
  }
}
