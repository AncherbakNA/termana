#include "platform.h"
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#include <aclapi.h>
#include <winpr/string.h>
#else
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

BOOL termana_mutex_init(TermanaMutex* mutex) {
    return InitializeCriticalSectionAndSpinCount(mutex,4000);
}
void termana_mutex_lock(TermanaMutex* mutex) { EnterCriticalSection(mutex); }
void termana_mutex_unlock(TermanaMutex* mutex) { LeaveCriticalSection(mutex); }
void termana_mutex_free(TermanaMutex* mutex) { DeleteCriticalSection(mutex); }
BOOL termana_condition_init(TermanaCondition* condition) {
    /* WinPR3.32.2 does not implement auto-reset events on POSIX. */
    *condition=CreateEventW(NULL,TRUE,FALSE,NULL); return *condition!=NULL;
}
void termana_condition_signal(TermanaCondition* condition) { SetEvent(*condition); }
BOOL termana_condition_wait(TermanaCondition* condition, TermanaMutex* mutex, DWORD ms) {
    /* Caller checked its predicate under this mutex. Reset before releasing it:
     * a producer cannot enqueue/signal until then; an unlock/Wait race is safe.
     */
    if (!ResetEvent(*condition)) return FALSE;
    termana_mutex_unlock(mutex);
    DWORD result=WaitForSingleObject(*condition,ms);
    termana_mutex_lock(mutex); return result==WAIT_OBJECT_0;
}
void termana_condition_free(TermanaCondition* condition) { CloseHandle(*condition); *condition=NULL; }
typedef struct { void* (*function)(void*); void* argument; } Start;
static DWORD WINAPI thread_entry(LPVOID value) {
    Start start=*(Start*)value; free(value); start.function(start.argument); return 0;
}
BOOL termana_thread_start(TermanaThread* thread, void* (*function)(void*), void* argument) {
    Start* start=malloc(sizeof(Start)); if (!start) return FALSE;
    start->function=function; start->argument=argument;
    *thread=CreateThread(NULL,0,thread_entry,start,0,NULL);
    if (!*thread) free(start);
    return *thread!=NULL;
}
void termana_thread_join(TermanaThread thread) {
    WaitForSingleObject(thread,INFINITE); CloseHandle(thread);
}
uint32_t termana_process_id(void) { return GetCurrentProcessId(); }

typedef struct { TermanaMutex* mutex; TermanaCondition* condition; BOOL done; } Test;
static void* self_test_thread(void* argument) {
    Test* test=argument;
    termana_mutex_lock(test->mutex); test->done=TRUE;
    termana_condition_signal(test->condition); termana_mutex_unlock(test->mutex); return NULL;
}
BOOL termana_platform_self_test(void) {
    TermanaMutex mutex; TermanaCondition condition=NULL; TermanaThread thread=NULL;
    if (!termana_mutex_init(&mutex)) return FALSE;
    if (!termana_condition_init(&condition)) { termana_mutex_free(&mutex); return FALSE; }
    termana_mutex_lock(&mutex); termana_mutex_lock(&mutex); termana_mutex_unlock(&mutex);
    BOOL valid=TRUE;
    for (unsigned i=0;valid && i<16;i++) {
        Test test={&mutex,&condition,FALSE};
        if (!termana_thread_start(&thread,self_test_thread,&test)) { valid=FALSE; break; }
        while (!test.done) {
            if (!termana_condition_wait(&condition,&mutex,1000)) { valid=FALSE; break; }
        }
        /* Worker may still be returning; never join while holding its mutex. */
        termana_mutex_unlock(&mutex); termana_thread_join(thread); termana_mutex_lock(&mutex);
    }
    valid=!termana_condition_wait(&condition,&mutex,20) && valid;
    termana_mutex_unlock(&mutex); termana_condition_free(&condition); termana_mutex_free(&mutex);
    return valid && termana_process_id()!=0;
}

#ifdef _WIN32
static HANDLE input_pipe, output_pipe, private_directory_handle=INVALID_HANDLE_VALUE;
BOOL termana_pipe_init(void) {
    input_pipe=GetStdHandle(STD_INPUT_HANDLE); output_pipe=GetStdHandle(STD_OUTPUT_HANDLE);
    return GetFileType(input_pipe)==FILE_TYPE_PIPE && GetFileType(output_pipe)==FILE_TYPE_PIPE;
}
int termana_pipe_read(void* bytes, size_t size) {
    DWORD available=0, received=0;
    if (!PeekNamedPipe(input_pipe,NULL,0,NULL,&available,NULL)) return -1;
    if (!available) { Sleep(10); return 0; }
    DWORD count=(DWORD)(size<available?size:available);
    if (!ReadFile(input_pipe,bytes,count,&received,NULL) || !received) return -1;
    return (int)received;
}
BOOL termana_pipe_write(const void* data, size_t size) {
    const BYTE* bytes=data;
    while (size) {
        DWORD written=0, count=(DWORD)(size>1048576?1048576:size);
        if (!WriteFile(output_pipe,bytes,count,&written,NULL) || !written) return FALSE;
        bytes+=written; size-=written;
    }
    return TRUE;
}
static void close_private_directory(void) {
    if (private_directory_handle!=INVALID_HANDLE_VALUE) CloseHandle(private_directory_handle);
    private_directory_handle=INVALID_HANDLE_VALUE;
}
BOOL termana_private_directory(const char* path) {
    /* Keep the verified directory open without delete sharing until process exit.
     * Reject aliases/reparse/UNC and unprotected, unknown or permissive ACLs.
     * No ACL repair, privilege escalation, or path-based trust file operation.
     */
    if (private_directory_handle!=INVALID_HANDLE_VALUE) return FALSE;
    WCHAR* wide=ConvertUtf8ToWCharAlloc(path,NULL);
    if (!wide) return FALSE;
    HANDLE directory=CreateFileW(wide,READ_CONTROL|FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ|FILE_SHARE_WRITE,NULL,OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,NULL);
    BOOL ok=FALSE; HANDLE token=NULL; TOKEN_USER* user=NULL;
    PSECURITY_DESCRIPTOR descriptor=NULL; PSID owner=NULL; PACL acl=NULL;
    BYTE system[SECURITY_MAX_SID_SIZE], admin[SECURITY_MAX_SID_SIZE];
    DWORD system_size=sizeof(system), admin_size=sizeof(admin), needed=0;
    BY_HANDLE_FILE_INFORMATION info={0}; SECURITY_DESCRIPTOR_CONTROL control=0; DWORD revision=0;
    WCHAR canonical[TERMANA_PATH_MAX], absolute[TERMANA_PATH_MAX];
    if (directory==INVALID_HANDLE_VALUE || GetFileType(directory)!=FILE_TYPE_DISK ||
        !GetFileInformationByHandle(directory,&info) ||
        !(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY) ||
        (info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)) goto done;
    DWORD length=GetFinalPathNameByHandleW(directory,canonical,TERMANA_PATH_MAX,FILE_NAME_NORMALIZED|VOLUME_NAME_DOS);
    DWORD full=GetFullPathNameW(wide,TERMANA_PATH_MAX,absolute,NULL);
    if (!length || length>=TERMANA_PATH_MAX || !full || full>=TERMANA_PATH_MAX ||
        wcsncmp(canonical,L"\\\\?\\",4) || absolute[1]!=L':' ||
        _wcsicmp(canonical+4,absolute) || _wcsicmp(wide,absolute)) goto done;
    if (!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) goto done;
    GetTokenInformation(token,TokenUser,NULL,0,&needed);
    if (!needed || needed>65536 || !(user=malloc(needed)) ||
        !GetTokenInformation(token,TokenUser,user,needed,&needed) ||
        !CreateWellKnownSid(WinLocalSystemSid,NULL,system,&system_size) ||
        !CreateWellKnownSid(WinBuiltinAdministratorsSid,NULL,admin,&admin_size)) goto done;
    if (GetSecurityInfo(directory,SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,
        &owner,NULL,&acl,NULL,&descriptor)!=ERROR_SUCCESS || !owner || !acl ||
        !IsValidAcl(acl) || !EqualSid(owner,user->User.Sid) ||
        !GetSecurityDescriptorControl(descriptor,&control,&revision) ||
        !(control&SE_DACL_PROTECTED) || acl->AceCount>128) goto done;
    for (DWORD i=0;i<acl->AceCount;i++) {
        void* entry=NULL;
        if (!GetAce(acl,i,&entry)) goto done;
        ACE_HEADER* header=entry;
        if (header->AceType==ACCESS_DENIED_ACE_TYPE) continue;
        if (header->AceType!=ACCESS_ALLOWED_ACE_TYPE) goto done;
        ACCESS_ALLOWED_ACE* allow=entry; PSID sid=&allow->SidStart;
        if (!IsValidSid(sid) || (!EqualSid(sid,user->User.Sid) &&
            !EqualSid(sid,system) && !EqualSid(sid,admin))) goto done;
    }
    private_directory_handle=directory;
    if (atexit(close_private_directory)) { private_directory_handle=INVALID_HANDLE_VALUE; goto done; }
    directory=INVALID_HANDLE_VALUE; ok=TRUE;
done:
    if (descriptor) LocalFree(descriptor);
    if (token) CloseHandle(token);
    free(user); free(wide);
    if (directory!=INVALID_HANDLE_VALUE) CloseHandle(directory);
    return ok;
}
#else
BOOL termana_pipe_init(void) {
    signal(SIGPIPE,SIG_DFL);
    struct stat input, output;
    return fstat(STDIN_FILENO,&input)==0 && fstat(STDOUT_FILENO,&output)==0 &&
        S_ISFIFO(input.st_mode) && S_ISFIFO(output.st_mode);
}
int termana_pipe_read(void* bytes, size_t size) {
    struct pollfd input={STDIN_FILENO,POLLIN,0};
    int rc=poll(&input,1,100);
    if (rc<0) return errno==EINTR?0:-1;
    if (!rc) return 0;
    ssize_t count=read(STDIN_FILENO,bytes,size);
    if (count<0 && errno==EINTR) return 0;
    return count>0?(int)count:-1;
}
BOOL termana_pipe_write(const void* data, size_t size) {
    const BYTE* bytes=data;
    while (size) {
        ssize_t count=write(STDOUT_FILENO,bytes,size);
        if (count<0 && errno==EINTR) continue;
        if (count<=0) return FALSE;
        bytes+=count; size-=(size_t)count;
    }
    return TRUE;
}
BOOL termana_private_directory(const char* path) {
    struct stat info; char canonical[PATH_MAX];
    return realpath(path,canonical) && !strcmp(path,canonical) && lstat(path,&info)==0 &&
        S_ISDIR(info.st_mode) && info.st_uid==getuid() && !(info.st_mode&0077);
}
#endif
