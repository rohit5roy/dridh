#!/usr/bin/env bash
# Push HEAD to a branch, open a pull request into main and enable squash auto-merge.
#
# main is protected (pull request, signed commits, required checks), so release commits cannot be pushed to it.
# RELEASE_BOT_TOKEN is used because pushes and pull requests made with the default GITHUB_TOKEN do not start the
# required checks, so the pull request would never merge.
#
# Usage: release_pr.sh <branch> <title> [wait]
#   wait: block until the pull request has merged and print the merge commit SHA on the last line.
set -euo pipefail

branch=$1
title=$2
mode=${3:-}
: "${RELEASE_BOT_TOKEN:?RELEASE_BOT_TOKEN is not set; create it and add it as a repository secret}"

auth=$(printf 'x-access-token:%s' "$RELEASE_BOT_TOKEN" | base64 | tr -d '\n')
# actions/checkout already stores the job's GITHUB_TOKEN as an extra Authorization header for this host. Adding a second
# one makes git send both, and GitHub rejects the push (HTTP 400, git exits 128). The empty value clears the inherited
# header first, so only RELEASE_BOT_TOKEN is sent.
header="http.${GITHUB_SERVER_URL:-https://github.com}/.extraheader"
git -c "$header=" -c "$header=AUTHORIZATION: basic $auth" push -q origin "HEAD:refs/heads/$branch"

export GH_TOKEN=$RELEASE_BOT_TOKEN
url=$(gh pr create --base main --head "$branch" --title "$title" --body "Automated release step. It merges once CI and the CLA check pass.")
gh pr merge --auto --squash "$url"

if [ "$mode" = wait ]; then
  for _ in $(seq 1 120); do
    state=$(gh pr view "$url" --json state --jq .state)
    if [ "$state" = MERGED ]; then
      gh pr view "$url" --json mergeCommit --jq .mergeCommit.oid
      exit 0
    fi
    if [ "$state" = CLOSED ]; then
      echo "::error::$url was closed without merging"; exit 1
    fi
    sleep 30
  done
  echo "::error::$url did not merge within 60 minutes"; exit 1
fi
echo "$url"
