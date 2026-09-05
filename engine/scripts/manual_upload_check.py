import requests
import json

url = "http://localhost:8001/reconcile/upload"

config = json.load(open("data/50k_stress/batch_config.json"))

data = {
    "batch_id": config["batch_id"],
    "net_amount": str(config["target_net_amount_inr"]),
    "currency": "INR",
    "settled_at": "2026-08-18T00:00:00Z",
    "settlement_window_days": "5"
}

files = {
    "gateway_file": open("data/50k_stress/gateway_report.csv", "rb"),
    "bank_file": open("data/50k_stress/bank_statement.csv", "rb"),
    "erp_file": open("data/50k_stress/erp_ledger.json", "rb")
}

print("Sending request...")
response = requests.post(url, data=data, files=files)
print(f"Status Code: {response.status_code}")

if response.status_code == 200:
    result = response.json()
    print(json.dumps(result, indent=2))
else:
    print(response.text)
