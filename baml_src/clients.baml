// BAML Client Definitions
// Uses Groq for fast, cheap LLM inference

client<llm> Groq {
  provider openai-generic
  options {
    base_url "https://api.groq.com/openai/v1"
    api_key env.GROQ_API_KEY
    model "openai/gpt-oss-20b"
  }
}
