#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=${1:-"$project_dir/dist/birdnet-go-trmnl-recipe.zip"}

mkdir -p "$(dirname -- "$output")"
rm -f "$output"
cd "$project_dir/recipe/src"
zip -q "$output" settings.yml full.liquid half_horizontal.liquid half_vertical.liquid quadrant.liquid shared.liquid
echo "$output"
