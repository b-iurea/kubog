import yaml
import requests
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from collections import defaultdict

class KubeAlertManager:
    def __init__(self, config_path, teams_webhook_url=None, mail_config=None):
        self.config_path = config_path
        self.teams_webhook_url = teams_webhook_url or os.getenv("TEAMS_WEBHOOK_URL")
        self.config = {}
        self.event_history = defaultdict(lambda: defaultdict(list))  # (ns, workload) -> type -> [timestamps]
        self.mail_config = mail_config or self._load_mail_config()
        self.last_alert_sent = defaultdict(lambda: defaultdict(lambda: None))  # 👈 AGGIUNTO
        self.load_config()

    def _load_mail_config(self):
        if os.getenv("SMTP_SERVER") and os.getenv("SMTP_TO"):
            return {
                "server": os.getenv("SMTP_SERVER"),
                "port": int(os.getenv("SMTP_PORT", "587")),
                "username": os.getenv("SMTP_USER"),
                "password": os.getenv("SMTP_PASS"),
                "from": os.getenv("SMTP_FROM", os.getenv("SMTP_USER")),
                "to": os.getenv("SMTP_TO"),
                "subject_prefix": os.getenv("SMTP_SUBJECT", "[KuBog Alert]")
            }
        return None

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"⚠️ Failed to load alert config: {e}")
            self.config = {}

    def should_alert(self, event):
        ns = event.get("namespace", "-")
        workload = event.get("workload", "Unknown")
        now = datetime.utcnow()

        # Determina la chiave
        exit_code = event.get("exit_code")
        reason = event.get("reason")
        event_type = event.get("type")

        possible_keys = []
        if exit_code is not None:
            possible_keys.append(f"ExitCode_{exit_code}")
        if reason:
            possible_keys.append(reason)
        if event_type:
            possible_keys.append(event_type)

        rule = None
        used_key = None
        for key in possible_keys:
            rule = self.config.get("kube-alerts", {}).get(key)
            if rule and rule.get("enabled", False):
                used_key = key
                break

        if not rule:
            return False, None

        # Salva evento nella history
        self.event_history[(ns, workload)][used_key].append(now)

        within_minutes = rule.get("within_minutes", 60)
        cutoff = now - timedelta(minutes=within_minutes)

        self.event_history[(ns, workload)][used_key] = [
            t for t in self.event_history[(ns, workload)][used_key] if t >= cutoff
        ]

        count = len(self.event_history[(ns, workload)][used_key])
        min_occur = rule.get("min_occurrences", 1)

        # ⛔ Check: alert già inviato di recente?
        last_sent = self.last_alert_sent[(ns, workload)][used_key]
        if last_sent and last_sent > cutoff:
            return False, None

        if count >= min_occur and rule.get("notify", False):
            # ✅ Invia alert e salva timestamp
            self.last_alert_sent[(ns, workload)][used_key] = now
            return True, rule

        return False, None



    def send_teams_alert(self, event, rule):
        if not self.teams_webhook_url:
            print("⚠️ No Teams webhook configured.")
            return
            

        ns = event.get("namespace", "unknown")
        wl = event.get("workload", "unknown")
        typ = event.get("type")
        pod = event.get("pod", "")
        container = event.get("container", "")

        message = rule.get("message", f"🚨 Alert: {typ}")
        suggestion = rule.get("suggestion", "No suggestion available.")

        card = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": message,
            "themeColor": "FF0000",
            "title": message,
            "sections": [
                {
                    "activityTitle": f"📦 Namespace: **{ns}**<br>🧱 Workload: **{wl}**",
                    "facts": [
                        {"name": "Pod", "value": pod},
                        {"name": "Container", "value": container},
                        {"name": "Type", "value": typ},
                        {"name": "Exit Code", "value": str(event.get("exit_code", ""))},
                        {"name": "Reason", "value": str(event.get("reason", ""))},
                        {"name": "Message", "value": str(event.get("message", ""))}
                    ],
                    "markdown": True,
                }
            ],
            "potentialAction": [
                {
                    "@type": "OpenUri",
                    "name": "View in Cluster",
                    "targets": [{"os": "default", "uri": "https://your-k8s-dashboard"}]
                }
            ]
        }

        try:
            resp = requests.post(self.teams_webhook_url, json=card)
            if resp.status_code >= 300:
                print(f"❌ Failed to send alert to Teams: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"❌ Exception sending Teams alert: {e}")

    def send_email_alert(self, event, rule):
        if not self.mail_config:
            print("⚠️ No mail config defined.")
            return

        try:
            msg = EmailMessage()
            msg["Subject"] = f"{self.mail_config['subject_prefix']} - {event.get('type')}"
            msg["From"] = self.mail_config["from"]
            msg["To"] = self.mail_config["to"]

            body = f"""
🚨 KuBog Alert: {event.get('type')}

📦 Namespace:       {event.get('namespace')}
🧱 Workload:        {event.get('workload')}
🐳 Pod:             {event.get('pod')}
📦 Container:       {event.get('container')}

❗ Reason:           {event.get('reason')}
💥 Exit Code:       {event.get('exit_code')}
📝 Message:         {event.get('message')}

🧠 Suggestion:
{rule.get('suggestion')}

📅 Timestamp:       {datetime.utcnow().isoformat()}
                """

            msg.set_content(body)

            with smtplib.SMTP(self.mail_config["server"], self.mail_config["port"]) as s:
                s.starttls()
                s.login(self.mail_config["username"], self.mail_config["password"])
                s.send_message(msg)

        except Exception as e:
            print(f"❌ Exception sending mail alert: {e}")

    def send_nodes_email_alert(self, event, rule):
        if not self.mail_config:
            print("⚠️ No mail config defined.")
            return

        try:
            msg = EmailMessage()
            msg["Subject"] = f"{self.mail_config['subject_prefix']} - {event.get('type')}"
            msg["From"] = self.mail_config["from"]
            msg["To"] = self.mail_config["to"]

            body = f"""
🚨 KuBog Alert: {event.get('type')}

📦 Node:       {event.get('node')}

📝 Message:         {event.get('message')}

🧠 Suggestion:
{rule.get('suggestion')}

📅 Timestamp:       {datetime.utcnow().isoformat()}
                """

            msg.set_content(body)

            with smtplib.SMTP(self.mail_config["server"], self.mail_config["port"]) as s:
                s.starttls()
                s.login(self.mail_config["username"], self.mail_config["password"])
                s.send_message(msg)

        except Exception as e:
            print(f"❌ Exception sending mail alert: {e}")