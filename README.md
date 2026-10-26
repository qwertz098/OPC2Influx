# OpcUA → InfluxDB

A web-based gateway that bridges OPC UA servers to InfluxDB and SQLite databases — SQLite mainly as a local backup.
Browse OPC UA node trees, subscribe to signals, and record time-series data — all configured through a browser UI.

## Concept

```
 OPC UA Servers         This App              Storage
 ┌──────────┐      ┌───────────────┐      ┌───────────┐
 │ PLC / DCS│─────▶│ Subscriptions │─────▶│  InfluxDB │
 │ Sensors  │      │ + Polling     │      │  SQLite   │
 └──────────┘      └───────────────┘      └───────────┘
                          │
                     Web UI @ :8000
                     Dashboard · Browser
                     Chart · Subscriptions
                     Servers · Tags · Alerts
                     Settings
```

The app connects to one or more OPC UA servers, subscribes to selected variables at configurable intervals, and writes the values to InfluxDB (and/or SQLite targets). A live chart lets you inspect any signal in real time without recording it.

---

## Getting Started

### Prerequisites

- Python 3.11+
- At least one OPC UA server (or use the included demo server)
- InfluxDB 2.x instance (optional but intended primary use case, for time-series storage)

### Quick Start

1. **Start the demo OPC UA server** (optional):
   ```
   start_demo_server.bat
   ```
   This creates a virtual environment, installs dependencies, and launches a simulated OPC UA server at `opc.tcp://localhost:48400`.

2. **Start the application**:
   ```
   start_app.bat
   ```
   Opens the web UI at [http://localhost:8000](http://localhost:8000).

3. **Add your OPC UA server** in the Servers tab, then browse and subscribe to signals.

---

## Dashboard

The landing page shows a quick overview of the system state:

- **OPC UA Servers** — all configured servers with connection status (green/red dot) and endpoint URL. Errors are shown inline below each server.
- **InfluxDB Targets** — each target with write mode, row count, last write timestamp, and any errors.
- **Activity** — number of active subscriptions and active alarms.
- **Events** — recent system events (connections, errors, alarm triggers).

Use the Dashboard to verify that all servers are connected and data is flowing to your storage targets.

---

## Browser

The Browser tab lets you explore the OPC UA node tree of each connected server. Expand folders to navigate the address space — every variable, object, and method is displayed with its data type.

For variable nodes, you can:

- **Read** the current value in real time.
- **Subscribe** directly by clicking the subscribe button, which opens the subscription form pre-filled with the node's details.

For folder nodes, you can additionally:

- **⟳ Subscribe dynamically** — every folder row shows a small round ⟳ button. Click it to create a dynamic watcher: the app automatically subscribes to all current child variables and keeps the subscriptions in sync as nodes appear or disappear (see [Dynamic Folder Watchers](#dynamic-folder-watchers) in the Subscriptions section). Before the first scan runs, a dialog offers to add a matching [Name Strip Prefix](#name-strip-prefixes) for the server:
  - **Strip full path** — removes the entire folder path from measurement names
  - **Keep folder name out of prefix** (recommended, default) — keeps the folder's own name in the measurement so multiple watchers sharing a common root stay distinguishable
  - **No trim** — use full node paths as measurement names
  - **Do not subscribe** — cancels; no watcher is created

  The prefix must be chosen before the first scan — InfluxDB can't retroactively rename measurements already written, so changing the prefix afterward would split history across the old and new names.

### Color scheme: manual vs. dynamic

The tree uses one consistent color language (also mirrored on the Subscriptions page):

| Color | Meaning |
|---|---|
| **Green ✓** | Manually subscribed — variable checkbox, name, and badge turn green; a folder checkbox turns green once everything below it is subscribed |
| **Violet ⟳** | Managed by a dynamic folder watcher — managed variables show a violet checkbox, name, and ⟳ badge; the watched folder carries a violet **⟳ auto** pill |
| Blue | Neutral / not subscribed |

Unchecking a **violet (dynamically managed)** variable shows a confirmation first: the watcher would silently re-create the subscription on its next rescan, so to remove it permanently you release the watcher on the folder instead.

The tree loads lazily — child nodes are fetched only when you expand a folder, keeping the UI responsive even for servers with large address spaces. Expanding a folder costs a fixed three OPC UA service calls regardless of how many children it has (attributes, values, and has-children flags are read in bulk), so browsing stays fast even over slow VPN links. There is no limit on the number of children shown per node.

### Structured Variables (PLC Structs)

Some PLCs (notably WAGO/Codesys) expose a struct-typed Variable (e.g. a "MBC" variable whose value is a single custom `ExtensionObject`) that **also** publishes each struct member as its own scalar child Variable. The app detects this automatically: a Variable with a non-scalar data type gets an expand arrow and folder-style checkbox just like an Object, so you can drill down and subscribe to the individual scalar members (`Locked`, `RotSpeed`, `HourOn`, …) instead of the unsupported struct value itself.

---

## Subscriptions

The Subscriptions page has two sections: **Dynamic Folder Watchers** at the top, and the **Active Subscriptions** list below.

### Dynamic Folder Watchers

Instead of subscribing to individual nodes, you can point the app at a folder and it keeps subscriptions in sync automatically:

- **New child nodes** — subscribed automatically on the next rescan
- **Vanished nodes** — their subscriptions are disabled (not deleted), so historical data is preserved
- **Returned nodes** — re-enabled automatically

**Creating a watcher:** open the OPC UA Browser, navigate to a folder, and click the **⟳** button on the folder row. The app immediately scans the folder and creates subscriptions for all current children. The button turns to **✕⟳** (violet) and the folder gets a violet **⟳ auto** pill to indicate it is now dynamically managed. Click **✕⟳** again to remove the watcher (existing subscriptions are kept unless you choose to delete them).

Everything watcher-related is shown in violet throughout the UI — the watcher's **⟳ Active** status badge, the **⟳ dynamic** badge on managed subscriptions, and the managed variables in the browser tree — so it is always obvious which subscriptions are auto-managed and which are manual (green).

The watcher table shows:

| Column | Description |
|---|---|
| **Folder** | Browse path and optional display name |
| **Server** | Which OPC UA server the folder lives on |
| **Rescan Interval** | How often to re-check for new/removed nodes (minimum 15 s) |
| **Managed Subs** | Count of subscriptions created by this watcher |
| **Status** | **⟳ Active** (violet) / Disabled |
| **Actions** | **⟳ Scan now** (rescan immediately, bypassing the rescan interval), Enable/Disable, and Remove (with optional managed-sub deletion) |

### Active Subscriptions

This section lists all individual subscriptions. Each subscription defines:

| Field | Description |
|---|---|
| **Name** | Human-readable label for the signal |
| **Server** | Display name of the OPC UA server to read from |
| **Node ID** | The OPC UA node identifier (e.g. `ns=2;i=5`) |
| **Measurement** | InfluxDB measurement name, shown with the server's [Name Strip Prefixes](#name-strip-prefixes) already applied (e.g. `TotalPower_kW` instead of `PowerMonitor.TotalPower_kW`) — hover to see the full unstripped name |
| **Interval** | How often to poll the value (e.g. `1 s`, `5 s`) |
| **Write Mode** | `always` writes every poll; `on_change` writes only when the value differs |
| **Heartbeat** | Maximum time between writes even in `on_change` mode |
| **Tags** | Key-value pairs attached to each data point in InfluxDB |

Subscriptions created by a dynamic folder watcher show a violet **⟳ dynamic** badge under the name. Hovering over the badge shows the source folder path.

The **Status** column shows Active/Disabled, plus a red **Offline** indicator when the subscription's OPC UA server is currently disconnected — so you can see at a glance which points are not delivering data right now.

Actions per subscription: **Edit**, **Disable/Enable**, **Delete**.

Use **Check for stale** to verify that all subscribed nodes still exist on their server — useful after OPC UA server updates or address-space restructures.

### Unsupported value types

Structs, arrays, and other non-scalar OPC UA values have no direct InfluxDB/SQLite field representation. If a subscription's node ever returns such a value, the poller skips that write (no data recorded) and logs a one-time warning naming the subscription and its browse path. Dynamic Folder Watchers avoid this automatically by only auto-subscribing to data types allowed in **Settings → Dynamic Folder Auto-Subscribe** (see below) and by drilling into struct Variables to reach their scalar members instead of subscribing to the struct itself.

---

## Chart (Live Scope)

The Chart tab is a real-time oscilloscope for any OPC UA signal — independent of subscriptions. Use it to inspect signals without recording them.

### Selecting Signals

The left column has two parts:

- **From Subscriptions** (only shown if any subscriptions exist) — a filterable flat list of all configured subscriptions, grouped by server. Type in the filter box to narrow by name or server, then check a row to add it to the chart. Uses the same signal-selection mechanism as the tree below, so no separate re-browsing is needed for signals you've already subscribed to.
- **Select Signals** — the same OPC UA server tree as the Browser. Check any variable node to add it to the chart. Uncheck to remove it.

### Controls

| Button | Function |
|---|---|
| **▶ Start** | Begin polling selected signals and plotting |
| **⏸ Pause / ▶ Resume** | Freeze the chart without losing data — useful for inspecting a value with the cursor |
| **■ Clear** | Stop polling and erase all chart data |
| **✕ Unselect All** | Stop charting and remove all signals, legend entries, and buffered data — resets the page to its initial state |
| **⏱ Log Time** | Toggle logarithmic time axis (see below) |

### Settings

- **Interval** — polling period (0.5 s to 10 s)
- **Time frame** — a sliding window (∞, 1 min, 5 min, 10 min, 30 min, 1 h, 5 h, 24 h, 7 d). Polling never stops on its own — older points simply scroll off the left edge as new ones arrive, keeping only the selected window visible. Choose **∞** to keep everything (bounded only by the [memory cap](#memory-safety) below).

### Signal Multiplier

Each signal in the legend has a **multiplier input** (default `1.0`). Change it to scale the signal visually in the chart — useful when plotting signals with very different magnitudes on the same axis.

```
—— PowerMonitor.TotalPower_kW     85.3   ×  [ 1.0 ]
—— PowerMonitor.Frequency_Hz      50.0   ×  [ 2.0 ]
```

The tooltip always shows the **original un-scaled value**, so you can read engineering units directly.

### Logarithmic Time Axis

Toggling **Log Time** compresses the time axis logarithmically:

- The rightmost tick is **1 s** ago (most recent data with full resolution)
- Next tick to the left is **10 s**, then **100 s**, **17 min**, etc.
- Old data compresses into the left side; recent data spreads out on the right

This is useful for keeping the full history visible while maintaining high resolution on recent values.

### Saved Views

A chart configuration (selected signals, multipliers, interval, time window, log/scale mode) can be saved under a name and reloaded later:

- **Save view** — prompts for a name and saves the current state to `config/saved_charts.yaml`
- **Select…** dropdown — lists all saved views
- **Load** — restores the selected view (signals, multipliers, all settings)
- **✕** — deletes the selected view
- **↗** — opens the selected view as a standalone embed in a new browser tab

Saved views are included in the config backup/restore.

### Embedded Chart

Any saved view can be embedded in an external page (dashboard, HMI, wiki) via an iframe:

```html
<iframe src="http://yourserver:8000/chart/embed/{id}" width="100%" height="400"></iframe>
```

The embed auto-starts polling on load, shows a live legend with current values and multipliers, and the view name with a timestamp in the footer bar.

#### Themes

Append a theme name to the URL to control the visual style:

| URL suffix | Description |
|---|---|
| `/dark` (default) | Dark background, line chart |
| `/dark_shaded` | Dark background, filled area under lines |
| `/light` | White background, dark axes |
| `/light_shaded` | White background, filled area under lines |

Example: `/chart/embed/{id}/light_shaded`

### Persistence

Chart state (selected signals, data buffers, running/paused state) is preserved when you navigate to other tabs. Return to the Chart tab and polling resumes automatically from where it left off. Only **■ Clear** erases the data.

### Memory Safety

Each signal buffer is capped at 2000 data points. When the limit is reached, the oldest half is decimated (every other point removed), keeping memory usage bounded during long charting sessions.

---

## Servers

Configure OPC UA server connections:

- **Display Name** — friendly name shown in the UI
- **Endpoint URL** — the OPC UA endpoint (e.g. `opc.tcp://192.168.1.10:4840`)
- **Security Policy** — `None`, `Basic256Sha256`, etc.
- **Authentication** — anonymous or username/password

Servers connect automatically on startup. Connection status is shown on the Dashboard and as a green/red dot throughout the UI.

### Name Strip Prefixes

If all subscriptions on a server share a common leading path segment (e.g. `Factory.Site_A.ProductionArea`), you can configure it as a strip prefix. The app removes the prefix from the measurement name before writing to InfluxDB, keeping names short.

Use the **Suggest** button to auto-detect the longest common prefix across all current subscriptions for that server. Click **Use this** to apply the suggestion directly to the input field.

Creating a Dynamic Folder Watcher also offers to add a prefix automatically — see [⟳ Subscribe dynamically](#browser) in the Browser section.

### InfluxDB field types

OPC UA data types are mapped to InfluxDB field types as follows:

| OPC UA type | Python | InfluxDB field type |
|---|---|---|
| Float / Double | `float` | float |
| Int16 / Int32 / Int64 / UInt* | `int` | integer |
| Boolean (default) | `bool` | boolean (`true`/`false`) |
| Boolean + **Log as 0/1** | `int` | integer (`0`/`1`) |

The **Log booleans as integer 0/1** option is available per server in the server edit form. Use it when:
- Your InfluxDB bucket already has these fields stored as integers (from a previous version of this app), or
- You want to apply arithmetic Flux functions (`mean()`, `sum()`) on boolean signals.

> **Schema conflict warning:** InfluxDB locks the field type on the first write. Switching a field between boolean and integer (or any other type combination) causes a `422 Unprocessable Entity` error for that field. The remaining unaffected fields in the same batch **are still written** (InfluxDB partial write). To recover: delete the conflicting field in InfluxDB (Data Explorer → Delete Data) and let the app re-create it with the new type.

---

## InfluxDB

Configure InfluxDB 2.x write targets:

- **URL** — InfluxDB instance URL (e.g. `http://localhost:8086`)
- **Organization** — InfluxDB organization name
- **Bucket** — target bucket for data points
- **Token** — authentication token. The token field offers two modes:
  - **Direct token** — entered as a password, stored in plaintext in `config/influxdb.yaml`
  - **Environment variable** — enter a variable name (e.g. `INFLUXDB_TOKEN`), stored as `${INFLUXDB_TOKEN}` in YAML and resolved at runtime from the process environment. If the variable isn't actually set in the process, the app fails loudly (clear error in the Test/Retention result and in `status.last_error`) rather than silently sending the placeholder text as the token.
- **Write Mode** — `always` or `on_change` (default for subscriptions that don't override)

Multiple InfluxDB targets can be configured to write the same data to different instances or buckets. Each target can use a different environment variable for its token (e.g. `INFLUX_TOKEN_1`, `INFLUX_TOKEN_2`).

**Security note:** Direct tokens are stored in plaintext in `config/influxdb.yaml`. For production deployments, use the "Environment variable" option instead. Inject the actual token via Docker environment variables, `.env` files (excluded from version control), or your deployment platform's secret management.

### Retention & Shard-Group Duration

Each target has a **Retention** button that opens a panel to view and edit the bucket's settings directly from InfluxDB (no local caching — always reads live):

- **Retention Duration (days)** — how long InfluxDB keeps data before deleting it; `0` = keep forever
- **Shard Group Duration** — **Auto** (recommended; InfluxDB picks a size based on the retention duration) or **Manual** (explicit size in hours)

Since this uses InfluxDB's bucket-management API (not the write path), it requires a token with `buckets:read`/`buckets:write` permission — a write-only token will show a permission error when opening the panel, but can still submit new values blind.

---

## SQLite

Configure local SQLite file targets for data recording:

- **File Path** — where to write the SQLite database file
- **Table Name** — table for data points
- **Total Rows** — shows the total row count across all tables in the database file (not just the current session)

The **Historical Data Viewer** link at the bottom opens a query interface where you can select signals and time ranges from your SQLite data, and visualize the results as time-series charts.

### CSV Export

Each SQLite target has an **Export CSV** button that downloads the recorded data:

- **Single server** — downloads a `.csv` file with a `time` column and one column per signal (measurement name as header)
- **Multiple servers** — downloads a `.zip` containing one `.csv` per server table

Column headers use the current subscription measurement names. Columns for deleted subscriptions fall back to the raw sanitized column name.

---

## Tags

In InfluxDB, a **tag** is a key-value string pair that is indexed and stored alongside every data point. Tags are the primary way to filter, group, and aggregate data in queries. This app supports three independent tagging mechanisms that can be combined freely.

---

### Overview of tag sources

```
Data point written to InfluxDB
├── Measurement (from subscription, e.g. "Line_0.Speed_rpm")
├── Field      (value, e.g. 1234.5)
└── Tags
    ├── Central tags     (Tags config — reusable, activated per subscription)
    ├── Server tags      (Servers config — fixed per server, always applied)
    └── Per-sub tags     (Subscription config — inline, e.g. node attributes)
```

All three sources are merged before writing. If the same key appears in more than one source, per-subscription tags win over server tags, and server tags win over central tags.

---

### 1. Central Tags (Tags config tab)

Central tags are **reusable, named tag definitions** that you define once and then activate on any number of subscriptions. They come in two variants: static and dynamic.

#### Static central tags

A static tag always carries the same hard-coded value, regardless of which node it is attached to.

**Example use cases:**

| Key | Value | Purpose |
|---|---|---|
| `site` | `Building_A` | Group data by physical location |
| `line` | `ProductionLine_1` | Label the production line |
| `env` | `production` | Distinguish production data from test data |

Static tags are useful when a fixed label applies to an entire subscription or a group of subscriptions.

#### Dynamic central tags

A dynamic tag reads its value **live from a sibling OPC UA node** at poll time. Instead of a fixed string, you supply a **relative browse path** pointing to the node whose current value should become the tag value.

**How the relative path works:**

The path is resolved relative to the browse path of the subscribed node:

| Syntax | Meaning |
|---|---|
| `./SiblingName` | A node at the same level as the subscribed node |
| `../ParentSibling` | One level up, then into a sibling |
| `../../Area/Tag` | Two levels up, then down into a sub-path |

**Example:**

Your OPC UA tree looks like this:
```
ProductionArea/
  Line_0/
    Speed_rpm          ← subscribed node
    RecipeName         ← sibling node holding the current recipe
    ActiveBatch        ← sibling node holding the current batch ID
```

Define a dynamic tag:
- **Key:** `recipe`
- **Relative path:** `./RecipeName`

Every data point written for `Line_0.Speed_rpm` will carry the tag `recipe = "Recipe_A"` (or whatever `RecipeName` currently reads). When the recipe changes on the PLC, the tag value changes automatically on the next poll.

**Why dynamic tags matter:** InfluxDB data is partitioned by tag values, so you can later query "all speed data recorded while Recipe_B was active" with a simple `filter()` — even though you never explicitly wrote a recipe-aware subscription. The batch/recipe context is automatically embedded in every data point.

> **Implementation note:** The resolved value is cached after the first successful read and reused for subsequent polls. Changes to the referenced node take effect on the next cache refresh (restart or reconcile).

#### Activating central tags on subscriptions

Tags defined in the Tags config are **not automatically applied to every subscription**. Instead, each subscription has an explicit activation list. This lets you use the same tag definition selectively:

- A tag with **Default active: on** is activated automatically on every new subscription created via the browser.
- Tags can be toggled on or off per subscription in the subscription list (badge toggle).
- Disabling a tag on a specific subscription stops writing that tag for that subscription without affecting others.

---

### 2. Server Static Tags (Servers config)

Static tags can also be defined directly on a server. They are applied to **every data point from any subscription on that server**, without needing to activate them per subscription.

**Typical use:** stamp all data from a server with a fixed label.

```
server:  MyPLC_Line1
tags:    plc = "Siemens_S7_300"
         plant = "Hamburg"
```

Every subscription on `MyPLC_Line1` automatically carries `plc` and `plant` on each write. You do not need to configure anything per subscription.

---

### 3. Per-Subscription Tags (Subscription inline tags)

Each subscription can also carry its own inline tags, defined directly in the subscription's InfluxDB config. These can be:

- **Static** — a fixed string value
- **Node attribute** — a value read from the OPC UA node's own metadata (e.g. `BrowseName`, `DisplayName`, `Description`, `NodeId`)

Node-attribute tags are resolved once per polling cycle from the node's OPC UA attributes and are useful for embedding node metadata directly into the data point without maintaining a separate OPC UA subscription for it.

---

### Choosing the right tag mechanism

| Scenario | Recommended approach |
|---|---|
| Label all data from one server (e.g. plant name) | Server static tag |
| Label a group of subscriptions with a shared fixed value | Central static tag |
| Embed a changing runtime value from the PLC (batch, recipe, operator) | Central dynamic tag |
| Embed OPC UA node metadata (browse name, description) | Per-subscription node-attribute tag |

---

## Alerts

The Alerts page (**Watchdog & Alarm Rules**) covers two independent mechanisms:

### Watchdog

The watchdog monitors the gap between data writes and triggers an email alert when no new values have been written for a configurable duration. Useful for detecting silent failures (server disconnect, polling stall, network outage).

### Alarm Rules

Define threshold conditions on any subscribed signal (e.g. `TotalPower_kW > 100`). When the condition becomes true the alarm activates; when it clears the alarm deactivates. Active alarms are shown on the Dashboard.

Email notifications for both Watchdog and Alarm Rules are sent via the SMTP config in **Settings** (see below).

---

## Settings

The Settings page (⚙ in the navbar) consolidates application-wide configuration:

### Site Identity

- **Site Name** — a short label shown in the browser tab title and email alert subjects (e.g. `Factory A`). Can be overridden via the `SITE_NAME` Docker environment variable (the env value takes precedence over the UI setting).

### Dynamic Folder Auto-Subscribe

Choose which OPC UA scalar data types (Boolean, Int16–UInt64, Float, Double, String, DateTime) Dynamic Folder Watchers are allowed to auto-subscribe to. Deselect types you never want recorded.

- **DateTime is off by default** — the app cannot currently write datetime values to InfluxDB/SQLite, so subscribing to them would only create subscriptions that never record data.
- **String is off by default** — numeric/boolean I/O is the common case for auto-discovery; string signals are usually intentional and best added explicitly rather than picked up automatically.

Structs, arrays, and other non-scalar types are never auto-subscribed regardless of this setting — see [Structured Variables](#structured-variables-plc-structs) above.

### Email (SMTP)

Configure the outgoing mail server used by Watchdog and Alarm Rule notifications:

- **Enable email sending** — master switch. When off, no email is ever sent (Watchdog and Alarm Rules still evaluate normally), but **Send Test Email** still works so you can verify configuration before turning it on.

| Field | Description |
|---|---|
| **SMTP Host** | Mail server hostname or IP |
| **Port** | Usually 587 (STARTTLS) or 465 (SSL) |
| **TLS** | Enable STARTTLS |
| **Username / Password** | SMTP credentials |
| **From Address** | Sender address |
| **To Addresses** | One recipient per line |

Use **Send test email** to verify the configuration immediately.

> **Security note:** SMTP credentials are stored in plaintext in `config/alerts.yaml`. For production deployments inject them via the `SMTP_USERNAME` / `SMTP_PASSWORD` environment variables instead.

### Backup & Restore

- **Export** — downloads a `.zip` containing all YAML config files: servers, subscriptions, InfluxDB/SQLite targets, tags, alert rules, saved chart views, dynamic folder watchers, and app settings
- **Import** — upload a previously exported `.zip` to restore configuration

Use backup to migrate between instances or to keep versioned snapshots of your setup.

---

## Performance & Communication

The app is designed to stay responsive over slow links (e.g. a PLC reached through an IXON VPN) and to keep the load on the OPC UA server low:

- **Batched polling** — subscriptions are grouped per server and polling interval, and each group is read with as few OPC UA Read service calls as possible per cycle. Requests are split into chunks of 100 nodes to respect PLC operation limits (the WAGO PFC300 rejects larger requests — `MaxNodesPerRead=100`); this splitting is transparent and never drops nodes. 100 subscriptions at 1 s therefore cost ~1 request/s instead of 100.
- **Bulk browsing** — expanding a folder in the Browser costs a fixed 3 service calls regardless of the number of children.
- **Batched chart reads** — the live chart serves values from the polling cache where possible and batches any remaining live reads into one call per server.
- **HTTP compression & caching** — all responses are gzip-compressed and static assets (including the ~1 MB chart library) are cached immutably with version-busting, so remote page loads stay small.
- **SQLite tuning** — snapshot targets use a persistent connection with WAL mode and an index on the time column, so Data Viewer queries stay fast as files grow, and the dashboard's row counts are cached instead of re-counting on every refresh.

---

## Docker Deployment

```bash
docker build -t opcua2influx .
```

Copy `.env.example` to `.env`, fill in your values, then:

```bash
docker compose up -d
```

Minimal `.env` example with multiple alert recipients:

```env
INFLUXDB_TOKEN=my-secret-token
SITE_NAME=Plant North
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USERNAME=alerts@example.com
SMTP_PASSWORD=secret
SMTP_FROM=alerts@example.com
SMTP_TO=operator@example.com,manager@example.com,oncall@example.com
```

All sensitive values are read from `.env` and never baked into the image. Environment variables:

| Variable | Default | Description |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Bind address |
| `APP_PORT` | `8000` | HTTP port |
| `APP_CONFIG_DIR` | `/config` | Configuration directory |
| `APP_DATA_DIR` | `/data` | Data directory |
| `INFLUXDB_TOKEN` | — | InfluxDB auth token (or any name configured per target) |
| `SITE_NAME` | — | Site label shown in browser tab and email subjects |
| `SMTP_HOST` | — | SMTP server hostname |
| `SMTP_PORT` | — | SMTP port (e.g. `587`) |
| `SMTP_TLS` | — | Enable STARTTLS (`true` / `false`) |
| `SMTP_USERNAME` | — | SMTP login username |
| `SMTP_PASSWORD` | — | SMTP login password |
| `SMTP_FROM` | — | Sender address for alert emails |
| `SMTP_TO` | — | Recipient address(es), comma-separated (e.g. `op@site.com,mgr@site.com`) |

When an SMTP env var is set it takes precedence over the value saved in the UI. The Settings page shows affected fields as read-only with the active env var displayed inline.

---

## IXON SecureEdge Pro Deployment

The app can run on the IXON SecureEdge Pro edge gateway (ARM64). See `IXON_DEPLOYMENT.md` for a detailed step-by-step guide.

### Gateway IP Address

The default gateway IP is `192.168.140.1`. If your gateway uses a different IP, you need to adapt in two places:

1. **Docker Desktop insecure registry** — replace `192.168.140.1:5000` with your gateway IP in the Docker Engine config
2. **Build script** — set the `EDGE_IP` environment variable before running the build script:
   - Linux/macOS: `EDGE_IP=10.0.0.50 ./build_and_push_edge.sh`
   - Windows: edit the `set EDGE_IP=` line in `build_and_push_edge.cmd`

### Quick Start

1. Add `"insecure-registries": ["<gateway-ip>:5000"]` to Docker Desktop settings and restart
2. Run the build script:
   - Linux/macOS: `./build_and_push_edge.sh`
   - Windows: `build_and_push_edge.cmd`
3. Open the gateway web interface, create a container from the pushed image
4. Configure: Bridge networking, port 8000, named volumes for `/config` and `/data`

### Constraints

- No `cap_add` or `privileged` — not supported on the SEP
- No bind mounts — use named volumes only
- OPC UA servers must be reachable from the gateway's network

---

## Demo Server

The included `demo_server.py` simulates an OPC UA server with realistic industrial data structured in a deep hierarchy. All values update every 0.5 s with sine-wave dynamics and noise.

Start it with `start_demo_server.bat` or `python demo_server.py`.

### Node tree

```
Factory/
  Site_A/
    ProductionArea/
      Line_0..2/
        Status, Speed_rpm, ProductCount, Alarm, CycleTime_ms
        RecipeName, ActiveAlarm, OperatorID
        Drives/
          Main/   Running, Speed_rpm, Current_A, Temp_C
          Feed/   Running, Speed_rpm, Current_A, Temp_C
        Conveyor/ Running, Speed_ms, Load_kg
        QualityControl/ InspectionOK, RejectCount, CameraOnline
  Site_B/
    Utilities/
      HeatExchanger_0..1/ InletTemp_C, OutletTemp_C, FlowRate_m3h, Pressure_bar, Efficiency_pct, Pump_Running
      WaterTreatment/
        Tank_0..2/ Level_pct, Temperature_C, Pressure_bar, InletValve_Open, OutletValve_Open

PowerGrid/
  Mains/ TotalPower_kW, Phase1..3_V/A, Frequency_Hz, PowerFactor
  UPS/   BatteryLevel_pct, OnBattery, EstRuntime_min

TestBench/
  Sensors/
    Pressure/     Ch_0..2/    Raw, Filtered, Alarm
    Temperature/  Pt100_0..1/ Value, Offset, Ok
    Flow/         FT_001..2/  Flow_m3h, Velocity_ms, Totalizer
  Array/
    Subarray_0..1/ Struct_0..1/ BoolValue, IntValue, FloatValue, StringValue
```

### Suggested strip prefixes for testing

The deep hierarchy is well-suited for testing the **Name Strip Prefixes** feature (configured per server). Configure one or more prefixes to shorten measurement names in InfluxDB:

| Strip prefix | Example result |
|---|---|
| `Factory.Site_A.ProductionArea` | `Line_0.Drives.Main.Speed_rpm` |
| `Factory.Site_B.Utilities` | `HeatExchanger_0.InletTemp_C` |
| `TestBench.Sensors` | `Pressure.Ch_0.Raw`, `Flow.FT_001.Flow_m3h` |
| `PowerGrid` | `Mains.TotalPower_kW`, `UPS.BatteryLevel_pct` |

### Dynamic tag opportunities

`RecipeName`, `ActiveAlarm`, and `OperatorID` are string nodes that change value during the simulation — ideal for testing dynamic central tags:

| Tag key | Relative path (from a `Line_N` child node) | Effect |
|---|---|---|
| `recipe` | `./RecipeName` | Tag each data point with the current recipe |
| `operator` | `./OperatorID` | Tag each data point with the logged-in operator |
| `alarm` | `./ActiveAlarm` | Tag each data point with any active alarm string |
