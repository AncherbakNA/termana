/* Termana direct NLA prototype. Private inherited pipes, no command-line secrets.
 * Temporary bounded RGBX pipe transport: <=1920x1080, two ACK-owned publications.
 * Core input/display run on the engine owner. Published channel callbacks have
 * their own interface-lifetime lock; the reader uses only documented abort.
 */
#include <freerdp/config.h>
#include <freerdp/freerdp.h>
#include <freerdp/settings.h>
#include <freerdp/version.h>
#include <freerdp/crypto/certificate.h>
#include <freerdp/codec/color.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/graphics.h>
#include <freerdp/input.h>
#include <freerdp/session.h>
#include <freerdp/utils/helpers.h>
#include <winpr/input.h>
#include <winpr/wlog.h>
#include <openssl/ssl.h>
#include <openssl/provider.h>
#include <openssl/evp.h>
#include "platform.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "session_channels.h"

enum { START=1, TRUST=2, SECRET=3, ACK=4, KEY=5, MOUSE=6, RELEASE=7, STOP=8,
       HELLO=101, CERT=102, AUTH=103, ACTIVE=104, FRAME=105, CURSOR=106, ENDED=107,
       DIAGNOSTIC=108, LOGON=109 };
#define CONTROL_MAX 16384
#define QUEUE_MAX 128
#define PIXELS_MAX (1920U*1080U)
#define FRAME_MAX (PIXELS_MAX*4U)
typedef struct { uint32_t type, request, length; BYTE data[CONTROL_MAX]; } Message;
typedef struct { BYTE header[68]; BYTE* data; uint32_t length; } Output;
static struct {
    freerdp* instance;
    BYTE owner[48]; /* unpredictable nonce, stable session UUID, fresh attempt UUID */
    char host[256];
    uint32_t port, width, height, flags, request, generation, sequence, pending[2];
    BOOL dirty, active, auth_used, cert_decided, cert_allowed, gdi_ready;
    BOOL refresh_pending, wait_for_paint;
    BOOL keys[256];
    uint32_t scans[256];
    uint16_t buttons;
    TermanaMutex mutex;
    TermanaCondition condition;
    Message queue[QUEUE_MAX];
    size_t head, count;
    BOOL stopped;
    uint32_t trace[48], trace_count;
    uint64_t last_frame_ms;
    uint32_t input_count, queue_peak;
    TermanaMutex out_mutex;
    TermanaCondition out_condition;
    Output output[64];
    size_t out_head, out_count;
    BOOL out_closing;
} app;

static uint32_t get32(const BYTE* p) {
    return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];
}
static void put32(BYTE* p, uint32_t v) {
    p[0]=(BYTE)(v>>24); p[1]=(BYTE)(v>>16); p[2]=(BYTE)(v>>8); p[3]=(BYTE)v;
}
static void wipe(void* data, size_t n) {
    volatile BYTE* p=data; while (n--) *p++=0;
}
static BOOL safe_log(const wLogMessage* message) {
    /* Whitelisted static source IDs + line only. Never copy formatted text,
     * variable arguments, usernames, tokens, buffers or server messages. */
    const char* names[]={"nla.c","ntlm.c","transport.c","credssp_auth.c","nego.c",
                         "mcs.c","settings.c","gdi.c","freerdp.c","sspi.c","tls.c"};
    const char* leaf=message->FileName?strrchr(message->FileName,'/'):NULL;
    leaf=leaf?leaf+1:message->FileName;
    if (!leaf || message->Level<WLOG_ERROR) return TRUE;
    for (unsigned i=0;i<sizeof(names)/sizeof(names[0]);i++) if (!strcmp(leaf,names[i])) {
        termana_mutex_lock(&app.mutex);
        if (app.trace_count<24) {
            app.trace[app.trace_count*2]=i+1;
            app.trace[app.trace_count*2+1]=(uint32_t)message->LineNumber;
            app.trace_count++;
        }
        termana_mutex_unlock(&app.mutex); break;
    }
    return TRUE;
}
static BOOL stopped(void) {
    termana_mutex_lock(&app.mutex); BOOL value=app.stopped;
    termana_mutex_unlock(&app.mutex); return value;
}
static void stop_reader(void) {
    termana_mutex_lock(&app.mutex); app.stopped=TRUE;
    termana_condition_signal(&app.condition); termana_mutex_unlock(&app.mutex);
    if (app.instance && app.instance->context) {
        BOOL result=freerdp_abort_connect_context(app.instance->context); (void)result;
    }
}
static BOOL read_exact(BYTE* data, size_t size, unsigned timeout_seconds) {
    uint64_t deadline=GetTickCount64()+(uint64_t)timeout_seconds*1000;
    while (size && !stopped()) {
        int n=termana_pipe_read(data,size);
        if (n<0 || GetTickCount64()>deadline) return FALSE;
        if (!n) continue;
        data+=n; size-=(size_t)n;
    }
    return size==0;
}
static BOOL read_message(Message* message, BOOL first) {
    BYTE header[64];
    if (!read_exact(header,64,first?10:86400) || memcmp(header,"TRDP",4)!=0 ||
        get32(header+4)!=3) return FALSE;
    message->type=get32(header+8); message->length=get32(header+12);
    message->request=get32(header+16);
    /* 68-byte header: magic/version/type/length/request plus 48-byte owner. */
    BYTE tail[4];
    if (!read_exact(tail,4,10)) return FALSE;
    BYTE owner[48]; memcpy(owner,header+20,44); memcpy(owner+44,tail,4);
    if (first) memcpy(app.owner,owner,48);
    else if (memcmp(app.owner,owner,48)!=0) return FALSE;
    if (message->length>CONTROL_MAX) return FALSE;
    return read_exact(message->data,message->length,10);
}
static BOOL write_all(const BYTE* bytes, size_t size) {
    return termana_pipe_write(bytes,size);
}
static BOOL send_event(uint32_t type, uint32_t request, const BYTE* data, uint32_t size) {
    if (size>FRAME_MAX+20 || (type!=FRAME && type!=CURSOR && size>CONTROL_MAX) ||
        (type==CURSOR && size>96*96*4+20)) return FALSE;
    BYTE* owned=malloc(size?size:1); if (!owned) return FALSE;
    if (size) memcpy(owned,data,size);
    termana_mutex_lock(&app.out_mutex);
    if (app.out_closing || app.out_count==64) {
        termana_mutex_unlock(&app.out_mutex); free(owned); return FALSE;
    }
    Output* output=&app.output[(app.out_head+app.out_count)%64];
    memcpy(output->header,"TRDP",4); put32(output->header+4,3);
    put32(output->header+8,type); put32(output->header+12,size);
    put32(output->header+16,request); memcpy(output->header+20,app.owner,48);
    output->data=owned; output->length=size; app.out_count++;
    termana_condition_signal(&app.out_condition); termana_mutex_unlock(&app.out_mutex);
    return TRUE;
}
static void* writer(void* unused) {
    (void)unused;
    for (;;) {
        termana_mutex_lock(&app.out_mutex);
        while (!app.out_count && !app.out_closing)
            termana_condition_wait(&app.out_condition,&app.out_mutex,INFINITE);
        if (!app.out_count && app.out_closing) {
            termana_mutex_unlock(&app.out_mutex); return NULL;
        }
        Output output=app.output[app.out_head];
        memset(&app.output[app.out_head],0,sizeof(Output));
        app.out_head=(app.out_head+1)%64; app.out_count--;
        termana_mutex_unlock(&app.out_mutex);
        BOOL ok=write_all(output.header,sizeof(output.header)) &&
            write_all(output.data,output.length);
        free(output.data);
        if (!ok) { stop_reader(); return NULL; }
    }
}
static void* reader(void* unused) {
    (void)unused; Message message;
    while (!stopped()) {
        if (!read_message(&message,FALSE) || message.type<TRUST || message.type>CLIP_REPLY ||
            message.type==STOP) break;
        termana_mutex_lock(&app.mutex);
        if (app.count==QUEUE_MAX) {
            termana_mutex_unlock(&app.mutex); break;
        }
        size_t slot=(app.head+app.count)%QUEUE_MAX;
        memcpy(&app.queue[slot],&message,sizeof(message)); app.count++;
        if (app.count>app.queue_peak) app.queue_peak=(uint32_t)app.count;
        termana_condition_signal(&app.condition); termana_mutex_unlock(&app.mutex);
        wipe(&message,sizeof(message));
    }
    wipe(&message,sizeof(message)); stop_reader(); return NULL;
}
static BOOL next_message(Message* message, BOOL wait) {
    termana_mutex_lock(&app.mutex);
    if (wait && !app.count && !app.stopped) {
        uint64_t deadline=GetTickCount64()+90000;
        while (!app.count && !app.stopped) {
            uint64_t now=GetTickCount64();
            if (now>=deadline || !termana_condition_wait(&app.condition,&app.mutex,
                                                       (DWORD)(deadline-now))) break;
        }
    }
    BOOL available=app.count>0 && !app.stopped;
    if (available) {
        memcpy(message,&app.queue[app.head],sizeof(*message));
        wipe(&app.queue[app.head],sizeof(*message));
        app.head=(app.head+1)%QUEUE_MAX; app.count--;
    }
    termana_mutex_unlock(&app.mutex); return available;
}
static char* field(const BYTE* data, size_t size, size_t* offset, size_t limit) {
    if (*offset+4>size) return NULL;
    size_t n=get32(data+*offset); *offset+=4;
    if (n>limit || n>size-*offset || memchr(data+*offset,0,n)) return NULL;
    char* text=calloc(n+1,1); if (!text) return NULL;
    memcpy(text,data+*offset,n); *offset+=n; return text;
}
static BOOL protocol_ok(void) {
    uint32_t selected=freerdp_settings_get_uint32(app.instance->context->settings,
                                                FreeRDP_SelectedProtocol);
    return selected==2 || selected==8; /* HYBRID or HYBRID_EX, not TLS-only. */
}
static BOOL reply(uint32_t type, uint32_t request, Message* message) {
    if (!next_message(message,TRUE)) return FALSE;
    if (message->type!=type || message->request!=request) {
        wipe(message,sizeof(*message)); stop_reader(); return FALSE;
    }
    return TRUE;
}
static int certificate(freerdp* instance, const BYTE* data, size_t length,
                       const char* hostname, UINT16 port, DWORD flags) {
    (void)instance;
    if (app.cert_decided || app.auth_used || !protocol_ok() || !hostname ||
        strcmp(hostname,app.host)!=0 || port!=app.port || !data || length>65536)
        return 0;
    char* pem=calloc(length+1,1); if (!pem) return 0;
    memcpy(pem,data,length); rdpCertificate* cert=freerdp_certificate_new_from_pem(pem);
    free(pem); if (!cert) return 0;
    char* hash=freerdp_certificate_get_fingerprint_by_hash_ex(cert,"sha256",FALSE);
    size_t der_length=0;
    BYTE* der=freerdp_certificate_get_der(cert,&der_length);
    freerdp_certificate_free(cert);
    if (!hash || strlen(hash)!=64 || !der || !der_length || der_length>CONTROL_MAX-72) {
        free(hash); free(der); return 0;
    }
    /* The actual connection's leaf, not a second probe or untrusted display string. */
    BYTE payload[CONTROL_MAX]; put32(payload,flags); memcpy(payload+4,hash,64); free(hash);
    put32(payload+68,(uint32_t)der_length); memcpy(payload+72,der,der_length); free(der);
    uint32_t request=++app.request; Message message;
    if (!send_event(CERT,request,payload,72+(uint32_t)der_length) || !reply(TRUST,request,&message))
        return 0;
    app.cert_decided=TRUE;
    app.cert_allowed=message.length==4 && get32(message.data)==1;
    wipe(&message,sizeof(message));
    return app.cert_allowed?2:0; /* 2 = this connection only; never persist. */
}
static BOOL authenticate(freerdp* instance, char** user, char** password, char** domain,
                         rdp_auth_reason reason) {
    (void)instance;
    if (app.auth_used || reason!=AUTH_NLA || !protocol_ok() ||
        (app.cert_decided && !app.cert_allowed)) return FALSE;
    app.auth_used=TRUE;
    BYTE payload[8]; put32(payload,(uint32_t)reason);
    put32(payload+4,freerdp_settings_get_uint32(app.instance->context->settings,
                                              FreeRDP_SelectedProtocol));
    uint32_t request=++app.request; Message message;
    if (!send_event(AUTH,request,payload,sizeof(payload)) || !reply(SECRET,request,&message))
        return FALSE;
    size_t offset=0;
    char* u=field(message.data,message.length,&offset,512);
    char* d=field(message.data,message.length,&offset,512);
    char* p=field(message.data,message.length,&offset,4096);
    BOOL valid=u && *u && d && p && *p && offset==message.length;
    if (valid) {
        free(*user); free(*domain); free(*password);
        *user=u; *domain=d; *password=p;
    } else { free(u); free(d); if (p) { wipe(p,strlen(p)); free(p); } }
    wipe(&message,sizeof(message)); return valid;
}
static BOOL reject_redirect(freerdp* instance) { (void)instance; return FALSE; }
static BOOL notify_logon(rdpContext* context, UINT32 type, const void* data) {
    (void)context; (void)data;
    /* Never forward logon-info structures: extended info can contain cookies. */
    if (type==INFO_TYPE_LOGON || type==INFO_TYPE_LOGON_LONG || type==INFO_TYPE_LOGON_PLAIN_NOTIFY) {
        BYTE payload[4]; put32(payload,type);
        return app.auth_used && protocol_ok() && send_event(LOGON,0,payload,4);
    }
    return TRUE;
}
static BOOL pre_connect(freerdp* instance) {
    instance->context->update->SaveSessionInfo=notify_logon;
    return session_channels_connect(instance);
}
static BOOL dimensions(rdpContext* context) {
    uint32_t w=freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopWidth);
    uint32_t h=freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopHeight);
    return w>0 && h>0 && w<=1920 && h<=1080 && w*h<=PIXELS_MAX;
}
static BOOL release_all(void) {
    if (!app.active) return TRUE;
    BOOL ok=TRUE;
    for (unsigned i=0;i<256;i++) if (app.keys[i]) {
        ok=freerdp_input_send_keyboard_event_ex(app.instance->context->input,FALSE,FALSE,
                                               app.scans[i]) && ok;
        app.keys[i]=FALSE;
    }
    for (uint16_t mask=PTR_FLAGS_BUTTON1;mask<=PTR_FLAGS_BUTTON3;mask<<=1) {
        if (app.buttons&mask)
            ok=freerdp_input_send_mouse_event(app.instance->context->input,mask,0,0) && ok;
    }
    app.buttons=0; return ok;
}
static BOOL publish_frame(void) {
    if (!app.active || !app.dirty || app.wait_for_paint ||
        !dimensions(app.instance->context)) return TRUE;
    uint64_t milliseconds=GetTickCount64();
    if (milliseconds-app.last_frame_ms<33) return TRUE;
    unsigned slot=app.pending[0]==0?0:1;
    if (app.pending[slot]) return TRUE;
    rdpGdi* gdi=app.instance->context->gdi;
    if (!gdi || !gdi->primary_buffer || gdi->stride<(UINT32)gdi->width*4U ||
        gdi->height<=0 || (uint64_t)gdi->stride*(unsigned)gdi->height>FRAME_MAX) return FALSE;
    uint32_t size=gdi->stride*(unsigned)gdi->height;
    BYTE* data=malloc(size+20); if (!data) return FALSE;
    put32(data,app.generation); put32(data+4,++app.sequence);
    put32(data+8,(uint32_t)gdi->width); put32(data+12,(uint32_t)gdi->height);
    put32(data+16,gdi->stride); memcpy(data+20,gdi->primary_buffer,size);
    app.pending[slot]=app.sequence; app.dirty=FALSE;
    app.last_frame_ms=milliseconds;
    BOOL ok=send_event(FRAME,app.sequence,data,size+20); free(data); return ok;
}
static BOOL drain_messages(void);
static BOOL begin_paint(rdpContext* context) {
    context->gdi->primary->hdc->hwnd->invalid->null=TRUE; return TRUE;
}
static BOOL end_paint(rdpContext* context) {
    (void)context;
    /* Publish completed full paints, not just legacy GDI damage rectangles.
     * Surface/bitmap update paths can finish without that damage flag. */
    app.dirty=TRUE;
    app.wait_for_paint=FALSE;
    /* Never block inside FreeRDP's network/paint dispatch on parent I/O.
     * Publish/coalesce once the engine batch has returned to our owner loop. */
    /* The library can dispatch a long graphics batch before returning from
     * check_event_handles. Service bounded input on this same engine thread
     * at paint boundaries too; never call input APIs from the reader thread. */
    return !app.active || drain_messages();
}
static BOOL resize_desktop(rdpContext* context) {
    if (!dimensions(context) || !release_all()) return FALSE;
    if (!gdi_resize(context->gdi,
                      freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopWidth),
                      freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopHeight))) return FALSE;
    app.generation++; app.dirty=FALSE;
    app.wait_for_paint=app.refresh_pending=TRUE;
    return TRUE;
}
static BOOL refresh_desktop(void) {
    rdpContext* context=app.instance->context;
    if (!app.refresh_pending || !freerdp_is_active_state(context)) return TRUE;
    if (!dimensions(context) || !context->update->RefreshRect) return FALSE;
    const RECTANGLE_16 area={0,0,
        (UINT16)(freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopWidth)-1U),
        (UINT16)(freerdp_settings_get_uint32(context->settings,FreeRDP_DesktopHeight)-1U)};
    if (!context->update->RefreshRect(context,1,&area)) return FALSE;
    /* Never publish a freshly allocated/partial GDI surface as a completed
     * resized desktop. Request a full paint only after RDP reactivation. */
    app.refresh_pending=FALSE; app.wait_for_paint=TRUE; app.dirty=FALSE;
    return TRUE;
}
static BOOL pointer_new(rdpContext* context, rdpPointer* pointer) {
    (void)context; return pointer->width<=96 && pointer->height<=96;
}
static void pointer_free(rdpContext* context, rdpPointer* pointer) { (void)context; (void)pointer; }
static BOOL pointer_set(rdpContext* context, rdpPointer* pointer) {
    (void)context;
    if (!pointer_new(context,pointer) || !pointer->width || !pointer->height ||
        pointer->xPos>=pointer->width || pointer->yPos>=pointer->height) return FALSE;
    size_t bytes=pointer->width*pointer->height*4U;
    BYTE* data=calloc(bytes+20,1); if (!data) return FALSE;
    put32(data,2); put32(data+4,pointer->width); put32(data+8,pointer->height);
    put32(data+12,pointer->xPos); put32(data+16,pointer->yPos);
    BOOL ok=freerdp_image_copy_from_pointer_data(data+20,PIXEL_FORMAT_RGBA32,
        pointer->width*4,0,0,pointer->width,pointer->height,pointer->xorMaskData,
        pointer->lengthXorMask,pointer->andMaskData,pointer->lengthAndMask,
        pointer->xorBpp,NULL);
    if (ok) ok=send_event(CURSOR,0,data,(uint32_t)bytes+20);
    free(data); return ok;
}
static BOOL pointer_null(rdpContext* context) {
    (void)context; BYTE data[4]={0}; return send_event(CURSOR,0,data,4);
}
static BOOL pointer_default(rdpContext* context) {
    (void)context; BYTE data[4]; put32(data,1); return send_event(CURSOR,0,data,4);
}
static BOOL post_connect(freerdp* instance) {
    if (!app.auth_used || !protocol_ok() || !dimensions(instance->context) ||
        !gdi_init(instance,PIXEL_FORMAT_RGBX32)) return FALSE;
    app.gdi_ready=TRUE; app.generation=1;
    instance->context->update->BeginPaint=begin_paint;
    instance->context->update->EndPaint=end_paint;
    instance->context->update->DesktopResize=resize_desktop;
    rdpPointer pointer={0}; pointer.size=sizeof(pointer);
    pointer.New=pointer_new; pointer.Free=pointer_free; pointer.Set=pointer_set;
    pointer.SetNull=pointer_null; pointer.SetDefault=pointer_default;
    graphics_register_pointer(instance->context->graphics,&pointer);
    app.active=TRUE; app.dirty=TRUE;
    BYTE data[4]; put32(data,freerdp_settings_get_uint32(instance->context->settings,
                                                       FreeRDP_SelectedProtocol));
    return send_event(ACTIVE,0,data,4);
}
static uint32_t key_scan(uint32_t vk) {
    /* WinPR expects sided modifiers and KBDEXT for the extended scan table,
     * not bare Windows virtual keys. Keep the wire intent platform-independent. */
    uint32_t mapped=vk;
    if (vk==VK_SHIFT) mapped=VK_LSHIFT;
    if (vk==VK_CONTROL) mapped=VK_LCONTROL;
    if (vk==VK_MENU) mapped=VK_LMENU;
    if ((vk>=VK_PRIOR && vk<=VK_DOWN) || vk==VK_INSERT || vk==VK_DELETE ||
        vk==VK_LWIN || vk==VK_RWIN) mapped|=KBDEXT;
    return GetVirtualScanCodeFromVirtualKeyCode(mapped,WINPR_KBD_TYPE_IBM_ENHANCED);
}
static BOOL input_message(Message* message) {
    if (message->type>=RESIZE && message->type<=CLIP_REPLY)
        return session_channels_command(message->type,message->request,message->data,message->length);
    if (message->type==ACK && message->length==0) {
        for (unsigned i=0;i<2;i++) if (app.pending[i]==message->request) {
            app.pending[i]=0; return TRUE;
        }
        return FALSE;
    }
    if (message->type==RELEASE && !message->length) return release_all();
    if (message->type==KEY && message->length==12) {
        app.input_count++;
        uint32_t vk=get32(message->data), down=get32(message->data+4), repeat=get32(message->data+8);
        if (vk==0 || vk>255 || down>1 || repeat>1) return FALSE;
        uint32_t scan=key_scan(vk);
        if (!scan) return FALSE;
        if (!down && !app.keys[vk]) return TRUE;
        app.keys[vk]=down; app.scans[vk]=scan;
        return freerdp_input_send_keyboard_event_ex(app.instance->context->input,down,repeat,scan);
    }
    if (message->type==MOUSE && message->length==16) {
        uint32_t generation=get32(message->data), flags=get32(message->data+4);
        uint32_t x=get32(message->data+8),y=get32(message->data+12);
        if (generation!=app.generation) return TRUE;
        if (flags>65535 || x>=(uint32_t)app.instance->context->gdi->width ||
            y>=(uint32_t)app.instance->context->gdi->height) return FALSE;
        uint16_t mask=(uint16_t)flags&(PTR_FLAGS_BUTTON1|PTR_FLAGS_BUTTON2|PTR_FLAGS_BUTTON3);
        if (flags&PTR_FLAGS_DOWN) app.buttons|=mask; else app.buttons&=(uint16_t)~mask;
        return freerdp_input_send_mouse_event(app.instance->context->input,(UINT16)flags,
                                              (UINT16)x,(UINT16)y);
    }
    return FALSE;
}
static BOOL drain_messages(void) {
    Message message;
    for (unsigned i=0;i<QUEUE_MAX && next_message(&message,FALSE);i++) {
        BOOL valid=input_message(&message); wipe(&message,sizeof(message));
        if (!valid) { stop_reader(); return FALSE; }
    }
    return !stopped();
}
static BOOL configure(rdpSettings* settings, const char* root) {
    const size_t disabled[]={FreeRDP_RdpSecurity,FreeRDP_TlsSecurity,FreeRDP_ExtSecurity,
        FreeRDP_IgnoreCertificate,FreeRDP_AutoAcceptCertificate,FreeRDP_AutoDenyCertificate,
        FreeRDP_ExternalCertificateManagement,FreeRDP_GatewayEnabled,
        FreeRDP_GatewayUseSameCredentials,FreeRDP_RemoteCredentialGuard,
        FreeRDP_RestrictedAdminModeRequired,FreeRDP_AutoReconnectionEnabled,
        FreeRDP_DeviceRedirection,FreeRDP_RedirectHomeDrive,FreeRDP_NetworkAutoDetect,
        FreeRDP_SupportHeartbeatPdu,FreeRDP_SupportMultitransport,
        FreeRDP_RedirectDrives,FreeRDP_RedirectPrinters,FreeRDP_RedirectSerialPorts,
        FreeRDP_RedirectParallelPorts,FreeRDP_AudioCapture,FreeRDP_AudioPlayback,
        FreeRDP_RedirectSmartCards,FreeRDP_SmartcardLogon,
        FreeRDP_RemoteApplicationMode,FreeRDP_DeactivateClientDecoding,
        FreeRDP_SupportGraphicsPipeline};
    for (size_t i=0;i<sizeof(disabled)/sizeof(disabled[0]);i++)
        if (!freerdp_settings_set_bool(settings,disabled[i],FALSE)) return FALSE;
    return freerdp_settings_set_string(settings,FreeRDP_ServerHostname,app.host) &&
        freerdp_settings_set_uint32(settings,FreeRDP_ServerPort,app.port) &&
        freerdp_settings_set_uint32(settings,FreeRDP_TcpConnectTimeout,4000) &&
        freerdp_settings_set_uint32(settings,FreeRDP_AuthenticationLevel,2) &&
        freerdp_settings_set_uint32(settings,FreeRDP_DesktopWidth,app.width) &&
        freerdp_settings_set_uint32(settings,FreeRDP_DesktopHeight,app.height) &&
        freerdp_settings_set_uint32(settings,FreeRDP_ColorDepth,32) &&
        freerdp_settings_set_uint32(settings,FreeRDP_ClipboardFeatureMask,
            ((app.flags&4U)?CLIPRDR_FLAG_LOCAL_TO_REMOTE:0) |
            ((app.flags&8U)?CLIPRDR_FLAG_REMOTE_TO_LOCAL:0)) &&
        freerdp_settings_set_uint32(settings,FreeRDP_KeyboardLayout,0x409) &&
        freerdp_settings_set_string(settings,FreeRDP_ConfigPath,root) &&
        freerdp_settings_set_string(settings,FreeRDP_HomePath,root) &&
        freerdp_settings_set_string(settings,FreeRDP_ActionScript,NULL) &&
        freerdp_settings_set_string(settings,FreeRDP_Username,"") &&
        freerdp_settings_set_string(settings,FreeRDP_Domain,"") &&
        freerdp_settings_set_string(settings,FreeRDP_Password,"") &&
        freerdp_settings_set_bool(settings,FreeRDP_NlaSecurity,TRUE) &&
        freerdp_settings_set_bool(settings,FreeRDP_AutoLogonEnabled,TRUE) &&
        freerdp_settings_set_bool(settings,FreeRDP_LogonNotify,TRUE) &&
        freerdp_settings_set_bool(settings,FreeRDP_Authentication,TRUE) &&
        freerdp_settings_set_bool(settings,FreeRDP_ConsoleSession,(app.flags&1U)!=0) &&
        freerdp_settings_set_bool(settings,FreeRDP_RedirectClipboard,(app.flags&12U)!=0) &&
        freerdp_settings_set_bool(settings,FreeRDP_SupportDynamicChannels,(app.flags&2U)!=0) &&
        freerdp_settings_set_bool(settings,FreeRDP_SupportDisplayControl,(app.flags&2U)!=0) &&
        freerdp_settings_set_bool(settings,FreeRDP_DynamicResolutionUpdate,(app.flags&2U)!=0);
}
static int only_default_provider(OSSL_PROVIDER* provider, void* argument) {
    BOOL* valid=argument;
    if (strcmp(OSSL_PROVIDER_get0_name(provider),"default")) *valid=FALSE;
    return 1;
}
/* Supplying a user main on MinGW bypasses its WinMain trampoline even for a
 * GUI-subsystem PE. Keep the Windows security/network entrypoint unavoidable. */
#ifdef _WIN32
#define TERMANA_CLIENT_MAIN termana_rdp_main
#else
#define TERMANA_CLIENT_MAIN main
#endif
int TERMANA_CLIENT_MAIN(int argc, char** argv) {
    /* Applies to offline diagnostics too: never read an ambient openssl.cnf. */
    if (OPENSSL_init_ssl(OPENSSL_INIT_NO_LOAD_CONFIG,NULL)!=1) return 70;
    if (argc==2 && !strcmp(argv[1],"--crypto-self-test")) {
        const BYTE expected[32]={0xba,0x78,0x16,0xbf,0x8f,0x01,0xcf,0xea,
            0x41,0x41,0x40,0xde,0x5d,0xae,0x22,0x23,0xb0,0x03,0x61,0xa3,
            0x96,0x17,0x7a,0x9c,0xb4,0x10,0xff,0x61,0xf2,0x00,0x15,0xad};
        BYTE hash[32]; size_t size=0;
        BOOL valid=OpenSSL_version_num()==0x30500090L &&
            EVP_Q_digest(NULL,"SHA256",NULL,"abc",3,hash,&size)==1 && size==32 &&
            !memcmp(hash,expected,32);
        if (!OSSL_PROVIDER_do_all(NULL,only_default_provider,&valid)) valid=FALSE;
        puts(valid?"{\"crypto\":\"3.5.9\",\"sha256\":true,\"default_provider_only\":true,\"connect\":false}":
                   "{\"crypto\":\"unexpected\",\"connect\":false}");
        return valid?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--platform-self-test")) {
        BOOL valid=termana_platform_self_test();
        puts(valid?"{\"platform\":true,\"connect\":false}":"{\"platform\":false,\"connect\":false}");
        return valid?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--registration-self-test")) {
        strcpy(app.host,"test.invalid"); app.port=3389; app.width=1280; app.height=720; app.flags=14;
        app.instance=freerdp_new(); rdpSettings* settings=freerdp_settings_new(0);
        BOOL valid=app.instance && settings && configure(settings,"/nonexistent-termana-offline-test") &&
            freerdp_context_new_ex(app.instance,settings);
        if (!valid) return 72;
        session_channels_init(app.flags,send_event);
        app.instance->LoadChannels=session_channels_load;
        valid=session_channels_connect(app.instance) && app.instance->LoadChannels(app.instance);
        settings=app.instance->context->settings;
        uint32_t dynamic=freerdp_settings_get_uint32(settings,FreeRDP_DynamicChannelCount);
        uint32_t actual=freerdp_settings_get_uint32(settings,FreeRDP_ChannelCount);
        printf("{\"registration\":%s,\"dynamic\":%u,\"actual\":%u,\"connect\":false}\n",
               valid?"true":"false",dynamic,actual);
        session_channels_unsubscribe(app.instance->context);
        freerdp_context_free(app.instance); freerdp_free(app.instance); session_channels_free();
        return valid && dynamic==1 && actual==2?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--channels-self-test")) {
        BOOL valid=session_channels_self_test();
        puts(valid?"{\"channels\":true,\"connect\":false}":"{\"channels\":false,\"connect\":false}");
        return valid?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--settings-self-test")) {
        rdpSettings* settings=freerdp_settings_new(0);
        strcpy(app.host,"test.invalid"); app.port=3389; app.width=1280; app.height=720;
        BOOL valid=settings!=NULL;
        for (uint32_t flags=0;valid && flags<16;flags++) {
            app.flags=flags;
            valid=configure(settings,"/nonexistent-termana-offline-test") &&
                freerdp_settings_get_bool(settings,FreeRDP_NlaSecurity) &&
                freerdp_settings_get_bool(settings,FreeRDP_ConsoleSession)==!!(flags&1U) &&
                freerdp_settings_get_bool(settings,FreeRDP_RedirectClipboard)==!!(flags&12U) &&
                freerdp_settings_get_bool(settings,FreeRDP_SupportDisplayControl)==!!(flags&2U) &&
                !freerdp_settings_get_bool(settings,FreeRDP_RestrictedAdminModeRequired) &&
                !freerdp_settings_get_bool(settings,FreeRDP_DeviceRedirection) &&
                !freerdp_settings_get_bool(settings,FreeRDP_RedirectDrives) &&
                !freerdp_settings_get_bool(settings,FreeRDP_AudioPlayback) &&
                !(freerdp_settings_get_uint32(settings,FreeRDP_ClipboardFeatureMask) &
                    (CLIPRDR_FLAG_LOCAL_TO_REMOTE_FILES|CLIPRDR_FLAG_REMOTE_TO_LOCAL_FILES)) &&
                !freerdp_settings_get_bool(settings,FreeRDP_IgnoreCertificate);
        }
        freerdp_settings_free(settings);
        puts(valid?"{\"settings\":true,\"connect\":false}":"{\"settings\":false,\"connect\":false}");
        return valid?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--paint-self-test")) {
        BOOL valid=end_paint(NULL) && app.dirty;
        puts(valid?"{\"paint_coalesced\":true,\"connect\":false}":"{\"paint_coalesced\":false,\"connect\":false}");
        return valid?0:72;
    }
    if (argc==2 && !strcmp(argv[1],"--keymap-self-test")) {
        BOOL valid=key_scan(VK_LWIN)==0x15B && key_scan(VK_SHIFT)==0x2A &&
            key_scan(VK_CONTROL)==0x1D && key_scan(VK_MENU)==0x38 &&
            key_scan(VK_RIGHT)==0x14D && key_scan(VK_DELETE)==0x153 &&
            key_scan(VK_KEY_A)==0x1E && key_scan(VK_RETURN)==0x1C;
        puts(valid?"{\"keymap\":true,\"connect\":false}":"{\"keymap\":false,\"connect\":false}");
        return valid?0:72;
    }
    if (argc!=2 || strcmp(argv[1],"--ipc-v3")) return 64;
    if (!termana_pipe_init() || !termana_mutex_init(&app.mutex) ||
        !termana_condition_init(&app.condition) || !termana_mutex_init(&app.out_mutex) ||
        !termana_condition_init(&app.out_condition)) return 70;
    Message start;
    if (!read_message(&start,TRUE) || start.type!=START || start.request!=0 || start.length<12)
        return 65;
    app.port=get32(start.data); size_t offset=4;
    char* root=field(start.data,start.length,&offset,TERMANA_PATH_MAX-1);
    char* host=field(start.data,start.length,&offset,255);
    app.width=1280; app.height=720;
    /* Optional bounded display extension; old START packets retain the default.
     * Parent still requires this exact reviewed executable digest before launch. */
    if (offset+12==start.length) {
        app.width=get32(start.data+offset); app.height=get32(start.data+offset+4); offset+=8;
        app.flags=get32(start.data+offset); offset+=4;
    }
    BOOL display_ok=(app.width==1024 && app.height==768) ||
        (app.width==1280 && app.height==720) || (app.width==1600 && app.height==900) ||
        (app.width==1920 && app.height==1080);
    if (!root || !host || !*host || app.port<1 || app.port>65535 ||
        !display_ok || app.flags>15 || offset!=start.length || !termana_private_directory(root)) {
        free(root); free(host); return 65;
    }
    memcpy(app.host,host,strlen(host)+1); free(host); wipe(&start,sizeof(start));
    /* Static OpenSSL must not dlopen a developer's legacy provider or load
     * external openssl.cnf. WinPR's reviewed internal MD4/RC4 are enabled. */
    if (OPENSSL_init_ssl(OPENSSL_INIT_NO_LOAD_CONFIG,NULL)!=1 ||
        OSSL_PROVIDER_set_default_search_path(NULL,root)!=1) { free(root); return 70; }
    wLogCallbacks callbacks={0}; callbacks.message=safe_log;
    if (strcmp(freerdp_get_version_string(),"3.32.2") ||
        !WLog_SetLogAppenderType(WLog_GetRoot(),WLOG_APPENDER_CALLBACK) ||
        !WLog_ConfigureAppender(WLog_GetLogAppender(WLog_GetRoot()),"callbacks",&callbacks) ||
        !WLog_SetLogLevel(WLog_GetRoot(),WLOG_ERROR) ||
        !freerdp_setApplicationDetails("Termana","Rdp",-1)) { free(root); return 70; }
    app.instance=freerdp_new(); rdpSettings* settings=freerdp_settings_new(0);
    if (!app.instance || !settings || !configure(settings,root)) { free(root); return 70; }
    free(root);
    app.instance->VerifyX509Certificate=certificate;
    app.instance->AuthenticateEx=authenticate; app.instance->PostConnect=post_connect;
    app.instance->PreConnect=pre_connect;
    app.instance->LoadChannels=session_channels_load;
    app.instance->Redirect=reject_redirect;
    if (!freerdp_context_new_ex(app.instance,settings)) return 70;
    session_channels_init(app.flags,send_event);
    TermanaThread output_thread;
    if (!termana_thread_start(&output_thread,writer,NULL)) return 70;
    BYTE hello[12]; put32(hello,termana_process_id()); memcpy(hello+4,"3.32.2\0",7); hello[11]=0;
    if (!send_event(HELLO,0,hello,sizeof(hello))) return 71;
    TermanaThread thread;
    if (!termana_thread_start(&thread,reader,NULL)) return 70;
    BOOL connected=freerdp_connect(app.instance);
    if (connected) {
        time_t last_metrics=0;
        while (!stopped() && !freerdp_shall_disconnect_context(app.instance->context)) {
            if (!drain_messages() || !session_channels_poll() || !refresh_desktop() || !publish_frame()) break;
            if (time(NULL)>last_metrics) {
                BYTE metrics[24]; put32(metrics,100); put32(metrics+4,app.input_count);
                put32(metrics+8,101); put32(metrics+12,app.sequence);
                termana_mutex_lock(&app.mutex);
                put32(metrics+16,102); put32(metrics+20,app.queue_peak);
                termana_mutex_unlock(&app.mutex);
                if (!send_event(DIAGNOSTIC,0,metrics,24)) break;
                last_metrics=time(NULL);
            }
            HANDLE handles[64]; DWORD n=freerdp_get_event_handles(app.instance->context,handles,64);
            if (!n) break;
            DWORD status=WaitForMultipleObjects(n,handles,FALSE,10);
            if (status==WAIT_FAILED) break;
            /* A poll timeout is not socket readiness. Checking network I/O
             * anyway can block TLS reads and starve the private input queue. */
            if (status!=WAIT_TIMEOUT && !freerdp_check_event_handles(app.instance->context)) break;
        }
        BOOL released=release_all(); (void)released;
    }
    uint32_t error=freerdp_get_last_error(app.instance->context);
    stop_reader(); termana_thread_join(thread);
    BOOL disconnected=freerdp_disconnect(app.instance); (void)disconnected;
    BYTE diagnostic[192];
    for (uint32_t i=0;i<app.trace_count*2;i++) put32(diagnostic+i*4,app.trace[i]);
    BOOL traced=send_event(DIAGNOSTIC,0,diagnostic,app.trace_count*8); (void)traced;
    BYTE ended[8]; put32(ended,error); put32(ended+4,connected?1:0);
    BOOL sent=send_event(ENDED,0,ended,8); (void)sent;
    termana_mutex_lock(&app.out_mutex); app.out_closing=TRUE;
    termana_condition_signal(&app.out_condition); termana_mutex_unlock(&app.out_mutex);
    termana_thread_join(output_thread);
    if (app.gdi_ready) gdi_free(app.instance);
    session_channels_unsubscribe(app.instance->context);
    freerdp_context_free(app.instance); freerdp_free(app.instance);
    session_channels_free();
    wipe(app.queue,sizeof(app.queue)); return 0;
}
