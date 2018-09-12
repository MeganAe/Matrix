#!/usr/bin/env bash

set -e

# CircleCI doesn't give CIRCLE_PR_NUMBER in the environment for non-forked PRs. Wonderful.
# In this case, we just need to do some ~shell magic~ to strip it out of the PULL_REQUEST URL.
echo 'export CIRCLE_PR_NUMBER="${CIRCLE_PR_NUMBER:-${CIRCLE_PULL_REQUEST##*/}}"' >> "$BASH_ENV"
source $BASH_ENV

if [[ -z "${CIRCLE_PR_NUMBER}" ]]
then
    echo "Can't figure out what the PR number is!"
    exit 1
fi

# Get the reference, using the GitHub API
GITBASE=`curl -q https://api.github.com/repos/matrix-org/synapse/pulls/${CIRCLE_PR_NUMBER} | jq -r '.base.ref'`

# Show what we are before
git show -s

# Fetch and merge. If it doesn't work, it will raise due to set -e.
git fetch -u origin $GITBASE
git merge --no-edit origin/$GITBASE

# Show what we are after.
git show -s