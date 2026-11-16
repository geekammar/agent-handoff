#!/bin/sh
set -eu
TOKEN_FILE="${1:-/root/gdrive-token.json}"
if [ -s "$TOKEN_FILE" ]; then
  chmod 600 "$TOKEN_FILE" 2>/dev/null || true
  exec agent-handoff auth --import-token-file "$TOKEN_FILE"
fi
cat <<'INSTRUCTIONS'
Google Drive authorization is the only manual setup step.

Never paste a token, password, or refresh token into an AI chat.

Option 1 - import a token produced elsewhere (headless-safe):
  1. On a machine with a browser (a laptop, or Termux on Android with rclone), run:
       rclone authorize "drive"
  2. Sign in with the Google account that owns "AI Project Operations".
  3. Copy the JSON token that rclone prints.
  4. On this VPS, save it as /root/gdrive-token.json without pasting it into chat.
     For example, run `cat > /root/gdrive-token.json`, paste the JSON, then press Ctrl-D.
     Ensure the file is mode 600.
  5. Run:
       agent-handoff auth --import-token-file /root/gdrive-token.json
       agent-handoff auth --check
       agent-handoff doctor
     The import command verifies Google Drive before keeping the config.

Option 2 - official interactive rclone flow on this VPS:
  Run `rclone config`, create a remote named `gdrive`, type `drive`, scope `drive`.
  For a headless machine answer No to auto config and paste a token from
  `rclone authorize "drive"` running on a browser-capable machine.
  Use the same rclone version on both machines when possible.

After authorization, retry queued reports:
  agent-handoff retry-upload --force
INSTRUCTIONS
printf '\nStart the official interactive rclone config now? [y/N] '
read ANSWER
case "$ANSWER" in
  y|Y|yes|YES) exec rclone config ;;
  *) echo "No changes made." ;;
esac
