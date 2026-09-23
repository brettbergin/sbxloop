# Uploaded-file analyzer isolation gate

Complex PDF, Office, image, archive, and executable parsers must not run in the
credentialed sbxloop API process or in a normal agent sandbox. Their input is
untrusted, and the agent sandbox may hold an inference credential and network
access. This gate proves the minimum disposable runtime boundary before adding
those parsers; it does not itself enable a format analyzer.

The `Channel analysis isolation` workflow boots a fresh Docker Sandbox shell VM
with one CPU, 512 MiB of memory, a 30-second probe deadline, a read-only mount
containing the inert ELF fixture in `tests/fixtures/channel_analysis`, shared
skills disabled, no injected credentials,
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
equivalent. A local deny can narrow policy, so the probe works even when the
host's global policy is `balanced`.

This is an isolation prerequisite, not an analyzer service. The next change
must create a production job runner that instantiates this same profile for
each analysis, copies only the selected immutable original into the VM,
limits scratch/output and parser-specific CPU/memory/page/expansion work,
validates the returned schema, and handles cancellation, restart, and cleanup.
The probe does not prove that a particular production host can start this
profile; that remains **field-unverified** until it is run there. No rich
format is advertised as supported on the basis of this workflow alone.
