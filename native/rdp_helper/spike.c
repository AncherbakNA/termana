/* Offline FreeRDP ABI/settings/pixel feasibility check. Not a working RDP client.
 * No freerdp_connect call, server argument, password input or fallback client.
 */
#include <freerdp/config.h>
#include <freerdp/codec/color.h>
#include <freerdp/freerdp.h>
#include <freerdp/settings.h>
#include <freerdp/utils/helpers.h>
#include <freerdp/version.h>
#include <stdio.h>
#include <string.h>

static BOOL set_policy(rdpSettings* settings)
{
    const size_t denied[] = {
        FreeRDP_RdpSecurity, FreeRDP_TlsSecurity, FreeRDP_ExtSecurity,
        FreeRDP_IgnoreCertificate, FreeRDP_AutoAcceptCertificate,
        FreeRDP_ExternalCertificateManagement, FreeRDP_GatewayEnabled,
        FreeRDP_GatewayUseSameCredentials, FreeRDP_AutoReconnectionEnabled,
        FreeRDP_RedirectClipboard, FreeRDP_RedirectDrives, FreeRDP_RedirectPrinters,
        FreeRDP_RedirectSmartCards, FreeRDP_SmartcardLogon,
    };
    for (size_t i = 0; i < sizeof(denied) / sizeof(denied[0]); ++i)
    {
        if (!freerdp_settings_set_bool(settings, denied[i], FALSE) ||
            freerdp_settings_get_bool(settings, denied[i]))
            return FALSE;
    }
    return freerdp_settings_set_bool(settings, FreeRDP_NlaSecurity, TRUE) &&
        freerdp_settings_set_bool(settings, FreeRDP_Authentication, TRUE) &&
        freerdp_settings_set_bool(settings, FreeRDP_DeactivateClientDecoding, FALSE) &&
        freerdp_settings_get_bool(settings, FreeRDP_NlaSecurity) &&
        freerdp_settings_get_bool(settings, FreeRDP_Authentication) &&
        !freerdp_settings_get_bool(settings, FreeRDP_DeactivateClientDecoding);
}

static BOOL check_pixels(void)
{
    /* Convert through the real engine, not a guessed XRGB32/Qt name mapping. */
    const BYTE source[] = {255, 0, 0, 255, 0, 255, 0, 255,
                           0, 0, 255, 255, 255, 255, 255, 255};
    BYTE native[16] = {0};
    BYTE output[16] = {0};
    if (!freerdp_image_copy(native, PIXEL_FORMAT_XRGB32, 8, 0, 0, 2, 2,
                            source, PIXEL_FORMAT_RGBX32, 8, 0, 0, NULL, 0))
        return FALSE;
    for (size_t i = 0; i < 4; ++i)
    {
        BYTE r = 0, g = 0, b = 0, a = 0;
        const UINT32 color = FreeRDPReadColor(&native[i * 4], PIXEL_FORMAT_XRGB32);
        FreeRDPSplitColor(color, PIXEL_FORMAT_XRGB32, &r, &g, &b, &a, NULL);
        if (r != source[i * 4] || g != source[i * 4 + 1] || b != source[i * 4 + 2])
            return FALSE;
    }
    if (!freerdp_image_copy(output, PIXEL_FORMAT_RGBX32, 8, 0, 0, 2, 2,
                            native, PIXEL_FORMAT_XRGB32, 8, 0, 0, NULL, 0))
        return FALSE;
    for (size_t i = 0; i < sizeof(output); ++i)
    {
        /* The unused X byte is not alpha and is intentionally unspecified. */
        if (i % 4 != 3 && output[i] != source[i])
            return FALSE;
    }
    return TRUE;
}

int main(int argc, char** argv)
{
    int result = 1;
    rdpSettings* settings = NULL;
    freerdp* instance = NULL;
    if (argc != 2 || strcmp(argv[1], "--self-test") != 0)
    {
        fputs("Usage: termana-rdp-spike --self-test (offline only)\n", stderr);
        return 64;
    }
    if (strcmp(freerdp_get_version_string(), "3.32.2") != 0 ||
        !freerdp_setApplicationDetails("io.github.ancherbakna", "termana-rdp-spike", 1))
        goto cleanup;
    settings = freerdp_settings_new(0);
    if (!settings || !set_policy(settings) || !check_pixels())
        goto cleanup;
    freerdp_settings_free(settings);
    settings = NULL;
    instance = freerdp_new();
    if (!instance)
        goto cleanup;
    if (!freerdp_context_new(instance))
        goto cleanup;
    /* Offline context allocation only: no connect, certificate decisions,
     * scripts or credentials. Private live config/TLS initialization ordering
     * is deliberately NOT advertised as verified by this feasibility check.
     */
    if (!set_policy(instance->context->settings))
        goto cleanup;
    puts("{\"component\":\"termana-rdp-spike\",\"engine\":\"3.32.2\","
         "\"mode\":\"offline-self-test\",\"connect\":false,"
         "\"context\":true,\"requested_nla_only\":true,\"rgbx_roundtrip\":true}");
    result = 0;
cleanup:
    if (instance)
    {
        freerdp_context_free(instance);
        freerdp_free(instance);
    }
    freerdp_settings_free(settings);
    if (result != 0)
        fputs("Offline RDP component self-test failed\n", stderr);
    return result;
}
