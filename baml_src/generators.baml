// BAML Generator Configuration
// Generates Python client code for PMCP

generator target {
  output_type python/pydantic
  output_dir "../src/pmcp"
  version "0.215.0"
  default_client_mode async
}
