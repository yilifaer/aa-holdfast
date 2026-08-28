#!/usr/bin/env bash
#
# Sync this working copy to the Pi, then pull any migrations back.
#
# The pull-back matters: migrations get generated on the Pi (that is where the
# Django settings and database live), but this directory is the source of
# truth. Since a deploy replaces the package directory wholesale, a migration
# left only on the Pi is destroyed by the next deploy while the database still
# records it as applied -- which breaks the app until it is regenerated.
#
# Usage:  ./deploy.sh [--restart]
set -euo pipefail

PI=pi
REMOTE_PKG=/home/allianceserver/aa-holdfast
VENV=/home/allianceserver/venv/auth/bin
MYAUTH=/home/allianceserver/myauth

echo ">> pushing"
tar czf - holdfast pyproject.toml README.md | ssh -o BatchMode=yes "$PI" 'cat > /tmp/aa-holdfast.tgz'
ssh -o BatchMode=yes "$PI" "sudo -n bash -c '
  rm -rf $REMOTE_PKG/holdfast
  tar xzf /tmp/aa-holdfast.tgz -C $REMOTE_PKG
  chown -R allianceserver:allianceserver $REMOTE_PKG
'"

echo ">> checking for unmade migrations"
ssh -o BatchMode=yes "$PI" "sudo -n -u allianceserver bash -c '
  cd $MYAUTH && $VENV/python manage.py makemigrations holdfast --check --dry-run
'" >/dev/null 2>&1 && echo "   none pending" || {
  echo "   model changes detected, generating"
  ssh -o BatchMode=yes "$PI" "sudo -n -u allianceserver bash -c '
    cd $MYAUTH && $VENV/python manage.py makemigrations holdfast
  '" 2>&1 | grep -vE '^\?:|HINT|^WARNINGS|^System check|^$' || true
}

echo ">> pulling migrations back"
# Note the root shell: the glob has to expand as root, since the login user
# cannot read the allianceserver home directory.
for name in $(ssh -o BatchMode=yes "$PI" "sudo -n bash -c 'ls $REMOTE_PKG/holdfast/migrations/*.py'" | xargs -n1 basename); do
  [ "$name" = "__init__.py" ] && continue
  ssh -o BatchMode=yes "$PI" "sudo -n bash -c 'cat $REMOTE_PKG/holdfast/migrations/$name'" > "holdfast/migrations/$name.tmp"
  if [ -s "holdfast/migrations/$name.tmp" ]; then
    mv "holdfast/migrations/$name.tmp" "holdfast/migrations/$name"
  else
    rm -f "holdfast/migrations/$name.tmp"
    echo "   !! $name came back empty, left local copy alone"
  fi
done
ls holdfast/migrations/*.py | sed 's/^/   /'

if [ "${1:-}" = "--restart" ]; then
  echo ">> migrating and restarting"
  ssh -o BatchMode=yes "$PI" "sudo -n -u allianceserver bash -c '
    cd $MYAUTH && $VENV/python manage.py migrate holdfast
  '" 2>&1 | tail -3
  ssh -o BatchMode=yes "$PI" 'sudo -n supervisorctl restart myauth:' | tail -6
fi

echo ">> done"
