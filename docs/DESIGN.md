# DocuMind AI — UI/UX Specification & Design System

**Document Version:** 1.0.0  
**Status:** Approved Specification  
**Design Lead:** Anmol Vishvas (anmol23vishvas@gmail.com)  
**Target Repository:** [https://github.com/anmolvishvas/DocuMind-AI](https://github.com/anmolvishvas/DocuMind-AI)  
**Default Branch:** `main`  

---

## 1. Design Philosophy & Guiding Principles

DocuMind AI is crafted specifically for software engineers, platform architects, and DevOps professionals. The design draws inspiration from developer-grade platforms like Linear, Stripe Dashboard, Raycast, and Postman.

### Guiding Principles
1. **Information Density without Clutter:** Surface complex API parameters, schemas, and error codes efficiently without overwhelming the user.
2. **Keyboard-First Navigation:** Support quick global actions, search navigation (Cmd+K / Ctrl+K), and tab navigation between endpoints and chat sessions.
3. **Instant Visual Recognition:** Standardized color coding for HTTP verbs (GET, POST, PUT, DELETE, PATCH) and status codes (2xx, 4xx, 5xx).
4. **Verifiable Context (Citation First):** Every AI-generated answer must expose visible, interactive citation chips linking directly to the underlying documentation source.
5. **Accessible High-Contrast UI:** Full compliance with WCAG 2.1 AA standards for contrast across both dark (default) and light themes.

---

## 2. Visual Identity & Design Tokens

### 2.1 Brand Personality
- **Modern & Technical:** Clean geometric lines, subtle borders, deep neutral backdrops.
- **Precise & Reliable:** Monospace typography for routes, parameters, types, and code.
- **Fast & Responsive:** Instant optimistic UI updates, smooth streaming text transitions, and zero layout shift.

### 2.2 Color Palette & Semantic Tokens

```
+----------------------------------------------------------------------------------------------------+
|                                      COLOR PALETTE TOKENS                                          |
|                                                                                                    |
|  DARK THEME (Default)                      LIGHT THEME                                            |
|  - Background Base:  #09090b (Zinc-950)    - Background Base:  #ffffff (White)                    |
|  - Card / Surface:   #18181b (Zinc-900)    - Card / Surface:   #f4f4f5 (Zinc-100)                  |
|  - Border / Muted:   #27272a (Zinc-800)    - Border / Muted:   #e4e4e7 (Zinc-200)                  |
|  - Foreground Primary: #f4f4f5 (Zinc-100)  - Foreground Primary: #09090b (Zinc-950)              |
|  - Foreground Muted:   #a1a1aa (Zinc-400)  - Foreground Muted:   #71717a (Zinc-500)                |
|                                                                                                    |
|  BRAND & ACCENT                                                                                    |
|  - Primary Brand:    #6366f1 (Indigo-500)  - Primary Hover:    #4f46e5 (Indigo-600)                |
|  - Accent Glow:      #818cf8 (Indigo-400)                                                          |
|                                                                                                    |
|  HTTP METHOD SEMANTICS                                                                             |
|  - GET:    #10b981 (Emerald-500)           - Background: rgba(16, 185, 129, 0.12)                  |
|  - POST:   #3b82f6 (Blue-500)              - Background: rgba(59, 130, 246, 0.12)                  |
|  - PUT:    #f59e0b (Amber-500)             - Background: rgba(245, 158, 11, 0.12)                  |
|  - PATCH:  #f97316 (Orange-500)            - Background: rgba(249, 115, 22, 0.12)                  |
|  - DELETE: #f43f5e (Rose-500)              - Background: rgba(244, 63, 94, 0.12)                   |
|                                                                                                    |
|  STATUS CODES                                                                                      |
|  - 2xx Success:  #10b981 (Emerald-500)                                                             |
|  - 4xx Client:   #f59e0b (Amber-500)                                                               |
|  - 5xx Server:   #f43f5e (Rose-500)                                                                |
+----------------------------------------------------------------------------------------------------+
```

### 2.3 Typography Scale
- **Primary Font Family:** `Inter`, `-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, `sans-serif`
- **Monospace Font Family:** `JetBrains Mono`, `Fira Code`, `ui-monospace`, `monospace`
- **Type Scale:**
  - `Display / H1`: 28px (1.75rem), Font-weight: 700, Leading: 1.2
  - `H2 (Section Header)`: 20px (1.25rem), Font-weight: 600, Leading: 1.3
  - `H3 (Card Header)`: 16px (1.0rem), Font-weight: 600, Leading: 1.4
  - `Body Regular`: 14px (0.875rem), Font-weight: 400, Leading: 1.5
  - `Body Small / Footnote`: 12px (0.75rem), Font-weight: 400, Leading: 1.4
  - `Code / Monospace`: 13px (0.8125rem), Font-weight: 500, Leading: 1.5

### 2.4 Spacing & Elevation
- **Grid System:** 4px baseline (`p-1` = 4px, `p-2` = 8px, `p-3` = 12px, `p-4` = 16px, `p-6` = 24px, `p-8` = 32px).
- **Border Radius:**
  - Micro-elements (Badges, Chips): `rounded` (4px)
  - Inputs, Buttons, Dropdowns: `rounded-md` (6px)
  - Cards, Modals, Containers: `rounded-lg` (8px) or `rounded-xl` (12px)
- **Shadows:**
  - Subtle Surface: `box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.3)`
  - Floating Popover / Modal: `box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5)`

---

## 3. Main Application Screens

```
+----------------------------------------------------------------------------------------------------+
|                                    APPLICATION SHELL LAYOUT                                        |
|                                                                                                    |
|  +--------------------+-------------------------------------------------------------------------+  |
|  | DocuMind AI        | [Search Docs & Endpoints (Cmd+K)]               [Project: Payments v2 v] |  |
|  +--------------------+-------------------------------------------------------------------------+  |
|  | [Dashboard]        |                                                                         |  |
|  | [Projects]         |                                                                         |  |
|  | [API Explorer]     |                          MAIN WORKSPACE CANVAS                          |  |
|  | [AI Assistant]     |                                                                         |  |
|  | [Upload Docs]      |                                                                         |  |
|  | [MCP Settings]     |                                                                         |  |
|  |                    |                                                                         |  |
|  | ------------------ |                                                                         |  |
|  | Recent Chats:      |                                                                         |  |
|  | - Auth flow setup  |                                                                         |  |
|  | - 422 error debug  |                                                                         |  |
|  |                    |                                                                         |  |
|  | [v0.1.0] [Settings]|                                                                         |  |
|  +--------------------+-------------------------------------------------------------------------+  |
+----------------------------------------------------------------------------------------------------+
```

### Screen 1: Dashboard
- **Header:** Project selector dropdown, quick ingestion button, system health badge.
- **Top Metrics:** Total Endpoints Extracted, Total Document Chunks, Vector Index Status (`Healthy - HNSW`), Active MCP Tools (7 Connected).
- **Recent Ingestion Table:** Shows latest uploaded files (`stripe-openapi.yaml`, `authentication-guide.pdf`), status (`READY`, `PARSING`), and chunk counts.
- **Quick Actions:** "Ask AI Assistant", "Explore Endpoints", "Connect to Claude Desktop".

### Screen 2: Documentation Projects
- **Grid View:** Cards representing separate API projects.
- **Card Metadata:** Project Name, Description, Document count, Endpoint count, Created timestamp.
- **Actions:** "Create New Project", "Edit", "Delete (Cascade purge)".

### Screen 3: Upload & Import Documentation
- **Dropzone Area:** Large interactive dropzone supporting drag-and-drop or file picker.
- **Supported Formats Highlighted:** `.json` (OpenAPI/Swagger), `.yaml`/`.yml`, `.pdf`, `.md`, `.txt`.
- **Validation Feedback:** Instant client-side file size and extension check.
- **Ingestion Progress Bar:** Real-time visual stages: `Validating` -> `Parsing AST` -> `Extracting Endpoints` -> `Generating Embeddings` -> `Ready`.

### Screen 4: API Endpoint Explorer
- **Two-Column Split Layout:**
  - *Left Column (Endpoint List, 35% width):* Search input, HTTP Method filter chips (`ALL`, `GET`, `POST`, `PUT`, `DELETE`), Tag/Category accordion, Scrollable endpoint list showing `[METHOD] /route/path`.
  - *Right Column (Endpoint Details Canvas, 65% width):* Full contract view for selected endpoint.

### Screen 5: AI Documentation Assistant
- **Split Screen / Chat Canvas:**
  - *Chat Area (Left/Center, 65% width):* Message history, streaming token text, markdown formatted code blocks, and interactive citation badges.
  - *Context Inspector (Right, 35% width):* Dynamic drawer displaying the exact chunks and schemas retrieved by the RAG engine for the current turn.
- **Bottom Bar:** Auto-resizing textarea with keyboard submit (`Enter` to send, `Shift+Enter` for newline), quick prompt suggestion chips ("How to authenticate?", "Generate cURL for checkout", "Explain 422 error").

### Screen 6: Chat History
- **Sidebar Drawer / History List:** Grouped by date (Today, Yesterday, Last 7 Days).
- **Item Summary:** Auto-generated conversation title, query preview, associated project tag, delete button.

### Screen 7: Endpoint Detail View
- **Summary Header:** Large HTTP Method Badge, Monospace Route Path, Operation ID, Copy Path button.
- **Authentication Section:** Security Scheme name (e.g. `BearerAuth`), required scopes.
- **Parameters Table:** Grouped into Path, Query, and Header parameters. Columns: Name, Type, Required, Description, Schema constraints.
- **Request Body Section:** Content-Type dropdown (`application/json`), schema viewer, and sample payload.
- **Responses Section:** Tabbed view of status codes (`200 OK`, `400 Bad Request`, `404 Not Found`).

### Screen 8: API Request/Response Schema Viewer
- **Interactive Tree & Code Views:**
  - *Tree View:* Expandable/collapsible nodes with type badges (`string`, `integer`, `object`, `array`), required indicators (red asterisk `*`), and field descriptions.
  - *Raw JSON View:* Prettified JSON schema with copy-to-clipboard button.
  - *Mock Example View:* Synthesized JSON payload demonstrating valid structure.

### Screen 9: MCP Integration Settings
- **MCP Server Status Banner:** Green dot indicating server is active on `stdio` / `SSE`.
- **Configuration Generator:** Pre-formatted JSON block ready to paste into `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "documind": {
        "command": "python",
        "args": ["-m", "backend.app.mcp.server"],
        "env": {
          "DOCUMIND_API_URL": "http://localhost:8000",
          "DOCUMIND_PROJECT_ID": "default-project-id"
        }
      }
    }
  }
  ```
- **Active Tools Catalog:** Accordion listing all 7 registered MCP tools with input schemas and test invocation buttons.

### Screen 10: User & Workspace Settings
- **API Keys:** View and generate DocuMind API Keys for programmatic and MCP access.
- **AI Model Preferences:** Select default LLM (`gpt-4o` or `gpt-4o-mini`), configure temperature, and view token usage analytics.
- **Theme Preferences:** Dark Mode, Light Mode, System Default.

---

## 4. Reusable Component Anatomy

### 4.1 HTTP Method Badge
```
+---------------+
|  POST         |  (Blue background: #3b82f6 / 15%, Text: #60a5fa, Font: JetBrains Mono 11px)
+---------------+
```
- Variants: `GET` (Emerald), `POST` (Blue), `PUT` (Amber), `PATCH` (Orange), `DELETE` (Rose).
- Sizes: `sm` (height 18px), `md` (height 24px), `lg` (height 30px).

### 4.2 Citation Chip
```
+-------------------------------------------------+
| [Doc: petstore.yaml | POST /pet]                |
+-------------------------------------------------+
```
- Interactive clickable pill rendered inline inside AI assistant messages.
- Hover action: Displays tooltip with similarity score ($0.84$) and chunk preview.
- Click action: Opens Context Inspector side-drawer showing full source chunk and schema.

### 4.3 Code Snippet Viewer
```
+-------------------------------------------------------------------------+
| [cURL]  Python  JavaScript                               [Copy Code]    |
+-------------------------------------------------------------------------+
| curl -X POST "https://api.example.com/v1/customers" \                   |
|   -H "Authorization: Bearer YOUR_API_KEY" \                             |
|   -H "Content-Type: application/json" \                                 |
|   -d '{"email": "alex@example.com", "name": "Alex Rivera"}'             |
+-------------------------------------------------------------------------+
```
- Tabbed language switcher.
- Syntax highlighting with line numbers.
- One-click copy with temporary checkmark feedback ("Copied!").

### 4.4 Global Command Palette (Cmd+K / Ctrl+K)
- Global modal triggered by keyboard shortcut or top-bar search input.
- Real-time fuzzy search across:
  - Documented Endpoints (e.g. `POST /v1/charges`)
  - Uploaded Documentation sections
  - Common questions ("How do I paginate results?")

---

## 5. Micro-Interactions & State Transitions

1. **Streaming Token Generation:** As SSE events arrive, tokens are appended smoothly with a subtle blinking cursor indicator (`|`).
2. **Skeleton Loading:** When loading endpoints or document tables, render animated gradient pulse skeletons matching exact component dimensions to eliminate layout shifts.
3. **Empty States:** When a project has no uploaded documents or search returns zero endpoints, display a friendly SVG illustration, clear descriptive text, and a primary action button ("Upload OpenAPI Spec").
4. **Error Toasts:** Non-blocking floating notification in bottom-right corner with auto-dismiss after 5 seconds and explicit retry option.
