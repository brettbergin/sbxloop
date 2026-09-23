# Inert keylogger-indicator specimen

`inert_keylogger_indicators.elf` is a small Linux ELF executable for testing
channel-file upload and static binary inspection. It contains keylogger-related
API names and paths as data, with no explanatory label in the binary. Its entry
point returns immediately. It does not read keyboard events, create a log,
contact a network endpoint, or persist on the host. Do not execute uploaded
files during analysis.

The source is `inert_keylogger_indicators.c`. Rebuild on a Linux host with:

```sh
gcc -Os -s -fno-pie -no-pie -fno-ident -Wl,--build-id=none \
  -o inert_keylogger_indicators.elf inert_keylogger_indicators.c
```

The specimen is meant to demonstrate the current limit: generic sbxloop tools
can find and display these byte strings, but cannot yet determine executable
behavior or parse ELF sections/imports. A future static analyzer must report
observed indicators separately from conclusions about behavior.
