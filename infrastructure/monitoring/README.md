# Host resource admission

These files are operational templates. Nothing installs or enables a service.
Phase 2 implements cloud generation behind disabled paid gates; narration and
rendering remain off. Host checks are required before any paid submission or clip
download, as well as at worker startup. Test fixtures are not ERP workload evidence.

An operator must supply the protected workload, PostgreSQL and Nginx service
units with `--erp-unit`, `--postgres-unit` and `--nginx-unit`, and a reviewed
HTTPS health endpoint with `--erp-health-url`. All four arguments are required;
there are no production targets or unit defaults. The example service uses
synthetic `erp.example.test` and `erp.service` values and must be adapted.
Credentials, query strings, fragments and redirects are refused. The collector
never reads environment files, process arguments, database records or full proxy
settings, and does not log response bodies or cookies.

Prepare a root-owned directory such as `/var/lib/video-studio` (0755) and run the
collector there. It reads `/proc/meminfo`, filesystem capacity, load averages,
CPU count and protected service status. Review the example units before installing.

Production Compose requires `VIDEO_HOST_SIGNAL_FILE` to name that existing
JSON file. Only the file is mounted read-only; no host directory, socket or ERP
path enters a container. Keep this file's inode stable when refreshing it. The
collector locks and rewrites it in place because replacing a file would leave
the container's bind mount reading the old inode.

The format is `schema_version: 1`, UTC `observed_at`, integer
`memory_available_bytes`, `root_free_bytes`, `cpu_count`, numeric `load1`, and
`services` booleans named `erp`, `postgresql` and `nginx`, plus
`erp_http_status` and finite `erp_response_ms`. Missing or malformed
data, signals older than 90 seconds or more than five seconds in the future,
any unhealthy protected service, available host memory below 2 GiB, root free
space below 15 GiB, one-minute load above 75% of logical CPUs, an ERP login HTTP
status other than 200, or ERP response time above 1000 ms refuse admission.
These provisional thresholds require measurements during a later isolated load
test. They are not evidence that heavy generation fits alongside ERP.

`admission.evaluate()` provides the fail-closed check. Worker startup uses it.
Generation submission and result-download tasks call the same policy at execution
time. Later rendering must also monitor pressure during bounded work; startup
validation alone cannot protect a long-running render. Missing signals in
development deliberately refuse workers. API, database and UI can run independently.

The Compose service ceilings total 1.45 CPUs and 2432 MiB including all three
opt-in workers and UI. Tmpfs counts inside the corresponding memory limit.
The optional collector is separately capped to 5% CPU and 64 MiB, giving an
aggregate ceiling of 1.50 CPUs and 2496 MiB. These caps
do not reserve resources for ERP and do not limit persistent-volume growth.
Track host pressure, storage and container restart counts before any rollout.
