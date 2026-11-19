#!/usr/bin/env bash
# Build the static site into _site/ (shared by the main and the intraday workflows).
set -euo pipefail
mkdir -p _site/data/picks
cp index.html picks.html fear.html _site/
cp data/latest.json _site/data/
cp data/picks/*.json _site/data/picks/        # includes intraday.json when the intraday job just wrote it
python3 scripts/build_hist.py _site/data/hist
if [ -f data/us.json ]; then cp data/us.json _site/data/; cp data/us_hist/*.json _site/data/hist/; fi
du -sh _site/data/hist
mkdir -p _site/data/fear && cp data/fear/tw.json data/fear/us.json data/fear/sox.json _site/data/fear/ 2>/dev/null || true
touch _site/.nojekyll
