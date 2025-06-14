
![alt text](kubog_logo_2.png)

# 📦 KuBog - Kubernetes Pod Debugger & Alert System

An advanced tool to **monitor**, **analyze**, and **generate alerts** for Pods, Nodes, and API usage in Kubernetes clusters — with dynamic namespace tracking and historical reporting.

---

## 🚀 Quick Start

```bash
python3 kubog_v1.py --context <your-context> --chaos --watch --nodes --probes --state-changes --messages --logs

```

---

## 📈 Workload Historical Data for Smarter Debugging

KuBog is designed with a long-term vision: not just reactive monitoring, but **historical visibility** into your workloads.

We continuously collect key metrics and lifecycle events into organized CSV files, grouped per namespace and workload. This enables:

- 📊 **Post-mortem debugging** of pods, containers, and deployments  
- 🔁 **Trend analysis** of probe failures, OOM kills, exit codes, etc.  
- 🧠 **Improved root cause suggestions** based on past behaviors  
- ⏳ **Time-based filtering** of alerts (e.g., “at least 3 events in 15 minutes”)

Each event is timestamped and saved with contextual information such as:

- Namespace, workload, pod, container
- Type (e.g. `TERMINATION`, `OOM_KILLED`, `PROBE_FAILURE`)
- Reason (e.g. `CrashLoopBackOff`)
- Exit codes
- Messages and termination logs

This makes KuBog not just a real-time monitor, but also a **persistent observer** of workload health.



## 🔍 Main Features

- Continuous monitoring of pods, containers, nodes, and workloads
- Support for events such as:
  - CrashLoopBackOff
  - OOMKilled
  - Probe failures
  - Exit code
  - Pod deletions and more
- Generation of CSV reports
- Watch API for real-time updates
- `--chaos` mode for dynamic monitoring of all namespaces
- Integration with Microsoft Teams for smart notifications

### 🧪 Example Commands

Monitor a specific namespace:
```bash
python3 kubog_v1.py --context dev --namespaces default
```

Use in-cluster ServiceAccount:
```bash
python3 kubog_v1.py --service-account --chaos
```

---

## 🧰 Command Line Flags

| Flag                | Description                                                             |
|---------------------|-------------------------------------------------------------------------|
| `--context`         | Use specific kubeconfig context                                         |
| `--service-account` | Use in-cluster authentication                                           |
| `--chaos`           | Monitor **all namespaces**, even new ones created at runtime           |
| `--namespaces`      | List of namespaces to monitor                                           |
| `--workloads`       | Monitor specific workloads (`deployment/foo`, `job/bar`)               |
| `--watch`           | Use Kubernetes Watch API for real-time monitoring                      |
| `--nodes`           | Enable node-level resource and condition tracking                      |
| `--probes`          | Enable probe failure detection                                          |
| `--state-changes`   | Track container state transitions (Waiting → Running, etc.)            |
| `--messages`        | Capture container termination messages                                 |
| `--csv`             | Save results in CSV format (enabled by default)                        |
| `--logs`            | Output JSON logs to stdout                                              |

---

## 🔁 Dynamic Namespace Tracking

When using `--chaos`:
- Automatically adds new namespaces to monitoring
- Removes deleted namespaces
- Handles expired `resourceVersion` with retry/backoff

---

## 📦 Node Resource Monitoring

When using `--nodes`, the script tracks:
- CPU: capacity, allocatable, usage, requests, limits
- Memory: same breakdown as CPU
- Node conditions (`Ready`, `MemoryPressure`, etc.)
- Taints

📁 CSV files are saved to `./nodes/debug_node_<node>.csv`

### 📐 Unit conversions

Units are normalized to:

- **CPU** → expressed in fractional cores (float)
  - Supports: `n` (nanocores), `m` (millicores), `1` (cores)
- **Memory** → expressed in MiB (mebibytes)
  - Supports: `Ki`, `Mi`, `Gi`, `Ti`, etc.

---

## 📊 Workload Debug Reports

Per-workload CSVs are saved under `./workload/<namespace>/debug_<workload>.csv`

A global summary is saved every 5 minutes:
- `workload_overview.csv`
- or `cluster_overview_chaos.csv` (when in chaos mode)

---

## 📡 API Usage Profiling

📈 A visual analyzer (`api_usage_analyzer.py`) runs every 5 min and saves:
- `api_analyzer/api_calls_per_minute.png`
- `api_analyzer/avg_duration_per_method.png`
- `api_analyzer/top_namespaces.png`

---

## 🔔 Alert System with Microsoft Teams (Work in progress) and Email

Alerts are defined in a YAML file:

```yaml
kube-alerts:
  OOM_KILLED:
    enabled: true
    notify: true
    message: "💥 Pod terminated due to OOM"
    suggestion: "Increase memory limits in the workload"
    min_occurrences: 1

  CrashLoopBackOff:
    enabled: true
    notify: true
    message: "🔥 CrashLoopBackOff detected"
    suggestion: "Inspect container logs using `kubectl logs`"
    min_occurrences: 1
```

📬 Alerts are sent via Microsoft Teams using an Incoming Webhook URL. (WORK IN PROGRESS)


📌 The keys can be:
- `type` (es. `OOM_KILLED`, `PROBE_FAILURE`)
- `reason` (es. `CrashLoopBackOff`, `Evicted`)
- `ExitCode_<N>` (es. `ExitCode_137`, `ExitCode_1`)

📬 Alerts are sent via Email using smtp. For Gmail smtp use this Environments:

- $env:SMTP_SERVER = "smtp.gmail.com"
- $env:SMTP_PORT = "587"
- $env:SMTP_USER = 
- $env:SMTP_PASS = APPLICATION TOKEN
- $env:SMTP_FROM = 
- $env:SMTP_TO = 
- $env:SMTP_SUBJECT = "[KuBog Alert]"

---

##🔔 Advanced Alert Thresholds (Implemented Features)

We’ve extended the alert system so you can fine-tune exactly **when** and **how often** KuBog notifies you:

- **Configurable Fields** in `kube-alerts.yaml` per alert key:
  - `enabled`   → turn the rule on/off  
  - `notify`    → enable notification (Teams/email)  
  - `min_occurrences` → how many matching events must occur  
  - `within_minutes`  → sliding window length in minutes  
  - `message` / `suggestion` → custom text for the alert

- **Sliding-Window Logic**  
  1. On each event, we append a timestamp to an in-memory history for `(namespace, workload, key)`.  
  2. We prune all timestamps older than `within_minutes`.  
  3. If the remaining count ≥ `min_occurrences`, we fire the alert **immediately**.  

- **One-Time Alert Suppression**  
  - We record the time of the last alert for each `(ns, workload, key)` and **do not** re-alert until that history window has fully expired.  

- **Key Resolution Order**  
  - We derive the rule key in priority:  
    1. `ExitCode_<N>` (if `exit_code` present)  
    2. `reason` (e.g. `CrashLoopBackOff`)  
    3. `type` (e.g. `PROBE_FAILURE`)  

- **Integrated Notification Channels**  
  - **Teams** alerts are sent only if a webhook URL is provided.  
  - **Email** alerts are sent only if SMTP settings are detected in environment variables.  
  - We check each channel **once per run** (not per event) to avoid spamming the log with missing-webhook messages.

- **No “Expiry Reminders”**  
  - If the threshold isn’t reached within the window, **no late reminder** is sent. Alerts fire only at the moment the count threshold is passed.

---

## 🧠 Root Cause Suggestion Engine

Beyond static messages, KuBog **dynamically inspects** each workload’s spec to offer tailored advice:

- **OOMKilled / ExitCode_**  
  • Reads the container’s `resources.limits.memory`  
  • Recommends a +50% (or +100Mi) increase  
- **Probe failures**  
  • Reads the container’s `initialDelaySeconds`  
  • Suggests doubling the delay  

Configuration: no extra flags needed.  
Ensure KuBog runs with permissions to **read Deployments** in your namespaces.

## 🧱 Architecture

- `kubog_v1.py`: main logic
- `api_profiler.py`: wraps and times API calls
- `api_usage_analyzer.py`: generates PNG visualizations from usage data
- `debugger_safety_patch.py`: adds resilience to CSVs, threading, config errors
- `kube_alerts.py`: reads `kube-alerts.yaml` and sends Teams notifications

---

## 🔒 Requirements

- Python 3.8+
- `kubernetes`, `pandas`, `matplotlib`, `pyyaml`, `requests`

---

## 🛡️ Stability Features

- Handles expired `resourceVersion` with automatic recovery
- Skips crashing if metrics-server is down
- Automatically disables failed namespace watchers

---

## 📬 Contact

Per supporto o contributi: `b.iurea94@gmail.com`
