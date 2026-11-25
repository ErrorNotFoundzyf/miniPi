#!/bin/bash
set -u
mkdir -p /logs/verifier
if cmp -s /app/result.txt <(printf 'native-agent-ok\n'); then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
