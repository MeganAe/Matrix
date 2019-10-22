#!/bin/bash
# Find linting errors in Synapse's default config file.
# Exits with 0 if there are no problems, or another code otherwise.

# Fix non-lowercase true/false values
sed -i -E "s/: +True/: true/g; s/: +False/: false/g;" docs/sample_config.yaml

# Check if anything changed
git diff --exit-code docs/sample_config.yaml
