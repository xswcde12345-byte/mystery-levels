#!/usr/bin/env bash
# Build the static site into _site/ (shared by the main and the intraday workflows).
set -euo pipefail
mkdir -p _site/data/picks
cp index.html picks.html _site/
cp data/latest.json _site/data/
cp data/picks/*.json _site/data/picks/        # includes intraday.json when the intraday job just wrote it
python3 scripts/build_hist.py _site/data/hist
if [ -f data/us.json ]; then cp data/us.json _site/data/; cp data/us_hist/*.json _site/data/hist/; fi
du -sh _site/data/hist
touch _site/.nojekyll
