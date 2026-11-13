#!/usr/bin/env bash
# Sounding installer
#   curl -sSL https://sounding.pathvector.dev/install.sh | bash
#
# Clones the Sounding repo, writes a .env with generated secrets, copies the example profile,
# then builds and starts the stack with docker compose. Run it again to update (it never touches .env).
#
# Environment overrides:
#   SOUNDING_DIR         install directory   (default: ~/sounding)
#   SOUNDING_REPO        git URL or path     (default: https://github.com/pathvector-studio/sounding.git)
#   SOUNDING_REF         branch or tag       (default: the latest release tag vX.Y.Z, or main if there is none)
#   SOUNDING_PORT        port on 127.0.0.1   (default: 8080; only used when .env is created)
#   SOUNDING_NO_START    set to 1 to prepare files only, without starting containers
#   SOUNDING_INSTANCE    only used when .env is created: sets SOUNDING_INSTANCE and COMPOSE_PROJECT_NAME in .env
#                        (container and compose-project names), to run a second instance on one machine
#   SOUNDING_NONINTERACTIVE
#                        set to 1 to behave as if there were no terminal (never ask, use the answers below)
# Answers to the questions (skip the question; only used when .env is created):
#   SOUNDING_FETCH_CONTACT   contact e-mail address for fetching (empty: IETF fetching stays off)
#   SOUNDING_LLM_PROVIDER    none | anthropic | openai
# The LLM API key is never read from an environment variable: it is typed at the terminal and written only to .env.
# Without a terminal and without these variables: no questions, contact empty, LLM provider none.
set -euo pipefail

# Wrapped in main so a truncated download never runs half a script.
main() {
  local dir="${SOUNDING_DIR:-$HOME/sounding}"
  local repo="${SOUNDING_REPO:-https://github.com/pathvector-studio/sounding.git}"
  local ref="${SOUNDING_REF:-}"

  if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
  else
    BOLD=''; RED=''; GRN=''; YLW=''; RST=''
  fi
  info() { printf '%s==>%s %s\n' "$BOLD" "$RST" "$*"; }
  ok()   { printf '%s ✓%s %s\n' "$GRN" "$RST" "$*"; }
  warn() { printf '%s !%s %s\n' "$YLW" "$RST" "$*" >&2; }
  die()  { printf '%s ✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

  gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 32
    else
      od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    fi
  }

  # Highest vX.Y.Z tag of the repo (pre-releases such as v1.0.0-rc1 are skipped), or nothing.
  latest_tag() {
    git ls-remote --tags --refs --sort=-v:refname "$repo" 'refs/tags/v*' \
      | awk '{ sub("^refs/tags/", "", $2) } $2 ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/ { print $2; exit }'
  }

  # Replace KEY=... in .env (or append it). Only shell builtins touch the value, so a secret never appears in the
  # argument list of another process (`ps`). Writes through: .env may be a symlink to a file outside the checkout.
  set_env() {
    local key="$1" value="$2" tmp line found=0
    tmp="$(mktemp .env.XXXXXX)"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "${key}="*) printf '%s=%s\n' "$key" "$value"; found=1 ;;
        *) printf '%s\n' "$line" ;;
      esac
    done < .env > "$tmp"
    if [ "$found" = 0 ]; then printf '%s=%s\n' "$key" "$value" >> "$tmp"; fi
    cat "$tmp" > .env
    rm -f "$tmp"
    chmod 600 .env
  }

  get_env() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | tr -d '"' || true; }

  # The questions are read from /dev/tty so they also work under `curl | bash`.
  have_tty() {
    [ "${SOUNDING_NONINTERACTIVE:-0}" != "1" ] && [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null
  }

  # FETCH_CONTACT: IETF asks fetchers to identify themselves. Empty keeps IETF fetching off (RSS still runs).
  choose_contact() {
    local contact=""
    if [ "${SOUNDING_FETCH_CONTACT+set}" = set ]; then
      contact="$SOUNDING_FETCH_CONTACT"
    elif have_tty; then
      printf '\n%sContact address for fetching (needed for IETF mailing lists)%s\n' "$BOLD" "$RST"
      printf '  IETF asks automated readers to identify themselves. This address is sent to IETF as the anonymous IMAP\n'
      printf '  password and in the HTTP User-Agent. It is not where the digest is sent.\n'
      printf '  Leave empty to skip: IETF fetching stays off until you set FETCH_CONTACT in .env (RSS still runs).\n'
      printf 'E-mail address: '
      read -r contact </dev/tty || contact=""
    fi
    case "$contact" in
      *[[:space:]]*) warn "The address contains spaces; leaving FETCH_CONTACT empty."; contact="" ;;
      ''|*@*) ;;
      *) warn "'${contact}' is not an e-mail address; leaving FETCH_CONTACT empty."; contact="" ;;
    esac
    set_env FETCH_CONTACT "$contact"
    if [ -n "$contact" ]; then
      ok "FETCH_CONTACT set"
    else
      warn "FETCH_CONTACT is empty: IETF mailing lists are not fetched until you set it in .env (RSS still runs)."
    fi
  }

  # LLM provider and key. none (default) sends nothing anywhere. The key is read without echo and written only to .env.
  choose_llm() {
    local provider="${SOUNDING_LLM_PROVIDER:-}" asked=0 key="" cheap="" main_model="" base=""
    if [ -z "$provider" ] && have_tty; then
      asked=1
      printf '\n%sSummaries with an LLM (optional)%s\n' "$BOLD" "$RST"
      printf '  With a provider chosen, the worker summarizes each IETF conversation daily. What is sent to that provider:\n'
      printf '  subjects, preprocessed message bodies (e-mail addresses masked) and display names. Nothing else.\n'
      printf '  You pay the provider with your own key; LLM_DAILY_BUDGET_USD (default 1.00) stops the day'"'"'s spending.\n'
      printf '  none = nothing is sent anywhere (the default); you can change it later in .env.\n'
      printf 'Provider [none/anthropic/openai] (none): '
      read -r provider </dev/tty || provider=""
    fi
    case "$provider" in
      ''|none) provider=none ;;
      anthropic) provider=anthropic ;;
      openai) provider=openai ;;
      *)
        if [ "$asked" = 1 ]; then
          warn "Unknown provider '${provider}'; using none."
          provider=none
        else
          die "SOUNDING_LLM_PROVIDER must be none, anthropic or openai."
        fi
        ;;
    esac
    set_env LLM_PROVIDER "$provider"
    if [ "$provider" = none ]; then
      ok "LLM: none (nothing is sent anywhere; set LLM_PROVIDER and LLM_API_KEY in .env to enable summaries)"
      return 0
    fi
    if [ "$asked" = 1 ]; then
      printf 'API key (input is hidden, stored only in .env; empty = set it later): '
      read -rs key </dev/tty || key=""
      printf '\n'
      case "$key" in
        *[[:space:]\"\'\$\`\\]*) warn "The key contains characters that are not allowed (spaces, quotes, \$, \`, \\); not saved."; key="" ;;
      esac
      if [ -n "$key" ]; then set_env LLM_API_KEY "$key"; fi
      key=""
      if [ "$provider" = openai ]; then
        printf 'Cheap model name (for long-conversation splitting): '
        read -r cheap </dev/tty || cheap=""
        printf 'Main model name (for "where it stands"): '
        read -r main_model </dev/tty || main_model=""
        printf 'API base URL (empty = OpenAI default; for OpenAI-compatible servers): '
        read -r base </dev/tty || base=""
        if [ -n "$cheap" ]; then set_env LLM_MODEL_CHEAP "$cheap"; fi
        if [ -n "$main_model" ]; then set_env LLM_MODEL_MAIN "$main_model"; fi
        if [ -n "$base" ]; then set_env LLM_API_BASE "$base"; fi
        if [ -z "$cheap" ] || [ -z "$main_model" ]; then
          warn "openai needs both LLM_MODEL_CHEAP and LLM_MODEL_MAIN; summaries stop with config_error until they are set in .env."
        fi
      fi
    fi
    case "$(get_env LLM_API_KEY)" in
      ''|change-me*) warn "LLM_PROVIDER=${provider} but no API key is stored: set LLM_API_KEY in .env, then: docker compose up -d" ;;
      *) ok "LLM: ${provider} (key stored in .env only)" ;;
    esac
  }

  port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

  printf '\n%s📡 Sounding installer%s\n\n' "$BOLD" "$RST"

  # 1. Requirements
  info "Checking requirements"
  command -v git >/dev/null 2>&1 || die "git is required."
  command -v docker >/dev/null 2>&1 \
    || die "Docker is not installed. Get it at https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 \
    || die "Docker Compose v2 is required ('docker compose'). Update Docker or install the compose plugin."
  docker info >/dev/null 2>&1 \
    || die "Cannot reach the Docker daemon. Start Docker (or add your user to the 'docker' group) and retry."
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), compose $(docker compose version --short 2>/dev/null || echo 'v2')"

  # 2. Source
  if [ -z "$ref" ]; then
    ref="$(latest_tag)" || die "Cannot reach ${repo}. Check your network and retry."
    ref="${ref:-main}"
  fi
  if [ -d "$dir/.git" ]; then
    info "Updating existing checkout in ${dir}"
    if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
      die "${dir} has local changes. Commit or stash them, then retry."
    fi
    git -C "$dir" fetch -q --depth 1 "$repo" "$ref"
    git -C "$dir" checkout -q FETCH_HEAD
  elif [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    die "${dir} exists and is not a Sounding checkout. Set SOUNDING_DIR to another path."
  else
    info "Cloning ${repo} (${ref}) into ${dir}"
    git clone -q --depth 1 --branch "$ref" "$repo" "$dir"
  fi
  cd "$dir"
  if [ ! -f docker-compose.yml ] || [ ! -f .env.example ]; then
    die "docker-compose.yml or .env.example missing in ${dir}"
  fi
  ok "Source ready at $(pwd)"

  # 3. .env
  if [ -f .env ]; then
    ok ".env already exists, leaving it untouched"
  else
    local port="${SOUNDING_PORT:-8080}"
    case "$port" in ''|*[!0-9]*) die "SOUNDING_PORT must be a number." ;; esac
    local instance="${SOUNDING_INSTANCE:-}"
    case "$instance" in
      *[!a-z0-9_-]*) die "SOUNDING_INSTANCE may contain only a-z, 0-9, '-' and '_' (it becomes a container and compose-project name)." ;;
    esac
    cp .env.example .env
    chmod 600 .env
    set_env POSTGRES_PASSWORD "$(gen_secret)"
    set_env WEB_DB_PASSWORD "$(gen_secret)"
    set_env SESSION_SECRET "$(gen_secret)"
    set_env IDENTITY_SECRET "$(gen_secret)"
    set_env SOUNDING_PORT "$port"
    set_env PUBLIC_BASE_URL "http://localhost:${port}"
    if [ -n "$instance" ]; then
      set_env SOUNDING_INSTANCE "$instance"
      set_env COMPOSE_PROJECT_NAME "$instance"
    fi
    # The containers run as APP_UID:APP_GID. On Linux, use the installing user's IDs.
    # Docker Desktop (macOS / Windows) maps file ownership itself; keep 1000.
    if [ "$(uname -s)" = "Linux" ]; then
      local uid gid
      uid="$(id -u)"; gid="$(id -g)"
      # IDs below 1000 (root, system users) would clash with accounts in the images.
      if [ "$uid" -ge 1000 ]; then set_env APP_UID "$uid"; fi
      if [ "$gid" -ge 1000 ]; then set_env APP_GID "$gid"; fi
    fi
    choose_contact
    choose_llm
    ok "Created .env with generated secrets"
  fi
  local k
  for k in POSTGRES_PASSWORD WEB_DB_PASSWORD SESSION_SECRET IDENTITY_SECRET; do
    case "$(get_env "$k")" in
      *change-me*) warn "${k} in .env still contains a change-me placeholder. Replace it (openssl rand -hex 32) before starting." ;;
    esac
  done

  # 4. Profile (which lists, feeds and keywords to follow)
  if [ -f config/profile.yaml ]; then
    ok "config/profile.yaml already exists, leaving it untouched"
  elif [ -f config/profile.example.yaml ]; then
    cp config/profile.example.yaml config/profile.yaml
    ok "Created config/profile.yaml from the example"
  else
    warn "config/profile.example.yaml is missing; create config/profile.yaml yourself."
  fi

  # 5. Start
  local port
  port="$(get_env SOUNDING_PORT)"; port="${port:-8080}"

  if [ "${SOUNDING_NO_START:-0}" = "1" ]; then
    info "SOUNDING_NO_START=1, skipping start. Run: cd ${dir} && docker compose up -d --build --wait"
    return 0
  fi
  # A compose project name is global on this machine: another Sounding checkout with the same name would have its
  # containers replaced by `up` here. Refuse that.
  local project here there
  project="$(get_env COMPOSE_PROJECT_NAME)"; project="${project:-sounding}"
  here="$(pwd -P)"
  there="$(docker ps -a --filter "label=com.docker.compose.project=${project}" \
             --format '{{.Label "com.docker.compose.project.working_dir"}}' | sort -u)"
  if [ -n "$there" ]; then
    local d
    while IFS= read -r d; do
      if [ "$(cd "$d" 2>/dev/null && pwd -P || echo "$d")" != "$here" ]; then
        die "Another Sounding (compose project '${project}') runs from ${d}. Stop it there, or give this one its own names in ${dir}/.env: SOUNDING_INSTANCE=sounding-2, COMPOSE_PROJECT_NAME=sounding-2, another SOUNDING_PORT and PUBLIC_BASE_URL (or delete .env and rerun with SOUNDING_INSTANCE=sounding-2 SOUNDING_PORT=<free port>)."
      fi
    done <<< "$there"
  elif port_in_use "$port"; then
    die "Port ${port} is already in use. Set SOUNDING_PORT and PUBLIC_BASE_URL in ${dir}/.env to another port (or delete .env and rerun with SOUNDING_PORT=<free port>), then retry."
  fi
  info "Building and starting Sounding (the first build takes a few minutes)"
  # sounding-worker and sounding-web are the same image (sounding-app:local). Build it once, without build
  # attestations, so the image id stays the same when nothing changed and a re-run does not recreate the running
  # containers (building it once per service would retag it twice).
  export BUILDX_NO_DEFAULT_ATTESTATIONS=1
  docker compose build sounding-worker \
    || die "The image build failed. See the output above."
  docker compose up -d --no-build --wait --wait-timeout 300 \
    || die "Some services did not become healthy. See: cd ${dir} && docker compose ps && docker compose logs"

  printf '\n%s📡 Sounding is running:%s http://localhost:%s\n\n' "$GRN$BOLD" "$RST" "$port"
  printf 'Next steps (in %s):\n' "$dir"
  printf '  1. Choose what to follow: edit config/profile.yaml, then restart the worker:\n'
  printf '       docker compose restart sounding-worker\n'
  printf '  2. Set a login password (without one, anyone who can reach the port can read the digest):\n'
  printf '       docker compose exec sounding-worker python -m sounding hash-password\n'
  printf "     put the output in .env as WEB_PASSWORD_HASH='scrypt\$...' (single quotes), then: docker compose up -d\n"
  printf '  3. Open it beyond this machine (LAN, Tailscale, Cloudflare Tunnel) and set up backups: see the README\n'
  printf '     https://github.com/pathvector-studio/sounding#readme\n'
  case "$(get_env FETCH_CONTACT)" in
    ''|change-me*)
      printf '\n%sIETF mailing lists are not fetched until FETCH_CONTACT is set in .env (then: docker compose up -d). RSS already runs.%s\n' "$YLW" "$RST"
      ;;
  esac
  printf '\n'
}

main "$@"
