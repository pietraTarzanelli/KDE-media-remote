#!/usr/bin/env bash
# Porta le due verticali alla stessa dimensione (890x1550, ritaglio
# centrale) così si allineano nel README senza essere deformate.
set -euo pipefail

cd "$(dirname "$0")"

if command -v magick >/dev/null 2>&1; then
    convert_bin=magick
else
    convert_bin=convert
fi

for file in homekde.jpg keyboardkde.jpg; do
    "$convert_bin" -quiet "$file" -resize 890x1550^ \
        -gravity center -extent 890x1550 -quality 90 "$file.tmp"
    mv "$file.tmp" "$file"
done

identify -format '%f  %wx%h\n' homekde.jpg keyboardkde.jpg
