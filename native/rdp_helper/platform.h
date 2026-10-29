/* Small reviewed OS boundary. WinPR synchronization on every client platform.
 * Each Condition has exactly ONE waiter. The manual-reset event is reset under
 * the SAME queue mutex before releasing it. Producers signal under that mutex.
 * It is not a general pthread condition replacement.
 */
#ifndef TERMANA_RDP_PLATFORM_H
#define TERMANA_RDP_PLATFORM_H
#include <winpr/synch.h>
#include <winpr/thread.h>
#include <winpr/sysinfo.h>
#include <stdint.h>
#include <stddef.h>

typedef CRITICAL_SECTION TermanaMutex;
typedef HANDLE TermanaCondition;
typedef HANDLE TermanaThread;
BOOL termana_mutex_init(TermanaMutex* mutex);
void termana_mutex_lock(TermanaMutex* mutex);
void termana_mutex_unlock(TermanaMutex* mutex);
void termana_mutex_free(TermanaMutex* mutex);
BOOL termana_condition_init(TermanaCondition* condition);
void termana_condition_signal(TermanaCondition* condition);
BOOL termana_condition_wait(TermanaCondition* condition, TermanaMutex* mutex, DWORD milliseconds);
void termana_condition_free(TermanaCondition* condition);
BOOL termana_thread_start(TermanaThread* thread, void* (*function)(void*), void* argument);
void termana_thread_join(TermanaThread thread);
BOOL termana_pipe_init(void);
/* 0 = poll timeout; -1 = closed/error; positive = byte count. */
int termana_pipe_read(void* bytes, size_t size);
BOOL termana_pipe_write(const void* bytes, size_t size);
BOOL termana_private_directory(const char* path);
uint32_t termana_process_id(void);
BOOL termana_platform_self_test(void);
#define TERMANA_PATH_MAX 32768
#endif
