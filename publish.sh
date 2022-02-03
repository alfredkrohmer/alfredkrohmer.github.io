#!/bin/sh

set -e

bundle exec jekyll build
git checkout gh-pages
for f in _site/*; do
  rm -rf "./$(basename "$f")"
done
mv _site/* .
rm -rf _site .jekyll-cache
git add .
git commit -m "Publish at $(date --rfc-email)"
git push origin gh-pages
git checkout -
