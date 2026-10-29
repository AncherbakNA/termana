"""Small public explanations for pinned FreeRDP3.32.2 end codes; no raw engine text."""


def end_explanation(code):
    if type(code) is not int or not 0<=code<=0xFFFFFFFF:
        raise ValueError("Invalid RDP end code")
    if code==0:
        return "The remote session ended. Reconnect when you are ready."
    messages = {
        0x20004: "The server name could not be resolved. Check the address and DNS.",
        0x20005: "The server name could not be resolved. Check the address and DNS.",
        0x20006: "The server could not be reached. Check the address, port and network.",
        0x20008: "A secure connection could not be established. Check the server TLS settings.",
        0x20009: "Sign-in failed. Check the username, domain and password.",
        0x2000A: "This account does not have permission to connect to this desktop.",
        0x2000B: "The connection was canceled.",
        0x2000C: "Secure connection negotiation failed. This client requires NLA.",
        0x2000D: "The connection was interrupted. Check the network, then reconnect.",
        0x2000E: "The password has expired. Update it through your administrator, then reconnect.",
        0x2000F: "The password has expired. Update it through your administrator, then reconnect.",
        0x20012: "This account is disabled. Contact your administrator.",
        0x20013: "This account requires a password change before it can connect.",
        0x20014: "Sign-in failed. Check the username, domain and password.",
        0x20015: "Sign-in failed. Check the username, domain and password.",
        0x20016: "This account does not have permission to connect to this desktop.",
        0x20017: "An account policy prevents this connection. Contact your administrator.",
        0x20018: "This account is locked. Contact your administrator before trying again.",
        0x20019: "This account has expired. Contact your administrator.",
        0x2001A: "This account is not allowed to sign in through Remote Desktop.",
        0x2001B: "Windows did not receive usable credentials. Sign in again.",
        0x2001C: "Windows did not start the desktop in time. Try reconnecting later.",
        0x2001D: "Windows is still starting. Try reconnecting shortly.",
    }
    return messages.get(code,"The remote session ended unexpectedly. Reconnect to try again.")
