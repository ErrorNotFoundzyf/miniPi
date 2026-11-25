from hashlib import sha256
from pathlib import Path


data = Path("settings.txt").read_bytes()
value = data.decode("utf-8").strip()
if value not in {"retries=2", "retries=3"}:
    raise SystemExit(f"Unsupported retry setting: {value!r}")
print("Retry setting is valid.")
print(f"REGRESSION_SHA256 settings.txt {sha256(data).hexdigest()}")
