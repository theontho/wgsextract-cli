/* bwa-mem2.exe for Windows: run the build for the best instruction set that
 * this CPU and Windows support (bwa-mem2.<isa>.exe next to this file), as
 * upstream's runsimd does with execv().
 *
 * Windows has no exec: MinGW's execv() starts a new process and exits at
 * once, so whoever started bwa-mem2 would see it finish before the alignment
 * did. Instead, start the build as a child with our command line unchanged,
 * in a job that kills it if we die, wait for it, and return its exit code.
 */
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static const wchar_t *best_isa(void)
{
	__builtin_cpu_init(); /* checks the OS saves AVX/AVX-512 state, too */
	if (__builtin_cpu_supports("avx512bw")) return L"avx512bw";
	if (__builtin_cpu_supports("avx2")) return L"avx2";
	if (__builtin_cpu_supports("avx")) return L"avx";
	if (__builtin_cpu_supports("sse4.2")) return L"sse42";
	if (__builtin_cpu_supports("sse4.1")) return L"sse41";
	return NULL;
}

/* The command line after argv[0], by the rules CommandLineToArgvW uses for
 * the program name: quoted up to the next quote, else up to whitespace. */
static const wchar_t *arguments(const wchar_t *cmdline)
{
	const wchar_t *p = cmdline;
	if (*p == L'"') {
		for (++p; *p && *p != L'"'; ++p);
		if (*p) ++p;
	} else {
		for (; *p && *p != L' ' && *p != L'\t'; ++p);
	}
	while (*p == L' ' || *p == L'\t') ++p;
	return p;
}

int main(void)
{
	wchar_t exe[MAX_PATH + 64], *slash, *cmdline;
	const wchar_t *isa = best_isa(), *args;
	STARTUPINFOW si;
	PROCESS_INFORMATION pi;
	JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
	HANDLE job;
	DWORD n, code = 1;
	size_t len;

	if (!isa) {
		fprintf(stderr, "bwa-mem2 needs a CPU with SSE4.1 or newer.\n");
		return 1;
	}
	n = GetModuleFileNameW(NULL, exe, MAX_PATH);
	if (n == 0 || n >= MAX_PATH) {
		fprintf(stderr, "bwa-mem2: can't find its own location (error %lu)\n", GetLastError());
		return 1;
	}
	slash = wcsrchr(exe, L'\\');
	swprintf(slash ? slash + 1 : exe, 64, L"bwa-mem2.%ls.exe", isa);
	if (GetFileAttributesW(exe) == INVALID_FILE_ATTRIBUTES) {
		fprintf(stderr, "bwa-mem2: %ls is missing\n", exe);
		return 1;
	}
	fprintf(stderr, "Launching executable \"%ls\"\n", exe);

	args = arguments(GetCommandLineW());
	len = wcslen(exe) + wcslen(args) + 4;
	cmdline = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, len * sizeof(wchar_t));
	if (!cmdline) return 1;
	swprintf(cmdline, len, L"\"%ls\" %ls", exe, args);

	job = CreateJobObjectW(NULL, NULL);
	if (job) {
		ZeroMemory(&limits, sizeof(limits));
		limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
		SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits));
	}
	/* Ctrl+C reaches the child too; outlive it to report its exit code. */
	SetConsoleCtrlHandler(NULL, TRUE);

	ZeroMemory(&si, sizeof(si));
	si.cb = sizeof(si);
	if (!CreateProcessW(exe, cmdline, NULL, NULL, TRUE, CREATE_SUSPENDED, NULL, NULL, &si, &pi)) {
		fprintf(stderr, "bwa-mem2: can't start %ls (error %lu)\n", exe, GetLastError());
		return 1;
	}
	if (job) AssignProcessToJobObject(job, pi.hProcess);
	ResumeThread(pi.hThread);
	WaitForSingleObject(pi.hProcess, INFINITE);
	GetExitCodeProcess(pi.hProcess, &code);
	return (int)code;
}
