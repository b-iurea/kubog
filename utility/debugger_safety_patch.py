import logging

# Initialize a central logger
logging.basicConfig(
    filename='debugger_errors.log',
    level=logging.WARNING,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def safe_write_csv(filepath, data, headers):
    try:
        import os
        import csv
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        file_exists = os.path.exists(filepath)
        with open(filepath, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            writer.writerows(data)
    except Exception as e:
        logging.warning(f"CSV write failed for {filepath}: {e}")

def safe_sort_dataframe(df, by_column):
    try:
        if df.empty or by_column not in df.columns:
            return df
        return df.sort_values(by=[by_column], ascending=False)
    except Exception as e:
        logging.warning(f"DataFrame sort failed: {e}")
        return df

def safe_alert_config(alerts_config, key, default_value=None):
    try:
        return alerts_config.get(key, default_value)
    except Exception as e:
        logging.warning(f"Missing key in alert config [{key}]: {e}")
        return default_value

def is_thread_running_for(namespace, watchers):
    return namespace in watchers and watchers[namespace].is_alive()