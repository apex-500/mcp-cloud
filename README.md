# MCP Cloud

Hosted MCP server platform. Connect AI agents to powerful tools via a simple REST API or the MCP protocol over SSE. API key authentication, usage tracking, rate limiting, and billing built in.

## Pricing

| Tier       | Price      | Daily Limit  | Tools       | Priority |
|------------|------------|--------------|-------------|----------|
| Free       | $0/month   | 100 calls    | Basic tools | No       |
| Pro        | $29/month  | 10,000 calls | All tools   | No       |
| Business   | $99/month  | 100,000 calls| All tools   | Yes      |

## Quick Start

### 1. Get an API Key

Contact the admin or use the admin API to create a key:

```bash
curl -X POST https://mcp-cloud-2w62.onrender.com/v1/keys/create \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "tier": "free"}'
```

### 2. Make Your First Call

```bash
curl -X POST https://mcp-cloud-2w62.onrender.com/v1/tools/crypto_price \
  -H "Authorization: Bearer mcp_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "bitcoin"}'
```

### 3. Check Usage

```bash
curl https://mcp-cloud-2w62.onrender.com/v1/usage \
  -H "Authorization: Bearer mcp_live_YOUR_KEY"
```

## API Reference

### Public Endpoints

| Method | Path            | Description                |
|--------|-----------------|----------------------------|
| GET    | `/health`       | Health check               |
| GET    | `/v1/tools`     | List available tools       |
| GET    | `/v1/pricing`   | View pricing tiers         |

### Authenticated Endpoints

| Method | Path                    | Description              |
|--------|-------------------------|--------------------------|
| POST   | `/v1/tools/{tool_name}` | Execute a tool           |
| GET    | `/v1/usage`             | Get your usage stats     |

### Admin Endpoints

| Method | Path               | Description            |
|--------|--------------------|------------------------|
| POST   | `/v1/keys/create`  | Create a new API key   |
| GET    | `/v1/admin/stats`  | Global usage stats     |

### MCP Protocol (SSE)

| Method | Path             | Description                        |
|--------|------------------|------------------------------------|
| GET    | `/sse`           | SSE stream for MCP clients         |
| POST   | `/mcp/messages`  | JSON-RPC message endpoint for MCP  |

## Available Tools

### Crypto
- `crypto_price` - Get current cryptocurrency price
- `crypto_prices_batch` - Get prices for multiple coins
- `trending_tokens` - Get trending tokens from CoinGecko

### Monitoring
- `api_health_check` - Check if an API endpoint is healthy
- `http_request` - Make an HTTP request

### Conversion
- `csv_to_json` - Convert CSV to JSON
- `json_to_csv` - Convert JSON to CSV
- `markdown_to_html` - Convert Markdown to HTML

## MCP Client Configuration

Connect Claude Desktop or any MCP client to the remote SSE endpoint:

```json
{
  "mcpServers": {
    "mcp-cloud": {
      "transport": {
        "type": "sse",
        "url": "https://mcp-cloud-2w62.onrender.com/sse"
      }
    }
  }
}
```

## Self-Hosting

### With Docker

```bash
docker build -t mcp-cloud .
docker run -p 8000:8000 -e ADMIN_KEY=your-secret-admin-key mcp-cloud
```

### Without Docker

```bash
pip install -e .
export ADMIN_KEY=your-secret-admin-key
mcp-cloud
```

### Deploy to Railway

1. Push this repo to GitHub
2. Connect the repo in Railway
3. Set the `ADMIN_KEY` environment variable
4. Deploy

The included `railway.toml` handles the rest.

### Environment Variables

| Variable              | Description                        | Default          |
|-----------------------|------------------------------------|------------------|
| `ADMIN_KEY`           | Admin API key                      | Auto-generated   |
| `MCP_CLOUD_DATA_DIR`  | Directory for keys/usage files     | `.` (project root)|
| `PORT`                | Server port                        | `8000`           |

## Development

```bash
pip install -e .
uvicorn src.app:app --reload
```

The admin key is printed to the console on first startup. Use it to create API keys for testing.

## License

MIT
