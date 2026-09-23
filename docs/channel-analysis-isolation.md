# Uploaded-file analyzer isolation gate

Complex PDF, Office, image, archive, and executable parsers must not run in the
credentialed sbxloop API process or in a normal agent sandbox. Their input is
untrusted, and the agent sandbox may hold an inference credential and network
access. This gate proves the disposable runtime boundary used by the PDF text
analyzer. Other rich formats still require their own bounded parsers and
real-sandbox CI checks.

The `Channel analysis isolation` workflow boots a fresh Docker Sandbox shell VM
with one CPU, 512 MiB of memory, a 30-second probe deadline, a disposable
writable scratch workspace and a separate read-only mount containing the inert
ELF fixture in `tests/fixtures/channel_analysis`, shared skills disabled, service credential variables explicitly cleared,
and a per-sandbox `**` network deny. The test checks that the input is readable
but cannot be changed, sibling host files and output paths are invisible,
common service credentials and a host-only sentinel are absent, outbound TCP
is denied even when the host runner can connect, and no probe output lands on
the host. It removes the VM after the job.

The profile uses Docker's documented [read-only workspace mounts and optional
workspace omission](https://docs.docker.com/reference/cli/sbx/create/shell/),
[per-sandbox network deny](https://docs.docker.com/reference/cli/sbx/policy/deny/network/),
and [VM host-filesystem isolation](https://docs.docker.com/ai/sandboxes/security/defaults/).
CI must pass on the actual sbx release; source review or a fake CLI test is not
equivalent. The workflow also invokes the production PDF runner on a two-page
fixture and verifies the page-two text. It installs sbx 0.43.0, matching the
current production default. A local deny can narrow policy, so the
probe also sets a deny-all policy and an explicit global network deny in the
dedicated analysis app. The probe requires real external HTTPS response
content: a transparent proxy can accept a TCP handshake before enforcing
policy, so a successful connection alone does not establish egress.

An uploaded PDF (identified by its `%PDF-` header, regardless of name) of at
most 20 MB gets a durable analysis job. The host verifies its stored checksum,
copies only that original and trusted parser code to a read-only mount, and
starts a separate `sbxloop-analysis` shell VM with the proven profile. The
worker has a 45-second execution deadline, 100-page limit, 10 MB content-stream
check per page, 8,000-character page limit and 240,000-character document limit.
The host validates its bounded JSON output before storing it in the database.
The job survives daemon restart and removes its VM on completion. The agent
reads saved text with `read_pdf_channel_input`, which checks current channel
membership and the turn's message snapshot every time. Extracted text remains
untrusted data. Scanned pages have no OCR text; encrypted PDFs and pages beyond
the limits report that limitation explicitly. Other file types retain generic
byte, search and string inspection.

The self-deploy workflow signs the production host into the separate
`sbxloop-analysis` app and initializes its deny-all policy before taking a
deploy hold; it fails before upgrading if either setup step fails. Operators
of other installations must run `sbx --app-name sbxloop-analysis login`,
`sbx --app-name sbxloop-analysis policy init deny-all`, and
`sbx --app-name sbxloop-analysis policy deny network '**'` once on their host.
The CI probe does not prove that a particular production host can start this
profile; that remains **field-unverified** until it is run there.
