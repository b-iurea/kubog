import pandas as pd
import matplotlib.pyplot as plt
import os
from datetime import datetime, timedelta

def run_api_analysis(records, output_dir="api_analyzer"):
    """
    records: lista di dict con chiavi
      ['timestamp','method','resource','namespace','duration_ms','status_code']
    """
    if not records:
        return

    # Costruiamo il DataFrame direttamente
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    now = datetime.utcnow()
    window_start = now - timedelta(hours=2)
    df = df[df["timestamp"] >= window_start]
    df = df[df["resource"] != "nodes"]
    if df.empty:
        return

    df["minute"] = df["timestamp"].dt.floor("min")
    os.makedirs(output_dir, exist_ok=True)

    # Plot 1: calls per minute
    calls_per_min = df.groupby("minute").size()
    plt.figure(figsize=(10,4))
    calls_per_min.plot(marker='o', title="API Calls Per Minute (last 2h)")
    plt.ylabel("Calls")
    plt.xlabel("Time")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "api_calls_per_minute.png"))
    plt.close()

    # Plot 2: avg duration
    avg_dur = df.groupby("method")["duration_ms"].mean().sort_values()
    plt.figure(figsize=(8,4))
    avg_dur.plot(kind="barh", title="Avg Response Time by Method (ms)")
    plt.xlabel("Milliseconds")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "avg_duration_per_method.png"))
    plt.close()

    # Plot 3: top namespaces
    top_ns = df["namespace"].value_counts().head(10)
    plt.figure(figsize=(8,4))
    top_ns.plot(kind="bar", title="Top 10 Namespaces by API Call Volume")
    plt.ylabel("Calls")
    plt.xticks(rotation=45)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "top_namespaces.png"))
    plt.close()
