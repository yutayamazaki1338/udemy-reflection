#!/bin/zsh
cd "${0:A:h}"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3が必要です。README.mdの手順を確認してください。'
  read -r '?Enterで閉じます'
  exit 1
fi
python3 app.py
