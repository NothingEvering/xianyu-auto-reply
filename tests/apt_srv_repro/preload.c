#define _GNU_SOURCE
#include <dlfcn.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>

/* 只改写 apt 实际连上的 SRV 目标，让它打到本测试的 500 端口。
   deb.debian.org 的地址解析保持系统解析器，不改主机 DNS。 */
static int srv_target(const char *node) {
    return node && (
        strcmp(node, "debian.map.fastlydns.net") == 0 ||
        strcmp(node, "debian.map.fastly.net") == 0
    );
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    static int (*real_getaddrinfo)(const char *, const char *, const struct addrinfo *, struct addrinfo **) = NULL;
    const char *port;
    if (!real_getaddrinfo)
        real_getaddrinfo = dlsym(RTLD_NEXT, "getaddrinfo");
    if (!srv_target(node))
        return real_getaddrinfo(node, service, hints, res);
    port = getenv("APT_SRV_RELAY_PORT");
    if (!port || !port[0])
        port = "18080";
    return real_getaddrinfo("127.0.0.1", port, hints, res);
}
