"""Application configuration.

All settings are overridable via environment variables (see field names,
upper-cased) or a local `.env` file. Defaults assume a container layout where
the validator jar lives at /opt/validator/validator_cli.jar and the FHIR
package/terminology cache lives under the process's home directory
(~/.fhir), which should be mounted as a persistent volume in production so
that downloaded IGs and cached ValueSets survive restarts.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # --- Validator engine process ---
    validator_jar_path: str = "/opt/validator/validator_cli.jar"
    validator_host: str = "127.0.0.1"
    validator_port: int = 8081
    auto_start_validator: bool = True
    """Set to False in tests/dev environments where java/the jar are unavailable."""

    # --- Validation engine options (only settable at engine startup, per
    # the validator's `server` subcommand -- see AGENTS.md) ---
    fhir_version: str = "4.0"
    startup_igs: str = ""
    """Comma-separated IG packages (e.g. "hl7.fhir.us.core#5.0.1,hl7.fhir.uv.ips")
    preloaded when the engine starts. Note: pydantic-settings would otherwise expect
    list-typed env vars to be JSON-encoded, so this is kept as a plain string and
    split via `startup_igs_list` for a simpler operator experience."""
    terminology_server: str | None = None
    """Passed as `-tx <url>`. Leave unset to use the validator's default (tx.fhir.org)."""
    terminology_cache_dir: str = ""
    """Directory for terminology-server responses and session state.

    Empty uses a service-specific directory under `~/.fhir` so another
    validator implementation cannot reuse this service's tx.fhir.org cache
    session by accident. Use `n/a` to disable terminology caching in the
    validator, if supported by the selected CLI version.
    """
    clear_terminology_cache_on_startup: bool = False
    """Pass `-clear-tx-cache` on startup to discard stale terminology state."""
    snomed_edition: str | None = "us"
    """Passed as `-sct <edition>`. Valid choices (per the validator's own `-sct` option):
    intl | us | uk | au | nl | ca | se | dk | es. Defaults to "us" so SNOMED CT valueset/
    codesystem validation resolves against the US edition rather than the validator's own
    default (International edition) -- set to None/empty to fall back to that default."""
    validator_extra_args: str = ""
    """Comma-separated additional raw CLI args appended verbatim to the `server` subcommand."""
    packages_dir: str = "packages"
    """Directory of pre-extracted `<packageId>#<version>/package/...` folders for
    "local IGs" -- packages not published on the FHIR package registry, e.g.
    draft/ballot/ci-build versions (see app/package_cache.py) -- copied into the
    shared FHIR package cache (`~/.fhir/packages`) before the engine starts, so
    those IGs never need a network fetch. Relative paths are resolved against the
    process's current working directory. A missing directory is a no-op, not an
    error -- this is optional."""
    load_cached_packages_on_startup: bool = True
    """After the engine reports ready, load every package already present in the FHIR
    package cache (not just the ones from `packages_dir`/`startup_igs`) into the running
    engine via POST /loadIG, so anything sitting in the cache is immediately usable rather
    than lazily loaded on first reference. Best-effort: a package that fails to load (e.g.
    a stale/conflicting version left over from unrelated prior use of a shared cache
    directory) is logged and skipped -- it does not abort startup or block other packages.
    Set to False to go back to purely lazy, on-demand loading."""

    # --- GET /fhir/$packages, and ensured present in the FHIR package cache
    # (and loaded into the running engine) at startup -- see
    # ValidatorEngine.load_configured_packages(). Packages also found in
    # `packages_dir` ("local IGs" -- not published on the FHIR package
    # registry) are copied from disk; everything else listed here is
    # fetched from the registry over the network. Of the 7 below, only
    # hl7.fhir.us.vdor#0.1.1-cibuild and hl7.fhir.us.mdi#3.0.0-draft are
    # unpublished (verified against packages.fhir.org) and need to live in
    # packages_dir; the rest are fetched. ---
    packages: str = (
        "hl7.fhir.us.vr-common-library#2.0.0,"
        "hl7.fhir.us.vdor#0.1.1-cibuild,"
        "hl7.fhir.us.mdi#3.0.0-draft,"
        "hl7.fhir.us.mdi#2.0.0,"
        "hl7.fhir.us.bser#2.0.0-ballot,"
        "hl7.fhir.us.vrdr#3.0.0,"
        "hl7.fhir.us.core#5.0.1"
    )
    """Comma-separated `<packageId>#<version>` list (see `packages_list` below
    for the parsed form). This Python-level value is only a fallback -- the
    canonical source is the `PACKAGES` line in `.env` (see `.env.example`),
    specifically so operators can edit the list without rebuilding the
    image: pass it via `docker run --env-file .env` (or mount `.env` into
    the container at `/app/.env`) rather than baking it into the image."""
    default_ig: str = ""
    """The primary `<packageId>#<version>` IG for this deployment (see
    `.env.example`). Loaded into the engine at startup like `packages`
    above; the validator automatically resolves and fetches *its* declared
    dependencies too (recursively), from the registry if not already
    cached, per the FHIR Package Cache spec's recursive resolution rules --
    we don't need any extra dependency-walking code of our own for this.
    Empty (default) disables it."""

    initial_load_resource_path: str = "initial_load_resource.json"
    """Path to a single FHIR resource (JSON or XML, by extension) validated
    at startup and periodically after DEFAULT_IG (and its dependencies) finish
    loading, as a warm-up/smoke test of that specific engine+IG combination --
    see `ValidatorEngine.validate_initial_load_resource()`. Relative paths are
    resolved against the process's current working directory. A missing file,
    or an empty `default_ig`, is a no-op, not an error -- this is optional and
    never blocks startup."""
    validate_initial_load_resource_on_startup: bool = True
    """Set to False to skip the startup warm-up validation above."""

    validator_keepalive_interval_seconds: float = 300.0
    """Run the initial-load resource validation periodically to keep the
    terminology-server cache/session active. Values less than or equal to zero
    disable the keepalive task."""

    ci_build_repos: str = ""
    """Comma-separated `<packageId>#<version>=<Org-or-User>/<Repo-Name>`
    entries (e.g. "hl7.fhir.us.vdor#0.1.1-cibuild=HL7/fhir-vdor"). Used as
    the *second* tier of the fallback chain `load_configured_packages()`
    applies to each entry in `packages`/`default_ig`: 1) the FHIR package
    registry, 2) for versions that look like drafts/ci-builds (see
    `app/ci_build.py::is_ci_build_version`) and have an entry here, a direct
    download of `https://build.fhir.org/ig/<Org-or-User>/<Repo-Name>/package.tgz`,
    3) the local `packages_dir` folder. Packages not listed here skip
    straight from tier 1 to tier 3."""

    @property
    def startup_igs_list(self) -> list[str]:
        return [ig.strip() for ig in self.startup_igs.split(",") if ig.strip()]

    @property
    def terminology_cache_dir_path(self) -> str:
        configured = self.terminology_cache_dir.strip()
        if configured:
            return str(Path(configured).expanduser())
        return str(Path.home() / ".fhir" / "validator-service" / "terminology-cache")

    @property
    def validator_extra_args_list(self) -> list[str]:
        return [arg.strip() for arg in self.validator_extra_args.split(",") if arg.strip()]

    @property
    def packages_list(self) -> list[str]:
        return [pkg.strip() for pkg in self.packages.split(",") if pkg.strip()]

    @property
    def ci_build_repos_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for entry in self.ci_build_repos.split(","):
            pkg, _, repo = entry.strip().partition("=")
            pkg, repo = pkg.strip(), repo.strip()
            if pkg and repo:
                mapping[pkg] = repo
        return mapping

    # --- Process lifecycle ---
    validator_startup_timeout_seconds: float = 300.0
    """Cold start (downloading/parsing IGs) can take minutes on first boot."""
    validator_request_timeout_seconds: float = 120.0
    auto_recover_validator: bool = True
    """Schedule an in-process validator restart after a transport failure."""
    validator_recovery_max_attempts: int = 3
    """Maximum consecutive restart attempts for one recovery event."""
    validator_recovery_backoff_seconds: float = 5.0
    """Delay before retrying a failed recovery attempt."""
    validator_recovery_backoff_multiplier: float = 2.0
    """Multiplier applied to the recovery retry delay after each failure."""
    validator_recovery_operation_drain_timeout_seconds: float = 10.0
    """Maximum time to wait for in-flight engine requests before restarting."""

    # --- HTTP API ---
    default_response_format: str = "application/fhir+json"

    cors_allow_origins: str = "*"
    """Comma-separated list of allowed CORS origins (e.g.
    "https://a.example.com,https://b.example.com"). Defaults to "*" (any
    origin). This also makes FastAPI/Starlette respond to CORS preflight
    `OPTIONS` requests on every route -- see `CORSMiddleware` in app/main.py."""

    log_level: str = "DEBUG"

    custom_path: str = ""
    """Optional path prefix (e.g. "/fhir-validator") prepended to the API docs
    URLs (`/docs`, `/redoc`, `/openapi.json`) -- useful when this service is
    deployed behind a reverse proxy under a non-root path. Leading/trailing
    slashes are normalized. Empty (default) leaves the docs URLs unprefixed."""

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def custom_path_normalized(self) -> str:
        stripped = self.custom_path.strip().strip("/")
        return f"/{stripped}" if stripped else ""
