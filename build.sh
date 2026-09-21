#!/bin/bash
set -e
rm -rf dist
mkdir -p dist
cp index.html dist/index.html
cp apple-redesign.css dist/apple-redesign.css
cp reference-contract.js dist/reference-contract.js
