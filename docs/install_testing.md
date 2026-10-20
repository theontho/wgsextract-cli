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
python3 scripts/installlab/lab.py run linux --install-dir "WGS Extract"   # a path with a space
python3 scripts/installlab/lab.py run linux --update-from v0.4.0        # update a real release
python3 scripts/installlab/lab.py judge out/installlab/runs/<run>/results
python3 scripts/installlab/lab.py run macos --gui-repo ../gui-for-cli     # the macOS app
python3 scripts/installlab/lab.py prepare windows-gui            # once: WebView2 + a desktop
python3 scripts/installlab/lab.py run windows --gui-repo ../gui-for-cli   # the Windows app
python3 scripts/installlab/lab.py run linux --gui-repo ../gui-for-cli     # the Linux app
python3 scripts/installlab/lab.py mutate out/installlab/runs/<run>/results  # must all FAIL
python3 scripts/installlab/lab.py gc --dry-run                    # what disk cleanup would remove
```

Results land in `out/installlab/runs/<target>-<mode>-<time>/`: the guest's evidence,
state snapshots, outputs, and `verdict.json`. The lab's state (runs, base
images, downloads, SSH key) lives in the **main checkout's** `out/installlab`,
shared by every git worktree: base images take hours to build, and a linked
worktree's `out/` is deleted with the worktree.

## Disk space

A lab machine can hold 60 GB or more: base images, Tart's downloaded macOS
images, per-run evidence, and the leftovers of a run that was killed. Everything
the lab reuses is kept as a cache only while it's being used. `lab.py run`,
`prepare` and `selftest` remove what has expired when they finish (afterwards,
so the image a run just used is never the one removed), and `lab.py gc` does
the same on demand:

| What | Removed when |
|---|---|
| A killed run's VM clone, container, QEMU work dir or QMP socket dir | not running and over an hour old |
| Run evidence (`out/installlab/runs/*`) and lab logs | 14 days old (`--runs-days`) |
| A run's `serve/` dir (installer, archive and dataset copies) | 1 day old |
| Base images (`wgse-lab-macos-base`, Windows `base`/`gui-base`), the dataset cache, the GUI build's Docker volumes and `ubuntu:24.04` | unused for 30 days (`--images-days`) |
| What only `prepare` needs (Tart's OCI images, the Windows and virtio ISOs, the WebView2 installer) | unused for 7 days (`--downloads-days`) |
| Pixi and Homebrew package caches | only with `--package-caches DAYS` |

"Used" is when a run last cloned or booted it: Tart records this for its VMs
and images, and the lab records it in `out/installlab/last-used.json` (main checkout) for the
rest (falling back to the file's modification time). Windows' `base` is kept as
long as `gui-base`, which is built on it. Only things the lab names and creates
are touched; `ubuntu:24.04` only once a run has recorded using it.
`pixi run clean` still removes all of `out/` and `tmp/` at once.

Test runs clean up after themselves too: pytest keeps the temporary directories
of failed tests only (`tmp_path_retention_policy = "failed"`).

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
   and the CLI must still work. With `--update-from <tag>` the run instead
   installs that published release with its own installer (and the
   prerequisites it had, e.g. git before 0.4.1), updates it to the tree under
   test, and runs the workload on the result. `--install-dir` installs into
   another folder under `$HOME`, such as one whose path has a space. CI runs
   both on Linux, updating from the latest release.
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

## The Windows app

`lab.py run windows --gui-repo <gui-for-cli checkout>` tests the WGS Extract
Windows app (Tauri, NSIS installer) the same way. It cross-builds the x64
installer from that checkout every time (`make package
PLATFORM=tauri-windows-cross`, which needs cargo-xwin, NSIS and LLVM; it says
which are missing); `--gui-installer <setup.exe>` tests a given installer
instead, such as a release. An app needs a signed-in desktop and the WebView2
runtime, so `prepare windows-gui` (once, after `prepare windows`) adds a
second read-only layer, `gui-base.qcow2`, on top of the CLI's base image: the
WebView2 Evergreen runtime (pinned by SHA-256; every Windows 11 PC has it, the
image built from Windows Update files doesn't), and Lab User signed in at
boot (autologon). Lab User's first desktop sign-in happens while preparing,
so a run's baseline is a user who has used their PC before. Each run records
the VM's screen once a minute (`screens/` next to `results/`). In the VM, as
the standard user Lab User:

1. The installer is downloaded and given the Mark of the Web a browser adds.
   Its Authenticode signature is checked: a release must verify (`Valid`), a
   local build must be reported unsigned (`NotSigned`). Then it is unblocked,
   as clicking SmartScreen's "Run anyway" does.
2. The installer runs with its defaults (`/S`); the Apps entry, the Start menu
   shortcut and the app must exist.
3. The app is opened on Lab User's desktop (through a scheduled task that only
   runs in their signed-in session) and runs its own setup
   (`GUI_FOR_CLI_AUTO_RUN_SETUP`); the setup events it logs are the evidence.
   The window must report that the page drew (`webAppRendered`). Then the
   window is closed, and nothing the app started may still run.
4. The real actions run through the app's own runtime: its bundled Node and
   the bundle-test runner it ships (`bundle-test-cli.js`), on the installed
   bundle and the app's workspace.
5. Uninstall is Settings > Apps > Uninstall: the registered UninstallString
   opens the uninstaller's wizard, where UI Automation ticks "Delete the
   application data" and clicks Uninstall and Close, so the controls used are
   the wizard's real ones. The machine must then match the baseline,
   including the registry (Apps entries, user `Path`), the Start menu and
   `%LOCALAPPDATA%`.

Setup installs the CLI release the app pins, or this tree with
`--cli-from-tree`. `lab.py mutate` has Windows-specific corruptions too (an
Apps entry, shortcut, `Path` entry or running process left behind, an unsigned
installer reported as signed).

## The Linux app

`lab.py run linux --gui-repo <gui-for-cli checkout>` tests the Linux app (Tauri,
`.deb`). Tauri can't cross-build Linux packages, so the lab builds the `.deb` the
way `make package PLATFORM=tauri` does, in an `amd64` `ubuntu:24.04` container
(Cargo and the build tree are kept in Docker volumes between builds);
`--gui-installer <deb>` tests a given package. The scenario
(`guest/gui-linux.sh`) runs in a fresh `ubuntu:24.04` container as a desktop
user who may use `sudo`, as Ubuntu's first user can, with Xvfb as the display:
`sudo apt install ./WGSExtract.deb`, the app runs its own setup, the plan's
actions run through the app's bundled Node and runner, then the app's own
**Uninstall** (File menu; `GUI_FOR_CLI_AUTO_RUN_UNINSTALL`), since a package
manager can't remove what setup put in the user's home, and finally
`sudo apt remove --autoremove`. The desktop entry must have been installed,
and the home folder must end as it started.

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
