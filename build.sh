#!/bin/bash
set -e
rm -rf dist
mkdir -p dist
cp index.html dist/index.html
mkdir -p dist/assets
cp assets/page-refresh.js assets/page-refresh.css dist/assets/
cp apple-redesign.css dist/apple-redesign.css
cp reference-contract.js dist/reference-contract.js
