cmake_minimum_required(VERSION 3.20)
project(TermanaRdpSpike LANGUAGES C)

# Isolated source-development targets, including the windowless direct-NLA client.
# No shipped QA package or all-platform acceptance is implied by this build.
# The TLS probe is separately invoked against one fixed approved QA endpoint;
# it always rejects certificates/auth and has no credential input.
if(NOT DEFINED TERMANA_FREERDP_SOURCE OR NOT EXISTS "${TERMANA_FREERDP_SOURCE}/.source_version")
  message(FATAL_ERROR "Provide the verified, extracted FreeRDP 3.32.2 release source")
endif()
file(READ "${TERMANA_FREERDP_SOURCE}/.source_version" TERMANA_SOURCE_VERSION)
string(STRIP "${TERMANA_SOURCE_VERSION}" TERMANA_SOURCE_VERSION)
if(NOT TERMANA_SOURCE_VERSION STREQUAL "3.32.2")
  message(FATAL_ERROR "This feasibility target is reviewed against FreeRDP 3.32.2 only")
endif()

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(USE_VERSION_FROM_GIT_TAG OFF CACHE BOOL "" FORCE)
set(USE_GIT_FOR_REVISION OFF CACHE BOOL "" FORCE)
set(FREERDP_UNIFIED_BUILD ON CACHE BOOL "" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(BUILD_FUZZERS OFF CACHE BOOL "" FORCE)
set(WITH_UNICODE_BUILTIN ON CACHE BOOL "" FORCE)
set(WITH_INTERNAL_MD4 ON CACHE BOOL "" FORCE)
set(WITH_INTERNAL_MD5 ON CACHE BOOL "" FORCE)
set(WITH_INTERNAL_RC4 ON CACHE BOOL "" FORCE)
set(WITH_OPENSSL ON CACHE BOOL "" FORCE)
set(OPENSSL_USE_STATIC_LIBS ON CACHE BOOL "" FORCE)
set(WITH_JSON_DISABLED ON CACHE BOOL "" FORCE)
set(FETCHCONTENT_FULLY_DISCONNECTED ON CACHE BOOL "" FORCE)
foreach(feature
    WITH_CLIENT WITH_CLIENT_COMMON WITH_CLIENT_SDL WITH_SERVER WITH_SHADOW WITH_PROXY
    WITH_SAMPLE WITH_CHANNELS WITH_CLIENT_CHANNELS WITH_SERVER_CHANNELS WITH_WINPR_TOOLS
    WITH_X11 WITH_WAYLAND WITH_FFMPEG WITH_SWSCALE WITH_OPENH264 WITH_GFX_AV1
    WITH_CAIRO WITH_JPEG WITH_PNG WITH_GSM WITH_SOXR WITH_OPUS WITH_CUPS WITH_FUSE
    WITH_ALSA WITH_PULSE WITH_OSS WITH_SNDIO WITH_MACAUDIO WITH_KRB5 WITH_NATIVE_SSPI
    WITH_PCSC WITH_PKCS11 WITH_SMARTCARD_EMULATE WITH_AAD WITH_WINPR_JSON
    WITH_URIPARSER WITH_SYSTEMD WITH_LIBSYSTEMD WITH_MANPAGES WITH_CCACHE
    WITH_CLANG_FORMAT WITH_DEBUG_ALL WITH_THIRD_PARTY WITH_RDTK)
  set(${feature} OFF CACHE BOOL "" FORCE)
endforeach()

# Only text clipboard and display control, statically linked. No platform client,
# filesystem/device/audio channels or dynamic add-in search is requested.
set(WITH_CHANNELS ON CACHE BOOL "" FORCE)
set(WITH_CLIENT_COMMON ON CACHE BOOL "" FORCE)
set(WITH_CLIENT_CHANNELS ON CACHE BOOL "" FORCE)
file(GLOB TERMANA_CHANNEL_OPTIONS "${TERMANA_FREERDP_SOURCE}/channels/*/ChannelOptions.cmake")
foreach(option_file ${TERMANA_CHANNEL_OPTIONS})
  get_filename_component(channel_directory "${option_file}" DIRECTORY)
  get_filename_component(channel_name "${channel_directory}" NAME)
  string(TOUPPER "${channel_name}" channel_upper)
  set(CHANNEL_${channel_upper} OFF CACHE BOOL "" FORCE)
  set(CHANNEL_${channel_upper}_CLIENT OFF CACHE BOOL "" FORCE)
  set(CHANNEL_${channel_upper}_SERVER OFF CACHE BOOL "" FORCE)
endforeach()
foreach(channel CLIPRDR DRDYNVC DISP)
  set(CHANNEL_${channel} ON CACHE BOOL "" FORCE)
  set(CHANNEL_${channel}_CLIENT ON CACHE BOOL "" FORCE)
endforeach()

find_package(OpenSSL 3.5.9 EXACT REQUIRED COMPONENTS SSL Crypto)
if(WIN32)
  # Upstream3.32.2 headers select dllimport unless *_EXPORTS is set. Every
  # consumer here is part of the SAME fully static executable, including the
  # static client/channel libraries; none should reference a FreeRDP/WinPR DLL.
  add_compile_definitions(FREERDP_EXPORTS WINPR_EXPORTS)
  # We use current AuthenticateEx/context/input APIs only. Exclude the unused
  # deprecated keyboard-remapping API with incompatible DWORD/UINT32 pointers
  # on MinGW, rather than weakening diagnostics or patching vendor sources.
  set(WITHOUT_FREERDP_3x_DEPRECATED ON CACHE BOOL "Current FreeRDP APIs only" FORCE)
  # The MinGW unwind.h is for compiler exception handling. WinPR's optional
  # generic unwind backtrace implementation also needs POSIX dlfcn.h, which
  # Windows does not provide. Keep WinPR's native Windows debug implementation.
  set(USE_UNWIND OFF CACHE BOOL "Use native Windows stack tracing" FORCE)
  set(USE_EXECINFO OFF CACHE BOOL "POSIX execinfo is unavailable on Windows" FORCE)
  include(CheckIncludeFile)
  check_include_file(threads.h TERMANA_HAVE_C11_THREADS_HEADER)
  if(NOT TERMANA_HAVE_C11_THREADS_HEADER)
    # MinGW's Windows SDK has native Win32 threads/TLS, not a C11 threads.h.
    # Select WinPR's existing Windows TLS branch; do not invent a pthread shim
    # or modify the verified upstream source to fake the missing header.
    add_compile_definitions(__STDC_NO_THREADS__=1)
  endif()
endif()
add_subdirectory("${TERMANA_FREERDP_SOURCE}" "${CMAKE_BINARY_DIR}/freerdp")
add_executable(termana-rdp-spike spike.c)
option(TERMANA_INTERNAL_TLS_PROBE "Build the private fixed-endpoint diagnostic" OFF)
if(TERMANA_INTERNAL_TLS_PROBE AND NOT WIN32)
  add_executable(termana-rdp-tls-probe tls_probe.c)
endif()
add_executable(termana-rdp-client client.c session_channels.c platform.c)
option(TERMANA_CLIPBOARD_QA_DIAGNOSTICS "Numeric-only clipboard QA metadata; no content" OFF)
if(TERMANA_CLIPBOARD_QA_DIAGNOSTICS)
  target_compile_definitions(termana-rdp-client PRIVATE TERMANA_CLIPBOARD_QA_DIAGNOSTICS=1)
endif()
if(WIN32)
  # Inherited IPC handles, no visible console window even when launched by Qt.
  set_target_properties(termana-rdp-client PROPERTIES WIN32_EXECUTABLE TRUE)
  target_link_libraries(termana-rdp-client PRIVATE advapi32 ws2_32)
  target_sources(termana-rdp-client PRIVATE windows_entry.c)
endif()
find_package(Threads REQUIRED)
target_link_libraries(termana-rdp-client PRIVATE Threads::Threads freerdp-client)
target_include_directories(termana-rdp-client SYSTEM PRIVATE "${OPENSSL_ROOT_DIR}/include")
set(TERMANA_HELPER_TARGETS termana-rdp-spike termana-rdp-client)
if(TERMANA_INTERNAL_TLS_PROBE AND NOT WIN32)
  list(APPEND TERMANA_HELPER_TARGETS termana-rdp-tls-probe)
endif()
foreach(target ${TERMANA_HELPER_TARGETS})
target_include_directories(${target} SYSTEM PRIVATE
    "${TERMANA_FREERDP_SOURCE}/include"
    "${TERMANA_FREERDP_SOURCE}/winpr/include"
    "${CMAKE_BINARY_DIR}/freerdp/include"
    "${CMAKE_BINARY_DIR}/freerdp/winpr/include")
target_link_libraries(${target} PRIVATE freerdp winpr)
if(CMAKE_C_COMPILER_ID MATCHES "Clang|GNU")
  target_compile_options(${target} PRIVATE -Wall -Wextra -Werror)
endif()
endforeach()
