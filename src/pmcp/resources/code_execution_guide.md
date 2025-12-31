# Code Execution with PMCP

## Overview

PMCP (Progressive MCP) is designed to reduce context bloat and enable efficient workflows through **code execution patterns**. Instead of making many individual tool calls that pass results through your context window, write code to orchestrate tools.

## Why Code Execution?

### 1. Context Efficiency
**Problem**: Direct tool calls load all results into context.
```python
# ❌ Without code execution - all rows flow through context
result = mcp.call_tool("gateway.invoke", {
    "tool_id": "gdrive::getSheet",
    "arguments": {"sheetId": "abc123"}
})
# 10,000 rows now in context!
```

**Solution**: Filter and transform in your execution environment.
```python
# ✅ With code execution - filter before returning
sheet = mcp.call_tool("gateway.invoke", {
    "tool_id": "gdrive::getSheet",
    "arguments": {"sheetId": "abc123"}
})
pending = [row for row in sheet if row["Status"] == "pending"]
print(f"Found {len(pending)} pending items")  # Only summary in context
```

### 2. Batch Operations
**Problem**: Chaining individual tool calls is slow and verbose.

**Solution**: Use loops to process multiple items efficiently.
```python
# ✅ Batch process URLs
urls = ["https://example.com", "https://github.com", "https://anthropic.com"]
screenshots = []

for url in urls:
    mcp.call_tool("gateway.invoke", {
        "tool_id": "playwright::browser_navigate",
        "arguments": {"url": url}
    })
    result = mcp.call_tool("gateway.invoke", {
        "tool_id": "playwright::browser_screenshot",
        "arguments": {}
    })
    screenshots.append(result)

print(f"Captured {len(screenshots)} screenshots")
```

### 3. Control Flow
**Problem**: Conditionals and error handling via tool calls is awkward.

**Solution**: Use familiar code patterns.
```python
# ✅ Conditional logic in code
try:
    result = mcp.call_tool("gateway.invoke", {
        "tool_id": "github::getIssue",
        "arguments": {"issueId": "123"}
    })

    if result.get("state") == "closed":
        print("Issue already closed")
    else:
        # Take action
        mcp.call_tool("gateway.invoke", {
            "tool_id": "github::closeIssue",
            "arguments": {"issueId": "123"}
        })
except Exception as e:
    print(f"Error: {e}")
    # Fallback logic
```

### 4. Privacy & Security
**Problem**: Sensitive data flows through context window.

**Solution**: Process data in execution environment without exposing it.
```python
# ✅ Sensitive data stays in execution environment
customers = mcp.call_tool("gateway.invoke", {
    "tool_id": "gdrive::getSheet",
    "arguments": {"sheetId": "customer-data"}
})

# Process PII without loading into context
for customer in customers:
    mcp.call_tool("gateway.invoke", {
        "tool_id": "salesforce::updateLead",
        "arguments": {
            "leadId": customer["id"],
            "email": customer["email"],  # Never logged
            "phone": customer["phone"]   # Never logged
        }
    })

print(f"Updated {len(customers)} customer records")  # Only summary visible
```

## Progressive Disclosure Methodology

PMCP uses a 4-layer approach to minimize context consumption:

### Layer 0: MCP Instructions
When you connect to PMCP, you see a brief philosophical statement:
```
Write code to orchestrate tools - use loops, filters, conditionals.
Search → describe → invoke via code execution.
```

This reminds you of the recommended pattern.

### Layer 1: Search for Capabilities
Use `gateway.catalog_search` to find tools:
```python
result = mcp.call_tool("gateway.catalog_search", {
    "query": "browser automation"
})
```

Returns compact capability cards with **code hints**:
```json
{
  "tool_id": "playwright::browser_navigate",
  "short_description": "Navigate browser to URL",
  "code_hint": "loop"  // ← Suggests using in a loop
}
```

### Layer 2: Get Tool Details
Use `gateway.describe` to see full schema:
```python
result = mcp.call_tool("gateway.describe", {
    "tool_id": "playwright::browser_navigate"
})
```

Returns detailed schema with optional **code snippet** (if enabled):
```python
# Loop example
for url in urls:
    mcp.call_tool("gateway.invoke", {
        "tool_id": "playwright::browser_navigate",
        "arguments": {"url": url}
    })
```

### Layer 3: Full Methodology Guide
You're reading it! This comprehensive guide is lazy-loaded only when requested.

## Common Patterns

### Pattern 1: Batch Processing with Loops
**When**: Operating on multiple items (URLs, files, records)
**Code hint**: "loop"

```python
items = ["item1", "item2", "item3"]
results = []

for item in items:
    result = mcp.call_tool("gateway.invoke", {
        "tool_id": "server::tool_name",
        "arguments": {"input": item}
    })
    results.append(result)

print(f"Processed {len(results)} items successfully")
```

### Pattern 2: Filtering and Transformation
**When**: Working with large datasets
**Code hint**: "filter"

```python
# Get all data
all_data = mcp.call_tool("gateway.invoke", {
    "tool_id": "database::query",
    "arguments": {"query": "SELECT * FROM orders"}
})

# Filter locally (don't load all into context)
pending_orders = [
    order for order in all_data
    if order["status"] == "pending" and order["amount"] > 100
]

# Only show summary
print(f"Found {len(pending_orders)} high-value pending orders")
print(pending_orders[:5])  # Preview first 5
```

### Pattern 3: Conditional Logic
**When**: Decisions based on tool results
**Code hint**: "if/else"

```python
status = mcp.call_tool("gateway.invoke", {
    "tool_id": "server::getStatus",
    "arguments": {"id": "123"}
})

if status["is_running"]:
    print("Already running, skipping...")
else:
    mcp.call_tool("gateway.invoke", {
        "tool_id": "server::start",
        "arguments": {"id": "123"}
    })
    print("Started successfully")
```

### Pattern 4: Error Handling
**When**: Tools might fail
**Code hint**: "try/catch"

```python
failed = []
succeeded = []

for item in items:
    try:
        result = mcp.call_tool("gateway.invoke", {
            "tool_id": "server::process",
            "arguments": {"item": item}
        })
        succeeded.append(item)
    except Exception as e:
        failed.append({"item": item, "error": str(e)})

print(f"Success: {len(succeeded)}, Failed: {len(failed)}")
if failed:
    print("Failed items:", failed)
```

### Pattern 5: Polling and Retry
**When**: Waiting for async operations
**Code hint**: "poll"

```python
import time

# Start long-running operation
mcp.call_tool("gateway.provision", {
    "server_name": "github"
})

# Poll until complete
max_attempts = 30
for attempt in range(max_attempts):
    status = mcp.call_tool("gateway.provision_status", {
        "server_name": "github"
    })

    if status["state"] == "ready":
        print("Provisioning complete!")
        break
    elif status["state"] == "failed":
        print(f"Provisioning failed: {status.get('error')}")
        break

    print(f"Waiting... ({attempt + 1}/{max_attempts})")
    time.sleep(2)
```

## Best Practices

### 1. Start with Search
Always use `gateway.catalog_search` before invoking tools:
```python
# ✅ Good: Discover first
results = mcp.call_tool("gateway.catalog_search", {"query": "screenshot"})
tool_id = results[0]["tool_id"]

# ❌ Bad: Hardcode tool IDs
# tool_id = "playwright::browser_screenshot"  # Might not exist!
```

### 2. Filter Early
Don't load large datasets into context:
```python
# ✅ Good: Filter in execution environment
data = get_large_dataset()
filtered = [x for x in data if x["matches_criteria"]]
print(f"Found {len(filtered)} matches")
print(filtered[:5])  # Show sample

# ❌ Bad: Load everything into context
print(data)  # 10,000 items dumped to context!
```

### 3. Use Descriptive Summaries
When you process data, return human-readable summaries:
```python
# ✅ Good: Informative summary
print(f"Processed {total} orders: {success} succeeded, {failed} failed")
print(f"Revenue: ${total_revenue:.2f}")

# ❌ Bad: Raw data dump
print(all_orders)  # Bloats context
```

### 4. Handle Errors Gracefully
Wrap risky operations in try/except:
```python
# ✅ Good: Robust error handling
try:
    result = mcp.call_tool("gateway.invoke", {
        "tool_id": "api::call",
        "arguments": params
    })
except Exception as e:
    print(f"API call failed: {e}")
    # Fallback or retry logic
```

### 5. Leverage Lazy Loading
Use `gateway.describe` only when you need detailed schemas:
```python
# ✅ Good: Progressive disclosure
search_results = mcp.call_tool("gateway.catalog_search", {"query": "github"})
# ... review results ...
# Only describe when needed:
schema = mcp.call_tool("gateway.describe", {"tool_id": "github::createIssue"})
```

## Configuration

You can control guidance levels via `~/.claude/gateway-guidance.yaml`:

```yaml
guidance:
  level: "minimal"  # Options: "off", "minimal", "standard"

  layers:
    mcp_instructions: true     # L0: Philosophy in server instructions
    code_hints: true           # L1: Single-word hints in search results
    code_snippets: false       # L2: Code examples (default: off to save tokens)
    methodology_resource: true # L3: This guide (lazy-loaded)
```

**Recommendations**:
- **Minimal mode** (~200 tokens overhead): Best for most users
- **Standard mode** (~320 tokens overhead): Enable L2 if you want inline examples
- **Off**: Disables all guidance (not recommended)

## Examples

### Example 1: Screenshot Multiple Websites

```python
urls = [
    "https://anthropic.com",
    "https://github.com",
    "https://claude.ai"
]

screenshots = []
for i, url in enumerate(urls):
    print(f"Capturing {url}...")

    # Navigate
    mcp.call_tool("gateway.invoke", {
        "tool_id": "playwright::browser_navigate",
        "arguments": {"url": url}
    })

    # Screenshot
    result = mcp.call_tool("gateway.invoke", {
        "tool_id": "playwright::browser_screenshot",
        "arguments": {"path": f"screenshot_{i}.png"}
    })
    screenshots.append({"url": url, "file": f"screenshot_{i}.png"})

print(f"✓ Captured {len(screenshots)} screenshots")
```

### Example 2: Sync GitHub Issues to Notion

```python
# Get open GitHub issues
issues = mcp.call_tool("gateway.invoke", {
    "tool_id": "github::listIssues",
    "arguments": {
        "repo": "anthropics/pmcp",
        "state": "open"
    }
})

# Filter high-priority
high_priority = [
    issue for issue in issues
    if "priority: high" in issue.get("labels", [])
]

# Create Notion pages
created = 0
for issue in high_priority:
    try:
        mcp.call_tool("gateway.invoke", {
            "tool_id": "notion::createPage",
            "arguments": {
                "title": issue["title"],
                "content": issue["body"],
                "properties": {
                    "GitHub URL": issue["url"],
                    "Status": "Open"
                }
            }
        })
        created += 1
    except Exception as e:
        print(f"Failed to create page for issue #{issue['number']}: {e}")

print(f"✓ Created {created}/{len(high_priority)} Notion pages")
```

### Example 3: Analyze Documentation Coverage

```python
# Get all TypeScript files
files = mcp.call_tool("gateway.invoke", {
    "tool_id": "filesystem::listFiles",
    "arguments": {"path": "./src", "pattern": "*.ts"}
})

# Analyze each file
undocumented = []
for file_path in files:
    content = mcp.call_tool("gateway.invoke", {
        "tool_id": "filesystem::readFile",
        "arguments": {"path": file_path}
    })

    # Check for JSDoc comments (simple heuristic)
    lines = content.split("\n")
    has_docs = any("/**" in line for line in lines)

    if not has_docs:
        undocumented.append(file_path)

print(f"Documentation coverage: {len(files) - len(undocumented)}/{len(files)} files")
if undocumented:
    print(f"Missing docs: {len(undocumented)} files")
    print(undocumented[:10])  # Show first 10
```

## Summary

**Key Principles**:
1. **Write code** to orchestrate tools instead of chaining tool calls
2. **Filter early** to keep large datasets out of context
3. **Use loops** for batch operations
4. **Handle errors** with try/except
5. **Return summaries** instead of raw data dumps

**Progressive Disclosure**:
- L0: Brief philosophy (always visible)
- L1: Code hints during search
- L2: Code snippets on demand (opt-in)
- L3: This comprehensive guide (lazy-loaded)

**Token Budget**:
- Minimal mode: ~200 tokens overhead
- Standard mode: ~320 tokens overhead
- Massive savings compared to loading all tool schemas upfront!

Happy orchestrating! 🎵
