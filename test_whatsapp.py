import requests, yaml
from pathlib import Path

with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

wa = cfg["whatsapp"]
message = "H2 System - WhatsApp notifications active. System online and ready."

params = {
    "phone":  wa["phone"],
    "text":   message,
    "apikey": wa["apikey"],
}

print(f"Sending to : +{wa['phone']}")
print(f"URL        : {wa['url']}")
print(f"API key    : {wa['apikey']}")
print(f"Message    : {message}")
print()

resp = requests.get(wa["url"], params=params, timeout=15)
print(f"HTTP status : {resp.status_code}")
print(f"Response    : {resp.text[:300]}")

if resp.status_code == 200:
    print("\nSUCCESS — message delivered.")
else:
    print(f"\nFAILED — check credentials or number registration.")
