# 🤖 QA Bot — AI-Powered Automated QA & Bug Reporting

A standalone automated QA testing tool that crawls web applications, tests UI interactions, checks for console errors, probes routes, and generates HTML/JSON reports. **No Claude or any AI API key required** to run in default mode.

---

## ✅ Can I run this without Claude?

**Yes, completely.** The default configuration uses `ai.provider: "mock"` which runs the full test suite — page loads, navigation crawl, console error analysis, button tests, performance metrics — and generates complete HTML + JSON reports with no API key needed.

Claude (or OpenAI) is optional and only adds AI-written bug descriptions on top of the raw test data.

---

## 🚀 Quick Start (No API Key Required)

### 1. Prerequisites

- Python 3.10+
- pip

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env if needed — all fields are optional for basic runs
```

### 4. Run against any URL

```bash
# Standard run (recommended)
python -m src.cli run --url https://example.com

# Light run (faster — page load + navigation only)
python -m src.cli run --url https://example.com --depth light

# Full run (all tests including buttons on every page)
python -m src.cli run --url https://example.com --depth full

# With a custom test scenario file
python -m src.cli run --url https://example.com --scenario config/test_scenarios.yaml

# Show browser window (non-headless, useful for debugging)
python -m src.cli run --url https://example.com --no-headless
```

Reports are saved to `outputs/reports/` as both `.html` and `.json`.

---

## 🧠 Optional: Enable AI Analysis

To get AI-powered bug descriptions and recommendations, add an API key to `.env` and update `config/default.yaml`:

### Using Claude (Anthropic)

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

```yaml
# config/default.yaml
ai:
  provider: "claude"
  model: "claude-opus-4-6"
```

### Using OpenAI

```bash
# .env
OPENAI_API_KEY=sk-your-key-here
```

```yaml
# config/default.yaml
ai:
  provider: "openai"
  model: "gpt-4o"
```

---

## 📁 Project Structure

```
qa-bot/
├── src/
│   ├── cli.py                  # Entry point — `python -m src.cli run`
│   ├── core/                   # Config, constants, models, exceptions
│   ├── engines/
│   │   ├── ui/                 # Playwright browser engine
│   │   │   └── tests/          # page_load, navigation, console_errors, buttons
│   │   ├── api/                # HTTP API testing engine
│   │   └── performance/        # Perf metrics engine
│   ├── orchestrator/           # Session management & test runner
│   ├── collectors/             # Result aggregation & issue classification
│   ├── reporting/              # HTML + JSON report generators
│   └── storage/                # SQLite/PostgreSQL persistence (optional)
├── config/
│   ├── default.yaml            # Default config (edit this for global settings)
│   └── test_scenarios.yaml     # Custom test flows per app
├── outputs/                    # Generated reports, screenshots, logs (gitignored)
├── requirements.txt
└── .env.example
```

---

## ⚙️ Configuration

All settings live in `config/default.yaml`. Key options:

| Setting | Default | Description |
|---|---|---|
| `target.test_depth` | `standard` | `light` / `standard` / `full` |
| `browser.headless` | `true` | Set `false` to watch the browser |
| `browser.browser_type` | `chromium` | `chromium` / `firefox` / `webkit` |
| `ai.provider` | `mock` | `mock` / `claude` / `openai` |
| `performance.enabled` | `true` | Capture load time metrics |
| `api.enabled` | `true` | Probe API endpoints |
| `output.formats` | `[html, json]` | Report output formats |

### Test depth explained

| Depth | What runs |
|---|---|
| `light` | Page load + navigation crawl |
| `standard` | + Console error analysis + button tests (root page only) |
| `full` | + Button tests on every discovered page |

---

## 🔐 Login-Protected Apps

To test apps that require login, configure auth in your scenario file:

```yaml
# config/test_scenarios.yaml
auth:
  type: "basic"              # none | basic | jwt | api_key | bearer
  login_url: "/login"
  username: "${QA_USERNAME}" # set in .env
  password: "${QA_PASSWORD}"
```

---

## 📊 Sample Output

```
╔══════════════════════════════════════════════════════╗
║  🤖 QA Bot  ·  https://example.com  ·  standard     ║
╚══════════════════════════════════════════════════════╝
Session ID: abc123

[✓] Page Load: https://example.com          226ms
[✓] Navigation: 4 pages discovered
[✗] Console Errors: 2 JS errors found       /dashboard
[✓] Button Tests: 8/8 passed
[✗] Button: CTA button unresponsive         /contact

─────────────────────────────────────────────────────
  Tests:   14   Passed: 12   Failed: 2   Errors: 0
  Issues:   3   Critical: 0  High: 1     Medium: 2
  Health Score: 74/100
─────────────────────────────────────────────────────
Reports saved:
  HTML → outputs/reports/report_abc123.html
  JSON → outputs/reports/report_abc123.json
```

---

## 🐛 Known Issues & Fixes

### Playwright browser not installing

If `playwright install chromium` fails (e.g., in a restricted network):

```bash
# Option 1: Use system Chromium
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser
python -m src.cli run --url https://example.com

# Option 2: Install system deps first (Linux)
sudo apt-get install -y chromium-browser

# Option 3: Use Firefox instead
# Edit config/default.yaml: browser_type: "firefox"
playwright install firefox
```

### asyncpg install fails on ARM64

```bash
sudo apt-get install -y libpq-dev gcc python3-dev
pip install asyncpg
```

Or skip PostgreSQL and use the default SQLite (no config change needed).

### Skip database entirely

```bash
python -m src.cli run --url https://example.com --no-db
```

---

## 📋 Requirements

```
Python      >= 3.10
Playwright  >= 1.44
```

All Python dependencies are in `requirements.txt`. No Docker required.

---

## 📄 License

MIT
