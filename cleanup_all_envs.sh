#!/bin/bash

# Usage: ./cleanup_all_envs.sh <TOKEN>
if [ -z "$1" ]; then
  echo "Usage: $0 <x-token>"
  exit 1
fi

API_URL="https://api-v1.zrok.io/api/v1"
TOKEN="$1"

# Step 1: Get all environment zIds
echo "[*] Fetching environment list..."
response=$(curl -s "$API_URL/overview" \
  -H "accept: */*" \
  -H "accept-language: en-US,en;q=0.9,pl;q=0.8" \
  -H "priority: u=1, i" \
  -H "referer: https://api-v1.zrok.io/" \
  -H "sec-ch-ua: \"Not)A;Brand\";v=\"8\", \"Chromium\";v=\"138\", \"Google Chrome\";v=\"138\"" \
  -H "sec-ch-ua-mobile: ?0" \
  -H "sec-ch-ua-platform: \"macOS\"" \
  -H "sec-fetch-dest: empty" \
  -H "sec-fetch-mode: cors" \
  -H "sec-fetch-site: same-origin" \
  -H "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36" \
  -H "x-token: $TOKEN")

# Step 2: Parse zIds
zids=$(echo "$response" | jq -r '.environments[].environment.zId')

# Step 3: Disable each environment
for zid in $zids; do
  echo "[*] Disabling environment with zId: $zid"
  curl -s -X POST "$API_URL/disable" \
    -H "accept: */*" \
    -H "accept-language: en-US,en;q=0.9,pl;q=0.8" \
    -H "content-type: application/zrok.v1+json" \
    -H "origin: https://api-v1.zrok.io" \
    -H "priority: u=1, i" \
    -H "referer: https://api-v1.zrok.io/" \
    -H "sec-ch-ua: \"Not)A;Brand\";v=\"8\", \"Chromium\";v=\"138\", \"Google Chrome\";v=\"138\"" \
    -H "sec-ch-ua-mobile: ?0" \
    -H "sec-ch-ua-platform: \"macOS\"" \
    -H "sec-fetch-dest: empty" \
    -H "sec-fetch-mode: cors" \
    -H "sec-fetch-site: same-origin" \
    -H "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36" \
    -H "x-token: $TOKEN" \
    --data-raw "{\"identity\":\"$zid\"}"
  echo -e "\n[+] Disabled $zid"
done

echo "[✓] All environments disabled."
