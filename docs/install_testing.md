# End-to-end install testing (the install lab)

`scripts/installlab/` tests what a new user actually does: install with the README
command on a machine that has never seen WGS Extract, run real commands on a real
genome, update, uninstall, and leave the machine as it was. It is built to be run
over and over on the same computer, and to make false passes hard.

```bash
pixi run -e dev python scripts/installlab/lab.py run linux       # fresh Ubuntu container
python3 scripts/installlab/lab.py prepare macos                  # once: clean macOS base image
python3 scripts/installlab/lab.py run macos                      # fresh macOS VM
python3 scripts/installlab/lab.py prepare windows                # once: Windows 11 ARM64 base image
python3 scripts/installlab/lab.py run windows                    # fresh Windows VM (QEMU)
python3 scripts/installlab/lab.py selftest linux                 # negative controls must fail
python3 scripts/installlab/lab.py judge out/installlab/runs/<run>/results
```

Results land in `out/installlab/runs/<target>-<mode>-<time>/`: the guest's evidence,
state snapshots, outputs, and `verdict.json`.

## Why installs used to "pass" while being broken

| Failure mode | Defence |
| --- | --- |
| Leftover state from earlier runs or the developer's own setup (`~/.pixi`, Homebrew tools on `PATH`, caches, `config.toml`, reference data) masks a missing dependency | Every run uses a **new machine cloned from a clean image** and destroys it afterwards: a Tart VM from Cirrus Labs' *vanilla* macOS image (no Homebrew, no tools), or a fresh `ubuntu:24.04` container. Nothing survives between runs, so cleanup on the host is never needed. |
| Tests fake the expensive parts (fake `pixi`, stub `wgsextract`, `--dry-run`, mocked downloads) | The guest runs the **README command** (`curl -fsSL …/install.sh \| sh`) with the real Pixi bootstrap and real package downloads. Only the source archive and dataset come from the host, over HTTP, so the tree under test is installed exactly like a release. |
| "It exited 0" counted as success; a command that does nothing passes | The guest never judges. It records exit codes, logs and outputs; the **host oracle** checks the outputs against values that come from biology, not from a previous run: HG00096's published mtDNA haplogroup (H16a1), recall and precision against its 1000 Genomes genotypes, read and mapping counts measured with the host's own samtools/bcftools. |
| Steps silently skipped | The oracle **fails closed**: every required step must leave evidence, and a missing step fails the run. A per-run nonce ties the evidence to this run. |
| Uninstall leaves files, rc edits, caches or config behind | The guest snapshots the machine (home, `/Applications`, `/usr/local`, `/opt`, launch agents, shell rc contents, `PATH`) **before install, after install and after uninstall**. The host diffs them: after uninstall the machine must match the baseline. The after-install snapshot must show the install, which proves the snapshots can see it. |
| The lab machine is more permissive than a user's (the vanilla macOS image ships with Gatekeeper off) | `prepare` turns Gatekeeper on, every macOS run records `spctl --status`, and the oracle fails a run whose machine had it off. |
| OS background activity makes state diffs flaky, tempting broad ignore lists | `--mode control` runs the same timeline **without installing** and prints what the OS changed by itself. Only those measured paths go in `noise/<target>.txt`, so ignore rules are evidence-based and reviewable. |
| The test harness itself is broken and passes everything | **Negative controls**: `--sabotage` deliberately breaks a run (installer does nothing, launcher returns 0 without working, samtools deleted, a file left behind, uninstall skipped). `selftest` runs them all; each must make the verdict FAIL on the expected checks. A harness that can't fail is caught. |
| Testing the developer build instead of what users get | Runs install the archive of the tree under test. The same scenario can point at a published release by leaving `LAB_ARCHIVE_URL` unset, so the installer resolves `releases/latest` like a user. |

## What one run covers

1. Baseline snapshot of a machine with only the documented prerequisites (macOS:
   Xcode Command Line Tools, baked into the base image by `prepare`; Ubuntu:
   `curl`, `ca-certificates`).
2. `curl -fsSL <installer> | sh` from the user's home directory, as a normal user.
3. The workload (`guest/workload.sh`) on the HG00096 mini genome: `--help`,
   `deps check`, `ref bootstrap` (a real GitHub release download with digest
   verification), `info` on BAM and CRAM, `extract mt-bam` / `mito-vcf`,
   `lineage mt-haplogroup` / `y-haplogroup`, `vcf snp`, `microarray`, `align`
   (bwa, from FASTQs), `vcf gatk` (first use of the optional `gatk` Pixi
   environment), and a command that must fail.
4. Update by rerunning the installer; a marker in `app/reference/` must survive,
   and the CLI must still work.
5. `uninstall.sh --yes --remove-config --remove-pixi`, then the final snapshot.

## The macOS app

`lab.py run macos --gui-repo <gui-for-cli checkout>` tests the WGS Extract app
the same way. It packages the DMG and builds the bundle-test runner from that
checkout every time (`--dmg` tests a given DMG instead), so a stale build can't
be what passes. In the VM:

1. The DMG is downloaded and given the quarantine flag a browser sets;
   Gatekeeper must accept a Developer ID build and must reject a local ad-hoc
   one (the base image has Gatekeeper on, and every run records its status).
2. The app is copied to `/Applications` as a drag install would, then
   launched; it runs its own setup (`GUI_FOR_CLI_AUTO_RUN_SETUP`) and writes the
   result. Setup installs the CLI release the app pins, or this CLI tree with
   `--cli-from-tree`.
3. The real actions run on the HG00096 mini genome through the same
   GUIForCLICore runner the app uses (`gui-for-cli bundle test`) with the
   installed app's bundle and workspace. Clicking isn't simulated; everything
   the click would start is.
4. The app's own **Uninstall** (`GUI_FOR_CLI_AUTO_RUN_UNINSTALL`) runs the bundle's
   uninstall steps, removes the app's data and moves it to the Trash; the Trash
   is emptied and the machine must match the baseline.

The oracle (`gui_oracle.py`) treats the app's own setup, uninstall and test
reports as evidence, not verdicts: every planned action must have passed, and
its outputs get the same biology checks as the CLI's. The app's microarray
action has no `--ref-vcf-tab`, so on the rebased mini genome only the kit's
format is checked there; the CLI lab measures concordance.

## Targets

- **macos** – Tart VM (Apple Silicon hosts). `prepare macos` clones
  `ghcr.io/cirruslabs/macos-tahoe-vanilla` and installs the Command Line Tools
  headlessly into `wgse-lab-macos-base`; each run clones that (an instant APFS
  clone), boots it headless, shares only the results and guest-script folders,
  and deletes it.
- **linux** – `ubuntu:24.04` `linux/amd64` container (Docker or OrbStack), with a
  non-root `tester` user.
- **host** – the current machine, for disposable CI runners only (refuses to run
  without `CI` set).

- **windows** – Windows 11 Pro ARM64 in QEMU (`-accel hvf`, Apple Silicon
  hosts). `prepare windows` builds `out/installlab/windows/base.qcow2` once; each
  run boots a throwaway qcow2 overlay of it (the base is read-only), reaches it
  over SSH on a forwarded localhost port, and deletes the overlay afterwards.
  The guest downloads artifacts from the host at `10.0.2.2`.
- **windows-host** – a new standard user on a disposable GitHub Windows runner
  (CI only; see below).

## Windows

The documented Windows path is: put `install_windows.bat` in an empty folder and
run it; it downloads the release `.zip`, installs Pixi and MSYS2, and sets up the
UCRT64 tools. `guest/windows/scenario.ps1` does exactly that as a standard
(non-admin) user named **Lab User**, so every profile path contains a space, as
on many real PCs. The workload is the same as on macOS/Linux, run as
`pixi run wgsextract …` from the install folder; "update" reruns the installer
in the same folder, and uninstall is
`uninstall_windows.bat --yes --remove-config --remove-prerequisites` followed by
deleting the folder the user downloaded. Each step's output goes through `cmd.exe`, so logs
are raw bytes, and only the step's own process is awaited (MSYS2's `gpg-agent`
outlives `pacman-key`). Before the workload, and after each installer run, the
scenario reloads the environment from the registry, as a new terminal would.

`guest/windows/inventory.ps1` snapshots the profile (depth 6, including
`AppData`), `C:\` top level (so `C:\msys64` appearing or staying behind is
seen), Program Files, ProgramData, Start Menu, Windows Error Reporting and crash
dump folders, user and machine environment variables (`Path`'s value, not just
its presence), Run keys, `HKCU\Software` vendors, Uninstall keys, scheduled
tasks and PowerShell profiles. Registry values are recorded raw (unexpanded) with their type, and
programs still running from outside `C:\Windows` are listed, so a background
agent left behind fails the run. The user's `%TEMP%` is treated as noise, like
`/tmp` on macOS/Linux. Paths are written with forward slashes so the same oracle
and noise patterns apply. A walk that errors for any reason other
than an access denial is retried and then reported as an `error` line, which
fails the run.

### Building the base image

Microsoft's own ISO download refuses scripted requests, so `prepare windows`
builds the install media from the files Windows Update serves (UUP dump), each
checked against the hash in Microsoft's update metadata. The conversion runs in
an `ubuntu:24.04` container (cabextract, wimlib, chntpw, genisoimage); the Linux
converter doesn't fold in cumulative updates, so the image is the 24H2 GA build
(26100.1). The virtio-win ISO (pinned by SHA-256) supplies the ARM64 NetKVM
driver for the VM's network card.

Deviations from a user's PC, all needed only to drive the VM headlessly, are
listed in `vm/windows/autounattend.xml`: Setup's TPM/Secure Boot checks are
bypassed, `lab-apply.cmd` applies the image from WinPE (24H2's new Setup host
stalls before touching the disk in a headless VM), a local `labadmin` account
replaces the Microsoft-account OOBE, and `lab-setup.ps1` adds OpenSSH Server,
the Lab User account, and turns off automatic Windows Update installs so a run
isn't rebooted halfway. Windows' firewall rule for SSH is opened on the Public profile (QEMU's NAT is an
unidentified, Public network, as on a user's PC). Defender stays on. Nothing WGS
Extract needs is in the base image; Lab User has logged in once (over SSH), so
the baseline isn't Windows' first-logon setup. The VM is ARM64, so Pixi
installs the win-64 environments under Windows' x64 emulation and MSYS2 runs
emulated too, as on a Snapdragon laptop.

### On GitHub's Windows runners

`windows-host` runs the same scenario on `windows-latest`, which ships MSYS2 at
`C:\msys64`, Git, conda, Java, Python, Strawberry Perl and more. Before the run,
`vm/windows/deprovision-runner.ps1` renames `C:\msys64`, resets the machine
`Path` to Windows' defaults and removes non-default machine environment
variables, and the scenario runs over SSH as a newly created standard user. The
tool survey (`pixi`, `samtools`, `bcftools`, `java`, `python`, `conda`, `bash`,
`gcc`, `perl`, MSYS2) fails the run if anything is still reachable. The oracle
needs samtools/bcftools, which have no native Windows Pixi build, so the
evidence is uploaded and judged (and mutated) by a separate Linux job.
