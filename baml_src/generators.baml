// BAML Generator Configuration
// Generates Python client code for MCP Gateway

generator target {
  output_type python/pydantic
  output_dir "../src/mcp_gateway"
  version "0.215.0"
  default_client_mode async
}
