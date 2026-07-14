# Mercato

**An agentic shopping assistant that finds products live across the internet — no pre-loaded catalog.**

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Tools](#tools)
- [Getting Started](#getting-started)
  - [Required Environment Variables](#required-environment-variables)
  - [IAM Permissions Required](#iam-permissions-required)
- [Usage Examples](#usage-examples)
- [Deploying to Lambda + API Gateway (Optional)](#deploying-to-lambda--api-gateway-optional)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [Security Best Practices](#security-best-practices)
- [Research Background](#research-background)
- [Project Structure](#project-structure)
- [License](#license)
- [Author](#author)

---

## Overview

Mercato is a command-line shopping assistant built on a LangGraph agent loop. You describe what you want in plain language — *"find me a phone under 20000 rupees"* — and the agent decides for itself which tools to reach for: querying UCP-compliant merchants, searching the open web, ranking what it finds by price, saving items to a wishlist, and handing back checkout links. It runs as a multi-turn conversation, so you can refine, compare, and save across a session.

What makes Mercato different is that **there is no product catalog**. Nothing is seeded, indexed, or embedded ahead of time — no static product database, no vector store, no Bedrock Knowledge Base. Every product the agent shows you was discovered live, in that moment, through the [Universal Commerce Protocol](#tools) or Tavily web search. The tradeoff is deliberate: results are always current, and the system carries no ingestion pipeline or staleness problem, but it is bounded by what those two sources can see right now.

This is a learning and portfolio project, built by a 2nd-year CS student targeting AWS-focused internships. It's written to be read: the infrastructure is scripted rather than clicked, the agent's reasoning is inspectable, and the limitations are documented rather than hidden.

---

## Key Features

### 🔍 Live Product Discovery

- Queries UCP-compliant merchants first for structured, purchase-ready data — prices, stock status, and verified checkout permalinks.
- Falls back automatically to Tavily web search when UCP returns nothing or is unreachable, so a search never dead-ends.
- No pre-loaded catalog: every result is fetched at query time, so prices and availability reflect the live web.
- The agent chooses its own search strategy and will re-query with narrower terms if the first pass is too vague.

### 💰 Price Comparison

- Ranks every candidate product cheapest-first and flags a single best deal.
- Merges results from UCP and web search into one comparison, so structured and unstructured sources compete on equal footing.
- Filters out failed-tool responses so an error never gets mistaken for a ₹0 product.
- Persists each comparison as a timestamped JSON artifact to S3 for later reference.

### ❤️ Wishlist Management

- Saves products to DynamoDB, scoped to your identity, with title, price, currency, merchant, and a saved-at timestamp.
- Product IDs are derived deterministically from the product URL, so saving the same item twice updates it instead of creating a duplicate.
- A dedicated `wishlist` command in the CLI reads your saved items directly, bypassing the agent loop entirely — no tokens spent just to look at a list.

### 🔗 Direct Checkout Links

- Returns the checkout link for any product and optionally opens it in your default browser.
- For UCP-sourced products this is a verified permalink that leads straight to purchase; for web-sourced products it's the product page.

### 🔐 Identity Without Login

- Your user ID is a SHA-256 hash of your AWS IAM ARN, derived at startup via STS. No Cognito, no signup, no password.
- The raw ARN is never stored — only the hash reaches DynamoDB or S3.

---

## Architecture

```
                        ┌──────────────────────────┐
                        │      cli/main.py         │
                        │  (click + rich, local)   │
                        └────────────┬─────────────┘
                                     │
        ┌────────────────────────────┴────────────────────────────┐
        │                                                         │
   LOCAL (default)                                    REMOTE (optional)
   direct in-process call                        ┌─────────────────────┐
        │                                        │   API Gateway       │
        │                                        │   POST /chat        │
        │                                        └──────────┬──────────┘
        │                                                   │
        │                                        ┌──────────▼──────────┐
        │                                        │  Lambda             │
        │                                        │  mercato-agent      │
        │                                        └──────────┬──────────┘
        │                                                   │
        └────────────────────────┬──────────────────────────┘
                                 │
                   ┌─────────────▼──────────────┐
                   │   LangGraph Agent Loop     │
                   │   (agent/agent.py)         │
                   │                            │
                   │   agent ──► tools ──┐      │
                   │     ▲               │      │
                   │     └───────────────┘      │
                   └─────────────┬──────────────┘
                                 │
                   ┌─────────────▼──────────────┐
                   │   Amazon Bedrock           │
                   │   Claude Sonnet 4.5        │
                   │   (tool-calling / Converse)│
                   └─────────────┬──────────────┘
                                 │
      ┌──────────────┬───────────┼───────────┬──────────────┬─────────────┐
      │              │           │           │              │             │
 ┌────▼────┐   ┌─────▼─────┐ ┌───▼─────┐ ┌───▼──────┐ ┌─────▼─────┐ ┌─────▼─────┐
 │ucp_query│   │web_search │ │price_   │ │save_     │ │get_       │ │checkout_  │
 │         │   │           │ │compare  │ │wishlist  │ │wishlist   │ │url        │
 └────┬────┘   └─────┬─────┘ └───┬─────┘ └───┬──────┘ └─────┬─────┘ └───────────┘
      │              │           │           │              │
   UCP API      Tavily API       │           └──────┬───────┘
                                 │                  │
                          ┌──────▼──────┐    ┌──────▼───────────────┐
                          │  S3         │    │  DynamoDB            │
                          │  artifacts  │    │  mercato-wishlist    │
                          │  + sessions │    │  mercato-sessions    │
                          └─────────────┘    └──────────────────────┘

  Identity: STS get_caller_identity() → SHA-256(IAM ARN)[:16] → user_id
            No Cognito. No login. No password.
```

### Data Flow

1. **Startup.** The CLI calls STS `get_caller_identity()`, hashes the returned IAM ARN with SHA-256, and takes the first 16 characters as your `user_id`. Every subsequent read and write is scoped to that value.
2. **You send a message.** It's appended to the conversation history and passed into the compiled LangGraph graph along with your `user_id`.
3. **The agent node runs.** Claude Sonnet 4.5, on Bedrock, sees the system prompt, the full history, and the six tool schemas, and decides whether to answer directly or call a tool.
4. **Tools execute.** If a tool is called, LangGraph's `ToolNode` runs it and appends the result to the conversation. Typically this means `ucp_query` first, `web_search` as a fallback, then `price_compare` to rank the merged results.
5. **The loop closes.** Control returns to the agent node, which sees the tool output and decides whether to call another tool or write a final answer. This repeats until the model stops requesting tools.
6. **The reply comes back.** The final message is returned to you, and the full session transcript is written to S3 in the background. Session logging never raises — if S3 is unreachable, the conversation continues regardless.

---

## Tech Stack

- **Python 3.11**
- **LangGraph** — the agent loop (`StateGraph`, `ToolNode`, `tools_condition`)
- **Claude Sonnet 4.5** on **Amazon Bedrock** — reasoning and tool selection, via the Converse API
- **Tavily API** — live web search
- **Universal Commerce Protocol (UCP)** — structured merchant product queries
- **Amazon DynamoDB** — wishlist and session storage
- **Amazon S3** — comparison artifacts and session transcripts
- **AWS Lambda** + **API Gateway** — optional hosted deployment
- **boto3** — all AWS access
- **click** + **rich** — the CLI

---

## Tools

The agent has six tools. It chooses which to call, in what order, and how many times — nothing here is a fixed pipeline.

| Tool | Purpose | Data Source | Description |
|------|---------|-------------|-------------|
| `ucp_query` | Structured product discovery | UCP API | Queries UCP-compliant merchants for live product data — price, stock status, verified checkout permalink, merchant, product ID, image. Tried **first** on every product search. Returns an error dict (not an exception) when unavailable, signalling the agent to fall back. |
| `web_search` | Broad product discovery | Tavily API | Searches the open web for candidate products, brands, and models. Returns title, URL, and a 300-character snippet. Does **not** extract structured prices — the model reads them from snippets. The fallback when UCP returns nothing. |
| `price_compare` | Ranking + artifact | In-memory + S3 | Merges UCP and web results into one list, drops any error dicts, sorts priced items cheapest-first, flags the best deal, and appends unpriced items at the end. Saves the ranked snapshot to `s3://{bucket}/users/{user_id}/comparisons/{timestamp}.json`. |
| `save_wishlist` | Persist an item | DynamoDB | Writes a product to `mercato-wishlist` under your `user_id`. The sort key `product_id` is the first 12 characters of the URL's MD5 hash, making saves idempotent per URL. Prices are stored as strings — DynamoDB's Python SDK can't serialize native floats. |
| `get_wishlist` | Read saved items | DynamoDB | Queries every item for your `user_id`, sorted newest-first by `saved_at`. |
| `checkout_url` | Open a product | Browser | Returns a product's checkout link and optionally opens it in your default browser. For UCP products this is a direct purchase permalink; for web products it's the product page. |

---

## Getting Started

### Prerequisites

- Python 3.11+
- An AWS account with Bedrock access in your chosen region
- The AWS CLI, configured with credentials
- A free [Tavily](https://tavily.com) API key (1,000 searches/month)

### Installation

```bash
git clone https://github.com/maasu-23/mercato.git
cd mercato

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

aws configure

cp .env.example .env           # then fill in TAVILY_API_KEY and S3_BUCKET_NAME

python infra/setup.py          # creates DynamoDB tables + S3 bucket, verifies Bedrock and Tavily
python -m cli.main             # start chatting
```

> **Note on `S3_BUCKET_NAME`:** S3 bucket names are globally unique across all AWS accounts. The value shipped in `.env.example` is already taken — change it to something of your own before running `infra/setup.py`.

> **⚠️ Bedrock model access is a one-time manual step.** Before Claude will respond, you must submit Anthropic's **use case details** form in the AWS Bedrock console under **Model access**. Approval is automatic but can take **up to 15 minutes to propagate**. Until it does, every invocation fails with `ResourceNotFoundException` — see [Troubleshooting](#troubleshooting).

`infra/setup.py` is idempotent and safe to re-run. It creates what's missing, skips what exists, and finishes by verifying that Bedrock can actually be *invoked* (not merely that the model is listed) and that your Tavily key is set.

### Required Environment Variables

Copy `.env.example` to `.env` and fill in the following. **`.env` is gitignored and must never be committed.**

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_REGION` | Yes | The AWS region for Bedrock, DynamoDB, and S3. Defaults to `ap-south-1`. |
| `BEDROCK_MODEL_ID` | Yes | The Bedrock model to run the agent on. Must be an **inference profile ID** — the shipped default is `global.anthropic.claude-sonnet-4-5-20250929-v1:0`. |
| `TAVILY_API_KEY` | Yes | Your Tavily API key. Get a free one at [tavily.com](https://tavily.com). Blank by default — `infra/setup.py` will tell you if it's missing. |
| `S3_BUCKET_NAME` | Yes | Bucket for comparison artifacts and session transcripts. **Must be globally unique** — pick your own. |
| `DYNAMODB_WISHLIST_TABLE` | Yes | Wishlist table name. Defaults to `mercato-wishlist`. |
| `DYNAMODB_SESSIONS_TABLE` | Yes | Sessions table name. Defaults to `mercato-sessions`. |
| `AWS_PROFILE` | No | Only needed to override the default AWS credential profile. Commented out by default. |
| `UCP_ENDPOINT` | No | Override the UCP base endpoint. Defaults to `https://api.ucp.dev/v1`. Commented out by default. |

### IAM Permissions Required

The credentials you run Mercato with need the following. This is the minimal set for local use — it covers table and bucket creation (`infra/setup.py`) as well as normal agent operation.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MercatoDynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable",
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:*:table/mercato-wishlist",
        "arn:aws:dynamodb:*:*:table/mercato-sessions"
      ]
    },
    {
      "Sid": "MercatoS3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:PutObject",
        "s3:PutBucketPublicAccessBlock"
      ],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET_NAME",
        "arn:aws:s3:::YOUR_BUCKET_NAME/*"
      ]
    },
    {
      "Sid": "MercatoBedrock",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:InvokeModel"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MercatoIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

Replace `YOUR_BUCKET_NAME` with the value you set for `S3_BUCKET_NAME`. If you also plan to deploy to Lambda and API Gateway, you'll need additional permissions for those services — see [Deploying to Lambda + API Gateway](#deploying-to-lambda--api-gateway-optional).

---

## Usage Examples

**Finding a product within a budget:**

```
> find me a phone under 20000 rupees

Mercato: Here are the top options under ₹20,000, ranked by price:

  1. Xiaomi Redmi 13 5G — ₹12,499  ⭐ BEST DEAL
     6GB RAM / 128GB · 108MP camera · 120Hz display · 5030mAh

  2. Vivo T4x 5G — ₹18,999
     8GB RAM / 128GB · 6500mAh battery

  3. realme P4x — ₹19,270
     6GB RAM / 128GB

  4. Motorola Moto G86 Power 5G — ₹19,999
     8GB RAM / 128GB · 6720mAh battery · Dimensity 7300

The Redmi 13 5G is the best value at ₹12,499. Want me to save any of
these to your wishlist, or get you a checkout link?
```

**Saving something you like:**

```
> save the redmi to my wishlist

Mercato: Saved — Xiaomi Redmi 13 5G (₹12,499) is now on your wishlist.
```

**Reviewing what you've saved, then leaving:**

```
> wishlist

                          Your Wishlist
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Title                 ┃  Price ┃ Currency ┃ Merchant          ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ Xiaomi Redmi 13 5G    │  12499 │ INR      │ Multiple retailers│
└───────────────────────┴────────┴──────────┴───────────────────┘

> exit

Goodbye — happy shopping!
```

The `wishlist` command reads DynamoDB directly rather than going through the agent, so it's instant and costs nothing. `exit` or `quit` ends the session.

---

## Deploying to Lambda + API Gateway (Optional)

Mercato runs locally by default, and that's the supported path. If you'd rather host it as an HTTP API, three scripts exist to do that:

```bash
python infra/build_lambda_package.py   # builds build/mercato-lambda.zip
python infra/deploy_lambda.py          # uploads to S3, updates the Lambda function code
python infra/setup_api_gateway.py      # creates the HTTP API, route, stage, and invoke permission
```

`infra/build_lambda_package.py` pins pip to `manylinux2014_x86_64` wheels for CPython 3.11 — without that, packages with compiled extensions (pydantic-core, numpy) build for your host platform and fail to import inside Lambda. The resulting package exceeds Lambda's 50 MB direct-upload limit, which is why `deploy_lambda.py` stages it through S3 rather than uploading it inline. `infra/lambda_handler.py` is the API Gateway entry point, and `infra/setup_api_gateway.py` is idempotent — re-running it reuses the existing API rather than creating a duplicate.

> **🚧 Roadmap — not yet implemented.** The intent is for the CLI to optionally talk to a deployed endpoint by setting `API_GATEWAY_URL` in `.env`, instead of calling the agent in-process. **This is not wired into `cli/main.py` yet.** Today the CLI always runs the agent locally, and the deployed Lambda is reachable only by calling its endpoint directly. Treat the deployment scripts as infrastructure that works, and the CLI integration as future work.

---

## Troubleshooting

**`ResourceNotFoundException: Model use case details have not been submitted for this account`**

- **Cause:** Anthropic requires a one-time use-case declaration before their models can be invoked on your Bedrock account. Listing the model succeeds without it — only *invoking* fails, which is why a model can appear "available" and still not work.
- **Solution:** Open the AWS Bedrock console → **Model access** → submit the **Anthropic use case details** form. Approval is automatic, but allow **up to 15 minutes** for it to propagate. Re-run `python infra/setup.py` to confirm; it performs a real invocation check, not just a catalog lookup.

**`ValidationException: Invocation of model ID ... with on-demand throughput isn't supported`**

- **Cause:** Claude Sonnet 4.5 can't be invoked by its bare model ID on on-demand throughput. It requires an **inference profile ID**.
- **Solution:** Make sure `BEDROCK_MODEL_ID` in `.env` uses the profile form with its region prefix — `global.anthropic.claude-sonnet-4-5-20250929-v1:0` — not `anthropic.claude-sonnet-4-5-20250929-v1:0`.

**`ModuleNotFoundError: No module named 'tavily'`** (or `langgraph`, `langchain_aws`, …)

- **Cause:** The virtual environment isn't active, or dependencies were installed into a different Python. This also shows up as `AttributeError: module 'langchain' has no attribute 'verbose'` when a globally-installed LangChain of a different version shadows the pinned one.
- **Solution:** Activate the venv (`venv\Scripts\activate` on Windows, `source venv/bin/activate` elsewhere) and re-run `pip install -r requirements.txt`. Confirm with `python -c "import sys; print(sys.executable)"` that you're on the venv's interpreter. Mercato pins exact versions in `requirements.txt` for exactly this reason — a global LangChain install will conflict.

**`ValidationException: Query condition missed key schema element: userId`**

- **Cause:** Your DynamoDB tables were created with the wrong partition key. Mercato uses **`user_id`** (snake_case), not `userId`.
- **Solution:** The tables must have partition key `user_id` (String) and sort key `product_id` (wishlist) or `timestamp` (sessions). If yours differ, delete them and re-run `python infra/setup.py`, which creates the correct schema. **Deleting a table deletes its data** — check the item count first if you have saved items you care about.

**`UnicodeEncodeError: 'charmap' codec can't encode character '✔'`**

- **Cause:** Windows terminals default to the cp1252 code page, which can't encode the ✔/✘ glyphs the scripts print.
- **Solution:** Already handled — every script calls `sys.stdout.reconfigure(encoding="utf-8")` at startup. If you hit this in your own code, set `PYTHONUTF8=1` in your environment.

---

## Known Limitations

Being straightforward about what this does and doesn't do:

- **UCP is an emerging protocol.** The public UCP endpoint may not resolve, and coverage across merchants is incomplete. In practice the agent frequently falls back to web search. This is handled gracefully — `ucp_query` returns an error dict rather than raising, and the agent moves on to `web_search` without the user noticing — but it means the "structured, purchase-ready data" path is aspirational more often than it's exercised.
- **Web-search prices are read from snippets, not extracted.** Tavily returns page text, not structured price fields. The model reads prices out of that text, which is usually right but is not a guarantee. Verify before you buy.
- **No authentication on the deployed API.** If you deploy to API Gateway, the `/chat` route has no authorizer — anyone with the URL can invoke it, and every call spends *your* Bedrock and Tavily budget. This is intended for local use with your own AWS credentials, not public hosting. Add a JWT authorizer or an API key before exposing it.
- **This is a portfolio and learning project, not a production shopping platform.** No rate limiting, no cost controls, no retry/backoff on tool failures, no evaluation harness. It's built to demonstrate an agentic architecture on AWS, and it's honest about being that.

---

## Security Best Practices

### Credentials Management

- `.env` is gitignored and must never be committed. `.env.example` is the template — it ships with **no** secret values.
- No credentials are hardcoded anywhere in the source. Every secret is read from the environment at runtime.
- AWS credentials come from the standard boto3 credential chain (`aws configure`, environment, or instance role). Mercato never asks for or stores an access key itself.

### Environment Variables

- **Safe to share:** `AWS_REGION`, `BEDROCK_MODEL_ID`, `DYNAMODB_WISHLIST_TABLE`, `DYNAMODB_SESSIONS_TABLE`, `UCP_ENDPOINT`. These are configuration, not secrets.
- **Never share:** `TAVILY_API_KEY`. Treat it like a password — it's billable.
- **Don't publish:** `S3_BUCKET_NAME` isn't a secret in the cryptographic sense, but publishing it tells the world where your artifacts live. Keep it out of screenshots and issue reports.
- If you deploy to Lambda, note that environment variables set on the function are **readable in plaintext** by anyone with `lambda:GetFunctionConfiguration`. For anything beyond a personal project, move `TAVILY_API_KEY` into AWS Secrets Manager.

### AWS IAM Best Practices

- Run Mercato under a **dedicated IAM user or role**, never your root account.
- Grant **least privilege** — the [policy above](#iam-permissions-required) is scoped to the specific tables and bucket Mercato uses, not `dynamodb:*` on `*`.
- Scope resource ARNs explicitly. Prefer `arn:aws:dynamodb:*:*:table/mercato-wishlist` over a wildcard.
- Rotate access keys periodically, and delete the ones you're not using.

### Data Protection

- **DynamoDB keys are hashed.** Your `user_id` is `SHA-256(IAM ARN)[:16]` — the raw ARN, which identifies your AWS account, is never written to DynamoDB or S3.
- **The S3 bucket blocks all public access.** `infra/setup.py` applies a full public-access block (`BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`, `RestrictPublicBuckets`) immediately after creating the bucket.
- **Session transcripts are stored.** Every conversation is written to S3 under `users/{user_id}/sessions/`. If you'd rather not persist them, leave `S3_BUCKET_NAME` unset — logging is skipped silently when it's absent.

---

## Research Background

Mercato is an applied project, but its design draws on a body of academic work in agentic AI, tool-use, and conversational commerce. The ReAct loop — interleaving reasoning with tool calls, rather than planning everything up front — is the direct ancestor of the LangGraph cycle at the core of this repo. The papers below informed how the agent reasons, when it reaches for tools, and how a shopping assistant differs from a general one.

1. **ReAct: Synergizing Reasoning and Acting in Language Models** — Yao et al., 2022 — [arxiv.org/abs/2210.03629](https://arxiv.org/abs/2210.03629)
2. **Reflexion: Language Agents with Verbal Reinforcement Learning** — Shinn et al., NeurIPS 2023 — [arxiv.org/abs/2303.11366](https://arxiv.org/abs/2303.11366)
3. **Toolformer: Language Models Can Teach Themselves to Use Tools** — Schick et al., Meta 2023 — [arxiv.org/abs/2302.04761](https://arxiv.org/abs/2302.04761)
4. **WebShop: Towards Scalable Real-World Web Interaction with Grounded Language Agents** — Yao et al., NeurIPS 2022 — [arxiv.org/abs/2207.01206](https://arxiv.org/abs/2207.01206)
5. **Model Context Protocol (MCP): Landscape, Security Threats, and Future Research Directions** — [arxiv.org/abs/2503.23278](https://arxiv.org/abs/2503.23278)
6. **Agentic Artificial Intelligence: Architectures, Taxonomies, and Evaluation of LLM Agents** — [arxiv.org/abs/2601.12560](https://arxiv.org/abs/2601.12560)
7. **Conversational Recommender System and Large Language Model Are Made for Each Other in E-commerce Pre-sales Dialogue** — Liu et al., 2023 — [arxiv.org/abs/2310.14626](https://arxiv.org/abs/2310.14626)
8. **The 2025 AI Agent Index** — MIT, 2025 — [arxiv.org/abs/2602.17753](https://arxiv.org/abs/2602.17753)
9. **ProductResearch: Training E-Commerce Deep Research Agents via Multi-Agent Synthetic Trajectory Distillation** — [arxiv.org/abs/2602.23716](https://arxiv.org/abs/2602.23716)
10. **Agentic Tool Use in Large Language Models** — [arxiv.org/abs/2604.00835](https://arxiv.org/abs/2604.00835)

---

## Project Structure

```
mercato/
├── agent/
│   ├── __init__.py
│   ├── agent.py                   # LangGraph loop, Bedrock LLM, chat() entry point
│   ├── state.py                   # AgentState TypedDict (messages, user_id, last_results)
│   └── tools/
│       ├── __init__.py            # ALL_TOOLS — the tool registry bound to the model
│       ├── ucp_query.py           # UCP merchant search
│       ├── web_search.py          # Tavily web search
│       ├── price_compare.py       # Ranking + S3 comparison artifacts
│       ├── wishlist.py            # save_wishlist / get_wishlist (DynamoDB)
│       └── checkout.py            # checkout_url
├── cli/
│   ├── __init__.py
│   └── main.py                    # Interactive CLI — the way you actually run this
├── infra/
│   ├── setup.py                   # Creates DynamoDB tables + S3 bucket; verifies Bedrock & Tavily
│   ├── lambda_handler.py          # API Gateway → Lambda entry point
│   ├── build_lambda_package.py    # Builds the deployment zip (manylinux wheels)
│   ├── deploy_lambda.py           # Uploads to S3, updates the Lambda function
│   └── setup_api_gateway.py       # Creates the HTTP API, route, stage, permissions
├── .env.example                   # Template — copy to .env and fill in
├── .gitignore
├── requirements.txt               # Exact pinned versions
└── README.md
```

---

## License

MIT.

> **Note:** a `LICENSE` file has not been added to this repository yet. Add one before treating the above as legally operative.

---

## Author

Built as a portfolio project by **Mahesh T** — [github.com/maasu-23](https://github.com/maasu-23)

2nd-year CS student, working toward AWS-focused engineering internships. Feedback and issues welcome.
