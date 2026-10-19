"""Minimal UPnP IGD helper. Set OPENSEEK_UPNP_BASE to the router's UPnP control root (e.g. http://192.168.x.1:49152) and
OPENSEEK_UPNP_DESC to its device-description path (default /rootDesc.xml). Usage: upnp_forward.py ip|list|add EXT LANIP PORT|del EXT"""
import os, re, sys, urllib.request
BASE = os.environ.get("OPENSEEK_UPNP_BASE", "")
if not BASE:
    sys.exit("set OPENSEEK_UPNP_BASE to your router's UPnP root URL, e.g. http://192.168.x.1:49152")
DESC = os.environ.get("OPENSEEK_UPNP_DESC", "/rootDesc.xml")
def get(u):
    return urllib.request.urlopen(u, timeout=8).read().decode(errors="replace")
desc = get(BASE + DESC)
svc = re.findall(r"<service>.*?</service>", desc, re.S)
for s in svc:
    t = re.search(r"<serviceType>(.*?)</serviceType>", s).group(1)
    if "WANIPConnection" in t or "WANPPPConnection" in t:
        ctl = re.search(r"<controlURL>(.*?)</controlURL>", s).group(1); stype = t
print("service:", stype, ctl)
def soap(action, args=""):
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{stype}">{args}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(BASE + ctl, body, {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'})
    try:
        return urllib.request.urlopen(req, timeout=10).read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return "HTTP %d: %s" % (e.code, e.read().decode(errors="replace")[:400])
if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "ip":
        print(re.findall(r"<NewExternalIPAddress>(.*?)<", soap("GetExternalIPAddress")))
    elif cmd == "list":
        for i in range(0, 30):
            r = soap("GetGenericPortMappingEntry", f"<NewPortMappingIndex>{i}</NewPortMappingIndex>")
            if r.startswith("HTTP"): print("end:", r[:120]); break
            g = lambda k: (re.search(f"<New{k}>(.*?)</New{k}>", r) or [None, ""])[1]
            print(i, g("ExternalPort"), g("Protocol"), g("InternalClient"), g("InternalPort"), g("PortMappingDescription"), "lease", g("LeaseDuration"), "enabled", g("Enabled"))
    elif cmd == "add":
        ext, ip, port = sys.argv[2:5]
        a = (f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{ext}</NewExternalPort><NewProtocol>TCP</NewProtocol><NewInternalPort>{port}</NewInternalPort>"
             f"<NewInternalClient>{ip}</NewInternalClient><NewEnabled>1</NewEnabled><NewPortMappingDescription>OpenSeek guest endpoint</NewPortMappingDescription><NewLeaseDuration>0</NewLeaseDuration>")
        print(soap("AddPortMapping", a)[:300])
    elif cmd == "del":
        print(soap("DeletePortMapping", f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{sys.argv[2]}</NewExternalPort><NewProtocol>TCP</NewProtocol>")[:300])
