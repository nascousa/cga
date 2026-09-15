#!/bin/sh
set -eu
umask 077
LC_ALL=C
export LC_ALL

BACKUP_ROOT=${BACKUP_ROOT:-/backups}
BACKUP_STACK_NAME=${BACKUP_STACK_NAME:-cga}
BACKUP_INTERVAL_SECONDS=${BACKUP_INTERVAL_SECONDS:-3600}
BACKUP_KEEP_COUNT=${BACKUP_KEEP_COUNT:-168}
BACKUP_LOCK_TIMEOUT_SECONDS=${BACKUP_LOCK_TIMEOUT_SECONDS:-300}
BACKUP_RUN_ONCE=${BACKUP_RUN_ONCE:-0}
PGHOST=${PGHOST:-postgres}
PGPORT=${PGPORT:-5432}
PGUSER=${PGUSER:-app}
PGDATABASE=${PGDATABASE:-appdb}
PGPASSWORD=${PGPASSWORD:-}
FALKORDB_HOST=${FALKORDB_HOST:-falkordb}
FALKORDB_PORT=${FALKORDB_PORT:-6379}
FALKORDB_DATA_DIR=${FALKORDB_DATA_DIR:-/falkordb-data}
FALKORDB_SERVER_DATA_DIR=${FALKORDB_SERVER_DATA_DIR:-/var/lib/falkordb/data}
FALKORDB_SNAPSHOT_TIMEOUT_SECONDS=${FALKORDB_SNAPSHOT_TIMEOUT_SECONDS:-300}
FALKORDB_PASSWORD=${FALKORDB_PASSWORD:-}
export PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD

check_integer() {
  case "$2" in
    ''|*[!0-9]*) echo "[backup] invalid $1: expected an integer >= $3" >&2; exit 1;;
  esac
  if ! [ "$2" -ge "$3" ] 2>/dev/null; then
    echo "[backup] invalid $1: expected an integer >= $3" >&2
    exit 1
  fi
}
check_integer BACKUP_KEEP_COUNT "$BACKUP_KEEP_COUNT" 1
check_integer BACKUP_INTERVAL_SECONDS "$BACKUP_INTERVAL_SECONDS" 1
check_integer BACKUP_LOCK_TIMEOUT_SECONDS "$BACKUP_LOCK_TIMEOUT_SECONDS" 0
check_integer FALKORDB_SNAPSHOT_TIMEOUT_SECONDS "$FALKORDB_SNAPSHOT_TIMEOUT_SECONDS" 1
check_integer FALKORDB_PORT "$FALKORDB_PORT" 1
case "$BACKUP_RUN_ONCE" in 0|1) ;; *) echo "[backup] invalid BACKUP_RUN_ONCE" >&2; exit 1;; esac
case "$BACKUP_STACK_NAME" in
  ''|.|..|*/*|*\\*) echo "[backup] invalid BACKUP_STACK_NAME" >&2; exit 1;;
esac
case "$FALKORDB_HOST" in ''|-*) echo "[backup] invalid FALKORDB_HOST" >&2; exit 1;; esac

AUTH_BACKUP_DIR="$BACKUP_ROOT/$BACKUP_STACK_NAME/auth"
FALKOR_BACKUP_DIR="$BACKUP_ROOT/$BACKUP_STACK_NAME/falkordb"
mkdir -p "$AUTH_BACKUP_DIR" "$FALKOR_BACKUP_DIR"

take_lock() {
  lock_dir="$1"
  if [ -d "$lock_dir" ]; then
    echo "[backup] legacy lock directory exists; stop old workers and verify it before removal" >&2
    return 1
  fi
  if ! command -v flock >/dev/null 2>&1; then
    echo "[backup] flock is required for crash-released backup locks" >&2
    return 1
  fi
  exec 9>> "$lock_dir" || return 1
  waited=0
  while ! flock -n -x 9; do
    if [ "$waited" -ge "$BACKUP_LOCK_TIMEOUT_SECONDS" ]; then
      echo "[backup] timed out waiting for active backup/restore lock $lock_dir" >&2
      return 1
    fi
    sleep 1 || return 1
    waited=$((waited + 1))
  done
  lock_owned=1
}

make_staging() {
  sequence=0
  while :; do
    candidate="$1/.in-progress-$$-$sequence"
    if mkdir "$candidate" 2>/dev/null; then
      stage="$candidate"
      return 0
    fi
    if [ ! -e "$candidate" ]; then
      echo "[backup] cannot create staging directory $candidate" >&2
      return 1
    fi
    sequence=$((sequence + 1))
  done
}

cleanup() {
  result=$?
  trap - 0
  if [ -n "$stage" ]; then
    rm -rf -- "$stage" || echo "[backup] cannot clean staging directory $stage" >&2
  fi
  if [ "$lock_owned" = 1 ]; then
    exec 9>&-
  fi
  exit "$result"
}

choose_snapshot() {
  timestamp=$(date -u +%Y%m%dT%H%M%SZ) || return 1
  sequence=0
  snapshot="$1/$2-$timestamp-$$-$sequence.$3"
  while [ -e "$snapshot" ]; do
    sequence=$((sequence + 1))
    snapshot="$1/$2-$timestamp-$$-$sequence.$3"
  done
}

publish_snapshot() {
  # Both renames stay on the backup filesystem. Never truncate a good latest.
  cp "$1" "$stage/latest" || return 1
  mv "$1" "$snapshot" || return 1
  mv "$stage/latest" "$2" || return 1
}

prune_backups() {
  # Patterns exclude latest, staging files and legacy SQLite snapshots.
  set -- "$1"/$2
  [ -f "$1" ] || return 0
  ls -1t -- "$@" > "$stage/retention" || return 1
  count=0
  while IFS= read -r file; do
    count=$((count + 1))
    if [ "$count" -gt "$BACKUP_KEEP_COUNT" ] && [ "$file" != "$snapshot" ]; then
      rm -f -- "$file" || return 1
    fi
  done < "$stage/retention"
}

backup_auth_db() (
  stage=
  lock_owned=0
  trap cleanup 0
  trap 'exit 1' HUP INT TERM
  take_lock "$AUTH_BACKUP_DIR/.auth.lock" || return 1
  make_staging "$AUTH_BACKUP_DIR" || return 1
  if ! command -v pg_dump >/dev/null 2>&1; then
    echo "[backup] pg_dump not installed; auth backup failed" >&2
    return 1
  fi
  # POSIX sh has no pipefail. Check dump and compression independently, and
  # leave pg_dump stderr visible rather than publishing a compressed failure.
  if ! pg_dump --no-owner --no-privileges --clean --if-exists --format=plain > "$stage/auth.sql"; then
    echo "[backup] pg_dump failed; previous auth backups retained" >&2
    return 1
  fi
  if ! gzip -c "$stage/auth.sql" > "$stage/auth.sql.gz"; then
    echo "[backup] auth compression failed; previous auth backups retained" >&2
    return 1
  fi
  gzip -t "$stage/auth.sql.gz" || return 1
  choose_snapshot "$AUTH_BACKUP_DIR" auth sql.gz || return 1
  if ! publish_snapshot "$stage/auth.sql.gz" "$AUTH_BACKUP_DIR/auth-latest.sql.gz"; then
    echo "[backup] auth publication failed" >&2
    return 1
  fi
  prune_backups "$AUTH_BACKUP_DIR" 'auth-2*.sql.gz' || return 1
  echo "[backup] auth snapshot -> $snapshot"
)

write_resp() {
  printf '*%s\r\n' "$#" || return 1
  for argument do
    printf '$%s\r\n%s\r\n' "${#argument}" "$argument" || return 1
  done
}

redis_command() {
  {
    if [ -n "$FALKORDB_PASSWORD" ]; then
      write_resp AUTH "$FALKORDB_PASSWORD" || return 1
    fi
    write_resp "$@" || return 1
    # QUIT makes BusyBox nc terminate on server EOF without a nonportable -q.
    write_resp QUIT || return 1
  } > "$stage/redis.request" || return 1
  if [ "$network_tool" = nc ]; then
    nc -w "$FALKORDB_SNAPSHOT_TIMEOUT_SECONDS" "$FALKORDB_HOST" "$FALKORDB_PORT" \
      < "$stage/redis.request" > "$stage/redis.response" || return 1
  else
    busybox nc -w "$FALKORDB_SNAPSHOT_TIMEOUT_SECONDS" "$FALKORDB_HOST" "$FALKORDB_PORT" \
      < "$stage/redis.request" > "$stage/redis.response" || return 1
  fi
  response=$(tr -d '\r' < "$stage/redis.response") || return 1
  if [ -n "$FALKORDB_PASSWORD" ]; then
    case "$response" in
      '+OK
'*) response=${response#*
};;
      *) echo "[backup] FalkorDB authentication failed" >&2; return 1;;
    esac
  fi
  printf '%s' "$response"
}

backup_falkordb() (
  stage=
  lock_owned=0
  trap cleanup 0
  trap 'exit 1' HUP INT TERM
  take_lock "$FALKOR_BACKUP_DIR/.falkordb.lock" || return 1
  make_staging "$FALKOR_BACKUP_DIR" || return 1
  if [ ! -d "$FALKORDB_DATA_DIR" ]; then
    echo "[backup] FalkorDB data directory not found at $FALKORDB_DATA_DIR" >&2
    return 1
  fi
  if command -v nc >/dev/null 2>&1; then
    network_tool=nc
  elif command -v busybox >/dev/null 2>&1 && busybox --list | grep -qx nc; then
    network_tool=busybox
  else
    echo "[backup] FalkorDB snapshot needs nc (provided by the postgres Alpine image's BusyBox)" >&2
    return 1
  fi

  expected=$(printf '*2\n$3\ndir\n$%s\n%s\n+OK' "${#FALKORDB_SERVER_DATA_DIR}" "$FALKORDB_SERVER_DATA_DIR")
  if ! reply=$(redis_command CONFIG GET dir) || [ "$reply" != "$expected" ]; then
    echo "[backup] FalkorDB CONFIG GET dir failed or mismatches the mounted data directory; refusing stale backup" >&2
    return 1
  fi
  expected=$(printf '*2\n$10\ndbfilename\n$8\ndump.rdb\n+OK')
  if ! reply=$(redis_command CONFIG GET dbfilename) || [ "$reply" != "$expected" ]; then
    echo "[backup] FalkorDB CONFIG GET dbfilename failed or is not dump.rdb; refusing stale backup" >&2
    return 1
  fi
  previous_identity=missing
  if [ -e "$FALKORDB_DATA_DIR/dump.rdb" ]; then
    if ! exec 3< "$FALKORDB_DATA_DIR/dump.rdb"; then
      echo "[backup] FalkorDB existing RDB cannot be opened" >&2
      return 1
    fi
    # Pin the old inode so subsequent automatic saves cannot recycle its ID.
    previous_identity=$(stat -Lc '%d:%i' /dev/fd/3) || return 1
  fi
  expected=$(printf '+OK\n+OK')
  # SAVE is synchronous: the first OK acknowledges completed RDB persistence.
  # A busy background save, timeout, Redis error, or partial reply is a failure,
  # not permission to archive an older dump. No freshness decision uses mtime.
  if ! reply=$(redis_command SAVE) || [ "$reply" != "$expected" ]; then
    echo "[backup] FalkorDB SAVE failed, busy, or timed out; previous graph backups retained: ${reply:-no complete response}" >&2
    return 1
  fi

  # A successful SAVE on the wrong container/volume is not enough. Redis
  # atomically replaces its RDB: require a new inode in the mounted directory.
  # Pin that inode before checking/copying, even if another save follows.
  if ! exec 4< "$FALKORDB_DATA_DIR/dump.rdb"; then
    echo "[backup] FalkorDB completed RDB is missing from the mounted volume" >&2
    return 1
  fi
  saved_identity=$(stat -Lc '%d:%i' /dev/fd/4) || return 1
  if [ "$saved_identity" = "$previous_identity" ]; then
    echo "[backup] FalkorDB SAVE did not replace the mounted RDB; check the volume mapping, refusing stale backup" >&2
    return 1
  fi
  if ! cat <&4 > "$stage/dump.rdb"; then
    echo "[backup] FalkorDB RDB copy failed" >&2
    return 1
  fi
  exec 3<&- 4<&-
  # Never tar a live directory containing AOF rewrites or in-progress RDB files.
  if [ "$(head -c 5 "$stage/dump.rdb")" != REDIS ]; then
    echo "[backup] FalkorDB snapshot does not contain an RDB header" >&2
    return 1
  fi
  if ! tar -czf "$stage/falkordb.tgz" -C "$stage" dump.rdb; then
    echo "[backup] FalkorDB archive failed; previous graph backups retained" >&2
    return 1
  fi
  choose_snapshot "$FALKOR_BACKUP_DIR" falkordb tgz || return 1
  if ! publish_snapshot "$stage/falkordb.tgz" "$FALKOR_BACKUP_DIR/falkordb-latest.tgz"; then
    echo "[backup] FalkorDB publication failed" >&2
    return 1
  fi
  prune_backups "$FALKOR_BACKUP_DIR" 'falkordb-2*.tgz' || return 1
  echo "[backup] FalkorDB RDB snapshot -> $snapshot"
)

backup_once() {
  result=0
  backup_auth_db || result=1
  backup_falkordb || result=1
  return "$result"
}

if [ "$BACKUP_RUN_ONCE" = 1 ]; then
  backup_once
  exit $?
fi

echo "[backup] starting periodic runtime backup loop for $BACKUP_STACK_NAME"
while :; do
  if ! backup_once; then
    echo "[backup] runtime backup cycle FAILED; inspect the errors above" >&2
  fi
  sleep "$BACKUP_INTERVAL_SECONDS"
done
