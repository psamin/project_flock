#!/bin/bash
input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // empty')
deny='rm -rf|DROP TABLE|DROP DATABASE|TRUNCATE|git push.*--force|\.env'
if echo "$cmd" | grep -qiE "$deny"; then
  echo "Blocked by guard.sh: destructive/credential pattern. Ask Praneeth to run this manually." >&2
  exit 2
fi
exit 0
