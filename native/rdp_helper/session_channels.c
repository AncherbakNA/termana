/* Only cliprdr Unicode text and disp are built/loaded. No OS clipboard API here.
 * FreeRDP channel workers report bounded metadata/data through our output queue.
 * Parent GUI makes every OS-clipboard decision. Published channel interfaces are
 * protected against disconnect; core display/input remain on the engine owner.
 */
#include "session_channels.h"
#include <freerdp/client/channels.h>
#include <freerdp/client/cmdline.h>
#include <freerdp/client/cliprdr.h>
#include <freerdp/client/disp.h>
#include <freerdp/channels/channels.h>
#include <freerdp/event.h>
#include <freerdp/settings.h>
#include <winpr/clipboard.h>
#include <winpr/crt.h>
#include <winpr/error.h>
#include <winpr/sysinfo.h>
#include <winpr/string.h>
#include "platform.h"
#include <stdlib.h>
#include <string.h>

static struct {
    TermanaMutex mutex;
    TermanaEmit emit;
    CliprdrClientContext* clip;
    DispClientContext* disp;
    uint32_t flags, policy, serial, offer, remote_format, local_revision;
    uint32_t local_request, local_request_revision, remote_request, remote_generation;
    uint64_t local_deadline, remote_deadline, display_area;
    uint32_t format_revisions[16], format_head, format_count;
    BOOL format_sent;
    BOOL monitor_ready, display_ready, present, remote_inflight;
} channel;
static uint32_t read32(const BYTE* p) { return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3]; }
static void store32(BYTE* p, uint32_t v) { p[0]=(BYTE)(v>>24); p[1]=(BYTE)(v>>16); p[2]=(BYTE)(v>>8); p[3]=(BYTE)v; }
static void erase(void* p, size_t n) { volatile BYTE* b=p; while(n--) *b++=0; }
static BOOL emit_number(uint32_t kind, uint32_t request, uint32_t value) {
    BYTE bytes[4]; store32(bytes,value); return channel.emit(kind,request,bytes,4);
}
static BOOL capabilities(void) {
    return emit_number(CHANNELS,0,(channel.monitor_ready?1U:0U)|(channel.display_ready?2U:0U));
}
static UINT advertise(void) {
    if (!channel.clip || !channel.monitor_ready) return CHANNEL_RC_OK;
    CLIPRDR_FORMAT format={0}; format.formatId=CF_UNICODETEXT;
    CLIPRDR_FORMAT_LIST list={0};
    list.common.msgType=CB_FORMAT_LIST;
    if ((channel.policy&1U) && channel.present) { list.numFormats=1; list.formats=&format; }
    /* FreeRDP deliberately suppresses later empty lists. Track only messages
     * actually submitted, so an untagged server ACK cannot approve a newer copy. */
    if (!list.numFormats && channel.format_sent) return CHANNEL_RC_OK;
    if (channel.format_count==16) return ERROR_INTERNAL_ERROR;
    channel.format_revisions[(channel.format_head+channel.format_count)%16]=
        list.numFormats?channel.local_revision:0;
    channel.format_count++; channel.format_sent=TRUE;
    return channel.clip->ClientFormatList(channel.clip,&list);
}
static UINT local_response(BOOL success, const BYTE* text, uint32_t size) {
    if (!channel.clip) return CHANNEL_RC_OK;
    WCHAR* wide=NULL;
    size_t count=0;
    if (success) {
        if (size>CLIP_TEXT_MAX || memchr(text,0,size)) success=FALSE;
        else if (!size) { wide=calloc(1,sizeof(WCHAR)); count=1; }
        else {
            wide=ConvertUtf8NToWCharAlloc((const char*)text,size,&count);
            if (!wide || !count) success=FALSE;
            else count++; /* Alloc API supplies the terminating NUL. */
        }
        if (!wide) success=FALSE;
    }
    CLIPRDR_FORMAT_DATA_RESPONSE response={0};
    response.common.msgType=CB_FORMAT_DATA_RESPONSE;
    response.common.msgFlags=success?CB_RESPONSE_OK:CB_RESPONSE_FAIL;
    response.common.dataLen=success?(UINT32)count*2U:0;
    response.requestedFormatData=(const BYTE*)wide;
    UINT result=channel.clip->ClientFormatDataResponse(channel.clip,&response);
    if (wide) { erase(wide,(size_t)count*sizeof(WCHAR)); free(wide); }
    return result;
}
static UINT monitor_ready(CliprdrClientContext* context, const CLIPRDR_MONITOR_READY* ready) {
    (void)ready;
    termana_mutex_lock(&channel.mutex);
    CLIPRDR_GENERAL_CAPABILITY_SET general={0};
    general.capabilitySetType=CB_CAPSTYPE_GENERAL; general.capabilitySetLength=CB_CAPSTYPE_GENERAL_LEN;
    general.version=CB_CAPS_VERSION_2; general.generalFlags=CB_USE_LONG_FORMAT_NAMES;
    CLIPRDR_CAPABILITIES caps={0}; caps.common.msgType=CB_CLIP_CAPS;
    caps.cCapabilitiesSets=1; caps.capabilitySets=(CLIPRDR_CAPABILITY_SET*)&general;
    UINT result=context->ClientCapabilities(context,&caps);
    channel.monitor_ready=result==CHANNEL_RC_OK;
    if (result==CHANNEL_RC_OK) result=advertise();
    if (result==CHANNEL_RC_OK && !capabilities()) result=ERROR_INTERNAL_ERROR;
    termana_mutex_unlock(&channel.mutex); return result;
}
static UINT server_caps(CliprdrClientContext* context, const CLIPRDR_CAPABILITIES* caps) {
    (void)context; (void)caps; return CHANNEL_RC_OK;
}
static UINT format_response(CliprdrClientContext* context, const CLIPRDR_FORMAT_LIST_RESPONSE* response) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    BOOL ok=TRUE;
    if (channel.format_count) {
        uint32_t revision=channel.format_revisions[channel.format_head];
        channel.format_head=(channel.format_head+1)%16; channel.format_count--;
        if (revision) ok=emit_number(CLIP_ACK,revision,response->common.msgFlags==CB_RESPONSE_OK);
    }
    termana_mutex_unlock(&channel.mutex); return ok?CHANNEL_RC_OK:ERROR_INTERNAL_ERROR;
}
static UINT formats(CliprdrClientContext* context, const CLIPRDR_FORMAT_LIST* list) {
    if (list->numFormats>128 || (list->numFormats && !list->formats)) return ERROR_INVALID_DATA;
    termana_mutex_lock(&channel.mutex);
    channel.remote_format=0;
    for (UINT32 i=0;i<list->numFormats;i++)
        if (list->formats[i].formatId==CF_UNICODETEXT) channel.remote_format=CF_UNICODETEXT;
    channel.offer=++channel.serial;
    CLIPRDR_FORMAT_LIST_RESPONSE response={0};
    response.common.msgType=CB_FORMAT_LIST_RESPONSE; response.common.msgFlags=CB_RESPONSE_OK;
    UINT result=context->ClientFormatListResponse(context,&response);
    if ((channel.policy&2U) && channel.remote_format &&
        !emit_number(CLIP_OFFER,channel.offer,CF_UNICODETEXT)) result=ERROR_INTERNAL_ERROR;
    termana_mutex_unlock(&channel.mutex); return result;
}
static UINT data_request(CliprdrClientContext* context, const CLIPRDR_FORMAT_DATA_REQUEST* request) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    UINT result=CHANNEL_RC_OK;
    if (channel.local_request) result=ERROR_INVALID_DATA;
    else if (!(channel.policy&1U) || !channel.present || request->requestedFormatId!=CF_UNICODETEXT)
        result=local_response(FALSE,NULL,0);
    else {
        channel.local_request=++channel.serial; channel.local_deadline=GetTickCount64()+5000;
        channel.local_request_revision=channel.local_revision;
        if (!emit_number(CLIP_REQUEST,channel.local_request,channel.local_revision)) result=ERROR_INTERNAL_ERROR;
    }
    termana_mutex_unlock(&channel.mutex); return result;
}
static UINT data_response(CliprdrClientContext* context, const CLIPRDR_FORMAT_DATA_RESPONSE* response) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    uint32_t request=channel.remote_request;
    BOOL valid=request && channel.remote_generation==channel.offer && (channel.policy&2U)
        && response->common.msgFlags==CB_RESPONSE_OK && response->requestedFormatData
        && response->common.dataLen>=2 && response->common.dataLen<=CLIP_TEXT_MAX*2U+2
        && !(response->common.dataLen&1U);
    char* text=NULL; size_t bytes=0;
#ifdef TERMANA_CLIPBOARD_QA_DIAGNOSTICS
    /* Explicit diagnostic build only. Booleans/flags, never clipboard bytes,
     * text, keys, credentials or an unbounded native log. */
    uint32_t qa_flags=(request?1U:0U)|(channel.remote_generation==channel.offer?2U:0U)
        |((channel.policy&2U)?4U:0U)|(response->common.msgFlags==CB_RESPONSE_OK?8U:0U);
    uint32_t qa_padding=0;
    if (response->requestedFormatData && response->common.dataLen>=2 &&
        response->common.dataLen<=CLIP_TEXT_MAX*2U+2 && !(response->common.dataLen&1U)) {
        qa_flags|=16U;
        const WCHAR* qa_wide=(const WCHAR*)response->requestedFormatData;
        size_t qa_count=response->common.dataLen/2;
        if (qa_wide[qa_count-1]==0) qa_flags|=32U;
        if (qa_count>1 && qa_wide[qa_count-1]==0 && qa_wide[qa_count-2]==0) qa_padding=1;
    }
#endif
    if (valid) {
        const WCHAR* wide=(const WCHAR*)response->requestedFormatData;
        size_t count=response->common.dataLen/2;
        size_t length=0;
        while (length<count && wide[length]) length++;
        valid=length<count;
        /* CF_UNICODETEXT ends at the first NUL; Windows may transmit the
         * zero-filled GlobalAlloc tail too (e.g. cmd | clip). Do not interpret
         * padding as embedded text, or expose any nonzero hidden tail. */
        for (size_t i=length;valid && i<count;i++) if (wide[i]) valid=FALSE;
        for (size_t i=0;valid && i<length;i++) {
            if (wide[i]>=0xDC00 && wide[i]<=0xDFFF) valid=FALSE;
            else if (wide[i]>=0xD800 && wide[i]<=0xDBFF) {
                if (i+1>=length || wide[i+1]<0xDC00 || wide[i+1]>0xDFFF) valid=FALSE;
                else i++;
            }
        }
        if (valid && length) {
            text=ConvertWCharNToUtf8Alloc(wide,length,&bytes);
            valid=text && bytes>0 && bytes<=CLIP_TEXT_MAX;
        }
    }
    BYTE payload[CLIP_TEXT_MAX+4]; store32(payload,valid?1U:0U);
#ifdef TERMANA_CLIPBOARD_QA_DIAGNOSTICS
    BYTE diagnostic[24];
    store32(diagnostic,110); store32(diagnostic+4,qa_flags);
    store32(diagnostic+8,111); store32(diagnostic+12,qa_padding);
    store32(diagnostic+16,112); store32(diagnostic+20,valid?1U:0U);
    BOOL diagnosed=channel.emit(108,0,diagnostic,sizeof(diagnostic)); (void)diagnosed;
#endif
    if (valid && bytes) memcpy(payload+4,text,(size_t)bytes);
    BOOL sent=!request || channel.emit(CLIP_TEXT,request,payload,valid?(uint32_t)bytes+4U:4U);
    if (text) { erase(text,(size_t)(bytes>0?bytes:0)); free(text); }
    erase(payload,sizeof(payload)); channel.remote_request=0;
    channel.remote_inflight=FALSE;
    termana_mutex_unlock(&channel.mutex); return sent?CHANNEL_RC_OK:ERROR_INTERNAL_ERROR;
}
static UINT display_caps(DispClientContext* context, UINT32 count, UINT32 a, UINT32 b) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    channel.display_ready=count>=1 && a>0 && b>0;
    channel.display_area=(uint64_t)a*b;
    BOOL ok=capabilities(); termana_mutex_unlock(&channel.mutex);
    return ok?CHANNEL_RC_OK:ERROR_INTERNAL_ERROR;
}
static void connected(void* context, const ChannelConnectedEventArgs* event) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    if (event->pInterface && !strcmp(event->name,CLIPRDR_SVC_CHANNEL_NAME)) {
        channel.clip=event->pInterface;
        channel.clip->custom=&channel;
        channel.clip->MonitorReady=monitor_ready; channel.clip->ServerCapabilities=server_caps;
        channel.clip->ServerFormatList=formats; channel.clip->ServerFormatListResponse=format_response;
        channel.clip->ServerFormatDataRequest=data_request; channel.clip->ServerFormatDataResponse=data_response;
    } else if (event->pInterface && !strcmp(event->name,DISP_DVC_CHANNEL_NAME)) {
        channel.disp=event->pInterface; channel.disp->custom=&channel;
        channel.disp->DisplayControlCaps=display_caps;
    }
    termana_mutex_unlock(&channel.mutex);
}
static void disconnected(void* context, const ChannelDisconnectedEventArgs* event) {
    (void)context;
    termana_mutex_lock(&channel.mutex);
    if (!strcmp(event->name,CLIPRDR_SVC_CHANNEL_NAME)) {
        channel.clip=NULL; channel.monitor_ready=FALSE; channel.policy=0;
        channel.local_request=channel.remote_request=channel.local_revision=0;
        channel.remote_inflight=FALSE;
        channel.format_sent=FALSE; channel.format_count=channel.format_head=0;
    } else if (!strcmp(event->name,DISP_DVC_CHANNEL_NAME)) { channel.disp=NULL; channel.display_ready=FALSE; }
    BOOL sent=capabilities(); (void)sent;
    termana_mutex_unlock(&channel.mutex);
}
void session_channels_init(uint32_t flags, TermanaEmit emit) {
    /* WinPR critical sections are recursive on all supported client systems. */
    if (!termana_mutex_init(&channel.mutex)) abort();
    channel.flags=flags; channel.emit=emit;
}
BOOL session_channels_connect(freerdp* instance) {
    if (PubSub_SubscribeChannelConnected(instance->context->pubSub,connected)<0 ||
        PubSub_SubscribeChannelDisconnected(instance->context->pubSub,disconnected)<0) return FALSE;
    return TRUE;
}
BOOL session_channels_load(freerdp* instance) {
    /* FreeRDP 3.32 replaces its channel manager AFTER PreConnect. Load only
     * through this published callback, or the registered plugins are discarded. */
    if (freerdp_register_addin_provider(freerdp_channels_load_static_addin_entry,0)<0) return FALSE;
    return freerdp_client_load_addins(instance->context->channels,instance->context->settings);
}
BOOL session_channels_command(uint32_t type, uint32_t request, const BYTE* data, uint32_t size) {
    termana_mutex_lock(&channel.mutex);
    BOOL ok=TRUE;
    if (type==CLIP_POLICY && size==4 && read32(data)<=3) {
        channel.policy=read32(data)&((channel.flags>>2)&3U);
        if (!(channel.policy&1U)) { channel.present=FALSE; channel.local_revision=0; }
        ok=advertise()==CHANNEL_RC_OK;
        if (ok && (channel.policy&2U) && channel.remote_format)
            ok=emit_number(CLIP_OFFER,channel.offer,CF_UNICODETEXT);
    } else if (type==CLIP_ANNOUNCE && request && size==4 && read32(data)<=1) {
        channel.local_revision=request; channel.present=(read32(data)!=0) && (channel.policy&1U);
        ok=advertise()==CHANNEL_RC_OK;
    } else if (type==CLIP_READ && !size && request) {
        if (channel.clip && channel.monitor_ready && (channel.policy&2U) &&
            channel.offer==request && channel.remote_format && !channel.remote_inflight) {
            CLIPRDR_FORMAT_DATA_REQUEST wanted={0}; wanted.common.msgType=CB_FORMAT_DATA_REQUEST;
            wanted.requestedFormatId=CF_UNICODETEXT;
            channel.remote_request=channel.remote_generation=request;
            channel.remote_inflight=TRUE;
            channel.remote_deadline=GetTickCount64()+5000;
            ok=channel.clip->ClientFormatDataRequest(channel.clip,&wanted)==CHANNEL_RC_OK;
        } else ok=emit_number(CLIP_TEXT,request,0);
    } else if (type==CLIP_REPLY && size>=4 && size<=CLIP_TEXT_MAX+4 && read32(data)<=1) {
        if (request && request==channel.local_request) {
            ok=local_response((read32(data)!=0) && (channel.policy&1U) && channel.present
                && channel.local_request_revision==channel.local_revision,data+4,size-4)==CHANNEL_RC_OK;
            channel.local_request=0;
        }
    } else if (type==RESIZE && size==8 && !request) {
        UINT32 w=read32(data), h=read32(data+4);
        if (w<200 || w>1920 || h<200 || h>1080 || (w&1U) || !(channel.flags&2U)) ok=FALSE;
        else if (channel.disp && channel.display_ready && (uint64_t)w*h<=channel.display_area) {
            DISPLAY_CONTROL_MONITOR_LAYOUT layout={0}; layout.Flags=DISPLAY_CONTROL_MONITOR_PRIMARY;
            layout.Width=w; layout.Height=h; layout.PhysicalWidth=0; layout.PhysicalHeight=0;
            layout.DesktopScaleFactor=100; layout.DeviceScaleFactor=100;
            ok=channel.disp->SendMonitorLayout(channel.disp,1,&layout)==CHANNEL_RC_OK;
        }
    } else ok=FALSE;
    termana_mutex_unlock(&channel.mutex); return ok;
}
BOOL session_channels_poll(void) {
    termana_mutex_lock(&channel.mutex);
    BOOL ok=TRUE; uint64_t now=GetTickCount64();
    if (channel.local_request && now>channel.local_deadline) {
        channel.local_request=0; ok=local_response(FALSE,NULL,0)==CHANNEL_RC_OK;
    }
    if (channel.remote_request && now>channel.remote_deadline) {
        uint32_t request=channel.remote_request; channel.remote_request=0;
        ok=emit_number(CLIP_TEXT,request,0) && ok;
    }
    termana_mutex_unlock(&channel.mutex); return ok;
}
void session_channels_unsubscribe(rdpContext* context) {
    PubSub_UnsubscribeChannelConnected(context->pubSub,connected);
    PubSub_UnsubscribeChannelDisconnected(context->pubSub,disconnected);
}
void session_channels_free(void) { termana_mutex_free(&channel.mutex); erase(&channel,sizeof(channel)); }

/* Offline published-interface mocks: no server, native clipboard or user data. */
static uint32_t test_type,test_value,test_size,test_advertised,test_width;
static BOOL test_emit(uint32_t type,uint32_t request,const BYTE* data,uint32_t size) {
    (void)request; test_type=type; test_value=size>=4?read32(data):0; test_size=size; return TRUE;
}
static UINT test_list(CliprdrClientContext* context,const CLIPRDR_FORMAT_LIST* list) {
    (void)context; test_advertised=list->numFormats; return CHANNEL_RC_OK;
}
static UINT test_response(CliprdrClientContext* context,const CLIPRDR_FORMAT_DATA_RESPONSE* response) {
    (void)context; test_value=response->common.msgFlags; test_size=response->common.dataLen;
    return CHANNEL_RC_OK;
}
static UINT test_layout(DispClientContext* context,UINT32 count,DISPLAY_CONTROL_MONITOR_LAYOUT* layout) {
    (void)context; test_width=count==1?layout->Width:0; return CHANNEL_RC_OK;
}
BOOL session_channels_self_test(void) {
    session_channels_init(14,test_emit);
    CliprdrClientContext clip={0}; DispClientContext disp={0};
    clip.ClientFormatList=test_list; clip.ClientFormatDataResponse=test_response;
    disp.SendMonitorLayout=test_layout;
    channel.clip=&clip; channel.monitor_ready=TRUE;
    channel.disp=&disp; channel.display_ready=TRUE; channel.display_area=1920U*1080U;
    BYTE number[8]; store32(number,1);
    BOOL valid=session_channels_command(CLIP_POLICY,0,number,4);
    valid=valid && session_channels_command(CLIP_ANNOUNCE,9,number,4) && test_advertised==1;
    CLIPRDR_FORMAT_LIST_RESPONSE acknowledgement={0}; acknowledgement.common.msgFlags=CB_RESPONSE_OK;
    valid=valid && format_response(&clip,&acknowledgement)==CHANNEL_RC_OK &&
        format_response(&clip,&acknowledgement)==CHANNEL_RC_OK && test_type==CLIP_ACK && test_value==1;
    CLIPRDR_FORMAT_DATA_REQUEST request={0}; request.requestedFormatId=CF_UNICODETEXT;
    valid=valid && data_request(&clip,&request)==CHANNEL_RC_OK && test_type==CLIP_REQUEST;
    BYTE reply[]={0,0,0,1,0xCE,0xA9};
    valid=valid && session_channels_command(CLIP_REPLY,channel.local_request,reply,sizeof(reply)) &&
        test_value==CB_RESPONSE_OK && test_size==4;
    store32(number,0);
    /* Later empty format lists are suppressed by FreeRDP. Access itself must
     * still be denied even if Windows retains the old format advertisement. */
    valid=valid && session_channels_command(CLIP_POLICY,0,number,4) && test_advertised==1 &&
        data_request(&clip,&request)==CHANNEL_RC_OK && test_value==CB_RESPONSE_FAIL;
    channel.policy=2; channel.offer=channel.remote_request=channel.remote_generation=21;
    channel.remote_inflight=TRUE;
    const WCHAR text[]={'Q',0x03A9,0};
    CLIPRDR_FORMAT_DATA_RESPONSE response={0}; response.common.msgFlags=CB_RESPONSE_OK;
    response.common.dataLen=sizeof(text); response.requestedFormatData=(const BYTE*)text;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_type==CLIP_TEXT &&
        test_value==1 && test_size==7 && !channel.remote_inflight;
    channel.remote_request=channel.remote_generation=channel.offer=23;
    const WCHAR padded[]={'Q',0x03A9,0,0,0};
    response.common.dataLen=sizeof(padded); response.requestedFormatData=(const BYTE*)padded;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==1 && test_size==7;
    channel.remote_request=channel.remote_generation=channel.offer=24;
    const WCHAR hidden[]={'Q',0,'X',0};
    response.common.dataLen=sizeof(hidden); response.requestedFormatData=(const BYTE*)hidden;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==0 && test_size==4;
    channel.remote_request=channel.remote_generation=channel.offer=25;
    const WCHAR empty[]={0,0};
    response.common.dataLen=sizeof(empty); response.requestedFormatData=(const BYTE*)empty;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==1 && test_size==4;
    channel.remote_request=channel.remote_generation=channel.offer=26;
    const WCHAR unterminated[]={'Q',0x03A9};
    response.common.dataLen=sizeof(unterminated); response.requestedFormatData=(const BYTE*)unterminated;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==0 && test_size==4;
    channel.remote_request=channel.remote_generation=channel.offer=27;
    const WCHAR supplementary[]={'X',0xD83D,0xDE00,0,0};
    response.common.dataLen=sizeof(supplementary); response.requestedFormatData=(const BYTE*)supplementary;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==1 && test_size==9;
    channel.remote_request=channel.remote_generation=channel.offer=22;
    const WCHAR bad[]={0xD800,0};
    response.common.dataLen=sizeof(bad); response.requestedFormatData=(const BYTE*)bad;
    valid=valid && data_response(&clip,&response)==CHANNEL_RC_OK && test_value==0 && test_size==4;
    store32(number,801); store32(number+4,600);
    valid=valid && !session_channels_command(RESIZE,0,number,8);
    store32(number,800);
    valid=valid && session_channels_command(RESIZE,0,number,8) && test_width==800;
    session_channels_free(); return valid;
}
