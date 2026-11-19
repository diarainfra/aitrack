#!/bin/sh
# aitrack brauseri laiendus (macOS: topeltklõpsa Finderis; Linux: käivita terminalis).
cd "$(dirname "$0")" || exit 1
python3 aitrack.py browser setup
echo
printf "Vajuta Enter, et aken sulgeda..."
read -r _
