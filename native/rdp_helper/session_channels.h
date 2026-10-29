#ifndef TERMANA_SESSION_CHANNELS_H
#define TERMANA_SESSION_CHANNELS_H
#include <freerdp/freerdp.h>
#include <stdint.h>
enum { RESIZE=9, CLIP_POLICY=10, CLIP_ANNOUNCE=11, CLIP_READ=12, CLIP_REPLY=13,
       CHANNELS=110, CLIP_OFFER=111, CLIP_REQUEST=112, CLIP_TEXT=113, CLIP_ACK=114 };
#define CLIP_TEXT_MAX 16000U
typedef BOOL (*TermanaEmit)(uint32_t, uint32_t, const BYTE*, uint32_t);
void session_channels_init(uint32_t flags, TermanaEmit emit);
BOOL session_channels_connect(freerdp* instance);
BOOL session_channels_load(freerdp* instance);
BOOL session_channels_command(uint32_t type, uint32_t request, const BYTE* data, uint32_t size);
BOOL session_channels_poll(void);
void session_channels_unsubscribe(rdpContext* context);
void session_channels_free(void);
BOOL session_channels_self_test(void);
#endif
