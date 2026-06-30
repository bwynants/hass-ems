---
name: restart-ha
description: Restart the Home Assistant instance at test.niobe.synology.me via its REST API, then poll until it is back up. Use when the user asks to restart Home Assistant / HA, or to apply integration code changes that need a full restart.
---

# Restart Home Assistant

Restarts the HA instance and waits for it to come back online.

## Config

- **Base URL:** `https://test.niobe.synology.me`
- **Token file:** `/Volumes/config/.ha_token` (long-lived access token, one line, mode 600, outside the git repo)

If the token file is missing, tell the user to recreate it:
```
umask 077; echo "PASTE_LONG_LIVED_TOKEN" > /Volumes/config/.ha_token
```
(Create a token in HA: profile → Security → Long-lived access tokens → Create token.)

## Steps

1. **Trigger the restart.** A `504` (or a curl timeout / connection-reset) here is the *expected* success signature — HA drops the in-flight request as it goes down. Treat `200`, `504`, and timeouts as "restart accepted"; only a `401` means the token is bad.
   ```bash
   TOKEN=$(cat /Volumes/config/.ha_token)
   curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -w "\nHTTP %{http_code}\n" --max-time 30 \
     https://test.niobe.synology.me/api/services/homeassistant/restart
   ```

2. **Poll until it's back.** `/api/` returns `401` without auth once HA is serving again.
   ```bash
   for i in $(seq 1 30); do
     code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 https://test.niobe.synology.me/api/ 2>/dev/null)
     echo "attempt $i: HTTP $code"
     if [ "$code" = "401" ] || [ "$code" = "200" ]; then echo "HA is back up"; break; fi
     sleep 6
   done
   ```

3. **Verify it's healthy** with the token:
   ```bash
   TOKEN=$(cat /Volumes/config/.ha_token)
   curl -s -H "Authorization: Bearer $TOKEN" -w "\nHTTP %{http_code}\n" --max-time 10 \
     https://test.niobe.synology.me/api/
   ```
   Expect `{"message":"API running."}` and HTTP `200`.

4. If checking the `energy_management_system` integration after the restart, list its entities:
   ```bash
   TOKEN=$(cat /Volumes/config/.ha_token)
   curl -s -H "Authorization: Bearer $TOKEN" --max-time 10 https://test.niobe.synology.me/api/states \
     | python3 -c "import sys,json; d=json.load(sys.stdin); [print(s['entity_id'],'=',s['state']) for s in d if 'energy_management_system' in s['entity_id']]"
   ```

## Notes

- Never print the token. Read it from the file at call time; do not paste it into commands that get echoed.
- The token file is plaintext on disk — acceptable because it lives in the already-sensitive HA config dir and outside the git repository, so it won't be committed.
