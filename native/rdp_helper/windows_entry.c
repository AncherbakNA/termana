/* GUI-subsystem executable with private inherited pipe handles, never conhost. */
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int termana_rdp_main(int argc, char** argv);
int WINAPI WinMain(HINSTANCE instance, HINSTANCE previous, LPSTR command, int show) {
    (void)instance; (void)previous; (void)command; (void)show;
    /* All FreeRDP/WinPR/crypto code is static. Any optional Windows platform
     * library must come from System32, never cwd, PATH or a third-party folder.
     * Import-table provenance is checked separately before this code can run. */
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32) || !SetDllDirectoryW(L""))
        return 70;
    /* Windows applications, not getaddrinfo/FreeRDP, own Winsock lifetime.
     * Without startup even a numeric server address fails with WSANOTINITIALISED.
     * Balance cleanup only after main has joined its network/IPC workers. */
    WSADATA sockets;
    if (WSAStartup(MAKEWORD(2,2),&sockets)!=0) return 70;
    int result=70;
    if (sockets.wVersion==MAKEWORD(2,2)) {
        if (__argc==2 && !strcmp(__argv[1],"--network-self-test")) {
            struct addrinfo hints={0}, *address=NULL;
            hints.ai_family=AF_INET; hints.ai_socktype=SOCK_STREAM;
            hints.ai_flags=AI_NUMERICHOST|AI_NUMERICSERV;
            int status=getaddrinfo("127.0.0.1","3389",&hints,&address);
            BOOL valid=status==0 && address && address->ai_family==AF_INET;
            if (address) freeaddrinfo(address);
            puts(valid?"{\"network\":true,\"numeric_only\":true,\"connect\":false}":
                       "{\"network\":false,\"connect\":false}");
            result=valid?0:72;
        } else result=termana_rdp_main(__argc,__argv);
    }
    WSACleanup();
    return result;
}
