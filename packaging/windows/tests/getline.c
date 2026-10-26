/* Exercise the native getline shim, including atomic reads of a shared stream. */
#include <windows.h>
#include <errno.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define REQUIRE(test) do { if (!(test)) { \
	fprintf(stderr, "getline test failed at line %d: %s\n", __LINE__, #test); \
	exit(1); \
} } while (0)

static void contents(void)
{
	static const char input[] = "a\r\nb\0c\nlast";
	FILE *stream = tmpfile();
	char *line = NULL;
	size_t capacity = 0;
	REQUIRE(stream != NULL);
	REQUIRE(fwrite(input, 1, sizeof(input) - 1, stream) == sizeof(input) - 1);
	rewind(stream);
	REQUIRE(getline(&line, &capacity, stream) == 3);
	REQUIRE(memcmp(line, "a\r\n\0", 4) == 0);
	REQUIRE(getline(&line, &capacity, stream) == 4);
	REQUIRE(memcmp(line, "b\0c\n\0", 5) == 0);
	/* Mixing ordinary stdio reads must not lose bytes to a private buffer. */
	REQUIRE(getc(stream) == 'l');
	REQUIRE(getline(&line, &capacity, stream) == 3);
	REQUIRE(strcmp(line, "ast") == 0);
	REQUIRE(getline(&line, &capacity, stream) == -1);
	REQUIRE(feof(stream));
	free(line);
	fclose(stream);
}

static void long_line(void)
{
	const size_t length = 1024 * 1024;
	FILE *stream = tmpfile();
	char *line = malloc(16);
	size_t capacity = 16;
	REQUIRE(stream != NULL && line != NULL);
	for (size_t i = 0; i < length; ++i) REQUIRE(putc('x', stream) != EOF);
	REQUIRE(putc('\n', stream) != EOF);
	rewind(stream);
	REQUIRE(getline(&line, &capacity, stream) == (ssize_t)(length + 1));
	REQUIRE(capacity >= length + 2);
	for (size_t i = 0; i < length; ++i) REQUIRE(line[i] == 'x');
	REQUIRE(line[length] == '\n' && line[length + 1] == '\0');
	free(line);
	fclose(stream);
}

struct shared {
	FILE *stream;
	volatile LONG lines;
	volatile LONG errors;
};

#define RECORD_SIZE 1024
#define RECORDS 4096

static unsigned __stdcall reader(void *argument)
{
	struct shared *shared = argument;
	char *line = NULL;
	size_t capacity = 0;
	ssize_t length;
	while ((length = getline(&line, &capacity, shared->stream)) != -1) {
		int valid = length == RECORD_SIZE && line[RECORD_SIZE - 1] == '\n';
		for (ssize_t i = 0; valid && i < length - 1; ++i) valid = line[i] == 'x';
		if (!valid) InterlockedIncrement(&shared->errors);
		InterlockedIncrement(&shared->lines);
	}
	free(line);
	return 0;
}

static void shared_stream(int empty)
{
	struct shared shared = {tmpfile(), 0, 0};
	HANDLE workers[4];
	char record[RECORD_SIZE];
	REQUIRE(shared.stream != NULL);
	memset(record, 'x', sizeof(record));
	record[sizeof(record) - 1] = '\n';
	if (!empty) {
		for (int i = 0; i < RECORDS; ++i)
			REQUIRE(fwrite(record, 1, sizeof(record), shared.stream) == sizeof(record));
	}
	rewind(shared.stream);
	for (int i = 0; i < 4; ++i) {
		workers[i] = (HANDLE)_beginthreadex(NULL, 0, reader, &shared, 0, NULL);
		REQUIRE(workers[i] != NULL);
	}
	/* An EOF path that forgets to unlock would strand the other readers. */
	REQUIRE(WaitForMultipleObjects(4, workers, TRUE, 30000) == WAIT_OBJECT_0);
	for (int i = 0; i < 4; ++i) REQUIRE(CloseHandle(workers[i]));
	REQUIRE(shared.errors == 0);
	REQUIRE(shared.lines == (empty ? 0 : RECORDS));
	fclose(shared.stream);
}

int main(void)
{
	char *line = NULL;
	size_t capacity = 0;
	errno = 0;
	REQUIRE(getline(&line, &capacity, NULL) == -1 && errno == EINVAL);
	contents();
	long_line();
	shared_stream(0);
	shared_stream(1);
	puts("Native getline: binary data, growth, EOF and shared-stream tests passed");
	return 0;
}
